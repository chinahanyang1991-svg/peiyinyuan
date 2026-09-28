"""U-6 真实 HTTP 复采：把 LLM 那 5 枚端点经 uvicorn 真端口跑一遍。

单测用的是 ASGITransport（进程内）；这里要的是**套接字层面**的判据：
Sidecar 实际不可达时，响应码/响应体在真实 HTTP 上长成什么样。
只挂 llm 这一条路由，不碰引擎/模型装载，故不吃 voxcpm 的内存。
"""
import json
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import httpx
import uvicorn
from fastapi import FastAPI

from backend.api.llm import router as llm_router

PORT = 8199
BASE = f"http://127.0.0.1:{PORT}"

app = FastAPI()
app.include_router(llm_router, prefix="/api/llm")

TEXT = "第一章 起\n他转身走了。" * 20


def main() -> int:
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(80):
        if server.started:
            break
        time.sleep(0.05)
    else:
        print("起不来，测量无效")
        return 2

    bad = []
    try:
        # trust_env=False：本机注册了 HTTP/HTTPS 代理，它会拦对 127.0.0.1 的请求
        # （GET 侥幸转发、POST 直接 WinError 10054 复位）。探针打的是本地套接字，
        # 必须绕开环境代理；被测代码到 Sidecar 的出网照旧走代理——那才是要复现的形态。
        with httpx.Client(timeout=60, trust_env=False) as c:
            for path in ("/api/llm/ready", "/api/llm/health"):
                r = c.get(BASE + path)
                print(f"GET  {path:26s} -> {r.status_code} {json.dumps(r.json(), ensure_ascii=False)[:200]}")
                if r.status_code != 200 or "reason" not in r.json():
                    bad.append(path)

            bodies = {
                "/api/llm/detect-chapters": {"text": TEXT},
                "/api/llm/polish": {"text": TEXT},
                "/api/llm/optimize-ssml": {"text": TEXT},
                "/api/llm/analyze-sentiment": {"text": TEXT},
                "/api/llm/parse-voice": {"description": "低沉有磁性的中年男声"},
            }
            for path, payload in bodies.items():
                r = c.post(BASE + path, json=payload)
                code = r.status_code
                detail = r.json() if code == 200 else r.json().get("detail", "")
                print(f"POST {path:26s} -> {code} {json.dumps(detail, ensure_ascii=False)[:220]}")
                # 判据：要么 503 且 detail 里有人话原因；要么 200 且带 status/source/reason
                if code == 503:
                    if not (isinstance(detail, str) and len(detail) > 10):
                        bad.append(path + " (503 但没原因)")
                elif code == 200:
                    if detail.get("status") not in ("ok", "degraded"):
                        bad.append(path + " (status 不在契约内)")
                    if detail.get("status") == "degraded" and not detail.get("reason"):
                        bad.append(path + " (degraded 但没原因)")
                else:
                    bad.append(f"{path} (意外状态码 {code})")
    finally:
        server.should_exit = True
        time.sleep(0.2)

    print("\n===== U-6 真实 HTTP 复采 =====")
    if bad:
        print("FAIL: " + " | ".join(bad))
        return 1
    print("PASS: 无一枚端点出现「200 + 空 + 无原因」")
    return 0


if __name__ == "__main__":
    sys.exit(main())
