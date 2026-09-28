"""OpenVoice 本地即时克隆档适配器的单元验证（假模型，不加载 278MB 权重）。

结构照 tests/test_gptsovits_adapter.py，覆盖 #76 S3 的契约面：
- PY-304 加载失败必须带原因上抛、不留半死引擎；
- PY-117 空/近静音产物判失败，偏轻产物确定性抬峰；
- 克隆 = 抽 SE（当场做、当场归因），音色行必须自带参考音频且属于本引擎；
- US-C4 seed 真兑现（同 seed 同产物）；pitch 显式拒绝。
"""
import os
import sys
import types

import numpy as np
import pytest
import soundfile as sf

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import modules.tts_engine.engines.openvoice_adapter as oa  # noqa: E402

SR = 22050


class FakeBase:
    """假基座：记录调用参数，产物峰值可注入；用 np.random 制造 seed 敏感性。"""

    def __init__(self, peak=0.18, seconds=1.0, rng_noise=True):
        self.hps = types.SimpleNamespace(
            data=types.SimpleNamespace(sampling_rate=SR),
            speakers={"default": 0})
        self.calls = []
        self.peak = peak
        self.seconds = seconds
        self.rng_noise = rng_noise

    def tts(self, text, output_path, speaker, language, speed):
        self.calls.append({"text": text, "speaker": speaker,
                           "language": language, "speed": speed})
        n = int(SR * self.seconds)
        t = np.arange(n) / SR
        y = self.peak * np.sin(2 * np.pi * 220 * t)
        if self.rng_noise:
            y = y + 0.5 * self.peak * np.random.randn(n)   # 受 seed 控制
        return np.asarray(y, dtype=np.float32)


class FakeConverter:
    def __init__(self, se=None, out=None, out_peak=0.2, noise=False):
        self.hps = types.SimpleNamespace(
            data=types.SimpleNamespace(sampling_rate=SR))
        self.extract_calls = []
        self.convert_calls = []
        self._se = np.zeros((1, 256), dtype=np.float32) if se is None else se
        self._out = out
        self._out_peak = out_peak
        # 默认给**确定性正弦**：噪声的峰值远高于 out_peak（22k 点 max|rng|≈4），
        # 会绕过待测的抬峰分支；只有 seed 复现性那一例才需要随机性。
        self._noise = noise

    def extract_se(self, ref_list, se_save_path=None):
        self.extract_calls.append(list(ref_list))
        return self._se

    def convert(self, audio_src_path, src_se, tgt_se, output_path=None,
                tau=0.3, message="default"):
        self.convert_calls.append({"audio_src_path": audio_src_path,
                                   "src_se": src_se, "tgt_se": tgt_se,
                                   "tau": tau})
        if self._out is not None:
            return np.asarray(self._out, dtype=np.float32)
        n = int(sf.info(audio_src_path).frames)
        if self._noise:
            return (self._out_peak * np.random.randn(n)).astype(np.float32)
        t = np.arange(n) / SR
        return (self._out_peak * np.sin(2 * np.pi * 300 * t)).astype(np.float32)


def _attach(engine, base=None, converter=None, src_se=None):
    """装一具假权重（等价于 load_model 成功后的形态）。"""
    base = base or FakeBase()
    converter = converter or FakeConverter()
    engine._model = oa._LoadedOpenVoice(
        base, converter,
        np.zeros((1, 256), dtype=np.float32) if src_se is None else src_se,
        SR, SR, engine.model_path)
    engine._loaded = True
    return base, converter


@pytest.fixture
def ref_wav(tmp_path):
    p = tmp_path / "ref.wav"
    t = np.arange(int(SR * 3.0)) / SR
    sf.write(str(p), (0.3 * np.sin(2 * np.pi * 120 * t)).astype(np.float32), SR)
    return str(p)


class TestContract:
    def test_registered_and_capabilities(self):
        from modules.tts_engine.engine_registry import engine_registry
        inst = engine_registry.get("openvoice")
        assert inst is not None
        assert inst.supports_cloning and inst.requires_model_object
        assert inst.sample_rate == 22050 and inst.supports_seed
        assert not getattr(inst, "online", False)   # 本地档：占预热槽，不走 HTTP
        assert engine_registry.is_loaded("openvoice") is False   # 仅实例化，未加载

    def test_load_failure_raises_and_leaves_no_half_dead_engine(self, tmp_path):
        eng = oa.OpenVoiceAdapter(model_path=str(tmp_path / "nowhere"))
        with pytest.raises(FileNotFoundError, match="权重不完整"):
            eng.load_model()
        assert eng._model is None and eng._loaded is False
        assert eng.is_loaded() is False             # PY-304
        # "吞掉异常只置 _loaded" 的半死形态同样不得放行
        eng._loaded = True
        assert eng.is_loaded() is False
        with pytest.raises(RuntimeError, match="未加载"):
            eng.synthesize("你好", voice_id="v1")


class TestClone:
    def test_clone_extracts_se_once_and_caches(self, ref_wav):
        eng = oa.OpenVoiceAdapter()
        _base, conv = _attach(eng)
        vid = eng.clone_voice(audio_path=ref_wav, transcript=None, voice_id="v1")
        assert vid == "v1"
        assert len(conv.extract_calls) == 1
        assert eng._voice_se["v1"] is not None
        # 合成期不再重复抽 SE
        eng.synthesize("测试文本", voice_id="v1")
        assert len(conv.extract_calls) == 1

    def test_clone_generates_id_when_missing(self, ref_wav):
        eng = oa.OpenVoiceAdapter()
        _attach(eng)
        vid = eng.clone_voice(audio_path=ref_wav)
        assert vid.startswith("cloned_") and vid in eng._cloned_voices

    def test_clone_missing_file_raises(self):
        eng = oa.OpenVoiceAdapter()
        _attach(eng)
        with pytest.raises(FileNotFoundError):
            eng.clone_voice(audio_path="D:/不存在/ghost.wav", voice_id="v1")

    def test_empty_se_rejected_not_silent_fallback(self, ref_wav):
        eng = oa.OpenVoiceAdapter()
        _attach(eng, converter=FakeConverter(se=np.zeros((0,), dtype=np.float32)))
        with pytest.raises(RuntimeError, match="音色向量为空"):
            eng.clone_voice(audio_path=ref_wav, voice_id="v1")

    def test_voice_without_ref_raises(self, monkeypatch):
        from modules.voice_manager import voice_store as vs_mod
        monkeypatch.setattr(vs_mod.voice_store, "get", lambda vid: types.SimpleNamespace(
            name="n", engine="openvoice", reference_audio_path="D:/无/此.wav",
            model_path=None, meta={}, source="cloned"))
        eng = oa.OpenVoiceAdapter()
        _attach(eng)
        with pytest.raises(RuntimeError, match="查无此行|参考音频缺失"):
            eng.synthesize("文本", voice_id="ghost")

    def test_voice_of_other_engine_is_not_stolen(self, monkeypatch, ref_wav):
        """PY-116 路由口径：engine 写明别家引擎的行，openvoice 绝不冒认。"""
        from modules.voice_manager import voice_store as vs_mod
        monkeypatch.setattr(vs_mod.voice_store, "get", lambda vid: types.SimpleNamespace(
            name="n", engine="voxcpm", reference_audio_path=ref_wav,
            model_path=ref_wav, meta={}, source="cloned"))
        eng = oa.OpenVoiceAdapter()
        _attach(eng)
        with pytest.raises(RuntimeError, match="不属于|由 openvoice"):
            eng.synthesize("文本", voice_id="other-engine")

    def test_restore_voices_only_own_engine(self, monkeypatch, ref_wav):
        rows = [
            types.SimpleNamespace(id="mine", engine="openvoice", source="cloned",
                                  reference_audio_path=ref_wav, model_path=ref_wav,
                                  meta={"transcript": "转写"}),
            types.SimpleNamespace(id="theirs", engine="voxcpm", source="cloned",
                                  reference_audio_path=ref_wav, model_path=ref_wav,
                                  meta={}),
            types.SimpleNamespace(id="broken", engine="openvoice", source="cloned",
                                  reference_audio_path="D:/无/此.wav",
                                  model_path=None, meta={}),
        ]
        from modules.voice_manager import voice_store as vs_mod
        monkeypatch.setattr(vs_mod.voice_store, "list",
                            lambda limit=100, offset=0, **kw: rows[offset:offset + limit])
        eng = oa.OpenVoiceAdapter()
        eng._restore_voices()
        assert list(eng._cloned_voices) == ["mine"]
        assert eng._cloned_voices["mine"][1] == "转写"


class TestSynthesize:
    def test_low_peak_normalized_and_float32(self, ref_wav):
        eng = oa.OpenVoiceAdapter()
        base, conv = _attach(eng, base=FakeBase(peak=0.18))
        eng.clone_voice(audio_path=ref_wav, voice_id="v1")
        audio = eng.synthesize("各位晚上好，欢迎收听。", voice_id="v1")
        assert audio.dtype == np.float32 and audio.ndim == 1
        peak = float(np.max(np.abs(audio)))
        assert 0.55 <= peak <= 0.85                      # 抬到 0.7 目标
        assert conv.convert_calls and conv.convert_calls[0]["tau"] == oa.DEFAULT_TAU
        tmp_wav = conv.convert_calls[0]["audio_src_path"]
        assert tmp_wav.endswith(".wav")
        assert not os.path.isfile(tmp_wav)               # 中间临时件已回收

    def test_builtin_voice_skips_conversion(self):
        eng = oa.OpenVoiceAdapter()
        _base, conv = _attach(eng)
        audio = eng.synthesize("这是基座音色。", voice_id="openvoice_default")
        assert conv.convert_calls == []                  # 无目标 SE → 不转换
        assert float(np.max(np.abs(audio))) > 0.02

    def test_silence_rejected(self):
        eng = oa.OpenVoiceAdapter()
        _attach(eng, converter=FakeConverter(out=np.zeros(SR, dtype=np.float32)))
        eng._cloned_voices["v1"] = ("D:/x/ref.wav", None)
        eng._voice_se["v1"] = np.zeros((1, 256), dtype=np.float32)
        with pytest.raises(RuntimeError, match="静音"):
            eng.synthesize("文本", voice_id="v1")

    def test_empty_audio_rejected(self):
        eng = oa.OpenVoiceAdapter()
        _attach(eng, converter=FakeConverter(out=np.zeros(0, dtype=np.float32)))
        eng._voice_se["v1"] = np.zeros((1, 256), dtype=np.float32)
        eng._cloned_voices["v1"] = ("D:/x/ref.wav", None)
        with pytest.raises(RuntimeError, match="空音频"):
            eng.synthesize("文本", voice_id="v1")

    def test_pitch_rejected_not_silent(self):
        eng = oa.OpenVoiceAdapter()
        _attach(eng)
        with pytest.raises(ValueError, match="pitch"):
            eng.synthesize("文本", voice_id=None, pitch=3.0)

    def test_empty_text_rejected(self):
        eng = oa.OpenVoiceAdapter()
        _attach(eng)
        with pytest.raises(ValueError, match="空文本"):
            eng.synthesize("   ", voice_id=None)

    def test_speed_clamped_and_language_pick(self):
        eng = oa.OpenVoiceAdapter()
        base, _conv = _attach(eng)
        eng.synthesize("中文句子", voice_id=None, speed=9.0)
        assert base.calls[-1]["speed"] == oa.SPEED_RANGE[1]
        assert base.calls[-1]["language"] == "chinese"
        eng.synthesize("a plain english sentence", voice_id=None, speed=0.1)
        assert base.calls[-1]["speed"] == oa.SPEED_RANGE[0]
        assert base.calls[-1]["language"] == "english"

    def test_seed_reproducible(self):
        """US-C4: supports_seed=True 必须**真**兑现 —— 同 seed 同产物。"""
        eng = oa.OpenVoiceAdapter()
        _attach(eng, converter=FakeConverter(out=None, noise=True))
        a = eng.synthesize("复现性验证", voice_id=None, seed=7)
        b = eng.synthesize("复现性验证", voice_id=None, seed=7)
        c = eng.synthesize("复现性验证", voice_id=None, seed=8)
        assert np.array_equal(a, b)
        assert not np.array_equal(a, c)

    def test_unload_restores_torch_threads(self):
        torch = pytest.importorskip("torch")
        eng = oa.OpenVoiceAdapter()
        before = int(torch.get_num_threads())
        eng._model = oa._LoadedOpenVoice(FakeBase(), FakeConverter(),
                                         np.zeros((1, 256), dtype=np.float32),
                                         SR, SR, eng.model_path)
        eng._loaded = True
        eng._torch_threads_before = before
        eng.unload_model()
        assert eng._model is None and eng.is_loaded() is False
        assert int(torch.get_num_threads()) == before


class TestPeakGate:
    """SPIKE §5 双门口径的确定性峰值归一（#76 S3 收尾：从"内联五行"提成单点闸门）。"""

    @staticmethod
    def _stub(peak: float, seconds: float = 1.0, seed: int = 3) -> np.ndarray:
        """峰宽可控的桩：正弦 + 突发，保证峰值不是单点毛刺（相关系数才有意义）。"""
        rng = np.random.RandomState(seed)
        n = int(SR * seconds)
        t = np.arange(n) / SR
        y = 0.6 * np.sin(2 * np.pi * 137 * t) + 0.4 * np.sin(2 * np.pi * 403 * t)
        y = y + 0.15 * rng.randn(n)
        y = y / np.max(np.abs(y)) * peak
        return y.astype(np.float32)

    def test_synthesize_lifts_014_peak_preserving_shape(self, ref_wav):
        """0.14 峰（SPIKE 实测短句档）→ 归一到 0.6~0.8，且**波形包络不变**。"""
        eng = oa.OpenVoiceAdapter()
        raw = self._stub(0.14)
        _attach(eng, converter=FakeConverter(out=raw))
        eng._cloned_voices["v1"] = (ref_wav, None)
        eng._voice_se["v1"] = np.zeros((1, 256), dtype=np.float32)
        out = eng.synthesize("短句峰值偏轻实测零点一四", voice_id="v1")
        peak = float(np.max(np.abs(out)))
        assert 0.6 <= peak <= 0.8, peak
        # 等比增益 ⇒ 相关系数 ≈ 1（钉死"只抬峰不整形"，日后换压限器必须改这条）
        corr = float(np.corrcoef(out[:raw.size], raw)[0, 1])
        assert corr > 0.9999, corr
        assert np.isclose(peak / 0.14, oa.NORMALIZE_TARGET_PEAK / 0.14, rtol=1e-3)

    def test_near_silence_raises_and_no_silent_file_left(self, tmp_path, ref_wav):
        """peak<0.02 → 抛错，且**不产出静音文件**（PY-117 双门；目录必须仍是空的）。"""
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        eng = oa.OpenVoiceAdapter()
        quiet = (1e-4 * np.sin(2 * np.pi * 200 * np.arange(SR) / SR)).astype(np.float32)
        assert float(np.max(np.abs(quiet))) < oa.SILENCE_PEAK_FLOOR
        _attach(eng, converter=FakeConverter(out=quiet))
        eng._cloned_voices["v1"] = (ref_wav, None)
        eng._voice_se["v1"] = np.zeros((1, 256), dtype=np.float32)
        cwd = os.getcwd()
        os.chdir(str(out_dir))
        try:
            with pytest.raises(RuntimeError, match="近静音"):
                eng.synthesize("会不会留下静音文件", voice_id="v1")
        finally:
            os.chdir(cwd)
        assert list(out_dir.iterdir()) == []

    @pytest.mark.parametrize("peak", [0.52, 0.7, 0.9, 0.98])
    def test_in_band_peak_untouched_bit_for_bit(self, peak):
        a = self._stub(peak, seconds=0.4)
        out, before, action = oa._peak_gate(a)
        assert before == pytest.approx(peak, abs=2e-3)
        assert action == ""
        assert np.array_equal(out, a.astype(np.float32))

    @pytest.mark.parametrize("peak", [0.995, 1.0])
    def test_over_ceiling_scaled_back_under_099(self, peak):
        a = self._stub(peak, seconds=0.4)
        out, before, action = oa._peak_gate(a)
        assert out.size == a.size
        assert float(np.max(np.abs(out))) == pytest.approx(oa.NORMALIZE_TARGET_PEAK,
                                                           abs=1e-3)
        assert "backoff" in action
        # clip_ratio 口径：回抬后不再有任何贴边采样点
        assert float(np.mean(np.abs(out) >= 0.999)) == 0.0

    def test_gate_empty_input_rejected(self):
        with pytest.raises(RuntimeError, match="空数组"):
            oa._peak_gate(np.zeros(0, dtype=np.float32))

    def test_synthesize_signature_declares_seed(self):
        """US-C4 签名对齐基类：`seed` 在参数表**末尾**（老位置调用不受影响）。"""
        import inspect
        params = list(inspect.signature(oa.OpenVoiceAdapter.synthesize).parameters)
        assert params[:6] == ["self", "text", "voice_id", "speed", "pitch", "emotion"]
        assert params[-1] == "seed"
        assert inspect.signature(oa.OpenVoiceAdapter.synthesize).parameters[
            "seed"].default is None
