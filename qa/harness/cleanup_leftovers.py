"""
清理 harness 遗留在共享 DB 中的测试书籍 (走生产 DELETE /api/books/{id}, 不直接改库)。

判定依据: 标题 + total_chars 与 qa/harness/fixtures 生成器的固定签名完全一致,
          且 created_at 不早于本 harness 首次运行日 (2026-09-20)。

运行: python qa/harness/cleanup_leftovers.py [--dry-run]
"""
import sys
import sqlite3
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness_lib as H  # noqa: E402

# (title, total_chars) 签名 -> 由 make_fixtures.py 的固定样本产生
SIGNATURES = {
    ("第一章 初入京城", 836),        # sample_small.txt / sample_gbk.txt
    ("第一章 超长边界", 2700901),    # probe_big.txt (超大文件边界用例)
}
SAFE_AFTER = "2026-09-20"           # harness 启用日, 之前属于用户真实数据


def main():
    dry = "--dry-run" in sys.argv
    be = H.start_backend(H.LOGS / "cleanup_backend.log") if not dry else None
    try:
        con = sqlite3.connect(f"file:{H.DB_PATH.as_posix()}?mode=ro", uri=True)
        rows = con.execute(
            "SELECT id,title,total_chars,created_at FROM books "
            "WHERE date(created_at) >= date(?)", (SAFE_AFTER,)).fetchall()
        con.close()
        hits = [r for r in rows if (r[1], r[2]) in SIGNATURES]
        print(f"候选 (created_at>={SAFE_AFTER}): {len(rows)} 本, 命中签名: {len(hits)} 本")
        for bid, title, tc, ca in hits:
            print(f"  {bid}  {title}  {tc}字  {ca}")
            if not dry:
                print("    DELETE ->", H.http("DELETE", f"/books/{bid}")[0])
        if dry:
            print("[dry-run] 未删除")
    finally:
        if be:
            H.stop_backend(be)


if __name__ == "__main__":
    main()
