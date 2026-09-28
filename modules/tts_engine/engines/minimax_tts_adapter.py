"""
MiniMax 语音合成适配器（在线付费·中文克隆音色谱系）

**本文件的验证状态（务必读）**：
- 端点路由：2026-09-24 本机 `POST https://api.minimax.chat/v1/t2a_v2`（空 body、无 Key）
  → HTTP 200 + `{"base_resp":{"status_code":1004,"status_msg":"login fail: Please carry
  the API secret key in the 'Authorization' field of the request header"}}`。
  这条回包本身是**厂商亲口告知的鉴权方式**（Authorization 头），不是我的推测。
- 请求体字段名/响应 `data.audio` 的编码形态：按厂方文档实现，**本机无 Key 未跑过真合成**，
  所以 `verified_note` 明写未实测；解码侧做了 hex / base64 / URL 三态容错，
  真跑第一次时最坏是"报错带原文"，不会静音。
"""

import base64
import binascii
import json
import os
import re
from typing import Optional

from modules.tts_engine.engines.cloud_tts_base import CloudTTSBase

DEFAULT_MODEL = "speech-02-hd"
DEFAULT_BASE_URL = "https://api.minimax.chat"
SAMPLE_RATE = 32000
_URL_RE = re.compile(r"^https?://\S+$")


class MiniMaxTTSAdapter(CloudTTSBase):
    """MiniMax t2a_v2（同步返回音频）。Key 缺失即不可用。"""

    engine_name = "minimax"
    engine_display = "MiniMax 语音(云端付费·音色多)"
    supported_languages = ["zh", "en"]
    supports_emotion = True
    sample_rate = SAMPLE_RATE
    requires_model_object = False
    key_env = ("MINIMAX_API_KEY",)
    verified_note = ("端点路由 + 鉴权方式 2026-09-24 实测（无 Key POST /v1/t2a_v2 回 HTTP 200 + "
                     "网关码 1004『login fail: Please carry the API secret key in the "
                     "'Authorization' field』）；请求体字段与 data.audio 编码形态无 Key 未实测")

    @property
    def base_url(self) -> str:
        return (os.getenv("MINIMAX_BASE_URL") or DEFAULT_BASE_URL).strip().rstrip("/")

    @property
    def endpoint(self) -> str:
        return f"{self.base_url}/v1/t2a_v2"

    def _resolve_voice(self, voice_id: Optional[str]) -> str:
        voice = (voice_id or "").strip()
        if voice and "-" in voice:
            return voice
        if voice:
            try:
                from modules.voice_manager.voice_store import voice_store
                row = voice_store.get(voice)
                mapped = (row.meta or {}).get("minimax_voice_id") if row else None
                if mapped:
                    return mapped
            except Exception:
                pass
        raise RuntimeError(
            f"voice_id {voice_id!r} 不是 MiniMax voice_id 且音色行未标 meta.minimax_voice_id"
            " —— 拒绝静默退化为默认音色")

    def request_audio(self, text, voice, speed, pitch):
        import httpx

        payload = {
            "model": (os.getenv("MINIMAX_TTS_MODEL") or DEFAULT_MODEL).strip(),
            "text": text,
            "stream": False,
            "voice_setting": {
                "voice_id": voice,
                "speed": max(0.5, min(2.0, float(speed))),
                "vol": 1.0,
                "pitch": int(round(float(pitch))),
            },
            "audio": {"format": "mp3", "sample_rate": SAMPLE_RATE, "bitrate": 128000},
        }
        headers = {"Authorization": f"Bearer {os.getenv('MINIMAX_API_KEY', '').strip()}",
                   "Content-Type": "application/json"}
        with httpx.Client(timeout=float(os.getenv("PYY_MINIMAX_TIMEOUT", "60"))) as client:
            resp = client.post(self.endpoint, headers=headers, json=payload)
            resp.raise_for_status()
            return self._extract_audio(resp.text), "mp3"

    @staticmethod
    def _extract_audio(body_text: str) -> bytes:
        """三态取音频：URL / hex / base64。取不到就把原文带进异常（不留静音）。"""
        try:
            data = json.loads(body_text)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"MiniMax 返回不是 JSON（{e}）: {body_text[:160]!r}") from e

        base = data.get("base_resp") or {}
        if base and base.get("status_code") not in (0, None):
            raise RuntimeError(
                f"MiniMax 业务错误 status_code={base.get('status_code')}: "
                f"{base.get('status_msg')}")

        payload = data.get("data") or {}
        audio = payload.get("audio") or payload.get("audio_url") or data.get("audio_url")
        if not audio:
            raise RuntimeError(f"MiniMax 200 但取不到音频字段: {body_text[:160]!r}")
        if isinstance(audio, str) and _URL_RE.match(audio):
            import httpx
            with httpx.Client(timeout=60) as client:
                return client.get(audio).content
        if isinstance(audio, str):
            for decoder in (bytes.fromhex, lambda s: base64.b64decode(s, validate=True)):
                try:
                    raw = decoder(audio)
                    if raw:
                        return raw
                except (ValueError, binascii.Error):
                    continue
        raise RuntimeError(f"MiniMax 音频字段形态不认识（前 80 字：{str(audio)[:80]!r}）")

    def list_voices(self) -> list:
        """保守子集：这几个 voice_id 出现在 2026-09-24 的调研材料里。

        全集要调 `GET /v1/get_voice`（需 Key）。**无 Key 未实测过任何一枚能否合成**，
        故每条带 `verified: False`，前端与音色入库都应把它当候选而非既成事实。
        """
        subset = [
            ("male-qn-qingse", "青涩男声", "male", "young"),
            ("male-qn-jingying", "精英男声", "male", "adult"),
            ("male-qn-badao", "霸道男声", "male", "adult"),
            ("female-shaonv", "少女", "female", "young"),
            ("female-yujie", "御姐", "female", "adult"),
            ("female-chengshu", "成熟女性", "female", "adult"),
            ("preset_tingnao_pub", "系统旁白", "neutral", "adult"),
        ]
        return [{"id": vid, "name": f"MiniMax-{name}", "engine": self.engine_name,
                 "gender": g, "age_group": a, "language": "zh", "online": True,
                 "minimax_voice_id": vid, "verified": False}
                for vid, name, g, a in subset]
