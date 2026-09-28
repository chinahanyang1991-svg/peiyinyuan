"""U-2 启动器护栏（PY-340/M-6 的回归门）。

钉住四件事，任何一条回退都算红：
1. 三条腿的默认 argv 里**不得**出现 `--reload`（热重载只能是 `--reload` 显式 opt-in）；
2. 腿一律以 argv 数组 + `shell=False` 直接 exec，`npm` 这类裸名要经 `shutil.which` 解析；
3. `--raw-b64` 桥只认 base64(JSON argv 数组)，非数组载荷（例如一整串 shell 命令）必须被拒；
4. 脚本化/无 tty 的调用方不会卡在 `input()` 上。
"""

from __future__ import annotations

import base64
import json
import subprocess
import sys
import time
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import start_all  # noqa: E402


# --------------------------------------------------------------- 1. 默认不带 --reload
def test_no_leg_defaults_to_reload():
    for lg in start_all.legs():
        assert "--reload" not in lg.argv, f"{lg.key} 默认 argv 里混进了 --reload：{lg.argv}"


def test_reload_flag_appends_it_once_for_backend_only(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(start_all, "decide", lambda lg, restart: "launch")
    monkeypatch.setattr(start_all, "spawn_window",
                        lambda lg, **kw: calls.append(list(lg.argv)) or True)
    monkeypatch.setattr(start_all, "health_gate", lambda args, probe_only=False: True)
    # PY-356 计时大户：这两枚从没桩 wait_for_launch_banner / time.sleep，三条腿于是
    # 每条真等 15s 横幅 + 1.2s 错峰（实测 51.93s / 49.06s）。先例见 leg_scope:76-77。
    monkeypatch.setattr(start_all, "wait_for_launch_banner", lambda *a, **k: True)
    monkeypatch.setattr(start_all.time, "sleep", lambda *_a: None)
    ns = start_all.argparse.Namespace(
        only="backend,frontend,gsv", restart="", restart_all=False,
        reload=True, no_health=True,
    )
    assert start_all.orchestrate(ns) is True
    assert sum(1 for a in calls if "--reload" in a) == 1, calls
    backend = [a for a in calls if any("uvicorn" in x for x in a)][0]
    assert backend.count("--reload") == 1


def test_default_run_has_no_reload_at_all(monkeypatch):
    calls = []
    monkeypatch.setattr(start_all, "decide", lambda lg, restart: "launch")
    monkeypatch.setattr(start_all, "spawn_window",
                        lambda lg, **kw: calls.append(list(lg.argv)) or True)
    monkeypatch.setattr(start_all, "health_gate", lambda args, probe_only=False: True)
    monkeypatch.setattr(start_all, "wait_for_launch_banner", lambda *a, **k: True)
    monkeypatch.setattr(start_all.time, "sleep", lambda *_a: None)
    ns = start_all.argparse.Namespace(
        only="backend,frontend,gsv", restart="", restart_all=False,
        reload=False, no_health=True,
    )
    start_all.orchestrate(ns)
    assert len(calls) == 3
    assert not any("--reload" in a for a in calls), calls


# --------------------------------------------------------------- 2. argv + shell=False
class _Recorder:
    def __init__(self):
        self.popen_calls = []

    def __call__(self, argv, **kw):
        self.popen_calls.append((list(argv), kw))
        raise OSError("stop here: 只需要看到 Popen 的入参")


def test_run_leg_execs_argv_without_shell(monkeypatch, tmp_path):
    rec = _Recorder()
    monkeypatch.setattr(subprocess, "Popen", rec)
    monkeypatch.setattr(start_all, "set_console_title", lambda t: None)
    monkeypatch.setattr(start_all, "owning_pids", lambda port: [])
    monkeypatch.setattr(start_all, "LOG_DIR", tmp_path)
    monkeypatch.setattr(sys, "stdin", type("_NoTty", (), {"isatty": lambda self: False})())
    rc = start_all.run_leg("backend")
    assert rc == 4, rc                                      # 启动失败路径，且没卡在 input
    argv, kw = rec.popen_calls[0]
    assert kw.get("shell") is False, kw
    assert isinstance(argv, list) and argv[0] == sys.executable
    assert "--reload" not in argv


def test_run_leg_real_exec_and_tee(monkeypatch, tmp_path):
    """去掉 shell=True 之后，真跑一条腿仍要通：子进程输出既上屏也落盘。"""
    monkeypatch.setattr(start_all, "set_console_title", lambda t: None)
    monkeypatch.setattr(start_all, "owning_pids", lambda port: [])
    monkeypatch.setattr(start_all, "LOG_DIR", tmp_path)
    monkeypatch.setattr(sys, "stdin", type("_NoTty", (), {"isatty": lambda self: False})())
    argv = [sys.executable, "-X", "utf8", "-c", "print('launcher-tee-probe')"]
    rc = start_all.run_leg("frontend", raw_argv=argv)
    assert rc == 0, rc
    log = (tmp_path / "frontend-5173.log").read_text(encoding="utf-8", errors="replace")
    assert "launcher-tee-probe" in log, log[-400:]
    assert "launch: " in log and "--reload" not in log


def test_bare_program_names_are_resolved_for_shell_false(monkeypatch):
    lg = start_all.Leg("frontend", "PY-Frontend", 5173, ".", ["npm", "run", "dev"], ())
    argv = lg.resolved_argv()
    assert argv[1:] == ["run", "dev"]
    assert argv[0] != "npm" or sys.platform != "win32", "Windows 上 npm 是 npm.cmd，必须解析全路径"
    assert Path(argv[0]).is_absolute() or "npm" in argv[0]


def test_window_spawn_lets_cmd_parse_the_line(monkeypatch, tmp_path):
    r"""开窗必须让 cmd 自己解析整行（字符串 + shell=True）。

    本题的反面就是这条测试原来的写法：它把"list-form + shell=False"钉成了契约，
    而那个形式在 Windows 上是**坏的** —— Python 把行内的 `"` 转义成 `\"`，cmd 于是把
    窗口标题 `PY-Backend` 当成要执行的文件（实测「系统找不到文件 \PYBackend\」），
    `start` 静默失败，启动器却照样打印"已开窗"。09-24 14:03 那次重启两条腿都没起来
    就是这么丢的（14:30 用 shell=True 重跑，两腿的 launch 标记都真落盘了）。
    行为侧的另一半（"没等到标记就不许认账"）在 tests/test_launcher_leg_scope.py。
    """
    seen = {}

    def fake_run(args, **kw):
        seen["args"] = args
        seen["kw"] = kw
        raise OSError("boom")                              # 走 except 分支也无妨，只要看清入参

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(start_all, "IS_WIN", True)
    # PY-356（全量回归门里"测试起了真后端"）：except 分支原来没有 return，会掉进
    # 兜底 Popen —— 用**生产 argv**（legs() 钉死 :8000）在 pytest 进程里真起一台后端，
    # 抢用户的 :8000（抢不动也先跑完 startup 钩子、往工作树 data/ 写库）。
    # 现在：LOG_DIR 改指 tmp（绝不落盘真 logs/）、Popen 换记账桩（绝不真 exec）、
    # 断言"默认不许兜底"⇒ 返回 False 且一次 Popen 都没有。
    rec = _Recorder()
    monkeypatch.setattr(subprocess, "Popen", rec)
    monkeypatch.setattr(start_all, "LOG_DIR", tmp_path)
    lg = start_all.legs()[0]
    assert start_all.spawn_window(lg) is False,               \
        "开窗失败后仍返回 True —— 调用方(orchestrate)接不住，兜底会回来"
    assert rec.popen_calls == [], f"开窗失败的用例竟起了进程: {rec.popen_calls}"
    assert not list(tmp_path.glob("*.log")),           \
        "兜底分支落了盘 —— 全量门里这就是'测试起了真后端'"
    assert isinstance(seen["args"], str), seen
    assert seen["args"].startswith('start "PY-Backend" cmd /k "'), seen["args"]
    assert seen["kw"].get("shell") is True


def test_spawn_window_hidden_fallback_needs_explicit_authority(monkeypatch, tmp_path):
    """PY-356 正向半：CLI 主路径（显式授权 + 开窗探测失败）兜底仍要起，argv 忠实、日志留 launch: 头。

    没有这一枚，`return False` 可以被改成"永远不起"而无人报警（兜底那扇门就无声消失了）；
    `launch:` 头是事后归因"这台是谁起的"的唯一抓手，摘掉它就等于回到查不了案发现场。
    """
    class _Starts:
        """记账桩：只算"真去 exec 一条腿"的 Popen（依赖探测那种子进程不算，别误伤）。"""

        def __init__(self):
            self.legs = []

        def __call__(self, argv, **kw):
            if "backend.main:app" in [str(a) for a in argv]:
                self.legs.append((list(argv), kw))
            return types.SimpleNamespace(pid=424242)

    st = _Starts()
    monkeypatch.setattr(subprocess, "Popen", st)
    monkeypatch.setattr(start_all, "LOG_DIR", tmp_path)
    lg = start_all.legs()[0]

    # 开窗探测**成功**（真·CLI 主路径的常态）：一条腿都不该被兜底带起
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: types.SimpleNamespace(returncode=0))
    assert start_all.spawn_window(lg, allow_hidden_fallback=True) is True
    assert st.legs == [], f"开窗成功却偷偷兜底起了一条腿: {st.legs}"

    # 开窗探测失败 + 显式授权：兜底起台（argv 是生产 argv），且落一条带 launch: 的归因行
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("开窗探针")))
    assert start_all.spawn_window(lg, allow_hidden_fallback=True) is True
    assert len(st.legs) == 1, f"授权兜底只该起一次: {st.legs}"
    argv, kw = st.legs[0]
    tail = ["-m", "uvicorn", "backend.main:app", "--host", "127.0.0.1", "--port", "8000"]
    assert argv[-len(tail):] == tail, argv
    assert Path(kw["cwd"]) == lg.cwd
    log = (tmp_path / "backend-8000.log").read_text(encoding="utf-8", errors="replace")
    assert "launch: " in log and lg.display in log, log


def test_legacy_launchers_are_gone():
    """U-1（2026-09-27）：旧三枚已下线 —— 本条在删除落地前必然是红的，红=删除确实在测。
    "新入口不偷偷带热重载"这半判据仍由本文件上方对 start_all.py 的 argv 用例群守着。"""
    for name in ("start.bat", "start.ps1", "start_gui.py"):
        assert not (ROOT / name).exists(), f"旧入口 {name} 又回到盘上：--reload 会从旧门回来"


# --------------------------------------------------------------- 3. --raw-b64 桥
def _b64(obj) -> str:
    raw = obj if isinstance(obj, str) else json.dumps(obj)
    return base64.b64encode(raw.encode("utf-8")).decode("ascii")


def test_raw_b64_accepts_only_an_argv_list(monkeypatch):
    captured = {}
    monkeypatch.setattr(start_all, "run_leg",
                        lambda key, title="", raw_argv=None:
                        captured.update(key=key, raw_argv=raw_argv) or 0)

    argv = ["python", "-m", "uvicorn", "backend.main:app"]
    start_all.main(["--run-leg", "backend", "--raw-b64", _b64(argv)])
    assert captured["raw_argv"] == argv

    captured.clear()
    evil = "python -m uvicorn backend.main:app --port 8000"   # 一整串命令，不是数组
    start_all.main(["--run-leg", "backend", "--raw-b64", _b64(evil)])
    assert captured["raw_argv"] is None, "整串 shell 命令被接受了 —— 桥必须只认 argv 数组"

    captured.clear()
    start_all.main(["--run-leg", "backend", "--raw-b64", _b64({"a": 1})])
    assert captured["raw_argv"] is None
