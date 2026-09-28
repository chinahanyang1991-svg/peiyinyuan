"""PY-340：`GET /api/workflows/system/status` 的两条回归护栏。

定罪现场（2026-09-23 08:31 本机实测，非推导）：
- `backend/api/workflows.py` 在 `async def` 里同步跑 `psutil.cpu_percent(interval=1)`，
  该端点自身恒定 1020-1052ms（连跑 4 次分布，排除冷启动伪像）；
- **并发消融**才暴露真正的危害：3 枚该请求串行成 1028 / 2049 / 3069ms，
  其间轻量端点 `/api/health` 从 8ms 被拖到 **1748ms（218 倍）**——
  即"一个可选端点"能冻住整个事件循环，连带卡住正在跑的转换任务进度回调。

因此本文件第 1 条测的是"循环不得被单个请求拖住"，而不是"这个端点快不快"：
单发计时对这类缺陷不具判别力（它一直"只有 1 秒"）。

口径（2026-09-23 21:4x 改）：第 1 条从"单发 max(gaps)"改成"5 连击取 worst 的中位数"。
原因是前者在满载机器上会假红——当日两次实测：一次被外来 `test_cb_freeze.py`
挤进同一时间窗，一次在本机后台有活时把 0.3s 预算顶到 411ms（`1 failed, 900 passed`），
单独重跑该文件却 3 次全绿。真阻塞的特征不是"某一枚尖峰"而是"每一枚请求都冻"，
所以判据换成中位数：预算不动（仍是 0.3s），但噪声不再能决定红绿灯。
"""
import asyncio
import statistics
import time

import httpx
import pytest
from fastapi import FastAPI

from backend.api import workflows

_app = FastAPI()
_app.include_router(workflows.router, prefix="/api/workflows")

STATUS_URL = "/api/workflows/system/status"
# 采集整体已搬进线程池，故循环里最多只剩协程调度。0.3s 是给 Windows
# 定时器精度与 CI 抖动留的余量；修复前该值约 1.0-3.1s，两数之间没有重叠带。
LOOP_STALL_BUDGET_S = 0.3
# 5 连击：奇数才让 median 是实打实的某一次观测值；且 K 太小挡不住"两枚绿三枚红"的抖动
BURSTS = 5
INFLIGHT = 3


async def _client():
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=_app), base_url="http://test")


async def _worst_gap_during_burst(client: httpx.AsyncClient) -> float:
    """一轮"3 枚在飞 + tick 采样"，返回这一轮里循环最大的空隙。"""
    gaps: list[float] = []
    stop = asyncio.Event()

    async def ticker():
        last = time.perf_counter()
        while not stop.is_set():
            await asyncio.sleep(0.01)
            now = time.perf_counter()
            gaps.append(now - last)
            last = now

    task = asyncio.create_task(ticker())
    try:
        results = await asyncio.gather(*(client.get(STATUS_URL) for _ in range(INFLIGHT)))
        await asyncio.sleep(0.05)
    finally:
        stop.set()
        await task

    assert all(r.status_code == 200 for r in results), [r.status_code for r in results]
    assert gaps, "ticker 没跑到，测量无效（夹具问题，不作产品绿）"
    return max(gaps)


async def test_system_status_does_not_stall_the_event_loop():
    """并发消融护栏：在飞 3 枚 status 请求时，循环的空隙必须小于预算。"""
    async with await _client() as c:
        worst_per_burst = [await _worst_gap_during_burst(c) for _ in range(BURSTS)]

    typical = statistics.median(worst_per_burst)
    assert typical < LOOP_STALL_BUDGET_S, (
        f"事件循环被 status 请求同步阻塞：{BURSTS} 连击的每轮 worst = "
        f"{[round(g * 1000) for g in worst_per_burst]}ms，中位数 {typical * 1000:.0f}ms ≥ "
        f"预算 {LOOP_STALL_BUDGET_S * 1000:.0f}ms —— handler 里又出现了阻塞调用"
        f"（对照 PY-340 定罪现场 1748ms）"
    )


async def test_response_shape_and_nonblocking_cpu_reading():
    """CPU 读数必须是"距上次调用的增量"口径，且不得再用 interval 阻塞。"""
    async with await _client() as c:
        body = (await c.get(STATUS_URL)).json()

    assert body["status"] == "ok"
    cpu = body["hardware"]["cpu_usage"]
    assert isinstance(cpu, (int, float)) and 0.0 <= cpu <= 100.0, cpu
    # 修前：该键是 interval=1 的结果；修后请求即刻返回，故整枚请求必须远快于 1s
    assert body["version"]["tts_engine"], body["version"]


async def test_unavailable_dependency_is_reported_not_zeroed(monkeypatch):
    """采集失败必须可见（PY-117 同一条线）：不得用 0/False 冒充"测到了"。"""
    from modules.tts_engine import model_preloader as mp

    def boom(*a, **k):
        raise RuntimeError("preloader 挂了")

    monkeypatch.setattr(mp.model_preloader, "snapshot", boom)
    from backend.services import task_service as ts

    def boom_count(*a, **k):
        raise RuntimeError("DB 不可用")

    monkeypatch.setattr(ts.task_service, "count_tasks", boom_count)

    out = await asyncio.to_thread(workflows._collect_system_status)

    assert out["services"]["tts_model_state"] == "unavailable"
    assert "preloader 挂了" in out["services"]["tts_model_reason"]
    assert out["services"]["task_count_error"] and "DB 不可用" in out["services"]["task_count_error"]
    # 出过错的地方，必须能在响应里读到原因，而不是只剩一个 0
    assert out["services"]["tts_model_loaded"] is False


async def test_task_counts_use_the_real_status_enum(monkeypatch):
    """`count_tasks(status="waiting")` 是模型里没有的值（恒 0）。钉成 queued。"""
    from backend.services import task_service as ts
    from backend.config import Config

    seen = []

    def fake_count(status=None):
        seen.append(status)
        return 3

    monkeypatch.setattr(ts.task_service, "count_tasks", fake_count)
    monkeypatch.setattr(Config, "TTS_ENGINE", "edgetts", raising=False)

    out = await asyncio.to_thread(workflows._collect_system_status)

    assert "waiting" not in seen, seen
    assert "queued" in seen, seen
    assert out["services"]["waiting_tasks"] == 3
    # 引擎名不得再硬编码 voxcpm
    assert out["version"]["tts_engine"] == "edgetts"


async def test_ffmpeg_probe_delegates_to_the_shared_one(monkeypatch):
    """ffmpeg 可用性走 cast 的三级兜底缓存探测，不再自跑 `ffmpeg -version` 只看 PATH。"""
    import backend.api.cast as cast_api

    calls = []

    def fake_probe():
        calls.append(1)
        return {"path": "X", "source": "imageio-ffmpeg", "version": "n6.1", "error": None}

    monkeypatch.setattr(cast_api, "ffmpeg_probe_info", fake_probe)

    out = await asyncio.to_thread(workflows._collect_system_status)

    assert calls, "没有复用共享探测（说明又起了一套自跑的探测）"
    assert out["services"]["ffmpeg_available"] is True
    assert out["services"]["ffmpeg_source"] == "imageio-ffmpeg"
