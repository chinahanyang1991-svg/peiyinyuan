"""指令#1「两套配音模式（在线默认 + 本地保留）」的接线判据。

钉的是**切换真的生效**这一件事，不是"配置项存在"。四张地图上的坑都来自同一句话
—— 主链上写死了引擎名，于是改配置是无效的：

  1. P7 句级路由：`settings.get("engine", "voxcpm")`（旧）—— 切了在线档仍烧本地模型；
  2. P4 分句策略：只看 `settings["engine"]`，缺位时用默认档（默认=voxcpm 的短句策略），
     与 P7 实际路由的引擎不同源 —— 分句口径会漂；
  3. 任务入口 `POST /api/tasks/convert`：`settings.tier` 写错要到 P4/P7 才炸；
  4. 模板出口 `POST /api/workflows/templates/{id}/run`：给自建模板硬补 "voxcpm"。

引擎全部用桩：不联网、不加载权重、不碰生产库。
"""
import asyncio
import os
import sys
import types

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from backend.config import Config                                   # noqa: E402
from modules.tts_engine import tier as tts_tier                     # noqa: E402
from modules.tts_engine.engine_registry import engine_registry      # noqa: E402
from modules.voice_manager import voice_store as vs_mod             # noqa: E402
from modules.workflow import cast_pipeline_hooks as hooks_mod       # noqa: E402
from modules.workflow import conversion_workflow as wmod            # noqa: E402
from tests.test_voice_preview_fast import _row                      # noqa: E402


class _RouteEngine:
    """句级路由桩：只记录"谁被叫去发了声"。"""

    def __init__(self, name, online, sample_rate=24000):
        self.engine_name = name
        self.sample_rate = sample_rate
        self.online = online
        self.calls = []

    def synthesize(self, text=None, voice_id=None, **kw):
        self.calls.append(voice_id)
        return np.full(12000, 0.4, dtype=np.float32)


def _fake_registry(monkeypatch, fakes):
    """`is_online_engine` 走 `engine_registry.get`，所以在线性由桩说了算。"""
    monkeypatch.setattr(engine_registry, "get", lambda name: fakes.get(name))
    monkeypatch.setattr(engine_registry, "get_loaded",
                        lambda name, strict=True: fakes[name])
    monkeypatch.setattr(engine_registry, "is_known_engine",
                        lambda name: bool(name) and name in fakes)


def _online_local_pair():
    return {name: _RouteEngine(name, online)
            for name, online in (("edgetts", True), ("voxcpm", False))}


async def _drive_p7(monkeypatch, settings, row, sentences=None):
    """跑一次真实的 P7（`_stage_synthesize`），返回 (context, 引擎桩表)。

    档位相关的账都记在 `context` 上（跨阶段状态的唯一载体，同 `engine_name`
    /`skipped_unpronounceable` 的口径），所以断言读 ctx 而不是阶段返回值。
    """
    fakes = _online_local_pair()
    _fake_registry(monkeypatch, fakes)
    monkeypatch.setattr(vs_mod.voice_store, "get", lambda vid: row)
    monkeypatch.setattr(hooks_mod, "synth_params_for_sentence",
                        lambda sent, s: {"speed": 1.0, "pitch": 0.0})
    monkeypatch.setattr(Config, "TTS_TIER", "online", raising=False)
    monkeypatch.setattr(Config, "TTS_ENGINE", "edgetts", raising=False)

    cw = wmod.ConversionWorkflow()
    logged = []
    monkeypatch.setattr(cw, "_append_task_log",
                        lambda tid, stage, message="", extra=None, **k:
                        logged.append((stage, message, extra)))
    monkeypatch.setattr(cw, "is_cancelled", lambda tid: False)

    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(cw, "_emit", _noop)
    ctx = {"task_id": "t-dual", "sentences": sentences or [
        {"text": "第一句念白内容", "voice_id": "v1", "chapter_id": "c1"}],
        "settings": dict(settings)}
    await cw._stage_synthesize(ctx, dict(settings))
    ctx["_logged"] = logged
    return ctx, fakes


# ================================================= 1) P7：档位真的决定谁发声
class TestSynthTierRouting:
    async def test_local_voice_is_downgraded_on_online_tier(self, monkeypatch):
        """在线档遇到"绑着本地引擎的音色"：改由在线引擎发声，且**逐枚计数留痕**。

        音色身份没保住（那是换嗓子），所以 `context` 里必须查得到降级枚数，
        并且这条事实要以任务日志的形式出口 —— 只进服务端日志就是骗用户（红线②）。
        """
        ctx, fakes = await _drive_p7(
            monkeypatch, {"tier": "online"}, _row(vid="v1", engine="voxcpm"))
        assert fakes["voxcpm"].calls == []
        assert fakes["edgetts"].calls == ["v1"]
        assert len(ctx["audio_arrays"]) == 1
        assert ctx["tier"] == "online"
        assert ctx["tier_voice_downgraded"] == 1
        assert [m for _, m, _ in ctx["_logged"]] == [
            m for _, m, _ in ctx["_logged"] if "音色身份未保留" in m]

    async def test_local_tier_keeps_the_voice_on_its_own_engine(self, monkeypatch):
        ctx, fakes = await _drive_p7(
            monkeypatch, {"tier": "local"}, _row(vid="v1", engine="voxcpm"))
        assert fakes["voxcpm"].calls == ["v1"]
        assert fakes["edgetts"].calls == []
        assert ctx["tier_voice_downgraded"] == 0
        assert ctx["_logged"] == []                 # 本地档不换嗓子，不播报

    async def test_nothing_configured_means_online(self, monkeypatch):
        """指令#1 的"默认"两个字：`settings={}` 也必须走在线档（升级前这里是 voxcpm）。"""
        ctx, fakes = await _drive_p7(monkeypatch, {}, _row(vid="v1", engine="voxcpm"))
        assert fakes["edgetts"].calls == ["v1"] and ctx["tier"] == "online"

    async def test_explicit_local_engine_on_online_tier_warns_but_runs(self, monkeypatch):
        """只告警不拦：在线档里显式点本地引擎是合法混用，但必须留下 `tier_warning`。"""
        ctx, fakes = await _drive_p7(
            monkeypatch, {"tier": "online", "engine": "voxcpm"},
            _row(vid="v1", engine="voxcpm"))
        assert fakes["voxcpm"].calls == ["v1"]
        assert ctx["tier_warning"] and "online" in ctx["tier_warning"]


# ================================================= 2) P4 与 P7 同源
def test_split_policy_uses_tier_default_when_engine_absent(monkeypatch):
    """分句策略：`settings` 没 engine 时按**档位默认引擎**取，不再退回"无引擎=默认"。

    旧形态下 P4 看到的是"没有引擎"，P7 看到的是 edgetts —— 两阶段各说各话。
    """
    from modules.text_processor.sentence_splitter import DEFAULT_SPLIT_POLICY
    cw = wmod.ConversionWorkflow()
    fakes = _online_local_pair()
    strict = types.SimpleNamespace(max_len=40, min_len=1, max_en_len=80,
                                   tail_merge=2, upper_bound=120)
    fakes["edgetts"].split_policy = strict
    _fake_registry(monkeypatch, fakes)
    monkeypatch.setattr(Config, "TTS_TIER", "online", raising=False)
    monkeypatch.setattr(Config, "TTS_ENGINE", "edgetts", raising=False)

    policy = cw._resolve_split_policy({"tier": "online"})
    assert policy.max_len == strict.max_len
    # 本地档：voxcpm 桩没声明 split_policy -> 落回 DEFAULT（不等于"没接线"）
    assert cw._resolve_split_policy({"tier": "local"}) == DEFAULT_SPLIT_POLICY


# ================================================= 3) 任务入口的边界校验
def _convert_client(monkeypatch):
    """真路由 + 真校验，只把"落库/起后台任务"这两步换成桩（不碰生产库）。"""
    from fastapi.testclient import TestClient
    import backend.api.tasks as tasks_api
    import backend.main as main_mod

    created = []
    monkeypatch.setattr(tasks_api.task_service, "create_task",
                        lambda **kw: created.append(kw) or {"id": "t-x"})
    monkeypatch.setattr(tasks_api.asyncio, "create_task", lambda coro: coro.close())
    return TestClient(main_mod.app), created


@pytest.mark.parametrize("settings,needle", [
    ({"tier": "wat"}, "未知配音档位"),
    ({"engine": "not-an-engine"}, "不是已注册引擎"),
])
def test_convert_rejects_bad_mode_at_the_boundary(monkeypatch, settings, needle):
    client, created = _convert_client(monkeypatch)
    resp = client.post("/api/tasks/convert",
                       json={"book_id": "b1", "output_format": "wav",
                             "settings": settings})
    assert resp.status_code == 422, resp.text
    assert needle in resp.text
    # 422 必须是"什么都没发生"：任务行一枚都不许落库
    assert created == []


def test_convert_accepts_the_two_modes(monkeypatch):
    client, created = _convert_client(monkeypatch)
    for tier in ("online", "local"):
        resp = client.post("/api/tasks/convert",
                           json={"book_id": "b1", "output_format": "wav",
                                 "settings": {"tier": tier}})
        assert resp.status_code == 200, resp.text
    assert [c["settings"]["tier"] for c in created] == ["online", "local"]


# ================================================= 4) 项目设置里的档位
def test_project_settings_validate_the_tier_value():
    from backend.services.dubbing_service import sanitize_global_settings
    clean, _ = sanitize_global_settings({"tier": "local", "speed": 1.1})
    assert clean["tier"] == "local"
    with pytest.raises(Exception) as ei:
        sanitize_global_settings({"tier": "online-ish"})
    assert "只能是" in str(ei.value.detail)


# ================================================= 5) 档位/引擎清单端点
def test_engines_endpoint_lists_both_modes_without_network(monkeypatch):
    """模式开关的数据源：零请求、零权重，缺 Key 的付费档也要被如实列出。"""
    import httpx
    from fastapi.testclient import TestClient
    import backend.main as main_mod

    def _boom(*a, **k):
        raise AssertionError("列引擎清单不该发出任何网络请求")

    monkeypatch.setattr(httpx, "Client", _boom)
    monkeypatch.setattr(Config, "TTS_TIER", "online", raising=False)
    monkeypatch.setattr(Config, "TTS_ENGINE", "edgetts", raising=False)
    body = TestClient(main_mod.app).get("/api/system/engines").json()
    assert body["status"] == "ok", body
    assert body["tier"] == "online"
    assert [m["tier"] for m in body["modes"]] == ["online", "local"]
    assert body["modes"][0]["engine"] == "edgetts"
    assert body["modes"][1]["engine"] == "voxcpm"
    by_name = {e["name"]: e for e in body["engines"]}
    assert by_name["edgetts"]["online"] is True
    assert by_name["voxcpm"]["online"] is False
    # 付费档：端口在、Key 没配 —— 必须显式可见，不能报成"可用"
    for paid in ("azure", "minimax"):
        port = by_name[paid]["port"]
        assert port["paid"] is True and port["key_present"] is False
        assert by_name[paid]["available"] is False


def test_tier_helpers_reject_unknown_and_never_promote_unverified():
    assert tts_tier.default_engine("online") == "edgetts"
    assert tts_tier.default_engine("local") == "voxcpm"
    with pytest.raises(ValueError):
        tts_tier.resolve_tier("hybrid")
    # 未实测的付费引擎不会因为"是在线的"就顶掉已实测的 edgetts
    assert "azure" in tts_tier.UNVERIFIED_CLOUD_ENGINES
    assert tts_tier.default_engine("online") not in tts_tier.UNVERIFIED_CLOUD_ENGINES


def test_template_run_does_not_inject_voxcpm(monkeypatch):
    """自建模板（engine 列留空）跑起来时不许被硬补成本地档。"""
    from backend.api import tasks as tasks_api
    from backend.api import workflows as wf_api
    from modules.workflow.workflow_service import workflow_template_service

    captured = {}

    async def fake_start(req):
        captured["settings"] = req.settings
        return {"status": "ok", "task": {"id": "t1", "output_format": "wav"}}

    # `run_template` 是**函数内** import 的，所以只能打源模块的属性。
    monkeypatch.setattr(tasks_api, "start_conversion", fake_start)
    monkeypatch.setattr(workflow_template_service, "get", lambda tid: {
        "id": tid, "name": "自建", "engine": "", "output_format": "wav",
        "settings": {}})
    monkeypatch.setattr(Config, "TTS_TIER", "online", raising=False)
    monkeypatch.setattr(Config, "TTS_ENGINE", "edgetts", raising=False)
    body = asyncio.run(wf_api.run_template(
        "tpl1", wf_api.RunTemplateRequest(book_id="b1")))
    assert "engine" not in captured["settings"]        # 交给档位定
    assert body["used"]["engine"] == "edgetts"
    assert body["used"]["engine_from_template"] is False
    # 建模板那条路同样不许写死本地引擎（否则模板一落地就被钉在 5GB 档）
    assert wf_api.CreateTemplateRequest(name="x").engine == ""
