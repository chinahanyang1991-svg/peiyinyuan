"""
文本规范化器
参考 VoxCPM2 的 text_normalize.py，规范化数字、符号、缩写等
"""

import re


class TextNormalizer:
    """文本规范化：数字、符号、特殊字符标准化"""

    CN_DIGITS = {
        "0": "零", "1": "一", "2": "二", "3": "三", "4": "四",
        "5": "五", "6": "六", "7": "七", "8": "八", "9": "九",
    }

    CN_UNITS = ["", "十", "百", "千", "万", "十万", "百万", "千万", "亿"]

    def normalize(self, text: str) -> str:
        """主规范化入口"""
        if not text:
            return text

        text = self._normalize_whitespace(text)
        text = self._normalize_numbers(text)
        text = self._normalize_punctuation(text)
        text = self._normalize_abbreviations(text)
        return text.strip()

    def _normalize_whitespace(self, text: str) -> str:
        """统一空白字符"""
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text

    def _normalize_numbers(self, text: str) -> str:
        """规范化数字（保留原样，供 TTS 引擎处理）"""
        text = re.sub(r"(\d),(\d)", r"\1\2", text)
        text = re.sub(r"(\d+)%", r"百分之\1", text)
        return text

    def _normalize_punctuation(self, text: str) -> str:
        """规范化标点符号"""
        text = text.replace('"', "\u201c").replace('"', "\u201d")
        text = re.sub(r"[。！？]{2,}", lambda m: m.group()[0], text)
        text = re.sub(r"([\u4e00-\u9fff])([A-Za-z])", r"\1 \2", text)
        text = re.sub(r"([A-Za-z])([\u4e00-\u9fff])", r"\1 \2", text)
        return text

    def _normalize_abbreviations(self, text: str) -> str:
        """展开常见缩写"""
        replacements = {
            "Mr.": "Mister", "Mrs.": "Misses", "Dr.": "Doctor",
            "etc.": "et cetera", "e.g.": "for example",
            "i.e.": "that is", "vs.": "versus",
        }
        for abbr, full in replacements.items():
            text = text.replace(abbr, full)
        return text

    def quick_normalize(self, text: str) -> str:
        """快速规范化（仅空白和基础标点）"""
        text = re.sub(r"[ \t]+", " ", text)
        text = text.strip()
        return text