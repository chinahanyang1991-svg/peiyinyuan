#!/usr/bin/env python3
"""
配音员 · 全栈一键启动器 v3 (#90 LAUNCHER-V3)

双击 `一键启动.bat` 即用（唯一入口；`start-all.cmd` 是同一实现的命令行壳）：
一条命令起 **后端 + 前端 + GPT-SoVITS**，
每条腿一枚**带标题的可见控制台窗口**（`PY-Backend` / `PY-Frontend` / `PY-GSV`），
窗口里实时滚动该模块的日志，同时**逐字落盘** `peiyinyuan/logs/<leg>-<port>.log`。
起完自动跑**健康门**并逐项打 PASS / FAIL / SKIP：

    1) GET /api/health                  = 200
    2) GET /api/system/engines          问在跑的实例它是哪一档，引擎门按档位分叉：
       在线档（默认）= 在线引擎可用即 PASS，**不等 5GB 本地权重**；
       本地档 / 读不到档位 = 回到 model_status 的 voxcpm ready 硬门
       —— 装载期"端口在听但接口不答"记为**饿死**并继续等（#86 的代价，量化报出来），
          只有 :8000 压根无监听 / 中途掉监听才判 FAIL
    3) GET http://localhost:5173        = 200   （vite 只绑 ::1，必须用 localhost）
    4) GET http://127.0.0.1:9880/docs   = 200   （GSV api_v2，冷启动给 --gsv-timeout 预算）
    5) MinerU :8001 / freellmapi :3001 / Redis :6379 —— **只探不谎**：
       在跑就纳管显示，没跑就 SKIP 并给出原因，绝不假装在跑。

端口占用先归因（谁的 PID + 命令行原文），只有命令行明确是本产品进程的才复用或清理；
**本启动器永不杀非本产品的进程**（`:9881` 那对第二实例、桌面上的其它 python 一律不动）。

用法：
    python start_all.py                      起全栈 + 健康门
    python start_all.py --only backend,gsv   只起指定腿（逗号分隔）
    python start_all.py --restart backend     先归因、清掉自家的旧进程，再起该腿
    python start_all.py --status             不起任何东西，只探活 + 打健康门
    python start_all.py --print-python       不起任何东西，只打印选定的后端解释器路径
                                             （PY-358 唯一后端解释器口径；三枚旧入口已于
                                             2026-09-27 下线，本出口仍是外部取数的唯一口径）
    python start_all.py --reload             仅开发期用：给后端加 uvicorn --reload。
                                             默认**不带** —— watchfiles 的监视根是仓库父目录，
                                             任何智能体往这棵树写 .py 都会重启后端，
                                             砍断在跑的合成任务 + 295s 量级的 voxcpm 装载（M-6）。
    python start_all.py --health-only        同上（别名），失败不启动
    python start_all.py --no-health          起完就退出，不等健康门
    python start_all.py --model-timeout 1200 --gsv-timeout 420 --front-timeout 120
    python start_all.py --run-leg backend    内部用：在当前窗口跑一条腿并 tee 日志

设计约束（不要改回去）：
  * GSV **必须**用 `GPT-SoVITS\\.venv\\Scripts\\python.exe`：系统 Python311 缺
    `ffmpeg` 模块，会在 `GPT_SoVITS/TTS_infer_pack/TTS.py:18` 直接崩（09-22 实测）。
  * 长期服务一律走"可见窗口"，不用隐藏的后台任务 —— 用户要看得到程序在跑。
  * Celery 轨已休眠（任务走 asyncio.create_task），Redis 不在必需面里，故只探不启。
  * 三条腿一律以 **argv 数组 + shell=False** 直接 exec：既避开 cmd 的引号地狱，也不给
    命令字符串留二次解析的口子（`npm` 这类需要 `shutil.which` 解析成 `npm.cmd` 全路径）。
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent            # peiyinyuan/
REPO = HERE.parent                                 # PYY/
LOG_DIR = HERE / "logs"

IS_WIN = os.name == "nt"


# ------------------------------------------------------------------ 模块台账
class Leg:
    """一条可启动的腿：窗口标题、端口、工作目录、argv、进程归因签名。"""

    def __init__(self, key, title, port, cwd, argv, signs, note=""):
        self.key = key
        self.title = title
        self.port = port
        self.cwd = Path(cwd)
        self.argv = list(argv)    # 直接 exec，不经 cmd 解析
        self.signs = signs          # 命令行里出现任一签名 => 认定是本产品进程
        self.note = note

    @property
    def display(self) -> str:
        """给人看的那一行（日志/窗口里自证用，不参与执行）。"""
        return " ".join(self.argv)

    def resolved_argv(self) -> list:
        """shell=False 不做 PATHEXT 解析：`npm` 在 Windows 上是 npm.cmd。"""
        argv = list(self.argv)
        exe = argv[0]
        if not Path(exe).is_absolute():
            argv[0] = shutil.which(exe) or exe
        return argv

    @property
    def log_path(self) -> Path:
        return LOG_DIR / f"{self.key}-{self.port}.log"


def _py(exe: str = "python") -> str:
    return f'"{exe}"' if " " in exe else exe


# ------------------------------------------------ 后端腿的解释器选定（PY-354）
# 事故（09-24 18:32）：`start-all.cmd` 里是 PATH 上的裸 `python`，那台机器上它解析到
# Python 3.14，而 edge_tts / torch 只装在 Python 3.11 —— 后端在缺依赖的解释器上起来，
# 试听必失败。所以**不能**再无条件沿用 `sys.executable`：逐台探测，用第一台齐全的。
BACKEND_DEPS = ("fastapi", "uvicorn", "sqlalchemy", "httpx", "numpy",
                "soundfile", "edge_tts", "torch")
# import 名 ≠ pip 包名（`edge_tts` 装的是 `edge-tts`），照抄 BACKEND_DEPS 会给出装不上的命令
BACKEND_INSTALL_NAMES = ("fastapi", "uvicorn", "sqlalchemy", "httpx", "numpy",
                         "soundfile", "edge-tts", "torch")


def probe_src(deps) -> str:
    """只问 import 可得性（find_spec），不真 import：真 import torch 会把启动白拖数十秒。"""
    return ("import importlib.util, json; print(json.dumps([m for m in "
            + json.dumps(list(deps))
            + " if importlib.util.find_spec(m) is None]))")


PROBE_SRC = probe_src(BACKEND_DEPS)
# 启动器**自己**的判据依赖：`owning_pids`/`describe_pid`/`free_gb` 全读 psutil。
# 与 BACKEND_DEPS 分开列是有意的 —— 后端那几处 psutil 用法都带降级出口
# （`backend/api/workflows.py:153`、`backend/runtime_guard.py:76`、
# `modules/tts_engine/model_preloader.py:83`），缺了只少遥测；
# 而启动器缺了会**下错判断**（把自家后端说成外部进程），所以要单独换台解决。
LAUNCHER_DEPS = ("psutil",)
LAUNCHER_PROBE_SRC = probe_src(LAUNCHER_DEPS)
REEXEC_ENV = "PYY_PY_REEXEC"
_BACKEND_PICK = {}
_LAUNCHER_PICK = {}
_ENCODER_PROBE = {}

# ---------------------------------------------- 软依赖：缺了只少几种格式，不拒起（PY-357）
# `imageio_ffmpeg` 是 pyproject 的 optional extra `codecs`，**故意不进** `BACKEND_DEPS`：
# 进了就变成"缺它拒起后端"，而它只服务 `FFMPEG_FORMATS = ("mp3", "m4b")` 这两种格式
# （wav/flac 由 soundfile 直接写），且还有 PATH ffmpeg 一档兜底。
# 判据也不按包名问 —— `modules/tts_engine/ffmpeg_utils.py:76` 的 `probe()` 才是后端
# 导出时会问的那个问题，问包名会对着"PATH 上有 ffmpeg"的机器误报缺东西。
SOFT_DEPS = {
    "imageio_ffmpeg": {
        "formats": ("mp3", "m4b"),
        "fix": "pip install imageio-ffmpeg（或把 ffmpeg 加入 PATH，或设环境变量 FFMPEG_PATH）",
    },
}
ENCODER_CWD = str(HERE)
ENCODER_PROBE_SRC = (
    "import json;"
    "from modules.tts_engine import ffmpeg_utils as fu;"
    "r=fu.probe();"
    "print(json.dumps({'path': r['path'], 'source': r['source'], 'error': r['error']},"
    "ensure_ascii=False))"
)


def probe_argv(exe: str, deps=BACKEND_DEPS) -> list:
    return [exe, "-X", "utf8", "-c", probe_src(deps)]


def probe_launcher_argv(exe: str) -> list:
    return probe_argv(exe, LAUNCHER_DEPS)


def _read_probe(exe: str, argv: list, timeout: float = 30.0):
    """问一台候选解释器「你缺哪些包」。

    返回缺失清单（[] = 齐全）；起不来、非零退出、输出不成形一律返回 None ——
    判**不合格**而不是判"没缺包"（探测失败蒙过门 = 事故重演）。
    """
    out = _read_probe_json(argv, timeout)
    return out if isinstance(out, list) else None


def probe_python(exe: str, timeout: float = 30.0):
    return _read_probe(exe, probe_argv(exe, BACKEND_DEPS), timeout)


def probe_launcher_python(exe: str, timeout: float = 30.0):
    return _read_probe(exe, probe_launcher_argv(exe), timeout)


def _read_probe_json(argv: list, timeout: float = 30.0):
    """跑一个探测子进程，把最后一行按 JSON 取回来；读不到一律 None（判"不可得"）。

    cwd 钉在 `ENCODER_CWD`（仓库根）：`python -c` 把 cwd 当作 sys.path[0]，
    飘到别的目录就会去 import 另一份代码 —— 那正是 PY-345 那台哑实例的形状。
    """
    try:
        r = subprocess.run(argv, capture_output=True, timeout=timeout,
                           encoding="utf-8", errors="replace", shell=False,
                           cwd=ENCODER_CWD)
    except Exception:
        return None
    if r.returncode != 0:
        return None
    lines = (r.stdout or "").strip().splitlines()
    if not lines:
        return None
    try:
        return json.loads(lines[-1])
    except Exception:
        return None


def probe_encoder_argv(exe: str) -> list:
    return [exe, "-X", "utf8", "-c", ENCODER_PROBE_SRC]


def probe_encoder(exe: str, timeout: float = 30.0):
    """问「这台解释器起的服务能不能出 mp3/m4b」——问的是产品自己的解析口。

    返回 {path, source, error}；探测本身失败返回 None（≠"编码器没问题"）。
    """
    out = _read_probe_json(probe_encoder_argv(exe), timeout)
    if not isinstance(out, dict) or "path" not in out:
        return None
    return {"path": out.get("path"), "source": out.get("source"),
            "error": out.get("error")}


def encoder_probe():
    """后端腿那台解释器的编码器结论，进程内只探一次（None 也要缓存，否则白起子进程）。"""
    if "result" not in _ENCODER_PROBE:
        bp = backend_pick()
        _ENCODER_PROBE["result"] = probe_encoder(bp["exe"]) if bp["ok"] else None
    return _ENCODER_PROBE["result"]


_SOFT = SOFT_DEPS["imageio_ffmpeg"]


def report_encoder(enc) -> None:
    """把"这台起的后端能不能出 mp3"说在前面 —— **只警告，不拒起**（PY-357）。

    以前只能等用户点导出撞上 503 才知道；但缺的只是两种格式，拒起整条后端腿
    会把能用的 wav/flac 一起废掉，所以这里用软口径。
    """
    fmts = "/".join(_SOFT["formats"])
    if enc is None:
        say(f"  !! 编码器 ({fmts} 导出): 读不到 —— 探测子进程没能给出结果"
            "（起不来/超时/输出不成形）。")
        say(f"     不代表缺不缺：后端照样起，wav/flac 不受影响，"
            f"但这次判断不了 {fmts} 能不能导出（缺了会在导出时撞 503）。")
        return
    if enc["path"]:
        say(f"  编码器 ({fmts} 导出): 可用 —— 来源 {enc['source']}: {enc['path']}")
        return
    say(f"  !! 编码器 ({fmts} 导出): 缺 {fmts} —— 后端照样起（wav/flac 不受影响），"
        f"但导出 {fmts} 会被拒 (503)：")
    say(f"     {enc['error'] or '（探测未给出原因）'}")
    say(f"     修法：{_SOFT['fix']}")



def py_launcher_installs() -> list:
    """`py -0p` 列出的本机全部 Python 安装。PATH 上裸 `python` 指向谁全看装了什么，
    而 `py` 启动器把每台都摊开 —— 这是不写死版本号也能找到 3.11 的唯一现成口子。"""
    try:
        r = subprocess.run(["py", "-0p"], capture_output=True, timeout=20,
                           encoding="utf-8", errors="replace", shell=False)
    except Exception:
        return []
    return parse_py_launcher_output(r.stdout or "")


# `py -0p` 每行形如 ` -V:3.11 *        C:\Program Files\Python311\python.exe`。
# 路径**合法地带空格**，所以只能锚定行尾整段取路径，绝不能 `split()[-1]`。
_PY0P_RE = re.compile(r"([A-Za-z]:[\\/].*(?:python|pythonw)\.exe)\s*$", re.IGNORECASE)


def parse_py_launcher_output(text: str) -> list:
    """从 `py -0p` 输出里取**完整**解释器路径（Windows-only；非 Windows 返回空）。"""
    out = []
    for line in (text or "").splitlines():
        m = _PY0P_RE.search(line.strip())
        if m:
            out.append(m.group(1).strip().strip('"'))
    return out


def candidate_pys() -> list:
    """候选顺序：人工指定（PYY_PYTHON）→ 当前解释器 → 本机其它安装。去重大小写不敏感。"""
    out = []
    for exe in ([os.environ.get("PYY_PYTHON", "")] + [sys.executable]
                + py_launcher_installs()):
        v = str(exe).strip().strip('"')
        if v and v.lower() not in {o.lower() for o in out}:
            out.append(v)
    return out


def _pick_from(candidates, probe) -> dict:
    """逐台探测，取第一台依赖齐全的。

    一台都不齐时 ok=False（调用方须拒绝起后端）；`tried` 保留每台缺什么，
    因为对用户有用的不是"起不来"，而是"哪台缺哪个包"。
    """
    tried = []
    for exe in (candidate_pys() if candidates is None else candidates):
        missing = probe(exe)
        tried.append((exe, missing))
        if missing == []:
            return {"exe": exe, "ok": True, "tried": tried}
    return {"exe": sys.executable, "ok": False, "tried": tried}


def pick_backend_python(candidates=None, probe=None) -> dict:
    return _pick_from(candidates, probe or probe_python)


def pick_launcher_python(candidates=None, probe=None) -> dict:
    return _pick_from(candidates, probe or probe_launcher_python)


def backend_pick() -> dict:
    """选定值进程内只算一次：每台候选都要起一个子进程，反复探就是启动白等。"""
    if not _BACKEND_PICK:
        _BACKEND_PICK.update(pick_backend_python())
    return _BACKEND_PICK


def console_pick() -> dict:
    if not _LAUNCHER_PICK:
        _LAUNCHER_PICK.update(pick_launcher_python())
    return _LAUNCHER_PICK


def backend_python() -> str:
    return backend_pick()["exe"]


def report_python_choice() -> int:
    """`--print-python` 的口径：成功=stdout 恰好一行选定解释器路径，失败=rc≠0 且 stdout 空。

    为什么要这枚出口（PY-358）：曾有三枚旧入口（start.bat / start.ps1 / start_gui.py）
    各自拼命令、绕开本文件的解释器预检，等于给 09-24 18:32 那起"裸 python 起后端 →
    缺 edge_tts → 用户 502"的事故留了三扇后门。三枚已于 2026-09-27 整体下线，
    本出口保留为**唯一后端解释器口径** —— 任何外部脚本要问"起后端该用哪台"都只走这里。
    实现只走 stdlib + 子进程探测，**绝不 import backend**：要答案的正是"没装依赖的那台"。
    """
    pick = backend_pick()
    if pick.get("ok"):
        print(pick["exe"])
        return 0
    print("start_all: 本机没有依赖齐全的后端解释器 —— 拒绝起后端。", file=sys.stderr)
    for exe, missing in pick.get("tried") or []:
        why = "探不出来（这台压根起不来）" if missing is None \
            else "缺 " + ", ".join(missing)
        print(f"  候选 {exe} —— {why}", file=sys.stderr)
    print("下一步：先跑 `python tools/setup_deps.py` 装依赖，或直接改用 start-all"
          "（它跑之前会做同一套环境检查）。", file=sys.stderr)
    return 1


def launcher_python() -> str:
    """主控台该跑在哪台解释器上；一台都不合格时返回 ''（不换台，宁可原地 degraded）。"""
    out = console_pick()
    return out["exe"] if out["ok"] else ""


def same_exe(a: str, b: str) -> bool:
    """两台路径是不是同一台解释器（大小写/分隔符/8.3 短名都算同一台）。"""
    try:
        return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))
    except OSError:
        return os.path.normcase(str(a)) == os.path.normcase(str(b))


def reexec_target(current: str = None, want: str = None) -> str:
    """主控台要不要换台执行。返回目标解释器路径，'' = 不换。

    三条"不换"：① 已经换过一次（环保护标记在环境里）；② 自己就是合格的那台；
    ③ 本机压根没有合格的候选 —— 那时换台只是白起一个子进程。
    """
    if os.environ.get(REEXEC_ENV):
        return ""
    current = sys.executable if current is None else current
    want = launcher_python() if want is None else want
    if not want or same_exe(want, current):
        return ""
    return want


def reexec_argv(exe: str, script: str, args) -> list:
    return [exe, "-X", "utf8", str(script)] + list(args)


def reexec_console(args=None, argv=None, script=None, current=None, run=None):
    """启动器自己也要有 psutil 才谈得上正确归因（PY-354 第二层，19:25 现场重放）。

    不合格时换到合格的那台解释器上重跑自己一次，并把子进程的退出码原样带回 ——
    否则用户看到的还是同一个"能双击、日志在滚、但归因说错话"的控制台。
    """
    if args is not None and getattr(args, "run_leg", ""):
        return None                      # 子窗口跑的是主控台定死的 argv，不换台
    current = sys.executable if current is None else current
    target = reexec_target(current=current)
    if not target:
        return None
    script = str(Path(script or __file__).resolve())
    args = list(sys.argv[1:] if argv is None else argv)
    say(f"[{now()}] 主控台换台执行: {current} -> {target}"
        f"（这台 Python 读不到判断端口占用者所需的 {', '.join(LAUNCHER_DEPS)}）")
    env = dict(os.environ)
    env[REEXEC_ENV] = "1"
    r = (run or subprocess.run)(reexec_argv(target, script, args), env=env, shell=False)
    return getattr(r, "returncode", r)



def legs() -> list:
    gsv_py = REPO / "GPT-SoVITS" / ".venv" / "Scripts" / "python.exe"
    return [
        Leg("backend", "PY-Backend", 8000, HERE,
            [backend_python(), "-X", "utf8", "-m", "uvicorn", "backend.main:app",
             "--host", "127.0.0.1", "--port", "8000"],
            # 注意：默认不带 --reload —— PY-340/M-6。需要热重载用 `--reload`（仅开发期）。
            signs=("backend.main:app",),
            note="FastAPI 后端（含 voxcpm 启动即装载，见 #86；无 --reload；"
                 "解释器按依赖探测选定，见 PY-354）"),
        Leg("frontend", "PY-Frontend", 5173, HERE / "frontend",
            ["npm", "run", "dev"],
            signs=("vite", "peiyinyuan\\frontend",),
            note="Vite 前端 dev（只绑 localhost/::1）"),
        Leg("gsv", "PY-GSV", 9880, REPO / "GPT-SoVITS",
            [str(gsv_py), "api_v2.py", "-a", "127.0.0.1", "-p", "9880",
             "-c", "GPT_SoVITS/configs/tts_infer.yaml"],
            signs=("api_v2.py",),
            note="GPT-SoVITS 侧车（必须用 .venv 解释器，系统 python 缺 ffmpeg 模块）"),
    ]


def leg_by_key(key: str):
    for lg in legs():
        if lg.key == key:
            return lg
    return None


# 只探不启的外围（装了/在跑就纳管显示，没跑就 SKIP 给原因）
PROBES = (
    {"key": "mineru", "label": "MinerU 文档解析", "port": 8001,
     "url": "http://127.0.0.1:8001/docs", "dir": REPO / "MinerU-master",
     "how": "手动起：MinerU-master 内 `mineru-api --port 8001`（本启动器不代起）"},
    {"key": "freellmapi", "label": "freellmapi (LLM 代理)", "port": 3001,
     "url": "http://127.0.0.1:3001/", "dir": REPO / "freellmapi-main",
     "how": "手动起：freellmapi-main 内 `npm start`（或 docker compose）"},
    {"key": "redis", "label": "Redis (Celery 轨，已休眠)", "port": 6379,
     "url": None, "dir": None,
     "how": "任务当前走 asyncio.create_task，不需要 Redis；要起用 docker-compose"},
)


# ------------------------------------------------------------------ 小工具
def now() -> str:
    return time.strftime("%H:%M:%S")


def say(msg=""):
    print(msg, flush=True)


def hdr(msg):
    say("")
    say(f"[{now()}] ==== {msg} ====")


def http_get(url: str, timeout: float = 5.0):
    """返回 (status_code | None, 错误原文)。错误原文用于健康门 FAIL 的可读原因。"""
    req = urllib.request.Request(url, headers={"User-Agent": "pyy-launcher/3"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(4096)
    except urllib.error.HTTPError as e:                  # 4xx/5xx 也有状态码
        return e.code, str(e).encode("utf-8", "replace")
    except Exception as e:
        return None, f"{type(e).__name__}: {e}".encode("utf-8", "replace")


def free_gb():
    try:
        import psutil                                     # type: ignore
        return round(psutil.virtual_memory().available / 1024 ** 3, 2)
    except Exception:
        return None


def memory_text() -> str:
    """没测到就说没测到 —— `可用内存 NoneGB` 是把"没有读数"伪装成一个数字。"""
    gb = free_gb()
    return "读不到(启动器缺 psutil)" if gb is None else f"{gb}GB"


def set_console_title(title: str) -> None:
    r"""把**当前**控制台窗口标题钉成 title。

    `start "PY-Frontend" cmd /k "title PY-Frontend && npm run dev"` 不够稳：
    09-22 11:10 实测 vite/npm 会把标题冲掉（前端窗口只剩 `C:\Windows\system32\cmd.exe`），
    所以由本进程直接 SetConsoleTitleW，并在 tee 循环里周期性重申。
    """
    if not IS_WIN:
        return
    try:
        import ctypes
        ctypes.windll.kernel32.SetConsoleTitleW(str(title))
    except Exception:
        pass


def owning_pids(port: int) -> list:
    """监听该端口的 PID 列表（psutil 优先，退到 netstat 解析）。"""
    pids = []
    try:
        import psutil                                     # type: ignore
        for c in psutil.net_connections(kind="tcp"):
            if c.laddr and c.laddr.port == port and c.status == "LISTEN" and c.pid:
                if c.pid not in pids:
                    pids.append(c.pid)
        if pids:
            return pids
    except Exception:
        pass
    try:
        out = subprocess.run(["netstat", "-ano", "-p", "TCP"],
                             capture_output=True, text=True,
                             encoding="utf-8", errors="replace").stdout
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 5 and parts[0].upper() == "TCP" \
                    and parts[3].upper() == "LISTENING" \
                    and parts[1].rsplit(":", 1)[-1] == str(port):
                pid = int(parts[4])
                if pid not in pids:
                    pids.append(pid)
    except Exception:
        pass
    return pids


def read_process_cmd(pid: int):
    """取某进程的命令行原文。返回 `(原文, None)` 或 `(None, 读不到的原因)`。"""
    try:
        import psutil                                     # type: ignore
    except Exception as e:
        return None, f"启动器所在解释器缺 psutil: {type(e).__name__}: {e}"
    try:
        p = psutil.Process(pid)
        cmd = " ".join(p.cmdline())
        if not cmd:
            return None, "cmdline 为空"
        return f"name={p.name()} cmd={cmd}", None
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def attribute_pid(pid: int, reader=None) -> tuple:
    """端口占用者的归因结论 = `(可读原文, 能否归因)`。

    "读不到命令行" 与 "读到了、但不是本产品" 是两件事，必须分开：前者是启动器自己
    没这个能力（缺 psutil / 权限不足），拿它当"外部进程"下结论，就会把自家后端
    说成别人、然后静默跳过该腿还回 0（PY-354 现场重放，09-24 19:25）。
    """
    cmd, err = (reader or read_process_cmd)(pid)
    if err is not None:
        return f"pid={pid} 命令行不可读({err})", False
    return f"pid={pid} {cmd}", True


def describe_pid(pid: int) -> str:
    """命令行原文（归因用）—— 拿不到就如实说拿不到。"""
    return attribute_pid(pid)[0]


def process_tree(pid: int) -> list:
    """某进程及其子孙（uvicorn --reload 的真身是子进程，命令行里没签名）。"""
    out = [pid]
    try:
        import psutil                                     # type: ignore
        for c in psutil.Process(pid).children(recursive=True):
            out.append(c.pid)
    except Exception:
        pass
    return out


def is_listening(port: int) -> bool:
    return bool(owning_pids(port))


# ------------------------------------------------------------------ 一条腿的跑法
def run_leg(key: str, title: str = "", raw_argv=None) -> int:
    """在当前（带标题的可见）窗口里跑一条腿，输出同时 tee 到 logs/。"""
    lg = leg_by_key(key)
    if lg is None:
        say(f"[{now()}] 未知模块 '{key}'，可选: backend / frontend / gsv")
        return 2
    if raw_argv:
        lg.argv = list(raw_argv)              # 主控台决定的实际 argv（如 --reload 后）
    shown = title or lg.title
    set_console_title(shown)                  # 窗口标题自证（start 给的是初始值，会被冲掉）

    # 标题看门狗：npm/node 会把自己的标题盖到控制台上，而且 vite 打印完 "ready"
    # 就长期静默 —— 靠"来一行输出才重申"兜不住（09-22 11:30 二次实测前端标题又被冲掉）。
    title_stop = threading.Event()

    def _title_watch():
        while not title_stop.wait(5.0):
            set_console_title(shown)

    threading.Thread(target=_title_watch, daemon=True).start()
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    if not lg.cwd.exists():
        say(f"[{lg.title}] 工作目录不存在: {lg.cwd} —— 放弃")
        return 3
    say(f"[{now()}] ==== {shown} 启动器包装 ====")
    say(f"  cwd    : {lg.cwd}")
    say(f"  命令   : {lg.display}")
    say(f"  日志   : {lg.log_path}")
    say(f"  说明   : {lg.note}")
    say(f"  内存   : 当前可用 {memory_text()}")
    owners = owning_pids(lg.port)
    if owners:
        say(f"  注意   : 端口 {lg.port} 已被占用（起前归因）:")
        for pid in owners:
            say(f"           {describe_pid(pid)}")
    say("")
    env = dict(os.environ)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env["PYTHONUNBUFFERED"] = "1"
    try:
        proc = subprocess.Popen(lg.resolved_argv(), cwd=str(lg.cwd), shell=False,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                env=env)
    except Exception as e:
        say(f"[{lg.title}] 无法启动: {type(e).__name__}: {e}")
        if IS_WIN and sys.stdin and sys.stdin.isatty():
            input("按回车退出本窗口...")
        return 4
    with open(lg.log_path, "ab") as f:
        f.write(f"\n===== [{time.strftime('%Y-%m-%d %H:%M:%S')}] "
                f"{lg.title} launch: {lg.display} | cwd={lg.cwd} =====\n".encode(
                    "utf-8", "replace"))
        f.flush()
        last_title = 0.0
        for raw in iter(proc.stdout.readline, b""):
            line = decode_line(raw)
            sys.stdout.write(line)
            sys.stdout.flush()
            f.write(line.encode("utf-8", "replace"))
            f.flush()
            if time.time() - last_title > 5.0:      # 子进程启动时会冲掉标题，重申
                set_console_title(shown)
                last_title = time.time()
        rc = proc.wait()
    title_stop.set()                          # 先停看门狗，否则 EXITED 标记会被冲掉
    say("")
    say(f"[{now()}] {lg.title} 子进程已退出 rc={rc} —— 日志见 {lg.log_path}")
    set_console_title(f"{shown} [EXITED rc={rc}]")
    if IS_WIN and sys.stdin and sys.stdin.isatty():
        try:
            input("按回车关闭本窗口...")
        except Exception:
            pass
    return rc


def decode_line(raw: bytes) -> str:
    """子进程输出可能是 UTF-8（我们给 python 加了 -X utf8）也可能是 GBK。"""
    for enc in ("utf-8", "gbk", "cp936"):
        try:
            return raw.decode(enc)
        except Exception:
            continue
    return raw.decode("utf-8", "replace")


# ------------------------------------------------------------------ 开窗
def _log_size(lg: Leg) -> int:
    try:
        return Path(lg.log_path).stat().st_size
    except OSError:
        return 0


def wait_for_launch_banner(lg: Leg, since: int, timeout: float = 15.0) -> bool:
    """等这条腿的日志里出现**新的** `launch:` 标记 —— 那行由子窗口的 run_leg 亲笔写下。

    它只在"窗口真起来了、子 python 真跑到 Popen 那一步"之后才存在。没等到就是开窗失败，
    不能拿"我打印了已开窗"当证据（U-6 口径：200/自报成功都不算，要看产物）。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with open(lg.log_path, "rb") as f:
                f.seek(since)
                if b"launch:" in f.read():
                    return True
        except OSError:
            pass
        time.sleep(0.5)
    return False


def spawn_window(lg: Leg, allow_hidden_fallback: bool = False) -> bool:
    """开一枚带标题的可见控制台窗口跑这条腿（不用隐藏后台任务）。

    `allow_hidden_fallback`（PY-356）：Windows 上开窗失败时**默认不再**擅自用隐藏进程
    把腿带起来。真因是 `legs()` 把后端端口钉死在 8000，任何一次兜底起台都会去抢
    用户那台 :8000（抢到 10048 就退出，但启动钩子已经往工作树 data/ 写过库），
    而全量回归门里那枚"开窗入参"用例正是这么变成"测试起了真后端"的（21:24:52 报障）。
    交回 False 由调用方（orchestrate 的"开窗未证实"判据）处置，不静默换一种起法。
    只有显式传 `allow_hidden_fallback=True` 的调用方（CLI 主路径 `orchestrate`）才保留
    "开窗失败也要把腿带起来"这层兜底；非 Windows 没有 `start cmd` 那扇门，隐藏 Popen
    就是唯一正常起法，同样走兜底（默认参数不拦它）。
    """
    script = Path(__file__).resolve()
    # 实际 argv 里有带空格的路径（如 `C:\Program Files\...`），直接嵌进
    # `cmd /k "..."` 会被引号解析打断 —— 用 base64(JSON) 过桥（纯字母数字，命令行安全），
    # 子窗口里再解回来。这样 `--reload` 之类的改动也能忠实传进窗口。
    raw_b64 = base64.b64encode(json.dumps(lg.argv).encode("utf-8")).decode("ascii")
    inner = (f'title {lg.title} && cd /d "{lg.cwd}" && '
             f'{_py(sys.executable)} -X utf8 "{script}" --run-leg {lg.key} '
             f'--title "{lg.title}" --raw-b64 {raw_b64}')
    if IS_WIN:
        line = f'start "{lg.title}" cmd /k "{inner}"'
        try:
            # 必须让 cmd 自己解析这一整行。用 list-form（["cmd","/c",line]）时
            # Python 会把行内的 `"` 转义成 `\"`，cmd 于是把标题 "PY-Backend" 当成
            # 要执行的文件 —— 实测报「系统找不到文件 \PYBackend\」，`start` 静默失败，
            # 而启动器照样打印"已开窗"（09-24 14:03 那次重启两条腿都没起来就是这么丢的）。
            subprocess.run(line, shell=True, check=False)
            return True
        except Exception as e:
            say(f"  !! 开窗失败({type(e).__name__}: {e})"
                + (" —— 不擅自退到隐藏进程（那会抢 :"
                   f"{lg.port} 并往本树 data/ 写，PY-356），交回调用方处置"
                   if not allow_hidden_fallback else " —— 按显式授权退到隐藏进程"))
            if not allow_hidden_fallback:
                return False
    # 走到这里：非 Windows（唯一正常起法），或 Windows 开窗失败但调用方显式授权。
    # 兜底起台也必须留得下归因：没有 `launch:` 头的日志事后查不到"是谁起的"（PY-356）。
    with open(lg.log_path, "ab") as f:
        f.write(f"\n===== [{time.strftime('%Y-%m-%d %H:%M:%S')}] "
                f"{lg.title} spawn-window-fallback launch: {lg.display} "
                f"| cwd={lg.cwd} =====\n".encode("utf-8", "replace"))
        f.flush()
        proc = subprocess.Popen(lg.resolved_argv(), cwd=str(lg.cwd), shell=False,
                                stdout=f, stderr=subprocess.STDOUT)
    say(f"  !! 已用隐藏进程带起 {lg.title} (pid={getattr(proc, 'pid', '?')}"
        + (f"，授权方=调用方 allow_hidden_fallback —— 日志 {lg.log_path}" if IS_WIN else ""))
    return True


# ------------------------------------------------------------------ 归因后处置
def decide(lg: Leg, restart: bool):
    """端口占用先归因，再决定 复用 / 清理 / 让路。

    返回 'launch' | 'reuse' | 'blocked'。归因读不到时也是 'blocked'（不动看不见的
    东西），但**报出去的话不同**：那是"我读不到"，不是"这不是本产品"。
    """
    owners = owning_pids(lg.port)
    if not owners:
        return "launch"
    foreign = []
    mine = []
    unknown = []
    for pid in owners:
        desc, attributable = attribute_pid(pid)
        hit = any(s.lower() in desc.lower() for s in lg.signs)
        if not hit and attributable:      # 命令行没签名 —— 再看它的父/兄弟是不是自家
            try:
                import psutil             # type: ignore
                ppid = psutil.Process(pid).ppid()
                pd, pok = attribute_pid(ppid)
                if pok and any(s.lower() in pd.lower() for s in lg.signs):
                    hit = True
                    desc += f" (父进程 {ppid} 是本产品)"
            except Exception:
                pass
        if hit:
            mine.append((pid, desc))
        elif attributable:
            foreign.append((pid, desc))
        else:
            unknown.append((pid, desc))
        say(f"  端口 {lg.port} 占用归因: {desc}")
    if foreign:
        say(f"  => :{lg.port} 上有**非本产品**进程 —— 本启动器不会杀它，该腿跳过。")
        say(f"     要起这一腿请先自行处理，或换端口（见 {lg.title} 日志）。")
        return "blocked"
    if unknown:
        say(f"  => :{lg.port} 上有 {len(unknown)} 个进程读不到它在跑什么 —— "
            "判断不了它是不是本程序自己的旧实例。")
        say("     本启动器从不处理判断不了的进程，所以这一项跳过（并不等于发现了外部程序）。")
        say(f"     多半是这个控制台开在没装 {', '.join(LAUNCHER_DEPS)} 的 Python 上"
            "（PY-354 起会自动换台；仍然如此就换一台装了它的 Python 双击 一键启动.bat）。")
        return "blocked"
    if mine and not restart:
        say(f"  => 判定为本产品旧实例，复用现成的（不重复起）。要换新代码用 --restart {lg.key}")
        return "reuse"
    if mine and restart:
        for pid, desc in mine:
            tree = process_tree(pid)
            try:
                import psutil             # type: ignore
                say(f"  => 清理自家旧进程: {desc}")
                for t in tree:
                    try:
                        psutil.Process(t).terminate()
                    except Exception:
                        pass
            except Exception as e:        # pragma: no cover
                say(f"  !! 清理不可用({type(e).__name__}: {e})，跳过该腿")
                return "blocked"
            deadline = time.time() + 12
            while time.time() < deadline and is_listening(lg.port):
                time.sleep(0.5)
        return "launch"
    return "launch"


# ------------------------------------------------------------------ 健康门
class Gate:
    def __init__(self):
        self.rows = []                    # (name, verdict, detail)

    def add(self, name, verdict, detail):
        self.rows.append((name, verdict, detail))
        say(f"  [{verdict:5}] {name:38} {detail}")


def read_instance_tier(port: int = 8000, timeout: float = 4.0):
    """问**在跑的那台实例**它自陈是哪一档（online/local）。返回 (tier, payload, 读不到的原因)。

    为什么不读本启动器的 env：档位受 `.env`、命令行、任务级切档影响，而健康门要判的是
    "这台进程现在的行为"（PY-345 的教训 —— 门读的是答案，不是我的猜测）。
    """
    code, body = http_get(f"http://127.0.0.1:{port}/api/system/engines", timeout=timeout)
    if code != 200:
        why = f"HTTP {code}" if code else (
            body.decode("utf-8", "replace") if isinstance(body, bytes) else str(body))
        return None, None, why
    try:
        payload = json.loads(body.decode("utf-8", "replace"))
    except Exception as e:
        return None, None, f"响应不可解析: {e}"
    if payload.get("status") != "ok":
        return None, payload, f"status={payload.get('status')} | error={payload.get('error')}"
    return payload.get("tier"), payload, None


def gate_engine(g: Gate, args, probe_only: bool, port: int = 8000) -> bool:
    """引擎门按档位分叉（指令#1：在线档为默认）。

    旧行为：无条件等 voxcpm `state=ready`。在线档现在**根本不在启动时装载**它
    （main.py 的 `preload_local_at_start()` 返回 False），于是这条门会在 900s 预算后
    对一次完全正常的启动报 FAIL —— 那是量具错，不是程序错。
    """
    tier, payload, why = read_instance_tier(port)
    if tier == "online":
        modes = {m.get("tier"): m for m in (payload.get("modes") or [])}
        online = modes.get("online") or {}
        local = modes.get("local") or {}
        log = f"peiyinyuan/logs/backend-{port}.log"
        if not online.get("available"):
            g.add("默认档位引擎就绪 (在线)", "FAIL",
                  f"在线档默认引擎 {online.get('engine')} 不可用: {online.get('reason')} | 日志 {log}")
            return False
        g.add("默认档位引擎就绪 (在线)", "PASS",
              f"{online.get('engine_display') or online.get('engine')} 可用"
              f" | 本地档 {local.get('engine')} 启动不装载（切到本地档时按需装载）| 日志 {log}")
        if not local.get("available"):
            g.add("本地档引擎（切档后可用性）", "WARN",
                  f"{local.get('engine')}: {local.get('reason')}")
        return True
    if tier is None and payload is not None:         # 端点在、但自陈不可信：读不到答案就明说
        g.add("引擎档位自陈", "FAIL",
              f"/api/system/engines 不可信（{why}）| 日志 peiyinyuan/logs/backend-{port}.log")
        return False
    # 本地档、老构建（无该端点）、或后端还在起 → 维持原来的 voxcpm ready 硬门，不放水
    return gate_model_loaded(g, 15.0 if probe_only else args.model_timeout,
                             dead_after=5.0 if probe_only else args.boot_timeout,
                             port=port)


def gate_backend_health(g: Gate, timeout: float):
    t0 = time.time()
    detail = ""
    while time.time() - t0 < timeout:
        code, body = http_get("http://127.0.0.1:8000/api/health", timeout=5)
        if code == 200:
            g.add("后端 /api/health = 200", "PASS",
                  f"用时 {time.time() - t0:.1f}s | {body[:160]!r}"
                  if isinstance(body, bytes) else f"用时 {time.time() - t0:.1f}s")
            return True
        detail = f"HTTP {code}" if code else f"不可达: {body.decode('utf-8','replace') if isinstance(body, bytes) else body}"
        time.sleep(2)
    g.add("后端 /api/health = 200", "FAIL",
          f"{timeout:.0f}s 内未 200（{detail}）| 日志 peiyinyuan/logs/backend-8000.log")
    return False


def gate_model_loaded(g: Gate, timeout: float, quiet_after: float = 60.0,
                      dead_after: float = 120.0, port: int = 8000):
    """voxcpm 必须真的 ready（state=ready）—— 不给"看起来成功"留缝。

    口径（09-22 11:10 冷启动实测后修正，别再改回去）：装载途中 torch 的权重反序列化
    会把 uvicorn 的事件循环饿死（GIL），表现是 **`:8000` 在听、但 HTTP 8s 不返**。
    这是"还在装"，不是"装坏了"；旧实现把它当"30s 不可达"直接判 FAIL，把一次正常装载
    误报成故障。现在只有**端口压根无监听**（后端没起来 / 已退出）才判死，
    其余情况在 `timeout` 预算内继续等，并把饿死秒数量化写进明细
    （#86"启动即装载"的代价必须看得见，而不是藏成 PASS）。
    """
    t0 = time.time()
    last = None
    last_progress = 0.0
    starved_s = 0.0            # 端口在听但接口不答的累计时长
    unreachable_since = None
    no_listener_since = None
    ever_listened = False
    url = f"http://127.0.0.1:{port}/api/system/model_status"
    log = f"peiyinyuan/logs/backend-{port}.log"
    while True:
        if not is_listening(port):
            if ever_listened:                   # 起来过又掉了 = 真死
                g.add("引擎 voxcpm 已装载 (ready)", "FAIL",
                      f":{port} 监听已消失（后端进程退出）—— 最后状态 "
                      f"{json.dumps(last, ensure_ascii=False)[:220]} | 日志 {log}")
                return False
            no_listener_since = no_listener_since or t0
            if time.time() - no_listener_since > dead_after:
                g.add("引擎 voxcpm 已装载 (ready)", "FAIL",
                      f"{dead_after:.0f}s 内 :{port} 从未监听（后端没起来或已崩溃）"
                      f" | 日志 {log}")
                return False
            snap = {"state": "no-listener", "message": f"等待 :{port} 绑定",
                    "error": f":{port} 无监听"}
        else:
            ever_listened = True
            no_listener_since = None
            code, body = http_get(url, timeout=8)
            snap = {}
            if code == 200:
                unreachable_since = None
                try:
                    payload = json.loads(body.decode("utf-8", "replace"))
                    snap = payload.get("model") or {}
                except Exception as e:
                    snap = {"state": "unknown", "error": f"响应不可解析: {e}"}
            else:
                why = f"HTTP {code}" if code else (
                    body.decode("utf-8", "replace")
                    if isinstance(body, bytes) else str(body))
                prev = unreachable_since
                unreachable_since = time.time()
                starved_s += 8.0 if prev is None else (unreachable_since - prev)
                snap = {"state": "loading(接口暂不可答)", "error": why,
                        "message": f"端口在听但接口未答（累计 {starved_s:.0f}s，装载占满 GIL）"}
        st = snap.get("state")
        last = snap
        loading = str(st).startswith("loading")
        if st == "ready":
            gate = snap.get("gate") or {}
            g.add("引擎 voxcpm 已装载 (ready)", "PASS",
                  f"加载耗时 {snap.get('load_seconds')}s | 尝试 {snap.get('attempts')} 次 "
                  f"| 门限 {gate.get('source')} | 结束可用内存 {gate.get('free_gb_after')}GB "
                  f"| 用时 {time.time() - t0:.0f}s"
                  + (f"（装载期接口饿死 {starved_s:.0f}s）" if starved_s > 5 else "")
                  + f" | 日志 {log}")
            return True
        if st == "failed":
            g.add("引擎 voxcpm 已装载 (ready)", "FAIL",
                  f"真实原因: {snap.get('error')}（尝试 {snap.get('attempts')} 次）"
                  f"| 日志 {log}")
            return False
        if time.time() - t0 > timeout:
            g.add("引擎 voxcpm 已装载 (ready)", "FAIL",
                  f"{timeout:.0f}s 等待上限内未 ready（最后状态 "
                  f"{json.dumps(last, ensure_ascii=False)[:220]}；饿死累计 {starved_s:.0f}s）"
                  f"| 日志 {log}")
            return False
        if time.time() - last_progress > (quiet_after if loading else 5.0):
            last_progress = time.time()
            say(f"  ....   引擎状态 {st} | {snap.get('message')} "
                f"| 可用内存 {memory_text()} | 已等 {time.time() - t0:.0f}s"
                f"/{timeout:.0f}s" + (f" | 饿死累计 {starved_s:.0f}s" if starved_s else ""))
        time.sleep(5 if loading else 3)


def gate_url(g: Gate, name: str, url: str, expect=(200,), log="",
             wait: float = 0.0, quiet: float = 20.0):
    """探一枚 HTTP 端点。`wait>0` 时给启动预算（GSV 冷启动要几十秒，秒判=误判）。"""
    t0 = time.time()
    why = last_code = None
    last_print = 0.0
    while True:
        code, body = http_get(url, timeout=8)
        if code in expect:
            g.add(name, "PASS", f"{url} -> {code}"
                  + (f" | 用时 {time.time() - t0:.1f}s" if time.time() - t0 > 2 else ""))
            return True
        last_code = code
        why = (f"HTTP {code}" if code else
               f"{body.decode('utf-8', 'replace') if isinstance(body, bytes) else body}")
        if time.time() - t0 >= wait:
            break
        if time.time() - last_print > quiet:
            last_print = time.time()
            say(f"  ....   {name}: 等 {time.time() - t0:.0f}s/{wait:.0f}s（{why[:110]}）")
        time.sleep(3)
    g.add(name, "FAIL", f"{url} -> {why}"
          + (f"（{wait:.0f}s 内未起来，最后一次 HTTP={last_code}）" if wait else "")
          + (f" | 日志 {log}" if log else ""))
    return False


def probe_optional(g: Gate):
    for p in PROBES:
        listening = is_listening(p["port"])
        if listening:
            desc = "; ".join(describe_pid(pid) for pid in listening)
            ok = True
            if p["url"]:
                code, _ = http_get(p["url"], timeout=4)
                ok = code in (200, 201, 204, 301, 302, 307, 401, 404)
                detail = f":{p['port']} 在跑（{desc}）| {p['url']} -> HTTP {code}"
            else:
                detail = f":{p['port']} 在跑（{desc}）"
            g.add(p["label"], "PASS" if ok else "WARN", detail + " | 已纳管显示，不由本启动器管辖")
            continue
        reason = []
        if p["dir"] is not None and not Path(p["dir"]).exists():
            reason.append(f"目录不存在: {p['dir']}")
        elif p["dir"] is not None:
            reason.append(f":{p['port']} 无监听（代码在 {p['dir']}，未被本启动器拉起）")
        else:
            reason.append(f":{p['port']} 无监听")
        reason.append(p["how"])
        g.add(p["label"], "SKIP", " | ".join(reason))


def health_gate(args, probe_only: bool = False, *, wanted=None) -> bool:
    g = Gate()
    hdr("健康门（逐项 PASS/FAIL/SKIP，不猜、不代跑）"
        + ("—— 只探活，不等启动" if probe_only else ""))
    # 三条腿的 wait 是给"启动器刚亲手拉起、正在冷启动"的进程留的预算；
    # --status 什么都没起，再等 90s/300s 只是把一次秒答变成 6 分钟等待。
    #
    # `wanted` = 本次真的要求启动的腿。不在里面的腿**不查也不占启动预算**
    # （09-24 14:41 实测：`--only backend,frontend` 照样等 GSV 300s，然后报一条
    #  "我没要求它起"造出来的 FAIL —— 报告里混进这种行，用户就分不清哪条真坏了）。
    keys = set(wanted) if wanted else {lg.key for lg in legs()}
    if "backend" in keys:
        gate_backend_health(g, args.boot_timeout)
        gate_engine(g, args, probe_only)
    else:
        g.add("后端 :8000", "SKIP", "本次 --only 未包含 backend，不查也不占预算")
    if "frontend" in keys:
        gate_url(g, "前端 localhost:5173 = 200", "http://localhost:5173/",
                 log="peiyinyuan/logs/frontend-5173.log",
                 wait=0.0 if probe_only else args.front_timeout)
    else:
        g.add("前端 :5173", "SKIP", "本次 --only 未包含 frontend，不查也不占预算")
    if "gsv" in keys:
        gate_url(g, "GSV 127.0.0.1:9880/docs = 200", "http://127.0.0.1:9880/docs",
                 log="peiyinyuan/logs/gsv-9880.log",
                 wait=0.0 if probe_only else args.gsv_timeout)
    else:
        g.add("GSV :9880", "SKIP", "本次 --only 未包含 gsv，不查也不占预算")
    probe_optional(g)
    fails = [r for r in g.rows if r[1] == "FAIL"]
    warns = [r for r in g.rows if r[1] == "WARN"]
    hdr(f"健康门结论: {len([r for r in g.rows if r[1] == 'PASS'])} PASS / "
        f"{len(fails)} FAIL / {len(warns)} WARN / "
        f"{len([r for r in g.rows if r[1] == 'SKIP'])} SKIP")
    if fails:
        say("  存在 FAIL —— 请按上面的可读原因与日志路径排查，不要当作已就绪。")
    return not fails


# ------------------------------------------------------------------ 主流程
def orchestrate(args) -> bool:
    wanted = [k.strip() for k in args.only.split(",") if k.strip()]
    all_keys = [lg.key for lg in legs()]
    if wanted and sorted(wanted) != sorted(all_keys):
        unknown = [k for k in wanted if k not in all_keys]
        if unknown:
            say(f"未知模块 {unknown}，可选 {all_keys}")
            return False
    else:
        wanted = all_keys
    restart_keys = [k.strip() for k in args.restart.split(",") if k.strip()]

    hdr(f"配音员全栈启动器 v3 | 仓库 {REPO} | 可用内存 {memory_text()}")
    say("  唯一入口：今后只需双击 一键启动.bat —— 旧三枚 start.bat / start.ps1 / start_gui.py 已于 2026-09-27 下线")
    cp = console_pick()
    # 归因/内存这些判据是启动器**自己**要用的能力：它跑在哪台上，决定它说的话可不可信。
    say(f"  主控台解释器: {sys.executable}"
        + ("" if cp["ok"] and same_exe(cp["exe"], sys.executable)
           else f" | 本机合格的是 {cp['exe'] if cp['ok'] else '(无 —— 归因只能报不可得)'}"))
    say(f"  环境变量 PYY_PRELOAD_MIN_FREE_GB = "
        f"{os.environ.get('PYY_PRELOAD_MIN_FREE_GB', '(未设置 -> 启动即无条件装载 voxcpm)')}")
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    started = []
    refused = []
    blocked = []
    for key in wanted:
        lg = leg_by_key(key)
        if args.reload and lg.key == "backend":
            lg.argv = lg.argv + ["--reload"]
            say("  (!! 按 --reload 起后端：仅开发期用。watchfiles 会因树内任何 .py 改动"
                "重启后端，打断 voxcpm 量级数分钟的装载并杀掉在跑的合成。)")
        hdr(f"{lg.title} (: {lg.port}) —— {lg.note}")
        say(f"  命令: {lg.display}")
        say(f"  日志: {lg.log_path}")
        decision = decide(lg, restart=True if key in restart_keys else args.restart_all)
        if lg.key == "backend":
            bp = backend_pick()
            # 自证用的是哪台解释器、其余候选为什么被否 —— 缺依赖要说缺哪个，
            # 否则用户只能看到"起不来"三个字（PY-354 的原始投诉形态）。
            say(f"  解释器: {bp['exe']}"
                + ("（按依赖探测选定）" if bp["ok"] else "（无合格候选，仅占位）"))
            for exe, missing in bp["tried"]:
                if missing == []:
                    say(f"           齐全 -> 采用: {exe}")
                elif missing is None:
                    say(f"           探测失败(起不来/无输出) -> 判不合格: {exe}")
                else:
                    say(f"           缺 {', '.join(missing)} -> 否: {exe}")
            if decision == "launch" and not bp["ok"]:
                say(f"  !! 拒绝启动后端：本机没有任何一台 Python 装齐后端依赖 "
                    f"({', '.join(BACKEND_DEPS)})。")
                say("     在缺依赖的解释器上起服务，得到的是一个『能打开但说不出话』的后端 "
                    "—— 试听/合成必失败（PY-354），所以这里宁可不起。")
                say("     修法：对上面任意一台执行 "
                    f"{_py(sys.executable)} -m pip install {' '.join(BACKEND_INSTALL_NAMES)}")
                refused.append(key)
                continue
            report_encoder(encoder_probe())
        if decision == "launch":
            since = _log_size(lg)
            # allow_hidden_fallback=True：CLI 主路径保留兜底（"至少把腿带起来"是有意的
            # 产品行为），但兜底那行日志现在带 launch: 头，事后能归因是谁起的（PY-356）。
            spawn_window(lg, allow_hidden_fallback=True)
            started.append(key)
            if wait_for_launch_banner(lg, since):
                say(f"  => 已开窗 {lg.title}（可见控制台，日志已见本腿的 launch 标记）")
            else:
                say(f"  !! 开窗未证实：{lg.log_path} 里没有出现新的 launch 标记 —— "
                    f"这一腿很可能根本没起来（不是『复用了现成的』，是真没开）")
            time.sleep(1.2)                 # 错峰，别仨窗口挤在同一秒抢 IO
        else:
            if decision == "blocked":
                blocked.append(key)
            say(f"  => {decision}（不重复开窗）")
    if refused:
        hdr(f"!! 有腿被拒起 {refused} —— 不是启动器坏了，是这台机器缺依赖（逐台清单见上）")
    if blocked:
        hdr(f"!! 有腿未起 {blocked} —— 端口归因门拦下（逐条原因见上），"
            "要的东西没全部做到，别当已就绪")
    if refused or blocked:
        if args.no_health:
            hdr("已按要求跳过健康门 (--no-health)")
            return False
        health_gate(args, wanted=wanted)
        say("  （上面的健康门只说明此刻有什么在跑；被拒/被拦的腿依然算没做到）")
        return False
    if args.no_health:
        hdr("已按要求跳过健康门 (--no-health)")
        return True
    return health_gate(args, wanted=wanted)


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", default="backend,frontend,gsv",
                    help="要启动的腿（逗号分隔），默认全部")
    ap.add_argument("--restart", default="",
                    help="先归因并清理该腿的自家旧进程再起")
    ap.add_argument("--restart-all", action="store_true",
                    help="所有要启动的腿都先清理自家旧进程")
    ap.add_argument("--status", action="store_true",
                    help="不启动任何进程，只探活 + 打健康门")
    ap.add_argument("--health-only", action="store_true", help="--status 的别名")
    ap.add_argument("--no-health", action="store_true", help="启动后不跑健康门")
    ap.add_argument("--reload", action="store_true",
                    help="仅开发期：给后端加 uvicorn --reload（默认不带，防 watchfiles 打断装载/杀在跑合成）")
    ap.add_argument("--run-leg", metavar="KEY", help="内部用：在当前窗口跑一条腿")
    ap.add_argument("--title", default="", help="内部用：子窗口自证标题")
    ap.add_argument("--raw-b64", default="",
                    help="内部用：子窗口要跑的实际命令（base64，避开 cmd 引号地狱）")
    ap.add_argument("--boot-timeout", type=float, default=120,
                    help="/api/health 等待上限秒数")
    ap.add_argument("--model-timeout", type=float, default=900,
                    help="voxcpm 装载等待上限秒数（CPU 冷启动 220s 起，低内存更久）")
    ap.add_argument("--front-timeout", type=float, default=90,
                    help="前端 :5173 等待上限秒数（vite 实测 ~1s，留点是给冷盘）")
    ap.add_argument("--gsv-timeout", type=float, default=300,
                    help="GSV :9880 等待上限秒数（09-22 实测冷启动要数十秒，秒判=误判）")
    ap.add_argument("--no-pause", action="store_true", help="结尾不等回车（脚本化用）")
    ap.add_argument("--print-python", action="store_true",
                    help="不起任何服务：只打印选定的后端解释器绝对路径（PY-358 唯一后端解释器口径，旧入口已下线仍走此出口）")
    args = ap.parse_args(argv)

    # 出口要在换台之前答：`--print-python` 是"我这台不行，谁的行"的问句本身，
    # 换台重跑（要 psutil）与起后端都与它无关。
    if args.print_python:
        return report_python_choice()

    # 主控台自己也要够格（读端口归属要 psutil）：不合格就换台重跑自己一次。
    rc = reexec_console(args=args)
    if rc is not None:
        return rc

    if args.run_leg:
        raw_argv = None
        if args.raw_b64:
            try:
                decoded = json.loads(base64.b64decode(
                    args.raw_b64.encode("ascii")).decode("utf-8", "replace"))
                if (isinstance(decoded, list) and decoded
                        and all(isinstance(a, str) for a in decoded)):
                    raw_argv = decoded
                else:
                    say(f"[{now()}] --raw-b64 不是 argv 数组 —— 退回内置默认命令")
            except Exception as e:
                say(f"[{now()}] --raw-b64 解不开({type(e).__name__}: {e}) —— "
                    f"将退回内置默认 argv（可能与主控台显示的不一致）")
        return run_leg(args.run_leg, title=args.title, raw_argv=raw_argv)

    ok = True
    try:
        if args.status or args.health_only:
            ok = health_gate(args, probe_only=True)
        else:
            ok = orchestrate(args)
    except KeyboardInterrupt:
        say("\n已中断（只影响本启动器窗口，不动已起的腿）")
        return 130
    say("")
    say(f"[{now()}] 结论: {'全部 PASS（外围 SKIP 不算失败）' if ok else '存在 FAIL —— 见上方逐项原因与日志路径'}")
    say("  后端 http://127.0.0.1:8000/api/docs | 前端 http://localhost:5173 | GSV http://127.0.0.1:9880/docs")
    # 暂停只给"人坐在控制台前"的场景：09-22 19:18 脚本化调用 `--status`
    # 在打印完结论后卡在 input()，调用方等到超时也拿不到退出码。
    if IS_WIN and not args.no_pause and not args.no_health and sys.stdin.isatty():
        try:
            input("按回车关闭本窗口（关窗口不会关掉已起的三条腿）...")
        except Exception:
            pass
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
