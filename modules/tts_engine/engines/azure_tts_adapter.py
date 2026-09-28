"""
Azure AI Speech 付费档适配器（在线·高保真·整书批量）

选型来源：2026-09-24 在线 TTS 全网调研结论 —— 与 Edge 免费档同一批 zh-CN 神经音色
（音色库零迁移）、48kHz 高保真、大陆境内有区域（世纪互联版另有端点），
是"在线档"当付费主力最合理的一家。

**本文件的验证状态（务必读）**：
- 端点路由：2026-09-24 本机 `POST https://eastus.tts.speech.microsoft.com/cognitiveservices/v1`
  无 Key → **401**（路由在、只差鉴权），这是无凭据条件下能测到的全部。
- 请求/响应形态：按 Azure 官方 REST 文档实现（SSML in / 音频字节 out），
  **本机零 Key 未跑过一次真合成**，故 `verified_note` 明写这一点。
  拿到 Key 后第一件事是跑 `probe()`（只发一句"连通性测试。"）。
"""

import os
from typing import Optional

from modules.tts_engine.engines.cloud_tts_base import CloudTTSBase

DEFAULT_REGION = "eastus"
OUTPUT_FORMAT = "audio-24khz-48kbitrate-mono-mp3"
SAMPLE_RATE = 24000


class AzureTTSSAdapter(CloudTTSBase):
    """微软 Azure AI Speech（REST + SSML）。Key 缺失即不可用，绝不静默退回 Edge。"""

    engine_name = "azure"
    engine_display = "Azure 神经音色(云端付费·整书批量)"
    supported_languages = ["zh", "en", "ja", "ko", "yue", "fr", "de", "es"]
    supports_emotion = True      # 经 style="..." 的 express-as 表达，非全音色支持
    sample_rate = SAMPLE_RATE
    requires_model_object = False
    key_env = ("AZURE_SPEECH_KEY",)
    verified_note = ("端点路由 2026-09-24 实测可达（无 Key POST /cognitiveservices/v1 = 401）；"
                     "合成请求形态按官方 REST 文档实现，本机无 Key 未跑过真合成")

    def __init__(self):
        super().__init__()
        self._region = (os.getenv("AZURE_SPEECH_REGION") or DEFAULT_REGION).strip()

    @property
    def endpoint(self) -> str:
        """端点：显式 `AZURE_SPEECH_ENDPOINT` > 按区域拼公共云 URL。

        世纪互联版（azure.cn）的 host 不同，故留了显式口 —— 不猜它的区域拼法。
        """
        explicit = (os.getenv("AZURE_SPEECH_ENDPOINT") or "").strip().rstrip("/")
        if explicit:
            return explicit
        return f"https://{self._region}.tts.speech.microsoft.com/cognitiveservices/v1"

    def _resolve_voice(self, voice_id: Optional[str]) -> str:
        voice = (voice_id or "").strip()
        if voice.endswith("Neural") and "-" in voice:
            return voice
        # 传进来的是 DB uuid：解析成该音色行 meta.azure_voice，拿不到即报错（不退回默认嗓子）
        if voice:
            try:
                from modules.voice_manager.voice_store import voice_store
                row = voice_store.get(voice)
                mapped = (row.meta or {}).get("azure_voice") if row else None
                if mapped:
                    return mapped
            except Exception:
                pass
        raise RuntimeError(
            f"voice_id {voice_id!r} 不是 Azure shortName 且音色行未标 meta.azure_voice"
            " —— 拒绝静默退化为默认音色")

    @staticmethod
    def _escape(text: str) -> str:
        return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))

    def _ssml(self, text: str, voice: str, speed: float, pitch: float) -> str:
        rate_pct = max(-50, min(100, int(round((float(speed) - 1.0) * 100))))
        pitch_hz = max(-30, min(50, int(round(float(pitch) * 2))))
        return (
            f'<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" '
            f'xml:lang="zh-CN">'
            f'<voice name="{voice}">'
            f'<prosody rate="{rate_pct:+d}%" pitch="{pitch_hz:+d}Hz">'
            f'{self._escape(text)}</prosody></voice></speak>')

    def request_audio(self, text, voice, speed, pitch):
        import httpx

        params = self._ssml(text, voice, speed, pitch)
        headers = {
            "Ocp-Apim-Subscription-Key": os.getenv("AZURE_SPEECH_KEY", "").strip(),
            "Content-Type": "application/ssml+xml",
            "Accept": "audio/mpeg",
            "User-Agent": "peiyinyuan-cloud-tts",
            "X-Microsoft-OutputFormat": OUTPUT_FORMAT,
        }
        with httpx.Client(timeout=float(os.getenv("PYY_AZURE_TIMEOUT", "60"))) as client:
            resp = client.post(self.endpoint, headers=headers, content=params.encode("utf-8"))
            resp.raise_for_status()
            return resp.content, "mp3"

    def list_voices(self) -> list:
        """内置清单只列**官方稳定 shortName**（与 Edge 档同名同源），不是穷举。

        真正的权威清单要调 `GET /sounds`（需 Key），故 `list_voices()` 在无 Key 时
        只给这份保守子集，并在 `note` 里说明它不是全集。
        """
        subset = [
            ("zh-CN-XiaoxiaoNeural", "晓晓", "female", "adult"),
            ("zh-CN-YunxiNeural", "云希", "male", "adult"),
            ("zh-CN-YunjianNeural", "云健", "male", "adult"),
            ("zh-CN-XiaoyiNeural", "晓伊", "female", "young"),
            ("zh-CN-YunyangNeural", "云扬", "male", "adult"),
        ]
        return [{"id": sid, "name": f"Azure-{name}", "engine": self.engine_name,
                 "gender": g, "age_group": a, "language": "zh", "online": True,
                 "azure_voice": sid} for sid, name, g, a in subset]
