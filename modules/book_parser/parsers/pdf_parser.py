"""
PDF 电子书解析器
基于 PyMuPDF (fitz)，集成 PdfClassifier 智能分类
"""

from pathlib import Path
from modules.book_parser.parser_factory import BookData

try:
    import fitz  # PyMuPDF
    HAS_PYMUPDF = True
except ImportError:
    HAS_PYMUPDF = False


class PdfParser:
    """PDF 格式解析器"""

    def __init__(self, file_path: str):
        if not HAS_PYMUPDF:
            raise ImportError("请安装 PyMuPDF: pip install pymupdf")
        self.file_path = file_path

    def parse(self) -> BookData:
        """解析 PDF 文件"""
        data = BookData()
        data.format = "pdf"
        data.file_path = self.file_path
        data.title = ""  # 初始化为空，稍后填充

        # 1. 智能 PDF 分类
        pdf_type = self._classify_pdf()

        doc = fitz.open(self.file_path)

        # 提取元数据
        pdf_meta = doc.metadata
        if pdf_meta:
            # 优先从元数据获取标题
            data.title = pdf_meta.get("title") or ""
            data.author = pdf_meta.get("author") or ""
            data.metadata = {
                "subject": pdf_meta.get("subject", ""),
                "creator": pdf_meta.get("creator", ""),
                "producer": pdf_meta.get("producer", ""),
                "page_count": doc.page_count,
                "pdf_type": pdf_type,  # 记录 PDF 类型
            }

        # 如果元数据中没有标题，尝试从首页正文提取
        if not data.title:
            data.title = self._extract_title_from_first_page(doc)

        # 如果仍然没有标题，使用文件名作为兜底
        if not data.title:
            data.title = Path(self.file_path).stem

        # 确保标题不为空
        if not data.title:
            data.title = "Unknown Book"

        # 提取封面（第一页转为图片）
        data.cover_path = self._extract_cover(doc)

        # 2. 根据分类决定提取策略
        full_text_parts = []
        if pdf_type == "txt":
            # 文本型 PDF：直接提取文本
            for page_num in range(doc.page_count):
                page = doc[page_num]
                text = page.get_text("text")
                if text.strip():
                    full_text_parts.append(text)
        else:
            # 扫描型 PDF：尝试 OCR（如果有）或提取备注/注释
            print(f"[PDF] 扫描型 PDF ({(pdf_type)}), 尝试提取可用文本...")
            for page_num in range(doc.page_count):
                page = doc[page_num]
                # 尝试提取文本层
                text = page.get_text("text")
                if text.strip():
                    full_text_parts.append(text)
                else:
                    # 尝试提取注释/备注
                    annots = page.get_text("rawdict")
                    footnotes = [b.get("text", "") for b in annots.get("blocks", [])
                                 if b.get("type") == 0]
                    if footnotes:
                        full_text_parts.append(" ".join(footnotes))

        full_text = '\n'.join(full_text_parts)

        # 整书作为一个章节（后续由 ChapterDetector 拆分）
        data.chapters = [{
            "title": data.title,
            "index": 0,
            "content": full_text,
            "char_count": len(full_text),
        }]

        data.total_chars = len(full_text)
        data.language = self._detect_language(full_text[:1000])
        doc.close()

        return data

    def _classify_pdf(self) -> str:
        """智能 PDF 分类：判断文本型还是扫描型"""
        try:
            from modules.mineru_tools.pdf_classifier import PdfClassifier
            with open(self.file_path, "rb") as f:
                pdf_bytes = f.read()
            result = PdfClassifier.classify(pdf_bytes)
            if result in ("txt", "ocr"):
                return result
        except ImportError:
            pass  # PdfClassifier 不可用，回退
        except Exception as e:
            print(f"[PDF] 分类失败: {e}")

        # 回退：简单检测
        try:
            import fitz
            doc = fitz.open(self.file_path)
            total_text = 0
            sample_pages = min(doc.page_count, 10)
            for i in range(sample_pages):
                text = doc[i].get_text("text").strip()
                total_text += len(text)
            doc.close()
            return "txt" if total_text / sample_pages > 50 else "ocr"
        except Exception:
            return "unknown"

    def _extract_title_from_first_page(self, doc) -> str:
        """从PDF第一页提取书名（尝试识别标题）"""
        if doc.page_count == 0:
            return ""
        
        # 获取第一页
        page = doc[0]
        
        # 提取文本块
        text_blocks = page.get_text("blocks")
        if not text_blocks:
            return ""
        
        # 按位置排序文本块（从上到下）
        text_blocks.sort(key=lambda block: block[1])  # 按y坐标排序
        
        # 尝试从文本块中识别书名
        # 通常书名在第一页的顶部，字体较大
        title_candidates = []
        for block in text_blocks:
            text = block[4].strip()
            if text:
                # 简单的标题检测：长度适中，不包含太多标点
                if len(text) > 5 and len(text) < 100 and not text.startswith("http"):
                    title_candidates.append(text)
        
        # 返回第一个候选标题
        if title_candidates:
            return title_candidates[0]
        
        return ""

    def _extract_cover(self, doc) -> str:
        """提取封面图片"""
        try:
            import tempfile
            page = doc[0]
            pix = page.get_pixmap(dpi=150)
            tmp_path = tempfile.mktemp(suffix=".png", prefix="pdf_cover_")
            pix.save(tmp_path)
            return tmp_path
        except Exception:
            return ""

    def _detect_language(self, text: str) -> str:
        import re
        chinese_chars = len(re.findall(r'[\u4e00-\u9fff]', text))
        return "zh" if chinese_chars > len(text) * 0.3 else "en"