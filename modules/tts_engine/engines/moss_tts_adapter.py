"""
MOSS-TTS TTS 引擎适配器
集成 OpenMOSS/MOSS-TTS-Local-Transformer 语音合成 + 语音克隆
"""

import os
import tempfile
import uuid
from pathlib import Path
from typing import Iterator, Optional
import numpy as np

from modules.tts_engine.engines.base_tts import BaseTTSEngine

# MOSS-TTS HuggingFace 模型 ID
HF_MODEL_ID = "OpenMOSS-Team/MOSS-TTS-Local-Transformer"

# 情感→Prompt 映射（MOSS-TTS 通过 reference_audio + text_prompt 控制风格）
EMOTION_PROMPTS = {
    "neutral":   "用自然平和的语气朗读。",
    "happy":     "用欢快愉悦的语气朗读，声音充满喜悦。",
    "sad":       "用低沉忧伤的语气朗读，声音带着悲伤。",
    "angry":     "用愤怒激昂的语气朗读，声音充满力量。",
    "surprised": "用惊讶的语气朗读，声音充满意外之情。",
    "fearful":   "用紧张害怕的语气朗读，声音微微颤抖。",
    "calm":      "用温柔平静的语气朗读，声音轻柔舒缓。",
    "excited":   "用兴奋激动的语气朗读，声音高昂饱满。",
    "gentle":    "用轻声细语的语气朗读，声音柔和温暖。",
}


class MOSSTTSAdapter(BaseTTSEngine):
    """MOSS-TTS TTS 引擎适配器"""

    engine_name = "mosstts"
    engine_display = "MOSS-TTS"
    supported_languages = ["zh", "en"]
    supports_cloning = True
    supports_design = False       # MOSS-TTS 使用参考音频，不支持描述设计
    supports_streaming = False    # 完整合成后返回
    supports_emotion = True       # 通过 prompt 控制情感风格
    sample_rate = 24000
    model_size = "~1B"
    # PY-304: 真实权重适配器 —— 就绪与否以 _model 为准，registry 加载后校验
    requires_model_object = True

    @classmethod
    def unavailable_reason(cls) -> str:
        """依赖缺失时返回人话原因；可用返回 ""。

        与 kokoro/indextts 同族问题：本适配器的 import 全在 `load_model`/`synthesize`
        里，registry 旧口径「能实例化=可用」会把它报成可用，用户点下去必失败。
        只查 import 可得性，不加载权重、不碰网络。
        """
        import importlib.util

        missing = [m for m in ("torch", "transformers", "soundfile", "librosa")
                   if importlib.util.find_spec(m) is None]
        if missing:
            return f"依赖库未安装: {', '.join(missing)}"
        return ""

    def __init__(self, model_path: str = None, device: str = "auto"):
        super().__init__()
        self.model_path = model_path or HF_MODEL_ID
        self.device = device
        self._processor = None
        self._generation_config = None

        # 参考音频缓存 {voice_id: audio_path}
        self._cloned_voices = {}
        # 内置说话人
        self._builtin_speakers = {
            "mosstts_default_cn": {
                "name": "MOSS-TTS 默认中文",
                "language": "zh",
                "gender": "female",
                "audio": None,
            },
            "mosstts_default_en": {
                "name": "MOSS-TTS Default English",
                "language": "en",
                "gender": "female",
                "audio": None,
            },
        }

    def load_model(self, model_path: str = None) -> None:
        """加载 MOSS-TTS 模型

        PY-304: 依赖缺失/权重缺失/加载异常一律带原因上抛，不再
        `_model=None; _loaded=True`（半死引擎 → 合成端落 np.zeros 静音替身）。
        """
        if self._loaded and self._model is not None:
            return

        self._model = None
        self._loaded = False
        path = model_path or self.model_path

        # 本地路径形态时先校验，避免把"目录不存在"混进网络异常里
        if os.sep in str(path) or str(path).startswith(".") or os.path.isdir(str(path)):
            if not os.path.isdir(str(path)):
                raise FileNotFoundError(f"MOSS-TTS 本地权重目录不存在: {path}")

        try:
            import torch
            from transformers import (
                AutoModel,
                AutoProcessor,
                GenerationConfig,
            )
        except ImportError as e:
            raise RuntimeError(
                f"torch/transformers 未安装，无法加载 MOSS-TTS: {e} "
                "(pip install torch transformers)") from e

        try:
            # 设备自动选择
            if self.device == "auto":
                self.device = "cuda" if torch.cuda.is_available() else "cpu"

            print(f"[MOSS-TTS] 正在加载模型: {path}")
            print(f"[MOSS-TTS] 设备: {self.device}")

            self._processor = AutoProcessor.from_pretrained(
                path, trust_remote_code=True
            )
            # 将 audio_tokenizer 移到目标设备
            if hasattr(self._processor, "audio_tokenizer"):
                self._processor.audio_tokenizer = self._processor.audio_tokenizer.to(
                    self.device
                )

            self._model = AutoModel.from_pretrained(
                path,
                trust_remote_code=True,
                torch_dtype=torch.float16 if self.device != "cpu" else torch.float32,
            ).to(self.device)

            self._generation_config = GenerationConfig.from_pretrained(path)

            self._model.eval()
            self._loaded = True
            print(f"[MOSS-TTS] 模型加载完成，设备: {self.device}")
        except Exception as e:
            self._model = None
            self._processor = None
            self._loaded = False
            print(f"[MOSS-TTS] 模型加载失败（不再回落静音替身，PY-304）: {e}")
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
        """文本→语音合成

        P0-2 裁定（为何本引擎**没有**接克隆 prompt 缓存）：``processor(text=…, audio=…)``
        把参考音频与目标文本**一起**编码成模型 inputs，没有可分离的"参考音频编码产物"
        可复用；唯一可分离的是 ``librosa.load`` 的解码结果，而它是几十毫秒量级，
        相对秒级的 generate 不值得换来一条 8 × 数 MB 的缓存。
        有可分离编码产物的引擎接法见 ``modules/tts_engine/clone_prompt_cache.py``
        + ``voxcpm_adapter._clone_prompt_cache``。

        US-C4: 本引擎 `supports_seed=False` ⇒ 收到显式 seed 一律显式拒绝。
        """
        self._reject_seed(seed)
        if not self._loaded:
            self.load_model()

        # PY-304: 静音替身（_mock_synthesize / np.zeros）已删除 —— 一律上抛
        if self._model is None:
            raise RuntimeError(
                f"MOSS-TTS 模型未加载，无法合成（权重 {self.model_path} 缺失或加载失败）")

        ref_audio = self._get_reference_audio(voice_id)
        if ref_audio is None:
            raise FileNotFoundError(
                f"MOSS-TTS 缺少音色 {voice_id!r} 的参考音频，无法合成；"
                f"请先克隆音色或放置 ref_cn.wav / ref_en.wav")

        emotion_prompt = EMOTION_PROMPTS.get(emotion, EMOTION_PROMPTS["neutral"])

        try:
            import torch
            import soundfile as sf
            import librosa

            # 加载并预处理参考音频
            audio, sr = librosa.load(ref_audio, sr=self.sample_rate, mono=True)

            # 使用 processor 处理输入
            inputs = self._processor(
                text=[text],
                audio=[audio],
                sampling_rate=self.sample_rate,
                return_tensors="pt",
            )
            inputs = {k: v.to(self.device) for k, v in inputs.items()}

            # 生成
            with torch.no_grad():
                generated = self._model.generate(
                    **inputs,
                    generation_config=self._generation_config,
                    max_new_tokens=2048,
                    do_sample=True,
                    temperature=0.8,
                )

            # 解码音频输出
            output_audio = self._processor.decode_audio(
                generated[0].cpu(), sampling_rate=self.sample_rate
            )

            return np.array(output_audio, dtype=np.float32)

        except Exception as e:
            # PY-304/PY-117: 合成失败不得用静音数组顶替 —— 上抛让任务标 failed
            print(f"[MOSS-TTS] 合成失败: {e}")
            raise RuntimeError(f"MOSS-TTS 合成失败: {type(e).__name__}: {e}") from e

    def synthesize_batch(
        self,
        sentences: list,
        voice_id: str = None,
        speed: float = 1.0,
        pitch: float = 0.0,
        emotion: str = "neutral",
        seed=None,
    ) -> list:
        """批量合成（逐句合成，避免长文本处理失败）"""
        if not self._loaded:
            self.load_model()

        if self._model is None:
            return super().synthesize_batch(
                sentences, voice_id, speed, pitch, emotion, seed
            )

        results = []
        for sent in sentences:
            audio = self.synthesize(sent, voice_id, speed, pitch, emotion,
                                    seed=seed)
            results.append((sent, audio))
        return results

    def synthesize_stream(
        self, text: str, voice_id: str = None, seed=None, **kwargs
    ) -> Iterator[np.ndarray]:
        """流式合成（MOSS-TTS 不支持流式，整段后分段输出）"""
        audio = self.synthesize(text, voice_id, seed=seed, **kwargs)
        chunk_size = int(self.sample_rate * 0.5)
        for i in range(0, len(audio), chunk_size):
            yield audio[i : i + chunk_size]

    def clone_voice(self, audio_path: str, transcript: str = None) -> str:
        """
        语音克隆

        Args:
            audio_path: 参考音频路径（建议 3-10 秒清晰语音）
            transcript: 参考音频的文字转写（可选，提供可提高质量）

        Returns:
            voice_id: 克隆音色ID
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
                "id": "mosstts_default_cn",
                "name": "MOSS-TTS 默认中文",
                "language": "zh",
                "gender": "female",
                "engine": "mosstts",
            },
            {
                "id": "mosstts_default_en",
                "name": "MOSS-TTS Default English",
                "language": "en",
                "gender": "female",
                "engine": "mosstts",
            },
        ]

        for vid, apath in self._cloned_voices.items():
            voices.append({
                "id": vid,
                "name": f"克隆音色-{vid[-4:]}",
                "source": "cloned",
                "language": "zh",
                "engine": "mosstts",
                "reference_audio": apath,
            })

        return voices

    def _get_reference_audio(self, voice_id: str = None) -> Optional[str]:
        """获取参考音频路径"""
        if voice_id is None or voice_id == "mosstts_default_cn":
            return self._get_builtin_audio("mosstts_default_cn")

        if voice_id == "mosstts_default_en":
            return self._get_builtin_audio("mosstts_default_en")

        if voice_id in self._cloned_voices:
            return self._cloned_voices[voice_id]

        return self._get_builtin_audio("mosstts_default_cn")

    def _get_builtin_audio(self, speaker_id: str) -> Optional[str]:
        """获取内置说话人的参考音频路径"""
        if speaker_id not in self._builtin_speakers:
            return None

        speaker = self._builtin_speakers[speaker_id]

        if speaker["audio"] and os.path.exists(speaker["audio"]):
            return speaker["audio"]

        # 尝试从内置资源目录查找
        resource_dir = (
            Path(__file__).parent.parent.parent.parent
            / "data"
            / "voices"
            / "mosstts_ref"
        )
        resource_dir.mkdir(parents=True, exist_ok=True)

        if speaker_id == "mosstts_default_cn":
            expected = resource_dir / "ref_cn.wav"
            if expected.exists():
                speaker["audio"] = str(expected)
                return str(expected)
        elif speaker_id == "mosstts_default_en":
            expected = resource_dir / "ref_en.wav"
            if expected.exists():
                speaker["audio"] = str(expected)
                return str(expected)

        print(
            f"[MOSS-TTS] 未找到内置参考音频，请放置 .wav 文件到 {resource_dir}"
        )
        return None