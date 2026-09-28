"""
电子书元数据提取器
提取书名、作者、封面等元信息
"""

import re
from typing import Optional


class MetadataExtractor:
    """从文本内容中提取元数据"""

    @classmethod
    def extract_title(cls, text: str) -> Optional[str]:
        """从开头文本中提取书名"""
        lines = [l.strip() for l in text.split('\n') if l.strip()]
        if not lines:
            return None

        # 常见中文书名模式
        title_patterns = [
            r'《(.{1,40})》',             # 《书名》
            r'书名[：:]\s*(.{1,40})',     # 书名：xxx
            r'^(.{1,30})\s*著',           # xxx 著
            r'^(.{1,30})\s*[作编]',       # xxx 作/编
        ]

        # 在前 10 行中搜索
        for line in lines[:10]:
            for pattern in title_patterns:
                match = re.search(pattern, line)
                if match:
                    return match.group(1).strip()

        return None

    @classmethod
    def extract_author(cls, text: str) -> Optional[str]:
        """从开头文本中提取作者"""
        lines = [l.strip() for l in text.split('\n') if l.strip()]

        author_patterns = [
            r'作者[：:]\s*(.{1,20})',
            r'[著编]者[：:]\s*(.{1,20})',
            r'^(.{1,24})\s*[著作]',
            r'原作[：:]\s*(.{1,20})',
            r'Written by\s+(.{1,30})',
            r'Author[：:]\s*(.{1,30})',
        ]

        for line in lines[:15]:
            for pattern in author_patterns:
                match = re.search(pattern, line, re.IGNORECASE)
                if match:
                    name = match.group(1).strip()
                    # 过滤明显不是人名的
                    if len(name) <= 10 and not name.startswith('第'):
                        return name

        return None

    @classmethod
    def guess_language(cls, text: str) -> str:
        """推测文本主要语言"""
        sample = text[:2000]
        chinese_chars = len(re.findall(r'[\u4e00-\u9fff]', sample))
        english_chars = len(re.findall(r'[a-zA-Z]', sample))

        if chinese_chars + english_chars == 0:
            return "unknown"

        chinese_ratio = chinese_chars / (chinese_chars + english_chars)
        return "zh" if chinese_ratio > 0.5 else "en"

    def extract(self, text: str) -> Optional[dict]:
        """综合提取元数据（书名、作者、语言）"""
        if not text:
            return None
        return {
            "title": self.extract_title(text),
            "author": self.extract_author(text),
            "language": self.guess_language(text),
        }