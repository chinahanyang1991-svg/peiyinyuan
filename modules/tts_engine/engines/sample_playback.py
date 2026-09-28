"""
Sample 音色直插播放引擎 (VA-4)

engine='sample' 的音色行（VA-3 采集管线入库的动物/电子/机械等五类样本）
此前无法被 _stage_synthesize 调用：句级引擎路由拿到 'sample' 后在 registry 查无此引擎。
本适配器补上这条路由 —— 绑定 sample 音色的句子直接输出该音色主样本音频（音效段语义），
speed/pitch/emotion 被有意忽略：音效段没有"语速/音调/情感"可言。

契约（与 BaseTTSEngine 一致）:
- synthesize 返回 float32 mono np.ndarray，采样率 self.sample_rate
- 无权重可加载: load_model 即就绪; online=True 让 workflow 跳过 model_preloader 单槽
- 失败必须 raise 带归因的异常，不得用静音冒充成品 (PY-117)
"""

import os
from typing import Dict, Tuple

import numpy as np

from modules.tts_engine.engines.base_tts import BaseTTSEngine

CACHE_MAX_ENTRIES = 16


class SamplePlaybackEngine(BaseTTSEngine):
    """样本库直插引擎：voice_id -> 该音色主样本 wav -> 原样返回波形"""

    engine_name = "sample"
    engine_display = "样本库直插"
    supported_languages: list = []
    supports_cloning = False
    supports_design = False
    supports_streaming = False
    supports_emotion = False
    sample_rate = 48000
    model_size = "0"
    online = True

    def __init__(self):
        super().__init__()
        # voice_id -> ((path, mtime_ns, size), audio)；主样本不可变，
        # 但 --refresh-existing 会原地换文件，故键含 mtime_ns+size。
        self._cache: Dict[str, Tuple[Tuple[str, int, int], np.ndarray]] = {}

    def load_model(self, model_path: str = None) -> None:
        self._loaded = True

    # -------- 内部 --------
    def _profile_for(self, voice_id: str):
        from modules.voice_manager.voice_store import voice_store
        return voice_store.get(voice_id)

    def _master_path(self, prof) -> str:
        # 主样本(model_path)优先，退回试听副本
        for p in (getattr(prof, "model_path", None),
                  getattr(prof, "sample_audio_path", None)):
            if p:
                return p
        return ""

    # -------- 合成 --------
    def synthesize(self, text: str, voice_id: str = None, speed: float = 1.0,
                   pitch: float = 0.0, emotion: str = "neutral",
                   seed=None) -> np.ndarray:
        # US-C4: 回放的是库里录好的 master，产物与 seed 无关 —— 显式拒绝而非静默收下
        self._reject_seed(seed)
        if not voice_id:
            raise RuntimeError(
                "sample 引擎必须绑定 voice_id（句子未携带音色，"
                "sample 音色无法按文本合成）")
        prof = self._profile_for(voice_id)
        if prof is None:
            raise RuntimeError(f"sample 引擎: 音色 {voice_id!r} 不存在于音色库")
        path = self._master_path(prof)
        if not path or not os.path.isfile(path):
            raise RuntimeError(
                f"sample 引擎: 音色 {getattr(prof, 'name', voice_id)!r} "
                f"的主样本文件缺失: {path!r}")

        st = os.stat(path)
        key = (path, st.st_mtime_ns, st.st_size)
        hit = self._cache.get(voice_id)
        if hit is not None and hit[0] == key:
            return hit[1]

        import soundfile as sf
        y, sr = sf.read(path, dtype="float32", always_2d=True)
        audio = np.asarray(y.mean(axis=1), dtype=np.float32)
        if sr != self.sample_rate:
            import soxr
            audio = np.asarray(soxr.resample(audio, sr, self.sample_rate),
                               dtype=np.float32)
        if audio.size == 0:
            raise RuntimeError(f"sample 引擎: {path!r} 解码后为空")
        peak = float(np.max(np.abs(audio)))
        if peak < 0.001:
            raise RuntimeError(
                f"sample 引擎: {path!r} 近似静音 (peak={peak:.6f})"
                f" —— 不以静音冒充成品 (PY-117)")

        if len(self._cache) >= CACHE_MAX_ENTRIES:
            self._cache.pop(next(iter(self._cache)))
        self._cache[voice_id] = (key, audio)
        return audio
