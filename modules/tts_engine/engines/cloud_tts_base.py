"""
云端付费 TTS 引擎的共同件（端口层，不含任何厂商协议）

三条纪律，与仓库既有口径一致：

1. **没有 Key 就一帧都不发**。`unavailable_reason()` 直接报"未配置 XXX"，
   `load_model()`/`synthesize()` 同样上抛 —— 绝不静默退回本机引擎或返回静音
   （PY-117/PY-304 那一族的反面教材）。
2. **产物必须可判**。空字节、空数组、解码失败一律带原因上抛，绝不产"completed 但全静音"。
3. **未实测的东西要自己说出口**。每家的 `verified` 字段记录"本次做到了哪一步验证"：
   路由可达性是无 Key 也能测的（POST → 鉴权错 = 路由在），而**请求体形态只能等真 Key**。
"""

import io
import time
from typing import Optional

import numpy as np

from modules.tts_engine.engines.base_tts import BaseTTSEngine

# 与 Edge 档同一组常量：云端合成的失败大多是网络抖动，重试三次再定罪
_RETRY_TIMES = 3
_RETRY_BACKOFF_S = (1.0, 3.0, 6.0)


class CloudTTSUnconfigured(RuntimeError):
    """缺 Key/端点配置 —— 与"云端报错"必须是两类异常（前者该去改配置，后者该重试）。"""


class CloudTTSBase(BaseTTSEngine):
    """付费云端引擎基类。子类给出 `key_env`、`request_audio()`、`list_voices()`。"""

    online = True
    supports_parallel = True
    supports_cloning = False
    supports_design = False
    supports_seed = False
    model_size = "cloud"
    key_env: tuple = ()
    # 本次（2026-09-24）在零 Key 条件下做到哪一步，供 /api/engines 原样出口
    verified_note = ""

    def missing_key(self, env: Optional[dict] = None) -> bool:
        import os
        environ = os.environ if env is None else env
        return not any((environ.get(name) or "").strip() for name in self.key_env)

    @classmethod
    def unavailable_reason(cls) -> str:
        """注册表判"可用"的口径：没 Key 就是**不可用**，并且要指名缺哪个环境变量。"""
        import os
        names = " 或 ".join(cls.key_env)
        if not any((os.getenv(name) or "").strip() for name in cls.key_env):
            return (f"未配置云端付费档：设 {names} 后可用"
                    f"（未配置时该引擎不会发出任何请求，也不会退回本地引擎）")
        return ""

    def load_model(self, model_path: str = None) -> None:
        reason = type(self).unavailable_reason()
        if reason:
            raise CloudTTSUnconfigured(f"{self.engine_display}: {reason}")
        self._loaded = True

    def is_loaded(self) -> bool:
        """云端引擎没有权重对象：只要 Key 在位即就绪，Key 被撤则立刻不算就绪。"""
        return self._loaded and not self.missing_key()

    def synthesize(self, text, voice_id=None, speed=1.0, pitch=0.0,
                   emotion="neutral", seed=None) -> np.ndarray:
        self._reject_seed(seed)   # US-C4: 云端产物不受我方 seed 控制，显式拒绝而非收下不用
        if self.missing_key():
            raise CloudTTSUnconfigured(f"{self.engine_display}: {type(self).unavailable_reason()}")
        if not text or not text.strip():
            raise ValueError(f"{self.engine_name}: 待合成文本为空")
        voice = self._resolve_voice(voice_id)
        last: Optional[BaseException] = None
        for attempt in range(_RETRY_TIMES):
            try:
                blob, fmt = self.request_audio(text, voice, speed, pitch)
                if not blob:
                    raise RuntimeError(f"{self.engine_name} 未返回任何音频字节")
                audio, sr = decode_audio_bytes(blob, fmt)
                if audio.size == 0:
                    raise RuntimeError(f"{self.engine_name} 音频解码得到空数组")
                if sr != self.sample_rate:
                    audio = resample_to_engine_rate(audio, sr, self.sample_rate)
                return audio
            except CloudTTSUnconfigured:
                raise
            except Exception as e:
                last = e
                if attempt < _RETRY_TIMES - 1:
                    time.sleep(_RETRY_BACKOFF_S[attempt])
        raise RuntimeError(
            f"{self.engine_display} 合成失败 (voice={voice}, text={text[:24]!r}, "
            f"{_RETRY_TIMES} 次重试): {type(last).__name__}: {last}") from last

    # ---- 子类需覆盖 ----
    def _resolve_voice(self, voice_id: Optional[str]) -> str:
        raise NotImplementedError

    def request_audio(self, text: str, voice: str, speed: float,
                      pitch: float) -> "tuple[bytes, str]":
        raise NotImplementedError

    # ---- 诊断出口（供 /api/engines 与 model_status 使用）----
    def port_info(self) -> dict:
        import os
        return {
            "engine": self.engine_name,
            "online": True,
            "paid": True,
            "key_env": list(self.key_env),
            "key_present": not self.missing_key(),
            "sample_rate": self.sample_rate,
            "verified_note": self.verified_note,
            "env_echo": {name: bool(os.getenv(name)) for name in self.key_env},
        }


def decode_audio_bytes(blob: bytes, fmt: str = "mp3") -> "tuple[np.ndarray, int]":
    """云端返回的 mp3/wav/flac → float32 mono + 实际采样率。

    与 Edge 档的 `_decode_mp3` 同法（soundfile 优先、ffmpeg 兜底），但**回传真实采样率**：
    付费档各家 24k/32k/48k 混存，把 sr 写死会让整条响度链拿到错的时间轴。
    """
    try:
        import soundfile as sf
        data, sr = sf.read(io.BytesIO(blob), dtype="float32", always_2d=True)
        arr = data.mean(axis=1) if data.shape[1] > 1 else data[:, 0]
        return np.ascontiguousarray(arr, dtype=np.float32), int(sr)
    except Exception:
        pass
    import subprocess

    from modules.tts_engine.ffmpeg_utils import resolve_ffmpeg
    exe = resolve_ffmpeg()
    inp = io.BytesIO(blob).read()
    p = subprocess.run(
        [exe, "-hide_banner", "-loglevel", "error", "-i", "pipe:0",
         "-f", "f32le", "-ac", "1", "pipe:1"],
        input=inp, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if p.returncode != 0:
        raise RuntimeError(
            f"云端音频解码失败 (fmt={fmt}, soundfile+ffmpeg 均不可用): "
            f"{p.stderr.decode('utf-8', 'ignore')[:200]}")
    # ffmpeg 兜底路径不知道原采样率（管道无头），故按声明格式回 24k 并让调用方看得到这一格
    return np.frombuffer(p.stdout, dtype=np.float32).copy(), 24000


def resample_to_engine_rate(audio: np.ndarray, src_sr: int, dst_sr: int) -> np.ndarray:
    """把云端实际返回的采样率归一到引擎声明值（工作流按 `sr = max(...)` 再统一重采样）。"""
    if src_sr <= 0 or src_sr == dst_sr:
        return audio
    import subprocess

    from modules.tts_engine.ffmpeg_utils import resolve_ffmpeg
    exe = resolve_ffmpeg()
    p = subprocess.run(
        [exe, "-hide_banner", "-loglevel", "error", "-f", "f32le", "-ar", str(src_sr),
         "-ac", "1", "-i", "pipe:0", "-f", "f32le", "-ar", str(dst_sr), "pipe:1"],
        input=audio.tobytes(), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if p.returncode != 0 or not p.stdout:
        raise RuntimeError(
            f"重采样失败 {src_sr}Hz→{dst_sr}Hz: "
            f"{p.stderr.decode('utf-8', 'ignore')[:200]}")
    return np.frombuffer(p.stdout, dtype=np.float32).copy()
