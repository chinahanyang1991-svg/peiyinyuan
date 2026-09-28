"""
音频后处理模块
音量归一化、降噪、静音去除、采样率转换、音频拼接
"""

import numpy as np
from typing import Optional


class AudioPostProcessor:
    """音频后处理器"""

    def __init__(self, sample_rate: int = 48000):
        self.sample_rate = sample_rate

    def normalize_volume(self, audio: np.ndarray, target_db: float = -3.0) -> np.ndarray:
        """
        音量归一化到目标分贝
        target_db: 目标峰值分贝 (-3 = 接近最大但不过载)
        """
        if len(audio) == 0:
            return audio

        peak = np.max(np.abs(audio))
        if peak < 1e-6:
            return audio

        # 目标峰值 (相对于 1.0)
        target_peak = 10 ** (target_db / 20)
        gain = target_peak / peak
        return audio * gain

    def remove_silence(self, audio: np.ndarray,
                    threshold_db: float = -40.0,
                    min_silence_ms: int = 300) -> np.ndarray:
        """
        去除首尾静音
        threshold_db: 静音判定阈值（低于此值视为静音）
        min_silence_ms: 最短静音长度（毫秒）
        """
        if len(audio) == 0:
            return audio

        threshold = 10 ** (threshold_db / 20)
        min_silence_samples = int(min_silence_ms * self.sample_rate / 1000)
        # 步长不能超过音频长度
        step = min(min_silence_samples, max(len(audio) // 10, 1))
        min_silence_samples = step

        # 使用能量检测
        energy = np.abs(audio)

        # 去除开头静音
        start = 0
        for i in range(0, len(energy), step):
            segment = energy[i:i + step]
            if np.max(segment) > threshold:
                start = i
                break

        # 去除结尾静音
        end = len(audio)
        for i in range(len(energy) - step, 0, -step):
            segment = energy[i:i + step]
            if np.max(segment) > threshold:
                end = i + step
                break

        return audio[start:end]

    def concat_audios(self, audio_list: list, gap_ms: int = 300) -> np.ndarray:
        """
        拼接多个音频片段（句子间添加间隔）
        优化：预计算总大小，一次性分配内存（避免大量 numpy 小分配造成的竞争条件）

        PY-122 修复：是否插入 gap 以"已写入的真实段数"为准，而不是入参原始下标。
        旧实现用 `i > 0` 判断，空首段被 continue 跳过后，真实首段仍被当成
        "非首段"补一次 gap → 成品开头凭空多出 300ms 前导静音
        （实测 [空]+4 段 = 733440 样本，应为 719040）。
        """
        if not audio_list:
            return np.array([], dtype=np.float32)

        gap_samples = int(gap_ms * self.sample_rate / 1000)

        # 先过滤空段并计算总长度（gap 只加在真实段之间）
        valid = [a for a in audio_list if a is not None and len(a) > 0]
        if not valid:
            return np.array([], dtype=np.float32)

        total_len = sum(len(a) for a in valid) + gap_samples * (len(valid) - 1)

        # 一次性分配并填充（更高效，减少分配竞争）
        result = np.zeros(total_len, dtype=np.float32)
        cursor = 0
        for j, audio in enumerate(valid):
            if j > 0:
                # gap 默认为零，无需显式写入（数组已经初始化为零）
                cursor += gap_samples
            # 确保音频是 float32
            if audio.dtype != np.float32:
                audio = np.asarray(audio, dtype=np.float32)
            seg_len = len(audio)
            if cursor + seg_len <= total_len:
                result[cursor:cursor + seg_len] = audio
                cursor += seg_len

        return result

    def resample(self, audio: np.ndarray, target_rate: int) -> np.ndarray:
        """
        采样率转换
        """
        if target_rate == self.sample_rate:
            return audio

        try:
            import librosa
            return librosa.resample(
                audio,
                orig_sr=self.sample_rate,
                target_sr=target_rate,
            )
        except ImportError:
            # 简易线性重采样（精度较低）
            ratio = target_rate / self.sample_rate
            new_len = int(len(audio) * ratio)
            indices = np.linspace(0, len(audio) - 1, new_len)
            return np.interp(indices, np.arange(len(audio)), audio)

    def apply_fade(self, audio: np.ndarray,
                fade_in_ms: int = 50, fade_out_ms: int = 200) -> np.ndarray:
        """添加淡入淡出效果"""
        if len(audio) == 0:
            return audio

        result = audio.copy()
        sample_rate_hz = self.sample_rate / 1000

        # 限制淡入/淡出长度：总长减去对向淡出后取半，确保中间不受影响
        max_fade = len(result) // 2
        fade_in_samples = min(int(fade_in_ms * sample_rate_hz), max_fade)
        fade_out_samples = min(int(fade_out_ms * sample_rate_hz), max_fade)

        if fade_in_samples > 0:
            result[:fade_in_samples] *= np.linspace(0, 1, fade_in_samples)
        if fade_out_samples > 0:
            result[-fade_out_samples:] *= np.linspace(1, 0, fade_out_samples)

        return result

    def process(self, audio: np.ndarray,
                normalize: bool = True,
                trim_silence: bool = True,
                fade: bool = False) -> np.ndarray:
        """一站式后处理"""
        if trim_silence:
            audio = self.remove_silence(audio)
        if normalize:
            audio = self.normalize_volume(audio)
        if fade:
            audio = self.apply_fade(audio)
        return audio

    def process_chapter(self, sentence_audios: list,
                         gap_ms: int = 300) -> np.ndarray:
        """
        处理完整章节：拼接句子 + 后处理
        """
        # 先对每个句子做后处理
        processed = [self.process(a, normalize=True, trim_silence=True)
                     for a in sentence_audios if len(a) > 0]

        # 拼接所有句子
        chapter_audio = self.concat_audios(processed, gap_ms=gap_ms)

        # 添加淡入淡出
        chapter_audio = self.apply_fade(chapter_audio)

        return chapter_audio