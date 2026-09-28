"""PY-348：让"实例自己消失"下次必可判 —— 日志默认落盘 + 心跳/退出留痕。

PY-346 的定罪卡在哪：生产 :8000 实例 06:40~06:54 之间没了，但它的 stdout/stderr
一个字节都没落盘（`PYY_LOG_FILE` 默认空 ⇒ 只写 stderr，而 stderr 在启动器里被丢进
控制台窗口），所以 OOM / 未捕获异常 / 被外部 taskkill 三种死法在盘上留下的是同一张
"什么都没有"。加看门狗只是把它拉回来，仍然解释不了它为什么走。

本模块补的是判别手段，三件事各自对应一种死法：

1. `resolve_log_file()`：未显式给 `PYY_LOG_FILE` 时自动落到 `logs/backend-<时刻>-p<pid>.log`
   ⇒ 下次无论哪种死法，traceback 都在文件里。
2. `faulthandler.enable()`（由 main.py 挂在同一枚文件流上）⇒ 原生层崩溃（段错误/栈溢出）
   也有栈可引。
3. `logs/liveness.log` 心跳 + `atexit` 的 exit 行 ⇒ 即使什么异常都没有，也能按
   "最后一条心跳的时刻/内存" 与 "有没有 exit 行" 三分：
   有 exit = 自己退的（异常或优雅关闭，配 1 能读到原因）；
   无 exit + 最后心跳里 free 很低 = 容量被打死；
   无 exit + 最后心跳 free 正常 = 外部强杀（taskkill / 结束任务），进程没机会写任何东西。

测试进程里三件全关：pytest 会 import backend.main 上百次，心跳线程和日志文件会在
纯树上凭空造出脏数据（口径见 memory「复证运行本身是写入源」）。
"""

from __future__ import annotations

import atexit
import json
import os
import threading
from datetime import datetime
from pathlib import Path

LOGS_DIRNAME = "logs"
# 内容是 JSONL，但名字必须带 .log —— .gitignore 只收 *.log，别把心跳留成待提交的脏文件
LIVENESS_FILENAME = "liveness.log"
HEARTBEAT_INTERVAL_S = 30.0
_MAX_LIVENESS_BYTES = 5 * 1024 * 1024
_KEEP_LINES_ON_ROTATE = 500

_state_lock = threading.Lock()
_heartbeat_thread: threading.Thread | None = None
_started = False


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def is_test_process() -> bool:
    # PYTEST_CURRENT_TEST 只在执行单个用例时有值，PYTEST_VERSION 在收集期就有
    return bool(os.getenv("PYTEST_CURRENT_TEST") or os.getenv("PYTEST_VERSION"))


def liveness_path() -> Path:
    return repo_root() / LOGS_DIRNAME / LIVENESS_FILENAME


def default_log_file() -> Path:
    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    return repo_root() / LOGS_DIRNAME / f"backend-{stamp}-p{os.getpid()}.log"


def resolve_log_file(env: dict | None = None) -> Path | None:
    """返回日志文件路径；None = 本次不落文件（仅测试进程会走到这一支）。"""
    env = os.environ if env is None else env
    explicit = (env.get("PYY_LOG_FILE") or "").strip()
    if explicit:
        return Path(explicit)
    if is_test_process():
        return None
    return default_log_file()


def _memory_snapshot() -> dict:
    try:
        import psutil  # type: ignore
    except Exception:
        return {"rss_mb": None, "free_gb": None}
    proc = psutil.Process(os.getpid())
    return {
        "rss_mb": round(proc.memory_info().rss / 2**20, 1),
        "free_gb": round(psutil.virtual_memory().available / 2**30, 2),
    }


def _append(line: str, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(line)
        fh.flush()
        os.fsync(fh.fileno())  # 硬杀也带不走这一行，否则死亡下界会漂到下一个 flush


def record(event: str, path: Path | None = None, **fields) -> dict:
    entry = {
        "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
        "pid": os.getpid(),
        "event": event,
    }
    entry.update(_memory_snapshot())
    entry.update(fields)
    target = path or liveness_path()
    try:
        _append(json.dumps(entry, ensure_ascii=False) + "\n", target)
    except Exception:
        pass  # 留痕失败绝不能拖垮服务本体
    return entry


def _rotate(path: Path) -> None:
    try:
        if path.exists() and path.stat().st_size > _MAX_LIVENESS_BYTES:
            lines = path.read_text(encoding="utf-8").splitlines()[-_KEEP_LINES_ON_ROTATE:]
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except Exception:
        pass


def _heartbeat_loop(interval_s: float, path: Path) -> None:
    while True:
        threading.Event().wait(interval_s)
        record("heartbeat", path=path)


def start(port: int | None = None, base_dir: str | None = None,
          interval_s: float = HEARTBEAT_INTERVAL_S) -> threading.Thread | None:
    """开一枚心跳线程并登记退出留痕；重复调用/测试进程返回 None。"""
    global _heartbeat_thread, _started
    with _state_lock:
        if _started or is_test_process():
            return None
        _started = True
        path = liveness_path()
        _rotate(path)
        record("start", path=path, port=port, base_dir=base_dir,
               log_dir=str(path.parent))
        atexit.register(_record_exit)
        thread = threading.Thread(
            target=_heartbeat_loop, args=(interval_s, path),
            name="py348-liveness", daemon=True)
        thread.start()
        _heartbeat_thread = thread
        return thread


def _record_exit() -> None:
    record("exit")


def read_last_events(count: int = 3, path: Path | None = None) -> list[dict]:
    """读最后 count 条留痕（判死时用；文件缺失返回空表而不是抛错）。"""
    target = path or liveness_path()
    try:
        lines = [l for l in target.read_text(encoding="utf-8").splitlines() if l.strip()]
    except Exception:
        return []
    out = []
    for line in lines[-count:]:
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out
