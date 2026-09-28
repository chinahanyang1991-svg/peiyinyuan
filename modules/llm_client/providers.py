"""
大模型接入端口（provider 预设 + 环境变量解析）—— 本模块是 LLM 端点的单一事实源。

为什么要有这一份：客户端原先把 `http://freellmapi:3001` 写死在构造默认值里，
除本机 docker sidecar 外谁也接不上；sidecar 一停，五枚 LLM 端点全体 503，
用户手里没有任何可切的路。这里只加**端口**，不加**猜测**：

- 每个预设的 `base_url` 都是 2026-09-24 在本机 `curl` 过的（结果逐条写在 `probe` 里，
  没实测过的预设一律标 `"probe": "未实测"`，不拿印象冒充测量）；
- 没有 API Key 时所有出口只回报"未配置 + 缺哪个环境变量"，**不发网络请求**，
  也不退回 sidecar（静默退回 = 用户以为在用云端）。

优先级：显式入参 > `LLM_*` 环境变量 > 预设默认值。
"""

import os
import re
from typing import Optional

# OpenAI 兼容端点的两种形态：base 已含版本段（/v1、/v4）时直接续 /chat/completions，
# 否则按 OpenAI 规范补 /v1。少这一层归一化，接智谱（/api/paas/v4）就会拼出双重版本。
_VERSION_SUFFIX_RE = re.compile(r"/v\d+$")

LOOPBACK_RE = re.compile(r"^https?://(localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1\])(:|/|$)")

# sidecar 的探活路径是它自己的私有接口（不是 OpenAI 规范的一部分），故单独成字段。
SIDECAR_PING_PATH = "/api/ping"

PRESET_ORDER = ("freellmapi", "deepseek", "dashscope", "zhipu", "moonshot",
                "siliconflow", "openai", "ollama", "custom")

LLM_PRESETS = {
    "freellmapi": {
        "display": "Sidecar",
        "base_url": "http://freellmapi:3001",
        "api_key_env": None,
        "default_key": "not-needed",
        "model": "auto",
        "ping_path": SIDECAR_PING_PATH,
        "needs_key": False,
        "online": False,
        # 本机 2026-09-24 实测：sidecar 没起（curl http://freellmapi:3001/api/ping → 连不上）
        "probe": "本机未运行（2026-09-24 curl /api/ping 连不上）",
    },
    "deepseek": {
        "display": "DeepSeek",
        "base_url": "https://api.deepseek.com/v1",
        "api_key_env": "DEEPSEEK_API_KEY",
        "model": "deepseek-chat",
        "needs_key": True,
        "online": True,
        # 2026-09-24 本机 `curl https://api.deepseek.com/v1/models` → 401（路由在、缺 Key）
        "probe": "境内直连可达（2026-09-24 无 Key GET /v1/models = 401）",
    },
    "dashscope": {
        "display": "阿里云百炼",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "api_key_env": "DASHSCOPE_API_KEY",
        "model": "qwen-plus",
        "needs_key": True,
        "online": True,
        "probe": "境内直连可达（2026-09-24 无 Key GET /compatible-mode/v1/models = 401）",
    },
    "zhipu": {
        "display": "智谱 GLM",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "api_key_env": "ZHIPU_API_KEY",
        "model": "glm-4-flash",
        "needs_key": True,
        "online": True,
        "probe": "境内直连可达（2026-09-24 无 Key GET /api/paas/v4/models = 401）",
    },
    "moonshot": {
        "display": "Kimi（月之暗面）",
        "base_url": "https://api.moonshot.cn/v1",
        "api_key_env": "MOONSHOT_API_KEY",
        "model": "moonshot-v1-8k",
        "needs_key": True,
        "online": True,
        "probe": "境内直连可达（2026-09-24 无 Key GET /v1/models = 401）",
    },
    "siliconflow": {
        "display": "硅基流动",
        "base_url": "https://api.siliconflow.cn/v1",
        "api_key_env": "SILICONFLOW_API_KEY",
        # 该家的模型名是"厂商/模型"两段式，这里不写死一个具体值：无 LLM_MODEL 时报"未指定"。
        "model": "",
        "needs_key": True,
        "online": True,
        "probe": "境内直连可达（2026-09-24 无 Key GET /v1/models = 401）",
    },
    "openai": {
        "display": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "api_key_env": "OPENAI_API_KEY",
        "model": "gpt-4o-mini",
        "needs_key": True,
        "online": True,
        # 2026-09-24 本机实测：curl 返回 000（这台机器直连不通），故不进默认位。
        "probe": "本机直连不通（2026-09-24 GET /v1/models 无响应，需代理或境外出口）",
    },
    "ollama": {
        "display": "Ollama（本地）",
        "base_url": "http://127.0.0.1:11434/v1",
        "api_key_env": None,
        "default_key": "not-needed",
        "model": "",
        "needs_key": False,
        "online": False,
        "probe": "本机未运行（2026-09-24 GET /v1/models 连不上）",
    },
    "custom": {
        "display": "自定义 OpenAI 兼容端点",
        "base_url": "",
        "api_key_env": "LLM_API_KEY",
        "model": "",
        "needs_key": False,
        "online": True,
        "probe": "由 LLM_BASE_URL 指定，本模块不代其担保可达",
    },
}


class ProviderConfig:
    """一次解析的结果。`configured=False` 时 `reason` 必非空（False 单独不算回答）。"""

    __slots__ = ("preset", "display", "base_url", "api_key", "model",
                 "needs_key", "online", "ping_path", "configured", "reason")

    def __init__(self, preset, display, base_url, api_key, model, needs_key,
                 online, ping_path, configured, reason):
        self.preset = preset
        self.display = display
        self.base_url = base_url
        self.api_key = api_key
        self.model = model
        self.needs_key = needs_key
        self.online = online
        self.ping_path = ping_path
        self.configured = configured
        self.reason = reason

    @property
    def key_present(self) -> bool:
        """有没有 Key —— 供 API 展示，**绝不返回 Key 本体**。"""
        return bool(self.api_key) and self.api_key != "not-needed"

    def describe(self) -> dict:
        """可进 API 响应体的一份自述：不含 Key，含"缺什么才可用"。"""
        return {
            "preset": self.preset,
            "display": self.display,
            "base_url": self.base_url,
            "model": self.model or None,
            "api_key_present": self.key_present,
            "requires_api_key": self.needs_key,
            "online": self.online,
            "configured": self.configured,
            "reason": self.reason,
            "probe": LLM_PRESETS.get(self.preset, {}).get("probe"),
        }


def _env_first(env: dict, *names) -> str:
    for name in names:
        if not name:
            continue
        value = (env.get(name) or "").strip()
        if value:
            return value
    return ""


def infer_preset(base_url: str) -> str:
    """从一个裸 base_url 猜它属于哪家：命中预设则用之，本机回环/docker 服务名算 sidecar。

    这一步只为兼容**旧构造写法** `FreeLLMAPIClient(base_url=...)`（历史语义就是
    "指向本机 sidecar"）。新配置一律走 `LLM_PRESET`，不靠猜。
    """
    if not base_url:
        return "custom"
    normalized = base_url.rstrip("/")
    for name, preset in LLM_PRESETS.items():
        if preset["base_url"].rstrip("/") == normalized:
            return name
    if LOOPBACK_RE.match(normalized) or "://freellmapi" in normalized:
        return "freellmapi"
    return "custom"


def resolve_provider(preset: str = None, base_url: str = None, api_key: str = None,
                     model: str = None, env: Optional[dict] = None) -> ProviderConfig:
    """按"入参 > 环境变量 > 预设默认"的优先级解析出可用的 LLM 端点配置。

    纯函数，不碰网络，因此可在无凭据的 CI 里全分支覆盖。
    """
    environ = dict(os.environ if env is None else env)

    name = (preset or environ.get("LLM_PRESET") or "").strip().lower()
    explicit_base = (base_url if base_url is not None else environ.get("LLM_BASE_URL") or "").strip()

    if name:
        if name not in LLM_PRESETS:
            # 未知预设名不得静默退回 sidecar —— 那是"用户以为在用云端、实际在打本机"的形状。
            return ProviderConfig(
                preset=name, display=name, base_url=explicit_base, api_key="", model="",
                needs_key=True, online=True, ping_path="", configured=False,
                reason=(f"未知 LLM 预设 '{name}'，可选: {', '.join(PRESET_ORDER)}"
                        "（设 LLM_PRESET 或换名重试）"))
    else:
        name = infer_preset(explicit_base) if explicit_base else "freellmapi"

    spec = LLM_PRESETS[name]
    resolved_base = (explicit_base or spec["base_url"]).rstrip("/")
    key_env_names = tuple(n for n in (spec.get("api_key_env"), "LLM_API_KEY") if n)
    resolved_key = (api_key if api_key is not None else _env_first(environ, *key_env_names)
                    ) or spec.get("default_key", "")
    resolved_model = (model if model is not None else (environ.get("LLM_MODEL") or "").strip()
                      ) or spec["model"]

    if not resolved_base:
        return ProviderConfig(
            preset=name, display=spec["display"], base_url="", api_key=resolved_key,
            model=resolved_model, needs_key=spec["needs_key"], online=spec["online"],
            ping_path=spec.get("ping_path", ""), configured=False,
            reason="未配置 LLM 端点：预设 '{}' 需要显式 LLM_BASE_URL".format(name))

    if spec["needs_key"] and (not resolved_key or resolved_key == "not-needed"):
        return ProviderConfig(
            preset=name, display=spec["display"], base_url=resolved_base, api_key="",
            model=resolved_model, needs_key=True, online=spec["online"],
            ping_path=spec.get("ping_path", ""), configured=False,
            reason=(f"未配置 {spec['display']} 的 API Key：设 {spec['api_key_env']}"
                    f"（或 LLM_API_KEY）后重试，程序不会替你退回本机 sidecar"))

    if not resolved_model:
        return ProviderConfig(
            preset=name, display=spec["display"], base_url=resolved_base,
            api_key=resolved_key, model="", needs_key=spec["needs_key"],
            online=spec["online"], ping_path=spec.get("ping_path", ""), configured=False,
            reason=f"{spec['display']} 已配 Key 但未指定模型：设 LLM_MODEL（该家模型名不唯一，不猜）")

    return ProviderConfig(
        preset=name, display=spec["display"], base_url=resolved_base,
        api_key=resolved_key or "not-needed", model=resolved_model,
        needs_key=spec["needs_key"], online=spec["online"],
        ping_path=spec.get("ping_path", ""), configured=True, reason=None)


def chat_completions_url(base_url: str) -> str:
    base = (base_url or "").rstrip("/")
    suffix = "/chat/completions" if _VERSION_SUFFIX_RE.search(base) else "/v1/chat/completions"
    return f"{base}{suffix}"


def models_url(base_url: str) -> str:
    base = (base_url or "").rstrip("/")
    suffix = "/models" if _VERSION_SUFFIX_RE.search(base) else "/v1/models"
    return f"{base}{suffix}"
