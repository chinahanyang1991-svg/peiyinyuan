"""
VoxCPM TTS 引擎适配器
封装 VoxCPM 2B 参数模型，提供高质量语音合成
基于 VoxCPM2 真实源码 API 重写

融合方案:
- VoxCPM 源码: modules/tts_engine/voxcpm/ (从 VoxCPM-main 复制)
- VoxCPM2 模型: data/models/voxcpm2/ (从 VoxCPM2 复制)
- 依赖安装: torch>=2.5.0, transformers>=4.36.2, torchcodec 等
"""

import sys
import os
import logging
import threading
import time
from pathlib import Path
from typing import Iterator, Optional
import numpy as np

from modules.tts_engine.engines.base_tts import BaseTTSEngine

logger = logging.getLogger(__name__)

# US-C5: 仅用于"惰性创建实例级推理锁"的进程级互斥（双重检查锁的外层锁）。
# 每个适配器实例最终持有自己的 RLock，这把小锁只在第一次取锁时短暂持有。
_INFER_LOCK_MUTEX = threading.Lock()


# ---------------------------------------------------- PY-322 协作式取消
class VoxCPMCancelled(RuntimeError):
    """PY-322：调用方（试听墙钟预算）到点，本次推理在**块边界**主动退出。

    与"合成失败"的区别是刻意的：这不是引擎坏了，而是**这单已被判死**。
    抛出即走出 `with self._infer_lock` —— 串行锁当场释放，半截音频**不落盘**
    （PY-117：绝不拿半个文件冒充成功产物）。
    """


def _check_cancel(cancel_event, where: str) -> None:
    """预算到点就在**当前这个安全检查点**退出（不碰 torch 内核，只在块/入口边界）。"""
    if cancel_event is not None and cancel_event.is_set():
        raise VoxCPMCancelled(f"VoxCPM 在 {where} 收到取消信号（试听预算已判死该任务）")


# ---------- 内部路径解析 ----------
_MODULE_DIR = Path(__file__).resolve().parent  # engines/
_TTS_DIR = _MODULE_DIR.parent                  # tts_engine/
_VOXCPM_DIR = _TTS_DIR / "voxcpm"               # VoxCPM 源码包
_PROJECT_ROOT = _TTS_DIR.parent.parent          # peiyinyuan/
_DEFAULT_MODEL_DIR = _PROJECT_ROOT / "data" / "models" / "voxcpm2"

# 将 tts_engine 目录加入 sys.path，使 `from voxcpm.core import VoxCPM` 可解析
if _VOXCPM_DIR.exists():
    sys.path.insert(0, str(_TTS_DIR))


class VoxCPMAdapter(BaseTTSEngine):
    """VoxCPM TTS 引擎适配器 - 主引擎"""

    engine_name = "voxcpm"
    engine_display = "VoxCPM 2B"
    supported_languages = [
        "zh", "en", "ja", "ko", "fr", "de", "es", "pt", "it", "ru",
        "ar", "th", "vi", "id", "ms", "hi", "tr", "pl", "nl", "sv",
        "da", "fi", "no", "el", "he", "km", "lo", "my", "tl", "sw",
    ]
    supports_cloning = True
    supports_design = True
    supports_streaming = True
    supports_emotion = True
    # US-C4: VoxCPM 的底层 `core.py:_generate()` **没有** seed 形参，随机性来自
    # `torch.randn` 取全局生成器（voxcpm2/unified_cfm/audio_vae 皆然）。所以这里
    # 在**推理互斥闸内**自灌全局 RNG（`apply_global_seed`）来兑现真 seed：
    # 同 seed → 同采样轨迹 → 同产物。锁是 seed 生效的前提，不是可选项。
    supports_seed = True
    sample_rate = 48000
    model_size = "2B"
    # PY-304: 真实权重适配器 —— 就绪与否以 _model 为准，registry 加载后校验
    requires_model_object = True

    # 情感→语音描述映射（用于文本前缀）
    EMOTION_PROMPTS = {
        "neutral": "",
        "excited": "（语气非常激动和兴奋，像发现了宝藏一般）",
        "sad": "（语气低沉，带着淡淡的忧伤，像是在诉说心事）",
        "angry": "（语气愤怒，充满力量，像是在严厉斥责）",
        "gentle": "（语气温柔，如春风拂面，像是在关心朋友）",
        "question": "（上扬的疑问语气，像是在好奇地询问）",
        "surprised": "（语气惊讶，难以置信的样子）",
    }

    def __init__(self, model_path: str = None, device: str = "auto"):
        super().__init__()
        self.model_path = model_path or str(_DEFAULT_MODEL_DIR)
        
        # 自动检测GPU可用性
        if device == "auto":
            try:
                import torch
                if torch.cuda.is_available():
                    self.device = "cuda"
                    print("[VoxCPM] GPU可用，使用CUDA模式")
                else:
                    self.device = "cpu"
                    print("[VoxCPM] GPU不可用，使用CPU模式")
            except ImportError:
                self.device = "cpu"
                print("[VoxCPM] torch未安装，使用CPU模式")
        else:
            self.device = device
            
        self._cloned_voices = {}   # voice_id → (audio_path, transcript)
        self._designed_voices = {} # voice_id → description
        # US-C5 (ANALYSIS-VOXCPM P0-1③④): 全进程只有这一个模型实例，而它**原本通篇
        # 无锁** —— 试听走 `asyncio.to_thread(engine.synthesize)`、正式任务走
        # `run_in_executor(..., synthesize)`，两条线程可以**同时**进入同一个
        # `model.generate()`：PyTorch 模块的 KV/缓存与 feature 状态被交叉写，
        # 结果是串音/shape 崩/退化音，而且 seed 也无法复现（另一路吃掉了 RNG 状态）。
        # 上游自身的用法就是"单实例串行"（core.py 里没有任何并发保护），
        # 所以互斥要由适配器补上。范式照搬 `gptsovits_adapter._serial_lock`。
        # 用 RLock 而非 Lock：synthesize 未加载时会回调 self.load_model()，同线程重入不得自锁。
        # 注意：锁本体存在 `__infer_lock_obj` 上、由 `_infer_lock` 属性惰性取用，
        # 这样即使有人用 `VoxCPMAdapter.__new__(...)` 跳过 __init__（既有测试桩的做法）
        # 也不会退化成"无锁"，而是照样拿到同一把属于该实例的锁。
        self.__infer_lock_obj = threading.RLock()
        # US-C6: load_model() 的分阶段计时（秒），由 model_preloader 放进
        # `/api/system/model_status` 的 detail 字段，让前端能说清"CPU 已跳过编译，纯加载 Xs"
        self.load_detail: dict = {}

    @property
    def _infer_lock(self) -> threading.RLock:
        """US-C5: 该实例的推理互斥锁（惰性可得，永不缺失）。

        正常路径由 `__init__` 建好；这里再兜一层，是为了让
        `VoxCPMAdapter.__new__(VoxCPMAdapter)` 这类跳过构造器的用法（既有
        P0 测试桩）也拿到同一把锁，而不是在属性缺失时直接 AttributeError、
        或悄悄退化成无锁。
        """
        lock = getattr(self, "_VoxCPMAdapter__infer_lock_obj", None)
        if lock is None:
            with _INFER_LOCK_MUTEX:
                lock = getattr(self, "_VoxCPMAdapter__infer_lock_obj", None)
                if lock is None:
                    lock = threading.RLock()
                    setattr(self, "_VoxCPMAdapter__infer_lock_obj", lock)
        return lock

    def _wants_compile(self) -> bool:
        """US-C6: 只有 CUDA 档才值得 torch.compile。

        依据 ANALYSIS-VOXCPM P0-5 / ANALYSIS-OPENVOICE OV-P0-1①：
        `VoxCPM2Model.optimize()` 里硬写着 `if self.device != "cuda": raise ValueError`
        （voxcpm2.py:274-295），CPU 上传 optimize=True 只会走成
        "Warning: torch.compile disabled" 一行 stderr；而 `core.py:96-102` 的
        warm-up generate 无条件跟着 optimize 跑，CPU 实测 35~95s 纯烧时间。
        `"cuda:0"` 这类带序号的设备串同样按 CUDA 处理。
        """
        return str(self.device or "").strip().lower().startswith("cuda")

    @classmethod
    def unavailable_reason(cls) -> str:
        """本地档的说话前提 = `torch` + 随仓源码包 `voxcpm`（本模块导入时已把
        `tts_engine/` 插进 sys.path，故 find_spec 与解释器无关地可解析）。

        PY-354: 与 edgetts 同一口径 —— 只查 import 可得性，不碰权重、不碰网络。
        权重缺失是 `load_model()` 的 `FileNotFoundError`（PY-304 已带原因上抛），
        不该把"装完包还没下模型"的机器误判成死档。
        """
        import importlib.util
        missing = [m for m in ("torch", "voxcpm")
                   if importlib.util.find_spec(m) is None]
        if missing:
            return (f"依赖库未安装: {', '.join(missing)} "
                    "(pip install torch>=2.5.0 transformers>=4.36.2)")
        return ""

    def load_model(self, model_path: str = None) -> None:
        """加载 VoxCPM 模型

        从 data/models/voxcpm2/ 加载本地模型权重。

        PY-304（"半死引擎"）: 此前源码缺失/依赖缺失/权重目录不存在一律
        `_model=None; _loaded=True` 后返回，使 engine_registry.get_loaded(strict=True)
        与 model_preloader 把这具空壳报成可用/ready，合成端再落进 np.zeros 静音替身。
        现在任何加载失败都带原因上抛，"模型缺失"成为 EngineLoadError 而不是静音成品。

        US-C6（冷启动分档 + 三段计时）: 上游 `VoxCPM.from_pretrained(..., optimize=True)`
        把「权重+VAE 反序列化 → torch.compile → warm-up generate」揉进一次调用，
        计时不可见且 CPU 档白跑后两段。这里拆成等价的两步：
          1) `from_pretrained(optimize=False)` 只做权重/VAE（CPU 档到此为止）；
          2) CUDA 档再显式 `tts_model.optimize()` + 一次 `max_len=10` 的 warm-up
             （与 core.py:96-102 同一动作、同一实参）。
        每段耗时写进 `self.load_detail`，由 model_preloader 放进
        `/api/system/model_status` 的 `detail` 字段，前端即可显示
        "CPU 已跳过编译，纯加载 Xs"。
        """
        self._model = None
        self._loaded = False
        path = model_path or self.model_path
        detail = {
            "device": str(self.device),
            "optimize": False,           # 本次是否真的走了 torch.compile
            "import_s": None, "weights_s": None,
            "compile_s": None, "warmup_s": None,
            "restore_voices_s": None, "total_s": None,
        }
        self.load_detail = detail
        t_all = time.time()
        try:
            # US-C5: 加载与推理同闸（RLock 重入安全），避免预热线程正在
            # 反序列化权重时，任务线程已经开始 generate 同一实例。
            with self._infer_lock:
                if not _VOXCPM_DIR.exists():
                    raise RuntimeError(f"VoxCPM 源码缺失: {_VOXCPM_DIR}")
                if not os.path.isdir(str(path)):
                    raise FileNotFoundError(f"VoxCPM 权重目录不存在: {path}")
                t0 = time.time()
                try:
                    from voxcpm.core import VoxCPM
                except ImportError as e:
                    raise RuntimeError(
                        f"VoxCPM/PyTorch 依赖未安装: {e} "
                        "(pip install torch>=2.5.0 transformers>=4.36.2)") from e
                detail["import_s"] = round(time.time() - t0, 2)

                t0 = time.time()
                self._model = VoxCPM.from_pretrained(path, load_denoiser=False,
                                                    device=self.device,
                                                    optimize=False)
                detail["weights_s"] = round(time.time() - t0, 2)
                if self._model is None:
                    raise RuntimeError(f"VoxCPM.from_pretrained 返回 None: {path}")

                if self._wants_compile():
                    detail["optimize"] = True
                    t0 = time.time()
                    optimize = getattr(self._model.tts_model, "optimize", None)
                    if callable(optimize):
                        optimize()          # 只是包一层 torch.compile，真正编译在首跑
                    detail["compile_s"] = round(time.time() - t0, 2)
                    t0 = time.time()
                    # 与 core.py:96-102 的 warm-up 同一动作、同一实参（把 lazy compile
                    # 与 CUDA graph 捕获的开销留在加载期，而不是留给第一句正式文本）
                    self._model.tts_model.generate(
                        target_text="Hello, this is the first test sentence.",
                        max_len=10)
                    detail["warmup_s"] = round(time.time() - t0, 2)
                detail["skipped_stage"] = None if detail["optimize"] else \
                    "torch.compile + warm-up（CPU 档：compile 必然失败、warm-up 纯烧时间）"
                self._loaded = True
                print(f"[VoxCPM] 模型加载完成: {path}")
        except Exception as e:
            self._model = None
            self._loaded = False
            detail["error"] = f"{type(e).__name__}: {e}"
            detail["total_s"] = round(time.time() - t_all, 2)
            print(f"[VoxCPM] 模型加载失败（不再回落静音替身，PY-304）: {e}")
            raise
        finally:
            # 在 try/except 之外恢复音色，确保异常不会影响音色表
            t0 = time.time()
            try:
                self._restore_voices()
            finally:
                detail["restore_voices_s"] = round(time.time() - t0, 2)
                detail["total_s"] = round(time.time() - t_all, 2)
                if detail.get("optimize"):
                    detail["message"] = (f"CUDA：含 torch.compile 与 warm-up，"
                                         f"权重 {detail.get('weights_s')}s + "
                                         f"warm-up {detail.get('warmup_s')}s")
                else:
                    detail["message"] = (f"CPU 已跳过编译与 warm-up，"
                                         f"纯加载 {detail.get('weights_s')}s")
                logger.info(f"[VoxCPM] 加载计时 {detail}")

    # ---------- P0-2: 参考音频编码产物的两级缓存 ----------
    # 与 voxcpm/core.py:_generate 的默认实参逐一对齐 —— 换的是"编码产物从哪来"，
    # 不是"怎么生成"，所以这些值必须和直接调 generate() 时完全一致。
    _GEN_MIN_LEN = 2
    _GEN_MAX_LEN = 4096
    _GEN_RETRY_BADCASE = True
    _GEN_RETRY_MAX_TIMES = 3
    _GEN_RETRY_RATIO_THRESHOLD = 6.0
    # 可无损往返 numpy 的浮点类型（bf16/fp16 -> fp32 存盘、按记录的 dtype 还原）
    _CACHEABLE_DTYPES = ("float16", "bfloat16", "float32", "float64")

    def _prompt_cache_namespace(self) -> str:
        """缓存命名空间：同引擎 + 同权重目录 + 同类名 = 同一份编码。

        换模型权重（换目录）或换模型类必然产生不同的键，旧编码不会被误用。
        """
        tts_model = getattr(self._model, "tts_model", None)
        return "|".join((
            "voxcpm",
            type(tts_model).__name__,
            str(self.model_path),
            str(getattr(tts_model, "_encode_sample_rate", "")),
            str(getattr(tts_model, "patch_size", "")),
        ))

    def _clone_prompt_cache(self, ref_wav: str) -> Optional[dict]:
        """取（或现算并回填）参考音频的 prompt_cache；不适用时返回 None。

        返回 None 的情形（全部退化为今天的内联编码，输出不变）：
          * 没有参考音频（非克隆音色）；
          * 底层模型没有 ``build_prompt_cache(reference_wav_path=...)`` /
            ``generate_with_prompt_cache`` 这一对能力（旧版 VoxCPM 模型、被换掉的引擎）；
          * 编码产物不是"reference 模式的单张 float latent patch"，或 dtype 无法与
            numpy 无损往返 —— 宁可不缓存，也不缓存一个我不认识的东西；
          * 缓存层自身出错（磁盘不可写、npz 损坏…）。
        """
        if not ref_wav:
            return None
        try:
            import inspect

            import torch

            from modules.tts_engine.clone_prompt_cache import clone_prompt_cache

            tts_model = getattr(self._model, "tts_model", None)
            build = getattr(tts_model, "build_prompt_cache", None)
            generate_cached = getattr(tts_model, "generate_with_prompt_cache", None)
            if not callable(build) or not callable(generate_cached):
                return None
            if "reference_wav_path" not in inspect.signature(build).parameters:
                return None
            if not os.path.exists(ref_wav):
                # 文件不存在交给内联路径去抛它原本那套 FileNotFoundError
                return None

            def encode():
                cache = build(prompt_text=None, prompt_wav_path=None,
                              reference_wav_path=ref_wav)
                if not isinstance(cache, dict) or \
                        set(cache) != {"ref_audio_feat", "mode"} or \
                        cache.get("mode") != "reference":
                    return None
                feat = cache["ref_audio_feat"]
                if not hasattr(feat, "detach"):
                    return None
                t = feat.detach()
                dtype_name = str(t.dtype).replace("torch.", "")
                if dtype_name not in self._CACHEABLE_DTYPES:
                    return None
                if dtype_name != "float32":
                    t = t.to(torch.float32)
                return {"ref_audio_feat": t.cpu().contiguous().numpy(),
                        "feat_dtype": np.array(dtype_name, dtype=np.str_)}

            key = clone_prompt_cache.build_key(self._prompt_cache_namespace(), ref_wav)
            entry = clone_prompt_cache.get_or_build(key, encode) if key else encode()
            if not isinstance(entry, dict) or "ref_audio_feat" not in entry:
                return None
            arr = np.asarray(entry["ref_audio_feat"], dtype=np.float32)
            recorded = str(np.asarray(entry["feat_dtype"]).item()) \
                if "feat_dtype" in entry else "float32"
            tensor = torch.from_numpy(np.ascontiguousarray(arr))
            if recorded and recorded != "float32":
                tensor = tensor.to(getattr(torch, recorded))
            return {"ref_audio_feat": tensor, "mode": "reference"}
        except Exception as e:                          # pragma: no cover - 尽力而为
            print(f"[VoxCPM] 克隆 prompt 缓存不可用，改用内联编码: "
                  f"{type(e).__name__}: {e}")
            return None

    def _generate_with_prompt_cache(self, text: str, prompt_cache: dict,
                                    cfg_value: float, inference_timesteps: int):
        """用缓存好的编码产物走模型的 generate_with_prompt_cache（异常原样上抛）。"""
        import re

        # 与 core._generate 同样的输入校验与空白归一化
        if not isinstance(text, str) or not text.strip():
            raise ValueError("target text must be a non-empty string")
        target_text = re.sub(r"\s+", " ", text.replace("\n", " "))
        wav, _tokens, _feat = self._model.tts_model.generate_with_prompt_cache(
            target_text=target_text,
            prompt_cache=prompt_cache,
            min_len=self._GEN_MIN_LEN,
            max_len=self._GEN_MAX_LEN,
            inference_timesteps=inference_timesteps,
            cfg_value=cfg_value,
            retry_badcase=self._GEN_RETRY_BADCASE,
            retry_badcase_max_times=self._GEN_RETRY_MAX_TIMES,
            retry_badcase_ratio_threshold=self._GEN_RETRY_RATIO_THRESHOLD,
        )
        # 与 core._generate 非流式出口同一句：squeeze(0) 后取 CPU 数组，不做设备往返
        return wav.squeeze(0).cpu().numpy() if hasattr(wav, "cpu") \
            else np.asarray(wav).squeeze(0)

    def _compose_request(self, text: str, voice_id: str, emotion: str) -> tuple:
        """把 (送模型的完整文本, 参考音频路径) 一次算清 —— 非流式与流式**共用**。

        US-C5: 流式路径此前收了 ``voice_id`` 却从不使用（等于克隆/设计音色在流式下
        静默退化成默认音色），根因就是这段组装代码只写在 ``synthesize`` 里。
        合并成一个口径后，两条路径对同一个 voice_id 必然产生同样的输入。
        """
        # 构建文本前缀（通过语音描述控制风格）
        emotion_prefix = self.EMOTION_PROMPTS.get(emotion, "")
        # 如果 voice_id 对应已设计的音色描述，也加入前缀
        design_desc = self._designed_voices.get(voice_id, "") if voice_id else ""
        prefix_parts = []
        if design_desc:
            prefix_parts.append(f"（{design_desc}）")
        if emotion_prefix:
            prefix_parts.append(emotion_prefix)
        full_text = "".join(prefix_parts) + text if prefix_parts else text
        # 如果 voice_id 对应克隆音色，使用 reference_wav_path
        ref_wav = None
        if voice_id and voice_id in self._cloned_voices:
            ref_wav = self._cloned_voices[voice_id][0]
        return full_text, ref_wav

    def _apply_seed(self, seed, op: str):
        """US-C4: 在**推理互斥闸内**把 seed 灌进全局 RNG。

        底层 `core.py:_generate()` 没有 seed 形参（也不该为了我去改 vendored 源码），
        而它的全部随机性都来自取全局生成器的 `torch.randn` —— 所以"锁内先播种"
        就是本引擎兑现 seed 的唯一正确姿势。锁外播种等于没播：另一路会吃掉状态。
        """
        if seed is None:
            return None
        seeded = self.apply_global_seed(seed)
        logger.info(f"[VoxCPM] {op} seed={seed} 已灌入全局 RNG {seeded}（推理锁内）")
        return seed

    def synthesize(
        self,
        text: str,
        voice_id: str = None,
        speed: float = 1.0,
        pitch: float = 0.0,
        emotion: str = "neutral",
        seed: Optional[int] = None,
        *,
        cancel_event=None,
    ) -> np.ndarray:
        """
        文本→语音合成
        
        VoxCPM2 不支持 speed/pitch 参数。
        speed 通过以下方式近似实现:
        - 使用 cfg_value 和 inference_timesteps 控制生成质量
        - pitch 暂不支持（VoxCPM2 无此参数）
        
        语音设计（emotion/voice_desc）通过文本前缀实现:
        "(语气描述)文本内容" → VoxCPM2 自动理解语气描述

        P0-2: 克隆音色（``reference_wav_path``）的音频 VAE 编码产物走两级缓存
        （``modules/tts_engine/clone_prompt_cache.py``）。整本书 × 多角色时同一份参考
        音频原本要被重新编码成百上千次，这里命中即省掉整段编码；缓存不命中/不适用
        时走原来的 ``model.generate()`` 内联编码，输出逐样本一致。

        US-C5: 全过程持 ``self._infer_lock`` —— 单实例模型不支持并发推理。
        US-C4: ``seed`` 非 None 时在锁内播种，同 seed 两次产物逐样本一致（CPU 档实测；
        CUDA 上 cudnn/atomics 的非确定性不受 seed 保证）。

        PY-322: ``cancel_event``（``threading.Event``）非 None 时本次调用**可取消**：
        入口/取锁前后各一个检查点，取到锁后走 `_generate_cancellable`（逐块生成、
        块间检查）。预算到点的调用方因此能在**下一个块边界**拿回锁，而不是等整条
        40 分钟的扩散跑完。``cancel_event=None``（整书合成等既有调用方）时走的还是
        上面那条一次性出口，输出与改动前逐样本一致 —— 取消只对被判死的那一单生效。
        """
        # PY-322: 排队等锁期间预算就可能已经到点 —— 别拿到锁再开始烧
        _check_cancel(cancel_event, "synthesize 入口")
        # US-C5: 一把 RLock 罩住"取音色 → 播种 → 推理"整段。
        with self._infer_lock:
            _check_cancel(cancel_event, "synthesize 取锁后")
            if not self._loaded:
                self.load_model()

            # PY-304: 模型不可用时绝不返回静音数组（np.zeros 替身已删除），直接上抛
            self._require_model("合成")

            # US-C4: seed 必须在锁内、且在模型调用前生效
            self._apply_seed(seed, "合成")

            # 音色→(文本前缀, 参考音频)：与流式共用 _compose_request
            full_text, ref_wav = self._compose_request(text, voice_id, emotion)

            try:
                prompt_cache = self._clone_prompt_cache(ref_wav)
                if cancel_event is not None:
                    # PY-322: 可取消出口 —— 逐块生成，块间检查
                    return self._generate_cancellable(
                        full_text, ref_wav, prompt_cache, cancel_event)
                if prompt_cache is not None:
                    # VoxCPM2 真实 API 的第二条入口：编码产物已在手，只跑推理
                    return self._generate_with_prompt_cache(
                        full_text, prompt_cache,
                        cfg_value=2.0, inference_timesteps=10,
                    )

                # VoxCPM2 真实 API: generate(text, ..., reference_wav_path=..., cfg_value=...)
                audio = self._model.generate(
                    text=full_text,
                    reference_wav_path=ref_wav,
                    cfg_value=2.0,
                    inference_timesteps=10,
                )
                # 将音频移动到指定设备
                if hasattr(audio, 'to'):
                    audio = audio.to(self.device)
                return audio.cpu().numpy() if hasattr(audio, 'cpu') else np.array(audio)
            except VoxCPMCancelled as e:
                # PY-322: 取消不是引擎故障，原样上抛（上层保留 timeout 结论）
                logger.warning(f"[VoxCPM] {e}")
                raise
            except Exception as e:
                # PY-304/PY-117: 合成失败不得用静音数组顶替 —— 上抛让任务标 failed
                print(f"[VoxCPM] 合成失败: {e}")
                raise RuntimeError(f"VoxCPM 合成失败: {type(e).__name__}: {e}") from e

    # ---------------------------------------------------------- PY-322
    def _as_np(self, chunk):
        """与 synthesize/synthesize_stream 同一口径：设备搬运后取 CPU numpy。"""
        if hasattr(chunk, 'to'):
            chunk = chunk.to(self.device)
        return chunk.cpu().numpy() if hasattr(chunk, 'cpu') else np.asarray(chunk)

    def _generate_cancellable(self, full_text: str, ref_wav: Optional[str],
                              prompt_cache: Optional[dict], cancel_event) -> np.ndarray:
        """把整条推理改走 voxcpm **自己的分块生成器**，在块边界检查取消信号。

        检查点是既有的自然边界，不是插进 torch 内核：
          `voxcpm/model/voxcpm2.py:1060 for i in tqdm(range(max_len))`（实测就是这条
          进度条 13/454 → 44/454）→ `streaming=True` 时每步 `yield`
          → `voxcpm/core.py:298 for wav, _, _ in generate_result`
          → 本方法的 `for item in stream`。
        所以取消粒度 = 一个生成步（CPU 实测 23.55s/it），不是整条 454 步。

        代价（如实记录，不是零成本）：
          * streaming 分块解码与一次性解码不逐比特相同，且上游在 streaming 模式下
            会关掉 badcase 重试（voxcpm2.py:820 的 warning）；
          * 因此这条分支**只**给"带了 cancel_event 的调用"（= 试听），整书正式合成
            不传 cancel_event，产物字节口径不变。

        取消时：先 `stream.close()`（走通上游 `finally`：关掉内层生成器、退出
        `audio_vae.streaming_decode()` 上下文），再抛 `VoxCPMCancelled` —— 锁随之释放，
        已收的半截块**不返回**，也就永远不会被落盘成缓存（PY-117）。
        """
        if not hasattr(cancel_event, "is_set"):
            raise TypeError("cancel_event 必须是 threading.Event 同型对象")
        chunks: list = []
        if prompt_cache is not None:
            stream = self._model.tts_model.generate_with_prompt_cache_streaming(
                target_text=full_text,
                prompt_cache=prompt_cache,
                min_len=self._GEN_MIN_LEN,
                max_len=self._GEN_MAX_LEN,
                inference_timesteps=10,
                cfg_value=2.0,
                retry_badcase=False,      # 上游在 streaming 下本就不支持重试
            )
            def take(item):               # 元组第一项是解码音频（与 core.py 同口径）
                return self._as_np(item[0])
        else:
            stream = self._model.generate_streaming(
                text=full_text,
                reference_wav_path=ref_wav,
                cfg_value=2.0,
                inference_timesteps=10,
            )
            take = self._as_np
        try:
            for item in stream:
                # 上一块已到手、下一块还没开始 —— 这就是那个安全边界
                if cancel_event.is_set():
                    raise VoxCPMCancelled(
                        f"VoxCPM 在第 {len(chunks) + 1} 个音频块边界收到取消信号")
                chunks.append(take(item))
        finally:
            try:
                stream.close()            # 确定性释放上游生成器/VAE 流式状态
            except Exception:             # pragma: no cover - 收尾失败不该盖掉结论
                pass
        if not chunks:
            return np.zeros(0, dtype=np.float32)   # 空产物由试听质量门判失败，不静音顶替
        return np.concatenate(chunks).astype(np.float32, copy=False)

    def synthesize_stream(
        self, text: str, voice_id: str = None, seed: Optional[int] = None,
        *, cancel_event=None, **kwargs
    ) -> Iterator[np.ndarray]:
        """
        流式合成
        
        VoxCPM2 真实 API: generate_streaming(text, ..., streaming=True)

        US-C5 修的三件事（原状：这段代码收了 ``voice_id`` 却从不使用，而且全程无锁）：
          1. **丢音色**：克隆/设计音色在流式下静默退化成默认音色 —— 现在与非流式
             共用 `_compose_request`，`voice_id` → 文本前缀 + ``reference_wav_path``；
          2. **并发触碰同一模型实例**：与 `synthesize` 抢同一把 `_infer_lock`
             （官方模型是单实例串行假设）；
          3. `emotion` 同样从 kwargs 里被丢弃 —— 一并接进前缀口径。

        锁的持有期 = 整条流的产出期（流式模型推理本身不能被并发共享）。消费者中途
        丢弃生成器时，GC/`close()` 会在 `yield` 处抛 GeneratorExit，锁随之释放，
        不会死锁其他请求。
        """
        # US-C5: 全程互斥（与 synthesize 同闸）
        # PY-322: 等锁期间预算可能已到点 —— 入口先查一次
        _check_cancel(cancel_event, "synthesize_stream 入口")
        with self._infer_lock:
            _check_cancel(cancel_event, "synthesize_stream 取锁后")
            if not self._loaded:
                self.load_model()

            # PY-304: 不再 yield 静音数组
            self._require_model("流式合成")

            # US-C4: 与流式同口径的 seed 兑现（锁内播种）
            self._apply_seed(seed, "流式合成")

            # US-C5: voice_id/emotion 不再被丢弃，与非流式同一组装口径
            emotion = kwargs.get("emotion") or "neutral"
            full_text, ref_wav = self._compose_request(text, voice_id, emotion)

            try:
                # VoxCPM2 真实流式 API: generate_streaming()
                stream = self._model.generate_streaming(
                    text=full_text,
                    reference_wav_path=ref_wav,
                    cfg_value=2.0,
                    inference_timesteps=10,
                    **{k: v for k, v in kwargs.items() if k in (
                        "prompt_wav_path", "prompt_text",
                        "cfg_value", "inference_timesteps", "min_len", "max_len",
                    )}
                )
                for chunk in stream:
                    # PY-322: 块边界 = 唯一安全检查点（不插进 torch 内核）
                    _check_cancel(cancel_event,
                                  "synthesize_stream 音频块边界")
                    # 将音频块移动到指定设备
                    if hasattr(chunk, 'to'):
                        chunk = chunk.to(self.device)
                    yield chunk.cpu().numpy() if hasattr(chunk, 'cpu') else np.array(chunk)
            except VoxCPMCancelled as e:
                # PY-322: 取消 ≠ 引擎故障；抛出即释放串行锁，半截流不落盘
                logger.warning(f"[VoxCPM] {e}")
                raise
            except Exception as e:
                # PY-304/PY-117: 流式失败同样上抛，不用静音顶替
                print(f"[VoxCPM] 流式合成失败: {e}")
                raise RuntimeError(
                    f"VoxCPM 流式合成失败: {type(e).__name__}: {e}") from e

    def clone_voice(self, audio_path: str, transcript: str = None,
                    voice_id: str = None) -> str:
        """
        语音克隆
        
        VoxCPM2 无独立 clone_voice() 方法。
        克隆通过 generate() 的 reference_wav_path 参数实现:
            model.generate(text=text, reference_wav_path=audio_path)
        
        此处保存音频路径 + 转录文本，synthesize() 时传入 reference_wav_path。
        
        Args:
            audio_path: 参考音频路径
            transcript: 参考文本（可选）
            voice_id: 音色ID（可选，不传则自动生成。推荐传入 voice_store 的 UUID 以保持一致性）
        """
        import uuid
        if voice_id is None:
            voice_id = f"cloned_{uuid.uuid4().hex[:8]}"

        if not self._loaded:
            self.load_model()

        # 保存克隆信息（实际克隆在 synthesize 时通过 reference_wav_path 完成）
        self._cloned_voices[voice_id] = (audio_path, transcript)
        print(f"[VoxCPM] 语音克隆已注册: {voice_id} -> {audio_path}")
        return voice_id

    def design_voice(self, description: str, voice_id: str = None) -> str:
        """
        语音设计
        
        VoxCPM2 无独立 design_voice() 方法。
        语音设计通过文本前缀实现: "(描述)文本内容"
        
        此处保存描述文本，synthesize() 时自动添加到文本前缀。
        
        Args:
            description: 音色描述
            voice_id: 音色ID（可选，不传则自动生成。推荐传入 voice_store 的 UUID 以保持一致性）
        """
        import uuid
        if voice_id is None:
            voice_id = f"designed_{uuid.uuid4().hex[:8]}"

        if not self._loaded:
            self.load_model()

        # 保存设计描述（在 synthesize 时通过文本前缀使用）
        self._designed_voices[voice_id] = description
        print(f"[VoxCPM] 语音设计已注册: {voice_id} -> {description[:50]}...")
        return voice_id

    def list_voices(self) -> list:
        """列出可用音色"""
        voices = [
            {"id": "voxcpm_default", "name": "VoxCPM 默认音色",
             "language": "zh", "gender": "neutral"},
        ]
        # 添加克隆的音色
        for vid, (apath, transcript) in self._cloned_voices.items():
            voices.append({
                "id": vid, "name": f"克隆音色-{vid[-4:]}",
                "source": "cloned", "language": "zh",
                "audio_path": apath,
                "transcript": transcript or "",
            })
        # 添加设计的音色
        for vid, desc in self._designed_voices.items():
            voices.append({
                "id": vid, "name": f"设计音色-{vid[-4:]}",
                "source": "designed", "description": desc,
            })
        return voices

    # ---- LoRA 微调接口 (参考 voxcpm/core.py L317-365) ----

    def load_lora(self, lora_weights_path: str) -> tuple:
        """
        加载 LoRA 微调权重

        Args:
            lora_weights_path: .pth 文件或包含 lora_weights.ckpt 的目录路径

        Returns:
            tuple: (loaded_keys, skipped_keys) 加载和跳过的参数名列表
        """
        if not self._loaded:
            self.load_model()

        if self._model is None:
            raise RuntimeError("VoxCPM 模型未加载，无法加载 LoRA 权重")

        if hasattr(self._model, 'load_lora'):
            return self._model.load_lora(lora_weights_path)
        raise RuntimeError("当前 VoxCPM 模型不支持 LoRA")

    def unload_lora(self):
        """卸载 LoRA 权重，恢复基础模型"""
        if self._model and hasattr(self._model, 'unload_lora'):
            self._model.unload_lora()

    def set_lora_enabled(self, enabled: bool):
        """启用/禁用 LoRA（不卸载权重）"""
        if self._model and hasattr(self._model, 'set_lora_enabled'):
            self._model.set_lora_enabled(enabled)

    @property
    def lora_enabled(self) -> bool:
        """检查是否配置了 LoRA"""
        if self._model and hasattr(self._model, 'lora_enabled'):
            return self._model.lora_enabled
        return False

    def get_lora_state_dict(self) -> dict:
        """获取当前 LoRA 参数"""
        if self._model and hasattr(self._model, 'get_lora_state_dict'):
            return self._model.get_lora_state_dict()
        return {}

    def _restore_voices(self):
        """从 DB 恢复已克隆/下载/设计的音色到内存字典

        在 load_model() 完成后自动调用，确保服务器重启后
        clone_voice() 和 design_voice() 注册的音色不会丢失。

        PY-303: 参考音频来源不止用户克隆——tools/resource_scout/import_to_voices.py
        把开放语料下载样本以 source="downloaded" 入库（该列同样写 reference_audio_path，
        且与 model_path 同值，与克隆行一致）。原判断只认 "cloned"，使这批真实参考音频
        出现在列表/UI 却在合成时被无视（退化为随机/design 音色）。方案 B：放宽来源集合，
        保留 source="downloaded" 以便与用户自建克隆在 UI 上分开。
        """
        try:
            from modules.voice_manager.voice_store import voice_store
            voices = voice_store.list_all()
            restored_count = 0
            builtin_count = 0
            for v in voices:
                if v.source in ("cloned", "downloaded") and v.reference_audio_path:
                    if os.path.exists(v.reference_audio_path):
                        # 从 meta 中提取 transcript（若有）
                        transcript = (v.meta or {}).get("transcript")
                        self._cloned_voices[v.id] = (v.reference_audio_path, transcript)
                        restored_count += 1
                elif v.source == "designed" and v.description:
                    self._designed_voices[v.id] = v.description
                    restored_count += 1
                elif v.source == "builtin" and v.description:
                    # PY-363: 4 枚内置音色（标准女声-温柔 等）各自带现采 description，
                    # 但上面两条分支都不认 source='builtin'，使它们在选声器可见可绑、
                    # 合成时却塌成模型默认声。并入 _designed_voices，走与 designed
                    # 完全相同的文本前缀路径（_compose_request 拼 `（description）`）。
                    self._designed_voices[v.id] = v.description
                    builtin_count += 1
                    restored_count += 1
            if restored_count > 0:
                print(f"[VoxCPM] 从 DB 恢复了 {restored_count} 个音色 "
                      f"(参考音频: {len(self._cloned_voices)} [cloned+downloaded], "
                      f"设计: {len(self._designed_voices) - builtin_count}, "
                      f"内置并入设计档: {builtin_count})")
        except Exception as e:
            print(f"[VoxCPM] 恢复音色失败 (可能 DB 尚未初始化): {e}")

    def _require_model(self, op: str) -> None:
        """PY-304: 取代 `_mock_synthesize`（np.zeros 静音替身，已删除）。

        模型对象缺失时带原因上抛，使"引擎不可用"成为任务 failed 的理由，
        而不是一整章听起来正常却全静音的成品。
        """
        if self._model is None:
            raise RuntimeError(
                f"VoxCPM 模型未加载，无法{op}（_model=None, 权重路径 {self.model_path}）"
                f"；加载失败原因请见 EngineLoadError / /api/system/model_status")