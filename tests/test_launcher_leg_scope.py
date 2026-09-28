r"""启动器"开窗"与"健康门查谁"两处缺陷的回归锁（2026-09-24 两次重启实测）。

缺陷一（14:03 那次重启）：`spawn_window` 用 list-form 调 cmd，Python 把行内的 `"`
转义成 `\"`，cmd 于是把窗口标题当成要执行的文件（「系统找不到文件 \PYBackend\」），
`start` 静默失败 —— 而启动器照样打印"已开窗"。用户看到的就是"双击了，窗口没出来，
话却说开了"。修法是让 cmd 自己解析这一整行（shell=True），并且**拿日志自证**：
新的 `launch:` 标记没出现就不认账。

缺陷二（14:41 那次重启）：`--only backend,frontend` 的健康门照样等 GSV，
把 `--gsv-timeout` 300s 全烧完然后报一条 FAIL —— 那条 FAIL 是"我没要求它起"造出来的。
"""

import os
import subprocess
import sys
import threading
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import start_all  # noqa: E402


def _leg(tmp_path, monkeypatch, key="backend", port=8000):
    """日志目录改指 tmp：别往真 `peiyinyuan/logs/` 里写测试产物。"""
    monkeypatch.setattr(start_all, "LOG_DIR", tmp_path / "logs")
    (tmp_path / "logs").mkdir(parents=True, exist_ok=True)
    return start_all.Leg(key, f"PY-{key.capitalize()}", port, tmp_path,
                         [sys.executable, "-c", "print(1)"], signs=("x",))


# ------------------------------------------------------------------ 开窗自证
def test_wait_for_launch_banner_only_counts_new_bytes(tmp_path, monkeypatch):
    """只有**本次新增**的字节里有 launch: 才算开窗成功；上次运行留下的标记不作数。"""
    lg = _leg(tmp_path, monkeypatch)
    lg.log_path.write_bytes("===== launch: 上一次运行留下的 =====\n".encode("utf-8"))
    since = start_all._log_size(lg)
    assert start_all.wait_for_launch_banner(lg, since, timeout=0.6) is False, \
        "旧日志里的 launch 标记被当成了本腿的证据"

    threading.Timer(0.15, lambda: lg.log_path.open("ab").write(
        b"===== [2026-09-24] PY-Backend launch: python -m uvicorn ...\n")).start()
    assert start_all.wait_for_launch_banner(lg, since, timeout=3.0) is True


def test_spawn_window_lets_cmd_parse_the_line(tmp_path, monkeypatch):
    """开窗那行必须交给 cmd 自己解析（shell=True + 单个字符串）。

    退回 list-form 时 Python 会转义内层引号 ⇒ 标题被当成文件名 ⇒ 窗口静默不开。
    PY-356 之后这里再多钉一层：Popen 必须桩住 —— 本用例起的是**真腿**（legs() 把后端
    端口钉死 8000），开窗一旦静默失败，没桩的兜底就会在测试进程里起一台真后端。
    """
    if not start_all.IS_WIN:
        pytest.skip("start/cmd 是 Windows 分支；非 Windows 走 Popen 兜底")
    seen = {}
    popen_calls = []

    def _run(cmd, **kw):
        seen["cmd"] = cmd
        seen["kw"] = kw
        return types.SimpleNamespace(returncode=0)

    def _popen(argv, **kw):
        popen_calls.append((list(argv), kw))
        raise AssertionError("开窗成功的用例不该走到 Popen")

    monkeypatch.setattr(start_all.subprocess, "run", _run)
    monkeypatch.setattr(subprocess, "Popen", _popen)
    assert start_all.spawn_window(_leg(tmp_path, monkeypatch)) is True
    assert popen_calls == [], popen_calls
    assert seen["kw"].get("shell") is True, seen
    assert isinstance(seen["cmd"], str), \
        f"list-form 会重新转义内层引号（实测报『系统找不到文件 \\PYBackend\\』）：{seen['cmd']!r}"
    assert seen["cmd"].startswith('start "PY-Backend" cmd /k "'), seen["cmd"]


def test_spawn_window_without_authority_does_not_start_anything(tmp_path, monkeypatch):
    """PY-356：Windows 上开窗探测失败 ⇒ 返回 False、一条进程都不起、一个字节都不落盘。

    这枚是产品语义的回归锁（旧行为：except 后没有 return ⇒ 掉进兜底 Popen 起真后端，
    全量回归门里就是这么抢了用户那台 :8000 并往工作树 data/ 写的）。
    """
    lg = _leg(tmp_path, monkeypatch)
    monkeypatch.setattr(start_all, "IS_WIN", True)
    popen_calls = []

    def _popen(argv, **kw):
        popen_calls.append((list(argv), kw))
        raise AssertionError("不该被调用")

    def _run(cmd, **kw):
        raise OSError("开窗探针（生产里对应 start 静默失败）")

    monkeypatch.setattr(start_all.subprocess, "run", _run)
    monkeypatch.setattr(subprocess, "Popen", _popen)
    assert start_all.spawn_window(lg) is False
    assert popen_calls == [], popen_calls
    assert not list((tmp_path / "logs").glob("*.log")), "兜底把日志落了盘 = 它真想起一条腿"


def test_orchestrate_does_not_claim_a_window_it_cannot_prove(tmp_path, monkeypatch, capsys):
    """行为判据（U-6 口径）：banner 没等到 ⇒ 不许说"已开窗"，必须明说"未证实"。"""
    lg = _leg(tmp_path, monkeypatch)
    monkeypatch.setattr(start_all, "legs", lambda: [lg])
    monkeypatch.setattr(start_all, "leg_by_key", lambda key: lg)
    monkeypatch.setattr(start_all, "decide", lambda *a, **k: "launch")
    monkeypatch.setattr(start_all, "spawn_window", lambda *_a, **_k: True)
    monkeypatch.setattr(start_all, "wait_for_launch_banner", lambda *a, **k: False)
    monkeypatch.setattr(start_all.time, "sleep", lambda *_a: None)
    args = types.SimpleNamespace(only="backend", restart="", restart_all=False,
                                 reload=False, no_health=True)
    assert start_all.orchestrate(args) is True      # no_health：本题只看开窗那段
    out = capsys.readouterr().out
    assert "未证实" in out, out
    assert "已开窗" not in out, out


def test_orchestrate_reports_opened_window_when_banner_arrives(tmp_path, monkeypatch, capsys):
    """另一侧：等到了标记才许说"已开窗"（防把守卫写成"永远报警"）。"""
    lg = _leg(tmp_path, monkeypatch)
    monkeypatch.setattr(start_all, "legs", lambda: [lg])
    monkeypatch.setattr(start_all, "leg_by_key", lambda key: lg)
    monkeypatch.setattr(start_all, "decide", lambda *a, **k: "launch")
    monkeypatch.setattr(start_all, "spawn_window", lambda *_a, **kw: True)
    monkeypatch.setattr(start_all, "wait_for_launch_banner", lambda *a, **k: True)
    monkeypatch.setattr(start_all.time, "sleep", lambda *_a: None)
    args = types.SimpleNamespace(only="backend", restart="", restart_all=False,
                                 reload=False, no_health=True)
    assert start_all.orchestrate(args) is True
    out = capsys.readouterr().out
    assert "已开窗" in out and "未证实" not in out, out


# ------------------------------------------------------------------ 健康门查谁
@pytest.fixture
def stub_gates(monkeypatch):
    """把下游门换成记账桩，只看 `health_gate` 决定查哪几条腿、给多少预算。"""
    calls = {"backend": [], "engine": [], "url": [], "gate": None}
    monkeypatch.setattr(start_all, "say", lambda *a, **k: None)
    monkeypatch.setattr(start_all, "hdr", lambda *a, **k: None)
    monkeypatch.setattr(start_all, "probe_optional", lambda g: None)
    monkeypatch.setattr(start_all, "read_instance_tier",
                        lambda port=8000, timeout=4.0: (None, None, "桩：不查档位"))

    def _capture(g):
        calls["gate"] = g

    def _backend(g, timeout, *a, **k):
        _capture(g)
        calls["backend"].append(timeout)
        return True

    def _engine(g, args, probe_only, *a, **k):
        _capture(g)
        calls["engine"].append(probe_only)
        return True

    def _url(g, name, url, expect=(200,), log="", wait=0.0, quiet=20.0):
        _capture(g)
        calls["url"].append((name, wait))
        return True

    monkeypatch.setattr(start_all, "gate_backend_health", _backend)
    monkeypatch.setattr(start_all, "gate_engine", _engine)
    monkeypatch.setattr(start_all, "gate_url", _url)
    return calls


def _args():
    return types.SimpleNamespace(boot_timeout=120, model_timeout=900,
                                 front_timeout=90, gsv_timeout=300)


def _rows(calls):
    assert calls["gate"] is not None, "一条门都没进 ⇒ wanted 拼错了？"
    return [(r[0], r[1]) for r in calls["gate"].rows]


def test_only_selected_legs_get_a_startup_budget(stub_gates):
    """`--only backend,frontend` ⇒ GSV 那条腿连问都不问，更不给 300s。"""
    assert start_all.health_gate(_args(), wanted=["backend", "frontend"]) is True
    names = [n for n, _w in stub_gates["url"]]
    assert names == ["前端 localhost:5173 = 200"], names
    assert ("GSV 127.0.0.1:9880/docs = 200", 300) not in stub_gates["url"], \
        "未选的腿又烧掉整个启动预算"
    assert ("GSV :9880", "SKIP") in _rows(stub_gates), _rows(stub_gates)


def test_default_still_checks_all_three_legs(stub_gates):
    """不传 wanted（`--status` 全量探活）⇒ 三条腿一条都不能少，防止改过头。"""
    assert start_all.health_gate(_args()) is True
    assert len(stub_gates["url"]) == 2, stub_gates["url"]
    assert [w for _n, w in stub_gates["url"]] == [90, 300], stub_gates["url"]
    assert stub_gates["backend"] == [120], stub_gates["backend"]
    assert [r for r in _rows(stub_gates) if r[1] == "SKIP"] == [], _rows(stub_gates)


def test_unselected_backend_skips_engine_and_model_gates(stub_gates):
    """只起前端时连档位/引擎都不问 —— 那条 FAIL 会是"我没要求它起"造出来的。"""
    assert start_all.health_gate(_args(), wanted=["frontend"]) is True
    assert stub_gates["backend"] == [] and stub_gates["engine"] == [], stub_gates
    assert [r for r in _rows(stub_gates) if r[1] == "SKIP"] == \
        [("后端 :8000", "SKIP"), ("GSV :9880", "SKIP")], _rows(stub_gates)


def test_gsv_only_never_waits_on_the_backend_boot_budget(stub_gates):
    """反向：`--only gsv` 时后端 120s 启动预算不该出场，GSV 拿自己的 300s。"""
    assert start_all.health_gate(_args(), wanted=["gsv"]) is True
    assert stub_gates["backend"] == [], stub_gates
    assert stub_gates["url"] == [("GSV 127.0.0.1:9880/docs = 200", 300)], stub_gates["url"]
