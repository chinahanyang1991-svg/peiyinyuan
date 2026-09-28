"""U-7 真路径探针：并发 10 次跨源在线搜索，量**线程到底积不累积**。

单测用的是假源（`tests/test_u7_online_threads.py`），这里走**真网络 + 真爬虫**：
`voice_service.hf_crawler`（hf-mirror 实测可用）与 `voice_service.github_crawler`
（api.github.com 实测可用）。判据不是"请求返回了"，而是这几条：

  ① 10 枚并发请求期间，共享池内活跃线程峰值 ≤ 并行度上限（默认 6）；
  ② 每一枚真正落到爬虫的作业都跑在带 `voice-online` 前缀的池线程上（不是按请求新建的池）；
  ③ 搜索全部收尾后池线程数回到基线，不随请求累积；
  ④ 记账自洽：`source_jobs == 2 × searches`，超时率与"放弃时仍在跑"的作业数都有值；
  ⑤ 不许出现编造条目：`fabricated` 恒 False，且死源必须带真实 reason/error（不静默）。

注：GitHub 未认证搜索配额是 10 次/分钟 —— 10 枚并发里后面几枚**如实**会是
`rate_limited`。这不是缺陷，是探针故意要的形态：源死了要在 `sources[]` 里说清，
而不是端一份假列表糊过去。

跑法（仓库根，需要出口网络）：
    PYTHONUTF8=1 python -X utf8 qa/harness/run_u7_thread_probe.py            # 默认 10 枚
    PYTHONUTF8=1 python -X utf8 qa/harness/run_u7_thread_probe.py --n 20     # 20 枚并发
"""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

CONCURRENCY = int(os.getenv("U7_PROBE_CONCURRENCY", "10"))
QUERIES = ["女声", "voice", "中文 男声", "robot", "anime"]


def _apply_argv(argv: list[str]) -> None:
    """`--n 20` 与 env 等价；**未识别的参数一律报错**。

    写死这条是因为实测踩过：给探针传了个它不认的 `--n 20`，它不报错、按默认 10 跑完
    并打印 PASS——那枚"20 枚并发"的证据根本不存在。静默忽略参数的探针比会红的探针更坏。
    """
    global CONCURRENCY
    if not argv:
        return
    if len(argv) == 2 and argv[0] in ("--n", "-n") and argv[1].isdigit():
        CONCURRENCY = max(2, int(argv[1]))
        return
    raise SystemExit(f"未识别的参数 {argv}（用法：[--n N]，或设 U7_PROBE_CONCURRENCY）")


def main() -> int:
    from backend.services import voice_online
    from backend.services.voice_service import voice_service     # 真爬虫（import 慢）

    print(f"[probe] 池并行度上限 = {voice_online.SEARCH_POOL_SIZE} "
          f"| 单源预算 = {voice_online.SOURCE_DEADLINE_S}s | 总预算 = {voice_online.ROUTE_DEADLINE_S}s")
    base_threads = voice_online.pool_threads_alive()
    print(f"[probe] 起点池线程数 = {base_threads}")

    peak = {"pool": base_threads, "total": threading.active_count()}
    seen_threads: set[str] = set()
    stop = threading.Event()

    real_hf_search = voice_service.hf_crawler.search_with_status
    real_gh_search = voice_service.github_crawler.search_with_status

    def wrap(crawler, fn):
        def inner(*a, **kw):
            seen_threads.add(threading.current_thread().name)
            return fn(*a, **kw)
        return inner

    voice_service.hf_crawler.search_with_status = wrap("hf", real_hf_search)
    voice_service.github_crawler.search_with_status = wrap("gh", real_gh_search)

    def watch():
        while not stop.is_set():
            peak["pool"] = max(peak["pool"], voice_online.pool_threads_alive())
            peak["total"] = max(peak["total"], threading.active_count())
            time.sleep(0.005)

    monitor = threading.Thread(target=watch, name="u7-probe-watcher")
    started = time.monotonic()
    monitor.start()
    results: list[dict] = []
    lock = threading.Lock()

    def worker(i: int):
        payload = voice_online.search_all(QUERIES[i % len(QUERIES)], source="all", limit=5)
        with lock:
            results.append(payload)

    callers = [threading.Thread(target=worker, args=(i,), name=f"u7-caller-{i}")
               for i in range(CONCURRENCY)]
    for t in callers:
        t.start()
    for t in callers:
        t.join(timeout=voice_online.ROUTE_DEADLINE_S + 30)
    stop.set()
    monitor.join(timeout=5)
    wall = time.monotonic() - started

    bad = []
    ok_sources, dead_sources = [], []
    for payload in results:
        if payload.get("fabricated") is not False:
            bad.append("fabricated 不为 False")
        for s in payload.get("sources", []):
            if s.get("ok"):
                ok_sources.append(s)
            else:
                dead_sources.append((s.get("source"), s.get("reason")))
                if not (s.get("error") or s.get("reason")):
                    bad.append(f"死源 {s.get('source')} 既无 error 也无 reason（静默失败）")
        for item in payload.get("results", []):
            if not item.get("model_id"):
                bad.append(f"结果条目缺 model_id: {list(item)[:4]}")
            if item.get("source") not in ("huggingface", "github"):
                bad.append(f"结果条目 source 不在册: {item.get('source')!r}")

    stats = voice_online.search_stats()
    end_threads = voice_online.pool_threads_alive()
    print(f"[probe] 请求 {len(results)}/{CONCURRENCY} 枚返回，墙钟 {wall:.2f}s")
    print(f"[probe] 池线程峰值 = {peak['pool']}（上限 {voice_online.SEARCH_POOL_SIZE}）"
          f"| 进程线程峰值 = {peak['total']} | 收尾池线程 = {end_threads}")
    print(f"[probe] 落到爬虫的线程名 {len(seen_threads)} 枚: {sorted(seen_threads)}")
    print(f"[probe] 活源 {len(ok_sources)} / 死源 {len(dead_sources)} -> "
          f"{sorted(set(dead_sources))[:6]}")
    print(f"[probe] 记账: { {k: v for k, v in stats.items() if k != 'recent'} }")
    print(f"[probe] 条目合计 {sum(len(p.get('results', [])) for p in results)} 枚（全部来自真响应）")

    if len(results) != CONCURRENCY:
        bad.append(f"只有 {len(results)}/{CONCURRENCY} 枚请求收尾")
    if peak["pool"] > voice_online.SEARCH_POOL_SIZE:
        bad.append(f"池线程峰值 {peak['pool']} 超上限 {voice_online.SEARCH_POOL_SIZE}（M-1 复发）")
    if seen_threads and not all(n.startswith(voice_online.POOL_THREAD_PREFIX)
                               for n in seen_threads):
        bad.append(f"有作业不在共享池上: {sorted(seen_threads)[:3]}")
    if end_threads > voice_online.SEARCH_POOL_SIZE:
        bad.append(f"收尾后池线程 {end_threads} 仍超上限（线程没回落）")
    if stats["source_jobs"] != 2 * CONCURRENCY:
        bad.append(f"source_jobs={stats['source_jobs']} 应为 {2 * CONCURRENCY}")
    if stats["searches"] != CONCURRENCY:
        bad.append(f"searches={stats['searches']} 应为 {CONCURRENCY}")
    if stats["cancelled_while_queued"] + stats["abandoned_still_running"] != \
            stats["source_timeouts"]:
        bad.append("超时作业数与'撤掉/仍在跑'两项之和对不上（M-2 的账不平）")
    if not ok_sources:
        bad.append("没有任何一个源真的活着 —— 线程判据仍成立，但这一枚不算真路径通过")

    if bad:
        print("FAIL:")
        for b in bad[:8]:
            print("  -", b)
        if len(bad) > 8:
            print(f"  ...（另有 {len(bad) - 8} 条同类）")
        return 1
    print(f"PASS: 并发 {CONCURRENCY} 次真网络搜索，池线程峰值 {peak['pool']} "
          f"(上限 {voice_online.SEARCH_POOL_SIZE})，作业全部有账，无编造条目")
    return 0


if __name__ == "__main__":
    _apply_argv(sys.argv[1:])
    raise SystemExit(main())
