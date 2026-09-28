"""指令#1 的漏网之鱼：启动器健康门还按"本地档"的老口径判在线档。

现象（本轮改代码后自查发现，未上线前就抓到）：默认档位改成在线档后
`backend/main.py` 不再在启动时装载 voxcpm（`preload_local_at_start()` 返回 False），
而 `start_all.py` 的健康门无条件等 `/api/system/model_status` 到 `ready` ——
于是一次**完全正常**的在线档启动会被报成 FAIL（还要先白等 `--model-timeout` 900s）。
用户双击 `start-all.cmd` 看到的就是"启动器说失败"，正是指令#2 抱怨的那类"看起来坏了"。

口径：引擎门必须跟着**在跑的那台实例自陈的档位**走，而不是启动器自己猜 env。
"""

import http.server
import json
import os
import socket
import sys
import threading
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import start_all  # noqa: E402

ONLINE_PAYLOAD = {
    "status": "ok",
    "tier": "online",
    "modes": [
        {"tier": "online", "engine": "edgetts", "engine_display": "微软 Edge 在线语音",
         "available": True, "reason": None, "online": True, "unverified_cloud": False},
        {"tier": "local", "engine": "voxcpm", "engine_display": "本地大模型",
         "available": True, "reason": None, "online": False, "unverified_cloud": False},
    ],
    "engines": [],
    "error": None,
}


@pytest.fixture
def gates(monkeypatch):
    """下游门的记账桩。真实 `gate_engine` 不桩掉 —— 它才是本题的判据。

    行记在 `health_gate` 自己 new 的 Gate 上，所以从 `gate_backend_health` 那次调用里
    把实例截下来（`seen["gate"].rows`），这样断言看的是最终账，不是桩的账。
    """
    seen = {"model": [], "backend": [], "gate": None}

    monkeypatch.setattr(start_all, "say", lambda *a, **k: None)
    monkeypatch.setattr(start_all, "hdr", lambda *a, **k: None)
    monkeypatch.setattr(start_all, "probe_optional", lambda g: None)
    monkeypatch.setattr(start_all, "gate_url", lambda *a, **k: True)

    def _backend(g, timeout, *a, **k):
        seen["backend"].append(timeout)
        seen["gate"] = g
        return True

    def _model(g, timeout, *a, **k):
        seen["model"].append((timeout, k.get("dead_after")))
        g.add("引擎 voxcpm 已装载 (ready)", "PASS", "桩：本地档硬门")
        return True

    monkeypatch.setattr(start_all, "gate_backend_health", _backend)
    monkeypatch.setattr(start_all, "gate_model_loaded", _model)
    return seen


def _args():
    return types.SimpleNamespace(boot_timeout=120, model_timeout=900,
                                 front_timeout=90, gsv_timeout=300)


def _rows(seen):
    return [(r[0], r[1]) for r in seen["gate"].rows]


def test_online_tier_never_waits_for_the_local_model(gates, monkeypatch):
    """在线档：不等 5GB 权重，900s 装载预算根本不该出场。"""
    monkeypatch.setattr(start_all, "read_instance_tier",
                        lambda port=8000, timeout=4.0: ("online", ONLINE_PAYLOAD, None))
    assert start_all.health_gate(_args()) is True
    assert gates["model"] == [], f"在线档仍去等 voxcpm ready: {gates['model']}"
    name, verdict, detail = gates["gate"].rows[0]
    assert name == "默认档位引擎就绪 (在线)" and verdict == "PASS", gates["gate"].rows
    assert "启动不装载" in detail and "按需装载" in detail, detail


def test_online_tier_fails_when_its_own_engine_is_unusable(gates, monkeypatch):
    """在线档也不放水：在线引擎本身不可用（断网/依赖缺）必须是 FAIL。"""
    payload = json.loads(json.dumps(ONLINE_PAYLOAD))
    payload["modes"][0].update({"available": False, "reason": "依赖 ffmpeg 缺失"})
    monkeypatch.setattr(start_all, "read_instance_tier",
                        lambda port=8000, timeout=4.0: ("online", payload, None))
    assert start_all.health_gate(_args()) is False
    assert gates["model"] == [], gates["model"]
    name, verdict, detail = gates["gate"].rows[0]
    assert verdict == "FAIL" and "依赖 ffmpeg 缺失" in detail, gates["gate"].rows


def test_local_tier_keeps_the_hard_ready_gate(gates, monkeypatch):
    """本地档维持原口径：必须真 ready，等待预算仍是 900s/120s。"""
    monkeypatch.setattr(start_all, "read_instance_tier",
                        lambda port=8000, timeout=4.0: ("local", {}, None))
    assert start_all.health_gate(_args()) is True
    assert gates["model"] == [(900, 120)], gates["model"]
    assert ("引擎 voxcpm 已装载 (ready)", "PASS") in _rows(gates), _rows(gates)


def test_unavailable_local_engine_warns_but_does_not_block_online_start(gates, monkeypatch):
    """在线档启动时本地权重坏了不该拦住开机，但必须看得见（WARN 而非沉默）。"""
    payload = json.loads(json.dumps(ONLINE_PAYLOAD))
    payload["modes"][1].update({"available": False, "reason": "权重目录不存在"})
    monkeypatch.setattr(start_all, "read_instance_tier",
                        lambda port=8000, timeout=4.0: ("online", payload, None))
    assert start_all.health_gate(_args()) is True
    assert ("本地档引擎（切档后可用性）", "WARN") in _rows(gates), _rows(gates)


def test_missing_endpoint_falls_back_to_ready_gate_not_to_pass(gates, monkeypatch):
    """读不到档位（老构建没这端点 / 后端还在起）⇒ 退回硬门，绝不当成在线档放行。"""
    monkeypatch.setattr(start_all, "read_instance_tier",
                        lambda port=8000, timeout=4.0: (None, None, "HTTP 404"))
    assert start_all.health_gate(_args()) is True
    assert gates["model"] == [(900, 120)], gates["model"]


def test_degraded_self_report_fails_fast(gates, monkeypatch):
    """端点在但自陈 degraded：答案不可信就明说，别拿 900s 去等一台可能没模型的进程。"""
    monkeypatch.setattr(start_all, "read_instance_tier",
                        lambda port=8000, timeout=4.0:
                        (None, {"status": "degraded", "error": "registry 抛异常"},
                         "status=degraded"))
    assert start_all.health_gate(_args()) is False
    assert gates["model"] == [], gates["model"]
    assert gates["gate"].rows[0][:2] == ("引擎档位自陈", "FAIL"), gates["gate"].rows


def test_probe_only_shrinks_the_fallback_budget(gates, monkeypatch):
    """`--status` 什么都没起：退回硬门时也只能给"看一眼"的预算（不能沿用 900s）。"""
    monkeypatch.setattr(start_all, "read_instance_tier",
                        lambda port=8000, timeout=4.0: (None, None, "ConnectionRefusedError"))
    assert start_all.health_gate(_args(), probe_only=True) is True
    assert gates["model"] == [(15.0, 5.0)], gates["model"]


# ------------------------------------------------------------------ 真实 HTTP 往返
class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):                                # noqa: N802
        body = json.dumps(ONLINE_PAYLOAD).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):                      # 别把测试输出灌进 stderr
        pass


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_read_instance_tier_parses_a_real_http_response():
    """不桩 http_get：真开一枚监听套接字走一遍，锁住"档位是从实例读来的"（零外网）。"""
    port = _free_port()
    httpd = http.server.HTTPServer(("127.0.0.1", port), _Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        tier, payload, why = start_all.read_instance_tier(port, timeout=5)
        assert why is None, why
        assert tier == "online", tier
        assert [m["engine"] for m in payload["modes"]] == ["edgetts", "voxcpm"]
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_read_instance_tier_reports_why_when_nothing_listens():
    """没人监听时必须给出**原因原文**：健康门据此退回硬门，而不是静默放行。"""
    tier, payload, why = start_all.read_instance_tier(_free_port(), timeout=2)
    assert tier is None and payload is None
    assert why, "读不到档位却不给原因 ⇒ 门会误判成老构建/在线档"


def test_launcher_and_backend_agree_on_the_tier_source():
    """护栏：健康门问的端点必须真在产品里（改名/摘掉时本测试先红，别让门变哑）。"""
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "backend", "main.py"), encoding="utf-8").read()
    assert '@app.get("/api/system/engines")' in src
    assert "read_instance_tier" in open(os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "start_all.py"), encoding="utf-8").read()
