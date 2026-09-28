"""
IndexTTS-2 TTS 引擎适配器
集成 IndexTTS-2 高精度零样本语音克隆 + 情感可控合成
"""

import os
import tempfile
import uuid
from pathlib import Path
from typing import Iterator, Optional
import numpy as np

from modules.tts_engine.engines.base_tts import BaseTTSEngine

# IndexTTS-2 情感向量映射（8维）
# Order: [happy, angry, sad, afraid, disgusted, melancholic, surprised, calm]
EMOTION_VECTORS = {
    "neutral":      [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.5],
    "happy":        [0.8, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
    "sad":          [0.0, 0.0, 0.8, 0.0, 0.0, 0.3, 0.0, 0.0],
    "angry":        [0.0, 0.8, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
    "surprised":    [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.8, 0.0],
    "fearful":      [0.0, 0.0, 0.0, 0.8, 0.0, 0.0, 0.3, 0.0],
    "disgusted":    [0.0, 0.0, 0.0, 0.0, 0.8, 0.0, 0.0, 0.0],
    "calm":         [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.9],
    "excited":      [0.9, 0.0, 0.0, 0.0, 0.0, 0.0, 0.3, 0.0],
    "gentle":       [0.2, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.7],
}

# 默认配置路径（相对于模型目录）
DEFAULT_CFG_PATH = "checkpoints/config.yaml"
DEFAULT_MODEL_DIR = "checkpoints"
# HuggingFace 模型 ID（自动下载）
HF_MODEL_ID = "index-team/IndexTTS2"


class IndexTTSAdapter(BaseTTSEngine):
    """IndexTTS-2 TTS 引擎适配器"""

    engine_name = "indextts"
    engine_display = "IndexTTS-2"
    supported_languages = ["zh", "en"]
    supports_cloning = True
    supports_design = False  # IndexTTS 使用参考音频克隆，不支持文本描述设计
    supports_streaming = False  # IndexTTS 输出完整 WAV 文件
    supports_emotion = True  # 支持 8 维情感向量控制
    sample_rate = 24000  # IndexTTS 输出 24kHz
    model_size = "~1B"
    # PY-304: 真实权重适配器 —— 就绪与否以 _model 为准，registry 加载后校验
    requires_model_object = True

    def __init__(self, model_dir: str = None, cfg_path: str = None, device: str = "auto"):
        super().__init__()
        self.model_dir = model_dir or DEFAULT_MODEL_DIR
        self.cfg_path = cfg_path or DEFAULT_CFG_PATH
        self.device = device
        self._use_fp16 = device != "cpu"
        self._use_cuda_kernel = device != "cpu"

        # 参考音频缓存 {voice_id: audio_path}
        self._cloned_voices = {}
        # 内置说话人参考音频
        self._builtin_speakers = {
            "indextts_default_cn": {
                "name": "IndexTTS 默认中文",
                "language": "zh",
                "gender": "female",
                "audio": None,  # 首次使用时从内置资源加载
            },
            "indextts_default_en": {
                "name": "IndexTTS Default English",
                "language": "en",
                "gender": "female",
                "audio": None,
            },
        }

    @classmethod
    def unavailable_reason(cls) -> str:
        import importlib.util

        if importlib.util.find_spec("indextts") is None:
            return "indextts 库未安装"
        return ""

    def load_model(self, model_path: str = None) -> None:
        """加载 IndexTTS-2 模型

        PY-304: 权重缺失/依赖缺失/加载异常一律带原因上抛，不再
        `_model=None; _loaded=True`（半死引擎 → 合成端落 np.zeros 静音替身）。
        """
        if self._loaded and self._model is not None:
            return

        self._model = None
        self._loaded = False
        cfg = model_path or self.cfg_path
        model_dir = self.model_dir
        cfg_path_obj = Path(cfg)
        model_dir_obj = Path(model_dir)

        if not cfg_path_obj.exists() or not model_dir_obj.exists():
            raise FileNotFoundError(
                f"IndexTTS-2 本地权重缺失 (cfg={cfg_path_obj}, model_dir={model_dir_obj}); "
                f"请先下载模型: git lfs clone https://huggingface.co/{HF_MODEL_ID}")
        try:
            from indextts.infer_v2 import IndexTTS2
        except ImportError as e:
            raise RuntimeError(
                f"indextts 库未安装: {e} (pip install indextts)") from e

        try:
            self._model = IndexTTS2(
                cfg_path=str(cfg_path_obj),
                model_dir=str(model_dir_obj),
                use_fp16=self._use_fp16,
                use_cuda_kernel=self._use_cuda_kernel,
                use_deepspeed=False,
            )
            if self._model is None:
                raise RuntimeError("IndexTTS2 构造返回 None")
            self._loaded = True
            print(f"[IndexTTS] 模型加载完成，设备: {self.device}")
        except Exception as e:
            self._model = None
            self._loaded = False
            print(f"[IndexTTS] 模型加载失败（不再回落静音替身，PY-304）: {e}")
            raise

    def synthesize(
        self,
        text: str,
        voice_id: str = None,
        speed: float = 1.0,
        pitch: float = 0.0,
        emotion: str = "neutral",
        seed=None,
    ) -> np.ndarray:
        """文本→语音合成，支持情感控制

        P0-2 裁定（为何本引擎**没有**接克隆 prompt 缓存）：对外只有
        ``infer(spk_audio_prompt=<路径>, ...)`` —— 说话人编码在上游 ``indextts``
        包内部完成，与我们这一层的调用不分离，适配器手里没有任何可复用的中间产物
        可缓存。宁可不接，也不造一个假接口；参考实现见
        ``modules/tts_engine/clone_prompt_cache.py`` + ``voxcpm_adapter``。

        US-C4: 上游 `infer(...)` 没有 seed 形参 ⇒ 本引擎 `supports_seed=False`，
        收到显式 seed 一律**显式拒绝**（见下方 `_reject_seed`），绝不允许"收下不用"。
        """
        self._reject_seed(seed)
        if not self._loaded:
            self.load_model()

        # PY-304: 静音替身（_mock_synthesize / np.zeros）已删除 —— 一律上抛
        if self._model is None:
            raise RuntimeError(
                f"IndexTTS-2 模型未加载，无法合成（权重目录 {self.model_dir} 缺失或加载失败）")

        # 确定参考音频路径
        ref_audio = self._get_reference_audio(voice_id)
        if ref_audio is None:
            raise FileNotFoundError(
                f"IndexTTS-2 缺少音色 {voice_id!r} 的参考音频，无法合成；"
                f"请先克隆音色或放置 ref_cn.wav / ref_en.wav")

        # 准备情感参数
        emo_vector = EMOTION_VECTORS.get(emotion, EMOTION_VECTORS["neutral"])
        emo_alpha = min(1.0, max(0.0, (speed - 0.5) * 0.3 + 0.65))  # 情感强度随语速微调

        try:
            # 写入临时文件
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                output_path = tmp.name

            self._model.infer(
                spk_audio_prompt=ref_audio,
                text=text,
                output_path=output_path,
                emo_vector=emo_vector,
                emo_alpha=emo_alpha,
                use_random=False,
                verbose=False,
            )

            # 读取生成的音频
            import soundfile as sf
            audio, sr = sf.read(output_path)
            os.unlink(output_path)

            # 如果 sr != sample_rate，重采样
            if sr != self.sample_rate:
                from scipy import signal
                audio = signal.resample(
                    audio, int(len(audio) * self.sample_rate / sr)
                )

            return np.array(audio, dtype=np.float32)

        except Exception as e:
            # PY-304/PY-117: 合成失败不得用静音数组顶替 —— 上抛让任务标 failed
            print(f"[IndexTTS] 合成失败: {e}")
            raise RuntimeError(f"IndexTTS-2 合成失败: {type(e).__name__}: {e}") from e

    def synthesize_batch(
        self,
        sentences: list,
        voice_id: str = None,
        speed: float = 1.0,
        pitch: float = 0.0,
        emotion: str = "neutral",
        seed=None,
    ) -> list:
        """
        批量合成（IndexTTS 自动分段，合并多句可提高效率）

        返回: [(sentence, audio_array), ...]
        """
        if not self._loaded:
            self.load_model()

        # PY-304: 静音替身已删除 —— 引擎/参考音频不可用一律上抛，不返回全零段
        if self._model is None:
            raise RuntimeError(
                f"IndexTTS-2 模型未加载，无法批量合成（权重目录 {self.model_dir}）")

        ref_audio = self._get_reference_audio(voice_id)
        if ref_audio is None:
            raise FileNotFoundError(
                f"IndexTTS-2 缺少音色 {voice_id!r} 的参考音频，无法批量合成")

        # IndexTTS 自动处理长文本，将多句合并为一次调用
        combined_text = " ".join(sentences) if sentences else ""
        if not combined_text:
            return []

        audio = self.synthesize(combined_text, voice_id, speed, pitch, emotion,
                                seed=seed)

        # 按句切分音频（简易方式：等分）
        if len(sentences) == 1:
            return [(sentences[0], audio)]

        # 多句时按字符比例切分
        total_chars = sum(len(s) for s in sentences)
        results = []
        start = 0
        for sent in sentences:
            ratio = len(sent) / total_chars if total_chars > 0 else 1 / len(sentences)
            chunk_len = int(len(audio) * ratio)
            end = min(start + chunk_len, len(audio))
            results.append((sent, audio[start:end]))
            start = end

        return results

    def synthesize_stream(
        self, text: str, voice_id: str = None, seed=None, **kwargs
    ) -> Iterator[np.ndarray]:
        """流式合成（IndexTTS 不原生支持流式，整段合成后分段输出）"""
        audio = self.synthesize(text, voice_id, seed=seed, **kwargs)

        # 按 0.5 秒一段输出
        chunk_size = int(self.sample_rate * 0.5)
        for i in range(0, len(audio), chunk_size):
            yield audio[i:i + chunk_size]

    def clone_voice(self, audio_path: str, transcript: str = None) -> str:
        """
        语音克隆

        Args:
            audio_path: 参考音频路径（建议 3-10 秒清晰语音）
            transcript: 参考音频的文本转写（可选，提供可提高克隆质量）

        Returns:
            voice_id: 克隆音色 ID
        """
        if not os.path.exists(audio_path):
            raise FileNotFoundError(f"参考音频不存在: {audio_path}")

        voice_id = f"cloned_{uuid.uuid4().hex[:8]}"
        self._cloned_voices[voice_id] = audio_path
        return voice_id

    def list_voices(self) -> list:
        """列出可用音色"""
        voices = [
            {
                "id": "indextts_default_cn",
                "name": "IndexTTS 默认中文",
                "language": "zh",
                "gender": "female",
                "engine": "indextts",
            },
            {
                "id": "indextts_default_en",
                "name": "IndexTTS Default English",
                "language": "en",
                "gender": "female",
                "engine": "indextts",
            },
        ]

        # 添加克隆的音色
        for vid, apath in self._cloned_voices.items():
            voices.append({
                "id": vid,
                "name": f"克隆音色-{vid[-4:]}",
                "source": "cloned",
                "language": "zh",
                "engine": "indextts",
                "reference_audio": apath,
            })

        return voices

    def _get_reference_audio(self, voice_id: str = None) -> Optional[str]:
        """获取参考音频路径"""
        if voice_id is None or voice_id == "indextts_default_cn":
            # 使用内置默认中文参考音频
            return self._get_builtin_audio("indextts_default_cn")

        if voice_id == "indextts_default_en":
            return self._get_builtin_audio("indextts_default_en")

        if voice_id in self._cloned_voices:
            return self._cloned_voices[voice_id]

        return self._get_builtin_audio("indextts_default_cn")

    def _get_builtin_audio(self, speaker_id: str) -> Optional[str]:
        """获取内置说话人参考音频路径"""
        if speaker_id not in self._builtin_speakers:
            return None

        speaker = self._builtin_speakers[speaker_id]

        # 如果已缓存路径
        if speaker["audio"] and os.path.exists(speaker["audio"]):
            return speaker["audio"]

        # 尝试从内置资源加载
        resource_dir = Path(__file__).parent.parent.parent.parent / "data" / "voices" / "indextts_ref"
        resource_dir.mkdir(parents=True, exist_ok=True)

        if speaker_id == "indextts_default_cn":
            expected = resource_dir / "ref_cn.wav"
            if expected.exists():
                speaker["audio"] = str(expected)
                return str(expected)
        elif speaker_id == "indextts_default_en":
            expected = resource_dir / "ref_en.wav"
            if expected.exists():
                speaker["audio"] = str(expected)
                return str(expected)

        # 无内置参考音频时，提示用户提供
        print(f"[IndexTTS] 请提供参考音频或进行语音克隆: 放置 .wav 文件到 {resource_dir}")
        return None