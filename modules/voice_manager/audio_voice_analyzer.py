"""
音频音色特征分析器
分析上传的音频文件，提取声学特征（音高、音色MFCC、语速等）
生成可存储的音色特征档
"""

import os
import uuid
import logging
import numpy as np
from typing import Optional
from pathlib import Path
from dataclasses import dataclass, field, asdict

logger = logging.getLogger(__name__)


@dataclass
class VoiceFeatures:
    """从音频中提取的声学特征"""
    # 基本元信息
    name: str = ""
    duration: float = 0.0
    sample_rate: int = 0
    channels: int = 1

    # 音高特征
    mean_pitch: float = 0.0          # 平均基频 (Hz)
    pitch_std: float = 0.0           # 基频标准差
    pitch_range: tuple = (0.0, 0.0)  # 基频范围 (min, max)

    # 音色特征
    mfcc_mean: list = field(default_factory=list)   # MFCC 均值（13维）
    mfcc_std: list = field(default_factory=list)    # MFCC 标准差
    spectral_centroid: float = 0.0   # 频谱质心
    spectral_bandwidth: float = 0.0  # 频谱带宽

    # 能量特征
    rms_energy: float = 0.0          # 均方根能量
    energy_std: float = 0.0          # 能量标准差

    # 语速特征
    speaking_rate: float = 0.0       # 语速 (syllables/sec, 估算)
    zero_crossing_rate: float = 0.0  # 过零率

    # 性别推断
    inferred_gender: str = ""        # "male" / "female" / "unknown"

    # 情感标签（根据声学特征推断）
    inferred_emotion: str = "neutral"

    def to_dict(self) -> dict:
        d = asdict(self)
        if isinstance(d.get("pitch_range"), tuple):
            d["pitch_range"] = list(d["pitch_range"])
        return d


class AudioVoiceAnalyzer:
    """
    音频音色特征分析器
    使用 librosa 分析上传的音频文件，提取声学特征并生成音色特征档
    """

    SUPPORTED_FORMATS = ('.wav', '.mp3', '.flac', '.m4a', '.ogg', '.webm')

    def __init__(self):
        self._librosa_available = self._check_librosa()

    def _check_librosa(self) -> bool:
        try:
            import librosa
            return True
        except ImportError:
            logger.warning("librosa 未安装，音频分析功能不可用")
            return False

    def analyze(self, audio_path: str, name: str = "") -> VoiceFeatures:
        """
        分析音频文件，提取声学特征

        Args:
            audio_path: 音频文件路径
            name: 音色名称

        Returns:
            VoiceFeatures 对象
        """
        if not os.path.exists(audio_path):
            raise FileNotFoundError(f"音频文件不存在: {audio_path}")

        ext = os.path.splitext(audio_path)[1].lower()
        if ext not in self.SUPPORTED_FORMATS:
            raise ValueError(f"不支持的音频格式: {ext}，支持: {self.SUPPORTED_FORMATS}")

        if not self._librosa_available:
            # 无 librosa 时返回基础信息
            return self._basic_analysis(audio_path, name)

        return self._deep_analysis(audio_path, name)

    def _basic_analysis(self, audio_path: str, name: str) -> VoiceFeatures:
        """无 librosa 时的基础分析"""
        import soundfile as sf

        data, sr = sf.read(audio_path)
        duration = len(data) / sr if sr > 0 else 0

        features = VoiceFeatures(
            name=name or Path(audio_path).stem,
            duration=round(duration, 2),
            sample_rate=sr,
            channels=data.ndim if data.ndim > 1 else 1,
        )

        # 简单能量计算
        if len(data.shape) > 1:
            data = np.mean(data, axis=1)
        rms = np.sqrt(np.mean(data ** 2))
        features.rms_energy = round(float(rms), 4)
        features.energy_std = round(float(np.std(data)), 4)

        # 简单过零率
        zcr = np.mean(np.abs(np.diff(np.sign(data)))) / 2
        features.zero_crossing_rate = round(float(zcr), 4)

        # 简单性别推断（基于能量特征）
        if rms > 0.15:
            features.inferred_gender = "male"
        elif rms > 0.05:
            features.inferred_gender = "female"

        return features

    def _deep_analysis(self, audio_path: str, name: str) -> VoiceFeatures:
        """基于 librosa 的深度分析"""
        import librosa

        # 加载音频
        data, sr = librosa.load(audio_path, sr=None, mono=True)
        duration = len(data) / sr

        features = VoiceFeatures(
            name=name or Path(audio_path).stem,
            duration=round(duration, 2),
            sample_rate=sr,
            channels=1,
        )

        # 1. 音高特征 (Pitch)
        try:
            f0, voiced_flag, _ = librosa.pyin(
                data, fmin=librosa.note_to_hz('C2'),
                fmax=librosa.note_to_hz('C7'), sr=sr
            )
            f0_valid = f0[~np.isnan(f0)]
            if len(f0_valid) > 0:
                features.mean_pitch = round(float(np.mean(f0_valid)), 2)
                features.pitch_std = round(float(np.std(f0_valid)), 2)
                features.pitch_range = (
                    round(float(np.min(f0_valid)), 2),
                    round(float(np.max(f0_valid)), 2),
                )
                # 性别推断：平均音高 < 160Hz 倾向男性，> 200Hz 倾向女性
                if features.mean_pitch < 160:
                    features.inferred_gender = "male"
                elif features.mean_pitch > 200:
                    features.inferred_gender = "female"
                else:
                    features.inferred_gender = "unknown"
        except Exception as e:
            logger.warning(f"音高提取失败: {e}")

        # 2. MFCC 音色特征
        try:
            mfcc = librosa.feature.mfcc(y=data, sr=sr, n_mfcc=13)
            features.mfcc_mean = [round(float(v), 4) for v in np.mean(mfcc, axis=1)]
            features.mfcc_std = [round(float(v), 4) for v in np.std(mfcc, axis=1)]
        except Exception as e:
            logger.warning(f"MFCC 提取失败: {e}")

        # 3. 频谱特征
        try:
            spectral_centroids = librosa.feature.spectral_centroid(y=data, sr=sr)
            features.spectral_centroid = round(float(np.mean(spectral_centroids)), 2)

            spectral_bw = librosa.feature.spectral_bandwidth(y=data, sr=sr)
            features.spectral_bandwidth = round(float(np.mean(spectral_bw)), 2)
        except Exception as e:
            logger.warning(f"频谱特征提取失败: {e}")

        # 4. 能量特征
        rms = librosa.feature.rms(y=data)
        features.rms_energy = round(float(np.mean(rms)), 4)
        features.energy_std = round(float(np.std(rms)), 4)

        # 5. 语速和过零率
        try:
            zcr = librosa.feature.zero_crossing_rate(data)
            features.zero_crossing_rate = round(float(np.mean(zcr)), 4)

            # 估算语速（基于能量包络的峰值检测）
            # 使用 onset 检测估算
            onset_frames = librosa.onset.onset_detect(y=data, sr=sr)
            if len(onset_frames) > 0 and duration > 0:
                onset_rate = len(onset_frames) / duration
                features.speaking_rate = round(onset_rate, 2)
        except Exception as e:
            logger.warning(f"语速提取失败: {e}")

        # 6. 情感推断（简单规则）
        if features.mean_pitch > 300 and features.pitch_std > 50:
            features.inferred_emotion = "excited"
        elif features.mean_pitch < 120 and features.rms_energy < 0.05:
            features.inferred_emotion = "sad"
        elif features.rms_energy > 0.2 and features.spectral_centroid > 3000:
            features.inferred_emotion = "angry"
        else:
            features.inferred_emotion = "neutral"

        return features

    def features_to_voice_profile(self, features: VoiceFeatures) -> dict:
        """
        将声学特征转换为音色配置档
        可用于创建 VoiceDesigner 可识别的描述
        """
        profile = {
            "name": features.name,
            "language": "zh",  # 默认为中文
            "gender": features.inferred_gender or "unknown",
            "age_group": "adult",
            "style_tags": [],
            "acoustic_features": features.to_dict(),
        }

        # 从声学特征推断风格标签
        if features.mean_pitch > 250:
            profile["style_tags"].append("清亮")
        elif features.mean_pitch < 130:
            profile["style_tags"].append("低沉")

        if features.spectral_centroid > 2500:
            profile["style_tags"].append("高亢")
        elif features.spectral_centroid < 1500:
            profile["style_tags"].append("沉稳")

        if features.speaking_rate > 4.0:
            profile["style_tags"].append("语速快")
        elif features.speaking_rate < 2.0 and features.speaking_rate > 0:
            profile["style_tags"].append("语速慢")

        if features.rms_energy > 0.15:
            profile["style_tags"].append("大声")
        elif features.rms_energy < 0.03:
            profile["style_tags"].append("轻柔")

        if features.inferred_emotion != "neutral":
            profile["style_tags"].append(features.inferred_emotion)

        return profile


# 全局单例
audio_analyzer = AudioVoiceAnalyzer()