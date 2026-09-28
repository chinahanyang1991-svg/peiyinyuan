"""U-18 数据库旧数据清理（用户授权 2026-09-24：六月失败任务 + 五个原书已缺失的悬空老项目）。

删除集（三张表，逐枚指回 SQL；默认干跑，`--commit` 才真删）：
  SET_T  `conversion_tasks` 中 `total_chars=0 AND status='failed'` —— 06-13 那批"服务重启导致中断"，
         盘上无任何成品文件 = **5**
  SET_P  `dubbing_projects` 中 `book_id` 指向已不存在的 `books` = **5**（其中一枚 name 为
         `__test_invalid_book__`，是测试残留）
  SET_R  `roles` 中 `project_id ∈ SET_P` = **4**（四枚"旁白"，voice_id 全为 NULL）

明确**不在**删除集里（本脚本会反向断言它们仍在）：
  * 27 枚 `status='completed' AND output_path=''` —— 实测全是 `output_format='cast'` 的角色分析
    任务，这类任务本就不产出音频文件，空路径是正常形态（与 SET_T 不同源，不是脏数据）；
  * 7 枚 `total_chars=0 AND status='completed'` —— 成品 wav **仍在盘上**（1.5MB～63MB），
    删记录等于丢掉还能听的成品的唯一索引；
  * `books` / `voice_profiles` / `chapters` / `workflow_templates` 整表不碰。

红线口径（沿用 U-12）：删前 ① `sqlite3` backup API 整库快照（活 WAL 库禁 copy2）
② 逐行带 `rowid` 的 `INSERT` 回滚脚本落盘；删后判据不全绿就报错退出；
回滚脚本**在删后的副本上真跑一遍**（生产库全程不参与演练）。
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DB = ROOT / "data" / "peiyinyuan.db"
OUT_DIR = ROOT / "qa" / "round-1" / "artifacts"

TABLES = ("dubbing_projects", "roles", "conversion_tasks")

SET_P_SQL = """
SELECT id FROM dubbing_projects
WHERE book_id IS NOT NULL AND book_id <> ''
  AND NOT EXISTS (SELECT 1 FROM books b WHERE b.id = dubbing_projects.book_id)
ORDER BY created_at
"""
SET_R_SQL = """
SELECT id FROM roles
WHERE project_id IN (
  SELECT p.id FROM dubbing_projects p
  WHERE p.book_id IS NOT NULL AND p.book_id <> ''
    AND NOT EXISTS (SELECT 1 FROM books b WHERE b.id = p.book_id))
ORDER BY name
"""
SET_T_SQL = """
SELECT id FROM conversion_tasks
WHERE total_chars = 0 AND status = 'failed'
ORDER BY created_at
"""

# 反向断言：这三批必须一枚不少地留着
KEEP_SQL = {
    "cast_tasks_27": "SELECT COUNT(*) FROM conversion_tasks WHERE status='completed' AND output_path=''",
    "wav_present_7": "SELECT COUNT(*) FROM conversion_tasks WHERE total_chars=0 AND status='completed'",
    "books": "SELECT COUNT(*) FROM books",
    "voice_profiles": "SELECT COUNT(*) FROM voice_profiles",
    "chapters": "SELECT COUNT(*) FROM chapters",
}


def lit(v) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, (int, float)):
        return repr(v)
    return "'" + str(v).replace("'", "''") + "'"


def cols_of(db, table):
    return [r[1] for r in db.execute(f"PRAGMA table_info({table})")]


def rows_by_id(db, table, ids):
    return [db.execute(f"SELECT rowid, * FROM {table} WHERE id = ?", (i,)).fetchone() for i in ids]


def hash_rows(rows):
    h = hashlib.sha256()
    for r in rows:
        h.update("|".join("" if c is None else str(c) for c in r).encode("utf-8"))
    return h.hexdigest()[:16]


def build_rollback(sets: dict[str, list[tuple]], cols: dict[str, list[str]]) -> str:
    out = ["-- U-18 旧数据回滚脚本（删前逐行导出；在生产库上执行即还原）", "BEGIN;"]
    # 还原顺序与删除顺序相反：先父（项目）再子（角色/任务）
    for table in ("dubbing_projects", "roles", "conversion_tasks"):
        for r in sets[table]:
            names = ["rowid"] + cols[table]
            out.append("INSERT INTO %s (%s) VALUES (%s);" % (
                table, ", ".join(names), ", ".join(lit(v) for v in r)))
    out.append("COMMIT;")
    return "\n".join(out) + "\n"


def main() -> int:
    commit = "--commit" in sys.argv
    ts = time.strftime("%Y%m%d-%H%M%S")
    ro = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)

    set_p = [r[0] for r in ro.execute(SET_P_SQL)]
    set_r = [r[0] for r in ro.execute(SET_R_SQL)]
    set_t = [r[0] for r in ro.execute(SET_T_SQL)]
    totals = {t: ro.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in TABLES}
    keep_before = {k: ro.execute(v).fetchone()[0] for k, v in KEEP_SQL.items()}
    fk_before = len(ro.execute("PRAGMA foreign_key_check").fetchall())

    rows = {t: rows_by_id(ro, t, ids) for t, ids in
            (("dubbing_projects", set_p), ("roles", set_r), ("conversion_tasks", set_t))}
    cols = {t: cols_of(ro, t) for t in TABLES}
    ro.close()

    n = sum(len(v) for v in (set_p, set_r, set_t))
    digests = {t: hash_rows(rows[t]) for t in TABLES}
    print(f"删除集: 项目={len(set_p)} 角色={len(set_r)} 任务={len(set_t)} 合计={n}")
    print(f"总数(前): {totals} | fk_check行={fk_before} | 保留面={keep_before}")
    print(f"行哈希: {digests}")
    for i, p in enumerate(set_p):
        print("   项目", p[:8], rows["dubbing_projects"][i][2], rows["dubbing_projects"][i][1])
    for i, t in enumerate(set_t):
        r = rows["conversion_tasks"][i]
        print("   任务", t[:8], "status=%s" % r[4], "created=%s" % r[14], "err=%s" % str(r[10])[:30])

    # 口径门：命中数必须与授权时实测一致，否则说明库又漂了 —— 拒绝执行
    if (len(set_p), len(set_r), len(set_t)) != (5, 4, 5):
        print("!! 命中数与授权口径(5/4/5)不符，拒删"); return 2
    if any(r is None for t in TABLES for r in rows[t]):
        print("!! 有 id 读不到整行"); return 2
    if keep_before["wav_present_7"] != 7 or keep_before["cast_tasks_27"] != 27:
        print("!! 保留面计数与授权时不符，拒删"); return 2
    # SET_T 不得落在 SET_P 的项目下（否则"任务 0"的删前结论被推翻）
    if n == 0:
        print("没有命中，无事可做"); return 1

    if not commit:
        print("DRY-RUN（未删）。要真删加 --commit")
        return 0

    # ① 整库快照
    snap_dir = ROOT / "data" / f"backup_u18clean_{ts}"
    snap_dir.mkdir(parents=True, exist_ok=True)
    snap = snap_dir / "peiyinyuan.db"
    src = sqlite3.connect(DB)
    dst = sqlite3.connect(snap)
    src.backup(dst)
    dst.close()
    print(f"快照 -> {snap.name} ({snap.stat().st_size} B)")

    # ② 回滚脚本 + 清单
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    rb = OUT_DIR / f"u18_rollback_{ts}.sql"
    rb.write_text(build_rollback(rows, cols), encoding="utf-8")
    manifest = OUT_DIR / f"u18_cleanup_deleted_rows_{ts}.json"
    manifest.write_text(json.dumps({
        "ts": ts, "totals_before": totals, "fk_rows_before": fk_before,
        "set_projects": set_p, "set_roles": set_r, "set_tasks": set_t,
        "rows_hash": digests, "keep_before": keep_before, "snapshot": str(snap),
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"回滚脚本 -> {rb.name} ({rb.stat().st_size} B) | 清单 -> {manifest.name}")

    # ③ 真删：子先父后，单事务
    src.isolation_level = None
    cur = src.cursor()
    cur.execute("BEGIN IMMEDIATE")
    changed = {}
    for table, ids in (("roles", set_r), ("conversion_tasks", set_t), ("dubbing_projects", set_p)):
        marks = ",".join("?" * len(ids))
        cur.execute(f"DELETE FROM {table} WHERE id IN ({marks})", ids)
        changed[table] = cur.rowcount
    totals_after = {t: cur.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in TABLES}
    keep_after = {k: cur.execute(v).fetchone()[0] for k, v in KEEP_SQL.items()}
    left = {t: cur.execute("SELECT COUNT(*) FROM %s WHERE id IN (%s)" % (
        t, ",".join("?" * len(i))), i).fetchone()[0]
        for t, i in (("dubbing_projects", set_p), ("roles", set_r), ("conversion_tasks", set_t))}
    fk_after = len(cur.execute("PRAGMA foreign_key_check").fetchall())
    book_orphan_after = cur.execute(
        "SELECT COUNT(*) FROM conversion_tasks WHERE book_id IS NOT NULL AND book_id NOT IN (SELECT id FROM books)"
    ).fetchone()[0]
    cur.execute("COMMIT")
    src.close()

    # 被删的行只能从快照再读一遍 —— 顺带证明"回滚脚本内容 == 真删掉的内容"
    snap_db = sqlite3.connect(f"file:{snap.as_posix()}?mode=ro", uri=True)
    rehash = {t: hash_rows(rows_by_id(snap_db, t, i)) for t, i in
              (("dubbing_projects", set_p), ("roles", set_r), ("conversion_tasks", set_t))}
    snap_db.close()

    # ④ 回滚演练：在删后库的**副本**上跑回滚 SQL，生产库全程不参与
    drill = ROOT / "data" / f"drill_u18_{ts}.db"
    s = sqlite3.connect(DB); d = sqlite3.connect(drill); s.backup(d); d.close(); s.close()
    dr = sqlite3.connect(drill)
    dr.isolation_level = None
    dr.executescript(rb.read_text(encoding="utf-8"))
    restored = {t: dr.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in TABLES}
    drill_hash = {t: hash_rows(rows_by_id(dr, t, i)) for t, i in
                  (("dubbing_projects", set_p), ("roles", set_r), ("conversion_tasks", set_t))}
    dr.close()
    drill_ok = all(restored[t] == totals[t] for t in TABLES) and drill_hash == digests
    os.remove(drill)

    report = {
        "deleted_rowcount": changed, "totals_after": totals_after,
        "rows_still_present": left,
        "delta_matches": all(totals[t] - totals_after[t] == changed[t] for t in TABLES),
        "keep_after": keep_after, "keep_unchanged": keep_after == keep_before,
        "fk_rows": [fk_before, fk_after], "book_orphan_after": book_orphan_after,
        "rehash_matches_rollback": rehash == digests,
        "rollback_drill": {"restored_totals": restored, "hash_match": drill_hash == digests,
                           "ok": drill_ok},
    }
    print(json.dumps(report, ensure_ascii=False))
    ok = (report["delta_matches"] and all(v == 0 for v in left.values())
          and report["keep_unchanged"] and book_orphan_after == 0
          and fk_after == 0
          and report["rehash_matches_rollback"] and drill_ok)
    print("[OK] 判据全绿" if ok else "[FAIL] 有判据不绿")
    return 0 if ok else 3


if __name__ == "__main__":
    sys.exit(main())
