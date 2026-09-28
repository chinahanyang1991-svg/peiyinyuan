"""
语气/语调检测器
基于标点符号、关键词和句式结构检测语句情感
"""

import re


class ToneDetector:
    """语气/语调检测器，为 TTS 生成情感控制参数"""

    # 情感关键词库
    EMOTION_KEYWORDS = {
        "excited": [
            "！", "!!", "太好了", "太棒了", "天啊", "天哪",
            "哇", "哈哈", "呵呵", "耶", "万岁", "厉害",
            "amazing", "wow", "great", "fantastic", "awesome",
        ],
        "question": [
            "？", "?", "什么", "为何", "为什么", "怎么", "哪里",
            "吗", "呢", "吧", "谁", "哪", "几时",
            "what", "why", "how", "when", "where", "who",
        ],
        "sad": [
            "……", "...", "难过", "悲伤", "哭泣", "流泪",
            "呜呜", "痛苦", "绝望", "叹息", "伤心",
            "唉", "罢了",
        ],
        "angry": [
            "混蛋", "可恶", "该死", "滚", "去死",
            "混账", "放肆", "岂有此理", "你够了",
            "你太过分了", "真是过分",
        ],
        "gentle": [
            "请", "谢谢", "感谢", "麻烦", "不好意思",
            "打扰", "抱歉", "费心", "不客气",
        ],
        "surprised": [
            "竟然", "居然", "怎么会", "不可思议",
            "居然如此", "难以想象",
        ],
    }

    # 标点符号映射 (PY-110: 改为带权重; ？为疑问硬信号, ！仅软信号,
    # 避免 "混蛋，你给我滚！" 这类带 angry 词汇的句子被 ！ 误判为 excited)
    PUNCT_MAP = {
        "？": ("question", 0.8),
        "?": ("question", 0.8),
        "！": ("excited", 0.4),
        "!": ("excited", 0.4),
        "……": ("sad", 0.8),
        "...": ("sad", 0.8),
    }

    # 句首疑问硬信号（"请问/为什么..." 本身就是疑问句式）
    QUESTION_PREFIXES = ("请问", "为什么", "为何", "怎么", "如何", "哪里",
                         "谁", "是不是", "能不能", "可不可以", "怎么办", "难道")

    def __init__(self):
        # 编译关键词正则
        self.keyword_patterns = {}
        for emotion, keywords in self.EMOTION_KEYWORDS.items():
            escaped = [re.escape(k) for k in keywords]
            self.keyword_patterns[emotion] = re.compile(
                '|'.join(escaped), re.IGNORECASE
            )

    def detect(self, text: str) -> dict:
        """
        检测文本情感/语气
        返回: {"primary": str, "confidence": float, "scores": {emotion: float}}
        """
        if not text or not text.strip():
            return {"primary": "neutral", "confidence": 0.5, "scores": {}}

        text = text.strip()
        scores = {}

        # 1. 标点符号判定（带权重: ？为硬信号, ！为软信号）
        for punct, (emotion, weight) in self.PUNCT_MAP.items():
            if text.endswith(punct):
                scores[emotion] = scores.get(emotion, 0) + weight

        # 2. 疑问硬信号 (PY-110): 句首疑问词 / 句末语气词 "吗/呢" —— 优先级最高
        if text.startswith(self.QUESTION_PREFIXES):
            scores["question"] = scores.get("question", 0) + 0.85
        if re.search(r'(吗|呢|么)[\u201d"\u300d]?\s*$', text):
            scores["question"] = scores.get("question", 0) + 0.6

        # 3. 关键词匹配
        for emotion, pattern in self.keyword_patterns.items():
            matches = pattern.findall(text)
            if matches:
                # 每个匹配关键词 +0.15 分，上限 0.7
                score = min(0.7, len(matches) * 0.15)
                scores[emotion] = scores.get(emotion, 0) + score

        # 4. 愤怒词汇为强信号（不被 ！ 的 excited 软信号压过）
        if scores.get("angry", 0) > 0:
            scores["angry"] += 0.6

        # 5. 句式特征
        if text.startswith("能不能") or text.startswith("可以"):
            scores["question"] = scores.get("question", 0) + 0.3
        if text.startswith("为什么") or text.startswith("如何"):
            scores["question"] = scores.get("question", 0) + 0.4

        # 4. 确定主要情感
        if not scores:
            return {"primary": "neutral", "confidence": 0.5, "scores": {}}

        primary = max(scores, key=scores.get)
        confidence = min(0.95, scores[primary])

        return {
            "primary": primary,
            "confidence": round(confidence, 2),
            "scores": {k: round(v, 2) for k, v in scores.items()},
        }

    def to_tts_params(self, tone: dict) -> dict:
        """
        将情感检测结果转换为 TTS 控制参数
        """
        emotion_map = {
            "neutral": {"emotion": "neutral", "speed": 1.0, "pitch": 0.0},
            "excited": {"emotion": "excited", "speed": 1.15, "pitch": 3.0},
            "question": {"emotion": "neutral", "speed": 1.0, "pitch": 5.0},  # 疑问上扬
            "sad": {"emotion": "sad", "speed": 0.85, "pitch": -2.0},
            "angry": {"emotion": "angry", "speed": 1.1, "pitch": 2.0},
            "gentle": {"emotion": "gentle", "speed": 0.9, "pitch": 0.0},
            "surprised": {"emotion": "excited", "speed": 1.1, "pitch": 4.0},
        }

        primary = tone.get("primary", "neutral")
        params = emotion_map.get(primary, emotion_map["neutral"]).copy()
        params["tone_confidence"] = tone.get("confidence", 0.5)
        return params