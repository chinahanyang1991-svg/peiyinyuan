"""
[P7 补充探针] 下载端点中文文件名 500 最小复现 (无需重新合成, 复用 DB 中已完成任务)
运行: python qa/harness/probe_p7_download.py
判定: 对 data/output 下真实存在的产物, GET /api/tasks/{id}/download 应 200+字节;
      中文书名产物实测 500 (Content-Disposition latin-1 编码失败) -> PY-119
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness_lib as H  # noqa: E402


def main():
    log = H.LOGS / "probe_p7_download.log"
    with H.Tee(log):
        su = H.Suite("P7b-download-probe", "probe_p7_download.py")
        r = su.new("已完成任务下载端点可用性 (中文书名产物)")
        r.evidence_add(log)
        be = H.start_backend(H.LOGS / "probe_download_backend.log")
        try:
            code, body = H.http("GET", "/tasks/", timeout=60)
            tasks = body.get("tasks", []) if isinstance(body, dict) else []
            done = [t for t in tasks if t.get("status") == "completed"]
            H.kv("已完成任务数", len(done))
            import os
            checked = 0
            ascii_ok = 0
            cjk_fail = 0
            for t in done:
                p = t.get("output_path") or ""
                if not p or not os.path.exists(p):
                    continue
                name = os.path.basename(p)
                is_cjk = any("\u4e00" <= c <= "\u9fff" for c in name)
                code2, raw = H.http("GET", f"/tasks/{t['id']}/download", timeout=60, raw=True)
                n = len(raw) if isinstance(raw, bytes) else str(raw)[:120]
                tag = "中文名" if is_cjk else "ASCII名"
                H.kv(f"  {tag} {name[:40]}", f"HTTP {code2} bytes={n}")
                checked += 1
                if is_cjk:
                    if code2 != 200:
                        cjk_fail += 1
                elif code2 == 200:
                    ascii_ok += 1
                if checked >= 12:
                    break
            r.check("有被检查的已完成任务产物", checked > 0, f"checked={checked}")
            r.check("全部已完成任务下载端点返回 200", cjk_fail == 0 and checked > 0,
                    f"{cjk_fail}/{checked} 个下载非 200 -> backend/api/tasks.py:251 "
                    f"Content-Disposition 直接放中文名, starlette 以 latin-1 编 header 抛 "
                    f"UnicodeEncodeError")
            r.check("对照组: ASCII 名产物可正常下载(证明故障仅与中文名相关)",
                    ascii_ok > 0, f"ASCII 名成功 {ascii_ok} 个")
        finally:
            H.stop_backend(be)
        su.finish(exit_when_fail=False)


if __name__ == "__main__":
    main()
