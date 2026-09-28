"""
VA-4 sample 直插播放引擎的单元验证

只跑单元级：不连生产 DB、不加载权重。voice_store 以桩替身注入。
验证的是"路由接线 + 失败归因 + 缓存幂等"，不是音质。
"""
import os
import sys
import types

import numpy as np
import pytest
import soundfile as sf

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from modules.tts_engine.engines.sample_playback import SamplePlaybackEngine


def _write_wav(path, sr, seconds=1.0, freq=220.0, stereo=False, level=0.5):
    n = int(sr * seconds)
    t = np.arange(n, dtype=np.float64) / sr
    mono = (level * np.sin(2 * np.pi * freq * t)).astype(np.float32)
    y = np.stack([mono, mono * 0.5], axis=1) if stereo else mono
    sf.write(str(path), y, sr, subtype="PCM_16", format="WAV")
    return path


def _profile(path):
    return types.SimpleNamespace(id="v1", name="测试样本", model_path=str(path),
                                 sample_audio_path=None)


@pytest.fixture
def engine_with_stub_profile(tmp_path, monkeypatch):
    """引擎 + 把 _profile_for 换成查表桩（不碰 voice_store/DB）。"""
    eng = SamplePlaybackEngine()
    holder = {"profile": None}

    def fake_profile_for(self, voice_id):
        return holder["profile"] if voice_id == "v1" else None

    monkeypatch.setattr(SamplePlaybackEngine, "_profile_for", fake_profile_for)
    return eng, holder


class TestSynthesize:
    def test_requires_voice_id(self, engine_with_stub_profile):
        eng, _ = engine_with_stub_profile
        with pytest.raises(RuntimeError, match="voice_id"):
            eng.synthesize(text="任何文本", voice_id=None)

    def test_missing_profile_raises(self, engine_with_stub_profile):
        eng, _ = engine_with_stub_profile
        with pytest.raises(RuntimeError, match="不存在"):
            eng.synthesize(text="x", voice_id="ghost")

    def test_missing_file_raises(self, engine_with_stub_profile):
        eng, holder = engine_with_stub_profile
        holder["profile"] = _profile("no_such_dir/no_such_sample.wav")
        with pytest.raises(RuntimeError, match="缺失"):
            eng.synthesize(text="x", voice_id="v1")

    def test_master_native_sr_passthrough(self, engine_with_stub_profile, tmp_path):
        eng, holder = engine_with_stub_profile
        wav = _write_wav(tmp_path / "a.wav", eng.sample_rate, seconds=1.5)
        holder["profile"] = _profile(wav)
        audio = eng.synthesize(text="x", voice_id="v1")
        assert audio.dtype == np.float32 and audio.ndim == 1
        assert abs(len(audio) / eng.sample_rate - 1.5) < 0.01

    def test_resamples_other_sr(self, engine_with_stub_profile, tmp_path):
        eng, holder = engine_with_stub_profile
        wav = _write_wav(tmp_path / "b.wav", 44100, seconds=1.0)
        holder["profile"] = _profile(wav)
        audio = eng.synthesize(text="x", voice_id="v1")
        assert abs(len(audio) / eng.sample_rate - 1.0) < 0.02

    def test_stereo_downmixed(self, engine_with_stub_profile, tmp_path):
        eng, holder = engine_with_stub_profile
        wav = _write_wav(tmp_path / "c.wav", eng.sample_rate, stereo=True)
        holder["profile"] = _profile(wav)
        audio = eng.synthesize(text="x", voice_id="v1")
        assert audio.ndim == 1
        # 均值降混: L=0.5, R=0.25 -> peak 0.375
        assert abs(float(np.max(np.abs(audio))) - 0.375) < 0.02

    def test_silence_rejected_not_passthrough(self, engine_with_stub_profile, tmp_path):
        eng, holder = engine_with_stub_profile
        wav = tmp_path / "sil.wav"
        sf.write(str(wav), np.zeros(4800, dtype=np.float32), eng.sample_rate)
        holder["profile"] = _profile(wav)
        with pytest.raises(RuntimeError, match="静音"):
            eng.synthesize(text="x", voice_id="v1")

    def test_speed_pitch_emotion_ignored(self, engine_with_stub_profile, tmp_path):
        eng, holder = engine_with_stub_profile
        wav = _write_wav(tmp_path / "d.wav", eng.sample_rate)
        holder["profile"] = _profile(wav)
        a1 = eng.synthesize(text="x", voice_id="v1")
        a2 = eng.synthesize(text="y", voice_id="v1", speed=2.0, pitch=5.0,
                            emotion="angry")
        assert np.array_equal(a1, a2)  # 音效段语义：文本/参数都不参与


class TestCache:
    def test_hit_returns_same_object(self, engine_with_stub_profile, tmp_path):
        eng, holder = engine_with_stub_profile
        wav = _write_wav(tmp_path / "e.wav", eng.sample_rate)
        holder["profile"] = _profile(wav)
        a1 = eng.synthesize(text="x", voice_id="v1")
        a2 = eng.synthesize(text="x", voice_id="v1")
        assert a1 is a2

    def test_file_change_invalidates(self, engine_with_stub_profile, tmp_path):
        eng, holder = engine_with_stub_profile
        wav = _write_wav(tmp_path / "f.wav", eng.sample_rate, freq=220.0)
        holder["profile"] = _profile(wav)
        a1 = eng.synthesize(text="x", voice_id="v1")
        _write_wav(wav, eng.sample_rate, freq=440.0)  # size 同、mtime 变
        os.utime(wav, ns=(os.stat(wav).st_atime_ns, os.stat(wav).st_mtime_ns + 10**9))
        a2 = eng.synthesize(text="x", voice_id="v1")
        assert a1 is not a2

    def test_cache_evicted_at_max(self, engine_with_stub_profile, tmp_path, monkeypatch):
        import modules.tts_engine.engines.sample_playback as sp
        monkeypatch.setattr(sp, "CACHE_MAX_ENTRIES", 2)
        eng = sp.SamplePlaybackEngine()
        profiles = {}

        def fake_profile_for(self, voice_id):
            return profiles.get(voice_id)

        monkeypatch.setattr(sp.SamplePlaybackEngine, "_profile_for", fake_profile_for)
        for k in "abc":
            wav = _write_wav(tmp_path / f"{k}.wav", eng.sample_rate)
            eng2_prof = types.SimpleNamespace(id=k, name=k, model_path=str(wav),
                                              sample_audio_path=None)
            profiles[k] = eng2_prof
            eng.synthesize(text="x", voice_id=k)
        assert len(eng._cache) == 2  # 3 进 2 留


class TestRegistryWiring:
    def test_sample_registered_and_online(self):
        from modules.tts_engine.engine_registry import engine_registry
        inst = engine_registry.get("sample")
        assert inst is not None and inst.engine_name == "sample"
        assert getattr(inst, "online", False) is True

    def test_get_loaded_strict_ok(self):
        from modules.tts_engine.engine_registry import engine_registry
        eng = engine_registry.get_loaded("sample", strict=True)
        assert eng is not None and eng.is_loaded()

    def test_conditioning_routes_sample_row(self):
        """workflow 的 voice_engine_map 靠 resolve_profile_conditioning 读行 engine。"""
        from modules.voice_manager.conditioning import resolve_profile_conditioning
        prof = types.SimpleNamespace(id="v1", name="n", source="sample",
                                     category="animal", engine="sample",
                                     language=None, gender=None, age_group=None,
                                     style_tags=None, meta={}, description=None,
                                     model_path="x", sample_audio_path=None,
                                     reference_audio_path=None)
        cond = resolve_profile_conditioning(prof, defaults={"engine": "voxcpm"})
        assert cond["engine"] == "sample"
