"""
TTS 引擎统一抽象接口
所有 TTS 引擎必须实现此接口
"""

from abc import ABC, abstractmethod
from typing import Iterator, Optional
import numpy as np


class BaseTTSEngine(ABC):
    """TTS 引擎统一抽象基类"""

    # 子类需覆盖的类属性
    engine_name: str = "base"
    engine_display: str = "基础引擎"
    supported_languages: list = []
    supports_cloning: bool = False
    supports_design: bool = False
    supports_streaming: bool = False
    supports_emotion: bool = False
    sample_rate: int = 24000
    model_size: str = "unknown"
    # PY-304: 真实权重适配器置 True —— 表示"就绪"还要求 self._model 非空，
    # engine_registry 加载后会据此拒绝"吞异常 + _loaded=True + _model=None"的半死引擎。
    # 默认 False：不持有 _model 槽的测试替身/第三方引擎不受该判定影响。
    requires_model_object: bool = False
    # US-C4 (ChatTTS 的 seed 契约思想，接口设计层面，未搬其代码): 这个引擎是否**兑现**
    # seed —— 传同一个 seed 两次产物可复现。默认 False 的适配器必须用 `_reject_seed()`
    # **显式拒绝**收到的 seed，绝不允许"收下不用"（静默忽略 = 上层以为换 seed 重试过，
    # 实际两次是同一条采样轨迹，重试预算白烧）。
    supports_seed: bool = False

    def __init__(self):
        self._model = None
        self._loaded = False

    # ---------- US-C4: seed 契约的两个共享件 ----------

    def _reject_seed(self, seed) -> None:
        """不支持 seed 的引擎收到显式 seed 时的统一拒绝口径（照 pitch 的纪律）。

        `seed=None`（调用方没有种子概念）放行，保持既有调用方的行为不变；
        只有真的传了 seed 才报错——报错比"假装换了 seed"诚实。
        """
        if seed is not None:
            raise ValueError(
                f"引擎 '{self.engine_name}' 不支持 seed 参数（收到 seed={seed}）。"
                "该引擎的产物不受 seed 控制，静默收下会让上层的质量门重试误判成"
                "'已换 seed'（US-C4）。请改用 supports_seed=True 的引擎，或不传 seed。")

    @staticmethod
    def apply_global_seed(seed) -> list:
        """把 seed 灌进全局 RNG，返回被播种的后端名列表；`seed=None` 时不动任何东西。

        给"底层模型没有 seed 形参"的本地引擎用（VoxCPM 即如此：`core.py:_generate`
        无 seed，随机性来自 `torch.randn` 取全局生成器）。**必须在推理互斥闸内调用**，
        否则并发的另一路会吃掉刚播下的状态，可复现性归零。
        """
        if seed is None:
            return []
        value = int(seed)
        seeded = []
        try:
            import random
            random.seed(value)
            seeded.append("random")
        except Exception:                              # pragma: no cover
            pass
        try:
            import numpy as np
            np.random.seed(value % (2 ** 32))
            seeded.append("numpy")
        except Exception:                              # pragma: no cover
            pass
        try:
            import torch
            torch.manual_seed(value)
            if getattr(torch.cuda, "is_available", lambda: False)():
                torch.cuda.manual_seed_all(value)
                seeded.append("torch.cuda")
            seeded.append("torch")
        except Exception:                              # pragma: no cover - 无 torch 环境
            pass
        return seeded

    @abstractmethod
    def load_model(self, model_path: str = None) -> None:
        """加载模型到内存

        PY-304 契约：加载失败必须抛异常（不得吞掉异常后把 _loaded 置真）。
        """
        ...

    def is_loaded(self) -> bool:
        """检查模型是否已加载。

        PY-304: `_loaded` 只代表"加载流程跑完"；模型对象缺失即视为未就绪，
        避免适配器吞异常时上层（registry / 预热状态 / model_status）把失败报成 ready。
        """
        if not self._loaded:
            return False
        if self.requires_model_object and self._model is None:
            return False
        return True

    @abstractmethod
    def synthesize(
        self,
        text: str,
        voice_id: str = None,
        speed: float = 1.0,
        pitch: float = 0.0,
        emotion: str = "neutral",
        seed: Optional[int] = None,
    ) -> np.ndarray:
        """
        将文本合成为音频数组

        Args:
            text: 输入文本
            voice_id: 音色ID
            speed: 语速倍率 (0.5-2.0)
            pitch: 音调偏移 (-10 ~ +10)
            emotion: 情感模式
            seed: 采样种子 (US-C4)。None = 调用方没有种子概念（与升级前完全等价）；
                非 None 时 `supports_seed=True` 的引擎必须让它生效（同 seed → 同产物），
                `supports_seed=False` 的引擎必须**显式拒绝**（`_reject_seed()`）。
                放在参数表**末尾**是刻意的：所有既有位置调用不受影响。

        Returns:
            numpy array, shape (samples,)
        """
        ...

    def synthesize_stream(
        self, text: str, voice_id: str = None, seed: Optional[int] = None, **kwargs
    ) -> Iterator[np.ndarray]:
        """流式合成（可选实现）

        `seed` 与 `synthesize` 同义（US-C4）：兑现的引擎要让它生效，不兑现的显式拒绝。
        """
        raise NotImplementedError(
            f"{self.engine_name} 不支持流式合成"
        )

    def synthesize_batch(
        self,
        sentences: list,
        voice_id: str = None,
        speed: float = 1.0,
        pitch: float = 0.0,
        emotion: str = "neutral",
        seed: Optional[int] = None,
    ) -> list:
        """
        批量合成（默认逐句合成，子类可重写优化）
        返回: [(sentence, audio_array), ...]
        """
        # 只有调用方真的给了 seed 才透传：未升级签名的第三方/测试替身引擎不会被
        # 一个多余的 keyword 参数打坏（US-C4 的位置兼容纪律）。
        extra = {} if seed is None else {"seed": seed}
        results = []
        for sent in sentences:
            audio = self.synthesize(sent, voice_id, speed, pitch, emotion, **extra)
            results.append((sent, audio))
        return results

    def clone_voice(self, audio_path: str, transcript: str = None) -> str:
        """语音克隆，返回 voice_id"""
        raise NotImplementedError(
            f"{self.engine_name} 不支持语音克隆"
        )

    def design_voice(self, description: str) -> str:
        """语音设计：根据文本描述生成音色，返回 voice_id"""
        raise NotImplementedError(
            f"{self.engine_name} 不支持语音设计"
        )

    def list_voices(self) -> list:
        """列出内置音色"""
        return []

    def resolves_voice(self, voice_id) -> bool:
        """这台引擎能不能发 `voice_id` 这枚音色 (PY-360)。

        默认"能"：本地引擎（voxcpm/openvoice/gptsovits）按 uuid 到全库恢复音色行，
        任何 uuid 都认。只有"只认自己那本音色库、认不出就直接抛错"的适配器（Edge，
        见 `edgetts_adapter._resolve_voice` 的 PY-117 纪律）必须覆盖本方法 —— 工作流
        把句子改派到别的引擎发声时，先问这一句，答"不认"就连带换掉 voice_id，
        否则整本书会在第一句就死。只查内存里的表，不加载权重、不碰网络。
        """
        return True

    def unload_model(self):
        """释放模型内存"""
        self._model = None
        self._loaded = False

    @classmethod
    def unavailable_reason(cls) -> str:
        """依赖缺失时返回人话原因；可用返回 ""。

        注册表判"可用"的旧口径是"能实例化"，而真权重适配器的依赖多在 `load_model`
        里才 import —— 于是缺包的引擎会被报成可用，用户点下去必失败。凡属这类
        适配器都应覆盖本方法（只查 import 可得性，不加载权重、不碰网络）。
        """
        return ""

    def get_info(self) -> dict:
        """获取引擎信息"""
        reason = self.unavailable_reason()
        return {
            "name": self.engine_name,
            "display": self.engine_display,
            "loaded": self.is_loaded(),  # PY-304: 而非裸 _loaded（半死引擎不得报 loaded）
            "available": not reason,
            "unavailable_reason": reason,
            "sample_rate": self.sample_rate,
            "supports_cloning": self.supports_cloning,
            "supports_design": self.supports_design,
            "supports_streaming": self.supports_streaming,
            "supports_emotion": self.supports_emotion,
            "supports_seed": self.supports_seed,
            "supported_languages": self.supported_languages,
            "model_size": self.model_size,
        }