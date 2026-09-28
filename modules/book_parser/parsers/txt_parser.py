"""
TXT 纯文本电子书解析器
自动编码检测 + 智能章节拆分
"""

import os
import re
from pathlib import Path
from modules.book_parser.parser_factory import BookData

try:
    import chardet
    HAS_CHARDET = True
except ImportError:
    HAS_CHARDET = False


class TxtParser:
    """TXT 格式解析器"""

    def __init__(self, file_path: str, encoding: str = None):
        self.file_path = file_path
        self.encoding = encoding or "utf-8"

    def parse(self) -> BookData:
        """解析 TXT 文件"""
        data = BookData()
        data.format = "txt"
        data.file_path = self.file_path
        data.title = Path(self.file_path).stem

        # 1. 检测编码并读取
        raw_content, detected_enc = self._read_with_encoding_detection()

        # 2. 尝试从文件名/第一行提取书名和作者
        lines = raw_content.split('\n')
        first_line = lines[0].strip() if lines else ""
        if first_line and len(first_line) < 100:
            data.title = first_line
        elif len(first_line) >= 100:
            # 首行是超长正文（无独立书名行）: 回退启发式取正文开头做书名,
            # 避免退化为内部 UUID 存储文件名 (PY-105)
            data.title = self._derive_title(first_line)
        else:
            stem = Path(self.file_path).stem
            data.title = stem if stem else "未命名"

        # 3. 整本书作为一个章节（后续由 ChapterDetector 拆分）
        data.chapters = [{
            "title": data.title,
            "index": 0,
            "content": raw_content,
            "char_count": len(raw_content),
        }]

        # 4. 检测语言
        data.language = self._detect_language(raw_content[:1000])
        data.total_chars = len(raw_content)

        return data

    @staticmethod
    def _derive_title(first_line: str) -> str:
        """
        首行超长时的书名回退启发 (PY-105)：
        取正文开头最多 30 字，若在前 30 字内遇到标点且切点足够长(>=6 字)则在该处截断。
        """
        head = first_line[:30]
        m = re.search(r'[。！？!?；;，、,]', head)
        if m and m.start() >= 6:
            head = head[:m.start()]
        return head.strip() or "未命名"

    def _read_with_encoding_detection(self) -> tuple:
        """
        自动检测编码并读取文件

        Returns:
            (解码后的文本, 使用的编码)
        """
        # 先读取原始字节
        with open(self.file_path, 'rb') as f:
            raw_bytes = f.read()

        if not raw_bytes:
            return "", "utf-8"

        # 0. 如果用户显式指定了编码，优先尝试
        if self.encoding and self.encoding.lower() not in ("auto", "utf-8"):
            try:
                return raw_bytes.decode(self.encoding, errors='replace'), self.encoding
            except (UnicodeDecodeError, LookupError):
                pass  # 指定的编码失败，继续自动检测

        # 1. BOM 检测
        if raw_bytes[:3] == b'\xef\xbb\xbf':
            self.encoding = "utf-8-sig"
            return raw_bytes.decode('utf-8-sig', errors='replace'), self.encoding
        elif raw_bytes[:2] == b'\xff\xfe':
            self.encoding = "utf-16-le"
            return raw_bytes.decode('utf-16-le', errors='replace'), self.encoding
        elif raw_bytes[:2] == b'\xfe\xff':
            self.encoding = "utf-16-be"
            return raw_bytes.decode('utf-16-be', errors='replace'), self.encoding

        # 2. chardet 自动检测
        detected_enc = None
        if HAS_CHARDET:
            result = chardet.detect(raw_bytes[:min(len(raw_bytes), 200000)])
            detected_enc = result.get('encoding', None)
            confidence = result.get('confidence', 0)

            if confidence > 0.3 and detected_enc:
                # 标准化编码名
                enc = self._normalize_encoding(detected_enc)
                try:
                    text = raw_bytes.decode(enc, errors='replace')
                    self.encoding = enc
                    return text, enc
                except (UnicodeDecodeError, LookupError):
                    pass

        # 3. 回退链: 尝试常见中文编码
        fallbacks = ['utf-8', 'gbk', 'gb18030', 'gb2312', 'big5', 'big5hkscs',
                     'shift_jis', 'euc-jp', 'euc-kr', 'latin-1']
        # 如果 chardet 检测到了编码且不在回退列表中，加入列表
        if detected_enc and detected_enc.lower() not in [e.lower() for e in fallbacks]:
            norm_enc = self._normalize_encoding(detected_enc)
            if norm_enc not in fallbacks:
                fallbacks.insert(0, norm_enc)

        for enc in fallbacks:
            try:
                text = raw_bytes.decode(enc)
                self.encoding = enc
                return text, enc
            except (UnicodeDecodeError, LookupError):
                continue

        # 4. 终极回退
        self.encoding = "utf-8"
        return raw_bytes.decode('utf-8', errors='replace'), "utf-8"

    def _normalize_encoding(self, enc: str) -> str:
        """标准化编码名称"""
        enc = enc.lower().replace('-', '').replace('_', '')
        mapping = {
            'utf8': 'utf-8', 'utf-8': 'utf-8',
            'gbk': 'gbk', 'gb2312': 'gb2312', 'gb18030': 'gb18030',
            'big5': 'big5', 'big5hkscs': 'big5hkscs',
            'shiftjis': 'shift_jis', 'eucjp': 'euc-jp',
            'ascii': 'ascii', 'latin1': 'latin-1',
            'utf16': 'utf-16', 'utf16le': 'utf-16-le', 'utf16be': 'utf-16-be',
            'cp936': 'gbk', 'cp950': 'big5',
        }
        return mapping.get(enc, enc)

    def _detect_language(self, text: str) -> str:
        """简单语言检测"""
        chinese_chars = len(re.findall(r'[\u4e00-\u9fff]', text))
        if chinese_chars > len(text) * 0.3:
            return "zh"
        # 日文检测
        japanese_chars = len(re.findall(r'[\u3040-\u309f\u30a0-\u30ff]', text))
        if japanese_chars > len(text) * 0.1:
            return "ja"
        return "en"