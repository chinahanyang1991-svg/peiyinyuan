"""PY-322 —— 试听预算到点必须**终止执行者**（含 poisoned 态）。

缺陷原形（logs/backend-restart-151002.err.log 实测）：
  16:07:19 `seq=1` 起合成 → 16:10:19 `WARNING … 超过预算 180.0s -> 标记 timeout 失败
  （reason=budget）` → **然后 voxcpm 进度条照旧从 13/454 涨到 44/454**
  （23.55s/it，ETA 2:40:56）。HTTP 侧当场 504，工作线程却把整条推理跑完，
  而 `voxcpm_adapter.synthesize` 全程持 `_infer_lock` 且没有取消点 ——
  一枚用户已放弃的请求把唯一的重引擎锁死 2.5 小时，期间所有 voxcpm 档吃 429。

本文件用**注入的慢引擎**证明三件事（真 voxcpm 一句要 44 分钟，不能用来做断言）：
  (a) 预算到点 job 被判死（`timeout`/504，且没有产物落盘）；
  (b) 执行线程在**有界时间**内真的退出：`worker_exited` 置位、线程不再 alive、
      引擎串行锁真的还回来了（用 `acquire(blocking=False)` 取证，不靠 sleep 猜）；
  (c) poisoned 期间第二个请求拿到可区分的 `engine_busy_cancelling`，
      **且引擎没有被叫第二次**（没排队 = 没有第二枚任务焊在同一条锁上）。

另有一条单元级用例直接跑 `VoxCPMAdapter`（`_model` 用桩，不 import torch）：
证明取消点落在 voxcpm **自己的音频块循环边界**上，且上游生成器被 `close()`。

变异口径：把 `_abandon()` 里的 `job.cancel_event.set()` 注释掉，
本文件的 (b) 必红（线程永不提前退出）；把 `_finish` 的 `worker_exited` 登记删掉，
(c) 的解封断言必红 —— 原始形态"只标失败不杀线程"在这里无处藏身。
"""
import json
import os
import sys
import threading
import time
import types

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from backend.services import voice_preview as vp        # noqa: E402


# ============================================================ 声学夹具
def _speech_like(sample_rate=24000, seconds=0.5, level=0.5, seed=3):
    """类语音小块（过双门 + 试听口径），用来拼"跑了很多块"的证据。"""
    rng = np.random.default_rng(seed)
    n = int(sample_rate * seconds)
    t = np.arange(n, dtype=np.float64) / sample_rate
    f0 = 130.0 + 25.0 * np.sin(2 * np.pi * 1.9 * t)
    x = np.zeros(n)
    for k in range(1, 18):
        x += (1.0 / k) * (0.6 + 0.4 * rng.random()) * np.sin(2 * np.pi * f0 * k * t)
    x += 0.4 * rng.standard_normal(n)
    env = np.clip(0.55 + 0.45 * np.sin(2 * np.pi * 3.3 * t + 0.6), 0.05, 1.0)
    x = x * env
    x /= max(float(np.max(np.abs(x))), 1e-9)
    return (x * level).astype(np.float32)


def _row(vid="v-322", name="试听预算测试", engine="voxcpm", gender="female",
         description="温柔知性的标准女声"):
    return types.SimpleNamespace(id=vid, name=name, engine=engine, gender=gender,
                                 age_group="adult", style_tags=None,
                                 description=description, meta={},
                                 sample_audio_path=None, model_path=None)


@pytest.fixture
def cache_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("VOICE_PREVIEW_DIR", str(tmp_path / "previews_generated"))
    # 并发闸也要换成全新的：别的测试文件可能留下一个还在烧的任务占着
    # `_HIGH_SEM`（默认只有 1 个槽位），那会让本文件的夹具根本进不了引擎。
    monkeypatch.setattr(vp, "_FAST_SEM", threading.BoundedSemaphore(4))
    monkeypatch.setattr(vp, "_HIGH_SEM", threading.BoundedSemaphore(1))
    vp.reset_jobs()
    yield tmp_path / "previews_generated"
    vp.reset_jobs()


# ============================================== 注入的慢引擎（voxcpm 同形状）
class SlowEngine:
    """一把重型串行锁 + 逐块生成 + 块边界查 cancel_event —— 与 voxcpm 适配器同形状。

    `chunks * chunk_s` 刻意远大于预算：这就是"预算只判死等待方、执行者照烧"
    能烧多久的那个放大系数。`exit_gate` 用来把"收到取消"与"真的退出"分开，
    好在 poisoned 窗口里做断言。
    """

    engine_name = "fakeslow"
    sample_rate = 24000
    supports_seed = False
    supports_design = False

    def __init__(self, chunk_s=0.05, chunks=200, cancel_aware=True,
                 exit_gate=None):
        self.serial_lock = threading.RLock()          # 模拟 _infer_lock
        self.chunk_s = chunk_s
        self.chunks = chunks
        self.cancel_aware = cancel_aware
        self.exit_gate = exit_gate
        self.calls = []                               # 每次 synthesize 一条
        self.blocks = 0                               # 产出过多少块
        self.lock_held = threading.Event()
        self.cancel_seen_at = None
        self.exited_at = None
        self.completed = False                        # 跑完了全部块？

    # 只声明该形参才算"支持取消"（引擎侧口径由签名决定）
    def synthesize(self, text=None, voice_id=None, speed=1.0, pitch=0.0,
                   emotion="neutral", seed=None, cancel_event=None):
        self.calls.append({"text": text, "voice_id": voice_id,
                           "cancel_event": cancel_event})
        if not self.cancel_aware:
            # 旧世界：没有取消点，一把 sleep 烧到底（锁全程不放手）
            with self.serial_lock:
                self.lock_held.set()
                try:
                    time.sleep(self.chunk_s * self.chunks)
                    self.completed = True
                    return _speech_like(seconds=2.0)
                finally:
                    self.exited_at = time.time()
                    self.lock_held.clear()
        with self.serial_lock:                        # 全程持锁（US-C5 同构）
            self.lock_held.set()
            try:
                parts = []
                for i in range(self.chunks):
                    if cancel_event is not None and cancel_event.is_set():
                        self.cancel_seen_at = time.time()
                        if self.exit_gate is not None:
                            self.exit_gate.wait(timeout=10)   # 还在"退出路上"
                        raise RuntimeError("fakeslow: 块边界收到取消信号")
                    time.sleep(self.chunk_s)
                    self.blocks += 1
                    parts.append(_speech_like(seed=i))
                self.completed = True
                return np.concatenate(parts)
            finally:
                self.exited_at = time.time()
                self.lock_held.clear()

    def design_voice(self, description, voice_id=None):
        return {"status": "ok"}


class LegacyEngine:
    """**签名里不收 cancel_event** 的老适配器（今天的 edgetts / gptsovits 档）：
    试听侧无从递 token，预算到点只能"判死 + 产物作废"，执行者必然跑完。
    """

    engine_name = "fakelegacy"
    sample_rate = 24000
    supports_seed = False
    supports_design = False

    def __init__(self, burn_s=2.0):
        self.burn_s = burn_s
        self.serial_lock = threading.RLock()
        self.lock_held = threading.Event()
        self.calls = []
        self.completed = False

    def synthesize(self, text=None, voice_id=None, speed=1.0, pitch=0.0,
                   emotion="neutral", seed=None):
        self.calls.append({"text": text})
        with self.serial_lock:
            self.lock_held.set()
            try:
                time.sleep(self.burn_s)
                self.completed = True
                return _speech_like(seconds=2.0)
            finally:
                self.lock_held.clear()


def _install(monkeypatch, engine, row):
    monkeypatch.setattr(vp, "_load_row", lambda vid: row)
    monkeypatch.setattr(vp, "_native_sample", lambda vid: None)
    monkeypatch.setattr(vp, "_load_engine", lambda name: engine)


def _job_of(key):
    with vp._jobs_lock:
        return vp._jobs.get(key)


# ========================================== (a)+(b) 判死之后执行者必须真退出
class TestBudgetCancelsTheExecutor:
    def test_over_budget_thread_exits_and_returns_the_serial_lock(self, cache_dir,
                                                                 monkeypatch):
        budget = 0.8
        monkeypatch.setattr(vp, "HIGH_BUDGET_S", budget)
        monkeypatch.setattr(vp, "HIGH_RETRY_COOLDOWN_S", 600.0)
        eng = SlowEngine(chunk_s=0.05, chunks=200)    # 不取消要烧 10s
        _install(monkeypatch, eng, _row())

        out = vp.resolve_preview("v-322", wait_s=6.0, quality="high")
        key = "v-322::high"
        job = _job_of(key)
        assert job is not None, "判死后任务应仍在表里（冷却复用）"

        # (a) 对外：可区分的 timeout，且没有半成品落盘
        assert out["ok"] is False and out["code"] == "timeout"
        assert out["status"] == 504 and out["cancelling"] is True
        wav, meta = vp.cache_paths("v-322", "high")
        assert not wav.exists(), "PY-117：取消后不得有可用缓存落盘"
        assert meta.exists()
        recorded = json.loads(meta.read_text(encoding="utf-8"))
        assert recorded["code"] == "timeout" and recorded["cancel_signalled"] is True

        # (b) 执行者：**有界时间内**真的停了（不是等它自己跑完 10s）
        assert job.worker_exited.wait(timeout=2.0), "线程未在预算后 2s 内退出 = 还在烧锁"
        # worker_exited 是执行线程自己的 finally 置位的，那一刻 Thread 对象可能还在退栈；
        # 满载机器上出现过"已确认退出+锁已释放"而 is_alive() 仍 True 的窗口，故有界轮询。
        _deadline = time.time() + 2.0
        while job.thread.is_alive() and time.time() < _deadline:
            time.sleep(0.02)
        assert not job.thread.is_alive()
        assert eng.cancel_seen_at is not None, "引擎从未观察到取消信号"
        assert eng.blocks < 20, f"跑到了第 {eng.blocks} 块，说明没在块边界尽早退出"
        assert eng.completed is False
        # 锁还回来的直接取证：非阻塞拿到 == 执行者确实松手了
        assert eng.serial_lock.acquire(blocking=False), "重引擎串行锁未释放"
        eng.serial_lock.release()
        # 判死 -> 退出的实测延迟（这才是 PY-322 要的那个数字）
        latency = eng.exited_at - job.poisoned_at
        assert latency < 2.0, f"判死到退出耗时 {latency:.2f}s，不是有界退出"
        # poisoned 已随线程退出解封
        assert key not in vp.poisoned_jobs()
        assert len(eng.calls) == 1

    def test_engine_that_ignores_cancellation_still_poisons_until_it_finishes(
            self, cache_dir, monkeypatch):
        """无法取消的路径（签名不收 cancel_event 的老适配器）：判死 + poisoned 挡人。"""
        monkeypatch.setattr(vp, "FAST_BUDGET_S", 0.5)
        monkeypatch.setattr(vp, "FAST_RETRY_COOLDOWN_S", 600.0)
        eng = LegacyEngine(burn_s=2.0)
        _install(monkeypatch, eng, _row(engine="edgetts"))
        out = vp.resolve_preview("v-nc", wait_s=5.0)
        assert out["code"] == "timeout"
        # 线程仍在烧（无可取消的检查点）：poisoned 必须挡住第二发
        again = vp.resolve_preview("v-nc", wait_s=0)
        assert again["code"] == "engine_busy_cancelling"
        assert again["engine_cancellable"] is False
        assert len(eng.calls) == 1
        assert "v-nc::fast" in vp.poisoned_jobs()
        # 它自己跑完（≈2s）后必须自动解封，不能永久锁死
        deadline = time.time() + 6.0
        while time.time() < deadline and "v-nc::fast" in vp.poisoned_jobs():
            time.sleep(0.05)
        assert eng.completed is True, "该路径本就没有取消点：跑完才对（如实记录）"
        assert "v-nc::fast" not in vp.poisoned_jobs()


# ================================================== (c) poisoned 语义
class TestPoisonedKeyRefusesToQueue:
    def test_second_request_gets_distinguishable_code_not_a_queue(self, cache_dir,
                                                                  monkeypatch):
        monkeypatch.setattr(vp, "HIGH_BUDGET_S", 0.6)
        monkeypatch.setattr(vp, "HIGH_RETRY_COOLDOWN_S", 600.0)
        exit_gate = threading.Event()
        eng = SlowEngine(chunk_s=0.04, chunks=200, exit_gate=exit_gate)
        _install(monkeypatch, eng, _row())

        out = vp.resolve_preview("v-p", wait_s=5.0, quality="high")
        assert out["code"] == "timeout"
        job = _job_of("v-p::high")
        # 引擎已收到取消、但**还卡在退出路上**（锁仍未释放）—— 这就是 poisoned 窗口
        deadline = time.time() + 2.0
        while time.time() < deadline and eng.cancel_seen_at is None:
            time.sleep(0.02)
        assert eng.cancel_seen_at is not None
        assert not job.worker_exited.is_set()
        assert eng.lock_held.is_set(), "夹具没锁住，测试失去意义"

        forced = vp.resolve_preview("v-p", wait_s=5.0, force=True, quality="high")
        assert forced["ok"] is False
        assert forced["code"] == "engine_busy_cancelling"     # 可区分，不是 in_progress
        assert forced["status"] == 429
        assert forced["seq"] == job.seq
        assert forced["engine_cancellable"] is True
        assert "retry_after" in forced and forced["retry_after"] >= 3
        # 关键：没有第二枚任务被排到仍在烧的引擎上
        assert len(eng.calls) == 1
        assert "v-p::high" in vp.poisoned_jobs()

        # 执行者真正确认退出 -> 自动解封，才允许重开
        exit_gate.set()
        assert job.worker_exited.wait(timeout=2.0)
        deadline = time.time() + 2.0
        while time.time() < deadline and "v-p::high" in vp.poisoned_jobs():
            time.sleep(0.02)
        assert "v-p::high" not in vp.poisoned_jobs()
        assert eng.serial_lock.acquire(blocking=False)
        eng.serial_lock.release()
        # 高档冷却期内、非 regenerate：仍复用 timeout（#96 PREVIEW-GATE 口径不变）
        cooled = vp.resolve_preview("v-p", wait_s=0.5, quality="high")
        assert cooled["code"] == "timeout"
        assert len(eng.calls) == 1

    def test_poison_error_shape_is_structured(self, cache_dir, monkeypatch):
        monkeypatch.setattr(vp, "HIGH_BUDGET_S", 0.4)
        eng = SlowEngine(chunk_s=0.05, chunks=100)
        _install(monkeypatch, eng, _row())
        out = vp.resolve_preview("v-shape", wait_s=3.0, quality="high")
        assert out["code"] == "timeout"
        err = vp._poison_error(_job_of("v-shape::high"))
        assert set(("code", "message", "status", "retry_after", "next")) <= set(err)
        assert err["code"] == "engine_busy_cancelling"
        assert err["status"] == 429 and err["seq"] and err["budget_s"] == 0.4
        assert len(err["message"]) > 20
        assert vp.ERROR_STATUS["engine_busy_cancelling"] == 429


# ============================== 引擎侧：voxcpm 的真实取消点（不 import torch）
class TestVoxcpmAdapterCancelsAtChunkBoundary:
    def _adapter(self, stub):
        from modules.tts_engine.engines.voxcpm_adapter import VoxCPMAdapter
        ad = VoxCPMAdapter.__new__(VoxCPMAdapter)     # 既有 P0 测试桩的用法
        ad._loaded = True
        ad._model = stub
        ad.device = "cpu"
        ad._cloned_voices = {}
        ad._designed_voices = {}
        ad.model_path = "unused"
        return ad

    def test_cancel_event_stops_chunk_loop_and_releases_lock(self):
        from modules.tts_engine.engines.voxcpm_adapter import VoxCPMCancelled

        class StubVox:
            def __init__(self, chunks=500, chunk_s=0.01):
                self.chunks, self.chunk_s = chunks, chunk_s
                self.pulled = 0
                self.closed = threading.Event()
                self.finished = False

            def generate_streaming(self, **kw):
                try:
                    for _ in range(self.chunks):
                        time.sleep(self.chunk_s)
                        self.pulled += 1
                        yield np.zeros(2400, dtype=np.float32)
                    self.finished = True
                finally:
                    self.closed.set()             # 上游 finally（关内层/VAE）

        stub = StubVox()
        ad = self._adapter(stub)
        ev = threading.Event()
        holder = {}
        # 取消信号由看门狗在"预算到点"时发；这里等第 3 块之后再发
        watcher = threading.Timer(0.08, ev.set)
        watcher.daemon = True

        def run():
            try:
                ad.synthesize(text="试听", voice_id=None, cancel_event=ev)
            except BaseException as e:                # noqa: BLE001
                holder["err"] = e
            holder["alive"] = True

        watcher.start()
        t = threading.Thread(target=run)
        t.start()
        t.join(timeout=5)
        watcher.cancel()

        assert isinstance(holder.get("err"), VoxCPMCancelled), \
            f"应带 VoxCPMCancelled 退出，实为 {holder.get('err')!r}"
        assert stub.pulled < 30, f"取消后仍在拉块：{stub.pulled}/{stub.chunks}"
        assert stub.finished is False
        assert stub.closed.is_set(), "上游生成器未被 close（半截流没收尾）"
        assert ad._infer_lock.acquire(blocking=False), "串行锁未释放 -> 仍会锁死重引擎"
        ad._infer_lock.release()

    def test_uncancelled_call_path_is_untouched(self):
        """不传 cancel_event（整书正式合成）时不得改走分块出口。"""
        class StubVox:
            def __init__(self):
                self.stream_calls = 0
                self.generate_calls = 0

            def generate(self, **kw):
                self.generate_calls += 1
                return np.ones(4800, dtype=np.float32)

            def generate_streaming(self, **kw):
                self.stream_calls += 1
                yield np.ones(4800, dtype=np.float32)

        stub = StubVox()
        ad = self._adapter(stub)
        out = ad.synthesize(text="正文一整句")
        assert stub.generate_calls == 1 and stub.stream_calls == 0
        assert np.asarray(out).size == 4800

    def test_cancel_before_lock_does_not_enter_the_model(self):
        from modules.tts_engine.engines.voxcpm_adapter import (VoxCPMCancelled,
                                                              VoxCPMAdapter)
        class Never:
            def generate(self, **kw):                   # pragma: no cover
                raise AssertionError("已判死的任务不得再进模型")

        ad = self._adapter(Never())
        ev = threading.Event()
        ev.set()
        with pytest.raises(VoxCPMCancelled):
            VoxCPMAdapter.synthesize(ad, text="x", cancel_event=ev)
