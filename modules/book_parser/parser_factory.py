"""
电子书解析器工厂
根据文件扩展名自动选择合适的解析器
"""

import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class BookData:
    """解析后的电子书数据容器"""

    def __init__(self):
        self.title: str = ""
        self.author: str = ""
        self.language: str = "zh"
        self.format: str = ""
        self.file_path: str = ""
        self.cover_path: Optional[str] = None
        self.total_chars: int = 0
        self.chapters: list = []  # [{title, index, content, char_count}]
        self.metadata: dict = {}

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "author": self.author,
            "language": self.language,
            "format": self.format,
            "file_path": self.file_path,
            "cover_path": self.cover_path,
            "total_chars": self.total_chars,
            "chapter_count": len(self.chapters),
            "metadata": self.metadata,
        }


class ParserFactory:
    """解析器工厂：根据文件扩展名选择解析器"""

    # 注意: .chm 无解析器实现, 已从支持列表移除 (PY-102);
    # 如后续实现 chm_parser.py 再恢复声明
    FORMAT_MAP = {
        ".epub": "epub",
        ".mobi": "mobi",
        ".azw3": "mobi",  # AZW3 复用 MOBI 解析逻辑
        ".pdf": "pdf",
        ".txt": "txt",
        ".md": "md",
        ".html": "html",
        ".htm": "html",
        ".docx": "docx",
    }

    @classmethod
    def get_parser(cls, file_path: str, encoding: str = None):
        """根据文件路径获取对应的解析器
        返回 None 表示格式不支持（调用方需自行处理）。
        """
        ext = Path(file_path).suffix.lower()
        fmt = cls.FORMAT_MAP.get(ext)

        if fmt is None:
            logger.info(f"[ParserFactory] 不支持的文件格式: {ext}")
            return None
        if fmt == "epub":
            from modules.book_parser.parsers.epub_parser import EpubParser
            return EpubParser(file_path)
        elif fmt == "mobi":
            from modules.book_parser.parsers.mobi_parser import MobiParser
            return MobiParser(file_path)
        elif fmt == "pdf":
            from modules.book_parser.parsers.pdf_parser import PdfParser
            return PdfParser(file_path)
        elif fmt == "txt":
            from modules.book_parser.parsers.txt_parser import TxtParser
            return TxtParser(file_path, encoding=encoding)
        elif fmt == "md":
            from modules.book_parser.parsers.md_parser import MarkdownParser
            return MarkdownParser(file_path, encoding=encoding)
        elif fmt == "html":
            from modules.book_parser.parsers.html_parser import HtmlParser
            return HtmlParser(file_path)
        elif fmt == "docx":
            from modules.book_parser.parsers.docx_parser import DocxParser
            return DocxParser(file_path)
        return None

    @classmethod
    def parse(cls, file_path: str, use_llm: bool = False, use_mineru: bool = False,
              encoding: str = None) -> BookData:
        """一站式解析入口：自动检测格式 → 解析 → 检测章节 → 清洗"""
        logger.info(f"[ParserFactory] 开始解析: {file_path}")
        # MinerU 解析（仅 PDF 和 Office 格式）
        if use_mineru:
            ext = Path(file_path).suffix.lower()
            if ext in (".pdf", ".docx", ".pptx", ".xlsx"):
                try:
                    book_data = cls._parse_with_mineru(file_path)
                    if book_data is not None:
                        return book_data
                except Exception as e:
                    logger.warning(f"[ParserFactory] MinerU 解析失败，回退到本地解析: {e}")

        parser = cls.get_parser(file_path, encoding=encoding)
        if parser is None:
            raise ValueError(
                f"不支持的文件格式: {Path(file_path).suffix.lower()} "
                f"（支持: {', '.join(cls.FORMAT_MAP.keys())}）"
            )

        # 1. 解析文件（捕获解析器的结构化错误）
        try:
            book_data = parser.parse()
        except (RuntimeError, ValueError, ImportError):
            raise  # 直接传递用户友好的异常
        except Exception as e:
            logger.exception(f"[ParserFactory] 解析器执行失败: {type(e).__name__}")
            raise RuntimeError(f"解析 {Path(file_path).name} 时发生未知错误: {e}") from e

        # 2. 智能章节检测 + 清洗
        # 关键决策：解析器已经按 TOC 分好章节（>1章）时，不再用 ChapterDetector 二次拆分
        # 二次拆分会把已有章节标题替换成 "正文"，导致内容丢失
        # 只有解析器只返回了 1 章（整本书）时，才需要 ChapterDetector 来分章
        from modules.book_parser.chapter_detector import ChapterDetector
        from modules.book_parser.text_cleaner import TextCleaner

        cleaner = TextCleaner()
        detected_chapters = []

        if len(book_data.chapters) > 1:
            # 解析器已分好章节 → 只清洗，不拆分
            for ch in book_data.chapters:
                cleaned_content = cleaner.clean(ch["content"])
                if not cleaned_content or len(cleaned_content.strip()) < 20:
                    continue
                detected_chapters.append({
                    "title": ch.get("title", f"章节 {len(detected_chapters) + 1}"),
                    "index": len(detected_chapters),
                    "content": cleaned_content,
                    "char_count": len(cleaned_content),
                })
        else:
            # 解析器只返回了 1 章（整本书） → 用 ChapterDetector 智能分章
            full_text = "\n\n".join(ch["content"] for ch in book_data.chapters)
            if not full_text or len(full_text.strip()) < 20:
                pass  # 空内容，跳过
            else:
                sub_chapters = ChapterDetector(full_text).detect(min_content_length=50)
                if sub_chapters and len(sub_chapters) > 1:
                    for sub in sub_chapters:
                        cleaned_content = cleaner.clean(sub["content"])
                        if not cleaned_content or len(cleaned_content.strip()) < 20:
                            continue
                        detected_chapters.append({
                            "title": sub["title"],
                            "index": len(detected_chapters),
                            "content": cleaned_content,
                            "char_count": len(cleaned_content),
                        })
                else:
                    # ChapterDetector 也没找到分章点 → 保留原章
                    cleaned_content = cleaner.clean(full_text)
                    if cleaned_content and len(cleaned_content.strip()) >= 20:
                        detected_chapters.append({
                            "title": book_data.chapters[0].get("title", "正文") if book_data.chapters else "正文",
                            "index": 0,
                            "content": cleaned_content,
                            "char_count": len(cleaned_content),
                        })

        # 3. LLM 增强
        if use_llm and len(detected_chapters) < 2:
            try:
                full_text = "\n\n".join(ch["content"] for ch in detected_chapters) if detected_chapters else (book_data.chapters[0]["content"] if book_data.chapters else "")
                if full_text and len(full_text) > 200:
                    from modules.llm_client.llm_service import LLMService
                    llm = LLMService()
                    if llm.is_ready():
                        llm_result = llm.smart_chapter_detect(full_text)
                        llm_chapters = llm_result.value
                        if not llm_chapters:
                            logger.warning(
                                f"[ParserFactory] LLM 章节增强无产出，沿用正则结果: {llm_result.reason}")
                        if llm_chapters and len(llm_chapters) > 1:
                            llm_detected = []
                            for i, lch in enumerate(llm_chapters):
                                llm_detected.append({
                                    "title": lch.get("title", f"章节 {i+1}"),
                                    "index": i, "content": "", "char_count": 0,
                                })
                            if llm_detected:
                                detected_chapters = llm_detected
            except Exception as e:
                logger.warning(f"[ParserFactory] LLM 增强跳过（沿用正则结果）: {type(e).__name__}: {e}")

        book_data.chapters = detected_chapters
        book_data.total_chars = sum(ch["char_count"] for ch in detected_chapters)

        if not book_data.chapters:
            raise RuntimeError(
                f"解析 {Path(file_path).name} 后未找到任何可读章节。\n"
                "  - 文件可能为空、加密或格式不标准\n"
                "  - EPUB/MOBI 请先用 Calibre 重新转换后再导入"
            )

        logger.info(f"[ParserFactory] 解析完成: {book_data.title} ({len(book_data.chapters)} 章, {book_data.total_chars} 字)")
        return book_data

    @classmethod
    def _parse_with_mineru(cls, file_path: str) -> Optional[BookData]:
        """使用 MinerU API 解析文件"""
        from modules.mineru_tools.mineru_api_client import MinerUApiClient

        client = MinerUApiClient()

        # 健康检查
        try:
            client.health_check()
        except Exception:
            raise ConnectionError("MinerU 服务未运行")

        # 提交并等待结果
        result = client.submit_and_wait(file_path, max_wait=300.0)

        # 解析结果
        book_data = BookData()
        book_data.file_path = file_path
        book_data.format = Path(file_path).suffix.lower().lstrip(".")

        # 从 MinerU 结果提取内容
        if "pages" in result:
            # 合并所有页面的文本
            full_content = []
            for page in result["pages"]:
                if "text" in page:
                    full_content.append(page["text"])

            text = "\n\n".join(full_content)

            # 智能章节检测
            from modules.book_parser.chapter_detector import ChapterDetector
            chapters = ChapterDetector(text).detect()

            if chapters:
                book_data.chapters = [
                    {
                        "title": ch["title"],
                        "index": i,
                        "content": ch["content"],
                        "char_count": ch["char_count"],
                    }
                    for i, ch in enumerate(chapters)
                ]
            else:
                book_data.chapters = [
                    {
                        "title": "全文",
                        "index": 0,
                        "content": text,
                        "char_count": len(text),
                    }
                ]
        elif "markdown" in result:
            # Markdown 格式结果
            md = result["markdown"]
            text = md if isinstance(md, str) else md.get("content", str(md))

            from modules.book_parser.chapter_detector import ChapterDetector
            chapters = ChapterDetector(text).detect()
            if chapters:
                book_data.chapters = [
                    {
                        "title": ch["title"],
                        "index": i,
                        "content": ch["content"],
                        "char_count": ch["char_count"],
                    }
                    for i, ch in enumerate(chapters)
                ]
            else:
                book_data.chapters = [
                    {"title": "全文", "index": 0, "content": text, "char_count": len(text)}
                ]
        else:
            # 尝试从 content_list 或 text_content 获取
            text = result.get("text_content") or result.get("content", "")
            if isinstance(text, list):
                text = "\n".join(text)

            book_data.chapters = [
                {"title": "全文", "index": 0, "content": str(text), "char_count": len(str(text))}
            ]

        book_data.title = Path(file_path).stem
        book_data.total_chars = sum(ch["char_count"] for ch in book_data.chapters)

        return book_data
