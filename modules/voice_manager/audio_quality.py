"""
试听 / 克隆参考音频的"质量双门"（可复用校验函数）

背景（为什么只看时长+峰值是不够的）：
扩散式 TTS 退化时有两种"听起来是坏的、指标看起来是好的"产物 ——
1) **死渲染**：模型没产出内容，只留下噪声地板，事后一旦被峰值归一放大就变成全尺度嘶声；
2) **响的纯音嗡嗡声**：采样轨迹塌到一条谐波线上，峰值很健康（规整后接近 -2 dBFS），
   静音门完全放过去，但作为克隆参考会把音色钉成"嗡——"。

因此本模块给出两道门（顺序执行，先便宜后昂贵）：

门 1 —— 静音地板门（`SILENCE_FLOOR_DBFS`）
    口径：整段取 ``max(|x|)`` 作为"有效峰值"，按 float 满量程 1.0 = 0 dBFS 折算 dBFS。
    峰值低于 -50 dBFS（线性 0.0031623）判失败。
    为什么是 -50 dBFS：峰值归一（本项目 ``audio_postprocess.normalize_volume``、也
    是常规 mastering 做法）不会把地板以下的信号拉起来，所以"死渲染"一定停在这个
    电平附近；而连耳语都远高于它 —— 用它做"无声"判据不会误伤轻声。

门 2 —— 退化音门（分帧谱平坦度，`DEGENERATE_SPECTRAL_FLATNESS`）
    谱平坦度定义为**同一帧内** ``几何均值 / 算术均值``（功率谱上），取值 (0, 1]。
    方向核对（务必看清，阈值判据方向与它直觉相反）：
        白噪声   -> 谱平坦 -> 比值 -> 1（我们实测 5.6e-1）
        纯音/嗡鸣 -> 谱集中在少数 bin，几何均值被大量空 bin 拉塌 -> 比值 -> 0
                    （我们实测 120Hz 谐波嗡鸣 5.5e-11、1kHz 纯音 7.8e-11）
        真语音   -> 谐波 + 噪声 + 共振峰（实测本仓库 data/voices 的 16 条 TTS 语音
                    为 1.2e-2 ~ 2.5e-2）
    所以**退化方向是"平坦度过小"**：``flatness < 阈值`` 判失败，阈值取 1e-7 ——
    比真语音低约 5 个数量级、比嗡鸣高约 3 个数量级，两侧都有充足余量。
    （注意：如果有人把这条门写成"平坦度 > 阈值判失败"，那判的是噪声，方向正好反了。）

    两个实现要点（缺一个就会误判）：
    a) **必须分帧**（默认 25ms 帧 / 10ms 帧移）。整段一次 FFT 的频率分辨率随时长增长，
       语音谐波会在谱上挖出越来越深的陷波，几何均值随之塌陷 —— 测出来的是"音频有多长"
       而不是"听起来像什么"，长语音会被判成退化。
    b) **必须丢掉低能量帧**（默认丢到最响帧能量 1e-4 以下）。词间空隙只剩噪声地板，
       留着它们会把测量值拖向"静音听起来像什么"。

门 2 的不可测情形（太短 / 全部帧被能量门丢掉 / 采样率未知）返回 ``None`` 并**放行**：
这两道门是为了抓"确定坏了"的产物，不是为了给测不了的样本定罪。

一次重试（换 seed）：
    `synthesize_with_gate()` 把"合成 -> 双门 -> 不过就换 seed 再试一次 -> 仍不过才报错"
    收敛成一个函数。US-C4 之后本项目**有了** seed 接口（`BaseTTSEngine.synthesize`
    末尾的 `seed=None`，以及 `VoiceProfile.seed` 列），但"有参数"和"兑现"是两回事，
    所以这里仍然两道判定：
      * `_accepts_seed()` —— 签名收不收 seed（第三方 callable / 未升级的老替身可能不收）；
      * `supports_seed` —— 引擎是否**真的**让 seed 生效。默认 False 的适配器收到显式
        seed 会直接 `ValueError`（`_reject_seed()`），所以给它们传 seed 是错的，
        必须改走"再抽一条采样轨迹"的老退路。
    两者都为真才透传 `seed + attempt`；否则与升级前一样"重试 = 再抽一条采样轨迹"，
    与"换 seed"目的一致但不是同一个动作 —— 不假造 seed 接口。

清洁室声明：设计参考 VoiceStudio（AGPL-3.0）§5.6/§5.7 的判据思路，代码为本仓库独立实现，
未复制其任何源码文本或注释。seed 契约的"每次生成可指定 seed"思想取自 ChatTTS 的
公开接口设计（只取思想，未读未抄其源码）。
"""

from __future__ import annotations

import inspect
import logging
from typing import Any, Callable, Dict, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)

# ---------- 门 1：静音地板 ----------
SILENCE_FLOOR_DBFS: float = -50.0
# 10^(-50/20) = 0.0031622776601683794，即"有效峰值线性"地板
SILENCE_FLOOR_LINEAR: float = float(10.0 ** (SILENCE_FLOOR_DBFS / 20.0))

# ---------- 门 2：退化音（谱平坦度过小） ----------
DEGENERATE_SPECTRAL_FLATNESS: float = 1e-7
FRAME_MS: float = 25.0            # 帧长（20~30ms 是语音分析常规区间）
HOP_MS: float = 10.0              # 帧移
FRAME_ENERGY_DROP_RATIO: float = 1e-4   # 丢弃能量低于"最响帧 * 该比例"的帧
MIN_FRAMES_TO_MEASURE: int = 3          # 少于这个帧数就认为测不准
# 双门失败时的重试预算：1 次首发 + 1 次换 seed 重试（报告 §5.6 的"换 seed 重试一次"）
SYNTH_MAX_ATTEMPTS: int = 2

# ---------- 结果码 ----------
CODE_OK = "ok"
CODE_EMPTY = "empty"                    # 零采样
CODE_NON_FINITE = "non_finite"          # NaN / inf
CODE_SILENT = "silent_floor"            # 门 1 失败
CODE_DEGENERATE = "degenerate_tone"     # 门 2 失败

__all__ = [
    "SILENCE_FLOOR_DBFS", "SILENCE_FLOOR_LINEAR",
    "DEGENERATE_SPECTRAL_FLATNESS", "FRAME_MS", "HOP_MS",
    "FRAME_ENERGY_DROP_RATIO", "MIN_FRAMES_TO_MEASURE",
    "CODE_OK", "CODE_EMPTY", "CODE_NON_FINITE", "CODE_SILENT", "CODE_DEGENERATE",
    "AudioQualityError", "GateOutcome",
    "as_float_mono", "effective_peak", "peak_dbfs", "spectral_flatness",
    "assess", "assess_file", "is_usable", "synthesize_with_gate",
]


class AudioQualityError(RuntimeError):
    """双门判定为不可用（换 seed 重试用尽后抛出）。"""

    def __init__(self, message: str, code: str, report: Dict[str, Any], attempts: int):
        super().__init__(message)
        self.code = code
        self.report = report
        self.attempts = attempts


class GateOutcome:
    """`synthesize_with_gate` 的返回：音频 + 最后一次门检报告 + 实际尝试次数 + 生效 seed。

    US-C4: `seed` = 产出这条音频**实际用上**的 seed（引擎兑现 seed 时才有值；
    引擎不兑现 seed 或不接受 seed 时为 None —— 不假造）。末尾新增字段，
    既有 `GateOutcome(audio, report, attempts)` 三参构造与三属性读取不受影响。
    """

    __slots__ = ("audio", "report", "attempts", "seed")

    def __init__(self, audio: Any, report: Dict[str, Any], attempts: int,
                 seed: Optional[int] = None):
        self.audio = audio
        self.report = report
        self.attempts = attempts
        self.seed = seed

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<GateOutcome attempts={self.attempts} seed={self.seed} " \
               f"report={self.report}>"


def as_float_mono(audio: Any) -> np.ndarray:
    """把任意"音频形状"转成一维 float32 样本序列。

    - 整数 dtype（int16/int32）按其位宽满量程折算到 [-1, 1)；
    - (n, 1) / (1, n) / (n, ch) 一律按均值降为单声道；
    - torch tensor 等带 ``numpy()/detach()`` 的对象先取 numpy。
    """
    obj = audio
    if hasattr(obj, "detach"):                      # torch tensor
        obj = obj.detach().cpu().numpy()
    elif hasattr(obj, "numpy") and not isinstance(obj, np.ndarray):
        obj = obj.numpy()
    arr = np.asarray(obj)
    if arr.ndim > 1:
        arr = arr.mean(axis=tuple(range(1, arr.ndim))) if arr.shape[0] > 1 else arr.reshape(-1)
        arr = np.asarray(arr)
    arr = np.reshape(arr, -1)
    if np.issubdtype(arr.dtype, np.integer):
        full = float(np.iinfo(arr.dtype).max) or 1.0
        return (arr.astype(np.float32) / full)
    return arr.astype(np.float32, copy=False)


def effective_peak(audio: Any) -> float:
    """有效峰值 = ``max(|x|)``（空数组返回 0.0，非有限值原样返回 inf/nan 由调用方判）。"""
    x = as_float_mono(audio)
    if x.size == 0:
        return 0.0
    return float(np.max(np.abs(x)))


def peak_dbfs(peak_linear: float) -> float:
    """线性峰值 -> dBFS（0 dBFS = float 满量程 1.0）；<=0 返回 -inf。"""
    if peak_linear is None or peak_linear <= 0.0:
        return float("-inf")
    if not np.isfinite(peak_linear):
        return float("inf")
    return float(20.0 * np.log10(peak_linear))


def spectral_flatness(audio: Any,
                      sample_rate: Optional[int] = None,
                      frame_ms: float = FRAME_MS,
                      hop_ms: float = HOP_MS,
                      energy_drop_ratio: float = FRAME_ENERGY_DROP_RATIO) -> Optional[float]:
    """分帧谱平坦度（几何均值 / 算术均值 的帧平均）；测不准时返回 None。

    见模块 docstring 的门 2：**值越小越退化**（纯音/嗡鸣），越大越像噪声。
    """
    x = as_float_mono(audio).astype(np.float64, copy=False)
    if x.size == 0 or sample_rate is None:
        return None
    try:
        sr = float(sample_rate)
    except (TypeError, ValueError):
        return None
    if not np.isfinite(sr) or sr <= 0:
        return None

    frame_len = int(round(sr * frame_ms / 1000.0))
    hop = int(round(sr * hop_ms / 1000.0))
    if frame_len < 32 or hop < 1 or x.size < frame_len:
        return None                                   # 太短 / 采样率太低，不做判定

    n_frames = 1 + int((x.size - frame_len) // hop)
    if n_frames < MIN_FRAMES_TO_MEASURE:
        return None

    idx = np.arange(frame_len)[None, :] + hop * np.arange(n_frames)[:, None]
    frames = x[idx] * np.hanning(frame_len)
    power = np.abs(np.fft.rfft(frames, axis=1)) ** 2
    if power.size == 0:
        return None

    arithmetic = power.mean(axis=1)
    # eps 地板：空 bin 的 log 不能是 -inf（那会把整帧几何均值直接砸成 0，
    # 退化方向虽然一致，但会失去"轻度退化"的分辨率）
    geometric = np.exp(np.log(np.maximum(power, np.finfo(np.float64).eps)).mean(axis=1))

    loud = float(arithmetic.max())
    if not np.isfinite(loud) or loud <= 0.0:
        return None
    keep = arithmetic > loud * energy_drop_ratio      # 丢低能量帧（静音/词间空隙）
    if int(keep.sum()) < MIN_FRAMES_TO_MEASURE:
        return None
    flat = geometric[keep] / np.maximum(arithmetic[keep], np.finfo(np.float64).tiny)
    value = float(np.mean(flat))
    return value if np.isfinite(value) else None


def assess(audio: Any,
           sample_rate: Optional[int] = None,
           *,
           silence_floor_linear: float = SILENCE_FLOOR_LINEAR,
           flatness_threshold: float = DEGENERATE_SPECTRAL_FLATNESS,
           frame_ms: float = FRAME_MS,
           hop_ms: float = HOP_MS) -> Dict[str, Any]:
    """跑双门，返回可断言的报告字典。

    ``{"ok": bool, "code": str, "reason": str, "peak": float, "peak_dbfs": float,
       "spectral_flatness": float|None, "sample_rate": int|None, "samples": int}``
    """
    x = as_float_mono(audio)
    peak = float(np.max(np.abs(x))) if x.size else 0.0
    report: Dict[str, Any] = {
        "ok": True,
        "code": CODE_OK,
        "reason": "",
        "samples": int(x.size),
        "sample_rate": int(sample_rate) if sample_rate else None,
        "peak": peak,
        "peak_dbfs": peak_dbfs(peak),
        "spectral_flatness": None,
    }

    if x.size == 0:
        report.update(ok=False, code=CODE_EMPTY, reason="音频为空（0 采样）")
        return report
    if not np.all(np.isfinite(x)):
        report.update(ok=False, code=CODE_NON_FINITE,
                      reason="音频含非有限采样（NaN/inf）")
        return report

    # ---- 门 1：静音地板 ----
    if peak < silence_floor_linear:
        report.update(ok=False, code=CODE_SILENT,
                      reason=(f"整段有效峰值 {peak:.6f}（{peak_dbfs(peak):.1f} dBFS）"
                              f"低于静音地板 {silence_floor_linear:.6f}"
                              f"（{SILENCE_FLOOR_DBFS:g} dBFS）—— 死渲染/无声"))
        return report

    # ---- 门 2：退化音（谱平坦度过小） ----
    flat = spectral_flatness(x, sample_rate, frame_ms=frame_ms, hop_ms=hop_ms)
    report["spectral_flatness"] = flat
    if flat is not None and flat < flatness_threshold:
        report.update(ok=False, code=CODE_DEGENERATE,
                      reason=(f"分帧谱平坦度 {flat:.3e} 低于退化阈值 "
                              f"{flatness_threshold:.1e} —— 峰值健康但是响的纯音/"
                              f"嗡嗡声（扩散轨迹退化），不可用作参考/试听"))
        return report
    return report


def assess_file(path: str, **kwargs) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """对磁盘上的音频文件跑双门。

    返回 ``(report, error)``：
      - 正常解码 -> (report, None)
      - 解码失败 -> (None, 原因)。**调用方自行决定要不要放行**（本仓库现状是
        "解不了就不判"，见 voice_cloner.validate_audio 的注释），因为门是判
        "确定坏了"的，不是给读不动的文件定罪的。
    """
    try:
        import soundfile as sf
        data, sr = sf.read(path, always_2d=False)
    except Exception as exc:                         # pragma: no cover - 依赖具体环境
        try:
            from pydub import AudioSegment
            seg = AudioSegment.from_file(path)
            data, sr = np.frombuffer(seg.raw_data, dtype=np.int16) / 32768.0, seg.frame_rate
        except Exception as exc2:
            return None, f"{type(exc).__name__}: {exc} / {type(exc2).__name__}: {exc2}"
    return assess(data, sr, **kwargs), None


def is_usable(audio: Any, sample_rate: Optional[int] = None, **kwargs) -> bool:
    """双门是否通过（只要布尔答案时用这个）。"""
    return bool(assess(audio, sample_rate, **kwargs)["ok"])


def _accepts_seed(synthesize: Callable[..., Any]) -> bool:
    """引擎的 synthesize 是否有 seed 形参（有则换 seed 重试，无则换采样轨迹）。"""
    try:
        sig = inspect.signature(synthesize)
    except (TypeError, ValueError):                  # pragma: no cover - 内建/C 函数
        return False
    params = sig.parameters.values()
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params):
        return True
    return "seed" in sig.parameters


def _honors_seed(engine: Any, synthesize: Callable[..., Any]) -> bool:
    """引擎是否**兑现** seed（US-C4）—— 与"签名收不收 seed"是两回事。

    `supports_seed=False` 的适配器收到显式 seed 会 `ValueError`（`_reject_seed()`），
    所以这里必须先看这个声明位；老代码/裸 callable 没有该属性时退回签名探测，
    保持升级前的行为（不会突然给一个能吃 seed 的 callable 少传 seed）。
    """
    declared = getattr(engine, "supports_seed", None)
    if declared is not None:
        return bool(declared)
    return _accepts_seed(synthesize)


def synthesize_with_gate(engine_or_fn: Any,
                         *,
                         text: str,
                         voice_id: Optional[str] = None,
                         speed: float = 1.0,
                         pitch: float = 0.0,
                         emotion: str = "neutral",
                         sample_rate: Optional[int] = None,
                         seed: Optional[int] = None,
                         extra_kwargs: Optional[Dict[str, Any]] = None,
                         silence_floor_linear: float = SILENCE_FLOOR_LINEAR,
                         flatness_threshold: float = DEGENERATE_SPECTRAL_FLATNESS) -> GateOutcome:
    """"合成 -> 双门 -> 换 seed 重试一次 -> 仍不过才报错"。

    Args:
        engine_or_fn: 带 ``synthesize`` 的引擎对象，或直接的 callable。
        seed: 基准种子；``None`` 表示调用方没有种子概念（本项目当前情形）。
        其余参数与 ``BaseTTSEngine.synthesize`` 一致。

    Returns:
        GateOutcome(audio, report, attempts)

    Raises:
        AudioQualityError: 两次尝试都没过门。合成本身抛出的异常**原样上抛**
        （PY-117/PY-304 语义：引擎坏了要让任务 failed，而不是被质量门掩盖）。
    """
    synth = getattr(engine_or_fn, "synthesize", None) or engine_or_fn
    if not callable(synth):
        raise TypeError("synthesize_with_gate 需要引擎对象或 callable")
    if sample_rate is None:
        sample_rate = getattr(engine_or_fn, "sample_rate", None)
    # US-C4: 透传 seed 的两个前提 —— 签名收得到 + 引擎真兑现。
    # 只满足前者会给"收下不用"的适配器塞一个它必然拒绝的参数（ValueError）。
    can_seed = seed is not None and _accepts_seed(synth) and _honors_seed(
        engine_or_fn, synth)
    attempts = SYNTH_MAX_ATTEMPTS               # 首次 + 一次重试

    kwargs: Dict[str, Any] = {"text": text, "voice_id": voice_id,
                              "speed": speed, "pitch": pitch, "emotion": emotion}
    if extra_kwargs:
        kwargs.update(extra_kwargs)

    last_report: Dict[str, Any] = {"ok": False, "code": CODE_EMPTY,
                                   "reason": "未产生任何合成结果", "peak": 0.0,
                                   "peak_dbfs": float("-inf"),
                                   "spectral_flatness": None, "samples": 0,
                                   "sample_rate": sample_rate}
    used = 0
    seed_used: Optional[int] = None
    for attempt in range(attempts):
        call_kwargs = dict(kwargs)
        if can_seed:
            seed_used = int(seed) + attempt
            call_kwargs["seed"] = seed_used
            logger.info(f"[quality_gate] 第 {attempt + 1} 次合成使用 seed={seed_used}")
        elif attempt:
            logger.info("[quality_gate] 引擎不兑现 seed（无 seed 形参或 supports_seed=False），"
                        "重试改为再抽一条采样轨迹")
        audio = synth(**call_kwargs)
        used = attempt + 1
        last_report = assess(audio, sample_rate,
                             silence_floor_linear=silence_floor_linear,
                             flatness_threshold=flatness_threshold)
        if last_report["ok"]:
            if used > 1:
                logger.info(f"[quality_gate] 第 {used} 次尝试通过双门 "
                            f"(peak={last_report['peak']:.4f}, "
                            f"flatness={last_report['spectral_flatness']}, "
                            f"seed={seed_used})")
            return GateOutcome(audio, last_report, used, seed_used)
        logger.warning(f"[quality_gate] 第 {attempt + 1} 次产物未过双门"
                       f"（seed={seed_used}）: {last_report['reason']}")

    raise AudioQualityError(
        f"试听/克隆音频质量门未通过（尝试 {used} 次, 最后一次 seed="
        f"{seed_used if seed_used is not None else '未使用（引擎不兑现 seed）'}）: "
        f"{last_report['reason']}",
        last_report.get("code", CODE_DEGENERATE), last_report, used)
