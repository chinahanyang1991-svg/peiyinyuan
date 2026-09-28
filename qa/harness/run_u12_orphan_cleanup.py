"""U-12：项目级孤儿清理的**可执行且实际跑过一遍**的回滚方案（AUDIT-2D §6 / §8 U-12）。

为什么要有这个文件：本轮立过一条教训——「回滚 SQL 不实际跑一遍 = 没有回滚」。所以这里不是
"生成一份 SQL 交出去"，而是把三段都真跑：

    ① 快照：sqlite **backup API**（生产库是活 WAL，copy2 会拷出半截页）
    ② 真删：在快照的工作副本上执行 cleanup.sql，复采 `PRAGMA foreign_key_check` 与逐表计数
    ③ 真回滚：在删完的副本上执行 rollback.sql，再与快照**逐行哈希**比对，证明还得回

两个"悬空"定义各自成集并互相印证（§6 钉过的口径，防再次各说各话）：
    A 悬空项目 = `dubbing_projects.book_id` 指向已删的书 —— 与 foreign_key_check 报的同源
    B 项目级残留任务 = `conversion_tasks.project_id ∉ dubbing_projects` —— §6 的 15 行，全在 06-13
连带面 = A 的子行（roles / voice_assignments / conversion_tasks 按 project_id）。

生产库全程只以 `mode=ro` 打开；**本 harness 不写生产**（`--commit` 显式拒绝）：
清这 15 行属用户侧裁定，见 `qa/AUDIT-2D-REPORT.md` §6 与 §9。

用法：PYTHONUTF8=1 python -X utf8 qa/harness/run_u12_orphan_cleanup.py
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PROD_DB = REPO / "data" / "peiyinyuan.db"
OUT_DIR = REPO.parent / "agent-ops" / "audit" / "u12"

DANGLING_PROJECTS_SQL = """
SELECT id FROM dubbing_projects
WHERE book_id IS NOT NULL AND book_id NOT IN (SELECT id FROM books)
ORDER BY id"""
ORPHAN_TASKS_SQL = """
SELECT id FROM conversion_tasks
WHERE project_id IS NOT NULL AND project_id NOT IN (SELECT id FROM dubbing_projects)
ORDER BY id"""
BOOK_ORPHAN_TASKS_SQL = """
SELECT id FROM conversion_tasks
WHERE book_id IS NOT NULL AND book_id NOT IN (SELECT id FROM books)
ORDER BY id"""

TABLES = ("dubbing_projects", "roles", "voice_assignments", "conversion_tasks")
# 删：先子后父；插回：先父后子。两序都是"外键方向"的两侧。
DELETE_ORDER = ("voice_assignments", "roles", "conversion_tasks", "dubbing_projects")
INSERT_ORDER = ("dubbing_projects", "roles", "voice_assignments", "conversion_tasks")


def open_ro(path: Path) -> sqlite3.Connection:
    return sqlite3.connect("file:{}?mode=ro".format(str(path).replace("\\", "/")), uri=True)


def snapshot(src: Path, dst: Path) -> None:
    """活 WAL 库必须走 backup API，不能 copy2。"""
    if dst.exists():
        dst.unlink()
    src_c, dst_c = open_ro(src), sqlite3.connect(str(dst))
    with dst_c:
        src_c.backup(dst_c)
    dst_c.close()
    src_c.close()


def lit(v) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, (bytes, bytearray)):
        return "X'{}'".format(bytes(v).hex())
    return "'" + str(v).replace("'", "''") + "'"


def columns(db: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in db.execute("PRAGMA table_info(%s)" % table)]


def rows_of(db: sqlite3.Connection, table: str, ids: list[str]) -> list[tuple]:
    """连 `rowid` 一起取：这四张表都是 TEXT 主键的 rowid 表，而
    `PRAGMA foreign_key_check` 报的正是 rowid，回滚不钉住 rowid 就没法两相比对。"""
    out = []
    for pk in ids:
        row = db.execute("SELECT rowid, * FROM %s WHERE id = ?" % table, (pk,)).fetchone()
        assert row is not None, "%s 行 %s 取不到（快照自相矛盾）" % (table, pk)
        out.append(tuple(row))
    return out


def hash_rows(rows: list[tuple]) -> str:
    blob = json.dumps(rows, ensure_ascii=False, default=str).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:16]


def delete_sets(db: sqlite3.Connection) -> dict[str, list[str]]:
    """按两个定义 + 连带面算出每一张表要摘掉的主键。"""
    projects = [r[0] for r in db.execute(DANGLING_PROJECTS_SQL)]
    marks = ",".join("?" * len(projects)) if projects else "NULL"
    child = lambda t: (  # noqa: E731 - 三处同形小工具
        [r[0] for r in db.execute(
            "SELECT id FROM %s WHERE project_id IN (%s) ORDER BY id" % (t, marks), projects)]
        if projects else []
    )
    orphan_tasks = [r[0] for r in db.execute(ORPHAN_TASKS_SQL)]
    return {
        "dubbing_projects": projects,
        "roles": child("roles"),
        "voice_assignments": child("voice_assignments"),
        "conversion_tasks": sorted(set(child("conversion_tasks")) | set(orphan_tasks)),
    }


def build_sql(db: sqlite3.Connection, sets: dict[str, list[str]], dumped: dict) -> tuple[str, str]:
    cleanup = ["-- U-12 项目级孤儿清理（含 A 悬空项目的连带子行 + B 项目级残留任务）", "BEGIN;"]
    for t in DELETE_ORDER:
        cleanup.append("-- %s: %d 行" % (t, len(sets[t])))
        for pk in sets[t]:
            cleanup.append("DELETE FROM %s WHERE id = %s;" % (t, lit(pk)))
    cleanup.append("COMMIT;")

    rollback = ["-- U-12 回滚：把 cleanup 摘掉的每一行按原值插回（先父后子）", "BEGIN;"]
    for t in INSERT_ORDER:
        if not dumped[t]:
            continue
        cols = ", ".join(["rowid"] + columns(db, t))
        for row in dumped[t]:
            rollback.append("INSERT INTO %s (%s) VALUES (%s);" % (
                t, cols, ", ".join(lit(v) for v in row)))
    rollback.append("COMMIT;")
    return "\n".join(cleanup) + "\n", "\n".join(rollback) + "\n"


def counts(db: sqlite3.Connection) -> dict:
    return {t: db.execute("SELECT count(*) FROM %s" % t).fetchone()[0] for t in TABLES}


def fk_rows(db: sqlite3.Connection) -> list[tuple]:
    return [tuple(r) for r in db.execute("PRAGMA foreign_key_check")]


def main() -> int:
    if "--commit" in sys.argv:
        print("REFUSED：本 harness 只验证方案，不写生产库。清生产由用户/主线显式执行。")
        return 2
    if not PROD_DB.exists():
        print("找不到生产库:", PROD_DB)
        return 2

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    pre = OUT_DIR / ("pre_%s.db" % stamp)
    work = OUT_DIR / ("work_%s.db" % stamp)
    back = OUT_DIR / ("restored_%s.db" % stamp)
    wal = PROD_DB.with_suffix(PROD_DB.suffix + "-wal")
    print("生产库 %d B / -wal %s B（只读打开，本 harness 零写入）" % (
        PROD_DB.stat().st_size, wal.stat().st_size if wal.exists() else "无"))

    snapshot(PROD_DB, pre)
    db = sqlite3.connect(str(pre))
    sets = delete_sets(db)
    dumped = {t: rows_of(db, t, sets[t]) for t in TABLES}
    cleanup_sql, rollback_sql = build_sql(db, sets, dumped)
    pre_counts, pre_fk = counts(db), fk_rows(db)
    pre_hashes = {t: hash_rows(dumped[t]) for t in TABLES}
    book_orphans = [r[0] for r in db.execute(BOOK_ORPHAN_TASKS_SQL)]
    db.close()

    # 口径交叉印证：A 必须与 foreign_key_check 报的同一批行（它报的是 rowid），
    # 否则 §6 那套"两个定义各说各话"又要重来一遍
    fk_projects = sorted({r[1] for r in pre_fk if r[0] == "dubbing_projects"})
    dumped_projects = sorted(r[0] for r in dumped["dubbing_projects"])
    assert fk_projects == dumped_projects, (fk_projects, dumped_projects)

    snapshot(pre, work)
    db = sqlite3.connect(str(work))
    db.executescript(cleanup_sql)
    post_counts, post_fk = counts(db), fk_rows(db)
    dangling_left = [r[0] for r in db.execute(DANGLING_PROJECTS_SQL)]
    orphans_left = [r[0] for r in db.execute(ORPHAN_TASKS_SQL)]
    db.close()
    assert post_fk == [], "删后 foreign_key_check 仍非空：%r" % (post_fk,)
    assert dangling_left == [] and orphans_left == [], (dangling_left, orphans_left)
    for t in TABLES:
        assert post_counts[t] == pre_counts[t] - len(sets[t]), (t, pre_counts[t], post_counts[t], len(sets[t]))

    snapshot(work, back)
    db = sqlite3.connect(str(back))
    db.executescript(rollback_sql)
    rb_counts, rb_fk = counts(db), fk_rows(db)
    rb_hashes = {t: hash_rows(rows_of(db, t, sets[t])) for t in TABLES}
    db.close()
    assert rb_counts == pre_counts, (rb_counts, pre_counts)
    assert rb_hashes == pre_hashes, ("回滚后逐行哈希与快照不符", rb_hashes, pre_hashes)
    assert rb_fk == pre_fk, (rb_fk, pre_fk)

    (OUT_DIR / ("cleanup_%s.sql" % stamp)).write_text(cleanup_sql, encoding="utf-8")
    (OUT_DIR / ("rollback_%s.sql" % stamp)).write_text(rollback_sql, encoding="utf-8")
    report = {
        "stamp": stamp,
        "production_touched": False,
        "delete_sets": {t: len(v) for t, v in sets.items()},
        "deleted_ids": sets,
        "pre_counts": pre_counts,
        "post_counts": post_counts,
        "rollback_counts": rb_counts,
        "row_hashes_pre": pre_hashes,
        "row_hashes_after_rollback": rb_hashes,
        "rollback_restores_byte_equal_rows": True,
        # 逐行哈希把 rowid 也编进去了（rows_of 取 `rowid, *`），故此项由 rb_hashes 断言实证
        "rollback_preserves_rowid": rb_hashes == pre_hashes,
        "dangling_project_rowids": dumped_projects,
        "foreign_key_check": {"pre": len(pre_fk), "after_cleanup": len(post_fk),
                              "after_rollback": len(rb_fk)},
        "book_level_orphan_tasks": len(book_orphans),
        "files": {"pre": pre.name, "work": work.name, "restored": back.name,
                  "cleanup_sql": "cleanup_%s.sql" % stamp,
                  "rollback_sql": "rollback_%s.sql" % stamp},
    }
    (OUT_DIR / ("report_%s.json" % stamp)).write_text(
        json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=1))
    print("PASS: 快照→真删→真回滚三段跑完；副本上 foreign_key_check %d→%d→%d，"
          "回滚后逐行哈希与快照一致" % (len(pre_fk), len(post_fk), len(rb_fk)))
    print("产出目录:", OUT_DIR)
    return 0


if __name__ == "__main__":
    sys.exit(main())
