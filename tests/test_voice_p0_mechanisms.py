"""
VoiceStudio P0 机制移植的单元验证（P0-1 条件单点解析 / P0-2 克隆 prompt 两级缓存 /
P0-4 试听双门）

这些测试只跑单元级：不加载模型、不连数据库、不打 HTTP。
引擎/模型一律用桩（stub）替身 —— 本项目 CPU 环境下真实权重不参与，
所以这里验证的是"接线与判据"，不是"音质"。
"""
import json
import os
import sys
import threading
import types

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from modules.tts_engine.clone_prompt_cache import ClonePromptCache
from modules.voice_manager import audio_quality as aq
from modules.voice_manager.conditioning import resolve_profile_conditioning


# ============================================================ 测试用信号
def _hum(sample_rate=16000, seconds=2.0, level=0.5):
    """峰值健康但频谱塌成几条线的"嗡嗡声"（退化音，应被门 2 判失败）。"""
    n = int(sample_rate * seconds)
    t = np.arange(n, dtype=np.float64) / sample_rate
    x = np.zeros(n)
    for k, amp in enumerate([1.0, 0.5, 0.25], start=1):
        x += amp * np.sin(2 * np.pi * 100.0 * k * t)
    x /= np.max(np.abs(x))
    return (x * level).astype(np.float32)


def _speech_like(sample_rate=16000, seconds=2.0, level=0.5, seed=7):
    """类语音：谐波簇 + 宽带噪声 + 幅度调制 + 词间空隙（应通过双门）。"""
    rng = np.random.default_rng(seed)
    n = int(sample_rate * seconds)
    t = np.arange(n, dtype=np.float64) / sample_rate
    f0 = 120.0 + 30.0 * np.sin(2 * np.pi * 1.7 * t)
    x = np.zeros(n)
    for k in range(1, 20):
        x += (1.0 / k) * (0.6 + 0.4 * rng.random()) * np.sin(2 * np.pi * f0 * k * t)
    x += 0.35 * rng.standard_normal(n)                      # 呼吸/擦音成分
    env = 0.55 + 0.45 * np.sin(2 * np.pi * 3.1 * t + 0.7)
    env = np.clip(env, 0.05, 1.0)
    for _ in range(4):                                      # 词间空隙
        s = int(rng.integers(0, n - 1200))
        env[s:s + 1200] *= 0.02
    x = x * env
    x /= max(np.max(np.abs(x)), 1e-9)
    return (x * level).astype(np.float32)


# ============================================================ P0-2 两级缓存
class TestClonePromptCacheKey:
    """"参考音频被替换后永不命中旧编码"—— 键必须含 mtime 与 size。"""

    def _ref(self, tmp_path, content=b"a" * 1024):
        p = tmp_path / "ref.wav"
        p.write_bytes(content)
        return str(p)

    def test_key_stable_for_same_file(self, tmp_path):
        cache = ClonePromptCache(disk_dir=tmp_path / "c")
        ref = self._ref(tmp_path)
        assert cache.build_key("voxcpm|v2", ref) == cache.build_key("voxcpm|v2", ref)

    def test_key_changes_when_mtime_changes(self, tmp_path):
        cache = ClonePromptCache(disk_dir=tmp_path / "c")
        ref = self._ref(tmp_path)
        k1 = cache.build_key("ns", ref)
        st = os.stat(ref)
        os.utime(ref, (st.st_atime, st.st_mtime + 3600))
        assert cache.build_key("ns", ref) != k1

    def test_key_changes_when_content_grows(self, tmp_path):
        cache = ClonePromptCache(disk_dir=tmp_path / "c")
        ref = self._ref(tmp_path)
        k1 = cache.build_key("ns", ref)
        with open(ref, "ab") as f:
            f.write(b"bb")
        assert cache.build_key("ns", ref) != k1

    def test_key_isolated_per_namespace_and_extra(self, tmp_path):
        cache = ClonePromptCache(disk_dir=tmp_path / "c")
        ref = self._ref(tmp_path)
        assert cache.build_key("ns-a", ref) != cache.build_key("ns-b", ref)
        assert cache.build_key("ns", ref) != cache.build_key("ns", ref, extra=["sr=16000"])

    def test_key_none_when_file_missing(self, tmp_path):
        cache = ClonePromptCache(disk_dir=tmp_path / "c")
        assert cache.build_key("ns", str(tmp_path / "nope.wav")) is None


class TestClonePromptCacheLevels:
    def _entry(self, value=1.0):
        return {"feat": np.full((3, 2), value, dtype=np.float32),
                "dtype": np.array("float32", dtype=np.str_)}

    def test_memory_hit_calls_builder_once(self, tmp_path):
        cache = ClonePromptCache(disk_dir=tmp_path / "c")
        key = "ns_k"
        calls = []

        def build():
            calls.append(1)
            return self._entry()

        first = cache.get_or_build(key, build)
        second = cache.get_or_build(key, build)
        assert len(calls) == 1 and cache.stats()["hits_mem"] == 1
        np.testing.assert_allclose(first["feat"], second["feat"])

    def test_memory_layer_evicts_oldest(self, tmp_path):
        cache = ClonePromptCache(disk_dir=tmp_path / "c", use_disk=False, mem_max=2)
        for i, k in enumerate("abc"):
            cache.put(k, self._entry(float(i)))
        assert cache.stats()["mem_entries"] == 2
        assert cache.get("a") is None                 # 最久未用被挤掉
        assert cache.get("c") is not None

    def test_disk_hit_after_simulated_restart(self, tmp_path):
        d = tmp_path / "c"
        cache = ClonePromptCache(disk_dir=d)
        cache.put("ns_x", self._entry(3.5))
        assert cache.get("ns_x") is not None

        # 新实例 = 进程重启（内存层为空），必须从磁盘命中，不再重算
        restarted = ClonePromptCache(disk_dir=d)
        calls = []

        def build():
            calls.append(1)
            return self._entry(0.0)

        hit = restarted.get_or_build("ns_x", build)
        assert calls == [] and restarted.stats()["hits_disk"] == 1
        np.testing.assert_allclose(hit["feat"], np.full((3, 2), 3.5, np.float32))

    def test_disk_capacity_pruned(self, tmp_path):
        d = tmp_path / "c"
        cache = ClonePromptCache(disk_dir=d, disk_max=2)
        for i in range(5):
            cache.put(f"ns_{i}", self._entry(float(i)))
        assert len(list(d.glob("*.npz"))) == 2
        assert cache.stats()["disk_entries"] == 2

    def test_corrupt_npz_degrades_to_miss(self, tmp_path):
        d = tmp_path / "c"
        d.mkdir(parents=True)
        (d / "ns_bad.npz").write_bytes(b"not an npz at all")
        cache = ClonePromptCache(disk_dir=d)
        calls = []

        def build():
            calls.append(1)
            return self._entry(9.0)

        got = cache.get_or_build("ns_bad", build)
        assert len(calls) == 1 and got is not None
        assert cache.stats()["failures"] >= 1

    def test_wrong_format_tag_ignored(self, tmp_path):
        d = tmp_path / "c"
        d.mkdir(parents=True)
        np.savez(d / "ns_old.npz", allow_pickle=False,
                 feat=np.zeros((2, 2), np.float32),
                 **{"__meta__": np.array(json.dumps({"format": "other-vendor-v9"}))})
        cache = ClonePromptCache(disk_dir=d)
        assert cache.get("ns_old") is None

    def test_builder_returning_none_is_not_cached(self, tmp_path):
        cache = ClonePromptCache(disk_dir=tmp_path / "c")
        calls = []
        assert cache.get_or_build("ns_n", lambda: calls.append(1) or None) is None
        assert cache.get_or_build("ns_n", lambda: calls.append(1) or None) is None
        assert len(calls) == 2 and cache.stats()["mem_entries"] == 0

    def test_non_ndarray_payload_rejected(self, tmp_path):
        cache = ClonePromptCache(disk_dir=tmp_path / "c")
        cache.put("ns_p", {"feat": [1, 2, 3]})          # 非 ndarray -> 不缓存
        assert cache.get("ns_p") is None

    def test_env_switch_disables_disk(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PEIYINYUAN_CLONE_PROMPT_CACHE", "0")
        cache = ClonePromptCache(disk_dir=tmp_path / "c")
        cache.put("ns_e", self._entry())
        assert cache.stats()["disk_enabled"] is False
        assert list((tmp_path / "c").glob("*.npz")) == []
        assert cache.get("ns_e") is not None             # 内存层照常工作

    def test_clear_purges_only_its_namespace(self, tmp_path):
        d = tmp_path / "c"
        cache = ClonePromptCache(disk_dir=d)
        ref_a, ref_b = tmp_path / "a.wav", tmp_path / "b.wav"
        ref_a.write_bytes(b"x" * 10)
        ref_b.write_bytes(b"y" * 10)
        k1 = cache.build_key("ns", str(ref_a))
        k2 = cache.build_key("other-ns", str(ref_b))
        cache.put(k1, self._entry())
        cache.put(k2, self._entry())

        assert cache.clear(namespace="ns") == 1
        assert cache.get(k1) is None and cache.get(k2) is not None
        # get() 是按键直接寻址磁盘文件的，"已清掉"必须真的落盘删除
        assert not (d / f"{k1}.npz").exists()
        assert (d / f"{k2}.npz").exists()

    def test_concurrent_get_or_build_is_safe(self, tmp_path):
        """8 线程抢同一条键：不许抛、不许脏读，条目自洽。"""
        cache = ClonePromptCache(disk_dir=tmp_path / "c")

        def build():
            return self._entry(2.0)

        results = []
        errors = []

        def worker():
            try:
                entry = cache.get_or_build("ns_t", build)
                results.append(np.asarray(entry["feat"]).sum())
            except Exception as exc:                     # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        assert errors == [] and len(results) == 8
        assert set(results) == {float(np.full((3, 2), 2.0, np.float32).sum())}


# ---------- 适配器接线（桩模型，不加载权重） ----------
LAT = np.linspace(-1, 1, 5 * 3 * 8, dtype=np.float32).reshape(5, 3, 8)


class _StubV2Model:
    """模仿 voxcpm2.VoxCPM2Model 的 build_prompt_cache / generate_with_prompt_cache。"""

    def __init__(self, ref_path, dtype="float32"):
        self.ref_path = ref_path
        self.dtype = dtype
        self.build_calls = 0
        self.last_kwargs = None
        self.last_cache = None
        self._encode_sample_rate = 16000
        self.patch_size = 3

    def build_prompt_cache(self, prompt_text=None, prompt_wav_path=None,
                           reference_wav_path=None, trim_silence_vad=False):
        import torch

        self.build_calls += 1
        assert reference_wav_path == self.ref_path
        assert prompt_wav_path is None and prompt_text is None
        return {"ref_audio_feat": torch.from_numpy(LAT.copy()).to(
            getattr(torch, self.dtype)), "mode": "reference"}

    def generate_with_prompt_cache(self, target_text, prompt_cache, **kwargs):
        import torch

        self.last_kwargs = dict(target_text=target_text, **kwargs)
        self.last_cache = prompt_cache
        assert prompt_cache["mode"] == "reference"
        assert tuple(prompt_cache["ref_audio_feat"].shape) == LAT.shape
        wav = torch.zeros(1, 40, dtype=torch.float32)
        wav[0] = torch.arange(40, dtype=torch.float32)
        return wav, torch.zeros(2, dtype=torch.int32), torch.zeros(1, 3, 8)


class _StubLegacyModel(_StubV2Model):
    """旧版模型：build_prompt_cache 没有 reference_wav_path 形参（能力不符）。"""

    def build_prompt_cache(self, prompt_text=None, prompt_wav_path=None):
        self.build_calls += 1
        return {"audio_feat": np.zeros((1, 1), np.float32), "mode": "continuation"}


class _StubCore:
    def __init__(self, tts_model, ref_path):
        self.tts_model = tts_model
        self.ref_path = ref_path
        self.generate_calls = 0
        self.last_reference_wav_path = "unset"

    def generate(self, text, reference_wav_path=None, cfg_value=2.0,
                 inference_timesteps=10):
        import torch

        self.generate_calls += 1
        self.last_reference_wav_path = reference_wav_path
        wav = torch.zeros(1, 40, dtype=torch.float32)
        wav[0] = torch.arange(40, dtype=torch.float32)
        return wav.squeeze(0).cpu().numpy()


def _make_adapter(tmp_path, monkeypatch, model_cls=_StubV2Model, **model_kw):
    import soundfile as sf

    from modules.tts_engine import clone_prompt_cache as cpc_mod
    from modules.tts_engine.engines.voxcpm_adapter import VoxCPMAdapter

    ref = str(tmp_path / "ref.wav")
    sr = 16000
    t = np.arange(sr, dtype=np.float32) / sr
    sf.write(ref, _speech_like(sr, 1.0), sr)

    monkeypatch.setattr(cpc_mod, "clone_prompt_cache",
                        ClonePromptCache(disk_dir=tmp_path / "cache"))
    adapter = VoxCPMAdapter.__new__(VoxCPMAdapter)
    adapter.model_path = str(tmp_path / "weights")
    adapter.device = "cpu"
    adapter._loaded = True
    adapter._cloned_voices = {"v1": (ref, None)}
    adapter._designed_voices = {}
    adapter._model = _StubCore(model_cls(ref, **model_kw), ref)
    return adapter, ref


class TestVoxCPMAdapterCacheWiring:
    def test_second_call_reuses_encoding(self, tmp_path, monkeypatch):
        adapter, _ = _make_adapter(tmp_path, monkeypatch)
        out1 = adapter.synthesize("第一段\n文本", voice_id="v1")
        out2 = adapter.synthesize("第二段文本", voice_id="v1")
        assert adapter._model.tts_model.build_calls == 1
        assert adapter._model.generate_calls == 0
        np.testing.assert_array_equal(out1, out2)

    def test_replaced_reference_file_is_reencoded(self, tmp_path, monkeypatch):
        """参考音频被换掉（mtime/大小变了）-> 键变了 -> 必须重新编码，不得用旧产物。"""
        adapter, ref = _make_adapter(tmp_path, monkeypatch)
        adapter.synthesize("第一段", voice_id="v1")
        assert adapter._model.tts_model.build_calls == 1

        st = os.stat(ref)
        os.utime(ref, (st.st_atime, st.st_mtime + 3600))
        adapter.synthesize("第二段", voice_id="v1")
        assert adapter._model.tts_model.build_calls == 2

    def test_generation_kwargs_match_core_generate(self, tmp_path, monkeypatch):
        """缓存路径必须与直接 generate() 逐参等价，否则就是改了语义。"""
        adapter, _ = _make_adapter(tmp_path, monkeypatch)
        adapter.synthesize("你好\n世界   啊", voice_id="v1")
        kw = adapter._model.tts_model.last_kwargs
        assert kw["target_text"] == "你好 世界 啊"           # 与 core 的空白归一化一致
        assert kw["min_len"] == 2 and kw["max_len"] == 4096
        assert kw["cfg_value"] == 2.0 and kw["inference_timesteps"] == 10
        assert kw["retry_badcase"] is True
        assert kw["retry_badcase_max_times"] == 3
        assert kw["retry_badcase_ratio_threshold"] == 6.0

    def test_legacy_model_falls_back_to_inline(self, tmp_path, monkeypatch):
        adapter, ref = _make_adapter(tmp_path, monkeypatch, model_cls=_StubLegacyModel)
        adapter.synthesize("你好", voice_id="v1")
        assert adapter._model.generate_calls == 1
        assert adapter._model.tts_model.build_calls == 0
        # 回落必须照样带上参考音频，否则克隆音色静默退化成随机音色
        assert adapter._model.last_reference_wav_path == ref

    def test_voice_without_reference_stays_inline(self, tmp_path, monkeypatch):
        adapter, _ = _make_adapter(tmp_path, monkeypatch)
        adapter._cloned_voices = {}
        adapter.synthesize("你好", voice_id=None)
        assert adapter._model.generate_calls == 1
        assert adapter._model.tts_model.build_calls == 0
        assert adapter._model.last_reference_wav_path is None

    def test_cached_path_output_identical_to_inline(self, tmp_path, monkeypatch):
        """同一句话：走缓存与走内联拿到逐样本相同的音频。"""
        adapter, _ = _make_adapter(tmp_path, monkeypatch)
        cached = adapter.synthesize("同一句", voice_id="v1")
        adapter._model.tts_model.build_calls = 0
        inline = adapter._model.generate(text="同一句", reference_wav_path=adapter._cloned_voices["v1"][0])
        np.testing.assert_array_equal(cached, inline)

    def test_bfloat16_encoding_round_trips_exactly(self, tmp_path, monkeypatch):
        import torch

        adapter, _ = _make_adapter(tmp_path, monkeypatch, dtype="bfloat16")
        adapter.synthesize("一", voice_id="v1")
        got = adapter._model.tts_model.last_cache["ref_audio_feat"]
        assert got.dtype == torch.bfloat16
        assert torch.equal(got, torch.from_numpy(LAT).to(torch.bfloat16))

    def test_cache_failure_never_breaks_synthesis(self, tmp_path, monkeypatch):
        """缓存层整个坏掉（构造即抛）时，合成必须照常走内联路径完成。"""
        import modules.tts_engine.clone_prompt_cache as cpc_mod

        class Broken:
            def build_key(self, *a, **k):
                raise OSError("cache dir on fire")

            def get_or_build(self, *a, **k):
                raise OSError("cache dir on fire")

        adapter, _ = _make_adapter(tmp_path, monkeypatch)
        monkeypatch.setattr(cpc_mod, "clone_prompt_cache", Broken())
        out = adapter.synthesize("你好", voice_id="v1")
        assert out.shape == (40,) and adapter._model.generate_calls == 1

    def test_engine_exception_is_not_swallowed(self, tmp_path, monkeypatch):
        adapter, _ = _make_adapter(tmp_path, monkeypatch)
        adapter.synthesize("一", voice_id="v1")            # 先建好缓存

        def boom(*a, **k):
            raise RuntimeError("模型炸了")

        adapter._model.tts_model.generate_with_prompt_cache = boom
        with pytest.raises(RuntimeError, match="模型炸了"):
            adapter.synthesize("二", voice_id="v1")


# ============================================================ P0-4 质量双门
class TestSilenceFloorGate:
    def test_threshold_matches_dbfs_spec(self):
        assert aq.SILENCE_FLOOR_DBFS == -50.0
        assert aq.SILENCE_FLOOR_LINEAR == pytest.approx(10.0 ** (-50.0 / 20.0), rel=1e-12)
        assert aq.peak_dbfs(1.0) == pytest.approx(0.0)
        assert aq.peak_dbfs(aq.SILENCE_FLOOR_LINEAR) == pytest.approx(-50.0, abs=1e-9)
        assert aq.peak_dbfs(0.0) == float("-inf")

    def test_dead_render_fails_gate1(self):
        dead = np.full(16000, 1e-4, dtype=np.float32)      # -80 dBFS
        rep = aq.assess(dead, 16000)
        assert rep["ok"] is False and rep["code"] == aq.CODE_SILENT
        assert rep["peak_dbfs"] < aq.SILENCE_FLOOR_DBFS

    def test_audio_just_above_floor_passes_gate1(self):
        x = _speech_like(seconds=1.0, level=0.004)          # ≈ -48 dBFS
        assert float(np.max(np.abs(x))) > aq.SILENCE_FLOOR_LINEAR
        rep = aq.assess(x, 16000)
        assert rep["code"] != aq.CODE_SILENT and rep["ok"] is True

    def test_empty_and_non_finite_are_named(self):
        assert aq.assess(np.zeros(0, np.float32), 16000)["code"] == aq.CODE_EMPTY
        bad = np.full(16000, 0.3, np.float32)
        bad[10] = np.nan
        assert aq.assess(bad, 16000)["code"] == aq.CODE_NON_FINITE

    def test_int16_input_scaled_by_full_scale(self):
        x = np.full(200, 32767 // 2, dtype=np.int16)
        assert 0.49 < aq.effective_peak(x) < 0.51


class TestDegenerateFlatnessGate:
    def test_flatness_direction_is_small_means_degenerate(self):
        """判据方向：嗡鸣 << 阈值 << 噪声/语音。写反了这条测试就红。"""
        hum = aq.spectral_flatness(_hum(), 16000)
        speech = aq.spectral_flatness(_speech_like(), 16000)
        noise = aq.spectral_flatness(np.random.default_rng(1).standard_normal(32000), 16000)
        assert hum is not None and speech is not None
        assert hum < aq.DEGENERATE_SPECTRAL_FLATNESS
        assert speech > aq.DEGENERATE_SPECTRAL_FLATNESS * 100
        assert noise > speech * 0.5                        # 噪声最平坦
        assert hum < aq.DEGENERATE_SPECTRAL_FLATNESS < noise

    def test_loud_hum_fails_gate2_though_peak_is_healthy(self):
        rep = aq.assess(_hum(level=0.5), 16000)
        assert rep["ok"] is False and rep["code"] == aq.CODE_DEGENERATE
        assert rep["peak"] > 0.4                           # 门 1 完全放过
        assert rep["peak_dbfs"] > aq.SILENCE_FLOOR_DBFS

    def test_speech_like_passes_both_gates(self):
        rep = aq.assess(_speech_like(), 16000)
        assert rep["ok"] is True and rep["code"] == aq.CODE_OK
        assert rep["spectral_flatness"] > aq.DEGENERATE_SPECTRAL_FLATNESS

    def test_unmeasurable_is_not_convicted(self):
        """太短 / 无采样率 -> 测不准 -> 放行（门只给"确定坏了"定罪）。"""
        assert aq.spectral_flatness(_hum()[:80], 16000) is None
        assert aq.spectral_flatness(_hum(), None) is None
        rep = aq.assess(_hum(level=0.5)[:200], 16000)
        assert rep["ok"] is True and rep["spectral_flatness"] is None

    def test_low_energy_frames_are_dropped(self):
        """整段里塞满静音帧也不该把测量拖成"退化"。"""
        body = _speech_like(seconds=1.0)
        padded = np.concatenate([np.zeros(16000, np.float32), body,
                                 np.zeros(16000, np.float32)])
        rep = aq.assess(padded, 16000)
        assert rep["ok"] is True and rep["spectral_flatness"] > aq.DEGENERATE_SPECTRAL_FLATNESS


class TestAssessFile:
    def test_wav_round_trip(self, tmp_path):
        import soundfile as sf

        p = tmp_path / "speech.wav"
        sf.write(str(p), _speech_like(), 16000)
        rep, err = aq.assess_file(str(p))
        assert err is None and rep["ok"] is True

    def test_undecodable_returns_none_report(self, tmp_path):
        p = tmp_path / "broken.wav"
        p.write_bytes(b"this is not audio")
        rep, err = aq.assess_file(str(p))
        assert rep is None and err


class TestGateWithRetry:
    """换 seed 重试一次：第一次坏、第二次好就算成功。"""

    def _engine(self, outputs):
        class Engine:
            sample_rate = 16000

            def __init__(self):
                self.calls = []

            def synthesize(self, text, voice_id=None, speed=1.0, pitch=0.0,
                           emotion="neutral"):
                self.calls.append(dict(text=text, voice_id=voice_id, speed=speed,
                                       pitch=pitch, emotion=emotion))
                return outputs[len(self.calls) - 1]
        return Engine()

    def test_first_bad_second_good_retries_once(self):
        eng = self._engine([_hum(), _speech_like()])
        outcome = aq.synthesize_with_gate(eng, text=" x ", voice_id="v",
                                          speed=1.1, pitch=0.0, emotion="sad")
        assert outcome.attempts == 2 and outcome.report["ok"] is True
        assert len(eng.calls) == 2
        assert eng.calls[0]["emotion"] == "sad" and eng.calls[0]["voice_id"] == "v"

    def test_both_bad_raises_with_reason(self):
        eng = self._engine([_hum(), _hum()])
        with pytest.raises(aq.AudioQualityError) as exc:
            aq.synthesize_with_gate(eng, text="x")
        assert exc.value.attempts == 2
        assert exc.value.code == aq.CODE_DEGENERATE
        assert len(eng.calls) == aq.SYNTH_MAX_ATTEMPTS

    def test_engine_exception_propagates_unmasked(self):
        """PY-117：引擎抛错必须原样上抛，不能被质量门"重试到成功"。"""
        class Broken:
            sample_rate = 16000

            def __init__(self):
                self.calls = 0

            def synthesize(self, **kw):
                self.calls += 1
                raise RuntimeError("CUDA out of memory")

        eng = Broken()
        with pytest.raises(RuntimeError, match="out of memory"):
            aq.synthesize_with_gate(eng, text="x")
        assert eng.calls == 1

    def test_seed_passed_only_when_engine_accepts_it(self):
        """不假造 seed 接口：有形参就透传 base+attempt，没有就照原样调。"""
        class SeedEngine:
            sample_rate = 16000

            def __init__(self):
                self.seeds = []

            def synthesize(self, text, voice_id=None, speed=1.0, pitch=0.0,
                           emotion="neutral", seed=None):
                self.seeds.append(seed)
                return [_hum(), _speech_like()][len(self.seeds) - 1]

        eng = SeedEngine()
        outcome = aq.synthesize_with_gate(eng, text="x", seed=42)
        assert eng.seeds == [42, 43] and outcome.attempts == 2

        eng2 = self._engine([_hum(), _speech_like()])
        aq.synthesize_with_gate(eng2, text="x", seed=42)
        assert all("seed" not in c for c in eng2.calls)


class TestCloneValidationUsesGates:
    def test_loud_hum_rejected_as_reference(self, tmp_path):
        import soundfile as sf

        from modules.voice_manager.voice_cloner import VoiceCloner

        p = tmp_path / "hum.wav"
        sf.write(str(p), _hum(seconds=4.0), 16000)
        res = VoiceCloner().validate_audio(str(p))
        assert res["valid"] is False
        assert res["quality"]["code"] == aq.CODE_DEGENERATE
        assert "质量不合格" in res["message"]

    def test_speech_like_reference_accepted(self, tmp_path):
        import soundfile as sf

        from modules.voice_manager.voice_cloner import VoiceCloner

        p = tmp_path / "speech.wav"
        sf.write(str(p), _speech_like(seconds=12.0), 16000)
        res = VoiceCloner().validate_audio(str(p))
        assert res["valid"] is True and res["duration"] > 10

    def test_too_short_still_rejected_before_gates(self, tmp_path):
        import soundfile as sf

        from modules.voice_manager.voice_cloner import VoiceCloner

        p = tmp_path / "short.wav"
        sf.write(str(p), _speech_like(seconds=1.0), 16000)
        res = VoiceCloner().validate_audio(str(p))
        assert res["valid"] is False and "太短" in res["message"]

    def test_preview_path_skips_degenerate_sample(self, tmp_path, monkeypatch):
        """get_preview_audio 只给通过双门的样本；坏样本 -> None（调用方 404）。"""
        import soundfile as sf

        from backend.services.voice_service import VoiceService
        from modules.voice_manager import voice_store as vs_mod

        good = tmp_path / "good.wav"
        bad = tmp_path / "bad.wav"
        sf.write(str(good), _speech_like(seconds=6.0), 16000)
        sf.write(str(bad), _hum(seconds=6.0), 16000)

        service = VoiceService.__new__(VoiceService)
        row = types.SimpleNamespace(sample_audio_path=str(bad),
                                    model_path=str(good))
        monkeypatch.setattr(vs_mod.voice_store, "get", lambda vid: row)
        assert service.get_preview_audio("v") == str(good)

        row2 = types.SimpleNamespace(sample_audio_path=str(bad), model_path=None)
        monkeypatch.setattr(vs_mod.voice_store, "get", lambda vid: row2)
        assert service.get_preview_audio("v") is None


# ============================================================ P0-1 条件单点解析
class TestConditioningPriority:
    def test_overrides_beat_layers_beat_row_beat_defaults(self):
        profile = {"id": "v9", "engine": "voxcpm", "language": "en",
                   "gender": "female", "speed": 1.2, "pitch": 1.0,
                   "emotion": "gentle", "style_tags": "温柔, 女声",
                   "meta": {"speed": 1.5, "pitch": 2.0, "emotion": "sad"}}
        cond = resolve_profile_conditioning(
            profile,
            overrides={"speed": 0.9},
            layers=[{"pitch": 3.0}, {"emotion": "excited", "pitch": 9.0}],
            defaults={"speed": 1.0, "pitch": 0.0, "emotion": "neutral"},
        )
        assert cond["speed"] == 0.9                      # overrides 最高
        assert cond["pitch"] == 3.0                      # layers 先者优先
        assert cond["emotion"] == "excited"              # 音色行 meta 让位于 layer
        assert cond["engine"] == "voxcpm"                # 音色行声明的引擎
        assert cond["voice_id"] == "v9"
        assert cond["language"] == "en" and cond["gender"] == "female"
        assert cond["style_tags"] == ["温柔", "女声"]     # CSV 切开

    def test_meta_layer_below_explicit_layers(self):
        profile = {"engine": "voxcpm", "meta": {"speed": 1.8, "emotion": "sad"}}
        cond = resolve_profile_conditioning(profile, layers=[{"speed": 1.1}])
        assert cond["speed"] == 1.1
        cond2 = resolve_profile_conditioning(profile)
        assert cond2["speed"] == 1.8 and cond2["emotion"] == "sad"

    def test_defaults_used_when_nothing_declared(self):
        cond = resolve_profile_conditioning(None, defaults={"speed": 1.35, "pitch": -2.0})
        assert cond["speed"] == 1.35 and cond["pitch"] == -2.0
        assert cond["emotion"] == "neutral"

    def test_engine_is_never_guessed(self):
        """音色行没写引擎时返回 None —— 路由兜底是调用点的业务知识，不替调用方猜。"""
        assert resolve_profile_conditioning({"name": "x"})["engine"] is None
        assert resolve_profile_conditioning(None, defaults={"engine": "edge"}
                                            )["engine"] == "edge"

    def test_orm_row_attribute_access(self):
        row = types.SimpleNamespace(id="abc", name="旁白", source="builtin",
                                    category="human", engine="voxcpm",
                                    language="zh", gender="male", age_group="adult",
                                    description="d", reference_audio_path="r.wav",
                                    sample_audio_path="s.wav",
                                    style_tags=["沉稳"], meta={"speed": 1.1})
        cond = resolve_profile_conditioning(row)
        assert cond["engine"] == "voxcpm" and cond["speed"] == 1.1
        assert cond["style_tags"] == ["沉稳"]
        assert cond["reference_audio_path"] == "r.wav"

    def test_pure_function_does_not_mutate_inputs(self):
        profile = {"engine": "voxcpm", "meta": {"speed": 1.4}}
        layers = [{"speed": 2.5}]
        resolve_profile_conditioning(profile, layers=layers)
        assert profile == {"engine": "voxcpm", "meta": {"speed": 1.4}}
        assert layers == [{"speed": 2.5}]

    def test_clamping_and_non_numeric_fallthrough(self):
        assert resolve_profile_conditioning(None, overrides={"speed": 9})["speed"] == 2.0
        assert resolve_profile_conditioning(None, overrides={"speed": 0.01})["speed"] == 0.5
        assert resolve_profile_conditioning(None, overrides={"pitch": -99})["pitch"] == -12.0
        assert resolve_profile_conditioning(None, overrides={"pitch": 99})["pitch"] == 12.0
        # 非法值不算"给了值"，继续向下一层探，而不是直接跳内置默认
        cond = resolve_profile_conditioning(
            None, layers=[{"speed": "abc", "pitch": float("nan")},
                          {"speed": 1.3, "pitch": 2.0}],
            defaults={"speed": 1.0, "pitch": 0.0})
        assert cond["speed"] == 1.3 and cond["pitch"] == 2.0

    def test_string_numbers_are_coerced(self):
        cond = resolve_profile_conditioning(None, layers=[{"speed": "1.25", "pitch": "-1"}])
        assert cond["speed"] == 1.25 and cond["pitch"] == -1.0

    def test_return_key_set_is_fixed(self):
        from modules.voice_manager.conditioning import CONDITIONING_KEYS

        cond = resolve_profile_conditioning(None)
        assert set(cond) == set(CONDITIONING_KEYS)
        assert {"speed", "pitch", "emotion"} <= set(cond)   # 与引擎形参同名


class TestPipelineSiteEquivalence:
    """P7 站点重构前后的等价性：用旧实现当参照，逐条输入比对。"""

    @staticmethod
    def _old_impl(sent, settings=None, default_speed=1.0, default_pitch=0.0):
        """HEAD 版本 modules/workflow/cast_pipeline_hooks.py::synth_params_for_sentence
        的算法（此处按行为重写一遍当参照物，不是复制来的实现）。"""
        def num(value, fallback):
            try:
                v = float(value)
            except (TypeError, ValueError):
                return float(fallback)
            return v if np.isfinite(v) else float(fallback)

        settings = settings if isinstance(settings, dict) else {}
        g_speed = num(settings.get("speed"), default_speed)
        g_pitch = num(settings.get("pitch"), default_pitch)
        sent = sent if isinstance(sent, dict) else {}
        speed = num(sent.get("speed"), g_speed)
        pitch = num(sent.get("pitch"), g_pitch)
        speed = float(min(max(speed, 0.5), 2.0))
        pitch = float(min(max(pitch, -12.0), 12.0))
        return {"speed": speed, "pitch": pitch, "emotion": str(sent.get("tone") or "neutral")}

    CASES = [
        ({}, None, 1.0, 0.0),
        ({"speed": 1.4}, {"speed": 1.1}, 1.0, 0.0),
        ({"speed": "1.4", "pitch": "2"}, {"pitch": 3}, 1.0, 0.0),
        ({"speed": 99, "pitch": 999}, {}, 1.0, 0.0),
        ({"speed": 0.01, "pitch": -999}, {}, 1.0, 0.0),
        ({"speed": None, "pitch": float("nan")}, {"speed": float("nan")}, 1.25, -0.5),
        ({"speed": "abc"}, {"speed": "def"}, 1.5, 1.0),
        ({"tone": "sad"}, {"speed": 1.2}, 1.0, 0.0),
        ({"tone": ""}, {}, 1.0, 0.0),
        ({"tone": None}, {}, 1.0, 0.0),
        (None, None, 1.0, 0.0),
        ("not-a-dict", ["nope"], 1.0, 0.0),
        ({"speed": 1, "pitch": 0}, {"speed": 2, "pitch": 1}, 1.0, 0.0),
    ]

    @pytest.mark.parametrize("sent,settings,ds,dp", CASES)
    def test_new_matches_old(self, sent, settings, ds, dp):
        from modules.workflow.cast_pipeline_hooks import synth_params_for_sentence

        assert synth_params_for_sentence(sent, settings, ds, dp) == \
            self._old_impl(sent, settings, ds, dp)

    def test_cast_preview_site_equivalence(self):
        """cast.py 试听站点：旧写法（借道 P7 helper）与新写法（直调单点函数）同值。"""
        from modules.workflow.cast_pipeline_hooks import synth_params_for_sentence

        for role_mix in [{}, {"speed": 1.3}, {"speed": None, "pitch": 2.0},
                         {"speed": "x", "pitch": None}, {"speed": 5, "pitch": -50}]:
            cast = {"mix": {"speed": 1.9}}                  # 旧写法从不读它
            old = synth_params_for_sentence(
                {"speed": role_mix.get("speed"), "pitch": role_mix.get("pitch"),
                 "role_id": "r1"},
                {"speed": None, "pitch": None, "mix": cast.get("mix") or {}},
            )
            new = resolve_profile_conditioning(
                None, overrides={"emotion": "neutral"}, layers=[role_mix],
                defaults={"speed": 1.0, "pitch": 0.0})
            assert (old["speed"], old["pitch"], old["emotion"]) == \
                (new["speed"], new["pitch"], new["emotion"])

    def test_dubbing_preview_site_priority(self):
        """dubbing 试听站点：项目全局 settings > 音色自带 meta > 默认。

        项目 settings 是用户在这个项目上显式拧的旋钮，压过音色自带的偏好；
        settings 缺键（或给了 None）时才轮到 meta。旧实现只看 settings、
        从不传 pitch、也不夹 speed。
        """
        profile = {"engine": "voxcpm", "meta": {"speed": 1.7, "pitch": 3.0}}
        defaults = {"speed": 1.0, "pitch": 0.0, "emotion": "neutral"}

        cond = resolve_profile_conditioning(
            profile, layers=[{"speed": 1.2, "pitch": 1.0, "emotion": "gentle"}],
            defaults=defaults)
        assert cond["speed"] == 1.2 and cond["pitch"] == 1.0
        assert cond["emotion"] == "gentle" and cond["engine"] == "voxcpm"

        # settings 未指定（None/缺键）-> 轮到音色 meta
        cond2 = resolve_profile_conditioning(
            profile, layers=[{"speed": None, "emotion": "sad"}], defaults=defaults)
        assert cond2["speed"] == 1.7 and cond2["pitch"] == 3.0
        assert cond2["emotion"] == "sad"

        # 两层都没给 -> 调用点默认值
        cond3 = resolve_profile_conditioning(None, layers=[{}], defaults=defaults)
        assert cond3["speed"] == 1.0 and cond3["pitch"] == 0.0

    def test_engine_routing_site_uses_profile_engine(self):
        """conversion_workflow 的句级引擎路由：行里有 engine 用它，没有则落回默认。"""
        engine_name = "voxcpm"
        assert resolve_profile_conditioning({"engine": "edgetts"},
                                            defaults={"engine": engine_name}
                                            )["engine"] == "edgetts"
        assert resolve_profile_conditioning({"engine": None},
                                            defaults={"engine": engine_name}
                                            )["engine"] == engine_name
        assert resolve_profile_conditioning(None, defaults={"engine": engine_name}
                                            )["engine"] == engine_name


# ============================================================ 试听端点接线（桩引擎）
class _StubPreviewEngine:
    """试听端点用到的最小引擎替身。"""

    engine_name = "voxcpm"
    sample_rate = 48000
    supports_cloning = True

    def __init__(self):
        self.audio = _speech_like(1.0, 48000)
        self.calls = []

    def synthesize(self, text, voice_id=None, speed=1.0, pitch=0.0,
                   emotion="neutral"):
        self.calls.append(dict(speed=speed, pitch=pitch, emotion=emotion,
                               voice_id=voice_id, text=text))
        return self.audio


@pytest.fixture
def stub_engine(monkeypatch):
    import backend.api.cast as cast_api
    import backend.api.dubbing as dubbing_api
    from modules.tts_engine.engine_registry import engine_registry

    engine = _StubPreviewEngine()
    monkeypatch.setattr(engine_registry, "get_loaded", lambda name, **kw: engine)
    monkeypatch.setattr(engine_registry, "is_loaded", lambda name: True)
    monkeypatch.setattr(cast_api, "_require_project", lambda pid: {
        "project_id": pid,
        "roles": [{"id": "r1", "name": "甲", "voice_id": "v1",
                   "mix": {"speed": 1.4, "pitch": 2.0},
                   "cast": {"sample_quotes": ["你好，我是甲。"]},
                   "extra_data": {}}],
        "mix": {"speed": 1.9}})
    monkeypatch.setattr(dubbing_api.dubbing_service, "get_project", lambda pid: {
        "id": pid, "assignments": [{"voice_id": "v1"}], "roles": [],
        "global_settings": {"speed": 1.2, "pitch": 1.0, "emotion": "gentle"}})
    from modules.voice_manager.voice_store import voice_store
    monkeypatch.setattr(voice_store, "get", lambda vid: types.SimpleNamespace(
        id=vid, name="n", source="cloned", category="human", engine="voxcpm",
        language="zh", gender="female", age_group="adult", description="",
        reference_audio_path="r.wav", sample_audio_path=None,
        style_tags=None, meta={"speed": 1.9}))
    return engine


def _client(router, prefix):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.include_router(router, prefix=prefix)
    return TestClient(app)


class TestPreviewEndpointWiring:
    def test_dubbing_preview_passes_conditioned_params(self, stub_engine):
        import backend.api.dubbing as dubbing_api

        client = _client(dubbing_api.router, "/api/dubbing")
        resp = client.post("/api/dubbing/projects/p1/preview")
        assert resp.status_code == 200, resp.text
        assert resp.headers["content-type"].startswith("audio/")
        call = stub_engine.calls[-1]
        # 项目 settings 压过音色 meta.speed=1.9；pitch 现在也真的传了（旧代码不传）
        assert call["speed"] == 1.2 and call["pitch"] == 1.0
        assert call["emotion"] == "gentle" and call["voice_id"] == "v1"

    def test_dubbing_preview_clamps_out_of_range(self, stub_engine, monkeypatch):
        import backend.api.dubbing as dubbing_api

        monkeypatch.setattr(dubbing_api.dubbing_service, "get_project", lambda pid: {
            "id": pid, "assignments": [{"voice_id": "v1"}], "roles": [],
            "global_settings": {"speed": 9, "pitch": 40, "emotion": "sad"}})
        client = _client(dubbing_api.router, "/api/dubbing")
        assert client.post("/api/dubbing/projects/p1/preview").status_code == 200
        call = stub_engine.calls[-1]
        assert call["speed"] == 2.0 and call["pitch"] == 12.0

    def test_dubbing_preview_gates_bad_audio_with_one_retry(self, stub_engine):
        import backend.api.dubbing as dubbing_api

        stub_engine.audio = _hum(seconds=1.0, sample_rate=48000)
        client = _client(dubbing_api.router, "/api/dubbing")
        before = len(stub_engine.calls)
        resp = client.post("/api/dubbing/projects/p1/preview")
        assert resp.status_code == 502
        assert "质量门" in resp.json()["detail"]
        assert len(stub_engine.calls) - before == aq.SYNTH_MAX_ATTEMPTS   # 首发 + 一次重试

    def test_cast_preview_uses_role_mix(self, stub_engine):
        import backend.api.cast as cast_api

        client = _client(cast_api.router, "/api")
        resp = client.post("/api/dubbing/projects/p1/cast/preview",
                           json={"role_id": "r1"})
        assert resp.status_code == 200, resp.text
        call = stub_engine.calls[-1]
        assert call["speed"] == 1.4 and call["pitch"] == 2.0
        assert call["emotion"] == "neutral"                  # 试听不参与 P5 语气检测

    def test_cast_preview_gates_bad_audio_with_one_retry(self, stub_engine):
        import backend.api.cast as cast_api

        stub_engine.audio = _hum(seconds=1.0, sample_rate=48000)
        client = _client(cast_api.router, "/api")
        before = len(stub_engine.calls)
        resp = client.post("/api/dubbing/projects/p1/cast/preview",
                           json={"role_id": "r1"})
        assert resp.status_code == 502
        assert len(stub_engine.calls) - before == aq.SYNTH_MAX_ATTEMPTS
