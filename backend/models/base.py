"""
数据库基础配置 - SQLAlchemy ORM + SQLite
"""

import logging
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker, declarative_base

logger = logging.getLogger("peiyinyuan.db")

# 声明基类
Base = declarative_base()

# 全局引擎和会话工厂
_engine = None
_SessionLocal = None

# F-R4-3: 最近一次启动补齐的列（"table.column" 列表），供 init_db()/startup 日志打印
LAST_ADDED_COLUMNS: list = []
# PERF-IDX: 最近一次启动补建的索引名，口径与 LAST_ADDED_COLUMNS 一致
LAST_ADDED_INDEXES: list = []


def get_db_path() -> str:
    """获取数据库文件路径。

    PY-366：以前这里硬写 `<repo>/data`，等于绕开了 data 根的唯一入口 ——
    跑一次全量回归门就在这个检出树里 `mkdir` + 建出 `peiyinyuan.db`(-wal/-shm)。
    现在走 `Config.DATA_DIR`，`PEIYINYUAN_DATA_DIR` 一设就整体指到 scratch；
    不设时路径与旧实现逐字节一致。
    """
    from backend.config import Config

    data_dir = Path(Config.DATA_DIR)
    data_dir.mkdir(parents=True, exist_ok=True)
    return str(data_dir / "peiyinyuan.db")


def get_engine():
    """获取数据库引擎（单例，支持连接池 + WAL 模式）"""
    global _engine
    if _engine is None:
        from sqlalchemy import event
        db_path = get_db_path()
        _engine = create_engine(
            f"sqlite:///{db_path}",
            connect_args={"check_same_thread": False},
            echo=False,
            pool_size=10,           # 连接池大小
            max_overflow=20,        # 溢出连接数
            pool_pre_ping=True,     # 连接前检查有效性
            pool_recycle=3600,      # 1小时回收连接
        )

        # 启用 WAL 模式（大幅提升并发读性能）
        @event.listens_for(_engine, "connect")
        def _set_sqlite_pragma(dbapi_connection, connection_record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL;")
            cursor.execute("PRAGMA synchronous=NORMAL;")
            cursor.execute("PRAGMA busy_timeout=5000;")
            cursor.close()

        # 确保所有表都被创建（包括后续新增的表）
        _ensure_all_tables(_engine)
    return _engine


def _ensure_all_tables(engine):
    """确保所有模型表都被创建（支持增量添加新表）"""
    from backend.models.book import Book, Chapter  # noqa: F401
    from backend.models.voice import VoiceProfile  # noqa: F401
    from backend.models.project import DubbingProject, Role, VoiceAssignment  # noqa: F401
    from backend.models.task import ConversionTask  # noqa: F401
    from backend.models.workflow import WorkflowTemplate  # noqa: F401
    from backend.models.memory import Memory  # noqa: F401  # DB-SCHEMA: 记忆表纳入建表清单

    Base.metadata.create_all(bind=engine)

    # F-R4-3: create_all 只补"缺表"，不补既有表的"缺列" -> 显式轻量列迁移
    # 注意：真正补列的常常是这条路径（get_engine 早于 init_db），所以必须把结果
    # 累加进 LAST_ADDED_COLUMNS，否则 init_db 再跑一次时已无事可做、启动日志永远空白。
    _record_added(_migrate_added_columns(engine))
    _record_added_indexes(_migrate_missing_indexes(engine))
    _backfill_voice_category(engine)

    # 初始化内置音色
    _init_builtin_voices()
    _init_edge_voices()


def _record_added(added) -> list:
    """把一次迁移补好的列名累加进模块级 LAST_ADDED_COLUMNS（去重、保序）。"""
    global LAST_ADDED_COLUMNS
    for name in added:
        if name not in LAST_ADDED_COLUMNS:
            LAST_ADDED_COLUMNS.append(name)
    return added


def _record_added_indexes(created) -> list:
    """把一次迁移补建的索引名累加进模块级 LAST_ADDED_INDEXES（去重、保序）。"""
    global LAST_ADDED_INDEXES
    for name in created:
        if name not in LAST_ADDED_INDEXES:
            LAST_ADDED_INDEXES.append(name)
    return created


def _table_columns(conn, table_name: str) -> set:
    """实际表列名集合（PRAGMA table_info，与 SQLAlchemy 模型声明列比对用）。"""
    rows = conn.execute(text(f'PRAGMA table_info("{table_name}")')).fetchall()
    return {r[1] for r in rows}


def _index_names(conn) -> set:
    """库里已有的索引名（含 sqlite_autoindex_*，与模型索引名不冲突）。"""
    rows = conn.execute(
        text("SELECT name FROM sqlite_master WHERE type='index'")).fetchall()
    return {r[0] for r in rows}


def _migrate_missing_indexes(engine) -> list:
    """PERF-IDX: 轻量索引迁移 —— 缺索引就 CREATE INDEX IF NOT EXISTS，只建索引、不动任何行。

    和 `_migrate_added_columns` 同一套机制：`Base.metadata.create_all()` 对**已存在的表**
    整张跳过，既不补列也不补索引，所以老库文件（data/peiyinyuan.db）里的热路径查询
    一直停在 SCAN + TEMP B-TREE。必须排在补列**之后**：复合索引可能落在刚 ADD 的列上。

    约束（刻意为之）:
      - 只 CREATE INDEX，绝不 DROP / VACUUM / UPDATE，已有数据行不受影响
      - 幂等：名字已在 sqlite_master 里就跳过
      - 列名经 dialect.identifier_preparer 加引号（`index` 是 SQL 保留字）
    """
    created = []
    preparer = engine.dialect.identifier_preparer
    with engine.connect() as conn:
        existing = _index_names(conn)
        for table in Base.metadata.tables.values():
            for index in sorted(table.indexes, key=lambda i: i.name):
                if index.name in existing:
                    continue
                cols = ", ".join(
                    preparer.quote(expr.name if hasattr(expr, "name") else str(expr))
                    for expr in index.expressions
                )
                conn.execute(text(
                    f'CREATE INDEX IF NOT EXISTS {preparer.quote(index.name)} '
                    f'ON {preparer.quote(table.name)} ({cols})'
                ))
                created.append(index.name)
        conn.commit()

    if created:
        logger.info(f"[数据库][PERF-IDX] 已补建缺失索引 {len(created)} 个: {', '.join(created)}")
    return created


def _migrate_added_columns(engine) -> list:
    """F-R4-3: 轻量列迁移 —— 只 ADD COLUMN，不删列、不改列、不搬数据。

    背景: `Base.metadata.create_all()` 只创建**缺失的表**，对**已存在的表**不会补新列
    (verify-round4 §5 关联风险)。于是给 `conversion_tasks` 之类的表加一个模型列，
    老库文件（data/peiyinyuan.db）会静默缺列，读写才在运行期报
    `no such column`。这里在启动时用 PRAGMA table_info 逐表比对模型声明列，
    缺哪列补哪列，并把补齐的列名记下来供启动日志打印。

    约束（刻意为之）:
      - 只加列，绝不 DROP / ALTER COLUMN / 重建表（本地单机库，数据不可再生）
      - 新列一律可空（我们全部模型列都无 NOT NULL），SQLite 因此允许无默认值 ADD
      - 幂等：列已存在则跳过；重复调用无副作用
    """
    added = []
    with engine.connect() as conn:
        for table_name, table in Base.metadata.tables.items():
            existing = _table_columns(conn, table_name)
            if not existing:
                continue  # 表由 create_all 新建（或不存在），无需补列
            for column in table.columns:
                if column.name in existing:
                    continue
                col_type = column.type.compile(dialect=engine.dialect)
                ddl = f'ALTER TABLE "{table_name}" ADD COLUMN "{column.name}" {col_type}'
                conn.execute(text(ddl))
                added.append(f"{table_name}.{column.name}")
        conn.commit()

    if added:
        logger.info(f"[数据库][F-R4-3] 已补齐缺失列 {len(added)} 个: {', '.join(added)}")
    return added


def _backfill_voice_category(engine):
    """VA-2: ADD COLUMN 只补列不补值——历史音色行 category 为 NULL 时一律归 human。
    新增的入库路径必须显式带类别，本回填只在启动时做一次幂等修补。"""
    with engine.connect() as conn:
        conn.execute(text(
            'UPDATE voice_profiles SET category = \'human\' '
            'WHERE category IS NULL OR category = \'\''
        ))
        conn.commit()


def get_session():
    """获取数据库会话"""
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(
            autocommit=False,
            autoflush=False,
            bind=get_engine(),
        )
    return _SessionLocal()


def init_db():
    """初始化数据库表。

    F-R4-3: create_all 之后追加一次**轻量列迁移**（PRAGMA table_info 比对 +
    ALTER TABLE ADD COLUMN，只加不删改），返回被补齐的列名列表供启动日志打印。
    PERF-IDX: 列迁移后再补一次**轻量索引迁移**（sqlite_master 比对 +
    CREATE INDEX IF NOT EXISTS），让老库启动一次就带上热路径索引。
    """
    from backend.models.book import Book, Chapter  # noqa: F401
    from backend.models.voice import VoiceProfile  # noqa: F401
    from backend.models.project import DubbingProject, Role, VoiceAssignment  # noqa: F401
    from backend.models.task import ConversionTask  # noqa: F401
    from backend.models.workflow import WorkflowTemplate  # noqa: F401
    from backend.models.memory import Memory  # noqa: F401  # DB-SCHEMA: 记忆表纳入建表清单

    engine = get_engine()
    Base.metadata.create_all(bind=engine)

    # get_engine() 建引擎那条路径已跑过一次列迁移（_ensure_all_tables 内）；
    # create_all 又可能刚补齐若干"缺表"，这里再跑一次覆盖"表在、列缺"的形态。
    # 两次结果都累加进 LAST_ADDED_COLUMNS，故无论实际补列发生在哪条路径，
    # 启动日志都能拿到真名（2F-COLMIG 探针判据①③抓到过这条断链）。
    _record_added(_migrate_added_columns(engine))
    _record_added_indexes(_migrate_missing_indexes(engine))
    _backfill_voice_category(engine)

    # 初始化内置音色数据
    _init_builtin_voices()
    # PY-116: Edge 在线音色独立幂等种子（老库 builtin 已存在也要补）
    _init_edge_voices()
    # U-5: 官方工作流模板播种（同名即跳过，异常不阻断启动）
    from modules.workflow.workflow_service import seed_official_templates
    seeded = seed_official_templates()
    if seeded:
        print(f"[数据库][U-5] 播种官方工作流模板 {len(seeded)} 条")

    print(f"[数据库] 初始化完成: {get_db_path()}")
    if LAST_ADDED_COLUMNS:
        print(f"[数据库][F-R4-3] 补齐缺失列 {len(LAST_ADDED_COLUMNS)} 个: "
              f"{', '.join(LAST_ADDED_COLUMNS)}")
    if LAST_ADDED_INDEXES:
        print(f"[数据库][PERF-IDX] 补建缺失索引 {len(LAST_ADDED_INDEXES)} 个: "
              f"{', '.join(LAST_ADDED_INDEXES)}")
    return list(LAST_ADDED_COLUMNS)


def _init_builtin_voices():
    """初始化内置音色到数据库。

    清单唯一真值 = `modules/voice_manager/builtin_voices.BUILTIN_VOICES`。
    这里**不再自带一份名单**：两份名单已经漂透过（本地 4 条、真值 11 条，
    连标签描述都不一致），而测试测的是真值那份 —— 等于没人守生产种子。
    """
    from backend.models.voice import VoiceProfile
    from modules.voice_manager.builtin_voices import BUILTIN_VOICES
    import uuid

    session = get_session()
    try:
        existing = session.query(VoiceProfile).filter_by(source="builtin").count()
        if existing > 0:
            return  # 已初始化

        for spec in BUILTIN_VOICES:
            session.add(VoiceProfile(id=str(uuid.uuid4()), **spec))
        session.commit()
        print(f"[数据库] 已初始化 {len(BUILTIN_VOICES)} 个内置音色")

    finally:
        session.close()


def _init_edge_voices():
    """PY-116: Edge 在线音色的幂等种子（按 name 补缺，不动既有行）。

    与 _init_builtin_voices 的"存在即跳过"不同——老库必然已有 builtin 行，
    这里必须按条目补，否则升级后 edge 档永远不进音色池/匹配器候选。
    """
    from backend.models.voice import VoiceProfile
    from modules.tts_engine.engines.edgetts_adapter import EDGE_ZH_VOICES
    import uuid

    session = get_session()
    try:
        have = {n for (n,) in session.query(VoiceProfile.name)
                            .filter_by(engine="edgetts").all()}
        added = 0
        for spec in EDGE_ZH_VOICES:
            if spec["name"] in have:
                continue
            session.add(VoiceProfile(
                id=str(uuid.uuid4()),
                name=spec["name"],
                source="builtin",
                engine="edgetts",
                language="zh",
                gender=spec["gender"],
                age_group=spec["age_group"],
                style_tags=spec["style_tags"],
                description=spec["description"],
                meta={"edge_voice": spec["voice"], "online": True},
            ))
            added += 1
        if added:
            session.commit()
            print(f"[数据库] 已补充 {added} 个 Edge 在线音色 (PY-116)")
    except Exception as e:
        session.rollback()
        print(f"[数据库] Edge 音色种子失败(不致命): {type(e).__name__}: {e}")
    finally:
        session.close()
