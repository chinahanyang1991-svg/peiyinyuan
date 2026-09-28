"""转录文本格式化器（参考 claude-supermemory transcript-formatter）"""

import re


def format_transcript(raw_text: str, speaker_labels: list = None) -> str:
    """
    将原始转录文本格式化为结构化对话

    Args:
        raw_text: 原始转录文本
        speaker_labels: 说话人标签列表，如 ["说话人A", "说话人B"]

    Returns:
        str: 格式化的对话文本
    """
    if not raw_text:
        return ""

    sentences = re.split(r"(?<=[。！？.!?])", raw_text)
    sentences = [s.strip() for s in sentences if s.strip()]

    if speaker_labels and len(speaker_labels) >= 2:
        formatted = []
        for i, sent in enumerate(sentences):
            speaker = speaker_labels[i % len(speaker_labels)]
            formatted.append(f"{speaker}: {sent}")
        return "\n".join(formatted)

    return "\n".join(sentences)


def extract_timestamps(text: str) -> list:
    """从转录文本中提取时间戳"""
    pattern = r"\[(\d{2}:\d{2}:\d{2}(?:\.\d+)?)\]"
    return re.findall(pattern, text)


def clean_transcript(text: str) -> str:
    """清理转录文本中的噪声标记"""
    text = re.sub(r"\[\d{2}:\d{2}:\d{2}(?:\.\d+)?\]", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()