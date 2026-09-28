"""US-C1 (泳道 C / 波次 1) —— 预热状态机 per-engine Slot 化 + 引擎名断言

事故链（ANALYSIS-OPENVOICE OV-P0-1 / M5，UPGRADE-SPEC US-C1）：
  任务 A 正在后台加载本地引擎（state=LOADING, engine=A）
  → 任务 B 需要本地引擎 C：`pre.start(C)` 命中"单槽 + STATE_LOADING 早退"
  → B 的 `wait_terminal` 等的是**共享 Event**，A 加载完即置位
  → B 看到 state=="ready" 却没人给它加载过 C（假 ready）
  → `_acquire_engine` 末尾在 **async 协程里同步** 调 `get_loaded(C)`
  → 冷启动 220~460s 压在事件循环上 → 整个 uvicorn 停摆、全局心跳哑火。

本文件先"复现事故"（`test_c1_accident_...`，修复前必红），再断言修复后的行为。
引擎全部用桩（慢加载替身），不碰真实权重、不碰 :9880/:9881。
"""

import asyncio
import math
import time

import numpy as np
import pytest

from modules.tts_engine.engine_registry import EngineLoadError, engine_registry
from modules.tts_engine.engines.base_tts import BaseTTSEngine
from modules.tts_engine.model_preloader import (RETRY_LIMIT, STATE_FAILED,
                                                STATE_LOADING, STATE_READY,
                                                model_preloader)

SR = 48000
LOAD_S = 0.8          # 桩引擎"冷启动"耗时（真实世界 220~460s）
TICK_MS = 20.0        # 心跳协程节拍


class SlowEngine(BaseTTSEngine):
    """加载耗时可控的替身引擎：只有真的被 load 过才置 _loaded。"""

    supports_cloning = True
    sample_rate = SR

    def __init__(self, name, load_s=LOAD_S):
        super().__init__()
        self.engine_name = name
        self.engine_display = f"替身 {name}"
        self._load_s = load_s
        self.load_calls = 0

    def load_model(self, model_path: str = None) -> None:
        self.load_calls += 1
        time.sleep(self._load_s)          # 模拟 CPU 冷启动
        self._model = object()
        self._loaded = True

    def synthesize(self, text="", voice_id=None, speed=1.0, pitch=0.0,
                   emotion="neutral", seed=None):
        n = max(int(len(text) * 0.08 * SR), SR)
        t = np.arange(n, dtype=np.float32) / SR
        return (0.5 * np.sin(2 * math.pi * 220 * t)).astype(np.float32)


def _register(name, engine):
    engine_registry._loaded.pop(name, None)
    engine_registry._engines[name] = engine
    engine_registry._engine_factories[name] = lambda: engine
    return engine


@pytest.fixture
def two_slow_engines():
    """两个互不相同的"慢本地引擎"，用完清理注册表与预热槽，绝不污染其它用例。"""
    names = ["w1c_engine_a", "w1c_engine_c"]
    engines = {n: SlowEngine(n) for n in names}
    for n, e in engines.items():
        _register(n, e)
    model_preloader.reset()
    yield engines
    for n in names:
        engine_registry._loaded.pop(n, None)
        engine_registry._engines.pop(n, None)
        engine_registry._engine_factories.pop(n, None)
    model_preloader.reset()


def _acquire(two_slow_engines, engine_name: str):
    """跑真实的 `ConversionWorkflow._acquire_engine`，返回 (引擎, 心跳节拍数, 墙钟)。

    心跳协程每 20ms 记一次数：事件循环一旦被同步冷启动压住，计数就会停住 ——
    这就是"全局心跳哑火"的机器可读形态。
    """
    from modules.workflow.conversion_workflow import conversion_workflow as wf

    beats = {"n": 0}
    emit_msgs = []

    async def heartbeat():
        while True:
            beats["n"] += 1
            await asyncio.sleep(TICK_MS / 1000.0)

    real_emit = wf._emit                      # 单例，用完必须还原

    async def main():
        async def fake_emit(task_id, context, stage, progress, message="", **kw):
            emit_msgs.append((stage, message))
        wf._emit = fake_emit
        hb = asyncio.ensure_future(heartbeat())
        t0 = time.time()
        try:
            engine = await wf._acquire_engine("w1c-task", {}, engine_name)
        finally:
            elapsed = time.time() - t0
            hb.cancel()
            wf._emit = real_emit
        return engine, elapsed, emit_msgs

    engine, elapsed, msgs = asyncio.run(main())
    return engine, elapsed, beats["n"], msgs


# ---------------------------------------------------------------- 事故复现
def test_c1_accident_second_engine_does_not_block_loop(two_slow_engines):
    """复现：A 预热在飞时建任务 B（需要引擎 C）—— 不得在协程里同步冷启动 C。

    修复前（单槽）必红：`start(C)` 被 A 的 LOADING 早退吞掉 → C 从未被后台加载 →
    `_acquire_engine` 在"假 ready"之后于协程内同步加载 C，心跳计数在 LOAD_S 秒内
    几乎不增长（真实世界 = 220~460s 停摆）。
    """
    eng_a = two_slow_engines["w1c_engine_a"]
    eng_c = two_slow_engines["w1c_engine_c"]

    assert model_preloader.start("w1c_engine_a") == STATE_LOADING
    # 事故入口：A 还在 LOADING 时，B 请求另一个本地引擎
    model_preloader.start("w1c_engine_c")

    engine, elapsed, beats, _msgs = _acquire(two_slow_engines, "w1c_engine_c")

    assert engine is eng_c, "取到的必须是本任务要的那个引擎实例"
    assert eng_c.load_calls == 1, "C 的权重必须且只能被后台加载一次"
    assert eng_c.is_loaded()
    # 等待时长 >= C 自己的加载耗时（C 是真等的，不是搭 A 的车）
    assert elapsed >= LOAD_S * 0.9, f"等待时间异常短（假 ready）: {elapsed:.2f}s"
    # 事件循环没被压住：20ms 节拍在 LOAD_S 秒里应有几十次
    expected_min_ticks = int(elapsed / (TICK_MS / 1000.0) * 0.5)
    assert beats >= expected_min_ticks, (
        f"事件循环被同步冷启动阻塞：{elapsed:.2f}s 内心跳仅 {beats} 次 "
        f"(期望 >= {expected_min_ticks}) —— 假 ready 后在协程内加载了 C")


# ---------------------------------------------------------------- 槽位隔离
def test_c1_slots_are_independent_per_engine(two_slow_engines):
    """每个引擎一个槽：A ready / C failed 互不覆盖，等待按引擎各自的 Event。"""
    model_preloader.reset()
    model_preloader.start("w1c_engine_a")
    assert model_preloader.wait_terminal(10, "w1c_engine_a") is True
    assert model_preloader.snapshot("w1c_engine_a")["state"] == STATE_READY

    # C 从未 start —— 它不得因为"A 加载完了"就报 ready（假 ready 是本次要买的性质）
    snap_c = model_preloader.snapshot("w1c_engine_c")
    assert snap_c["state"] == "idle", snap_c
    assert snap_c["engine"] == "w1c_engine_c"
    assert model_preloader.snapshot("w1c_engine_a")["state"] == STATE_READY

    # 各槽的 engine 字段自证，不再是一个共享 _engine_name；
    # 且"查一个没 start 过的引擎"不得顺手造槽（只读查询不写状态）
    assert model_preloader.snapshot("w1c_engine_a")["engine"] == "w1c_engine_a"
    slots = model_preloader.snapshot()["slots"]
    assert "w1c_engine_a" in slots and "w1c_engine_c" not in slots, sorted(slots)


def test_c1_concurrent_two_engines_both_reach_own_ready(two_slow_engines):
    """两个引擎并行请求：串行加载（内存峰值约束）保留，但各自终态各自成立。"""
    model_preloader.reset()
    s1 = model_preloader.start("w1c_engine_a")
    s2 = model_preloader.start("w1c_engine_c")
    assert s1 == STATE_LOADING and s2 == STATE_LOADING, (s1, s2)
    assert model_preloader.wait_terminal(30, "w1c_engine_a")
    assert model_preloader.wait_terminal(30, "w1c_engine_c")
    a = model_preloader.snapshot("w1c_engine_a")
    c = model_preloader.snapshot("w1c_engine_c")
    assert a["state"] == c["state"] == STATE_READY, (a, c)
    assert a["load_seconds"] is not None and c["load_seconds"] is not None
    # 各自只加载一次（绝不重复加载两份权重）
    assert two_slow_engines["w1c_engine_a"].load_calls == 1
    assert two_slow_engines["w1c_engine_c"].load_calls == 1


def test_c1_failed_slot_stays_failed_and_does_not_poison_others(
        monkeypatch, two_slow_engines):
    """加载失败只脏自己那个槽；同槽再次 start 可重试。

    注（#86 回归时发现的既有缺陷）：本用例此前漏挂 `two_slow_engines`，
    `w1c_engine_a` 从来没被注册过 —— "健康槽不受脏槽影响"这条主断言实际是在拿一个
    未注册引擎当健康引擎，无论怎么改预热器都必红。现补上夹具，让 A 真能被加载。

    #86 ENG-PRELOAD 起，装载失败不再一次定生死：后台按 `RETRY_LIMIT` 带上限重试，
    每次之间退避 `RETRY_DELAY_S`（默认 10s）。本用例买的是**槽位隔离**，不是退避时长
    —— 所以把退避归零（旧 `wait_terminal(20)` 的预算会被 2×10s 退避吃光而误判超时）。
    重试次数上限本身的断言在 `tests/test_eng86_preload_force.py`。
    """
    monkeypatch.setattr("modules.tts_engine.model_preloader.RETRY_DELAY_S", 0.0)
    bad = SlowEngine("w1c_engine_bad", load_s=0.05)
    bad.load_model = lambda model_path=None: (_ for _ in ()).throw(
        RuntimeError("权重缺失"))
    _register("w1c_engine_bad", bad)
    try:
        model_preloader.start("w1c_engine_a")
        model_preloader.start("w1c_engine_bad")
        assert model_preloader.wait_terminal(20, "w1c_engine_bad")
        snap_bad = model_preloader.snapshot("w1c_engine_bad")
        assert snap_bad["state"] == STATE_FAILED, snap_bad
        assert "权重缺失" in (snap_bad["error"] or "")
        assert snap_bad["attempts"] == 1 + RETRY_LIMIT, snap_bad
        assert model_preloader.wait_terminal(20, "w1c_engine_a")
        assert model_preloader.snapshot("w1c_engine_a")["state"] == STATE_READY
    finally:
        for d in (engine_registry._loaded, engine_registry._engines,
                  engine_registry._engine_factories):
            d.pop("w1c_engine_bad", None)


def test_c1_reset_engine_scoped():
    """reset(单个引擎) 不清别家槽位。"""
    e = SlowEngine("w1c_engine_r", load_s=0.05)
    _register("w1c_engine_r", e)
    try:
        model_preloader.start("w1c_engine_r")
        assert model_preloader.wait_terminal(20, "w1c_engine_r")
        assert model_preloader.snapshot("w1c_engine_r")["state"] == STATE_READY
        model_preloader.reset("w1c_engine_r")
        assert model_preloader.snapshot("w1c_engine_r")["state"] == "idle"
    finally:
        model_preloader.reset()
        for d in (engine_registry._loaded, engine_registry._engines,
                  engine_registry._engine_factories):
            d.pop("w1c_engine_r", None)


# ---------------------------------------------------------------- 向后兼容
def test_c1_snapshot_keeps_legacy_keys_for_model_status():
    """/api/system/model_status 的旧 `model` 载荷形状不破（state/engine/message/...）。"""
    snap = model_preloader.snapshot()
    for key in ("state", "engine", "load_seconds", "elapsed_seconds",
                "error", "skipped_reason", "message"):
        assert key in snap, f"旧键 {key} 丢失 —— 前端/system 页会读到 None"
    assert isinstance(snap["message"], str) and snap["message"]
    # 新增载荷
    assert "slots" in snap and isinstance(snap["slots"], dict)
    assert "detail" in snap


def test_c1_acquire_engine_refuses_cross_engine_false_ready(monkeypatch,
                                                            two_slow_engines):
    """防御断言：即便槽位被"张冠李戴"，_acquire_engine 也绝不放行去同步加载。"""
    from modules.workflow import conversion_workflow as cw_mod

    model_preloader.start("w1c_engine_a")
    assert model_preloader.wait_terminal(20, "w1c_engine_a")

    # 伪造一个"只认识 A"的预热器：C 的槽永远返回 A 的 ready
    real_snapshot = model_preloader.snapshot

    def lying_snapshot(engine_name=None):
        s = real_snapshot(None if engine_name is None else "w1c_engine_a")
        if engine_name == "w1c_engine_c":
            s = dict(s)
            s["engine"] = "w1c_engine_a"
        return s

    monkeypatch.setattr(model_preloader, "snapshot", lying_snapshot)
    monkeypatch.setattr(cw_mod.ConversionWorkflow, "_emit",
                        lambda *a, **k: asyncio.sleep(0))
    with pytest.raises(EngineLoadError) as ei:
        asyncio.run(cw_mod.ConversionWorkflow()._acquire_engine(
            "t", {}, "w1c_engine_c"))
    assert "w1c_engine_c" in str(ei.value) or "engine" in str(ei.value).lower()
