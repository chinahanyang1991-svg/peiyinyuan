"""升级波次 · 泳道 C — US-C5 / US-C6（VoxCPM 适配器：推理互斥闸 + 冷启动分档计时）

对应规格：
  * `ANALYSIS-VOXCPM.md` P0-1③④（单实例无并发保护）、P0-5（CPU 档不该要 torch.compile
    与 warm-up）、P1-6①（流式收了 voice_id 不用 = 丢音色）
  * `ANALYSIS-OPENVOICE.md` OV-P0-1（冷启动分档 + 计时可见）
  * `UPGRADE-SPEC.md` 泳道 C 的 US-C5 / US-C6 两行

清洁室：仅参考"单实例串行""按设备分档"这类**思想**，未复制任何外部源码文本/注释；
锁的写法照本仓库既有 `gptsovits_adapter._serial_lock` 范式。

全部用假内核（`FakeVoxCPM`），**不加载真权重、不碰 :9880/:9881、不写生产库**。
"""

import inspect
import sys
import threading
import time
import types

import numpy as np
import pytest

import modules.tts_engine.model_preloader as mp
from modules.tts_engine.engines.voxcpm_adapter import VoxCPMAdapter

HOLD_S = 0.06           # 假推理耗时，用来把"是否重叠"变成可测事实
EXISTING_DIR = str(__import__("pathlib").Path(__file__).resolve().parent)  # 任意真实目录


class FakeTTSModel:
    """内层模型：只为 CUDA 的 optimize()/warm-up 而存在。"""

    def __init__(self):
        self.optimize_calls = 0
        self.warmup_calls = []

    def optimize(self, disable: bool = False):
        self.optimize_calls += 1
        return self

    def generate(self, target_text=None, max_len=None, **kw):
        self.warmup_calls.append((target_text, max_len))
        time.sleep(HOLD_S)
        return np.zeros((1, 16), dtype=np.float32)


class FakeVoxCPM:
    """假 `voxcpm.core.VoxCPM`：把"并发/入参/随机性"三件事变成可观测记录。

    * `max_concurrent` 若 > 1，说明两条线程同时进了模型推理（US-C5 要修的正是这个）；
    * 产物取值来自 **numpy 全局 RNG** —— 于是 seed 是否真生效可直接断言。
    """

    from_pretrained_kwargs = None
    from_pretrained_error = None

    def __init__(self):
        self.calls = []
        self.tts_model = FakeTTSModel()
        self._guard = threading.Lock()
        self.concurrent = 0
        self.max_concurrent = 0

    # ---- 观测用的进入/退出 ----
    def _enter(self, op, kwargs):
        with self._guard:
            self.concurrent += 1
            self.max_concurrent = max(self.max_concurrent, self.concurrent)
            self.calls.append((op, kwargs))
        time.sleep(HOLD_S)                      # 制造可观测的重叠窗口

    def _exit(self):
        with self._guard:
            self.concurrent -= 1

    def _render(self):
        # 关键：随机性来自全局 RNG，所以"seed 是否被真的灌进去"直接决定产物可否复现
        return np.full(8, float(np.random.random()), dtype=np.float32)

    @classmethod
    def from_pretrained(cls, *args, **kwargs):
        cls.from_pretrained_kwargs = {"args": args, "kwargs": kwargs}
        if cls.from_pretrained_error:
            raise cls.from_pretrained_error
        return cls()

    def generate(self, text=None, reference_wav_path=None, **kw):
        try:
            self._enter("generate", {"text": text,
                                     "reference_wav_path": reference_wav_path,
                                     **kw})
            return self._render()
        finally:
            self._exit()

    def generate_streaming(self, text=None, reference_wav_path=None, **kw):
        self._enter("generate_streaming", {"text": text,
                                           "reference_wav_path": reference_wav_path,
                                           **kw})
        try:
            for _ in range(3):
                yield self._render()
                time.sleep(HOLD_S / 3)
        finally:
            self._exit()


@pytest.fixture
def fake_core(monkeypatch):
    """把 vendored `voxcpm.core` 换成假内核（真包会拉起 torch/transformers）。"""
    FakeVoxCPM.from_pretrained_kwargs = None
    FakeVoxCPM.from_pretrained_error = None
    mod = types.ModuleType("voxcpm.core")
    mod.VoxCPM = FakeVoxCPM
    pkg = types.ModuleType("voxcpm")
    pkg.core = mod
    monkeypatch.setitem(sys.modules, "voxcpm", pkg)
    monkeypatch.setitem(sys.modules, "voxcpm.core", mod)
    return FakeVoxCPM


@pytest.fixture
def warm_adapter(fake_core):
    """已"加载"的适配器（真加载链路走一遍，但内核是假的），并屏蔽 DB 音色恢复。"""
    a = VoxCPMAdapter(model_path=EXISTING_DIR, device="cpu")
    a._restore_voices = lambda: None
    a.load_model()
    assert isinstance(a._model, FakeVoxCPM)
    a._model.calls.clear()
    return a


# ---------------- US-C5: 推理互斥闸 ----------------

def test_c5_synthesize_is_serialized(warm_adapter):
    """两线程并发合成：**同一时刻只允许 1 条在模型里**（旧代码无锁 → 会测到 2）。"""
    fake = warm_adapter._model
    threads = [threading.Thread(target=warm_adapter.synthesize,
                                kwargs={"text": f"第{i}句"})
               for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert fake.max_concurrent == 1, (
        f"并发进入模型推理（max_concurrent={fake.max_concurrent}）—— 单实例串行假设被破")
    assert len(fake.calls) == 4


def test_c5_preview_and_task_do_not_overlap(warm_adapter):
    """复刻事故形态：试听线程(`to_thread`) × 后台任务线程(`run_in_executor`) 同时打。"""
    fake = warm_adapter._model
    b = threading.Barrier(2, timeout=5)

    def worker():
        b.wait()                                # 两路尽量同时起跑
        warm_adapter.synthesize(text="同一时刻的合成")

    ts = [threading.Thread(target=worker) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert fake.max_concurrent == 1
    assert len(fake.calls) == 2
    # 排队但都成功：不是"一个成功一个报错"，也不是串音
    assert all(op == "generate" for op, _ in fake.calls)


def test_c5_stream_shares_the_same_lock(warm_adapter):
    """流式与非流式抢同一把闸（否则锁等于没锁）。"""
    fake = warm_adapter._model
    b = threading.Barrier(2, timeout=5)

    def run_stream():
        b.wait()
        list(warm_adapter.synthesize_stream(text="流式一句"))

    def run_plain():
        b.wait()
        warm_adapter.synthesize(text="非流式一句")

    ts = [threading.Thread(target=run_stream), threading.Thread(target=run_plain)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert fake.max_concurrent == 1, "流式与非流式同时进了模型"
    assert {op for op, _ in fake.calls} == {"generate", "generate_streaming"}


def test_c5_lock_is_reentrant_for_lazy_load(warm_adapter):
    """未加载时 synthesize 会回调 load_model：同线程重入不得自锁（RLock 的理由）。"""
    a = VoxCPMAdapter(model_path=EXISTING_DIR, device="cpu")
    a._restore_voices = lambda: None
    with a._infer_lock:                         # 模拟"外层已持闸"
        a.synthesize(text="不应死锁")
    assert a._loaded is True


def test_c5_stream_keeps_voice_id(warm_adapter):
    """US-C5 顺手修的丢音色：流式必须吃到 voice_id → reference_wav_path + 设计前缀。

    旧实现签名收了 `voice_id` 却从不使用（且把 reference_wav_path 留在 kwargs 白名单里
    但从未传进来）→ 流式试听拿到的是默认音色。
    """
    fake = warm_adapter._model
    ref = str(EXISTING_DIR)                     # 只要求"路径存在"，不要求是音频
    vid = "cloned_voice_for_stream"
    warm_adapter._cloned_voices[vid] = (ref, "参考文本")
    warm_adapter._designed_voices["design_vid"] = "低沉男声"

    chunks = list(warm_adapter.synthesize_stream(text="正文", voice_id=vid))
    assert len(chunks) == 3
    op, kw = fake.calls[-1]
    assert op == "generate_streaming"
    assert kw["reference_wav_path"] == ref, "流式丢了克隆音色（US-C5）"
    assert kw["text"] == "正文", f"neutral 之外的前缀不该出现: {kw['text']!r}"

    fake.calls.clear()
    list(warm_adapter.synthesize_stream(text="正文", voice_id="design_vid",
                                        emotion="sad"))
    op, kw = fake.calls[-1]
    assert kw["text"].startswith("（低沉男声）"), f"设计音色前缀在流式下丢失: {kw['text']!r}"
    assert "（语气低沉" in kw["text"], "emotion 前缀在流式下丢失"

    # 非流式同一 voice_id 走同一口径 —— 两条路径必须给出同样的输入
    fake.calls.clear()
    warm_adapter.synthesize(text="正文", voice_id=vid)
    assert fake.calls[-1][1]["reference_wav_path"] == ref


def test_c5_stream_lock_released_when_consumer_abandons(warm_adapter):
    """消费者中途丢弃生成器：闸必须随 GeneratorExit 释放，否则后续请求全被饿死。"""
    gen = warm_adapter.synthesize_stream(text="只取一块")
    next(gen)
    gen.close()
    assert not warm_adapter._infer_lock._is_owned(), "流式提前终止后仍持闸（死锁风险）"
    # 还能继续正常服务
    out = warm_adapter.synthesize(text="下一条")
    assert out.shape == (8,)


# ---------------- US-C4（适配器侧）: seed 真生效 ----------------

def test_c4_same_seed_same_audio(warm_adapter):
    """同 seed 两次产物逐样本一致；异 seed 必须不同（假内核用全局 numpy RNG）。"""
    a = warm_adapter
    first = a.synthesize(text="固定文本", seed=1234)
    second = a.synthesize(text="固定文本", seed=1234)
    third = a.synthesize(text="固定文本", seed=1235)
    assert np.array_equal(first, second), "同 seed 产物不一致 —— seed 没有真的生效"
    assert not np.array_equal(first, third), "不同 seed 产物相同 —— seed 被吞了"
    assert np.array_equal(first, a.synthesize(text="固定文本", seed=1234))


def test_c4_seed_none_keeps_old_behaviour(warm_adapter):
    """seed 缺省 = 与升级前逐字同义（不播种、不报错、位置参数兼容）。"""
    sig = inspect.signature(VoxCPMAdapter.synthesize)
    params = list(sig.parameters)
    assert params[:6] == ["self", "text", "voice_id", "speed", "pitch", "emotion"]
    # PY-322 之后 seed 仍是**最后一个位置参数**（cancel_event 刻意做成 keyword-only），
    # 所以"位置调用不受影响"这条 US-C4 不变量原样成立。
    assert params[-2] == "seed" and sig.parameters["seed"].default is None
    cancel = sig.parameters["cancel_event"]
    assert cancel.kind is inspect.Parameter.KEYWORD_ONLY and cancel.default is None
    out = warm_adapter.synthesize("位置参数仍然可用", "vid", 1.0, 0.0, "neutral")
    assert out.shape == (8,)


def test_c4_unsupporting_engine_rejects_seed_explicitly():
    """不接受 seed 的适配器必须**显式拒绝**（照 gptsovits 对 pitch 的纪律），不得静默收下。"""
    from modules.tts_engine.engines.base_tts import BaseTTSEngine

    engine = VoxCPMAdapter.__new__(VoxCPMAdapter)      # 不跑 __init__，只测契约件
    engine.engine_name = "no_seed_engine"
    engine._reject_seed(None)                          # None 放行
    with pytest.raises(ValueError) as exc:
        engine._reject_seed(7)
    assert "不支持 seed" in str(exc.value) and "no_seed_engine" in str(exc.value)
    assert VoxCPMAdapter.supports_seed is True
    assert BaseTTSEngine.supports_seed is False


def test_c4_apply_global_seed_seeds_numpy_and_torch():
    from modules.tts_engine.engines.base_tts import BaseTTSEngine

    seeded = BaseTTSEngine.apply_global_seed(999)
    assert "numpy" in seeded
    a = float(np.random.random())
    BaseTTSEngine.apply_global_seed(999)
    assert float(np.random.random()) == a
    assert BaseTTSEngine.apply_global_seed(None) == []


# ---------------- US-C6: CPU 分档 + 三段计时 ----------------

def test_c6_cpu_skips_compile_and_warmup(fake_core):
    """CPU 档：不再向 from_pretrained 要 compile，也不跑 35~95s 的 warm-up。"""
    a = VoxCPMAdapter(model_path=EXISTING_DIR, device="cpu")
    a._restore_voices = lambda: None
    a.load_model()
    kwargs = fake_core.from_pretrained_kwargs["kwargs"]
    assert kwargs["optimize"] is False, "CPU 档仍请求 torch.compile (US-C6)"
    assert kwargs["load_denoiser"] is False
    assert a._model.tts_model.optimize_calls == 0
    assert a._model.tts_model.warmup_calls == []
    detail = a.load_detail
    assert detail["optimize"] is False
    assert detail["weights_s"] is not None and detail["total_s"] is not None
    assert detail["import_s"] is not None
    assert detail["warmup_s"] is None
    assert detail["message"].startswith("CPU 已跳过编译"), detail["message"]
    assert "纯加载" in detail["message"]
    assert detail["restore_voices_s"] is not None


def test_c6_cuda_keeps_compile_and_measures_warmup(fake_core):
    """CUDA 档：compile + warm-up 保留，且**分别计时**（原来这两段完全不可见）。"""
    a = VoxCPMAdapter(model_path=EXISTING_DIR, device="cuda")
    a._restore_voices = lambda: None
    a.load_model()
    assert a._model.tts_model.optimize_calls == 1
    assert a._model.tts_model.warmup_calls, "CUDA 档应做一次 warm-up generate"
    assert a._model.tts_model.warmup_calls[0][1] == 10     # 与 core.py 同实参
    detail = a.load_detail
    assert detail["optimize"] is True
    assert detail["compile_s"] is not None
    assert detail["warmup_s"] is not None and detail["warmup_s"] >= 0
    assert "torch.compile" in detail["message"]


def test_c6_load_failure_records_error_and_still_raises(fake_core):
    """PY-304 语义不变：加载失败照样上抛，但 detail 里留下原因与计时。"""
    fake_core.from_pretrained_error = RuntimeError("权重缺失（模拟）")
    a = VoxCPMAdapter(model_path=EXISTING_DIR, device="cpu")
    a._restore_voices = lambda: None
    with pytest.raises(RuntimeError, match="权重缺失"):
        a.load_model()
    assert a.is_loaded() is False
    assert "权重缺失" in a.load_detail["error"]
    assert a.load_detail["total_s"] is not None


def test_c6_missing_weights_dir_raises_before_any_import(fake_core):
    a = VoxCPMAdapter(model_path=str(EXISTING_DIR + "/__no_such_dir__"), device="cpu")
    a._restore_voices = lambda: None
    with pytest.raises(FileNotFoundError):
        a.load_model()
    assert fake_core.from_pretrained_kwargs is None
    assert "FileNotFoundError" in a.load_detail["error"]


# ---------------- US-C6 × US-C1: 计时确实进 snapshot.detail ----------------

class _DetailedEngine:
    """假引擎：load_model 时按 VoxCPM 的口径交出 load_detail。"""

    engine_name = "w1c_detail_engine"
    sample_rate = 48000

    def __init__(self):
        self.load_detail = {}
        self._loaded = False

    def load_model(self, model_path: str = None):
        time.sleep(0.05)
        self.load_detail = {"device": "cpu", "optimize": False,
                            "weights_s": 41.2, "warmup_s": None,
                            "message": "CPU 已跳过编译与 warm-up，纯加载 41.2s"}
        self._loaded = True

    def is_loaded(self):
        return self._loaded


def test_c6_load_detail_reaches_model_status_snapshot():
    """`/api/system/model_status` 的 detail 字段 = 适配器交出的 load_detail（US-C6 出口）。"""
    from modules.tts_engine.engine_registry import engine_registry

    eng = _DetailedEngine()
    engine_registry.register(eng.engine_name, lambda: eng)
    try:
        pre = mp.ModelPreloader()
        pre.start(eng.engine_name)
        assert pre.wait_terminal(20.0, eng.engine_name) is True
        snap = pre.snapshot(eng.engine_name)
        assert snap["state"] == "ready"
        assert snap["detail"]["weights_s"] == 41.2
        assert "CPU 已跳过编译" in snap["detail"]["message"]
        # 顶层旧键不受影响（前端消费者语义不破）
        for k in ("state", "engine", "load_seconds", "elapsed_seconds",
                  "error", "skipped_reason", "message"):
            assert k in snap
        assert isinstance(snap["load_seconds"], float)
    finally:
        pre.reset(eng.engine_name)
        engine_registry._engines.pop(eng.engine_name, None)
        engine_registry._loaded.pop(eng.engine_name, None)
        engine_registry._engine_factories.pop(eng.engine_name, None)
