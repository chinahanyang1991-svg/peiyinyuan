"""大模型接入端口（LLM provider ports）的行为约束。

这一族测试全部**离线**：没有任何一家云端 Key 在本机存在，所以判据只能是
"配齐了才发请求、没配齐就一帧都不发并且说清缺什么"。为此把 `httpx.Client`
换成一枚记账桩——**它被构造本身就是失败**，比"断言返回 None"强（后者在
"没配齐但还是打出去并恰好连不上"时也会绿）。
"""

import httpx
import pytest

from modules.llm_client.freellmapi_client import FreeLLMAPIClient
from modules.llm_client.providers import (LLM_PRESETS, chat_completions_url,
                                           infer_preset, models_url, resolve_provider)


class _NoNetwork:
    """任何一次网络构造都是本测试的 FAIL。"""

    calls = None

    def __init__(self, *args, **kwargs):
        type(self).calls = type(self).calls or []
        type(self).calls.append(kwargs or {"args": args})

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def post(self, url, **kw):  # pragma: no cover - 到不了这里
        raise AssertionError(f"不该发出请求：{url}")

    def get(self, url, **kw):  # pragma: no cover
        raise AssertionError(f"不该发出请求：{url}")


@pytest.fixture
def no_network(monkeypatch):
    _NoNetwork.calls = []
    monkeypatch.setattr(httpx, "Client", _NoNetwork)
    return _NoNetwork


# ---------------------------------------------------------------- 纯解析：优先级与归一化
def test_sidecar_default_is_unchanged():
    cfg = resolve_provider(env={})
    assert cfg.preset == "freellmapi" and cfg.configured, cfg.reason
    assert cfg.base_url == "http://freellmapi:3001"
    assert cfg.model == "auto"
    assert cfg.api_key == "not-needed"
    # 旧口径的 URL 一字不动（接进云端后仍要能连本机 sidecar）
    assert cfg.ping_path == "/api/ping"


def test_env_beats_preset_default_and_explicit_beats_env():
    cfg = resolve_provider(env={"LLM_PRESET": "deepseek", "DEEPSEEK_API_KEY": "sk-env",
                                "LLM_MODEL": "deepseek-reasoner"})
    assert (cfg.model, cfg.api_key) == ("deepseek-reasoner", "sk-env")
    cfg2 = resolve_provider(env={"LLM_PRESET": "deepseek", "DEEPSEEK_API_KEY": "sk-env"},
                            model="glm-4-flash", base_url="https://example.test/v1")
    assert cfg2.model == "glm-4-flash" and cfg2.base_url == "https://example.test/v1"
    # 显式 api_key=None（"没给"）时环境变量仍然生效；显式给串则盖过环境变量
    cfg3 = resolve_provider(env={"LLM_PRESET": "deepseek", "DEEPSEEK_API_KEY": "sk-env"},
                            api_key="sk-explicit")
    assert cfg3.api_key == "sk-explicit"


def test_placeholder_key_does_not_shadow_env_key():
    """老写法 `FreeLLMAPIClient(api_key="not-needed")` 不得把环境变量里的真 Key 盖掉。"""
    client = FreeLLMAPIClient(api_key="not-needed", preset="deepseek",
                              env={"DEEPSEEK_API_KEY": "sk-real"})
    assert client.api_key == "sk-real", client.config.reason


@pytest.mark.parametrize("preset,expected_chat", [
    ("deepseek", "https://api.deepseek.com/v1/chat/completions"),
    ("zhipu", "https://open.bigmodel.cn/api/paas/v4/chat/completions"),
    ("dashscope", "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"),
])
def test_openai_compatible_url_normalization(preset, expected_chat):
    """已含版本段的 base 不得再补 /v1（否则智谱会拼成 /api/paas/v4/v1/...）。"""
    base = LLM_PRESETS[preset]["base_url"]
    assert chat_completions_url(base) == expected_chat
    assert models_url(base) == expected_chat.replace("/chat/completions", "/models")
    assert "//" not in expected_chat.split("://", 1)[1]


def test_bare_host_style_base_gets_v1():
    assert chat_completions_url("http://freellmapi:3001/") == \
        "http://freellmapi:3001/v1/chat/completions"


def test_infer_preset_only_for_legacy_construction():
    assert infer_preset("http://127.0.0.1:11434/v1") == "ollama"
    assert infer_preset("http://localhost:9000") == "freellmapi"
    assert infer_preset("https://api.deepseek.com/v1") == "deepseek"
    assert infer_preset("https://my-gateway.internal/v1") == "custom"


# ---------------------------------------------------------------- 没配齐：不发请求 + 说清缺什么
def test_missing_key_is_inert_and_names_the_env_var(no_network):
    client = FreeLLMAPIClient(preset="deepseek", env={})
    assert client.config.configured is False
    assert "DEEPSEEK_API_KEY" in client.config.reason

    assert client.chat_ex(messages=[{"role": "user", "content": "hi"}])[0] is None
    assert client.health_check_ex()[0] is False
    assert client.list_models_ex()[0] == []
    assert no_network.calls == [], "缺 Key 却把请求打出去了"


def test_unknown_preset_does_not_fall_back_to_sidecar(no_network):
    """设了 LLM_PRESET 但拼错 → 必须报错，不许"静默退回本机"（那是骗用户在用云端）。"""
    client = FreeLLMAPIClient(env={"LLM_PRESET": "deepseekk", "LLM_BASE_URL": "",
                                   "DEEPSEEK_API_KEY": ""})
    assert client.config.configured is False
    assert "未知 LLM 预设" in client.config.reason and "deepseek" in client.config.reason
    content, reason = client.chat_ex(messages=[])
    assert content is None and "未配置" in reason
    assert no_network.calls == []


def test_key_present_but_model_unnamed_still_asks(no_network):
    """硅基流动/Ollama 的模型名不唯一：宁可报"设 LLM_MODEL"，也不猜一个模型名冒充可用。"""
    cfg = resolve_provider(env={"LLM_PRESET": "siliconflow", "SILICONFLOW_API_KEY": "sk-x"})
    assert cfg.configured is False and "LLM_MODEL" in cfg.reason
    client = FreeLLMAPIClient(preset="ollama", env={})
    assert client.config.configured is False and "LLM_MODEL" in client.config.reason
    assert no_network.calls == []


def test_custom_preset_needs_base_url():
    cfg = resolve_provider(env={"LLM_PRESET": "custom", "LLM_API_KEY": "sk-x",
                                "LLM_MODEL": "any"})
    assert cfg.configured is False and "LLM_BASE_URL" in cfg.reason
    ok = resolve_provider(env={"LLM_PRESET": "custom", "LLM_MODEL": "any",
                               "LLM_BASE_URL": "https://gateway.local/v1"})
    assert ok.configured and ok.display == "自定义 OpenAI 兼容端点"


# ---------------------------------------------------------------- Key 不外泄
def test_describe_never_leaks_the_key():
    cfg = resolve_provider(env={"LLM_PRESET": "deepseek", "DEEPSEEK_API_KEY": "sk-secret-123"})
    body = cfg.describe()
    assert body["api_key_present"] is True
    flat = repr(body)
    assert "sk-secret-123" not in flat, "describe() 会把 Key 带进 API 响应体"
    assert "api_key" not in body, "响应体里只许有 api_key_present 这个布尔"


# ---------------------------------------------------------------- 端点层
def test_provider_endpoint_reports_config_without_key(monkeypatch):
    """`GET /api/llm/provider` 与网络无关：sidecar 和云端全不可达时也要答得出"缺什么"。

    走 `backend.main.app`（真挂载路径 `/api/llm/...`）而不是裸 router —— 裸 APIRouter
    没有 FastAPI 的中间件栈，TestClient 会在框架层炸掉，测不到本端点的行为。
    """
    from fastapi.testclient import TestClient

    import backend.api.llm as llm_api
    import backend.main as main
    from modules.llm_client.llm_service import LLMService

    monkeypatch.setattr(llm_api, "llm_service",
                        LLMService(FreeLLMAPIClient(preset="deepseek", env={})))
    client = TestClient(main.app)
    resp = client.get("/api/llm/provider")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "unconfigured"
    assert body["preset"] == "deepseek" and body["configured"] is False
    assert "DEEPSEEK_API_KEY" in body["reason"]
    assert "sk-" not in repr(body)
    named = {p["preset"] for p in body["presets"]}
    assert {"freellmapi", "deepseek", "dashscope", "zhipu", "ollama"} <= named


def test_hint_follows_the_configured_provider():
    """503 那句"怎么办"不许写死成"起 docker"——接云端时那是误导。"""
    from fastapi import HTTPException

    import backend.api.llm as llm_api
    from modules.llm_client.llm_service import LLMResult, LLMService

    original = llm_api.llm_service
    try:
        llm_api.llm_service = LLMService(FreeLLMAPIClient(preset="deepseek", env={}))
        with pytest.raises(HTTPException) as boom:
            llm_api._shell(LLMResult([], "empty", "云端 200 但 choices 空"))
        assert "API Key" in boom.value.detail and "docker" not in boom.value.detail
    finally:
        llm_api.llm_service = original
