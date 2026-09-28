"""
TTS 模型预热与加载状态机 (PY-115 / US-C1)

背景（qa/issues-round1b.md PY-115）：
  VoxCPM2 CPU 冷启动实测 220.6s（4.5GB safetensors + AudioVAE + warm-up），
  旧实现把它压在 `_stage_synthesize` 的第一句之前同步执行，期间
  `/tasks/{id}/progress` 一直停在 `stage=import progress=0.0`，
  约 4 分钟无任何反馈 —— 用户视角等同卡死。

本模块提供：
  1. 后台预热：`start(name)` 幂等地在守护线程里加载模型（后端启动 / 任务创建时调用），
     加载路径与任务路径共用 engine_registry 的加载锁，绝不重复加载两份权重。
  2. 状态机快照 `snapshot(engine_name=None)`：idle / loading / ready / failed
     + 已耗时 + 错误原因 + **per-engine 槽位表**，供 `/api/system/model_status`
     与进度事件（"模型加载中"）消费。
  3. `wait_terminal(timeout, engine_name)`：任务侧等待**自己那个引擎**进入终态。

US-C1（波次 1 泳道 C，机制出处 ANALYSIS-OPENVOICE OV-P0-1/M5）：
  本模块**过去是单槽**——一个 `_engine_name` + 一个共享 `_terminal` Event。事故链：
    任务 A 正在后台加载本地引擎（state=LOADING, engine=A）
    → 任务 B 需要本地引擎 C：`start(C)` 命中 "STATE_LOADING 就 return" 早退，
      C 从未被后台加载
    → B 的 `wait_terminal()` 等的是**共享 Event**，A 一加载完就置位
    → B 看到 `state=="ready"`（假 ready），`_acquire_engine` 放行
    → 放行那行在 async 协程里**同步** `get_loaded(C)`，冷启动 220~460s 压在事件循环上
    → 整个 uvicorn 停摆：所有任务的 WS 心跳/进度接口一起哑火（PY-115 修掉的症状
      从另一条路复活并升级为全局故障）。
  现在改为 `dict[engine_name] -> EngineSlot`：每槽独立 state/Event/线程/耗时/错误，
  `start` 的幂等只看**自己那个槽**；加载仍然串行（`engine_registry._load_lock`
  保内存峰值约束），但等待按槽位 Event，且 `queue_ahead`（前面还压着几个引擎在加载）
  可见 —— 只是把卡死变慢是不够的。
  配套断言在 `conversion_workflow._acquire_engine`：`snap["engine"] == engine_name`
  不符即判失败，绝不落到"就地同步加载"那条路。
  `/api/system/model_status` 的旧载荷形状不破：无参 `snapshot()` 仍在顶层给出
  state/engine/load_seconds/elapsed_seconds/error/skipped_reason/message 七个旧键
  （取"主槽"= 最近一次 start 的引擎），新增 `slots` / `primary` / `detail`。

环境约束说明（ENG-PRELOAD 改判，2026-09-22）：
  **启动即无条件触发预装载** —— 内存不足不再是默认的跳过理由。
  历史形态是"可用内存 < `PYY_PRELOAD_MIN_FREE_GB`（默认 6GB）就把启动预热判成
  `idle` + `skipped_reason`"，而本机常年在 3~4GB 可用 ⇒ 每次启动都被静默跳过，
  voxcpm 永远不在启动时装载，用户视角就是"引擎没装载"。
  现在门限只在**用户显式设置该环境变量**时生效（`memory_gate_setting()` 在每次
  调用时读环境，不再 import 期定死）；`PYY_PRELOAD_MODEL=0` 同理是用户显式关。
  装载失败不再退回 `idle`：后台线程按 `RETRY_LIMIT` 有上限地重试，仍失败则落
  `state="failed"` + `error=<真实异常原文>`，由 `/api/system/model_status` 直出。
  内存确实是失败的常见真因，所以快照额外带 `gate`（门限是否生效 + 观测到的可用
  内存）与 `attempts`，让"为什么没装起来"可查、可归因，不靠猜。
"""

import gc
import logging
import os
import threading
import time
from typing import Optional

logger = logging.getLogger(__name__)

STATE_IDLE = "idle"
STATE_LOADING = "loading"
STATE_READY = "ready"
STATE_FAILED = "failed"
_TERMINAL_STATES = (STATE_READY, STATE_FAILED)

# 预热开关 / 内存门限。**只在调用时读环境**，这样"用户设没设"是事实而不是 import 时序。
_PRELOAD_ENV = "PYY_PRELOAD_MODEL"
_GATE_ENV = "PYY_PRELOAD_MIN_FREE_GB"
_OFF = ("0", "false", "no", "off")
# 旧默认门限（GB）：仅用于在快照里说明"现在观测到的内存低于旧默认值、但仍照装"，
# 不再参与任何判定 —— 留着是为了归因诚实，不是开关。
_LEGACY_DEFAULT_GATE_GB = 6.0
# ENG-PRELOAD: 装载失败的重试预算（固定常量，不再新增 feature flag）。
# 一次启动最多 1 + RETRY_LIMIT 次尝试：OOM/页面文件太小这类失败是"值得再试一次"的，
# 但无上限重试会在只剩 3GB 的机器上把内存与启动时间一起烧光。
RETRY_LIMIT = 2
RETRY_DELAY_S = 10.0


def available_memory_bytes() -> Optional[int]:
    """可用物理内存（字节）；探测不到时返回 None（按"允许预热"处理）。"""
    try:
        import psutil  # type: ignore
        return int(psutil.virtual_memory().available)
    except Exception:
        pass
    try:  # Windows 无 psutil 时的兜底
        import ctypes

        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        st = MEMORYSTATUSEX()
        st.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
            return int(st.ullAvailPhys)
    except Exception:
        pass
    return None


def free_gb() -> Optional[float]:
    """可用物理内存（GB，保留 2 位）；探测不到返回 None。"""
    avail = available_memory_bytes()
    return None if avail is None else round(avail / 1024 ** 3, 2)


def preload_enabled() -> bool:
    """用户是否**显式**关掉了启动预热（`PYY_PRELOAD_MODEL=0/false/no/off`）。

    缺省 = 开。这条与内存门限无关：它是用户自己拉闸，不是我们替他决定。
    """
    return os.getenv(_PRELOAD_ENV, "1").strip().lower() not in _OFF


def memory_gate_setting() -> dict:
    """内存门限的**当前**生效情况（ENG-PRELOAD：默认不生效，显式设置才生效）。

    返回字典（进快照，便于对外自证）：
      applied      本次是否真的用门限拦一道
      min_free_gb  生效时的门限值（GB）；未设/非法为 None
      source       一句话归因（哪个环境变量、设成了什么）
      free_gb      本次观测到的可用内存（GB）；探测不到为 None
      note         低于旧默认门限时的说明（只解释，不拦路）
    """
    raw = os.getenv(_GATE_ENV)
    if raw is None or not raw.strip():
        gate = {"applied": False, "min_free_gb": None,
                "source": f"{_GATE_ENV} 未设置 -> 启动即无条件预装载（ENG-PRELOAD）"}
    else:
        try:
            want = float(raw)
        except ValueError:
            gate = {"applied": False, "min_free_gb": None,
                    "source": f"{_GATE_ENV}={raw!r} 不是数字 -> 门限忽略，照常装载"}
        else:
            gate = {"applied": want > 0, "min_free_gb": want if want > 0 else None,
                    "source": f"{_GATE_ENV}={raw}（用户显式设置）"}
            if not gate["applied"]:
                gate["source"] += " -> 门限 <=0 视为不设门限"
    gate["free_gb"] = free_gb()
    if (gate["free_gb"] is not None and not gate["applied"]
            and gate["free_gb"] < _LEGACY_DEFAULT_GATE_GB):
        gate["note"] = (f"可用内存 {gate['free_gb']}GB 低于旧默认门限 "
                        f"{_LEGACY_DEFAULT_GATE_GB}GB：按启动即装载的要求**照装**，"
                        f"可能换页变慢；要拦这道请显式设置 {_GATE_ENV}")
    return gate


class EngineSlot:
    """**一个引擎**的加载槽：自己的状态机 + 自己的终态 Event。

    只在 `ModelPreloader._lock` 下改写字段（`_terminal.set()` 除外，它由加载线程在
    持锁段内调用）。Event 的语义严格限定为"本槽进入终态（ready/failed）或被重置"，
    因此别家引擎加载完不可能把本槽叫醒 —— 这正是 US-C1 要买的性质。
    """

    __slots__ = ("engine_name", "state", "started_at", "finished_at",
                 "load_seconds", "error", "thread", "skipped_reason",
                 "terminal", "detail", "seq", "attempts", "gate")

    def __init__(self, engine_name: str, seq: int = 0):
        self.engine_name = engine_name
        self.state = STATE_IDLE
        self.started_at: Optional[float] = None
        self.finished_at: Optional[float] = None
        self.load_seconds: Optional[float] = None
        self.error: Optional[str] = None
        self.thread: Optional[threading.Thread] = None
        self.skipped_reason: Optional[str] = None
        self.detail: Optional[dict] = None      # US-C6: 适配器交来的分阶段计时
        self.seq = seq                          # 启动次序（算 queue_ahead 用）
        self.attempts = 0                       # ENG-PRELOAD: 已尝试装载的次数
        self.gate: Optional[dict] = None        # ENG-PRELOAD: 门限生效情况自证
        self.terminal = threading.Event()
        self.terminal.set()                     # 空槽 = 没有在飞的东西，不拦人

    # ---- 查询（调用方持锁） ----
    def elapsed(self) -> Optional[float]:
        if self.started_at is None:
            return None
        end = self.finished_at if self.finished_at else time.time()
        return round(end - self.started_at, 1)

    def message(self) -> str:
        budget = RETRY_LIMIT + 1
        if self.state == STATE_LOADING:
            e = 0.0 if self.started_at is None else time.time() - self.started_at
            retry = (f"，第 {self.attempts}/{budget} 次尝试" if self.attempts > 1 else "")
            return (f"模型加载中（{self.engine_name}，已用 {e:.0f}s{retry}，"
                    f"CPU 冷启动约 3-4 分钟）")
        if self.state == STATE_READY:
            return (f"模型已就绪（{self.engine_name}，加载耗时 {self.load_seconds}s）")
        if self.state == STATE_FAILED:
            return (f"模型加载失败（{self.engine_name}，已尝试 {self.attempts} 次，"
                    f"上限 {budget} 次内未成功）：{self.error}")
        if self.state == STATE_IDLE and self.skipped_reason:
            return (f"模型未预装载（{self.engine_name}）：{self.skipped_reason}"
                    if self.engine_name else f"模型未预装载：{self.skipped_reason}")
        return f"模型未加载（{self.engine_name}）" if self.engine_name else "模型未加载"

    def view(self, queue_ahead: int = 0) -> dict:
        return {
            "state": self.state,
            "engine": self.engine_name,
            "load_seconds": self.load_seconds,
            "elapsed_seconds": self.elapsed(),
            "error": self.error,
            "skipped_reason": self.skipped_reason,
            "queue_ahead": queue_ahead,        # 前面还有几个引擎在加载（串行队列位次）
            "detail": self.detail,             # US-C6: "CPU 已跳过编译，纯加载 Xs" 之类
            "attempts": self.attempts,         # ENG-PRELOAD: 含重试的真实尝试次数
            "retry_limit": RETRY_LIMIT,        # ENG-PRELOAD: 重试预算（不是无限吃内存）
            "gate": self.gate,                 # ENG-PRELOAD: 门限生不生效、可用内存多少
            "message": self.message(),
        }


class ModelPreloader:
    """单例：后台加载 TTS 模型并**按引擎**对外暴露加载状态。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._slots: dict = {}
        self._primary: Optional[str] = None    # 最近一次 start 的引擎（旧单槽语义）
        self._seq = 0

    # ---------- 槽位 ----------
    def _slot_locked(self, engine_name: str) -> EngineSlot:
        """取（或建）某引擎的槽。调用方必须已持 `self._lock`。"""
        slot = self._slots.get(engine_name)
        if slot is None:
            slot = EngineSlot(engine_name)
            self._slots[engine_name] = slot
        return slot

    def _view_locked(self, engine_name: str) -> dict:
        """某引擎的槽视图 —— **只读**：查询一个从未 start 过的引擎不得凭空造槽。"""
        slot = self._slots.get(engine_name)
        if slot is None:
            return EngineSlot(engine_name).view(0)
        return slot.view(self._queue_ahead_locked(engine_name))

    def slot_names(self) -> list:
        with self._lock:
            return sorted(self._slots)

    def queue_position(self, engine_name: str) -> int:
        """本引擎在"串行加载队列"里前面压着几个（0=轮到它/它已就绪）。"""
        with self._lock:
            return self._queue_ahead_locked(engine_name)

    def _queue_ahead_locked(self, engine_name: str) -> int:
        me = self._slots.get(engine_name)
        if me is None or me.state in _TERMINAL_STATES or me.state == STATE_IDLE:
            return 0
        return sum(1 for other in self._slots.values()
                   if other is not me and other.state == STATE_LOADING
                   and other.seq < me.seq)

    # ---------- 控制 ----------
    def start(self, engine_name: str, respect_memory_gate: bool = False) -> str:
        """幂等启动后台加载（只幂等**自己那个槽**）。返回该槽调用后的状态。

        `respect_memory_gate=True` 用于"启动预热"。**ENG-PRELOAD 后它的确切含义是**：
        "按用户显式配置的规则决定要不要拦这一道" —— 用户没设 `PYY_PRELOAD_MIN_FREE_GB`
        就一律不拦（启动即无条件装载），设了就尊重用户值。任务路径传 False，
        拦不拦都一样要加载（任务反正要用模型）。

        失败不再退回 idle：`_load_with_retry` 按 `RETRY_LIMIT` 有上限地重试，
        终态 ready/failed 带真实原因。
        """
        with self._lock:
            slot = self._slot_locked(engine_name)
            self._primary = engine_name
            if slot.state == STATE_READY:
                return slot.state
            if slot.state == STATE_LOADING:
                return slot.state
            if respect_memory_gate:
                if not preload_enabled():
                    slot.skipped_reason = (f"{_PRELOAD_ENV}=0 已关停启动预热"
                                           f"（用户显式设置）")
                    slot.gate = {"applied": False, "min_free_gb": None,
                                 "source": f"{_PRELOAD_ENV} 关停了启动预热"}
                    slot.attempts = 0
                    logger.info(f"[Preload] 跳过: {slot.skipped_reason}")
                    return STATE_IDLE
                gate = memory_gate_setting()
                slot.gate = dict(gate)
                if gate["applied"]:
                    avail = available_memory_bytes()
                    if avail is not None and avail < gate["min_free_gb"] * (1024 ** 3):
                        slot.skipped_reason = (
                            f"可用内存 {avail / 1024 ** 3:.1f}GB < 用户显式设置的门限 "
                            f"{gate['min_free_gb']}GB（{gate['source']}），跳过启动预热"
                            f"（任务真正需要时仍会加载）")
                        logger.warning(f"[Preload] {slot.skipped_reason}")
                        return STATE_IDLE
                # 门限没生效 = 启动即装载。可用内存偏低只在日志里留痕，不改判。
                slot.skipped_reason = None
                if gate.get("note"):
                    logger.warning(f"[Preload] {engine_name}: {gate['note']}")
                else:
                    logger.info(f"[Preload] {engine_name}: {gate['source']}"
                                f" | 可用内存 {gate.get('free_gb')}GB")

            from modules.tts_engine.engine_registry import engine_registry
            self._seq += 1
            slot.state = STATE_LOADING
            slot.error = None
            slot.load_seconds = None
            slot.detail = None
            slot.started_at = time.time()
            slot.finished_at = None
            slot.seq = self._seq
            slot.attempts = 0
            slot.terminal.clear()               # 只关自己这一家的门

            def _load_with_retry():
                """1 + RETRY_LIMIT 次尝试；成功即返回，用尽预算则交出真实异常。"""
                attempt = 0
                while True:
                    attempt += 1
                    with self._lock:
                        slot.attempts = attempt
                    err = None
                    loaded = None
                    try:
                        loaded = engine_registry.get_loaded(engine_name, strict=True)
                    except Exception as e:      # EngineLoadError / ImportError / OOM...
                        err = e
                    if err is None:
                        # US-C6: 适配器若交出分阶段计时（load_detail），随快照一起对外
                        detail = None
                        if loaded is not None:
                            try:
                                detail = getattr(loaded, "load_detail", None)
                            except Exception:   # pragma: no cover - 尽力而为
                                detail = None
                        return None, detail, attempt
                    logger.error(
                        f"[Preload] 引擎 '{engine_name}' 第 {attempt}/{RETRY_LIMIT + 1} "
                        f"次装载失败: {type(err).__name__}: {err} "
                        f"| 可用内存 {free_gb()}GB")
                    if attempt > RETRY_LIMIT:
                        return err, None, attempt
                    with self._lock:            # 途中可见：仍在 loading，但已换第 N 次
                        slot.error = f"{type(err).__name__}: {err}"
                    gc.collect()                # 让上一次失败留下的半成品先还内存
                    time.sleep(max(0.0, float(RETRY_DELAY_S)))

            def _run():
                err, detail, attempt = _load_with_retry()
                finished = free_gb()            # 装载结束时的可用内存（归因证据）
                with self._lock:
                    slot.finished_at = time.time()
                    slot.load_seconds = round(slot.finished_at - (slot.started_at or 0), 1)
                    slot.detail = detail
                    slot.attempts = attempt
                    if slot.gate is not None:
                        slot.gate = dict(slot.gate)
                        slot.gate["free_gb_after"] = finished
                    if err is None:
                        slot.state = STATE_READY
                        slot.error = None
                        logger.info(
                            f"[Preload] 引擎 '{engine_name}' 预热完成 "
                            f"(尝试 {attempt} 次 / {slot.load_seconds}s"
                            + (f" | {detail.get('message')}" if isinstance(detail, dict)
                               and detail.get("message") else "")
                            + f" | 装载结束可用内存 {finished}GB)")
                    else:
                        slot.state = STATE_FAILED
                        slot.error = (f"{type(err).__name__}: {err} "
                                      f"(已尝试 {attempt} 次，装载结束可用内存 "
                                      f"{finished}GB)")
                        logger.error(
                            f"[Preload] 引擎 '{engine_name}' 预热失败 "
                            f"({attempt} 次尝试用尽): {slot.error}")
                    slot.terminal.set()

            slot.thread = threading.Thread(target=_run,
                                           name=f"pyy-preload-{engine_name}",
                                           daemon=True)
            slot.thread.start()
            logger.info(f"[Preload] 已启动后台加载: engine='{engine_name}'")
            return slot.state

    def reset(self, engine_name: str = None):
        """重置槽位：给 engine_name 只清那一个，不给则清全部（旧语义）。"""
        with self._lock:
            targets = ([engine_name] if engine_name is not None
                       else [s.engine_name for s in self._slots.values()])
            for name in targets:
                slot = self._slots.get(name)
                if slot is None:
                    continue
                slot.state = STATE_IDLE
                slot.error = None
                slot.load_seconds = None
                slot.detail = None
                slot.started_at = None
                slot.finished_at = None
                slot.skipped_reason = None
                slot.attempts = 0
                slot.gate = None
                slot.terminal.set()
            if engine_name is None:
                self._slots = {}
                self._primary = None
            elif self._primary == engine_name:
                self._primary = None

    # ---------- 查询 ----------
    @property
    def state(self) -> str:
        """主槽状态（旧单槽语义；要具体引擎请用 `snapshot(engine)`）。"""
        with self._lock:
            return self._primary_slot_locked().state

    def _primary_slot_locked(self) -> EngineSlot:
        if self._primary is not None:
            return self._slots[self._primary]
        # 没有任何 start 过的主槽：取一个已存在的槽，都没有则给一个匿名空槽
        if self._slots:
            return next(iter(self._slots.values()))
        return EngineSlot(None)

    def wait_terminal(self, timeout: float = None, engine_name: str = None) -> bool:
        """等待**指定引擎**的加载进入终态。True=已终态，False=超时仍在加载。

        `engine_name=None` 等主槽（向后兼容旧单槽调用）。空槽/已终态槽直接返回 True
        （没有在飞的东西，不拦人）；LOADING 才真正等 Event。
        """
        with self._lock:
            if engine_name is not None:
                slot = self._slots.get(engine_name)
                if slot is None:
                    return True          # 没在飞的东西（idle 槽）—— 不拦人，但也不报 ready
                ev = slot.terminal
            else:
                ev = self._primary_slot_locked().terminal
        return ev.wait(timeout)

    def snapshot(self, engine_name: str = None) -> dict:
        """状态快照。

        - 给 `engine_name` → 那一个引擎的槽视图（未 start 过时 state=idle）。
        - 不给 → 主槽视图 + `slots`（全部引擎）+ `primary`，**旧七个键原样保留**
          （`/api/system/model_status` 的 `model` 字段因此不破形状，见 main.py:156）。
        """
        with self._lock:
            if engine_name is not None:
                return self._view_locked(engine_name)
            primary = self._primary_slot_locked()
            view = primary.view(self._queue_ahead_locked(primary.engine_name))
            view["primary"] = self._primary
            view["slots"] = {
                name: s.view(self._queue_ahead_locked(name))
                for name, s in self._slots.items()
            }
            return view

    def engine(self, engine_name: str, strict: bool = True):
        """取已加载引擎（预热命中则即刻返回，不重复付冷启动）。"""
        from modules.tts_engine.engine_registry import engine_registry
        return engine_registry.get_loaded(engine_name, strict=strict)


# 全局单例
model_preloader = ModelPreloader()
