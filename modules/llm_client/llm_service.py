"""
LLM 智能服务
利用 FreeLLMAPI 提供智能分析能力的业务封装
"""

import json
import re
from typing import Optional

from modules.llm_client.freellmapi_client import FreeLLMAPIClient

# 系统提示词模板（用于不同场景）
SYSTEM_PROMPTS = {
    "chapter_detect": """你是一个专业的电子书章节分析助手。你的任务是分析给定的文本内容，找出所有章节标题和分隔点。

规则：
1. 识别章节标题（如"第一章"、"Chapter 1"、"一、"等）
2. 对每个章节输出：章节标题 + 内容概要（前50字）
3. 如果文本没有明显章节结构，请说明"无章节结构"
4. 输出格式为JSON数组：[{"title": "章节标题", "summary": "内容概要"}]

只输出JSON，不要输出其他内容。""",

    "text_polish": """你是一个专业的文本校对助手。你的任务是清洗和优化文本内容。

规则：
1. 修正明显的OCR错误（如"册0"→"用","0"→"的","l"→"1"等）
2. 移除异常重复字符
3. 保持原文含义不变，不要改写内容
4. 保持段落结构不变
5. 只输出清洗后的文本，不要添加解释""",

    "ssml_optimize": """你是一个专业的SSML语音合成标记专家。你的任务是为文本添加SSML标签以优化语音合成效果。

规则：
1. 识别文本中的情感变化（喜悦、悲伤、愤怒等），在相应位置添加 <emphasis level="strong">标签
2. 在较长的叙述段落后添加 <break time="500ms"/> 停顿标签
3. 识别对话内容（引号内），用 <prosody rate="medium"> 标签包裹
4. 识别章节标题，用 <prosody rate="slow" pitch="+10%"> 标签包裹
5. 只输出SSML格式文本，不要添加解释""",

    "sentiment_analysis": """你是一个情感分析专家。分析以下文本片段的情绪状态。

规则：
1. 输出格式为JSON：{"sentiment": "happy|sad|angry|neutral|calm|excited|fearful", "intensity": 0.0-1.0, "description": "简要说明"}
2. 只输出JSON，不要输出其他内容""",

    "voice_description": """你是一个语音设计专家。分析用户对音色的描述文本，提取关键的语音特征。

规则：
1. 输出格式为JSON：
{
  "gender": "male|female|neutral",
  "age_group": "young|middle|elderly",
  "style": "温柔|沉稳|活泼|庄重|甜美|磁性|清脆|浑厚",
  "speed": "slow|normal|fast",
  "pitch": "low|medium|high",
  "language": "zh|en|ja|ko",
  "keywords": ["关键词1", "关键词2"]
}
2. 只输出JSON，不要输出其他内容""",
}


class LLMResult:
    """一次 LLM 业务调用的诚实出口。

    M-4 的根因是"取不到"与"取到了但没内容"在返回值里长得一样，于是 API 层
    只能 200 + 空结构交差。这里把两者拆开：

      source = "llm"           LLM 真答了且解析成功，value 可用
      source = "passthrough"   LLM 没答，value 是**原文回透传**（不是润色/优化结果）
      source = "partial"       分段任务里只有部分段拿到 LLM 结果，其余段是原文
      source = "rule_fallback" LLM 没答，value 来自规则引擎（设计内兜底，但仍须标出）
      source = "empty"         既没答也无可兜底的值，value 为 None/[]
    """

    __slots__ = ("value", "source", "reason")

    def __init__(self, value, source: str, reason: Optional[str] = None):
        self.value = value
        self.source = source
        self.reason = reason

    @property
    def ok(self) -> bool:
        return self.source == "llm"


class LLMService:
    """
    LLM 智能服务

    封装了 FreeLLMAPI 的典型应用场景：
    - 智能章节检测
    - 文本校对/润色
    - SSML 优化
    - 情感分析
    - 语音描述解析
    """

    def __init__(self, client: FreeLLMAPIClient = None):
        self.client = client or FreeLLMAPIClient()

    # ----------------------------------------------------------------
    #  智能章节检测 (LLM增强版)
    # ----------------------------------------------------------------
    def smart_chapter_detect(self, text: str, max_text_len: int = 12000) -> LLMResult:
        """
        LLM 增强的章节检测

        对于正则无法处理的复杂章节结构，使用 LLM 分析。
        输入长文本时只分析首部和关键片段。

        Returns:
            LLMResult，value 为 [{"title": str, "summary": str}, ...] 或 []
        """
        # 只取文本前部（章节标题通常在前半部分）
        sample = text[:max_text_len]
        if len(text) > max_text_len:
            # 在 8000 字位置前后找章节边界
            mid_pos = len(text) // 2
            sample = text[:max_text_len] + "\n...\n" + text[mid_pos:mid_pos + 2000]

        result, reason = self.client.chat_ex(
            messages=[{"role": "user", "content": f"请分析以下文本的章节结构：\n\n{sample}"}],
            system_prompt=SYSTEM_PROMPTS["chapter_detect"],
            temperature=0.1,
        )

        if not result:
            return LLMResult([], "empty", reason or "LLM 无回复")

        try:
            chapters = json.loads(result)
        except json.JSONDecodeError:
            return LLMResult([], "empty",
                             f"章节检测回复不是合法 JSON（前 120 字：{result[:120]!r}）")
        if not isinstance(chapters, list):
            return LLMResult([], "empty",
                             f"章节检测回复不是 JSON 数组（实得 {type(chapters).__name__}）")
        return LLMResult(chapters, "llm")

    # ----------------------------------------------------------------
    #  文本校对/润色
    # ----------------------------------------------------------------
    def polish_text(self, text: str, max_len: int = 4000) -> LLMResult:
        """
        LLM 文本校对

        修正 OCR 错误、异常字符等。
        输入较长时会分段处理。

        回透传（未处理的原文）必须带 source，否则调用方读到的是"润色过了"。
        """
        def _one(chunk: str):
            result, reason = self.client.chat_ex(
                messages=[{"role": "user", "content": chunk}],
                system_prompt=SYSTEM_PROMPTS["text_polish"],
                temperature=0.1,
                max_tokens=8192,
            )
            return (result.strip() if result else None), reason

        if len(text) <= max_len:
            polished, reason = _one(text)
            if polished is None:
                return LLMResult(text, "passthrough", reason or "LLM 无回复，原文回透传")
            return LLMResult(polished, "llm")

        # 分段处理（长文本）
        chunks, failed = [], []
        for i in range(0, len(text), max_len):
            chunk = text[i:i + max_len]
            polished, reason = _one(chunk)
            if polished is None:
                failed.append(i // max_len + 1)
                chunks.append(chunk)
            else:
                chunks.append(polished)

        total = (len(text) + max_len - 1) // max_len
        if not failed:
            return LLMResult("\n".join(chunks), "llm")
        reason = f"{len(failed)}/{total} 段 LLM 未答，那些段是原文回透传（段号 {failed[:10]}）"
        source = "passthrough" if len(failed) == total else "partial"
        return LLMResult("\n".join(chunks), source, reason)

    # ----------------------------------------------------------------
    #  SSML 优化
    # ----------------------------------------------------------------
    def optimize_ssml(self, text: str) -> LLMResult:
        """
        LLM SSML 优化

        添加情感标记、停顿、语速控制标签。

        Returns:
            LLMResult，value 为 SSML 文本（无根标签，可直接嵌入 <speak> 中）；
            LLM 未答时 value 是**原文**，source="passthrough" 明示没加过标签。
        """
        result, reason = self.client.chat_ex(
            messages=[{"role": "user", "content": f"为以下文本添加SSML标签：\n\n{text}"}],
            system_prompt=SYSTEM_PROMPTS["ssml_optimize"],
            temperature=0.2,
            max_tokens=8192,
        )
        if not result:
            return LLMResult(text, "passthrough", reason or "LLM 无回复，原文回透传（未加 SSML 标签）")
        return LLMResult(result.strip(), "llm")

    # ----------------------------------------------------------------
    #  情感分析
    # ----------------------------------------------------------------
    def analyze_sentiment(self, text: str) -> LLMResult:
        """
        文本情感分析

        Returns:
            LLMResult，value 为 {"sentiment": str, "intensity": float, "description": str}
            或 None。取不到时**不再编造 neutral/0.5**（那是把 0 值伪装成测量值）。
        """
        result, reason = self.client.chat_ex(
            messages=[{"role": "user", "content": f"分析以下文本的情感：\n\n{text[:2000]}"}],
            system_prompt=SYSTEM_PROMPTS["sentiment_analysis"],
            temperature=0.1,
        )
        if not result:
            return LLMResult(None, "empty", reason or "LLM 无回复")

        parsed = self._loads_json_object(result)
        if parsed is None:
            return LLMResult(None, "empty", f"情感分析回复无法解析为 JSON（前 120 字：{result[:120]!r}）")
        return LLMResult(parsed, "llm")

    # ----------------------------------------------------------------
    #  语音描述解析 (LLM增强版)
    # ----------------------------------------------------------------
    def parse_voice_description(self, description: str) -> LLMResult:
        """
        LLM 增强的语音描述解析

        比规则引擎更准确地从自然语言描述中提取语音特征。

        Returns:
            LLMResult，value 为
            {"gender": ..., "age_group": ..., "style": ..., "speed": ..., "pitch": ..., "language": ..., "keywords": [...]}
            规则引擎兜底是设计内路径，但仍须以 source="rule_fallback" 标出。
        """
        result, reason = self.client.chat_ex(
            messages=[{"role": "user", "content": f"分析以下语音描述：\n\n{description}"}],
            system_prompt=SYSTEM_PROMPTS["voice_description"],
            temperature=0.1,
        )

        if not result:
            return LLMResult(self._fallback_parse(description), "rule_fallback",
                             reason or "LLM 无回复，走规则引擎兜底")

        parsed = self._loads_json_object(result)
        if parsed is None:
            return LLMResult(self._fallback_parse(description), "rule_fallback",
                             f"LLM 回复无法解析为 JSON，走规则引擎兜底（前 120 字：{result[:120]!r}）")
        return LLMResult(parsed, "llm")

    # ----------------------------------------------------------------
    #  API Ready 检查
    # ----------------------------------------------------------------
    @staticmethod
    def _loads_json_object(text: str) -> Optional[dict]:
        """解析模型返回的 JSON 对象；整串不行时再抠里面的 {...} 片段。"""
        try:
            parsed = json.loads(text)
            return parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", text, re.DOTALL)
            if match:
                try:
                    parsed = json.loads(match.group())
                    return parsed if isinstance(parsed, dict) else None
                except json.JSONDecodeError:
                    return None
        return None

    def availability(self) -> tuple:
        """(可用?, 不可用原因)。探活失败必须带原因——`False` 单独不算回答。"""
        return self.client.health_check_ex()

    def provider(self) -> dict:
        """当前接的是哪家大模型（不含 Key）。测试替身客户端可能没有 config 槽，故兜底。"""
        config = getattr(self.client, "config", None)
        if config is None:
            return {"preset": "unknown", "display": "未知客户端", "configured": False,
                    "reason": "客户端未暴露 provider 配置（非 FreeLLMAPIClient 实例）"}
        return config.describe()

    def is_ready(self) -> bool:
        """检查 LLM Sidecar 是否可用"""
        return self.availability()[0]

    # ----------------------------------------------------------------
    #  回退解析（LLM不可用时的规则解析）
    # ----------------------------------------------------------------
    @staticmethod
    def _fallback_parse(description: str) -> dict:
        """规则引擎回退"""
        desc_lower = description.lower()

        # 性别检测
        gender = "neutral"
        if any(w in desc_lower for w in ["男", "男性", "男生", "先生", "man", "male", "低沉", "浑厚", "磁性"]):
            gender = "male"
        elif any(w in desc_lower for w in ["女", "女性", "女生", "女士", "woman", "female", "甜美", "温柔", "清脆"]):
            gender = "female"

        # 年龄检测
        age_group = "middle"
        if any(w in desc_lower for w in ["年轻", "青年", "少年", "young"]):
            age_group = "young"
        elif any(w in desc_lower for w in ["年长", "老年", "老人", "elderly", "old"]):
            age_group = "elderly"

        # 风格检测
        style_map = {
            "温柔": "温柔", "沉稳": "沉稳", "活泼": "活泼",
            "庄重": "庄重", "甜美": "甜美", "磁性": "磁性",
            "清脆": "清脆", "浑厚": "浑厚", "专业": "沉稳",
            "温暖": "温柔", "可爱": "活泼", "严肃": "庄重",
        }
        style = "温柔"
        for kw, s in style_map.items():
            if kw in desc_lower:
                style = s
                break

        return {
            "gender": gender,
            "age_group": age_group,
            "style": style,
            "speed": "normal",
            "pitch": "medium",
            "language": "zh",
            "keywords": [description[:20]],
        }