"""重置某项目的 cast 角色表（三层可逆），随后由调用方重跑 analyze-cast。

用法: python tools/voice_library/reset_cast_roles.py <project_id> [--apply]
可逆层: 1) DB 在线备份(backup API) 2) 逐行 INSERT 回滚 SQL 3) 只删不改别的表
"""
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DB = REPO / "data" / "peiyinyuan.db"
COLS = [r[1] for r in sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)
        .execute("pragma table_info(roles)")]


def lit_sql(v):
    if v is None:
        return "NULL"
    if isinstance(v, (int, float)):
        return str(v)
    return "'" + str(v).replace("'", "''") + "'"


def build_rollback_sql(rows, cols=COLS, table="roles"):
    lines = (f"INSERT INTO {table} ({','.join(cols)}) VALUES ("
             + ",".join(lit_sql(r.get(c)) for c in cols) + ");" for r in rows)
    return "\n".join(lines)


def verify_rollback_sql(sql_text, expect_rows, table="roles"):
    """回滚 SQL 必须在内存库里真重放过一遍且行数对得上，否则等于没有回滚。

    起因: 本工具旧版的生成器把 `lit(r.get(c) for c in COLS)` 写成了把生成器整体
    当字面量，产出的 SQL 逐条语法错误，而三层可逆的第 2 层从未被跑过 → 无人发现。
    """
    ddl = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True).execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", [table]).fetchone()[0]
    mem = sqlite3.connect(":memory:")
    try:
        mem.execute(ddl)
        mem.executescript(sql_text)
        got = mem.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        return got == expect_rows, f"{got} 行"
    except sqlite3.Error as e:
        return False, f"重放报错 {type(e).__name__}: {e}"
    finally:
        mem.close()


def main() -> int:
    pid = sys.argv[1]
    apply = "--apply" in sys.argv
    con = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute(
        "SELECT * FROM roles WHERE project_id=?", [pid])]
    proj = con.execute("SELECT id,name,book_id FROM dubbing_projects WHERE id=?",
                       [pid]).fetchone()
    va = con.execute(
        "SELECT count(*) FROM voice_assignments WHERE role_id IN "
        "(SELECT id FROM roles WHERE project_id=?)", [pid]).fetchone()[0]
    con.close()
    print({"project": dict(proj) if proj else None, "roles_to_delete": len(rows),
           "voice_assignments_referencing": va})
    if not rows:
        print("无角色可删，中止")
        return 1
    if not apply:
        print("[dry-run] 未写库。加 --apply 执行。")
        return 0

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    outdir = REPO.parent / "agent-ops" / f"cast-reset-{ts}"
    outdir.mkdir(parents=True, exist_ok=True)
    backup = REPO / "data" / "backups" / f"peiyinyuan-preCastReset-{ts}.db"

    sql_text = build_rollback_sql(rows, COLS)
    ok_sql, got = verify_rollback_sql(sql_text, len(rows))
    if not ok_sql:
        print(f"回滚 SQL 自检失败({got}，期望 {len(rows)} 行)，中止，不删任何行。")
        return 1
    rollback = outdir / "rollback_roles.sql"
    rollback.write_text(sql_text, encoding="utf-8")
    rollback_sql = str(rollback)

    backup.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(DB)
    dst = sqlite3.connect(backup)
    src.backup(dst)
    dst.close()

    cur = src.cursor()
    n = cur.execute("DELETE FROM roles WHERE project_id=?", [pid]).rowcount
    src.commit()
    src.close()
    print({"backup": str(backup), "deleted": n, "rollback_sql": rollback_sql,
           "rollback_verified_in_memory": ok_sql})
    return 0


if __name__ == "__main__":
    sys.exit(main())
