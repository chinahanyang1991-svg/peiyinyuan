"""
流式合成器
支持边合成边播放，无需等待完整生成
"""

import threading
import queue
import time
from typing import Iterator, Callable
import numpy as np

from modules.tts_engine.engine_registry import engine_registry


class StreamSynthesizer:
    """流式 TTS 合成器"""

    def __init__(self, engine_name: str = None, voice_id: str = None):
        self.engine_name = engine_name or "voxcpm"
        self.voice_id = voice_id
        self._buffer = queue.Queue()
        self._running = False
        self._thread = None

    def synthesize_generator(
        self,
        text: str,
        speed: float = 1.0,
        pitch: float = 0.0,
        emotion: str = "neutral",
    ) -> Iterator[np.ndarray]:
        """
        流式合成生成器

        Yields:
            音频块 (numpy array)，每块约 0.5-1 秒音频
        """
        engine = engine_registry.get_loaded(self.engine_name)
        if engine is None:
            raise RuntimeError(f"引擎不可用: {self.engine_name}")

        if engine.supports_streaming:
            # 原生流式支持
            yield from engine.synthesize_stream(
                text=text,
                voice_id=self.voice_id,
                speed=speed,
                pitch=pitch,
                emotion=emotion,
            )
        else:
            # 回退: 一次性合成 → 分块输出
            full_audio = engine.synthesize(
                text=text,
                voice_id=self.voice_id,
                speed=speed,
                pitch=pitch,
                emotion=emotion,
            )
            # 按 1 秒分块
            chunk_size = engine.sample_rate  # 1 秒
            for i in range(0, len(full_audio), chunk_size):
                yield full_audio[i:i + chunk_size]

    def synthesize_to_queue(
        self,
        text: str,
        speed: float = 1.0,
        pitch: float = 0.0,
        emotion: str = "neutral",
    ):
        """后台线程中合成，结果放入队列"""
        self._running = True

        def _worker():
            try:
                for chunk in self.synthesize_generator(text, speed, pitch, emotion):
                    if not self._running:
                        break
                    self._buffer.put(chunk)
            except Exception as e:
                self._buffer.put(e)
            finally:
                self._buffer.put(None)  # 结束信号
                self._running = False

        self._thread = threading.Thread(target=_worker, daemon=True)
        self._thread.start()

    def read_chunk(self, timeout: float = 5.0) -> np.ndarray:
        """从队列读取下一个音频块"""
        try:
            chunk = self._buffer.get(timeout=timeout)
            if chunk is None:
                return None   # 结束
            if isinstance(chunk, Exception):
                raise chunk
            return chunk
        except queue.Empty:
            return None

    def stop(self):
        """停止合成"""
        self._running = False
        if self._thread:
            self._thread.join(timeout=2.0)

    def is_done(self) -> bool:
        """是否已完成"""
        return not self._running and self._buffer.empty()