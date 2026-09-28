"""U-12 历史脏任务清理 —— 生产执行版（默认干跑，`--commit` 才真删）。

口径两枚（与 AUDIT-2D §6/§8 登记的一致，逐枚指回 SQL）：
  SET1 项目级历史残留：`project_id` 非空且指向已不存在的 dubbing_projects（06-13 那批）= **15**
  SET2 假完成：`status='completed'` 且 `output_path IS NULL`，但项目仍在库 = **8**

⚠ SET2 的口径是**真 NULL**，不是"空也算没有"：库里另有 **27** 枚 `output_path=''`（空串）的
completed 行（9 月那批），它们与上面 8 枚不同源、也**不在用户的删除授权里**（用户点的是"8 条
标着完成但找不到成品文件"）。首版这里写成 `IS NULL OR =''` 时命中 35 枚，与账上 8 枚差 27——
差集恰好全为空串 ⇒ 收窄回 `IS NULL`。**要清那 27 枚得另立一条授权**。

红线口径：
- 本脚本**只删 `conversion_tasks` 行**，不碰 dubbing_projects / cast_roles / voice_assignments
  （那三张表的悬空行不在用户的删除授权里，仍是 AUDIT-2D 的待裁定项）。
- 删前两件事必做：① `sqlite3` backup API 整库快照（活 WAL 库禁 copy2）；
  ② 逐行 `INSERT ... VALUES`（带 rowid）回滚脚本落盘 —— 回滚不靠"记得内容"，靠跑这份 SQL。
- 删后复采：总数差 = 删除集大小、逐 id 已消失、book 级孤儿仍 0、`foreign_key_check` 不因本次改动。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DB = ROOT / "data" / "peiyinyuan.db"
OUT_DIR = ROOT / "qa" / "round-1" / "artifacts"

SET1_SQL = """
SELECT id FROM conversion_tasks
WHERE project_id IS NOT NULL AND project_id NOT IN (SELECT id FROM dubbing_projects)
ORDER BY created_at
"""
SET2_SQL = """
SELECT id FROM conversion_tasks
WHERE status = 'completed' AND output_path IS NULL
  AND project_id IN (SELECT id FROM dubbing_projects)
ORDER BY created_at
"""
BOOK_ORPHAN_SQL = """
SELECT id FROM conversion_tasks
WHERE book_id IS NOT NULL AND book_id NOT IN (SELECT id FROM books)
"""


def lit(v) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, (int, float)):
        return repr(v)
    return "'" + str(v).replace("'", "''") + "'"


def columns(db: sqlite3.Connection) -> list[str]:
    return [r[1] for r in db.execute("PRAGMA table_info(conversion_tasks)")]


def rows_by_id(db: sqlite3.Connection, ids: list[str]) -> list[tuple]:
    return [db.execute("SELECT rowid, * FROM conversion_tasks WHERE id = ?", (i,)).fetchone()
            for i in ids]


def hash_rows(rows: list[tuple]) -> str:
    h = hashlib.sha256()
    for r in rows:
        h.update("|".join("" if c is None else str(c) for c in r).encode("utf-8"))
    return h.hexdigest()[:16]


def build_rollback(rows: list[tuple], cols: list[str]) -> str:
    names = ["rowid"] + cols
    out = ["-- U-12 历史脏任务回滚脚本（删前逐行导出；在生产库上执行即还原）",
           "BEGIN;", ]
    for r in rows:
        out.append("INSERT INTO conversion_tasks (%s) VALUES (%s);" % (
            ", ".join(names), ", ".join(lit(v) for v in r)))
    out.append("COMMIT;")
    return "\n".join(out) + "\n"


def main() -> int:
    commit = "--commit" in sys.argv
    ts = time.strftime("%Y%m%d-%H%M%S")
    ro = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)
    total_before = ro.execute("SELECT count(*) FROM conversion_tasks").fetchone()[0]
    set1 = [r[0] for r in ro.execute(SET1_SQL)]
    set2 = [r[0] for r in ro.execute(SET2_SQL)]
    overlap = sorted(set(set1) & set(set2))
    ids = sorted(set(set1) | set(set2))
    book_orphans = [r[0] for r in ro.execute(BOOK_ORPHAN_SQL)]
    fk_before = ro.execute("PRAGMA foreign_key_check").fetchall()
    cols = columns(ro)
    rows = rows_by_id(ro, ids)
    digest = hash_rows(rows)
    months = {}
    for r in ro.execute("SELECT id, substr(created_at,1,7), status FROM conversion_tasks"):
        months[r[1]] = months.get(r[1], 0) + 1
    print(f"总数(前)={total_before} SET1={len(set1)} SET2={len(set2)} 交集={len(overlap)} "
          f"删除集={len(ids)} book级孤儿={len(book_orphans)} fk_check行={len(fk_before)} "
          f"删除集行哈希={digest} 月份分布={months}")
    if overlap:
        print("!! 两集合有交集，口径不互斥，拒删"); return 2
    if not ids:
        print("没有命中，无事可做"); return 1
    if any(r is None for r in rows):
        print("!! 有 id 读不到整行"); return 2
    ro.close()

    if not commit:
        print("DRY-RUN（未删）。要真删加 --commit")
        for i in ids:
            r = rows[ids.index(i)]
            print("   ", i[:8], "status=%s" % r[4], "created=%s" % r[15],
                  "path=%s" % ("<NULL>" if r[8] is None else repr(r[8])[:24]),
                  "proj=%s" % ("活" if i in set2 else "孤儿"))
        return 0

    # ① 整库快照（backup API，活 WAL 必须走这条）
    snap_dir = ROOT / "data" / f"backup_u12clean_{ts}"
    snap_dir.mkdir(parents=True, exist_ok=True)
    snap = snap_dir / "peiyinyuan.db"
    src = sqlite3.connect(DB)
    dst = sqlite3.connect(snap)
    src.backup(dst)
    dst.close()
    print(f"快照 -> {snap.name} ({snap.stat().st_size} B)")

    # ② 回滚脚本 + 行清单落盘
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    rb = OUT_DIR / f"u12_rollback_{ts}.sql"
    rb.write_text(build_rollback(rows, cols), encoding="utf-8")
    manifest = OUT_DIR / f"u12_cleanup_deleted_rows_{ts}.json"
    manifest.write_text(json.dumps({
        "ts": ts, "total_before": total_before, "set1": set1, "set2": set2,
        "deleted_ids": ids, "rows_hash": digest, "snapshot": str(snap),
        "fk_check_rows_before": len(fk_before),
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"回滚脚本 -> {rb.name} ({rb.stat().st_size} B) | 清单 -> {manifest.name}")

    # ③ 真删（单事务；isolation_level=None 才轮得到我们自己 BEGIN）
    src.isolation_level = None
    cur = src.cursor()
    marks = ",".join("?" * len(ids))
    cur.execute("BEGIN IMMEDIATE")
    cur.execute(f"DELETE FROM conversion_tasks WHERE id IN ({marks})", ids)
    changed = cur.rowcount
    total_after = cur.execute("SELECT count(*) FROM conversion_tasks").fetchone()[0]
    cur.execute("COMMIT")
    print(f"DELETE rowcount={changed} 总数(后)={total_after}")

    # ④ 复采判据
    left = cur.execute(f"SELECT count(*) FROM conversion_tasks WHERE id IN ({marks})", ids).fetchone()[0]
    book_orphans_after = len(cur.execute(BOOK_ORPHAN_SQL).fetchall())
    fk_after = cur.execute("PRAGMA foreign_key_check").fetchall()
    # 被删的行只能从快照里再读一遍——这同时证明"回滚脚本里的内容 == 当初真删掉的内容"
    snap_db = sqlite3.connect(f"file:{snap.as_posix()}?mode=ro", uri=True)
    rehash = hash_rows(rows_by_id(snap_db, ids))
    snap_db.close()
    report = {
        "total_before": total_before, "total_after": total_after,
        "deleted": len(ids), "rows_still_present": left,
        "delta_matches": (total_before - total_after) == len(ids),
        "book_orphan_after": book_orphans_after,
        "fk_rows_before": len(fk_before), "fk_rows_after": len(fk_after),
        "deleted_rows_hash": digest, "rehash_of_dumped_rows": rehash,
    }
    print(json.dumps(report, ensure_ascii=False))
    ok = (report["delta_matches"] and left == 0 and book_orphans_after == 0
          and len(fk_after) == len(fk_before) and digest == rehash)
    print("[OK] 判据全绿" if ok else "[FAIL] 判据未全绿 —— 用回滚脚本还原")
    src.close()
    return 0 if ok else 3


if __name__ == "__main__":
    sys.exit(main())
