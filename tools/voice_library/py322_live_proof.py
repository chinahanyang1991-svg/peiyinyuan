"""PY-322 真机复跑：试听预算到点必须**终止执行者**，并在窗口内让同键请求拿 poisoned 拒答。

口径（不采信任何一方自述，只看 HTTP + 引擎日志两侧对表）：
  * T0 发一枚 `quality=high&regenerate=1&wait=240`（无样本音色 → 必然现合成，
    CPU 档实测单枚要 30-44 分钟，所以它**一定**会撞到 180s 预算）。
  * 从 T0+170s 起每 2s 用**同一 voice_id + 同一 quality** 再打一枪（不带 regenerate），
    观察预算到点前后返回码的切换：`in_progress`(503) → 冷却期内复用 `timeout`(504)。
  * T0+181s 起补打 `regenerate=1` 的 force 枪（**只在 poisoned 窗口内**，命中 429 即停）：
    不带 regenerate 的复探按口径只会复用冷却结论，打不到 `engine_busy_cancelling`。
  * 主请求返回后，立刻打一枪默认档 `quality=fast`：**能秒回 = 串行锁真还回来了**
    （这才是"执行者真停了"的行为面证据，日志文案不作数）。

用法：python -X utf8 tools/voice_library/py322_live_proof.py
"""

import json
import threading
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8000"
VOICE = "4b60f1e7-cafb-4850-8c24-ba7a2b2a8f77"      # 内置·标准女声-温柔·无样本
BUDGET_S = 180.0
POLL_FROM_S = 170.0
POLL_UNTIL_S = 215.0
# 第二跑（19:57）实测：同键**不带 regenerate** 的复探在冷却期内一律复用 timeout 结论
# （18 枪全 504、rt 0.01s），按 `voice_preview` 的口径边界这是**设计如此**，
# 也就永远打不到 `engine_busy_cancelling` 429。想验证 poisoned 拒答只有一条路：
# 预算到点后、**仍在 poisoned 窗口内**时打一枪 `regenerate=1`。
# 窗口外打 force 会合法重开任务，所以这里到点即停（首枚 429 就收手）。
FORCE_FROM_S = BUDGET_S + 1.0
FORCE_UNTIL_S = BUDGET_S + 17.0

_t0 = time.time()


def stamp():
    return f"+{time.time() - _t0:6.1f}s"


def get(path, timeout=300):
    """返回 (http_code, 小写 reason 摘要, 原始 body 前 300 字)。"""
    req = urllib.request.Request(BASE + path, headers={"User-Agent": "pyy-py322"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read(4000)
            return r.status, "audio" if r.headers.get(
                "content-type", "").startswith("audio/") else body[:200].decode(
                    "utf-8", "replace"), body
    except urllib.error.HTTPError as e:
        body = e.read(4000)
        try:
            detail = json.loads(body.decode("utf-8", "replace")).get("detail") or {}
            code = detail.get("code") or detail.get("reason") or "?"
        except Exception:
            code = "?"
        return e.code, str(code), body[:300].decode("utf-8", "replace")
    except Exception as e:
        return None, f"{type(e).__name__}: {e}", ""


main_result = {}
polls = []
forced = []


def run_main():
    t = time.time()
    code, why, body = get(
        f"/api/voices/{VOICE}/preview?quality=high&regenerate=1&wait=240")
    main_result.update(http_code=code, reason=why, elapsed_s=round(
        time.time() - t, 1), body=body[:300])
    print(f"[{stamp()}] 主请求(high) -> HTTP {code} reason={why} "
          f"耗时 {main_result['elapsed_s']}s", flush=True)


def run_polls():
    while time.time() - _t0 < POLL_UNTIL_S:
        el = time.time() - _t0
        if el >= POLL_FROM_S:
            t = time.time()
            code, why, _ = get(f"/api/voices/{VOICE}/preview?quality=high", timeout=25)
            polls.append({"at_s": round(el, 1), "http": code, "reason": why,
                          "rt_s": round(time.time() - t, 2)})
            print(f"[{stamp()}] 同键复探(high) -> HTTP {code} reason={why}", flush=True)
            time.sleep(2.0)
        else:
            time.sleep(0.5)


def run_force_probe():
    """poisoned 窗口内打 force：期望 429 `engine_busy_cancelling`。

    命中即停 —— 窗口外这一枪会**合法重开**一枚 180s 任务，不能乱打。
    """
    while time.time() - _t0 < FORCE_UNTIL_S:
        el = time.time() - _t0
        if el >= FORCE_FROM_S:
            t = time.time()
            code, why, _ = get(
                f"/api/voices/{VOICE}/preview?quality=high&regenerate=1", timeout=25)
            forced.append({"at_s": round(el, 1), "http": code, "reason": why,
                           "rt_s": round(time.time() - t, 2)})
            print(f"[{stamp()}] force 复探 -> HTTP {code} reason={why}", flush=True)
            if code == 429:
                print(f"[{stamp()}] 命中 poisoned 拒答，收手（不再冒重开任务的风险）",
                      flush=True)
                return
            time.sleep(1.5)
        else:
            time.sleep(0.5)


th_main = threading.Thread(target=run_main)
th_poll = threading.Thread(target=run_polls)
th_force = threading.Thread(target=run_force_probe)
th_main.start()
th_poll.start()
th_main.join()                       # force 只在主请求到点之后进窗口
th_force.start()
th_poll.join()

print(f"\n[{stamp()}] === 执行者是否真停：同键立即打一枪默认档 ===", flush=True)
t = time.time()
code, why, body = get(f"/api/voices/{VOICE}/preview?quality=fast&wait=25")
print(f"[{stamp()}] 默认档(fast) -> HTTP {code} reason={why} 耗时 {time.time()-t:.1f}s",
      flush=True)

out = {"main": main_result, "polls": polls, "forced": forced,
       "fast_after": {"http": code, "reason": why,
                      "elapsed_s": round(time.time() - t, 1)}}
p = "agent-ops/py322_live_proof.json"
with open(p, "w", encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False, indent=1)
print(f"\n已落盘 {p}", flush=True)
