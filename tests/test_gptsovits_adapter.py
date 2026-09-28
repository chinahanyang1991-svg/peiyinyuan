"""GPT-SoVITS HTTP 适配器的单元验证（不连真服务端，requests 打桩）。"""
import io
import os
import sys
import types

import numpy as np
import pytest
import soundfile as sf

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import modules.tts_engine.engines.gptsovits_adapter as ga


def _wav_bytes(sr=32000, seconds=1.0, level=0.12):
    t = np.arange(int(sr * seconds), dtype=np.float64) / sr
    y = (level * np.sin(2 * np.pi * 300 * t)).astype(np.float32)
    buf = io.BytesIO()
    sf.write(buf, y, sr, subtype="PCM_16", format="WAV")
    return buf.getvalue()


class _Resp:
    def __init__(self, status=200, content=b"", text=""):
        self.status_code, self.content, self.text = status, content, text


@pytest.fixture
def patched(monkeypatch, tmp_path):
    import requests
    ref = tmp_path / "ref.wav"
    sf.write(str(ref), (0.3 * np.sin(np.arange(32000) / 50)).astype(np.float32),
             32000)
    prof = types.SimpleNamespace(name="克隆测试", language="zh",
                                 reference_audio_path=str(ref),
                                 model_path=None,
                                 meta={"transcript": "参考转写"})
    calls = []

    class FakeRequests:
        @staticmethod
        def get(url, timeout=None):
            calls.append(("GET", url, timeout))
            return _Resp(200)

        @staticmethod
        def post(url, json=None, timeout=None):
            calls.append(("POST", url, json, timeout))
            return _Resp(200, _wav_bytes())

    monkeypatch.setattr(requests, "get", FakeRequests.get)
    monkeypatch.setattr(requests, "post", FakeRequests.post)
    monkeypatch.setattr(ga.GPTSoVITSAdapter, "_ref_for_voice",
                        lambda self, vid: (str(ref), "参考转写", "zh"))
    return FakeRequests, calls, ref


class TestAdapter:
    def test_contract_registration(self):
        from modules.tts_engine.engine_registry import engine_registry
        inst = engine_registry.get("gptsovits")
        assert inst is not None and getattr(inst, "online", False)
        assert inst.supports_cloning and inst.sample_rate == 32000

    def test_health_probe_in_load(self, patched):
        _, calls, _ = patched
        eng = ga.GPTSoVITSAdapter(base_url="http://x:9880")
        eng.load_model()
        assert eng.is_loaded()
        assert calls[0][:2] == ("GET", "http://x:9880/docs")

    def test_payload_follows_spike_contract(self, patched):
        _, calls, ref = patched
        eng = ga.GPTSoVITSAdapter()
        eng.load_model()
        audio = eng.synthesize("测试文本", voice_id="v1", speed=1.2)
        post = [c for c in calls if c[0] == "POST"][0]
        payload = post[2]
        assert post[3] >= 600  # 超时契约
        for k in ("text", "text_lang", "ref_audio_path", "prompt_text",
                  "prompt_lang", "media_type", "streaming_mode", "seed",
                  "speed_factor", "batch_size", "text_split_method"):
            assert k in payload
        assert payload["streaming_mode"] == 0
        assert payload["speed_factor"] == 1.2
        assert audio.dtype == np.float32 and audio.ndim == 1

    def test_low_peak_normalized(self, patched):
        eng = ga.GPTSoVITSAdapter()
        eng.load_model()
        audio = eng.synthesize("x", voice_id="v1")  # stub 返回 peak 0.12
        assert 0.6 < float(np.max(np.abs(audio))) <= 0.85

    def test_pitch_rejected_not_silent(self, patched):
        eng = ga.GPTSoVITSAdapter()
        eng.load_model()
        with pytest.raises(ValueError, match="pitch"):
            eng.synthesize("x", voice_id="v1", pitch=3.0)

    def test_400_raises_with_body(self, patched, monkeypatch):
        import requests
        monkeypatch.setattr(requests, "post",
                            lambda url, json=None, timeout=None: _Resp(400, b"", '{"message":"ref_audio_path is required"}'))
        eng = ga.GPTSoVITSAdapter()
        eng.load_model()
        with pytest.raises(RuntimeError, match="400"):
            eng.synthesize("x", voice_id="v1")

    def test_silence_rejected(self, patched, monkeypatch):
        import requests
        monkeypatch.setattr(requests, "post",
                            lambda url, json=None, timeout=None: _Resp(
                                200, _wav_bytes(level=0.0001)))
        eng = ga.GPTSoVITSAdapter()
        eng.load_model()
        with pytest.raises(RuntimeError, match="静音"):
            eng.synthesize("x", voice_id="v1")

    def test_dead_server_fails_fast(self, monkeypatch):
        import requests

        def boom(url, timeout=None):
            raise requests.exceptions.ConnectionError("refused")

        monkeypatch.setattr(requests, "get", boom)
        eng = ga.GPTSoVITSAdapter(base_url="http://127.0.0.1:1")
        with pytest.raises(RuntimeError, match="不可达"):
            eng.load_model()

    def test_ref_lookup_requires_file(self, monkeypatch, tmp_path):
        from modules.voice_manager import voice_store as vs_mod
        monkeypatch.setattr(vs_mod.voice_store, "get",
                            lambda vid: types.SimpleNamespace(
                                name="n", language="zh",
                                reference_audio_path=str(tmp_path / "ghost.wav"),
                                model_path=None, meta={}))
        eng = ga.GPTSoVITSAdapter()
        with pytest.raises(RuntimeError, match="参考音频缺失"):
            eng._ref_for_voice("v1")
