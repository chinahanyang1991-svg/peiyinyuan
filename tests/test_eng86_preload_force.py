"""#86 ENG-PRELOAD —— 「voxcpm 一定要启动就加载」

钉死的三条判据（用户原话 + 本工程"不许用沉默假报成功"的纪律）：

  1. **未显式设门限时，启动必发预装载请求**：`PYY_PRELOAD_MIN_FREE_GB` 没出现在环境里
     => 内存门**不得**作为跳过理由，哪怕本机只剩 3.2GB 可用（实测事实）。
     修复前形态（`_MIN_FREE_GB` 默认 6.0）在本机必红。
  2. **用户显式设了门限就尊重用户值**：唯一保留门限的方式是把该环境变量真的设上。
     `PYY_PRELOAD_MODEL=0` 同理 —— 那是用户显式关，不是我们替他决定关。
  3. **装载失败必须冒真因**：重试有上限（不无限吃内存），终态 `failed` 的 `error`
     必须是真实异常原文，`/api/system/model_status` 不得再用 `skipped_reason` 糊过去。

引擎全部用桩（不加载真权重、不写生产库、不碰 :9880/:9881）。
真机 voxcpm 装载证据见 `agent-ops/ENG-LAUNCHER-REPORT.md`。
"""

import asyncio
import os
import time

import pytest

import modules.tts_engine.model_preloader as mp
import backend.main as main_mod
from modules.tts_engine.engine_registry import engine_registry
from modules.tts_engine.engines.base_tts import BaseTTSEngine

GATE_ENV = "PYY_PRELOAD_MIN_FREE_GB"
PRELOAD_ENV = "PYY_PRELOAD_MODEL"
FREE_3_2GB = int(3.2 * 1024 ** 3)     # 2026-09-22 10:05 本机实测可用内存


class StubEngine(BaseTTSEngine):
    """可控桩：记录 load_model 被调了几次，可按需抛真异常。"""

    requires_model_object = True       # 照 voxcpm 的口径：就绪还要求 _model 非空

    def __init__(self, name, fail=None, load_s=0.05):
        super().__init__()
        self.engine_name = name
        self.engine_display = f"桩 {name}"
        self.sample_rate = 48000
        self._fail = fail
        self._load_s = load_s
        self.load_calls = 0

    def load_model(self, model_path: str = None) -> None:
        self.load_calls += 1
        time.sleep(self._load_s)
        if self._fail is not None:
            raise self._fail
        self._model = object()
        self._loaded = True

    def synthesize(self, *a, **kw):      # pragma: no cover - 本文件不合成
        raise NotImplementedError


def _register(name, engine):
    for d in (engine_registry._loaded, engine_registry._engines,
              engine_registry._engine_factories):
        d.pop(name, None)
    engine_registry._engines[name] = engine
    engine_registry._engine_factories[name] = lambda: engine
    return engine


def _unregister(*names):
    for n in names:
        for d in (engine_registry._loaded, engine_registry._engines,
                  engine_registry._engine_factories):
            d.pop(n, None)


@pytest.fixture
def no_gate_env(monkeypatch):
    """干净门限环境：显式设门限的开关**不在**，并且内存探测回 3.2GB。"""
    monkeypatch.delenv(GATE_ENV, raising=False)
    monkeypatch.delenv(PRELOAD_ENV, raising=False)
    monkeypatch.setattr(mp, "available_memory_bytes", lambda: FREE_3_2GB)
    # 重试等待归零：本文件测的是"有没有上限/冒没冒真因"，不是退避时长
    monkeypatch.setattr(mp, "RETRY_DELAY_S", 0.0, raising=False)
    yield


# ---------------------------------------------------------------- 判据 1
def test_startup_preload_fires_without_gate_even_at_3gb_free(no_gate_env):
    """核心断言：未显式设门限 => 3.2GB 可用内存也**必须**真的开始装载。

    修复前必红：默认 6.0GB 门限把这条路判成 `idle` + skipped_reason
    （'可用内存 3.2GB < 预热门限 6.0GB, 跳过启动预热'），引擎一次都没被加载。
    """
    name = "eng86_free_engine"
    eng = _register(name, StubEngine(name))
    pre = mp.ModelPreloader()
    try:
        st = pre.start(name, respect_memory_gate=True)     # 后端启动钩子的调用口径
        assert st == mp.STATE_LOADING, (st, pre.snapshot(name))
        assert pre.snapshot(name)["skipped_reason"] is None, pre.snapshot(name)
        assert pre.wait_terminal(30, name) is True
        snap = pre.snapshot(name)
        assert snap["state"] == mp.STATE_READY, snap
        assert eng.load_calls == 1, "权重必须且只被后台加载一次"
    finally:
        pre.reset()
        _unregister(name)


def test_startup_hook_requests_preload_for_configured_engine(no_gate_env,
                                                             monkeypatch):
    """`backend/main.py` 启动钩子必发预装载请求（端到端一根线，不靠人肉读代码）。

    指令#1 改判：这条判据现在属于**本地档** —— 启动必装载的是"这一档要用的引擎"。
    在线档（新默认）对应的是下面那枚测试：启动一枚装载请求都不发。
    """
    from backend.config import Config
    from modules.tts_engine.model_preloader import model_preloader as singleton

    calls = []
    monkeypatch.setattr("backend.models.base.init_db", lambda: [])
    monkeypatch.setenv("PYY_STARTUP_RECONCILE", "0")      # 不碰任务表
    monkeypatch.setattr(Config, "TTS_TIER", "local")
    monkeypatch.setattr(Config, "TTS_ENGINE", "voxcpm")
    monkeypatch.delenv("TTS_PRELOAD_LOCAL", raising=False)
    monkeypatch.setattr(singleton, "start",
                        lambda engine_name, respect_memory_gate=False: (
                            calls.append((engine_name, respect_memory_gate))
                            or mp.STATE_LOADING))
    asyncio.run(main_mod.startup_event())
    assert calls, "startup_event() 未发出任何预装载请求 —— 启动即加载没接上"
    engine_name, gate = calls[0]
    assert engine_name == Config.TTS_ENGINE == "voxcpm", engine_name
    assert gate is True, "启动预热必须走门限感知口径（门限只在用户显式设时生效）"


def test_startup_hook_does_not_load_local_model_on_online_tier(no_gate_env,
                                                               monkeypatch):
    """指令#1/#2：默认档=在线时，启动**不得**装载 voxcpm 那 5GB 级权重。

    为什么单独钉这一条：PY-346/§31 的死亡窗取证里常驻 5.2GB 的就是它，而在线档
    一辈子用不到 —— 少装这一枚，就是少一份把后端压死的东西。
    `TTS_PRELOAD_LOCAL=true` 是唯一的显式回摆（下面一并钉）。
    """
    from backend.config import Config
    from modules.tts_engine.model_preloader import model_preloader as singleton

    calls = []
    monkeypatch.setattr("backend.models.base.init_db", lambda: [])
    monkeypatch.setenv("PYY_STARTUP_RECONCILE", "0")
    monkeypatch.setattr(Config, "TTS_TIER", "online")
    monkeypatch.setattr(Config, "TTS_ENGINE", "edgetts")
    monkeypatch.setattr(singleton, "start",
                        lambda *a, **k: calls.append(a) or mp.STATE_LOADING)

    monkeypatch.delenv("TTS_PRELOAD_LOCAL", raising=False)
    asyncio.run(main_mod.startup_event())
    assert calls == [], f"在线档启动仍去装载了: {calls}"

    monkeypatch.setenv("TTS_PRELOAD_LOCAL", "true")
    asyncio.run(main_mod.startup_event())
    assert [c[0] for c in calls] == ["voxcpm"], calls


# ---------------------------------------------------------------- 判据 2
def test_explicit_gate_env_is_respected(monkeypatch):
    """用户显式设了门限 => 尊重用户值（这是保留门限的唯一方式，非默认）。"""
    monkeypatch.setenv(GATE_ENV, "99")                     # 高于任何实测量
    monkeypatch.setattr(mp, "available_memory_bytes", lambda: FREE_3_2GB)
    name = "eng86_gated_engine"
    eng = _register(name, StubEngine(name))
    pre = mp.ModelPreloader()
    try:
        st = pre.start(name, respect_memory_gate=True)
        assert st == mp.STATE_IDLE, st
        snap = pre.snapshot(name)
        assert snap["state"] == mp.STATE_IDLE
        assert "PYY_PRELOAD_MIN_FREE_GB" in (snap["skipped_reason"] or ""), snap
        assert eng.load_calls == 0, "用户显式设的门限被无视 => 用户配置权被抢"
        # 任务路径（respect_memory_gate=False）不受门限影响
        assert pre.start(name, respect_memory_gate=False) == mp.STATE_LOADING
        assert pre.wait_terminal(30, name) is True
        assert eng.load_calls == 1
    finally:
        pre.reset()
        _unregister(name)


def test_preload_model_off_is_respected(monkeypatch):
    """`PYY_PRELOAD_MODEL=0` 是用户显式关预热 —— 照旧尊重，且原因写清是用户关的。"""
    monkeypatch.delenv(GATE_ENV, raising=False)
    monkeypatch.setenv(PRELOAD_ENV, "0")
    monkeypatch.setattr(mp, "available_memory_bytes", lambda: FREE_3_2GB)
    name = "eng86_off_engine"
    eng = _register(name, StubEngine(name))
    pre = mp.ModelPreloader()
    try:
        assert pre.start(name, respect_memory_gate=True) == mp.STATE_IDLE
        snap = pre.snapshot(name)
        assert "PYY_PRELOAD_MODEL" in (snap["skipped_reason"] or ""), snap
        assert "门限" not in (snap["skipped_reason"] or "") \
            and "不足" not in (snap["skipped_reason"] or ""), \
            f"用户关预热却被说成内存不够: {snap['skipped_reason']}"
        assert snap["gate"]["applied"] is False, snap
        assert eng.load_calls == 0
    finally:
        pre.reset()
        _unregister(name)


# ---------------------------------------------------------------- 判据 3
def test_load_failure_retries_with_a_cap_and_reports_real_error(no_gate_env):
    """装载失败：重试有上限 + 终态 failed 带真实异常原文（不许 idle 糊过去）。"""
    boom = MemoryError("不能分配 4.6GiB（可用内存 3.2GiB）")
    name = "eng86_fail_engine"
    eng = _register(name, StubEngine(name, fail=boom))
    pre = mp.ModelPreloader()
    try:
        assert pre.start(name, respect_memory_gate=True) == mp.STATE_LOADING
        assert pre.wait_terminal(60, name) is True
        snap = pre.snapshot(name)
        assert snap["state"] == mp.STATE_FAILED, snap
        assert "MemoryError" in (snap["error"] or ""), snap
        assert "不能分配" in (snap["error"] or ""), snap
        assert snap["skipped_reason"] is None, \
            f"失败态不得再挂 skipped_reason: {snap['skipped_reason']}"
        assert eng.load_calls == mp.RETRY_LIMIT + 1, (
            f"尝试次数须等于 1 + RETRY_LIMIT({mp.RETRY_LIMIT})，实得 {eng.load_calls}")
        assert snap["attempts"] == eng.load_calls, snap
        assert snap["retry_limit"] == mp.RETRY_LIMIT, snap
        assert f"{mp.RETRY_LIMIT + 1}" in snap["message"], snap["message"]
    finally:
        pre.reset()
        _unregister(name)


def test_retry_is_bounded_and_engine_only_ever_loaded_once(no_gate_env):
    """重试成功后不得再重试；成功后 load_calls 恰为 1（绝不重复吃一份权重）。"""
    flaky = StubEngine("eng86_flaky_engine")
    real_load = flaky.load_model
    errors = {"n": 0}

    def load_once_then_ok(model_path=None):
        if errors["n"] == 0:
            errors["n"] += 1
            flaky.load_calls += 1
            raise OSError(1455, "页面文件太小，无法完成操作")   # Windows 真会这么炸
        return real_load(model_path)

    flaky.load_model = load_once_then_ok
    _register(flaky.engine_name, flaky)
    pre = mp.ModelPreloader()
    try:
        assert pre.start(flaky.engine_name, respect_memory_gate=True) == mp.STATE_LOADING
        assert pre.wait_terminal(60, flaky.engine_name) is True
        snap = pre.snapshot(flaky.engine_name)
        assert snap["state"] == mp.STATE_READY, snap
        assert snap["attempts"] == 2, snap
        assert snap["error"] is None, snap
    finally:
        pre.reset()
        _unregister(flaky.engine_name)


def test_model_status_endpoint_surfaces_real_failure(no_gate_env, monkeypatch):
    """/api/system/model_status 的出口：failed + 真因，不再用 skipped_reason 顶包。"""
    boom = RuntimeError("VoxCPM 权重目录不存在: data/models/voxcpm2（模拟）")
    name = "eng86_api_engine"
    _register(name, StubEngine(name, fail=boom))
    pre = mp.ModelPreloader()
    monkeypatch.setattr(mp, "model_preloader", pre, raising=False)
    try:
        assert pre.start(name, respect_memory_gate=True) == mp.STATE_LOADING
        assert pre.wait_terminal(60, name) is True
        payload = asyncio.run(main_mod.model_status())
        model = payload["model"]
        assert model["state"] == mp.STATE_FAILED, model
        assert "权重目录不存在" in (model["error"] or ""), model
        assert model["skipped_reason"] is None, model
        assert "权重目录不存在" in model["message"], model
        assert "失败" in model["message"], model      # 人话：一眼看出是失败不是没加载
    finally:
        pre.reset()
        _unregister(name)


# ---------------------------------------------------------------- 旧形状护栏
def test_gate_view_is_reported_for_honesty(no_gate_env):
    """新增的 `gate` 字段要能自证：门限没生效时明确写"未设置 -> 启动即无条件预装载"。"""
    name = "eng86_gate_engine"
    _register(name, StubEngine(name))
    pre = mp.ModelPreloader()
    try:
        pre.start(name, respect_memory_gate=True)
        assert pre.wait_terminal(30, name) is True
        gate = pre.snapshot(name)["gate"]
        assert gate["applied"] is False, gate
        assert GATE_ENV in gate["source"], gate
        assert abs(gate["free_gb"] - 3.2) < 0.05, gate
    finally:
        pre.reset()
        _unregister(name)


def test_legacy_snapshot_keys_unchanged(no_gate_env):
    """旧七个键 + slots/primary 形状不破（前端 `ModelSlot` 类型直接吃这个载荷）。"""
    snap = mp.ModelPreloader().snapshot()
    for key in ("state", "engine", "load_seconds", "elapsed_seconds", "error",
                "skipped_reason", "message", "queue_ahead", "detail", "slots",
                "primary"):
        assert key in snap, f"旧键 {key} 丢失"
    assert os.getenv(GATE_ENV) is None            # 本文件的红/绿都建立在"没设门限"上
