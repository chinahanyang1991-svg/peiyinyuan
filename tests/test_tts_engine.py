"""
TTS 引擎适配层单元测试
"""
import pytest
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from modules.tts_engine.engine_registry import EngineRegistry
from modules.tts_engine.engines.base_tts import BaseTTSEngine


class TestEngineRegistry:
    """引擎注册中心测试"""

    def setup_method(self):
        self.registry = EngineRegistry()

    def test_get_default_engine(self):
        """应获取默认引擎"""
        engine = self.registry.get("voxcpm")
        assert engine is not None
        assert engine.engine_name == "voxcpm"

    def test_get_kokoro_engine(self):
        """应获取Kokoro引擎"""
        engine = self.registry.get("kokoro")
        assert engine is not None
        assert engine.engine_name == "kokoro"

    def test_list_engines(self):
        """应列出所有引擎"""
        engines = self.registry.list_engines()
        assert len(engines) >= 2
        names = [e["name"] for e in engines]
        assert "voxcpm" in names
        assert "kokoro" in names

    def test_engine_properties(self):
        """引擎应包含必要的属性"""
        engine = self.registry.get("voxcpm")
        assert isinstance(engine, BaseTTSEngine)
        assert hasattr(engine, "engine_name")
        assert hasattr(engine, "supported_languages")
        assert hasattr(engine, "sample_rate")

    def test_register_custom_engine(self):
        """应能注册自定义引擎"""
        original_count = len(self.registry.list_engines())

        class MockEngine(BaseTTSEngine):
            engine_name = "mock"
            supported_languages = ["zh"]
            sample_rate = 22050

            def load_model(self, model_path=None):
                self._loaded = True

            def synthesize(self, text, voice_id=None, speed=1.0, pitch=0.0, emotion="neutral"):
                import numpy as np
                return np.zeros(100, dtype=np.float32)

        self.registry.register("mock", MockEngine())
        engines = self.registry.list_engines()
        assert any(e["name"] == "mock" for e in engines)

    def test_get_mosstts_engine(self):
        """应获取 MOSS-TTS 引擎"""
        engine = self.registry.get("mosstts")
        assert engine is not None
        assert engine.engine_name == "mosstts"

    def test_mosstts_properties(self):
        """MOSS-TTS 引擎属性验证"""
        engine = self.registry.get("mosstts")
        assert engine.supports_cloning == True
        assert engine.supports_emotion == True
        assert engine.sample_rate == 24000
        assert "zh" in engine.supported_languages
        assert "en" in engine.supported_languages


class TestBaseTTSEngine:
    """基础TTS引擎接口测试"""

    def test_abstract_methods(self):
        """抽象方法应抛出NotImplementedError"""
        # Can't instantiate abstract class directly
        pass

    def test_engine_interface_compliance(self):
        """所有注册引擎应符合接口规范"""
        registry = EngineRegistry()
        for engine_info in registry.list_engines():
            engine = registry.get(engine_info["name"])
            assert hasattr(engine, "load_model")
            assert hasattr(engine, "synthesize")
            assert hasattr(engine, "is_loaded")

    def test_engine_display_name(self):
        """引擎应有显示名称"""
        registry = EngineRegistry()
        voxcpm = registry.get("voxcpm")
        assert hasattr(voxcpm, "engine_display")
        assert len(voxcpm.engine_display) > 0


class TestAudioPostProcessor:
    """音频后处理器测试"""

    def setup_method(self):
        from modules.tts_engine.audio_postprocess import AudioPostProcessor
        self.processor = AudioPostProcessor(sample_rate=48000)

    def test_normalize_volume(self):
        """音量归一化应在合理范围"""
        import numpy as np
        audio = np.random.randn(1000).astype(np.float32) * 0.1
        result = self.processor.normalize_volume(audio)
        assert len(result) == len(audio)
        assert np.max(np.abs(result)) <= 1.0

    def test_remove_silence(self):
        """应去除首尾静音"""
        import numpy as np
        # Create audio with silence at both ends
        silence = np.zeros(500, dtype=np.float32)
        signal = np.random.randn(1000).astype(np.float32) * 0.1
        audio = np.concatenate([silence, signal, silence])
        result = self.processor.remove_silence(audio)
        assert len(result) < len(audio)
        assert len(result) > 0

    def test_apply_fade(self):
        """应添加淡入淡出效果"""
        import numpy as np
        audio = np.ones(1000, dtype=np.float32)
        result = self.processor.apply_fade(audio, fade_in_ms=50, fade_out_ms=100)
        assert len(result) == len(audio)
        # Check fade in
        assert result[0] < 0.5
        # Check fade out
        assert result[-1] < 0.5
        # Check middle is unchanged
        assert result[500] == 1.0

    def test_concat_audios(self):
        """应正确拼接多个音频"""
        import numpy as np
        audio1 = np.ones(100, dtype=np.float32)
        audio2 = np.ones(200, dtype=np.float32)
        result = self.processor.concat_audios([audio1, audio2], gap_ms=100)
        assert len(result) > 300  # 100 + 200 + gap

    def test_empty_inputs(self):
        """空输入应返回空数组"""
        import numpy as np
        result = self.processor.concat_audios([])
        assert len(result) == 0
        assert isinstance(result, np.ndarray)

    def test_process_chapter(self):
        """完整章节处理不应崩溃"""
        import numpy as np
        sentences = [np.random.randn(500).astype(np.float32) for _ in range(3)]
        result = self.processor.process_chapter(sentences, gap_ms=200)
        assert len(result) > 0


class TestMOSSTTSAdapter:
    """MOSS-TTS 适配器单元测试"""

    def setup_method(self):
        from modules.tts_engine.engines.moss_tts_adapter import MOSSTTSAdapter
        self.adapter = MOSSTTSAdapter()

    def test_engine_name(self):
        assert self.adapter.engine_name == "mosstts"
        assert self.adapter.engine_display == "MOSS-TTS"

    def test_mock_synthesize_removed(self):
        """PY-304: np.zeros 静音替身必须已删除（引擎全挂不得伪装成成品）"""
        assert not hasattr(self.adapter, "_mock_synthesize")
        src = open(os.path.join(os.path.dirname(__file__), "..",
                                "modules", "tts_engine", "engines",
                                "moss_tts_adapter.py"), encoding="utf-8").read()
        assert "np.zeros(" not in src

    def test_synthesize_without_model_raises(self):
        """PY-304: 半死引擎（_loaded=True 而 _model=None）合成必须上抛，不得返回静音"""
        import pytest
        self.adapter._loaded = True      # 复刻旧缺陷形态，避免真加载（不走网络）
        self.adapter._model = None
        with pytest.raises(RuntimeError) as ei:
            self.adapter.synthesize("测试文本")
        assert "未加载" in str(ei.value)
        assert self.adapter.is_loaded() is False   # requires_model_object 生效

    def test_list_voices(self):
        """应返回内置音色列表"""
        voices = self.adapter.list_voices()
        assert len(voices) >= 2
        ids = [v["id"] for v in voices]
        assert "mosstts_default_cn" in ids
        assert "mosstts_default_en" in ids

    def test_clone_voice_file_not_found(self):
        """克隆不存在的文件应抛异常"""
        import pytest
        with pytest.raises(FileNotFoundError):
            self.adapter.clone_voice("/nonexistent/audio.wav")

    def test_clone_voice(self):
        """语音克隆应返回 voice_id"""
        import tempfile
        import numpy as np
        import soundfile as sf

        # 创建临时 WAV 文件
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            sf.write(tmp.name, np.zeros(8000, dtype=np.float32), 16000)
            tmp_path = tmp.name

        try:
            voice_id = self.adapter.clone_voice(tmp_path)
            assert voice_id.startswith("cloned_")
            # 克隆后音色列表增加
            voices = self.adapter.list_voices()
            assert any(v["id"] == voice_id for v in voices)
        finally:
            os.unlink(tmp_path)

    def test_emotion_prompts(self):
        """情感 Prompt 映射应完整"""
        from modules.tts_engine.engines.moss_tts_adapter import EMOTION_PROMPTS
        expected_emotions = ["neutral", "happy", "sad", "angry", "surprised",
                             "fearful", "calm", "excited", "gentle"]
        for emo in expected_emotions:
            assert emo in EMOTION_PROMPTS
            assert len(EMOTION_PROMPTS[emo]) > 0