"""
LLM 智能分析 API 路由
提供文本校对、章节分析、SSML优化、情感分析等智能化接口

响应外壳（U-6 / M-4）—— 本文件的原则是「取不到」不许长得像「取到了」：

    status = "ok"        LLM 真答了且解析成功
    status = "degraded"  值来自兜底（原文回透传 / 分段部分失败 / 规则引擎），带 reason
    HTTP 503             LLM 既没答也没有可兜底的值，detail 里是人话原因

旧实现五种情况一律 `{"status": "ok", ...}` + 空数组 / 原文回显 / neutral-0.5，
调用方无法区分"这本书没有章节"和"Sidecar 根本没起"——那正是 M-4 定罪的东西。

下面的端点**故意不写 async def**：`FreeLLMAPIClient` 是阻塞式 httpx（超时 120s），
挂在事件循环上时一枚卡住的 Sidecar 请求会把整个后端冻住（同 PY-340 那一族）。
声明成普通函数后 FastAPI 把它丢进线程池，循环只付协程调度的代价。
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from modules.llm_client.llm_service import LLMResult, LLMService
from modules.llm_client.providers import LLM_PRESETS

router = APIRouter(tags=["LLM 智能分析"])

# 单例
llm_service = LLMService()


# ========== 请求/响应模型 ==========

class ChapterDetectRequest(BaseModel):
    text: str
    max_len: int = 12000

class PolishRequest(BaseModel):
    text: str
    max_len: int = 4000

class SSMLRequest(BaseModel):
    text: str

class SentimentRequest(BaseModel):
    text: str

class VoiceDescriptionRequest(BaseModel):
    description: str


# ========== 出口装配 ==========

def _hint() -> str:
    """503 里那句"怎么办"必须跟着当前接的端点走。

    写死"请先起 docker/llm"在接云端大模型时是一句误导（用户照着起 sidecar，
    而缺的其实是一个 API Key），所以按 preset 分岔。
    """
    config = llm_service.provider()
    if config.get("preset") == "freellmapi":
        return "（本机 Sidecar 未启动：起 docker compose --profile llm，或设 LLM_PRESET 接云端大模型）"
    if config.get("requires_api_key") and not config.get("api_key_present"):
        return f"（{config.get('display')} 缺 API Key：设该厂商的 *_API_KEY 或 LLM_API_KEY 环境变量）"
    return f"（当前端点：{config.get('display')} @ {config.get('base_url')}）"


def _shell(result: LLMResult, **payload) -> dict:
    """把 LLMResult 摊成响应体。source="empty" 一律 503，不再 200+空。"""
    if result.source == "empty":
        raise HTTPException(
            status_code=503,
            detail=f"LLM 未产出可用结果：{result.reason or '无原因返回'} {_hint()} "
                   "或改用不带 LLM 的入口")
    return {
        **payload,
        # 契约键放最后：LLM 解析出的 dict 里若恰好带 status/source/reason，不得覆盖外壳。
        "status": "ok" if result.source == "llm" else "degraded",
        "source": result.source,
        "reason": result.reason,
    }


# ========== API 端点 ==========

@router.post("/detect-chapters")
def llm_chapter_detect(req: ChapterDetectRequest):
    """LLM 智能章节检测"""
    if not req.text.strip():
        raise HTTPException(400, "文本内容不能为空")
    result = llm_service.smart_chapter_detect(req.text, req.max_len)
    chapters = result.value or []
    return _shell(result, chapters=chapters, count=len(chapters))


@router.post("/polish")
def llm_polish(req: PolishRequest):
    """LLM 文本校对/润色"""
    if not req.text.strip():
        raise HTTPException(400, "文本内容不能为空")
    result = llm_service.polish_text(req.text, req.max_len)
    text = result.value if result.value is not None else ""
    return _shell(
        result,
        original_length=len(req.text),
        polished_length=len(text),
        changed=text != req.text,
        text=text,
    )


@router.post("/optimize-ssml")
def llm_ssml(req: SSMLRequest):
    """LLM SSML 优化"""
    if not req.text.strip():
        raise HTTPException(400, "文本内容不能为空")
    result = llm_service.optimize_ssml(req.text)
    ssml = result.value if result.value is not None else ""
    return _shell(result, ssml=ssml, has_tags="<break" in ssml or "<prosody" in ssml)


@router.post("/analyze-sentiment")
def llm_sentiment(req: SentimentRequest):
    """LLM 情感分析（取不到时不再编造 neutral/0.5）"""
    if not req.text.strip():
        raise HTTPException(400, "文本内容不能为空")
    result = llm_service.analyze_sentiment(req.text)
    return _shell(result, **(result.value or {}))


@router.post("/parse-voice")
def llm_parse_voice(req: VoiceDescriptionRequest):
    """LLM 语音描述解析（规则引擎兜底会明确标 source=rule_fallback）"""
    if not req.description.strip():
        raise HTTPException(400, "描述不能为空")
    result = llm_service.parse_voice_description(req.description)
    return _shell(result, **(result.value or {}))


@router.get("/ready")
def llm_ready():
    """检查 LLM 端点是否可用（不可用时带原因，不只报 False）"""
    ready, reason = llm_service.availability()
    return {"status": "ok" if ready else "unavailable", "ready": ready, "reason": reason}


@router.get("/provider")
def llm_provider():
    """当前大模型接入端口：接的是哪一家、缺什么才可用（**不返回 Key**）。

    单独一枚而不塞进 /health，是因为这题的答案与网络无关（纯配置解析），
    前端要能在 sidecar/云端都不可达时仍然画得出"该填哪个环境变量"。
    """
    config = llm_service.provider()
    return {
        "status": "ok" if config.get("configured") else "unconfigured",
        **config,
        "presets": [
            {"preset": name, "display": spec["display"], "base_url": spec["base_url"],
             "requires_api_key": spec["needs_key"], "api_key_env": spec.get("api_key_env"),
             "default_model": spec["model"] or None, "probe": spec.get("probe")}
            for name, spec in LLM_PRESETS.items()
        ],
    }


@router.get("/health")
def llm_health():
    """LLM 服务健康检查（含细节）

    旧实现在 sidecar 不可达时返 `{"status":"ok","ready":false,"model_count":0,"models":[]}`
    —— 200 + 空清单，读不出"为什么空"。现在 `status` 随可用性变，并带 reason。
    """
    ready, reason = llm_service.availability()
    if not ready:
        return {
            "status": "unavailable",
            "ready": False,
            "reason": reason,
            "model_count": 0,
            "models": [],
        }
    models, models_reason = llm_service.client.list_models_ex()
    return {
        "status": "ok" if not models_reason else "degraded",
        "ready": True,
        "reason": models_reason,
        "model_count": len(models),
        "models": [m.get("id", str(m)) for m in models[:10]],
    }
