"""PY-348：实例"自己消失"必须下次可判 —— 日志默认落盘 + 心跳/退出留痕 + 启动器不强杀。

定罪背景在 qa/issues.md PY-346：06:47:40 生产 :8000 还在（工作树实例撞 10048 退出），
06:54 起拒连，而这台实例的 stdout/stderr 一个字节都没落盘 ⇒ 三种死法（内存耗尽 /
未捕获异常 / 外部强杀）在盘上留的是同一张"什么都没有"。本文件钉的是**判别手段本身**。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend import runtime_guard  # noqa: E402
from backend.main import app  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


def _run_detached(tmp_path: Path, code: str) -> dict:
    """把 runtime_guard 复制到临时树里、剥掉 PYTEST_* 后真跑一次。

    为什么绕这一圈：repo_root() 跟着 __file__ 走（PY-345 同一枚机制），复制到
    tmp_path/backend/ 下就能在纯树里看它真实写盘，不污染主检出的 logs/；
    而 PYTEST_* 不清掉的话，子进程会被判成"测试进程"、三件套全关，测不到激活分支。
    """
    dst = tmp_path / "backend"
    dst.mkdir(parents=True, exist_ok=True)
    shutil.copy(ROOT / "backend" / "runtime_guard.py", dst / "runtime_guard.py")
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("PYTEST_")}
    env["PYTHONPATH"] = str(tmp_path)
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run(
        [sys.executable, "-X", "utf8", "-c", code],
        cwd=str(tmp_path), env=env, capture_output=True, text=True,
        encoding="utf-8", timeout=90)
    assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"
    last = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""
    return json.loads(last) if last.startswith("{") else {}


def test_explicit_env_still_wins(tmp_path):
    p = tmp_path / "given.log"
    got = runtime_guard.resolve_log_file({"PYY_LOG_FILE": str(p)})
    assert got == p


def test_test_process_writes_nothing(tmp_path):
    # 测试进程里返回 None ⇒ pytest import backend.main 上百次不会凭空造日志文件
    assert runtime_guard.resolve_log_file({}) is None
    assert runtime_guard.start(port=8999) is None


def test_default_log_file_is_in_repo_logs(tmp_path):
    got = _run_detached(tmp_path, (
        "import json,os;from backend import runtime_guard as g;"
        "p=g.resolve_log_file(os.environ);"
        "print(json.dumps({'path': str(p) if p else None,"
        " 'is_test': g.is_test_process(), 'pid': os.getpid(),"
        " 'logs_dir': str(g.repo_root()/'logs')}))"))
    assert got["is_test"] is False, "剥掉 PYTEST_* 后应当判为真进程"
    path = Path(got["path"])
    assert path.parent == Path(got["logs_dir"])
    assert re.fullmatch(r"backend-\d{8}-\d{6}-p\d+\.log", path.name), path.name
    assert str(got["pid"]) in path.name
    assert not path.exists(), "resolve 只定路径，不许顺手把文件建出来"


def test_heartbeat_and_exit_lines_are_written(tmp_path):
    got = _run_detached(tmp_path, (
        "import json,time;from backend import runtime_guard as g;"
        "t=g.start(port=8999, base_dir='X', interval_s=0.2);"
        "time.sleep(0.5);"
        "lines=open(g.liveness_path(),encoding='utf-8').read().splitlines();"
        "print(json.dumps({'first': lines[0], 'last': lines[-1]}))"))
    first = json.loads(got["first"])
    assert first["event"] == "start" and first["port"] == 8999
    hb = json.loads(got["last"])
    assert hb["event"] == "heartbeat"
    assert {"ts", "pid", "rss_mb", "free_gb"} <= set(hb), hb

    # atexit 的 exit 行：进程正常退出时必须补上，这是"自己退的"与"被强杀"的分界
    _run_detached(tmp_path, (
        "from backend import runtime_guard as g;"
        "g.start(port=8999, base_dir='X', interval_s=0.2);print('ok')"))
    lines = (tmp_path / "logs" / "liveness.log").read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[-1])["event"] == "exit", lines[-1]


def test_missing_liveness_file_reads_empty(tmp_path):
    assert runtime_guard.read_last_events(3, path=tmp_path / "nope.log") == []


def test_liveness_endpoint_contract():
    with TestClient(app) as client:
        r = client.get("/api/system/liveness")
        assert r.status_code == 200, r.text
        body = r.json()
        assert set(["log_file", "exists", "last_events"]) <= set(body)
        assert isinstance(body["last_events"], list)
        assert body["log_file"].endswith("liveness.log")
        # 越界参数不得炸，也不得让人一次拉走整个文件
        assert client.get("/api/system/liveness?count=0").status_code == 200
        assert len(client.get("/api/system/liveness?count=9999").json()["last_events"]) <= 50


def test_main_does_not_open_a_log_file_during_pytest():
    # 反向护栏：这条若变红，说明测试进程开始往 logs/ 里写文件了（938 项门会自造脏数据）
    assert app is not None
    import backend.main as m
    assert m._GUARD_LOG_PATH is None


def test_legacy_entrypoints_are_gone():
    """U-1（2026-09-27）：旧三枚入口已下线（本条在删除落地前必然是红的，红=删除确实在测）。
    原先钉在 start.bat 上的两条判据——"Full Start 不自动强杀在跑实例"与"杀端口前先归因"——
    由 start_all.py 的 decide() 承担并已在 tests/test_launcher_backend_python.py
    （TestUnattributableIsNotForeign：外部进程 blocked、读不到判 unattributable、绝不盲杀）守着。"""
    for name in ("start.bat", "start.ps1", "start_gui.py"):
        assert not (ROOT / name).exists(), f"旧入口 {name} 又回到盘上"
