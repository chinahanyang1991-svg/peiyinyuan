"""
Kokoro-82M TTS 引擎适配器
轻量级 TTS 引擎，适合快速预览和资源受限环境
"""

from typing import Iterator
import numpy as np

from modules.tts_engine.engines.base_tts import BaseTTSEngine


class KokoroAdapter(BaseTTSEngine):
    """Kokoro-82M TTS 引擎适配器 - 轻量备选引擎"""

    engine_name = "kokoro"
    engine_display = "Kokoro-82M"
    supported_languages = [
        "en", "zh", "ja", "fr", "es", "it", "pt", "ko",
    ]
    supports_cloning = False
    supports_design = False
    supports_streaming = False
    supports_emotion = False
    sample_rate = 24000
    model_size = "82M"
    # PY-304: 真实权重适配器 —— 就绪与否以 _model 为准，registry 加载后校验
    requires_model_object = True

    # Kokoro 内置音色
    BUILTIN_VOICES = {
        "af_heart": "American Female - Heart (❤️)",
        "af_bella": "American Female - Bella (🔥)",
        "af_nicole": "American Female - Nicole (🎧)",
        "af_sky": "American Female - Sky",
        "am_adam": "American Male - Adam",
        "am_michael": "American Male - Michael",
        "bf_emma": "British Female - Emma",
        "bm_george": "British Male - George",
        "bm_lewis": "British Male - Lewis",
        "jf_alpha": "Japanese Female - Alpha",
        "jm_alpha": "Japanese Male - Alpha",
        "zf_xiaobei": "Chinese Female - 小北",
        "zf_xiaoni": "Chinese Female - 小妮",
        "zf_xiaoxiao": "Chinese Female - 小晓",
        "zf_xiaoyi": "Chinese Female - 小伊",
        "zm_yunjian": "Chinese Male - 云健",
        "zm_yunyang": "Chinese Male - 云扬",
    }

    def __init__(self, model_path: str = None):
        super().__init__()
        self.model_path = model_path

    @classmethod
    def unavailable_reason(cls) -> str:
        import importlib.util

        missing = [m for m in ("kokoro", "misaki", "phonemizer")
                   if importlib.util.find_spec(m) is None]
        if missing:
            return f"依赖库未安装: {', '.join(missing)}"
        return ""

    def load_model(self, model_path: str = None) -> None:
        """加载 Kokoro 模型

        PY-304: 依赖缺失/pipeline 构造失败一律带原因上抛，不再
        `_loaded=True` + `_model=None`（半死引擎 → 合成端落 np.zeros 静音替身）。
        """
        self._model = None
        self._loaded = False
        path = model_path or self.model_path or "hexgrad/Kokoro-82M"
        try:
            from kokoro import KPipeline
        except ImportError as e:
            raise RuntimeError(
                f"kokoro 库未安装，无法加载 Kokoro-82M: {e} (pip install kokoro)") from e
        try:
            self._model = KPipeline(lang_code='a')  # American English
            if self._model is None:
                raise RuntimeError(f"KPipeline 构造返回 None: {path}")
            self._loaded = True
            print(f"[Kokoro] 模型加载完成: {path}")
        except Exception as e:
            self._model = None
            self._loaded = False
            print(f"[Kokoro] 模型加载失败（不再回落静音替身，PY-304）: {e}")
            raise

    def synthesize(
        self,
        text: str,
        voice_id: str = "af_heart",
        speed: float = 1.0,
        pitch: float = 0.0,
        emotion: str = "neutral",
        seed=None,
    ) -> np.ndarray:
        """文本→语音合成

        US-C4: 本引擎 `supports_seed = False` —— 收到显式 seed 一律**显式拒绝**
        （静默收下会让上层质量门把"没换 seed 的重复合成"误判成"换 seed 重试过"）。
        """
        self._reject_seed(seed)
        if not self._loaded:
            self.load_model()

        # PY-304: 静音替身（_mock_synthesize / np.zeros）已删除 —— 一律上抛
        if self._model is None:
            raise RuntimeError("Kokoro-82M 模型未加载，无法合成")

        try:
            # 根据 voice_id 确定语言
            lang = self._voice_to_language(voice_id)
            pipeline = self._get_pipeline(lang)

            generator = pipeline(text, voice=voice_id, speed=speed)
            audio_chunks = []
            for _, _, audio in generator:
                audio_chunks.append(audio)

            if not audio_chunks:
                raise RuntimeError(
                    f"Kokoro pipeline 未产出任何音频块 (voice={voice_id}, lang={lang})")
            return np.concatenate(audio_chunks)
        except Exception as e:
            print(f"[Kokoro] 合成失败: {e}")
            raise RuntimeError(f"Kokoro-82M 合成失败: {type(e).__name__}: {e}") from e

    def list_voices(self) -> list:
        """列出内置音色"""
        return [
            {"id": vid, "name": vname, "engine": "kokoro"}
            for vid, vname in self.BUILTIN_VOICES.items()
        ]

    def _voice_to_language(self, voice_id: str) -> str:
        """根据音色ID推测语言"""
        lang_map = {
            'a': 'a',  # American English
            'b': 'b',  # British English
            'j': 'j',  # Japanese
            'z': 'z',  # Chinese
            'f': 'f',  # French
            'e': 'e',  # Spanish
            'i': 'i',  # Italian
            'p': 'p',  # Portuguese
            'k': 'k',  # Korean
        }
        prefix = voice_id[0].lower() if voice_id else 'a'
        return lang_map.get(prefix, 'a')

    def _get_pipeline(self, lang: str):
        """获取指定语言的 pipeline（PY-304: 取不到即上抛，不再返回 None 让上层落静音）"""
        try:
            from kokoro import KPipeline
            return KPipeline(lang_code=lang)
        except Exception as e:
            raise RuntimeError(
                f"Kokoro pipeline 不可用 (lang={lang}): {type(e).__name__}: {e}") from e