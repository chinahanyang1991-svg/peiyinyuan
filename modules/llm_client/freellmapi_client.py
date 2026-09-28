"""
FreeLLMAPI HTTP 客户端
通过 OpenAI 兼容接口与 FreeLLMAPI 通信
"""

import logging
import re
from typing import Optional

from modules.llm_client.providers import (ProviderConfig, chat_completions_url,
                                           models_url, resolve_provider)

logger = logging.getLogger(__name__)

_TAG_RE = re.compile(r"<[^>]+>")


def _brief(text: str, limit: int = 160) -> str:
    """把错误响应体收成一行短句：这些串会直接进 API 响应体和前端提示，
    代理/网关回的 HTML 页面对用户没有信息量。"""
    flat = re.sub(r"\s+", " ", _TAG_RE.sub(" ", text or "")).strip()
    return flat[:limit] + ("…" if len(flat) > limit else "")


class FreeLLMAPIClient:
    """
    OpenAI 兼容 Chat Completions 客户端

    仅同步 Chat Completions（异步/流式版本从未接线，已移除）。
    默认连接 Docker Sidecar (http://freellmapi:3001)；换云端大模型只需设
    `LLM_PRESET` + `<厂商>_API_KEY`（见 providers.LLM_PRESETS），
    没配 Key 时本客户端**一发请求都不发**，只回报缺什么。
    """

    def __init__(self, base_url: str = None, api_key: str = "not-needed",
                 timeout: int = 120, model: str = None, preset: str = None,
                 env: dict = None):
        # 显式传 "not-needed" 视作"没给 Key"，否则环境变量里的真 Key 会被这个占位符盖掉
        passed_key = None if api_key in (None, "not-needed") else api_key
        self.config: ProviderConfig = resolve_provider(
            preset=preset, base_url=base_url, api_key=passed_key, model=model, env=env)
        self.base_url = self.config.base_url
        self.api_key = self.config.api_key
        self.model = self.config.model
        self.display = self.config.display
        self.timeout = timeout

    @property
    def _chat_url(self) -> str:
        return chat_completions_url(self.base_url)

    @property
    def _models_url(self) -> str:
        return models_url(self.base_url)

    @property
    def _health_url(self) -> str:
        if self.config.ping_path:
            return f"{self.base_url}{self.config.ping_path}"
        return self._models_url

    @property
    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def _unconfigured(self) -> Optional[str]:
        """端点没配齐时的人话原因（含缺哪个环境变量）；已配齐返回 None。"""
        if self.config.configured:
            return None
        return (f"{self.display} 端点未配置："
                f"{self.config.reason or '缺少 base_url / Key / 模型'}")

    # ----------------------------------------------------------------
    #  Chat Completions (同步)
    # ----------------------------------------------------------------
    def chat(
        self,
        messages: list,
        model: str = None,
        temperature: float = 0.3,
        max_tokens: int = 4096,
        system_prompt: str = None,
    ) -> Optional[str]:
        """
        同步 Chat Completion

        Args:
            messages: 消息列表 [{"role": "user", "content": "..."}, ...]
            model: 模型 ID（不给则用配置解析出的模型；sidecar 预设下即 "auto"）
            temperature: 温度（0.0-1.0）
            max_tokens: 最大输出 token 数
            system_prompt: 可选的系统提示词（会自动插入）

        Returns:
            回复文本，出错时返回 None
        """
        content, _ = self.chat_ex(messages=messages, model=model, temperature=temperature,
                                  max_tokens=max_tokens, system_prompt=system_prompt)
        return content

    def chat_ex(
        self,
        messages: list,
        model: str = None,
        temperature: float = 0.3,
        max_tokens: int = 4096,
        system_prompt: str = None,
    ) -> tuple:
        """`chat()` 的带原因版本：返回 (content 或 None, reason 或 None)。

        原先所有失败都并成 `None`，调用方无法区分「Sidecar 没起」与「LLM 答了但
        内容不可用」，于是 API 层只能拿 200+空结构冒充成功（M-4）。
        """
        import httpx

        unconfigured = self._unconfigured()
        if unconfigured:
            logger.warning(f"[{self.display}] {unconfigured}")
            return None, unconfigured

        full_messages = list(messages)
        if system_prompt:
            full_messages.insert(0, {"role": "system", "content": system_prompt})

        payload = {
            "model": model or self.model,
            "messages": full_messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }

        try:
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.post(self._chat_url, headers=self._headers, json=payload)
                resp.raise_for_status()
                data = resp.json()
                content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
                if not content or not content.strip():
                    return None, (f"{self.display} 200 但 choices 空"
                                  f"（model={payload['model']}, url={self._chat_url}）")
                return content.strip(), None
        except httpx.HTTPStatusError as e:
            reason = f"{self.display} HTTP {e.response.status_code}: {_brief(e.response.text)}"
            logger.warning(f"[{self.display}] {reason}")
            return None, reason
        except httpx.RequestError as e:
            reason = f"连不上 {self.display}（{type(e).__name__}，url={self._chat_url}）: {e}"
            logger.warning(f"[{self.display}] {reason}")
            return None, reason
        except Exception as e:
            reason = f"{self.display} 响应异常（{type(e).__name__}）: {e}"
            logger.warning(f"[{self.display}] {reason}")
            return None, reason

    # ----------------------------------------------------------------
    #  模型列表
    # ----------------------------------------------------------------
    def list_models(self) -> list:
        """获取可用模型列表"""
        models, _ = self.list_models_ex()
        return models

    def list_models_ex(self) -> tuple:
        """返回 (models, reason)；失败时 models 为 []，reason 非空。"""
        import httpx

        unconfigured = self._unconfigured()
        if unconfigured:
            return [], unconfigured

        try:
            with httpx.Client(timeout=10) as client:
                resp = client.get(self._models_url, headers=self._headers)
                resp.raise_for_status()
                data = resp.json()
                return data.get("data", []), None
        except Exception as e:
            reason = f"模型列表取不到（{type(e).__name__}，url={self._models_url}）: {e}"
            logger.warning(f"[{self.display}] {reason}")
            return [], reason

    # ----------------------------------------------------------------
    #  健康检查
    # ----------------------------------------------------------------
    def health_check(self) -> bool:
        """检查 LLM 端点是否可用"""
        ok, _ = self.health_check_ex()
        return ok

    def health_check_ex(self) -> tuple:
        """返回 (可用?, 不可用原因)。探活失败必须带原因，不得只报 False。"""
        import httpx

        unconfigured = self._unconfigured()
        if unconfigured:
            return False, unconfigured

        url = self._health_url
        try:
            with httpx.Client(timeout=5) as client:
                resp = client.get(url, headers=self._headers)
                if resp.status_code == 200:
                    return True, None
                return False, f"{self.display} 探活 HTTP {resp.status_code}（url={url}）"
        except Exception as e:
            return False, f"{self.display} 未启动或不可达（{type(e).__name__}，url={url}）"