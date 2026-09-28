"""U-7 真实 HTTP 探针：把在线音色库的三枚端点放到真 socket 上过一遍。

单测打的是函数，这枚打的是**用户那条路**（uvicorn + 真 HTTP 客户端），覆盖 U-7
三处改动在 HTTP 层的形态：

  ① `GET /api/voices/online/sources` —— 200，且响应体里**看得见** M-2 的账
     （`sources.search_stats` 的超时率、撤掉数、仍在跑数、池内线程数）；
  ② `POST /api/voices/online/download` 带 `..` 路径段 —— 400 且 `detail.reason`
     为 `bad_repo`，且在发起任何网络请求之前就被拒（不留下半截产物）；
  ③ `GET /api/voices/online/search` —— 要么 200 + 真条目（`fabricated:false`），
     要么 502/504 + 结构化 reason；**不许**出现"挂住不返回"。整枚端点的墙钟必须
     落在路由预算 + 余量之内。

客户端必须 `trust_env=False`：本机注册了系统代理，它会拦对 127.0.0.1 的请求
（GET 侥幸转发、POST 直接 WinError 10054 复位）—— 这是 U-6 探针踩过的坑。
被测代码到 hf-mirror / api.github.com 的出网照旧走代理，那才是要复现的形态。

跑法：
    PYTHONUTF8=1 python -X utf8 qa/harness/run_u7_http_probe.py
"""

from __future__ import annotations

import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

TIMEOUT = float(os.getenv("U7_HTTP_PROBE_TIMEOUT", "45"))


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _get(url: str, timeout: float) -> tuple[int, bytes, float]:
    """不走环境代理的 GET（`ProxyHandler({})` 显式清空）。"""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    started = time.monotonic()
    try:
        with opener.open(url, timeout=timeout) as resp:
            return resp.status, resp.read(), time.monotonic() - started
    except urllib.error.HTTPError as e:
        return e.code, e.read() or b"", time.monotonic() - started


def _post(url: str, payload: dict, timeout: float) -> tuple[int, bytes, float]:
    import json

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    started = time.monotonic()
    try:
        with opener.open(req, timeout=timeout) as resp:
            return resp.status, resp.read(), time.monotonic() - started
    except urllib.error.HTTPError as e:
        return e.code, e.read() or b"", time.monotonic() - started


def main() -> int:
    import uvicorn
    from fastapi import FastAPI

    from backend.api import voices as voices_api
    from backend.services import voice_online

    app = FastAPI()
    app.include_router(voices_api.router, prefix="/api/voices")
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
                                           log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True, name="u7-probe-uvicorn")
    thread.start()
    for _ in range(120):
        if server.started:
            break
        time.sleep(0.25)
    base = f"http://127.0.0.1:{port}/api/voices"
    print(f"[probe] uvicorn 起于 127.0.0.1:{port}（只挂 voices 路由）")

    bad: list[str] = []
    try:
        # ① 诊断端点：账必须看得见
        code, body, took = _get(f"{base}/online/sources", TIMEOUT)
        import json
        payload = json.loads(body.decode("utf-8"))
        print(f"[probe] /online/sources -> {code} {len(body)}B t={took:.2f}s")
        if code != 200:
            bad.append(f"/online/sources 期望 200，实得 {code}")
        stats = payload.get("sources", {}).get("search_stats")
        if not isinstance(stats, dict):
            bad.append("/online/sources 的 sources.search_stats 缺席（M-2 的账没接到诊断入口）")
        else:
            for key in ("source_timeout_rate", "pool_max_workers", "pool_threads_alive",
                        "cancelled_while_queued", "abandoned_still_running"):
                if key not in stats:
                    bad.append(f"search_stats 缺 {key}")
            print(f"[probe]   账本: 超时率={stats.get('source_timeout_rate')} "
                  f"池上限={stats.get('pool_max_workers')} 在跑={stats.get('pool_threads_alive')}")

        # ② `..` 段必须在任何网络动作之前被拒
        t_before = time.time()
        code, body, took = _post(f"{base}/online/download", {
            "model_name": "hexgrad/Kokoro-82M",
            "file_path": "samples/../../Windows/win.ini",
        }, TIMEOUT)
        shell = json.loads(body.decode("utf-8"))
        # HTTPException 的外壳：reason 在 `detail.reason` 而不是顶层（探针初版读错层，
        # 把产品正确的 400 报成假红——已在 tests/test_u7_online_threads.py 钉成契约）。
        reason = (shell.get("detail") or {}).get("reason") if isinstance(shell, dict) else None
        print(f"[probe] /online/download(..) -> {code} t={took:.2f}s reason={reason}")
        if code != 400:
            bad.append(f"`..` 段路径期望 400，实得 {code}: {body[:120]!r}")
        elif reason != "bad_repo":
            bad.append(f"400 的 detail.reason 不是 bad_repo: {shell}")
        # 只认**本次请求之后**新落盘的东西：目录里可能本来就有历史中断残留，
        # 拿它们当判据会假红。
        out_dir = Path("data/voices/online")
        fresh = [p for p in (out_dir.rglob("*") if out_dir.exists() else [])
                 if p.is_file() and p.stat().st_mtime >= t_before]
        if fresh:
            bad.append(f"被拒的请求还是落了半截产物: {[str(p) for p in fresh][:3]}")

        # ③ 搜索：不许挂住，失败要结构化
        budget = voice_online.ROUTE_DEADLINE_S
        code, body, took = _get(f"{base}/online/search?query=voice&limit=5", TIMEOUT)
        payload = json.loads(body.decode("utf-8")) if body else {}
        print(f"[probe] /online/search -> {code} {len(body)}B t={took:.2f}s "
              f"total={payload.get('total')} ok={[s.get('source') for s in payload.get('sources', []) if s.get('ok')]}")
        if took > budget + 12:
            bad.append(f"搜索端点墙钟 {took:.1f}s 远超路由预算 {budget:.0f}s（挂住形态）")
        if code == 200:
            if payload.get("status") != "ok" or payload.get("fabricated") is not False:
                bad.append(f"200 但外壳不对: {list(payload)[:6]}")
            if not payload.get("results"):
                bad.append("200 却零条目且没走失败出口（可疑假成功）")
        elif code in (502, 504):
            det = payload.get("detail") or {}
            if not det.get("reason"):
                bad.append(f"{code} 但 detail 里没有 reason: {str(payload)[:160]}")
            print(f"[probe]   如实失败: reason={det.get('reason')}")
        else:
            bad.append(f"搜索端点出现未预期状态码 {code}: {body[:160]!r}")

        # 收尾再采一次账：路由没超时就不该有放弃数
        final = voice_online.search_stats()
        print(f"[probe] 收尾账本: { {k: v for k, v in final.items() if k != 'recent'} }")
    finally:
        server.should_exit = True
        thread.join(timeout=15)

    if bad:
        print("FAIL:")
        for b in bad[:8]:
            print("  -", b)
        return 1
    print("PASS: 诊断端点带账、`..` 段 400 先于任何网络动作、搜索端点不挂住且出口结构化")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
