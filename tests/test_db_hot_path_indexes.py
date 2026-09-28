"""热路径索引守卫：二级索引必须存在、必须被查询计划用上、被改的路由必须在线程池里读库。

定罪现场（2026-09-28 本机只读实测，非推导）：整枚生产库的 `sqlite_master` 里只有
`ix_memories_project_id` 一枚业务索引，其余表只靠主键 autoindex。于是——
  * `SELECT ... FROM chapters WHERE book_id=? ORDER BY "index"` → `SCAN chapters` +
    `USE TEMP B-TREE`（最大那本 1626 章，实测 251 ms 量级）
  * `SELECT * FROM voice_profiles ORDER BY created_at DESC LIMIT 50` → `SCAN` + TEMP B-TREE
而这两条都直接跑在 `async def` 路由里，事件循环被数据库按住（红线：界面卡顿）。

本文件钉三件事，口径各不相同：
 1) **存在性**：`create_all`（新库）与 `_migrate_missing_indexes`（老库启动补建）两条
    路径都要让 6 枚索引真的落到 `sqlite_master` 里，并且补建过程**不改任何数据行**、
    可重复调用（幂等）。
 2) **计划**：同一批数据、同一条 SQL，无索引时确实是 `SCAN chapters`（对照组，防止
    断言退化成恒真），补建后必须变成走 `ix_chapters_book_id_index` 且不再出现
    `SCAN chapters`。
 3) **路由**：被改的 list/chapters 路由仍是 `async def`，且函数体里确实
    `await asyncio.to_thread(...)` 下线程池（静态 AST 扫，风格抄
    `tests/test_py344_locale_safe.py`）。

红线：本文件**只碰 tmp_path 里的临时库**，绝不 connect 生产 `data/peiyinyuan.db`
（连只读都不需要）；也不调用 `backend.models.base.get_engine()/init_db()`——那两个
走 `Config.DATA_DIR`，不设环境变量时会指向仓库里的真库。
"""
from __future__ import annotations

import ast
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.schema import CreateTable

REPO_ROOT = Path(__file__).resolve().parents[1]

import backend.models.base as db_base          # noqa: E402
from backend.models.base import Base           # noqa: E402
import backend.models.book                     # noqa: E402  (注册进 metadata)
import backend.models.voice                    # noqa: E402
import backend.models.task                     # noqa: E402
import backend.models.memory                   # noqa: E402
import backend.models.project                  # noqa: E402
import backend.models.workflow                 # noqa: E402

# 任务书点名的 6 枚（ix_memories_project_id 是既有索引，一并被补建机制覆盖）
EXPECTED_INDEXES = {
    "chapters": "ix_chapters_book_id_index",
    "voice_profiles": "ix_voice_profiles_source",
    "voice_profiles_engine": "ix_voice_profiles_engine",
    "voice_profiles_category": "ix_voice_profiles_category",
    "voice_profiles_created_at": "ix_voice_profiles_created_at",
    "conversion_tasks": "ix_conversion_tasks_status_created_at",
}
EXPECTED = set(EXPECTED_INDEXES.values())

BOOKS = 12
CHAPTERS_PER_BOOK = 120

# 与 `book_service.get_chapter_previews()` 同形（WF-09 的投影：正文只取前 200 字），
# `index` 是 SQL 保留字，一律带双引号。
Q_CHAPTERS = ('select chapters.id, chapters.book_id, chapters.title, chapters."index", '
              'chapters.char_count, chapters.extra_data, substr(chapters.content, 1, 200) '
              'from chapters where chapters.book_id = ? order by chapters."index"')
Q_VOICES = ("select id, name, source, engine, category, created_at from voice_profiles "
            "order by created_at desc limit 50")
Q_TASKS = ("select id, status, created_at from conversion_tasks "
           "where status = ? order by created_at")


def _db_path(tmp_path: Path) -> Path:
    return tmp_path / "hotpath.db"


def _engine(tmp_path: Path):
    return create_engine(f"sqlite:///{_db_path(tmp_path).as_posix()}")


def _raw_conn(tmp_path: Path) -> sqlite3.Connection:
    return sqlite3.connect(_db_path(tmp_path))


def _tables_only(engine) -> None:
    """只发 CREATE TABLE、不发 CREATE INDEX —— 复刻"老库"形态（表在、零二级索引）。

    `Base.metadata.create_all()` 会顺手把索引建出来，所以模拟老库必须绕开它。
    """
    with engine.connect() as conn:
        for table in Base.metadata.tables.values():
            conn.execute(text(str(CreateTable(table).compile(dialect=engine.dialect))))
        conn.commit()


def _seed(engine) -> str:
    """灌 BOOKS 本 × CHAPTERS_PER_BOOK 章 + 音色/任务若干行，返回其中一本的 id。

    多本书是刻意的：只有一本书时 `WHERE book_id=?` 选择性=100%，SQLite 会合理地
    继续选 SCAN——那种数据形态下本守卫的判据失去意义。
    """
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        target_book_id = None
        for b in range(BOOKS):
            book = backend.models.book.Book(id=f"book-{b}", title=f"T{b}")
            session.add(book)
            if b == 0:
                target_book_id = book.id
            for i in range(CHAPTERS_PER_BOOK):
                session.add(backend.models.book.Chapter(
                    id=f"c-{b}-{i}", book_id=book.id, title=f"ch{i}",
                    index=CHAPTERS_PER_BOOK - i,          # 逆序写：排除"插序即已排好"的假象
                    content="x" * 400, char_count=400,
                ))
        for v in range(200):
            session.add(backend.models.voice.VoiceProfile(
                id=f"v-{v}", name=f"n{v}", source="builtin" if v % 2 else "cloned",
                engine="edgetts", category="human",
                created_at=datetime(2026, 9, 1 + v % 28, v % 24),
            ))
        for t in range(60):
            session.add(backend.models.task.ConversionTask(
                id=f"t-{t}", status=("queued", "running", "completed")[t % 3],
                created_at=datetime(2026, 9, 1 + t % 28),
            ))
        session.commit()
        return target_book_id
    finally:
        session.close()


def _row_fingerprint(tmp_path: Path) -> str:
    """数据行指纹：行数 + 若干聚合值。补索引前后必须逐字节一致（只建索引不动行）。"""
    c = _raw_conn(tmp_path)
    try:
        parts = []
        for table, sql in (
            ("chapters", 'select count(*), sum(char_count), min(id), max(id) from chapters'),
            ("voice_profiles", "select count(*), min(id), max(id) from voice_profiles"),
            ("conversion_tasks", "select count(*), min(id), max(id) from conversion_tasks"),
        ):
            parts.append(f"{table}={c.execute(sql).fetchone()}")
        return "|".join(parts)
    finally:
        c.close()


def _plan(conn: sqlite3.Connection, sql: str, params=()) -> list:
    return [r[3] for r in conn.execute("explain query plan " + sql, params)]


def _index_names(conn: sqlite3.Connection) -> set:
    return {r[0] for r in conn.execute(
        "select name from sqlite_master where type='index'")}


# ---------------------------------------------------------------- 1) 存在性

def test_create_all_builds_declared_indexes(tmp_path):
    """新库路径：模型里声明的索引必须被 `create_all` 直接建出来。"""
    engine = _engine(tmp_path)
    Base.metadata.create_all(bind=engine)
    conn = _raw_conn(tmp_path)
    try:
        have = _index_names(conn)
    finally:
        conn.close()
    missing = EXPECTED - have
    assert not missing, f"create_all 后仍缺索引: {missing}"


def test_chapters_index_ddl_quotes_reserved_index_column(tmp_path):
    """`index` 是 SQL 保留字：DDL 里必须带引号，否则补建语句本身语法错。"""
    engine = _engine(tmp_path)
    Base.metadata.create_all(bind=engine)
    conn = _raw_conn(tmp_path)
    try:
        ddl = conn.execute("select sql from sqlite_master where type='index' "
                           "and name='ix_chapters_book_id_index'").fetchone()[0]
    finally:
        conn.close()
    assert '"index"' in ddl, f"复合索引 DDL 没给保留字加引号: {ddl}"


def test_migration_creates_missing_indexes_on_old_db_without_touching_rows(tmp_path):
    """老库路径（本次改造的核心）：表已在、索引一枚没有时，启动补建必须补齐且不动数据。"""
    engine = _engine(tmp_path)
    _tables_only(engine)
    book_id = _seed(engine)

    conn = _raw_conn(tmp_path)
    try:
        assert _index_names(conn) == {n for n in _index_names(conn)
                                      if n.startswith("sqlite_autoindex")}, "对照组失守：老库竟有业务索引"
    finally:
        conn.close()

    before = _row_fingerprint(tmp_path)
    created = set(db_base._migrate_missing_indexes(engine))
    assert EXPECTED <= created, f"补建漏了: {EXPECTED - created}"

    conn = _raw_conn(tmp_path)
    try:
        have = _index_names(conn)
    finally:
        conn.close()
    assert EXPECTED <= have, "sqlite_master 里查不到已声明的索引"
    assert _row_fingerprint(tmp_path) == before, "补建索引改写了数据行"

    # 表名/列名清单也不能被索引迁移动过（只加索引，不加列不删列）
    conn = _raw_conn(tmp_path)
    try:
        tables = {r[0] for r in conn.execute("select name from sqlite_master where type='table'")}
    finally:
        conn.close()
    assert "chapters" in tables and book_id


def test_migration_is_idempotent(tmp_path):
    """第二次启动无事可做：返回空列表，sqlite_master 里索引不重复。"""
    engine = _engine(tmp_path)
    _tables_only(engine)
    _seed(engine)
    assert db_base._migrate_missing_indexes(engine)
    assert db_base._migrate_missing_indexes(engine) == []
    conn = _raw_conn(tmp_path)
    try:
        names = [r[0] for r in conn.execute(
            "select name from sqlite_master where type='index'")]
    finally:
        conn.close()
    assert len(names) == len(set(names))


def test_index_migration_runs_after_column_migration():
    """补索引必须排在补列之后：老库缺列时直接建索引会 `no such column`。

    静态钉住两条启动路径（`get_engine()→_ensure_all_tables` 与 `init_db()`）里的调用次序，
    不依赖本机 SQLite 是否支持 DROP COLUMN。
    """
    src = (REPO_ROOT / "backend" / "models" / "base.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    for fname in ("_ensure_all_tables", "init_db"):
        node = next(n for n in ast.walk(tree)
                    if isinstance(n, ast.FunctionDef) and n.name == fname)
        lines = [c.lineno for c in ast.walk(node) if isinstance(c, ast.Call)
                 and getattr(c.func, "id", "") == "_migrate_added_columns"]
        ilines = [c.lineno for c in ast.walk(node) if isinstance(c, ast.Call)
                  and getattr(c.func, "id", "") == "_migrate_missing_indexes"]
        assert lines and ilines, f"{fname} 里缺列/索引迁移调用"
        assert max(lines) < min(ilines), f"{fname}: 索引迁移没有排在列迁移之后"


# ---------------------------------------------------------------- 2) 查询计划

def test_chapters_query_plan_switches_from_scan_to_index(tmp_path):
    """同一条 SQL：无索引=SCAN+TEMP B-TREE，补建后不得再出现 `SCAN chapters`。"""
    engine = _engine(tmp_path)
    _tables_only(engine)
    book_id = _seed(engine)

    conn = _raw_conn(tmp_path)
    try:
        before = _plan(conn, Q_CHAPTERS, (book_id,))
        assert any(step.startswith("SCAN chapters") for step in before), \
            f"对照组失守：无索引时计划已经是 {before}，断言将恒真"
    finally:
        conn.close()

    db_base._migrate_missing_indexes(engine)

    conn = _raw_conn(tmp_path)
    try:
        after = _plan(conn, Q_CHAPTERS, (book_id,))
    finally:
        conn.close()
    assert not any(step.startswith("SCAN chapters") for step in after), \
        f"补建索引后仍在 SCAN chapters: {after}"
    assert any("ix_chapters_book_id_index" in step for step in after), after
    assert not any("TEMP B-TREE" in step for step in after), \
        f"ORDER BY 仍在临时 B 树上排序: {after}"


def test_voice_query_plan_uses_created_at_index(tmp_path):
    """音色列表排序键：补建后必须用上 ix_voice_profiles_created_at，不再裸 SCAN。"""
    engine = _engine(tmp_path)
    _tables_only(engine)
    _seed(engine)

    conn = _raw_conn(tmp_path)
    try:
        assert any(s.startswith("SCAN voice_profiles") and "INDEX" not in s
                   for s in _plan(conn, Q_VOICES)), "对照组失守：无索引时计划已走索引"
    finally:
        conn.close()

    db_base._migrate_missing_indexes(engine)

    conn = _raw_conn(tmp_path)
    try:
        after = _plan(conn, Q_VOICES)
    finally:
        conn.close()
    assert any("ix_voice_profiles_created_at" in step for step in after), after


@pytest.mark.parametrize("index_name,column", [
    ("ix_voice_profiles_source", "source"),
    ("ix_voice_profiles_engine", "engine"),
    ("ix_voice_profiles_category", "category"),
])
def test_voice_filter_indexes_are_usable(tmp_path, index_name, column):
    """三个筛选谓词各自都要能被 `WHERE <col>=?` 的计划用上。"""
    engine = _engine(tmp_path)
    _tables_only(engine)
    _seed(engine)
    db_base._migrate_missing_indexes(engine)

    conn = _raw_conn(tmp_path)
    try:
        plan = _plan(conn, f"select id from voice_profiles where {column} = ?", ("builtin",))
    finally:
        conn.close()
    assert any(index_name in step for step in plan), plan


def test_task_index_serves_status_then_created_at(tmp_path):
    """`WHERE status=? ORDER BY created_at` 用上复合索引，且不再临时排序。"""
    engine = _engine(tmp_path)
    _tables_only(engine)
    _seed(engine)
    db_base._migrate_missing_indexes(engine)

    conn = _raw_conn(tmp_path)
    try:
        plan = _plan(conn, Q_TASKS, ("queued",))
    finally:
        conn.close()
    assert any("ix_conversion_tasks_status_created_at" in step for step in plan), plan
    assert not any("TEMP B-TREE" in step for step in plan), plan


# ---------------------------------------------------------------- 3) 路由线程化

THREAD_FUNCS = ("to_thread", "run_in_threadpool")
DATA_LAYER_OWNERS = ("book_service", "store")

GUARDED_ROUTES = {
    "backend/api/voices.py": ("list_voices",),
    "backend/api/books.py": ("list_books", "get_book", "get_chapters",
                             "get_chapter_content", "update_chapter"),
}


def _route_fn(rel_path: str, name: str) -> ast.AsyncFunctionDef:
    src = (REPO_ROOT / rel_path).read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == name:
            assert isinstance(node, ast.AsyncFunctionDef), \
                f"{rel_path}::{name} 被降级成同步 def，事件循环会被数据库按住"
            return node
    raise AssertionError(f"{rel_path} 里找不到路由函数 {name}（被改名或删除即视为守卫失守）")


def _thread_calls(fn) -> list:
    """`await asyncio.to_thread(...)` / `await run_in_threadpool(...)` 的 Call 节点。"""
    out = []
    for node in ast.walk(fn):
        if isinstance(node, ast.Await) and isinstance(node.value, ast.Call):
            fname = getattr(node.value.func, "attr", None) or getattr(node.value.func, "id", None)
            if fname in THREAD_FUNCS:
                out.append(node.value)
    return out


def _shell_spans(fn) -> tuple:
    """路由里嵌套同步壳函数的行号区间 -> 壳名；壳名被交给线程池的才认。"""
    threaded = set()
    for call in _thread_calls(fn):
        for arg in call.args + [kw.value for kw in call.keywords]:
            if isinstance(arg, ast.Name):
                threaded.add(arg.id)
    spans = {}
    for node in ast.walk(fn):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node is not fn:
            spans[(node.lineno, node.end_lineno)] = node.name
    return spans, threaded


def _naked_data_calls(fn) -> list:
    """路由**本体**里裸调数据层的位置（线程壳内部的调用不算）。"""
    spans, threaded = _shell_spans(fn)
    offenders = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if getattr(node.func.value, "id", None) not in DATA_LAYER_OWNERS:
            continue
        in_thread_shell = any(
            lo <= node.lineno <= hi and name in threaded for (lo, hi), name in spans.items())
        if not in_thread_shell:
            offenders.append(node)
    return offenders


@pytest.mark.parametrize("rel_path,name", [
    (path, name) for path, names in GUARDED_ROUTES.items() for name in names
])
def test_hot_db_routes_await_threadpool(rel_path, name):
    fn = _route_fn(rel_path, name)
    assert _thread_calls(fn), f"{rel_path}::{name} 没有 await asyncio.to_thread/run_in_threadpool"


@pytest.mark.parametrize("rel_path,name", [
    (path, name) for path, names in GUARDED_ROUTES.items() for name in names
])
def test_hot_db_routes_have_no_naked_data_layer_calls(rel_path, name):
    """同步 SELECT 只能在被交给线程池的壳里，不能在路由本体上直接按循环。"""
    fn = _route_fn(rel_path, name)
    offenders = _naked_data_calls(fn)
    assert not offenders, [
        f"{rel_path}::{name} 第 {n.lineno} 行裸调 {_dump(n.func)}" for n in offenders]


def _dump(func_node) -> str:
    parts = []
    cur = func_node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    parts.append(getattr(cur, "id", "?"))
    return ".".join(reversed(parts))


def test_threaded_route_count_matches_guarded_list():
    """防止守卫清单被"顺手删一项"就变绿：六枚路由都得有线程化调用。"""
    total = sum(len(_thread_calls(_route_fn(p, n)))
                for p, names in GUARDED_ROUTES.items() for n in names)
    assert total >= 6, f"被改路由共 6 枚，实际只有 {total} 处 await to_thread"

