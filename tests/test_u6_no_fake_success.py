"""U-6：消灭「200 + 空」假成功（M-4 的读侧版本）

旧行为：Sidecar（`freellmapi:3001`）没起时，5 枚 LLM 端点照样 200 ——
`{"chapters":[]}` / 原文回显当"已润色" / `neutral+0.5` 当测量值 /
`/health` 给空 models。调用方读到的是"成功但什么都没发生"，与 PY-117 同性质。

现在的契约（见 `backend/api/llm.py` 模块注释）：
  status ok        = LLM 真答了
  status degraded  = 值来自兜底（passthrough / partial / rule_fallback），必带 reason
  HTTP 503         = 既没答也拿不出可兜底的值，detail 里是人话原因
"""

import asyncio
import inspect

import pytest
from fastapi import HTTPException

from backend.api import llm as llm_api
from modules.llm_client.freellmapi_client import FreeLLMAPIClient
from modules.llm_client.llm_service import LLMResult, LLMService
from modules.tts_engine.engines.moss_tts_adapter import MOSSTTSAdapter

UNREACHABLE = "Sidecar 未启动或不可达（ConnectError，url=http://freellmapi:3001/api/ping）"


def _run(coro):
    """端点已从 async def 降为普通 def（阻塞客户端要进线程池），
    所以这里既收协程也收返回值。"""
    return asyncio.run(coro) if inspect.isawaitable(coro) else coro


class _StubClient:
    """只实现 llm_service 用到的三个 ex 口，行为由构造参数决定。"""

    def __init__(self, reply=None, reply_error=None, ready=True, models=None):
        self.reply = reply
        self.reply_error = reply_error
        self.ready = ready
        self.models = models or []
        self.chat_calls = 0

    def chat_ex(self, messages, **kw):
        self.chat_calls += 1
        if self.reply is None:
            return None, self.reply_error or UNREACHABLE
        return self.reply, None

    def health_check_ex(self):
        return (True, None) if self.ready else (False, UNREACHABLE)

    def list_models_ex(self):
        if self.ready:
            return self.models, None
        return [], UNREACHABLE


@pytest.fixture
def stub(monkeypatch):
    def _install(**kw):
        client = _StubClient(**kw)
        monkeypatch.setattr(llm_api.llm_service, "client", client, raising=False)
        return client
    return _install


# ---------------------------------------------------------------- 503：什么都取不到
def test_detect_chapters_503s_when_llm_cannot_answer(stub):
    stub()
    with pytest.raises(HTTPException) as ei:
        _run(llm_api.llm_chapter_detect(
            llm_api.ChapterDetectRequest(text="第一章 起\n正文\n第二章 承\n正文")))
    assert ei.value.status_code == 503
    assert "Sidecar" in str(ei.value.detail)


def test_sentiment_never_invents_neutral_half(stub):
    """旧实现取不到时回 `neutral / intensity 0.5` —— 那是把 0 值伪装成测量值。"""
    stub()
    with pytest.raises(HTTPException) as ei:
        _run(llm_api.llm_sentiment(llm_api.SentimentRequest(text="他转身走了。")))
    assert ei.value.status_code == 503
    body = str(ei.value.detail)
    assert "0.5" not in body and "neutral" not in body


def test_ready_and_health_carry_a_reason(stub):
    stub(ready=False)
    ready = _run(llm_api.llm_ready())
    assert ready["ready"] is False
    assert ready["status"] == "unavailable"
    assert ready["reason"], "探活失败不得只报 False"

    health = _run(llm_api.llm_health())
    assert health["status"] == "unavailable", "旧值这里恒为 ok + 空 models"
    assert health["reason"]


# ---------------------------------------------------------------- degraded：兜底须自报
def test_polish_labels_the_echo_as_passthrough(stub):
    stub()
    body = _run(llm_api.llm_polish(llm_api.PolishRequest(text="原文就这样")))
    assert body["status"] == "degraded"
    assert body["source"] == "passthrough"
    assert body["changed"] is False, "回显原文却报 changed=True = 冒充润色结果"
    assert body["text"] == "原文就这样"
    assert body["reason"]


def test_ssml_reports_that_no_tags_were_added(stub):
    stub()
    body = _run(llm_api.llm_ssml(llm_api.SSMLRequest(text="一句话。")))
    assert body["status"] == "degraded"
    assert body["has_tags"] is False
    assert body["reason"]


def test_voice_parse_keeps_rule_fallback_but_names_it(stub):
    """规则引擎是设计内路径，保留；但必须 source=rule_fallback，不得混称 LLM 结果。"""
    stub()
    body = _run(llm_api.llm_parse_voice(
        llm_api.VoiceDescriptionRequest(description="低沉有磁性的中年男声")))
    assert body["status"] == "degraded"
    assert body["source"] == "rule_fallback"
    assert body["gender"] == "male"
    assert body["reason"]


# ---------------------------------------------------------------- ok：LLM 真答了
def test_llm_answer_cannot_forgery_the_envelope(stub):
    """`/analyze-sentiment` 把 LLM 解出的 dict 摊进响应体：模型若回一个自带
    `status:"ok"` 的对象，也不得覆盖外壳字段（否则又是一枚"自己宣布自己成功"）。"""
    stub(reply='{"sentiment": "sad", "intensity": 0.9, "status": "ok", "source": "llm"}')
    body = _run(llm_api.llm_sentiment(llm_api.SentimentRequest(text="他转身走了。")))
    assert body["sentiment"] == "sad"
    assert body["status"] == "ok" and body["source"] == "llm"
    assert body["reason"] is None

    stub(reply='{"sentiment": "sad", "status": "hacked", "reason": "我说我成功了"}')
    body2 = _run(llm_api.llm_sentiment(llm_api.SentimentRequest(text="他转身走了。")))
    assert body2["status"] == "ok", "模型自报的 status 不得顶掉外壳"
    assert body2["reason"] is None and body2["source"] == "llm"


def test_polish_reports_ok_when_llm_answers(stub):
    stub(reply="  润色后的文本  ")
    body = _run(llm_api.llm_polish(llm_api.PolishRequest(text="册0 的")))
    assert body["status"] == "ok" and body["source"] == "llm" and body["reason"] is None
    assert body["text"] == "润色后的文本"


def test_detect_chapters_accepts_a_real_json_answer(stub):
    stub(reply='[{"title": "第一章", "summary": "开场"}]')
    body = _run(llm_api.llm_chapter_detect(llm_api.ChapterDetectRequest(text="第一章 开场")))
    assert body["status"] == "ok"
    assert body["count"] == 1 and body["chapters"][0]["title"] == "第一章"


def test_detect_chapters_empty_array_from_llm_is_ok_not_503(stub):
    """LLM 活着且答"没有章节结构"→ 200 + count 0（这是真结论，不是取不到）。"""
    stub(reply="[]")
    body = _run(llm_api.llm_chapter_detect(llm_api.ChapterDetectRequest(text="一逗到底的正文")))
    assert body["status"] == "ok" and body["count"] == 0


def test_unparseable_answer_is_not_reported_as_success(stub):
    stub(reply="我看这段没有章节")
    with pytest.raises(HTTPException) as ei:
        _run(llm_api.llm_chapter_detect(llm_api.ChapterDetectRequest(text="随便什么")))
    assert ei.value.status_code == 503
    assert "不是合法 JSON" in str(ei.value.detail)


def test_partial_polish_of_a_long_text_names_the_failed_chunks(stub):
    """分段任务里 LLM 只答一部分：status 必须是 degraded 且 reason 给出段号。"""
    calls = {"n": 0}

    class _Half:
        def chat_ex(self, messages, **kw):
            calls["n"] += 1
            if calls["n"] % 2:
                return "X" * 10, None
            return None, "Sidecar HTTP 500: boom"

        def health_check_ex(self):
            return True, None

    import modules.llm_client.llm_service as svc_mod
    service = svc_mod.LLMService(client=_Half())
    result = service.polish_text("y" * 9000, max_len=4000)
    assert result.source == "partial", result.source
    assert "段" in result.reason and "1" in result.reason


# ---------------------------------------------------------------- 客户端层：原因不得丢
def _patch_connect_error(monkeypatch):
    """本机有 HTTP/HTTPS 代理会替不可达的主机作答（回 503 / 400），所以真打网络
    得到的失败种类不可控。这里把 `httpx.Client` 本身换成"一构造就 ConnectError"，
    钉住『连不上』这条确定的路。"""
    import httpx

    def _boom(*a, **k):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx, "Client", _boom)
    return httpx


def test_client_ex_ports_keep_a_reason_when_unreachable(monkeypatch):
    httpx = _patch_connect_error(monkeypatch)
    client = FreeLLMAPIClient(base_url="http://freellmapi:3001")

    content, reason = client.chat_ex(messages=[{"role": "user", "content": "hi"}])
    assert content is None
    assert reason and "连不上 Sidecar" in reason and "ConnectError" in reason, reason
    assert "http://freellmapi:3001/v1/chat/completions" in reason, "原因里要带打不出去的那个 url"

    ok, reason = client.health_check_ex()
    assert ok is False and "Sidecar 未启动或不可达" in reason, reason
    assert "/api/ping" in reason

    models, reason = client.list_models_ex()
    assert models == [] and "模型列表取不到" in reason, reason

    # 旧口仍在（兼容），但不得再"无声"
    assert client.chat(messages=[]) is None
    assert client.health_check() is False


def test_health_check_distinguishes_alive_but_erroring(monkeypatch):
    """代理/半死的 Sidecar 会回 5xx：那是"活着但报错"，与"连不上"必须是两句话。"""
    import httpx

    class _Resp:
        status_code = 503
        text = "upstream connect error"

        def raise_for_status(self):
            return None

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, **kw):
            return _Resp()

    monkeypatch.setattr(httpx, "Client", _Client)
    ok, reason = FreeLLMAPIClient(base_url="http://127.0.0.1:9").health_check_ex()
    assert ok is False
    assert "探活 HTTP 503" in reason and "不可达" not in reason, reason


def test_http_error_reason_is_flattened_to_one_line(monkeypatch):
    """代理/网关回的是整页 HTML，这串会原样进响应体和前端提示 —— 必须收成一行短句。"""
    import httpx

    html = ('<html>\r\n<head><title>400 The plain HTTP request was sent to HTTPS port'
            '</title></head>\r\n<body><center><h1>400 Bad Request</h1></center></body>')

    class _Resp:
        status_code = 400
        text = html

        def raise_for_status(self):
            raise httpx.HTTPStatusError("400", request=None, response=self)

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, **kw):
            return _Resp()

    monkeypatch.setattr(httpx, "Client", _Client)
    content, reason = FreeLLMAPIClient().chat_ex(messages=[])
    assert content is None
    assert "<" not in reason and "\r" not in reason and "\n" not in reason, reason
    assert "plain HTTP request was sent to HTTPS port" in reason, reason
    assert len(reason) < 220, reason


def test_chat_ex_names_an_empty_completion(monkeypatch):
    """200 但 choices 空 —— 也不能算"LLM 答了"。"""
    import httpx

    class _Resp:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": "   "}}]}

    seen = {}

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, **kw):
            seen["url"] = url
            return _Resp()

    monkeypatch.setattr(httpx, "Client", _Client)
    content, reason = FreeLLMAPIClient().chat_ex(messages=[])
    assert content is None
    assert reason and "choices 空" in reason


# ---------------------------------------------------------------- 红线①：卡住的 Sidecar 不得冻循环
BLOCK_S = 1.0


def test_hanging_sidecar_does_not_stall_the_event_loop(monkeypatch):
    """`FreeLLMAPIClient` 是阻塞 httpx。若端点写成 async def，一枚阻塞请求会把整个
    后端按住（PY-340 同族）。这里用**并发消融**定罪：三枚各阻塞 1s 的请求，
    进线程池 ≈1s 完事，挂在循环上 ≈3s 串行。

    口径说明：单量 ticker 空隙做不了判据——满载跑测试时本机连线程池版本都能量到
    0.3s 的空隙（PY-340 那枚护栏在全量跑里就是这么飘的）。总墙钟才是这个缺陷的
    判别量：负载对两种实现的影响是同比例的，串行的 3 倍关系不会因负载翻转。"""
    import time as _t

    import httpx as _httpx
    from fastapi import FastAPI

    class _Slow:
        def chat_ex(self, messages, **kw):
            _t.sleep(BLOCK_S)
            return None, "Sidecar 卡住"

        def health_check_ex(self):
            _t.sleep(BLOCK_S)
            return False, "Sidecar 卡住"

    monkeypatch.setattr(llm_api.llm_service, "client", _Slow(), raising=False)

    app = FastAPI()
    app.include_router(llm_api.router, prefix="/api/llm")

    async def scenario():
        gaps, stop = [], asyncio.Event()

        async def ticker():
            last = _t.perf_counter()
            while not stop.is_set():
                await asyncio.sleep(0.01)
                now = _t.perf_counter()
                gaps.append(now - last)
                last = now

        task = asyncio.create_task(ticker())
        started = _t.perf_counter()
        try:
            transport = _httpx.ASGITransport(app=app)
            async with _httpx.AsyncClient(transport=transport, base_url="http://t") as c:
                rs = await asyncio.gather(*[
                    c.post("/api/llm/polish", json={"text": "一句话"}) for _ in range(3)])
            await asyncio.sleep(0.05)
        finally:
            stop.set()
            await task
        return ([r.status_code for r in rs], _t.perf_counter() - started,
                max(gaps), len(gaps))

    codes, wall, worst_gap, ticks = _run(scenario())
    assert codes == [200, 200, 200], codes          # 三枚并发各自 degraded，不互相排队成 500
    assert ticks >= 3, "ticker 没跑到，测量无效（夹具问题，不作产品绿）"
    assert wall < BLOCK_S * 2, (
        f"三枚 {BLOCK_S}s 的阻塞请求串行成了 {wall:.2f}s —— 阻塞 I/O 回到了事件循环上（红线①）")
    assert worst_gap < BLOCK_S, f"事件循环被单次拖住 {worst_gap:.2f}s（红线①）"


def test_llm_router_has_no_async_endpoint():
    """源码门：写成 async def 就是上面那枚测试的红线，别等回归才发现。
    （只看定义行——模块注释里正好在说这个词。）"""
    import re as _re

    src = inspect.getsource(llm_api)
    assert not _re.findall(r"^async def ", src, _re.M), \
        "阻塞式 httpx 客户端不得挂在事件循环上"


# ---------------------------------------------------------------- 出口结构守卫
def test_every_llm_endpoint_goes_through_the_shell():
    """五枚 POST 必须经 `_shell`，否则新端点又能绕开契约私设 200+空。"""
    src = inspect.getsource(llm_api)
    assert src.count("return _shell(") == 5, src.count("return _shell(")   # 五枚 POST
    assert '"status": "ok",' not in src.split("# ========== API 端点 ==========")[1], \
        "端点里不得再出现写死的 status ok"


def test_llm_service_no_longer_fabricates_measurements():
    import modules.llm_client.llm_service as m

    src = inspect.getsource(m)
    assert '"intensity": 0.5' not in src, "取不到时不得再回 0.5 冒充测得的中性"
    assert "无法分析" not in src


def test_llm_result_source_vocabulary_is_closed():
    """source 只允许四个值；出现第五种说明有人在私造兜底态。"""
    src = inspect.getsource(LLMService)
    used = {s for s in ("llm", "passthrough", "partial", "rule_fallback", "empty")
            if f'"{s}"' in src}
    assert used == {"llm", "passthrough", "partial", "rule_fallback", "empty"}, used
    assert "LLMResult(" in src
    # 反向：不得有 LLMResult(x, "success") 这类野值
    import re as _re
    illegal = set(_re.findall(r'LLMResult\([^)]*?,\s*"([a-z_]+)"', src))
    assert illegal <= used, illegal


# ---------------------------------------------------------------- 引擎侧同类伪装
def test_moss_adapter_reason_names_the_missing_dependency(monkeypatch):
    """本机四枚依赖全装着，`unavailable_reason()` 合法地返回 ""——所以必须注入一次
    "缺依赖"，否则把探测改成恒返回 "" 的变异体在这台机器上测不出来。"""
    import importlib.util as iu

    real = iu.find_spec

    def fake(name):
        return None if name == "librosa" else real(name)

    monkeypatch.setattr(iu, "find_spec", fake)
    assert MOSSTTSAdapter.unavailable_reason() == "依赖库未安装: librosa"

    monkeypatch.undo()
    live = MOSSTTSAdapter.unavailable_reason()
    assert isinstance(live, str) and (live == "" or "依赖库未安装" in live), live

    # 出口须被 registry/get_info 用到（同 kokoro 的既有契约）
    info = MOSSTTSAdapter().get_info()
    assert info["unavailable_reason"] == live
    assert info["available"] == (live == "")


# ---------------------------------------------------------------- 画像来源标签（前端可见提示）
def test_cast_meta_labels_llm_provenance_as_scalar():
    """前端读的是 `stats.llm === "llm"`：meta 里塞 dict 时这判断永假，
    LLM 真跑出来的画像也报成"规则兜底"。故标签必须是标量。"""
    from backend.services.cast_service import profile_llm_status

    assert profile_llm_status({"llm_ready": True}) == ("llm", None)

    label, reason = profile_llm_status({"llm_ready": False,
                                        "last_error": "Sidecar 未启动或不可达（ConnectError）"})
    assert label == "rules"
    assert reason == "Sidecar 未启动或不可达（ConnectError）", "兜底必须带人话原因，前端据此提示"

    label, reason = profile_llm_status({})           # 什么都没记到
    assert label == "rules" and reason, "不得静默成'看起来正常'"


def test_cast_analyze_persists_the_label_not_the_stats_dict():
    from backend.services import cast_service as cs

    src = inspect.getsource(cs)
    assert '"llm": llm_label' in src, "meta 里的 llm 必须是标量标签"
    assert '"llm": profiler.stats()' not in src
    assert '"llm_reason": llm_reason' in src


def test_cast_wizard_shows_the_llm_reason_in_both_locales():
    """验收判据里那半句「前端有可见提示」——向导必须把原因画出来，两语都得有键。"""
    from pathlib import Path

    tsx = Path("frontend/src/components/cast/CastWizard.tsx").read_text(encoding="utf-8")
    assert "stats?.llm_reason" in tsx and "statProfileReason" in tsx
    for locale in ("zh", "en"):
        assert "statProfileReason" in Path(f"frontend/src/i18n/{locale}.ts").read_text(encoding="utf-8"), locale
