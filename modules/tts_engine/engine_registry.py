"""
TTS 引擎注册中心

修复 (P1 #惰性加载):
- 不再在模块导入时实例化所有引擎（避免未安装的依赖在 import 时抛错）
- get() / get_loaded() 按需实例化 + 加载模型，捕获 ImportError 并返回友好提示

使用方法：
    engine = engine_registry.get_loaded("voxcpm")
    audio = engine.synthesize(text="你好", voice_id="default")
"""

import logging
import threading
import time
from typing import Optional

from modules.tts_engine.engines.base_tts import BaseTTSEngine

logger = logging.getLogger(__name__)


class EngineLoadError(RuntimeError):
    """PY-118: 模型加载失败（此前只 warning 后 return engine，调用方无法分辨"半死引擎"）。"""

    def __init__(self, engine_name: str, cause: BaseException):
        self.engine_name = engine_name
        self.cause = cause
        super().__init__(
            f"TTS 引擎 '{engine_name}' 模型加载失败: {type(cause).__name__}: {cause}"
        )


class EngineRegistry:
    """TTS 引擎注册中心（线程安全：懒加载 + 一次实例化）。"""

    _instance = None

    # 引擎工厂（类对象，延迟实例化）
    _engine_factories = {
        "voxcpm": None,    # 下面 __init__ 中填充
        "kokoro": None,
        "indextts": None,
        "mosstts": None,
        "edgetts": None,
    }
    # 已实例化但未加载的引擎
    _engines: dict = {}
    # 已加载模型的引擎
    _loaded: dict = {}
    _initialized = False
    # PY-118/PY-115: 串行化模型加载，避免预热线程与任务线程各加载一份(4.5GB x2)
    _load_lock = threading.RLock()

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        self._register_defaults()

    # -------- 注册 --------
    def _register_defaults(self):
        """注册默认引擎。"""
        # 使用 lazy import：只有在真正 get() 时才 import 适配器，避免依赖缺失时启动失败
        def lazy_voxcpm():
            from modules.tts_engine.engines.voxcpm_adapter import VoxCPMAdapter
            return VoxCPMAdapter()

        def lazy_kokoro():
            from modules.tts_engine.engines.kokoro_adapter import KokoroAdapter
            return KokoroAdapter()

        def lazy_indextts():
            from modules.tts_engine.engines.index_tts_adapter import IndexTTSAdapter
            return IndexTTSAdapter()

        def lazy_mosstts():
            from modules.tts_engine.engines.moss_tts_adapter import MOSSTTSAdapter
            return MOSSTTSAdapter()

        def lazy_edgetts():
            from modules.tts_engine.engines.edgetts_adapter import EdgeTTSAdapter
            return EdgeTTSAdapter()

        def lazy_sample():
            from modules.tts_engine.engines.sample_playback import SamplePlaybackEngine
            return SamplePlaybackEngine()

        def lazy_gptsovits():
            from modules.tts_engine.engines.gptsovits_adapter import GPTSoVITSAdapter
            return GPTSoVITSAdapter()

        def lazy_openvoice():
            from modules.tts_engine.engines.openvoice_adapter import OpenVoiceAdapter
            return OpenVoiceAdapter()

        def lazy_azure():
            from modules.tts_engine.engines.azure_tts_adapter import AzureTTSSAdapter
            return AzureTTSSAdapter()

        def lazy_minimax():
            from modules.tts_engine.engines.minimax_tts_adapter import MiniMaxTTSAdapter
            return MiniMaxTTSAdapter()

        self._engine_factories = {
            "voxcpm": lazy_voxcpm,
            "kokoro": lazy_kokoro,
            "indextts": lazy_indextts,
            "mosstts": lazy_mosstts,
            "edgetts": lazy_edgetts,
            "sample": lazy_sample,
            "gptsovits": lazy_gptsovits,
            # #76: 本地即时克隆档（CPU，权重 MIT，克隆无需转写）
            "openvoice": lazy_openvoice,
            # 指令#3: 云端付费档的**端口**。注册即出现在清单里，但 `unavailable_reason()`
            # 会如实说"未配置 XX_KEY"，缺 Key 时一发请求都不会发。
            "azure": lazy_azure,
            "minimax": lazy_minimax,
        }
        self._engines = {}
        self._loaded = {}

    def register(self, name: str, engine_or_factory):
        """注册新引擎。

        支持两种调用方式：
          1. register("name", EngineClass)          # 传入类/工厂 callable，延迟实例化
          2. register("name", EngineClass())         # 传入已实例化的对象（向后兼容）
        """
        from modules.tts_engine.engines.base_tts import BaseTTSEngine
        if isinstance(engine_or_factory, BaseTTSEngine):
            # 已实例化的引擎：直接存入
            self._engine_factories[name] = lambda: engine_or_factory
            self._engines[name] = engine_or_factory
            logger.info(f"[EngineRegistry] '{name}' 已注册 (已实例化)")
        else:
            # callable 工厂（类或函数）- 延迟实例化
            self._engine_factories[name] = engine_or_factory
            logger.info(f"[EngineRegistry] '{name}' 已注册 (工厂)")
        self._loaded.pop(name, None)

    # -------- 查询 --------
    def list_engines(self) -> list:
        """列出所有已注册引擎（附带可用状态）。"""
        result = []
        for name in self._engine_factories.keys():
            info = {"name": name}
            # 尝试实例化以判断是否可用（不加载模型）
            inst = self.get(name)
            if inst is not None:
                reason = inst.unavailable_reason()
                info.update({
                    "display": getattr(inst, "engine_display", name),
                    "loaded": name in self._loaded,
                    "available": not reason,
                    "reason": reason,
                    "sample_rate": getattr(inst, "sample_rate", 0),
                    "supports_cloning": getattr(inst, "supports_cloning", False),
                    "supports_design": getattr(inst, "supports_design", False),
                    "supported_languages": getattr(inst, "supported_languages", []),
                    # 指令#1 的模式开关要按"在线/本地"分组，指令#3 要标"付费端口是否已配 Key"：
                    # 这两个键都从适配器类属性真取，不在 API 层另抄一份名单。
                    "online": bool(getattr(inst, "online", False)),
                    "port": inst.port_info() if hasattr(inst, "port_info") else None,
                })
            else:
                info["available"] = False
                info["reason"] = "依赖未安装"
            result.append(info)
        return result

    def get(self, name: str = None) -> Optional[BaseTTSEngine]:
        """获取引擎实例（未加载模型）。若依赖缺失返回 None，不抛错。"""
        if name is None:
            from backend.config import Config
            name = Config.TTS_ENGINE

        if name in self._engines:
            return self._engines[name]

        factory = self._engine_factories.get(name)
        if factory is None:
            logger.warning(f"[EngineRegistry] '{name}' 未注册")
            return None

        try:
            engine = factory()
            self._engines[name] = engine
            logger.info(f"[EngineRegistry] '{name}' 已实例化")
            return engine
        except ImportError as e:
            logger.warning(f"[EngineRegistry] '{name}' 依赖未安装: {e}")
            return None
        except Exception as e:
            logger.warning(f"[EngineRegistry] '{name}' 实例化失败: {e}")
            return None

    def get_loaded(self, name: str = None, strict: bool = True) -> Optional[BaseTTSEngine]:
        """获取已加载模型的引擎（若尚未加载则自动调用 load_model）。

        PY-118 修复：加载失败不再 "warning + return engine"（调用方拿到 _model=None 的
        半死引擎，与 PY-117 合流成"任务 completed 但成品全静音"），改为上抛
        EngineLoadError，错误原因经 pipeline 透到 task.error_message。
        PY-304 加固：除 load_model 抛错外，"load_model 未抛错但引擎未就绪"
        （_loaded 假 / requires_model_object 且 _model 为 None）同样上抛，
        使 model_preloader.snapshot() 无法把失败报成 ready。
        strict=False 保留旧行为（仅供交互式端点按需降级）。
        """
        engine = self.get(name)
        if engine is None:
            if strict:
                raise EngineLoadError(
                    name or "?", RuntimeError("引擎未注册或依赖未安装（实例化返回 None）"))
            return None
        if name in self._loaded:
            return engine

        with self._load_lock:
            if name in self._loaded:  # 双检：另一线程已完成加载
                return engine
            try:
                t0 = time.time()
                engine.load_model()
                # PY-304: 适配器若吞掉加载异常（只置 _loaded=True、_model=None），
                # 这里同样拒绝把它缓存为"已加载" —— 否则 model_preloader.snapshot()
                # 会报 state="ready"，静音替身重新获得合法性。
                ready = engine.is_loaded() and not (
                    getattr(engine, "requires_model_object", False)
                    and getattr(engine, "_model", None) is None)
                if not ready:
                    raise EngineLoadError(
                        name, RuntimeError(
                            "load_model 未抛错但引擎未就绪 "
                            f"(_loaded={getattr(engine, '_loaded', None)}, "
                            f"_model={type(engine._model).__name__ if engine._model is not None else 'None'})"
                            " —— 半死引擎形态，见 PY-304"))
                self._loaded[name] = True
                logger.info(
                    f"[EngineRegistry] '{name}' 模型已加载 (耗时 {time.time() - t0:.1f}s)")
            except EngineLoadError:
                raise
            except Exception as e:
                logger.error(
                    f"[EngineRegistry] '{name}' 模型加载失败: {type(e).__name__}: {e}")
                if strict:
                    raise EngineLoadError(name, e) from e
        return engine

    def is_loaded(self, name: str) -> bool:
        """引擎模型是否已加载（供预热状态/进度上报使用）。"""
        return name in self._loaded

    def is_instantiated(self, name: str) -> bool:
        return name in self._engines

    # -------- US-C2: 引擎名的单一事实源（写入前校验，别再让坏值进库） --------
    def known_engine_names(self) -> list:
        """已注册引擎名快照（= 工厂表键）。

        `voice_profiles.engine` 的合法取值只有这个集合：写进别的名字（历史上的
        "cloned" / "uploaded"）不会立刻报错，但**下次用到就必炸** ——
        `get_loaded()` 走 `factory is None` 分支 → EngineLoadError("引擎未注册")，
        并且预热槽会被这条坏引擎名污染（US-C1 修的那类跨引擎假 ready 的近亲）。
        """
        return list(self._engine_factories.keys())

    def is_known_engine(self, name) -> bool:
        return bool(name) and name in self._engine_factories

    def resolve_engine_name(self, name=None, default: str = None) -> str:
        """把一个（可能为空的）引擎名规整成**注册表里真有**的名字；非法即报错。

        Args:
            name: 调用方给的名字（None/空串走默认）。
            default: 覆盖默认值；不给时用 `Config.TTS_ENGINE`，再不行退回 "voxcpm"。
        Raises:
            ValueError: 名字不在白名单里（错误信息带上可选值，便于前端直接展示）。
        """
        if name:
            if self.is_known_engine(name):
                return name
            raise ValueError(
                f"未知 TTS 引擎 '{name}'，可选: {', '.join(self.known_engine_names())}")
        if default:
            if self.is_known_engine(default):
                return default
            raise ValueError(
                f"未知 TTS 引擎 '{default}'，可选: {', '.join(self.known_engine_names())}")
        try:
            from backend.config import Config
            fallback = getattr(Config, "TTS_ENGINE", None)
        except Exception:                            # pragma: no cover - 配置不可用
            fallback = None
        if fallback and self.is_known_engine(fallback):
            return fallback
        if self.is_known_engine("voxcpm"):
            return "voxcpm"
        names = self.known_engine_names()
        if not names:
            raise ValueError("引擎注册表为空，无法解析引擎名")
        return names[0]

    def get_all_voices(self) -> list:
        """获取所有引擎的内置音色列表。"""
        all_voices = []
        for name in self._engine_factories.keys():
            engine = self.get(name)
            if engine is None:
                continue
            try:
                for v in engine.list_voices():
                    if isinstance(v, dict):
                        v.setdefault("engine", name)
                    else:
                        v = {"name": str(v), "engine": name}
                    all_voices.append(v)
            except Exception as e:
                logger.warning(f"[EngineRegistry] '{name}' list_voices failed: {e}")
        return all_voices

    def unload_all(self):
        """卸载所有已加载模型（释放 GPU/CPU 内存）。"""
        for name, engine in list(self._engines.items()):
            try:
                engine.unload_model()
            except Exception:
                pass
        self._loaded.clear()
        logger.info("[EngineRegistry] 所有模型已卸载")


# 全局单例
engine_registry = EngineRegistry()
