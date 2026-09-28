"""LAUNCHER-V3：`--status` 只探活，不该付冷启动的等待预算。

09-22 19:18 实测：三条腿没起时 `--status` 跑了 6 分 41 秒才交出结论，
因为 gate_url 沿用了启动路径的 wait（前端 90s / GSV 300s）—— 那些预算是给
"启动器刚亲手拉起、正在装模型"的进程留的，而 `--status` 什么都没拉起。
本测试锁住"探活模式不等启动"，并且锁住启动模式的预算没被一起改掉。
"""

import os
import sys
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import start_all


@pytest.fixture
def recorder(monkeypatch):
    """把四只探针换成记账桩，只关心传进来的等待预算。"""
    seen = {"model": [], "url": [], "backend": []}

    monkeypatch.setattr(start_all, "Gate", lambda: types.SimpleNamespace(
        add=lambda *a, **k: None, rows=[]))
    monkeypatch.setattr(start_all, "probe_optional", lambda g: None)
    monkeypatch.setattr(start_all, "hdr", lambda *a, **k: None)
    monkeypatch.setattr(start_all, "say", lambda *a, **k: None)

    def _backend(g, timeout, *a, **k):
        seen["backend"].append(timeout)
        return True

    def _model(g, timeout, *a, **k):
        seen["model"].append(k.get("dead_after"))
        seen["backend"].append(timeout)
        return True

    def _url(g, name, url, *a, **k):
        seen["url"].append((name, k.get("wait")))
        return True

    monkeypatch.setattr(start_all, "gate_backend_health", _backend)
    monkeypatch.setattr(start_all, "gate_model_loaded", _model)
    monkeypatch.setattr(start_all, "gate_url", _url)
    # 档位门会先问实例"你是哪一档"。这里必须钉成 local，否则本机在跑的那台
    # （默认在线档）会让 gate_model_loaded 根本不被调用 —— 断言会随环境漂。
    monkeypatch.setattr(start_all, "read_instance_tier",
                        lambda port=8000, timeout=4.0: ("local", {}, None))
    return seen


def _args():
    return types.SimpleNamespace(boot_timeout=120, model_timeout=900,
                                 front_timeout=90, gsv_timeout=300)


def test_status_mode_waits_for_nothing(recorder):
    assert start_all.health_gate(_args(), probe_only=True) is True
    assert [w for _, w in recorder["url"]] == [0.0, 0.0], recorder["url"]
    # 后端/模型的等待也要压到"看一眼"的量级，不能沿用 900s 装载预算
    assert 15.0 in recorder["backend"], recorder["backend"]
    assert 5.0 in recorder["model"], recorder["model"]


def test_launch_mode_keeps_startup_budgets(recorder):
    start_all.health_gate(_args())
    assert dict(recorder["url"]) == {
        "前端 localhost:5173 = 200": 90,
        "GSV 127.0.0.1:9880/docs = 200": 300,
    }, recorder["url"]
    assert 900 in recorder["backend"]
    assert 120 in recorder["model"]


def test_main_routes_status_to_probe_only(monkeypatch):
    """`--status` / `--health-only` 必须真把 probe_only 传下去（否则前两条锁的是死代码）。"""
    calls = []
    monkeypatch.setattr(start_all, "health_gate",
                        lambda args, probe_only=False: calls.append(probe_only) or True)
    monkeypatch.setattr(start_all, "orchestrate",
                        lambda args: calls.append("orchestrate") or True)
    for argv in (["--status"], ["--health-only"]):
        calls.clear()
        assert start_all.main(argv + ["--no-pause"]) == 0
        assert calls == [True], argv
    calls.clear()
    assert start_all.main(["--no-pause"]) == 0
    assert calls == ["orchestrate"]


def test_status_never_blocks_a_scripted_caller(monkeypatch):
    """09-22 19:18 实测：脚本跑 `--status`（没带 --no-pause）打完结论后卡在 input()。

    收尾暂停是给"人双击开窗口"的，非 TTY 调用方必须直接拿到退出码。
    """
    monkeypatch.setattr(start_all, "health_gate", lambda a, **k: True)

    # 产品里 input() 外层有 `except Exception: pass`，抛异常的探针会被它吞掉
    # 变成恒真断言 —— 所以这里只记账，不抛。
    asked = []
    monkeypatch.setattr(start_all, "input",
                        lambda prompt="": asked.append(prompt), raising=False)

    monkeypatch.setattr(start_all.sys, "stdin", types.SimpleNamespace(isatty=lambda: False))
    assert start_all.main(["--status"]) == 0
    assert asked == [], f"非 TTY 不该问回车，实际问了 {asked}"

    # 反向：真控制台仍然要问（否则用户双击看完就崩窗口，抄不到原因）
    monkeypatch.setattr(start_all.sys, "stdin", types.SimpleNamespace(isatty=lambda: True))
    assert start_all.main(["--status"]) == 0
    assert len(asked) == 1 and "回车" in asked[0], asked
