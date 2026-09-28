"""#87 / #95 / #96 VOICE-PREVIEW —— 无样本音色的试听：**速度档秒级** + 预算 + 单飞。

缺陷单原文："音色试听失败，无法试听"。

第一代实现（#87，本文件的前一版）把"没有样本文件"这条分支接成了
"用**该音色自己绑定的引擎**现合成"，并承诺 `DEFAULT_WAIT_S=60` 超时后
"稍后再点一次试听即可命中缓存"。**这条承诺在真机上是假的**（主线 12:43–12:52 实测）：

  * `logs/backend-8000.log:102` 起 `[preview] 启动现合成 4b60f1e7-…`，紧跟 voxcpm
    进度条 `3%|▎| 9/268 [01:29<43:57, 10.18s/it]` —— 一条试听要 ~44 分钟；
    12:52 三次复探（同 voice_id）仍全部 503，`data/voices/previews_generated/`
    目录根本还没被创建。
  * 12:44:02 / 12:45:02 又各自为**另两个** voice_id 启动现合成 —— 按 voice_id 的
    合并挡住了"同一条重复排队"，但没有任何**全局并发闸**，多任务一起抢 CPU。
  * `logs/backend-8000.log:114`：那条 40 分钟的任务最后**死在落盘**——
    `sf.write(tmp, …)` 的后缀是 `.tmp`，soundfile 推不出格式
    （`No format specified and unable to get format from file extension`），
    40 分钟算力换回一个 `write_failed`，一个字节都没留下。

第二代实现（本文件，#95 + #96）兑现四条硬口径：

  A1 **试听走速度档**（项目既有裁定：重引擎 GPT-SoVITS/voxcpm 只进离线整书合成）
      无现成样本时用 `edge-tts`（在线神经音色，历史实测 RTF≈0.10）合一条短试听，
      目标 p95 ≤ 15s 首包、二次点击 ≤1s 命中缓存。音色绑的是重引擎也**绝不**为
      试听去等重引擎；重引擎只保留 `?quality=high` 显式出口（同样有预算）。
      edge 档对非 edgetts 音色是**代理声**（按 gender/age/描述词挑一条最接近的
      Edge 官方音色），这件事如实写进旁挂元数据与响应头，不假装是原引擎出的声。
  A2 **短 seed 文本**：送给引擎的文本固定 ≤14 字（`preview_text_for()`，可由
      `name` 派生但裁剪），绝不把长描述整段喂进合成 —— 长度直接决定重引擎的步数。
  A3 **PY-117 不静默成功**：失败一律结构化错误码（`engine_unavailable` /
      `engine_load_failed` / `synthesis_failed` / `timeout` / `quality_gate_failed` /
      `write_failed` / `engine_busy`）+ 真实原因；日志留送给引擎的**原句**；
      绝不返回静音、绝不返回"200 但字节不是 RIFF"。
  A4 **双质量门 + 试听专用门**：落盘前过 `audio_quality` 既有双门（静音地板 +
      谱平坦度退化音）**再叠**试听口径（时长 0.4~30s、`0.02 ≤ peak`、
      峰值 <0.5 抬到 0.7、抬完不得削顶）；落盘后再从磁盘读回复检 + RIFF 魔数核对。
      任一环节不过就把该缓存文件删掉并当作失败 —— 不"有文件就算成功"。

  B5 **单飞去重**：同 `voice_id` × 同 `quality` 只有一个生成任务在跑（`_jobs` 表 +
      互斥锁 + 引用计数）；另有全局并发闸 `_FAST_SEM`/`_HIGH_SEM`（重引擎档默认 1），
      挡住"多条音色一起现合成抢 CPU"。
  B6 **硬墙钟预算**：每个任务 `budget_s` 到点由 watchdog 判 `timeout` ——
      落旁挂失败元数据、HTTP 侧后续请求直接拿这个可区分错误码（冷却期内不再重跑），
      迟到的工作线程**不得**再发布产物（`job.abandoned` 在落盘前二次核对）。
      PY-322 补上原来缺的另一半：**预算不仅要判死等待方，还要终止执行者**。
      任务多一只 `job.cancel_event`，到点由 watchdog `set()`，并随 `synthesize`
      的 `cancel_event=` 形参递给**声明支持**的引擎（voxcpm 在音频块边界检查），
      引擎因此在下一个个块边界退出、**当场释放重引擎的串行锁**，半截产物不落盘；
      线程确认退出前该 `voice_id::quality` 键处于 **poisoned** 态，
      后续请求（含 `?regenerate=1`）不再排到仍在烧的任务上，立刻拿
      `engine_busy_cancelling`。不接收 `cancel_event` 的引擎（edge/GPT-SoVITS）
      仍是"只判死 + 产物作废"，本文件如实记在日志与报告里。
      Python 线程不可强杀，所以取消只能是协作式的：检查点只放在引擎
      **已有的循环边界**，绝不插进 torch 内核。
  B7 **默认路径秒级**：`?quality=high` 才允许碰重引擎。

对生产库**零写入**（不变）：产物只落 `data/voices/previews_generated/` 的文件 +
旁挂 JSON，不回写 `sample_audio_path` / `seed`；seed 经 `X-Last-Successful-Seed`
原样出口（US-C4 语义不变，edge 档不兑现 seed ⇒ 该头**不发**，不发比发假值诚实）。
"""

from __future__ import annotations

import json
import logging
import os
import re
import inspect
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


def _env_f(name: str, default: float) -> float:
    """取浮点环境变量；坏值回落默认并留日志（不让一个 typo 把试听打死）。"""
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return float(default)
    try:
        return float(raw)
    except ValueError:
        logger.warning(f"[preview] 环境变量 {name}={raw!r} 不是数字，沿用默认 {default}")
        return float(default)


# ------------------------------------------------------- A2: 短 seed 文本
# 刻意中性、且**短**：够听出音色像不像；重引擎的耗时几乎线性于文本长度
# （40 分钟那条 job 喂的是下面这句的 2.6 倍长度 + 268 步扩散）。
PREVIEW_TEXT = "这是一段音色试听，用来确认。"      # 14 字（含标点）
MAX_PREVIEW_CHARS = 14
# 只有"干净且短"的名字才值得进试听句；库里有 mojibake 名（如 `×ÊÔ´¿âÃ°ÑÌ-…`），
# 那种字符串喂进 TTS 只会得到一句无人听的噪声。
_NAME_OK = re.compile(r"^[一-鿿A-Za-z0-9·\- ]{2,6}$")


def preview_text_for(row) -> str:
    """按音色行拼一条**不超过 14 字**的试听文案（name 可派生，一律裁剪）。"""
    name = (getattr(row, "name", None) or "").strip()
    if _NAME_OK.match(name):
        text = f"{name}，这是一段试听。"
    else:
        text = PREVIEW_TEXT
    return text[:MAX_PREVIEW_CHARS]


# --------------------------------------------- B6/B7: 预算与等待口径（秒）
# HTTP 侧最多等多久 —— 速度档实测个位数秒，这里给到 20s 富余；
# 旧的 60s 是"给重引擎排队留面子"，如今重引擎不在默认路径上。
DEFAULT_WAIT_S = _env_f("VOICE_PREVIEW_WAIT_S", 20)
# 速度档任务的硬墙钟预算（到点判 timeout，产物作废）。
FAST_BUDGET_S = _env_f("VOICE_PREVIEW_FAST_BUDGET_S", 20)
# 高档（重引擎）预算：默认仍远小于实测的 44 分钟，绝不无界。
HIGH_BUDGET_S = _env_f("VOICE_PREVIEW_HIGH_BUDGET_S", 180)
# 失败后的重试冷却：速度档短（网络抖一下就该能再试），重引擎长（别反复点反复跑 3 分钟）。
FAST_RETRY_COOLDOWN_S = _env_f("VOICE_PREVIEW_FAST_RETRY_COOLDOWN_S", 15)
HIGH_RETRY_COOLDOWN_S = _env_f("VOICE_PREVIEW_HIGH_RETRY_COOLDOWN_S", 300)
# 全局并发闸：速度档是 IO 等待，给 4；重引擎档默认 1（挡 CPU 抢占）。
MAX_CONCURRENT_FAST = int(_env_f("VOICE_PREVIEW_MAX_CONCURRENT_FAST", 4))
MAX_CONCURRENT_HIGH = int(_env_f("VOICE_PREVIEW_MAX_CONCURRENT_HIGH", 1))
# PY-322 poisoned 态的兜底天花板（秒）：正常情况下"worker 确认退出"就解封；
# 但**不接收 cancel_event 的引擎**（edge 之外的老适配器/GPT-SoVITS）可能一直烧，
# 此时不能把一个键永久锁死 —— 到点当作"这一票认栽"，放行新任务（并留 warning 日志）。
CANCEL_POISON_MAX_S = _env_f("VOICE_PREVIEW_CANCEL_POISON_MAX_S", 900)

# ----------------------------------------- A4: 试听专用质量门（比双门更严）
PREVIEW_MIN_PEAK = 0.02        # 低于此=基本听不见（双门的 -50dBFS≈0.0032 太宽）
PREVIEW_BOOST_BELOW = 0.5      # 峰值偏低就抬
PREVIEW_BOOST_TARGET = 0.7     # 抬到这里（与主线既有口径一致）
PREVIEW_CLIP_LIMIT = 0.99      # 抬完超过它=削顶，判失败而不是交一个破音产物
PREVIEW_MIN_DURATION_S = 0.4
PREVIEW_MAX_DURATION_S = 30.0

# 结构化错误 -> HTTP 状态码
ERROR_STATUS: Dict[str, int] = {
    "voice_not_found": 404,
    "engine_unavailable": 503,
    "engine_load_failed": 503,
    "in_progress": 503,
    "engine_busy": 429,
    # PY-322：上一条任务已被预算判死、但它的执行线程还没确认退出（重引擎锁仍在烧）
    "engine_busy_cancelling": 429,
    "quality_gate_failed": 502,
    "synthesis_failed": 502,
    "timeout": 504,                 # B6：预算到点，可区分于 in_progress / 引擎坏了
    "write_failed": 500,
}
# 第一代实现用过的键名，保留映射以免外部诊断脚本读表落空
ERROR_STATUS["synth_failed"] = ERROR_STATUS["synthesis_failed"]

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_WAV_MAGIC = b"RIFF"

_jobs: Dict[str, "_Job"] = {}
_jobs_lock = threading.Lock()
# PY-322 poisoned 表：key -> 已被预算判死、但 worker 线程**尚未确认退出**的任务。
# 存在即表示"这个键后面那台引擎还在烧（重引擎只有一把串行锁）"，
# 期间不得再为该键起新任务，否则等于把第二个已放弃的请求也焊在同一条锁上。
_poisoned: Dict[str, "_Job"] = {}
# 引擎类 -> `synthesize` 是否收 `cancel_event`（探测一次即可，别每次反射）
_cancel_kwarg_cache: Dict[type, bool] = {}
_FAST_SEM = threading.BoundedSemaphore(MAX_CONCURRENT_FAST)
_HIGH_SEM = threading.BoundedSemaphore(MAX_CONCURRENT_HIGH)
_seq_lock = threading.Lock()
_seq = 0


def _next_seq() -> int:
    global _seq
    with _seq_lock:
        _seq += 1
        return _seq


# ------------------------------------------------------------------ 音色行
def _load_row(voice_id: str):
    """取音色行（单独包一层，便于单测注入）。"""
    from modules.voice_manager.voice_store import voice_store
    return voice_store.get(voice_id)


def _native_sample(voice_id: str) -> Optional[str]:
    """出口 1：音色行自带的样本文件（沿用 P0-4 双门判定，不改变旧语义）。"""
    from backend.services.voice_service import voice_service
    return voice_service.get_preview_audio(voice_id)


class _Job:
    """一条音色 × 一个质量档的一次生成任务（并发共享；单飞 = 同 key 只有一个）。

    PY-322 新增四只字段（预算的另一半：**终止执行者**）：
      * `cancel_event`  —— watchdog 判死时 `set()`；由 `_generate_*` 递进
        `engine.synthesize(cancel_event=…)`，引擎在**自己的块边界**上查它。
      * `worker_exited` —— 只有工作线程自己在 `finally` 里 `set()`，
        是"执行者真的停了/锁真的还回来了"的唯一凭据（poisoned 的解封条件）。
      * `cancellable`   —— 本次实际接走 token 的引擎是否支持取消（诊断用；
        False 时判死只能退回旧行为"产物作废"，日志与错误码都要如实标出来）。
      * `poisoned_at`   —— 进入 poisoned 态的时刻，用于 `CANCEL_POISON_MAX_S` 兜底。
    """

    __slots__ = ("voice_id", "quality", "done", "abandoned", "result", "thread",
                 "timer", "started_at", "deadline", "budget_s", "seq", "key",
                 "cancel_event", "worker_exited", "cancellable", "poisoned_at")

    def __init__(self, voice_id: str, quality: str, budget_s: float):
        self.voice_id = voice_id
        self.quality = quality
        self.key = f"{voice_id}::{quality}"
        self.budget_s = float(budget_s)
        self.started_at = time.time()
        self.deadline = self.started_at + self.budget_s
        self.done = threading.Event()
        self.abandoned = threading.Event()
        self.cancel_event = threading.Event()
        self.worker_exited = threading.Event()
        self.cancellable = False
        self.poisoned_at = 0.0
        self.result: Optional[dict] = None
        self.thread: Optional[threading.Thread] = None
        self.timer: Optional[threading.Timer] = None
        self.seq = _next_seq()


def preview_dir() -> Path:
    """生成缓存目录（`VOICE_PREVIEW_DIR` 可覆盖，单测用它指向 tmp_path）。"""
    override = os.getenv("VOICE_PREVIEW_DIR")
    if override:
        return Path(override)
    from backend.config import Config
    return Path(Config.VOICE_DIR) / "previews_generated"


def cache_paths(voice_id: str, quality: str = "fast") -> Tuple[Path, Path]:
    """(wav, json)。高档单独占一个键，避免 `?quality=high` 的产物污染默认缓存。

    注意：**不能**用 `Path(f"{id}.high").with_suffix(".wav")` —— 那样 `.high`
    会被当成待替换的旧后缀，高档与默认档落到同一个 `{id}.wav` 上（键失效）。
    """
    stem = f"{voice_id}{'' if quality == 'fast' else '.high'}"
    base = preview_dir()
    return base / f"{stem}.wav", base / f"{stem}.json"


def _read_meta(voice_id: str, quality: str = "fast") -> Optional[dict]:
    _, meta_path = cache_paths(voice_id, quality)
    try:
        if meta_path.exists():
            return json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception as e:                       # 坏 JSON 当成"没有缓存"处理
        logger.warning(f"[preview] 读缓存元数据失败 {meta_path}: {type(e).__name__}: {e}")
    return None


def _error(code: str, message: str, **extra) -> dict:
    status = ERROR_STATUS.get(code, 500)
    return {"ok": False, "code": code, "message": message, "status": status,
            "retry_after": extra.pop("retry_after", None), **extra}


def _write_meta(voice_id: str, quality: str, meta: dict) -> None:
    """旁挂元数据落盘（诊断/验收口径）；写失败只 warning —— 产物本身已经有结论了。"""
    _, meta_path = cache_paths(voice_id, quality)
    try:
        meta_path.parent.mkdir(parents=True, exist_ok=True)
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2,
                                        default=str), encoding="utf-8")
    except Exception as e:
        logger.warning(f"[preview] 元数据落盘失败 {meta_path}: {type(e).__name__}: {e}")


def _drop_cache(voice_id: str, quality: str) -> None:
    """删掉一个未过门的缓存对（A4：不许"有文件就算成功"）。"""
    wav_path, meta_path = cache_paths(voice_id, quality)
    for stale in (wav_path, meta_path):
        try:
            stale.unlink(missing_ok=True)
        except OSError as e:
            logger.warning(f"[preview] 清理坏缓存失败 {stale}: {e}")


# ---------------------------------------------------- A1: 速度档引擎与代理声
# 词表顺序即优先级：先特殊口音/童声，再年龄段，最后性别兜底。
_EDGE_PROXY_RULES: List[Tuple[Tuple[str, ...], str]] = [
    (("东北",), "zh-CN-liaoning-XiaobeiNeural"),
    (("陕西",), "zh-CN-shaanxi-XiaoniNeural"),
    (("台湾", "台式"), "zh-TW-HsiaoChenNeural"),
    (("粤语", "粤"), "zh-HK-WanLungNeural"),
    (("童声", "儿童", "小孩", "萝莉", "正太"), "zh-CN-YunxiaNeural"),
    (("新闻", "播报", "解说", "庄重", "沉稳", "大气"), "zh-CN-YunyangNeural"),
    (("磁性", "低沉", "浑厚", "大叔", "中年", "老年"), "zh-CN-YunjianNeural"),
    (("少女", "萌", "活泼", "清脆", "甜美", "傲娇"), "zh-CN-XiaoyiNeural"),
]
_EDGE_BY_GENDER = {"female": "zh-CN-XiaoxiaoNeural", "male": "zh-CN-YunxiNeural"}
_EDGE_DEFAULT = "zh-CN-XiaoxiaoNeural"


def select_edge_voice(row) -> Tuple[str, str]:
    """给一条音色挑**速度档**用的人声，返回 `(edge_short_name, binding)`。

    binding: `native` = 这行本来就是 edge 音色（meta.edge_voice）；
             `proxy`  = 按性别/年龄段/描述词挑的**代理声**，
             必须在元数据/响应头里如实标出（试听是"听个大概像不像"，不是终稿）。
    """
    engine = (getattr(row, "engine", None) or "").strip().lower()
    meta = getattr(row, "meta", None) or {}
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except Exception:
            meta = {}
    own = (meta.get("edge_voice") or "").strip() if isinstance(meta, dict) else ""
    if own and own.endswith("Neural"):
        return own, "native"
    if engine != "edgetts":
        pass                                 # 代理声：下面按文本特征挑
    hay = " ".join([str(getattr(row, "name", "") or ""),
                    str(getattr(row, "description", "") or ""),
                    str(getattr(row, "style_tags", "") or "")])
    for keys, voice in _EDGE_PROXY_RULES:
        if any(k in hay for k in keys):
            return voice, "proxy"
    gender = (getattr(row, "gender", None) or "").strip().lower()
    if gender in _EDGE_BY_GENDER:
        return _EDGE_BY_GENDER[gender], "proxy"
    return _EDGE_DEFAULT, "proxy"


def _load_engine(name: str):
    """按注册表键取引擎实例（已加载模型）。失败上抛 EngineLoadError。"""
    from modules.tts_engine.engine_registry import engine_registry
    return engine_registry.get_loaded(name)


# ------------------------------------------------- PY-322: 协作式取消的接线
def _engine_accepts_cancel(engine) -> bool:
    """该引擎的 `synthesize` 是否收 `cancel_event`（= 它有没有可取消的循环边界）。

    按**类**缓存反射结果：探测失败（内建/C 扩展/签名不可省）当作不支持，
    绝不因为探测而改变调用参数 —— 未声明该形参的引擎继续吃旧行为
    （判死 + 产物作废），这在本模块的日志与 `engine_busy_cancelling` 里如实标出。
    """
    cls = type(engine)
    cached = _cancel_kwarg_cache.get(cls)
    if cached is None:
        synth = getattr(engine, "synthesize", None)
        try:
            cached = "cancel_event" in inspect.signature(synth).parameters
        except (TypeError, ValueError):
            cached = False
        _cancel_kwarg_cache[cls] = cached
    return cached


def _cancel_kwargs(job: "_Job", engine) -> dict:
    """支持取消的引擎才递 token；同时把"这次到底可不可取消"记在 job 上。"""
    if _engine_accepts_cancel(engine):
        job.cancellable = True
        return {"cancel_event": job.cancel_event}
    return {}


# ------------------------------------------------------- A4: 质量门（数组级）
def gate_preview_array(audio, sample_rate, *, text, voice_id, engine_name):
    """试听口径的落盘前门：既有双门 + 时长 + 峰值地板 + 峰值抬升。

    Returns `(arr, gate_report, None)` 或 `(None, None, error_dict)`。
    """
    import numpy as np

    from modules.voice_manager.audio_quality import assess

    arr = np.asarray(audio, dtype=np.float32).reshape(-1)
    sr = int(sample_rate or 0)
    where = f"voice={voice_id} engine={engine_name} text={text!r}"

    if arr.size == 0:
        return None, None, _error("synthesis_failed",
                                  f"引擎返回空音频数组（0 采样）；{where}")
    if sr <= 0:
        return None, None, _error("synthesis_failed",
                                  f"引擎未给出有效采样率（{sample_rate!r}）；{where}")
    if not bool(np.all(np.isfinite(arr))):
        return None, None, _error("synthesis_failed",
                                  f"引擎返回含 NaN/inf 的采样；{where}")

    duration_s = arr.size / float(sr)
    if duration_s < PREVIEW_MIN_DURATION_S or duration_s > PREVIEW_MAX_DURATION_S:
        return None, None, _error(
            "quality_gate_failed",
            f"试听时长 {duration_s:.2f}s 不在合理区间 "
            f"[{PREVIEW_MIN_DURATION_S}, {PREVIEW_MAX_DURATION_S}]s；{where}",
            engine=engine_name, duration_s=round(duration_s, 3))

    peak = float(np.max(np.abs(arr)))
    if peak < PREVIEW_MIN_PEAK:
        # 静音替身红线（PY-304/PY-117）：宁可不给音频，也不给"听不见的文件"
        return None, None, _error(
            "quality_gate_failed",
            f"试听峰值 {peak:.6f} 低于地板 {PREVIEW_MIN_PEAK}"
            f"（≈{20 * (np.log10(max(peak, 1e-12))):.1f} dBFS）—— 近似无声；{where}",
            engine=engine_name, peak=peak)

    boosted = False
    if peak < PREVIEW_BOOST_BELOW:
        gain = PREVIEW_BOOST_TARGET / peak
        arr = (arr * gain).astype(np.float32)
        new_peak = float(np.max(np.abs(arr)))
        if new_peak > PREVIEW_CLIP_LIMIT:
            return None, None, _error(
                "quality_gate_failed",
                f"峰值 {peak:.4f} 抬到 {PREVIEW_BOOST_TARGET} 会削顶"
                f"（{new_peak:.4f} > {PREVIEW_CLIP_LIMIT}），拒绝交付；{where}",
                engine=engine_name, peak=peak)
        boosted = True
        peak = new_peak

    report = assess(arr, sr)                    # 既有双门：静音地板 + 谱平坦度退化音
    if not report.get("ok"):
        return None, None, _error(
            "quality_gate_failed",
            f"试听产物未过质量双门: {report.get('reason')}",
            engine=engine_name, quality_report=report)
    report.update(boosted=boosted, duration_s=round(duration_s, 3),
                  sample_rate=sr, peak_after=round(peak, 6))
    return arr, report, None


def _publish(wav_path: Path, meta_path: Path, *, arr, sample_rate: int,
             voice_id: str, quality: str, job: "_Job", engine_name: str,
             text: str, gate_report: dict, extra: dict) -> dict:
    """原子落盘 + 磁盘复检（RIFF 魔数 + 双门），任一环节不过就删掉并报错。"""
    import soundfile as sf

    wav_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = wav_path.with_name(f"{wav_path.name}.{job.seq}.tmp")
    try:
        # BUGFIX（logs/backend-8000.log:114）：sf.write 只看扩展名，`.tmp` 后缀会
        # "No format specified" —— 40 分钟算力换 0 字节。这里显式给 format。
        sf.write(tmp, arr, sample_rate, format="WAV", subtype="PCM_16")
        if job.abandoned.is_set():
            # B6：预算已经判死，迟到的产物作废（不发布 = 不出现"失败后突然变成功"）
            tmp.unlink(missing_ok=True)
            return _error("timeout",
                          f"生成任务超过预算 {job.budget_s:.0f}s 已被判失败，"
                          f"迟到产物已作废（不落缓存）",
                          engine=engine_name, voice_id=voice_id, quality=quality)
        os.replace(tmp, wav_path)               # 原子换入：并发读不会拿到半截文件
    except Exception as e:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        logger.error(f"[preview] 写缓存失败 {wav_path}: {type(e).__name__}: {e}")
        return _error("write_failed",
                      f"试听产物落盘失败: {type(e).__name__}: {e}", engine=engine_name)

    # 磁盘复检：字节必须真是 RIFF，且解得开、过门（A3 的"200 但字节不是 RIFF"红线）
    try:
        with open(wav_path, "rb") as f:
            magic = f.read(4)
    except OSError as e:
        _drop_cache(voice_id, quality)
        return _error("write_failed", f"试听产物读不回: {type(e).__name__}: {e}",
                      engine=engine_name)
    if magic != _WAV_MAGIC:
        _drop_cache(voice_id, quality)
        return _error("write_failed",
                      f"试听产物头部不是 RIFF（实为 {magic!r}），已删除",
                      engine=engine_name)
    from modules.voice_manager.audio_quality import assess_file
    report, err = assess_file(str(wav_path))
    if report is None or not report.get("ok"):
        _drop_cache(voice_id, quality)
        return _error("quality_gate_failed",
                      f"落盘后复检未通过: {report.get('reason') if report else err}",
                      engine=engine_name)

    duration_s = round(float(gate_report.get("duration_s")
                             or arr.size / float(sample_rate)), 3)
    meta = {
        "voice_id": voice_id,
        "status": "success",
        "origin": "generated_now",
        "quality": quality,
        "engine": engine_name,
        "requested_engine": (getattr(_load_row(voice_id), "engine", None) or None),
        "text": text,
        "sample_rate": int(sample_rate),
        "duration_s": duration_s,
        "bytes": wav_path.stat().st_size,
        "peak": round(float(report.get("peak", 0.0)), 4),
        "spectral_flatness": report.get("spectral_flatness"),
        "boosted": bool(gate_report.get("boosted")),
        "budget_s": job.budget_s,
        "seq": job.seq,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
        **extra,
    }
    _write_meta(voice_id, quality, meta)
    logger.info(f"[preview] seq={job.seq} {voice_id}/{quality} 生成完成: "
                f"{wav_path.name} {meta['bytes']}B {duration_s}s peak={meta['peak']} "
                f"engine={engine_name}")
    return {"ok": True, "path": str(wav_path), "origin": "generated_now",
            "quality": quality, "engine": engine_name, "seed": extra.get("seed"),
            "voice": extra.get("edge_voice"), "meta": meta}


# ------------------------------------------------------------ 生成主逻辑
def _generate_fast(job: "_Job", row) -> dict:
    """默认档位：edge-tts 速度档合一条短试听。"""
    voice_id = job.voice_id
    text = preview_text_for(row)
    logger.info(f"[preview] seq={job.seq} {voice_id}/fast 送引擎原句 text={text!r} "
                f"len={len(text)}")

    try:
        engine = _load_engine("edgetts")
    except Exception as e:
        cause = f"{type(e.cause).__name__}: {e.cause}" if getattr(e, "cause", None) else str(e)
        return _error("engine_unavailable",
                      f"速度档引擎 edgetts 不可用（试听不走重引擎兜底）: {cause}",
                      engine="edgetts",
                      next="确认已 `pip install edge-tts` 且可联网；"
                           "或改用 ?quality=high 显式走该音色绑定的引擎（有预算上限）")

    edge_voice, binding = select_edge_voice(row)
    t0 = time.time()
    try:
        audio = engine.synthesize(text=text, voice_id=edge_voice,
                                  speed=1.0, pitch=0.0, emotion="neutral",
                                  **_cancel_kwargs(job, engine))
    except Exception as e:
        logger.warning(f"[preview] seq={job.seq} 速度档合成失败 {voice_id} "
                       f"(text={text!r}, voice={edge_voice}): "
                       f"{type(e).__name__}: {e}"
                       f"{'（取消后退出）' if job.cancel_event.is_set() else ''}")
        return _error("synthesis_failed",
                      f"速度档引擎 edgetts 现合成试听失败（{time.time() - t0:.1f}s）: "
                      f"{type(e).__name__}: {e}",
                      engine="edgetts", edge_voice=edge_voice, text=text)

    sample_rate = int(getattr(engine, "sample_rate", 24000) or 24000)
    arr, gate_report, gate_err = gate_preview_array(
        audio, sample_rate, text=text, voice_id=voice_id, engine_name="edgetts")
    if gate_err is not None:
        return gate_err
    return _publish(*cache_paths(voice_id, "fast"), arr=arr, sample_rate=sample_rate,
                    voice_id=voice_id, quality="fast", job=job,
                    engine_name="edgetts", text=text, gate_report=gate_report,
                    extra={"edge_voice": edge_voice,
                           "voice_binding": binding,
                           "seed": None,
                           "synth_seconds": round(time.time() - t0, 2)})


def _generate_high(job: "_Job", row) -> dict:
    """高档出口（`?quality=high`）：用该音色**自己绑定的引擎**，同样受预算约束。

    保留 #87 的描述前缀注册与 seed 口径；默认路径永远不会走到这里。
    """
    from modules.voice_manager.audio_quality import AudioQualityError
    from modules.voice_manager.audio_quality import synthesize_with_gate
    from modules.voice_manager.language_tier import openvoice_tier_gate
    from backend.services.voice_service import make_preview_seed

    voice_id = job.voice_id
    from backend.config import Config
    declared_engine = (getattr(row, "engine", None) or "").strip().lower()
    cfg_engine = (getattr(Config, "TTS_ENGINE", "") or "").strip().lower()
    # PY-323(a′): 参考音频是外文的行不交给 openvoice（实测跨语种克隆字准不合格），
    # 原因必须留痕 —— 换引擎=换嗓子，不能静默。
    own, tier_reason = openvoice_tier_gate(declared_engine, row, fallback=cfg_engine)
    if tier_reason:
        logger.warning(f"[preview] seq={job.seq} {voice_id}/high 语种档降级 "
                       f"{voice_id[:8]}: {tier_reason}")
    names = [n for n in (own, cfg_engine) if n]
    names = list(dict.fromkeys(names))
    engine = None
    engine_name = None
    # 语种档降级已经把 own 换成了兜底引擎，循环里的 `name != own` 看不出来，
    # 所以这里以"闸门给了原因"作为"这枚产物不是声明引擎发的"的初值。
    fallback_used = bool(tier_reason)
    load_errors = []
    for idx, name in enumerate(names):
        try:
            engine = _load_engine(name)
        except Exception as e:
            cause = f"{type(e.cause).__name__}: {e.cause}" if getattr(e, "cause", None) else str(e)
            logger.warning(f"[preview] seq={job.seq} 引擎 {name} 加载失败 {voice_id}: {cause}")
            load_errors.append(f"{name}: {cause}")
            continue
        engine_name = name
        fallback_used = fallback_used or bool(own and name != own)
        break
    if engine is None:
        return _error("engine_unavailable",
                      f"该音色声明的引擎 {declared_engine or '(空)'} 不可用"
                      f"（实际尝试 {own or '(空)'}；未注册/依赖未装/加载失败），"
                      f"无法现合成试听",
                      engine=own or None,
                      next="; ".join(load_errors)[:400] or "安装该引擎依赖后重试")

    text = preview_text_for(row)
    logger.info(f"[preview] seq={job.seq} {voice_id}/high 送引擎原句 text={text!r} "
                f"len={len(text)} engine={engine_name}")
    desc = (getattr(row, "description", None) or "").strip()
    designed = False
    if desc and getattr(engine, "supports_design", False):
        try:
            engine.design_voice(desc, voice_id=voice_id)
            designed = True
        except Exception as e:
            logger.warning(f"[preview] 描述前缀注册失败 {voice_id}: {type(e).__name__}: {e}")

    seed_base = make_preview_seed(engine)
    t0 = time.time()
    # PY-322: 把取消闸递进重引擎（voxcpm 在音频块边界查它）。引擎不声明该形参
    # 时这里传空 dict，行为与升级前一致 —— 那一档就是"无法取消的路径"，
    # 由 poisoned 态兜住，不再叠第二个任务上去。
    cancel_extra = _cancel_kwargs(job, engine)
    if not cancel_extra:
        logger.warning(f"[preview] seq={job.seq} {voice_id}/high 引擎 {engine_name} "
                       f"未声明 cancel_event -> 预算到点只能判死+作废产物，"
                       f"执行者无法提前退出（该键将进入 poisoned 态）")
    try:
        outcome = synthesize_with_gate(engine, text=text, voice_id=voice_id,
                                       speed=1.0, pitch=0.0, emotion="neutral",
                                       seed=seed_base, extra_kwargs=cancel_extra or None)
    except AudioQualityError as qe:
        return _error("quality_gate_failed",
                      f"现合成的试听样本未通过质量双门: {qe.report.get('reason')}",
                      engine=engine_name, quality_report=qe.report,
                      text=text, attempts=qe.attempts)
    except Exception as e:
        logger.warning(f"[preview] seq={job.seq} 合成异常 {voice_id}: "
                       f"{type(e).__name__}: {e}"
                       f"{'（取消生效，执行者已在检查点退出）' if job.cancel_event.is_set() else ''}")
        if job.cancel_event.is_set():
            # 预算已经判死（_abandon）：这里只把"执行者确实停了"记进结论，
            # 不改对外码（对外保持 timeout）
            return _error("timeout",
                          f"引擎 '{engine_name}' 在预算判死后按取消信号退出: "
                          f"{type(e).__name__}: {e}",
                          voice_id=voice_id, quality=job.quality,
                          engine=engine_name, seq=job.seq,
                          budget_s=job.budget_s,
                          elapsed_s=round(time.time() - job.started_at, 1))
        return _error("synthesis_failed",
                      f"用引擎 '{engine_name}' 现合成试听失败: {type(e).__name__}: {e}",
                      engine=engine_name, text=text)

    sample_rate = int(getattr(engine, "sample_rate", 48000) or 48000)
    arr, gate_report, gate_err = gate_preview_array(
        outcome.audio, sample_rate, text=text, voice_id=voice_id,
        engine_name=engine_name)
    if gate_err is not None:
        return gate_err
    return _publish(*cache_paths(voice_id, "high"), arr=arr, sample_rate=sample_rate,
                    voice_id=voice_id, quality="high", job=job,
                    engine_name=engine_name, text=text, gate_report=gate_report,
                    extra={"seed": outcome.seed,
                           "attempts": outcome.attempts,
                           "edge_voice": None,
                           # 三态如实标：语种档降级 / 引擎加载失败回落 / 真用声明引擎
                           "voice_binding": ("tier_downgrade" if tier_reason
                                             else ("engine_fallback" if fallback_used
                                                   else "own_engine")),
                           "declared_engine": declared_engine or None,
                           "engine_tier_gate": tier_reason,
                           "engine_fallback": engine_name if fallback_used else None,
                           "description_applied": designed,
                           "synth_seconds": round(time.time() - t0, 2)})


def _run_generation(job: "_Job") -> dict:
    row = _load_row(job.voice_id)
    if row is None:
        return _error("voice_not_found", f"音色 {job.voice_id} 不存在")
    if job.quality == "high":
        return _generate_high(job, row)
    return _generate_fast(job, row)


def _abandon(job: "_Job", *, reason: str = "budget") -> None:
    """B6 预算到点：判 failure（可区分码）、**给执行者发取消信号**、键进 poisoned、
    落旁挂元数据、唤醒等待者。

    幂等：只有第一个到达者生效（`_jobs_lock` 下改状态）。

    PY-322：这里是从"只判死等待方"升级到"同时终止执行者"的唯一出口 ——
    `cancel_event.set()` 之后，支持取消的引擎会在**下一个它自己的循环边界**
    （voxcpm：音频块边界）退出并释放串行锁；不支持的引擎继续烧，
    但 `_poisoned` 保证这段时间没有第二个任务被排到它身上，
    `worker_exited` 一到才解封（`CANCEL_POISON_MAX_S` 是永久锁死的兜底天花板）。
    """
    with _jobs_lock:
        if job.done.is_set():
            return
        job.abandoned.set()
        job.cancel_event.set()             # PY-322：终止执行者（协作式）
        job.poisoned_at = time.time()
        _poisoned[job.key] = job           # PY-322：判死到线程确认退出之间不许再排任务
        waited = round(time.time() - job.started_at, 1)
        result = _error(
            "timeout",
            f"该音色的试听生成任务超过预算 {job.budget_s:.0f}s（已跑 {waited}s）"
            f"被判定失败，未产出缓存",
            voice_id=job.voice_id, quality=job.quality,
            engine="edgetts" if job.quality == "fast" else None,
            budget_s=job.budget_s, elapsed_s=waited, seq=job.seq,
            retry_after=int(max(5, min(job.budget_s, 60))),
            cancelling=bool(job.cancellable),
            next="预算到点即判死，并已给合成线程发退出信号：稍后再点一次试听会重开一个任务；"
                 "默认档走 edge-tts 速度档（个位数秒），若持续超时说明网络或引擎侧有问题")
        result["_failed_at"] = time.time()
        job.result = result
    logger.warning(f"[preview] seq={job.seq} {job.voice_id}/{job.quality} "
                   f"超过预算 {job.budget_s:.1f}s -> 标记 timeout 失败（reason={reason}）"
                   f"，已发取消信号（引擎支持取消={bool(job.cancellable)}）")
    # 元数据必须在 done 之前落盘：等待者一被唤醒就会去读它（否则"判死了但没证据"）
    _write_meta(job.voice_id, job.quality, {
        "voice_id": job.voice_id, "quality": job.quality, "status": "failure",
        "code": "timeout", "reason": result["message"], "budget_s": job.budget_s,
        "elapsed_s": waited, "seq": job.seq, "abandoned_at": time.time(),
        "cancel_signalled": True, "engine_cancellable": bool(job.cancellable),
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
    })
    with _jobs_lock:
        if not job.done.is_set():
            job.done.set()


def _poison_error(job: "_Job") -> dict:
    """PY-322：这个键上一条任务已被判死、执行线程还没确认退出时的可区分错误。

    与 `timeout` 的分工：`timeout` = "你等的那次跑完了没结果"（冷却期内复用），
    `engine_busy_cancelling` = "上一次还在退出路上，这次**不给你排队**"。
    """
    cancelling_for = round(time.time() - (job.poisoned_at or time.time()), 1)
    return _error("engine_busy_cancelling",
                  f"该音色的上一条试听任务 seq={job.seq}（{job.quality} 档）已因超过预算 "
                  f"{job.budget_s:.0f}s 被判死，但它的合成线程仍在退出路上"
                  f"（判死后已 {cancelling_for}s，引擎{'已收到取消信号' if job.cancellable else '不支持提前退出，只能等它跑完'}）"
                  f"；本次不再排到同一条重引擎串行锁上",
                  voice_id=job.voice_id, quality=job.quality, seq=job.seq,
                  budget_s=job.budget_s, cancelling_for_s=cancelling_for,
                  engine_cancellable=bool(job.cancellable),
                  retry_after=int(max(3, min(60, job.budget_s))),
                  next="等几秒再点：线程在音频块边界退出后该键自动解封；"
                       "重引擎档（?quality=high）不适合在线试听，请回到默认档")


def _finish(job: "_Job") -> None:
    """工作线程主体：并发闸 -> 生成 -> 落结论。

    PY-322：外面这一层**只**负责"执行者确认退出"的登记 ——
    `worker_exited` 是 poisoned 态唯一的解封凭据，所以必须放在 `finally`，
    异常/判死/正常返回三条路都要走到。
    """
    try:
        _run_job(job)
    finally:
        job.worker_exited.set()
        with _jobs_lock:
            if _poisoned.get(job.key) is job:
                _poisoned.pop(job.key, None)
        if job.cancel_event.is_set():
            logger.warning(f"[preview] seq={job.seq} {job.key} 执行线程已确认退出"
                           f"（判死->退出耗时 {round(time.time() - (job.poisoned_at or time.time()), 1)}s"
                           f"，串行锁已释放）")


def _run_job(job: "_Job") -> None:
    sem = _HIGH_SEM if job.quality == "high" else _FAST_SEM
    if job.abandoned.is_set():
        return
    # 拿不到槽位就别排队：排到一半必然被 watchdog 判 timeout，白占一个任务位。
    if not sem.acquire(timeout=max(0.5, job.budget_s * 0.5)):
        result = _error("engine_busy",
                        f"同时进行的 {job.quality} 档试听生成已达上限"
                        f"（fast={MAX_CONCURRENT_FAST}, high={MAX_CONCURRENT_HIGH}），"
                        f"本次未启动合成",
                        voice_id=job.voice_id, quality=job.quality, retry_after=10)
    else:
        try:
            # PY-322: 排队等槽位期间已被判死 -> 绝不进引擎，省掉一次白烧
            if job.abandoned.is_set():
                logger.warning(f"[preview] seq={job.seq} {job.key} 判死时尚未进入引擎，"
                               f"本次不启动合成")
                return
            result = _run_generation(job)
        except BaseException as e:                # 线程里绝不冒异常：留结构化失败给调用方
            logger.exception(f"[preview] 生成任务未捕获异常 {job.voice_id}")
            result = _error("synthesis_failed",
                            f"试听生成线程异常: {type(e).__name__}: {e}",
                            voice_id=job.voice_id)
        finally:
            sem.release()
    if job.abandoned.is_set():
        # 迟到：预算已经判死，保留 timeout 结论（_publish 已拒绝落盘）
        logger.warning(f"[preview] seq={job.seq} {job.voice_id}/{job.quality} "
                       f"在判死之后才返回（{result.get('code')}），结论保持 timeout")
        return
    with _jobs_lock:
        if not job.done.is_set():
            if not result.get("ok"):
                result["_failed_at"] = time.time()
            job.result = result
            job.done.set()
        else:
            result = job.result or {}
    timer = job.timer
    if timer is not None:
        timer.cancel()
    if result.get("ok"):
        logger.info(f"[preview] seq={job.seq} {job.voice_id}/{job.quality} 任务成功")
    else:
        logger.warning(f"[preview] seq={job.seq} {job.voice_id}/{job.quality} "
                       f"任务失败 code={result.get('code')}: {result.get('message')}")


# ----------------------------------------------------------- 缓存读侧（A4）
def _cached_usable(voice_id: str, quality: str) -> Optional[str]:
    """已有生成缓存是否可信：文件在 + RIFF + 过试听口径的门。

    不过门就当场删掉缓存对（"淘汰该缓存文件"），下次请求重新生成。
    """
    from backend.services.voice_service import voice_service
    wav_path, _ = cache_paths(voice_id, quality)
    try:
        if not wav_path.exists() or wav_path.stat().st_size <= 1024:
            return None
        with open(wav_path, "rb") as f:
            if f.read(4) != _WAV_MAGIC:
                logger.warning(f"[preview] 缓存 {wav_path.name} 头部不是 RIFF，淘汰")
                _drop_cache(voice_id, quality)
                return None
    except OSError as e:
        logger.warning(f"[preview] 读缓存失败 {wav_path}: {e}")
        return None
    ok, report, err = voice_service.is_preview_grade(str(wav_path))
    if not ok:
        logger.warning(f"[preview] 缓存 {wav_path.name} 未过试听质量门，淘汰并重新生成: "
                       f"{err} (peak={(report or {}).get('peak')})")
        _drop_cache(voice_id, quality)
        return None
    return str(wav_path)


# ------------------------------------------------------------- 对外入口
def resolve_preview(voice_id: str, wait_s: float = DEFAULT_WAIT_S,
                    force: bool = False, quality: str = "fast") -> dict:
    """取一条音色可播的试听音频：自带样本 -> 缓存 -> 速度档现合成。

    Args:
        voice_id: 音色行 id。
        wait_s: 本次请求最多等待现合成多少秒（0 = 只查文件/缓存，不等）。
        force: True 时忽略已有缓存，重新生成（`?regenerate=1`）。
        quality: `fast`(默认，edge-tts 速度档) | `high`(该音色绑定的引擎，需预算)。
    Returns:
        {"ok": True, "path", "origin", "engine", "voice", "seed", "meta", "quality"}
        或 {"ok": False, "code", "message", "status", ...}

    PY-322：某个 `voice_id::quality` 刚被预算判死、它的合成线程**还没确认退出**时，
    本键处于 poisoned 态 —— 此时**不排队也不重开任务**，立刻返回
    `code="engine_busy_cancelling"`（429），线程退出后自动解封。
    """
    quality = (quality or "fast").strip().lower()
    if quality not in ("fast", "high"):
        quality = "fast"
    if not voice_id or not _SAFE_ID.match(voice_id):
        return _error("voice_not_found", "音色 id 非法", voice_id=voice_id)
    row = _load_row(voice_id)
    if row is None:
        return _error("voice_not_found", f"音色 {voice_id} 不存在", voice_id=voice_id)

    if not force:
        # 出口 1：音色行自带样本
        sample = _native_sample(voice_id)
        if sample:
            return {"ok": True, "path": sample, "origin": "voice_sample",
                    "quality": quality, "engine": getattr(row, "engine", None),
                    "voice": None, "seed": None,
                    "meta": _read_meta(voice_id, quality)}
        # 出口 2：本模块此前生成的缓存（二次点击 ≤1s 就走这里）
        cached = _cached_usable(voice_id, quality)
        if cached:
            meta = _read_meta(voice_id, quality) or {}
            return {"ok": True, "path": cached, "origin": "generated_cache",
                    "quality": quality,
                    "engine": meta.get("engine") or ("edgetts" if quality == "fast" else None),
                    "voice": meta.get("edge_voice"),
                    "seed": meta.get("seed"), "meta": meta}

    budget = HIGH_BUDGET_S if quality == "high" else FAST_BUDGET_S
    cooldown = HIGH_RETRY_COOLDOWN_S if quality == "high" else FAST_RETRY_COOLDOWN_S
    key = f"{voice_id}::{quality}"
    now = time.time()
    with _jobs_lock:
        job = _jobs.get(key)
        # ---------------- PY-322: poisoned 门（判死 ≠ 执行者已停）
        # 上一票已被预算判死、它的线程还在引擎里退出来路上 —— 这时重开一票（或让
        # 等待者再挂到同一个已作废的结论上）等于把第二个"用户已放弃"的请求
        # 焊在同一条重引擎串行锁上（实测放大成 2.5 小时的 engine_busy 429）。
        # 口径边界：高档的**冷却期内非 regenerate 请求**仍复用 timeout 结论
        # （那是 #96 PREVIEW-GATE 定的"冷却期内不再重跑"，不排队、不重烧，保持不动）；
        # 其余三类（要重开任务的、速度档的）一律给可区分的 engine_busy_cancelling。
        # 这段必须在摘表**之前**判：被拒的请求不该顺手销毁冷却结论。
        reuse_timeout = False
        if job is not None and job.done.is_set() and not (job.result or {}).get("ok"):
            reuse_timeout = (quality == "high" and not force
                             and now - ((job.result or {}).get("_failed_at") or 0)
                             <= cooldown)
        stale = _poisoned.get(key)
        if stale is not None:
            exited = stale.worker_exited.is_set()
            overdue = (now - stale.poisoned_at) > CANCEL_POISON_MAX_S
            if exited or overdue:
                _poisoned.pop(key, None)
                if not exited:
                    logger.error(f"[preview] seq={stale.seq} {key} 判死后 "
                                 f"{CANCEL_POISON_MAX_S}s 仍未确认退出（引擎无可取消的"
                                 f"检查点？）-> 强制解封 poisoned，后续请求可能仍要排队")
                stale = None
            elif not reuse_timeout:
                return _poison_error(stale)

        if job is not None and job.done.is_set():
            res = job.result or {}
            failed_at = res.get("_failed_at") or 0
            # 冷却期内的旧失败直接复用（B6：后续请求拿到同一个可区分错误码），
            # 冷却期过掉才允许重开一个任务。
            if not res.get("ok") and (force or (
                    failed_at and now - failed_at > cooldown)):
                _jobs.pop(key, None)
                job = None
            force = False                      # 已经有一个失败/成功结论在手，别重复起
        if job is None and force:
            _jobs.pop(key, None)
            job = None
        if job is None:
            job = _Job(voice_id, quality, budget)
            _jobs[key] = job
            job.thread = threading.Thread(target=_finish, args=(job,),
                                          name=f"voice-preview-{voice_id[:8]}-{quality}",
                                          daemon=True)
            job.timer = threading.Timer(budget, _abandon, args=(job,))
            job.timer.daemon = True
            job.timer.start()
            job.thread.start()
            logger.info(f"[preview] 启动生成 seq={job.seq} {key} budget={budget}s "
                        f"wait={wait_s}s")

    if wait_s <= 0:
        if job.done.is_set():
            res = job.result or {}
            if res.get("ok"):
                res = dict(res)
                res["origin"] = "generated_now"
                return res
            return res
        return _error("in_progress",
                      "该音色暂无样本，已启动速度档后台生成，请稍后重试",
                      voice_id=voice_id, quality=quality, retry_after=3)

    # 等待上限 = min(调用方给的 wait_s, 预算+一点收尾余量)
    remaining = max(0.0, min(wait_s, job.deadline + 1.0 - time.time()))
    if not job.done.wait(timeout=remaining):
        if time.time() >= job.deadline:
            _abandon(job, reason="waiter")       # 预算已到：立刻判死并唤醒
        res = job.result or {}
        if res and not res.get("ok"):
            return res
        return _error("in_progress",
                      f"该音色没有现成样本，速度档后台生成中（本次已等 "
                      f"{int(wait_s)}s，预算 {job.budget_s:.0f}s 还剩 "
                      f"{max(0.0, job.deadline - time.time()):.0f}s）",
                      voice_id=voice_id, quality=quality, retry_after=3,
                      next="再点一次即可：默认档预算内完成会直接命中缓存；"
                           "超过预算会被判 timeout，不会再无界地跑")

    result = job.result or {}
    if not result:
        return _error("synthesis_failed", "试听生成任务未返回结果",
                      voice_id=voice_id, quality=quality)
    with _jobs_lock:
        if result.get("ok"):
            _jobs.pop(key, None)                 # 成功即摘表（下次走文件缓存）
    return result


def running_jobs() -> dict:
    """当前在跑的生成任务（诊断/自测用）。"""
    with _jobs_lock:
        return {k: {"elapsed": round(time.time() - j.started_at, 1),
                    "budget_s": j.budget_s, "seq": j.seq,
                    "abandoned": j.abandoned.is_set(),
                    "cancel_signalled": j.cancel_event.is_set(),
                    "cancellable": bool(j.cancellable),
                    "worker_exited": j.worker_exited.is_set()}
                for k, j in _jobs.items() if not j.done.is_set()}


def poisoned_jobs() -> dict:
    """PY-322：处于 poisoned 态的键（判死但执行线程未确认退出）。"""
    with _jobs_lock:
        return {k: {"seq": j.seq, "since": round(time.time() - j.poisoned_at, 1),
                    "cancellable": bool(j.cancellable),
                    "worker_exited": j.worker_exited.is_set()}
                for k, j in _poisoned.items()}


def reset_jobs() -> None:
    """清空任务表与 poisoned 表（单测/热重载用；不取消已在跑的线程，只摘索引）。"""
    with _jobs_lock:
        _jobs.clear()
        _poisoned.clear()
