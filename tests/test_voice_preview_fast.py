"""#95/#96 —— 试听速度档（edge-tts）+ 墙钟预算 + 单飞去重的单元验证。

只跑单元级：不加载真实模型、不连生产库、不打网络。
引擎一律用桩替身（`FakeEngine`），缓存目录用 `VOICE_PREVIEW_DIR` 指向 tmp_path。
这里断言的是"接线与判据"：默认档必须**只**碰 edgetts、预算必须把僵尸任务判死、
质量门必须把坏产物连同文件一起淘汰（PY-117：不许静默成功）。
"""
import json
import logging
import os
import sys
import threading
import time
import types

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from backend.services import voice_preview as vp        # noqa: E402


# ============================================================ 测试用信号
def _speech_like(sample_rate=24000, seconds=2.0, level=0.5, seed=3):
    """类语音（谐波 + 宽带噪声 + 幅度调制）：过双门、峰值由 level 控制。"""
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


def _near_silent(sample_rate=24000, seconds=1.5):
    """峰值 1e-3 的"死渲染"：必须被试听口径的地板门（0.02）拦下。"""
    return (np.random.default_rng(1).standard_normal(
        int(sample_rate * seconds)) * 1e-3).astype(np.float32)


# ============================================================ 桩引擎
class FakeEngine:
    """记录调用、可控耗时/异常的引擎桩。"""

    def __init__(self, audio=None, delay=0.0, error=None, sample_rate=24000,
                 supports_seed=False, supports_design=False):
        self.engine_name = "fake"
        self.sample_rate = sample_rate
        self.supports_seed = supports_seed
        self.supports_design = supports_design
        self._audio = audio if audio is not None else _speech_like()
        self._delay = delay
        self._error = error
        self.calls = []
        self.design_calls = []

    def synthesize(self, text=None, voice_id=None, speed=1.0, pitch=0.0,
                   emotion="neutral", seed=None):
        self.calls.append({"text": text, "voice_id": voice_id, "seed": seed})
        if self._delay:
            time.sleep(self._delay)
        if self._error:
            raise self._error
        return self._audio

    def design_voice(self, description, voice_id=None):
        self.design_calls.append((description, voice_id))
        return {"status": "ok"}


def _row(vid="v-1", name="标准女声-温柔", engine="voxcpm", gender="female",
         description="温柔知性的标准女声，适合大多数场景", **kw):
    return types.SimpleNamespace(id=vid, name=name, engine=engine, gender=gender,
                                 age_group=kw.get("age_group", "adult"),
                                 style_tags=kw.get("style_tags"),
                                 description=description, meta=kw.get("meta", {}),
                                 sample_audio_path=None, model_path=None)


@pytest.fixture
def cache_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("VOICE_PREVIEW_DIR", str(tmp_path / "previews_generated"))
    vp.reset_jobs()
    yield tmp_path / "previews_generated"
    vp.reset_jobs()


def _install(monkeypatch, engine, row, fast_engines=("edgetts",)):
    """把音色行/引擎解析换成桩，并记录被要过的引擎名。"""
    asked = []

    def fake_load(name):
        asked.append(name)
        if name in fast_engines:
            return engine
        raise AssertionError(f"默认试听路径不得触碰引擎 {name!r}（重引擎只进离线合成）")

    monkeypatch.setattr(vp, "_load_row", lambda vid: row)
    monkeypatch.setattr(vp, "_native_sample", lambda vid: None)
    monkeypatch.setattr(vp, "_load_engine", fake_load)
    return asked


def _meta_of(path):
    return json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))


# ============================================================ A2 短文本
class TestPreviewTextIsShort:
    def test_fixed_text_within_14_chars(self, cache_dir, monkeypatch):
        assert len(vp.preview_text_for(_row(name="×ÊÔ´¿âÃ°ÑÌ-³ÉÄêÄÐ-CV-501dea"))) \
            <= vp.MAX_PREVIEW_CHARS
        assert vp.PREVIEW_TEXT == vp.preview_text_for(_row(name="坏名字#*()"))

    def test_short_clean_name_is_used_but_trimmed(self, cache_dir):
        text = vp.preview_text_for(_row(name="少女音"))
        assert "少女音" in text
        assert len(text) <= vp.MAX_PREVIEW_CHARS

    def test_long_description_never_reaches_engine(self, cache_dir, monkeypatch):
        """40 分钟那条 job 的诱因之一：把长文本喂进扩散模型。这里钉死上限。"""
        eng = FakeEngine()
        _install(monkeypatch, eng, _row(description="很长" * 200))
        assert vp.resolve_preview("v-1", wait_s=5).get("ok")
        sent = eng.calls[0]["text"]
        assert len(sent) <= 14


# ============================================================ A1 速度档
class TestFastPathUsesEdgeTts:
    def test_default_path_only_touches_edgetts(self, cache_dir, monkeypatch):
        eng = FakeEngine()
        asked = _install(monkeypatch, eng, _row(engine="voxcpm"))
        out = vp.resolve_preview("v-1", wait_s=10)
        assert out["ok"] and out["origin"] == "generated_now"
        assert asked == ["edgetts"]                       # 一次都没要 voxcpm
        assert out["engine"] == "edgetts"
        meta = _meta_of(vp.cache_paths("v-1", "fast")[0])
        assert meta["engine"] == "edgetts"
        assert meta["requested_engine"] == "voxcpm"       # 如实标注：不是原引擎出的声
        assert meta["voice_binding"] == "proxy"

    def test_proxy_voice_selection_is_deterministic_and_gendered(self, cache_dir):
        a = vp.select_edge_voice(_row(gender="male", name="标准男声-磁性"))
        b = vp.select_edge_voice(_row(gender="male", name="标准男声-磁性"))
        assert a == b == ("zh-CN-YunjianNeural", "proxy")     # "磁性" 命中关键词
        f = vp.select_edge_voice(_row(gender="female", name="标准女声-温柔"))
        assert f == ("zh-CN-XiaoxiaoNeural", "proxy")          # 兜底按性别

    def test_native_edge_row_keeps_its_own_voice(self, cache_dir):
        voice, binding = vp.select_edge_voice(
            _row(engine="edgetts", meta={"edge_voice": "zh-CN-YunjianNeural"}))
        assert (voice, binding) == ("zh-CN-YunjianNeural", "native")

    def test_second_click_hits_cache_without_resynth(self, cache_dir, monkeypatch):
        eng = FakeEngine()
        _install(monkeypatch, eng, _row())
        t0 = time.perf_counter()
        assert vp.resolve_preview("v-1", wait_s=10)["ok"]
        first = time.perf_counter() - t0
        t0 = time.perf_counter()
        out = vp.resolve_preview("v-1", wait_s=10)
        second = time.perf_counter() - t0
        assert out["origin"] == "generated_cache"
        assert len(eng.calls) == 1                        # 只合成了一次
        assert second < 1.0 and first < 15.0              # ≤1s 命中 / 首包预算内

    def test_unknown_voice_is_404_not_silence(self, cache_dir, monkeypatch):
        monkeypatch.setattr(vp, "_load_row", lambda vid: None)
        out = vp.resolve_preview("nope", wait_s=5)
        assert not out["ok"] and out["code"] == "voice_not_found" and out["status"] == 404

    def test_illegal_voice_id_rejected(self, cache_dir):
        out = vp.resolve_preview("../../etc/passwd", wait_s=1)
        assert not out["ok"] and out["code"] == "voice_not_found"


# ============================================================ A4 双质量门
class TestPreviewQualityGates:
    def test_near_silent_product_fails_and_leaves_no_file(self, cache_dir, monkeypatch):
        eng = FakeEngine(audio=_near_silent())
        _install(monkeypatch, eng, _row())
        out = vp.resolve_preview("v-quiet", wait_s=10)
        assert not out["ok"] and out["code"] == "quality_gate_failed"
        assert out["status"] == 502
        wav, _ = vp.cache_paths("v-quiet", "fast")
        assert not wav.exists()                           # 不许"有文件就算成功"

    def test_low_peak_is_boosted_to_target(self, cache_dir, monkeypatch):
        import soundfile as sf
        eng = FakeEngine(audio=_speech_like(level=0.1))
        _install(monkeypatch, eng, _row())
        assert vp.resolve_preview("v-boost", wait_s=10)["ok"]
        wav, _ = vp.cache_paths("v-boost", "fast")
        assert wav.read_bytes()[:4] == b"RIFF"
        data, sr = sf.read(str(wav))
        peak = float(np.max(np.abs(data)))
        assert 0.6 <= peak <= 0.95                        # 0.1 -> 抬到 0.7 附近
        assert _meta_of(wav)["boosted"] is True

    def test_empty_array_is_a_failure_not_an_empty_wav(self, cache_dir, monkeypatch):
        eng = FakeEngine(audio=np.zeros(0, dtype=np.float32))
        _install(monkeypatch, eng, _row())
        out = vp.resolve_preview("v-empty", wait_s=10)
        assert not out["ok"] and out["code"] == "synthesis_failed"

    def test_too_short_product_rejected(self, cache_dir, monkeypatch):
        eng = FakeEngine(audio=_speech_like(seconds=0.05))
        _install(monkeypatch, eng, _row())
        out = vp.resolve_preview("v-short", wait_s=10)
        assert not out["ok"] and out["code"] == "quality_gate_failed"

    def test_corrupt_cache_is_evicted_and_regenerated(self, cache_dir, monkeypatch):
        wav, meta = vp.cache_paths("v-corrupt", "fast")
        wav.parent.mkdir(parents=True, exist_ok=True)
        wav.write_bytes(b"not even close to RIFF" * 100)  # 伪装成"有缓存"
        meta.write_text("{}", encoding="utf-8")
        eng = FakeEngine()
        _install(monkeypatch, eng, _row())
        out = vp.resolve_preview("v-corrupt", wait_s=10)
        assert out["ok"] and out["origin"] == "generated_now"
        assert wav.read_bytes()[:4] == b"RIFF"
        assert len(eng.calls) == 1

    def test_engine_exception_is_distinguishable(self, cache_dir, monkeypatch):
        eng = FakeEngine(error=RuntimeError("edge-tts 未返回任何音频字节"))
        _install(monkeypatch, eng, _row())
        out = vp.resolve_preview("v-boom", wait_s=10)
        assert not out["ok"] and out["code"] == "synthesis_failed"
        assert out["status"] == 502
        assert "未返回任何音频字节" in out["message"]      # 真实原因上抛，不吞
        assert not vp.cache_paths("v-boom", "fast")[0].exists()

    def test_missing_speed_engine_reports_engine_unavailable(self, cache_dir, monkeypatch):
        def boom(name):
            raise RuntimeError("edge-tts 库未安装")
        monkeypatch.setattr(vp, "_load_row", lambda vid: _row())
        monkeypatch.setattr(vp, "_native_sample", lambda vid: None)
        monkeypatch.setattr(vp, "_load_engine", boom)
        out = vp.resolve_preview("v-noedge", wait_s=10)
        assert not out["ok"] and out["code"] == "engine_unavailable"
        assert out["status"] == 503


# ============================================================ B5 单飞去重
class TestSingleFlight:
    def test_five_concurrent_requests_run_one_job(self, cache_dir, monkeypatch):
        gate = threading.Event()

        class Blocking(FakeEngine):
            def synthesize(self, **kw):
                self.calls.append(kw)
                gate.wait(timeout=10)
                return self._audio

        eng = Blocking()
        _install(monkeypatch, eng, _row())
        results = []
        threads = [threading.Thread(target=lambda: results.append(
            vp.resolve_preview("v-race", wait_s=15))) for _ in range(5)]
        for t in threads:
            t.start()
        deadline = time.time() + 2.0
        while time.time() < deadline:
            if eng.calls:
                break
        job = vp._jobs.get("v-race::fast")
        gate.set()
        for t in threads:
            t.join(timeout=20)
        # 单飞的取证走行为面，不扫日志文本：loguru 经 root 转发，caplog 能不能收到
        # INFO 取决于同会话前一个测试把 root level 留在了哪里（单跑绿/全量红的成因）。
        assert job is not None, "五路并发没有落到同一个 job 键上"
        assert len(eng.calls) == 1                        # 引擎只被叫了一次
        assert len(results) == 5
        assert all(r.get("ok") for r in results)
        assert len(vp.running_jobs()) == 0

    def test_different_voices_do_not_share_a_job(self, cache_dir, monkeypatch):
        rows = {"v-a": _row(vid="v-a"), "v-b": _row(vid="v-b")}
        eng = FakeEngine()
        monkeypatch.setattr(vp, "_load_row", lambda vid: rows[vid])
        monkeypatch.setattr(vp, "_native_sample", lambda vid: None)
        monkeypatch.setattr(vp, "_load_engine", lambda name: eng)
        assert vp.resolve_preview("v-a", wait_s=10)["ok"]
        assert vp.resolve_preview("v-b", wait_s=10)["ok"]
        assert len(eng.calls) == 2                        # 各自一条产物，不互相顶替
        assert vp.cache_paths("v-a", "fast")[0].exists()
        assert vp.cache_paths("v-b", "fast")[0].exists()

    def test_concurrency_gate_reports_engine_busy(self, cache_dir, monkeypatch):
        held = threading.Semaphore(1)
        assert held.acquire(blocking=False)
        monkeypatch.setattr(vp, "_FAST_SEM", held)
        monkeypatch.setattr(vp, "FAST_BUDGET_S", 2.0)
        eng = FakeEngine()
        _install(monkeypatch, eng, _row())
        out = vp.resolve_preview("v-busy", wait_s=10)
        assert not out["ok"] and out["code"] == "engine_busy"
        assert out["status"] == 429


# ============================================================ B6 墙钟预算
class TestBudgetTurnsZombiesIntoFailures:
    def test_over_budget_job_is_marked_timeout_and_writes_meta(self, cache_dir,
                                                               monkeypatch):
        monkeypatch.setenv("VOICE_PREVIEW_FAST_RETRY_COOLDOWN_S", "600")
        vp.FAST_RETRY_COOLDOWN_S = 600.0                  # 冷却期长一点，便于断言
        monkeypatch.setattr(vp, "FAST_BUDGET_S", 0.6)
        eng = FakeEngine(audio=_speech_like(), delay=2.5)
        _install(monkeypatch, eng, _row())
        out = vp.resolve_preview("v-slow", wait_s=15)
        assert not out["ok"] and out["code"] == "timeout"
        assert out["status"] == 504 and out["budget_s"] == 0.6
        wav, meta = vp.cache_paths("v-slow", "fast")
        assert not wav.exists()
        assert meta.exists()
        recorded = json.loads(meta.read_text(encoding="utf-8"))
        assert recorded["status"] == "failure" and recorded["code"] == "timeout"

        # PY-322：此刻执行线程还在烧（FakeEngine 要 2.5s 才退），该键处于 poisoned 态
        # —— 后续同类请求拿到**可区分**的 engine_busy_cancelling，而不是排到它身上
        again = vp.resolve_preview("v-slow", wait_s=2)
        assert again["code"] == "engine_busy_cancelling" and again["status"] == 429

        # 迟到线程不得把产物发布出去（"失败后突然又成功"= 自相矛盾）
        time.sleep(3.0)
        assert not wav.exists()
        assert len(eng.calls) == 1                        # 冷却期内也没有重跑

        # 执行者确认退出后，回到 B6 的冷却复用口径：同一个可区分 timeout
        after = vp.resolve_preview("v-slow", wait_s=2)
        assert after["code"] == "timeout" and after["status"] == 504

    def test_wait_zero_does_not_block_but_reports_in_progress(self, cache_dir,
                                                              monkeypatch):
        gate = threading.Event()
        eng = FakeEngine()

        def slow(**kw):
            eng.calls.append(kw)
            gate.wait(timeout=10)
            return eng._audio
        eng.synthesize = slow
        _install(monkeypatch, eng, _row())
        out = vp.resolve_preview("v-nowait", wait_s=0)
        assert not out["ok"] and out["code"] == "in_progress"
        assert out["status"] == 503 and out["retry_after"]
        gate.set()
        time.sleep(0.6)
        assert vp.resolve_preview("v-nowait", wait_s=5)["ok"]

    def test_force_regenerate_bypasses_cache(self, cache_dir, monkeypatch):
        eng = FakeEngine()
        _install(monkeypatch, eng, _row())
        assert vp.resolve_preview("v-force", wait_s=10)["ok"]
        assert vp.resolve_preview("v-force", wait_s=10, force=True)["ok"]
        assert len(eng.calls) == 2


# ============================================================ B7 高档出口
class TestHighQualityEscapeHatch:
    def test_high_uses_bound_engine_and_is_labelled(self, cache_dir, monkeypatch):
        eng = FakeEngine(audio=_speech_like(sample_rate=48000), sample_rate=48000)
        monkeypatch.setattr(vp, "_load_row", lambda vid: _row(engine="voxcpm"))
        monkeypatch.setattr(vp, "_native_sample", lambda vid: None)
        monkeypatch.setattr(vp, "_load_engine", lambda name: eng)
        out = vp.resolve_preview("v-high", wait_s=10, quality="high")
        assert out["ok"] and out["quality"] == "high"
        assert out["origin"] == "generated_now"
        wav, _ = vp.cache_paths("v-high", "high")
        assert wav.exists() and wav.read_bytes()[:4] == b"RIFF"
        # 高档不污染默认缓存键
        assert not vp.cache_paths("v-high", "fast")[0].exists()

    def test_high_path_also_has_a_budget(self, cache_dir, monkeypatch):
        monkeypatch.setattr(vp, "HIGH_BUDGET_S", 0.5)
        monkeypatch.setattr(vp, "HIGH_RETRY_COOLDOWN_S", 600.0)
        eng = FakeEngine(delay=3.0)
        monkeypatch.setattr(vp, "_load_row", lambda vid: _row(engine="voxcpm"))
        monkeypatch.setattr(vp, "_native_sample", lambda vid: None)
        monkeypatch.setattr(vp, "_load_engine", lambda name: eng)
        out = vp.resolve_preview("v-highslow", wait_s=15, quality="high")
        assert not out["ok"] and out["code"] == "timeout"
        assert out["budget_s"] == 0.5
        assert not vp.cache_paths("v-highslow", "high")[0].exists()


# ============================================================ 错误码表
class TestErrorCodeTable:
    @pytest.mark.parametrize("code,status", [
        ("engine_unavailable", 503), ("synthesis_failed", 502),
        ("timeout", 504), ("quality_gate_failed", 502),
        ("engine_busy", 429), ("write_failed", 500),
    ])
    def test_codes_map_to_distinct_statuses(self, code, status):
        assert vp.ERROR_STATUS[code] == status
        assert vp._error(code, "x")["status"] == status

    def test_no_code_yields_a_bare_success_shaped_dict(self):
        err = vp._error("timeout", "预算到点")
        assert err["ok"] is False and err["code"] == "timeout"
        assert "message" in err and err["status"]
