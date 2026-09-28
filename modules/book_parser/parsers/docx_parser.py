"""
DOCX 电子书解析器
基于 MinerU OfficeParser（底层使用 python-docx）

修复 (WF-14):
    导入链路里文件已被 backend 用 book_id 重命名保存，`Path(file_path).stem` 于是
    把 UUID 当成书名写进 Book.title，书籍列表/成品文件名/m4b 元数据全部显示 UUID；
    core.author 为 "python-docx" 时也被原样采信。现在：
      - 书名回退顺序 = 文档核心属性标题 -> Title 段 -> 首个 Heading -> 第一段
        -> 上传原始文件名 -> 非 UUID 的落盘名；仍无结果则留空，由 backend 用上传名
        兜底（绝不把落盘 UUID 写进 Book.title）;
      - 作者过滤掉 python-docx 这类工具默认值，退回"最后修改者"，都没有则留空;
      - 构造参数 original_name 供调用方传入上传时的真实文件名（可选）。
"""

from pathlib import Path
from modules.book_parser.parser_factory import BookData


class DocxParser:
    """DOCX 格式解析器"""

    def __init__(self, file_path: str, original_name: str = ""):
        self.file_path = file_path
        # 用户上传时的原始文件名（可选）。拿不到时见 _guess_original_name 的启发式。
        self.original_name = (original_name or "").strip()

    def _guess_original_name(self) -> str:
        """调用方没传原始文件名时的启发式：落盘名不是 UUID 就当它就是原名。

        直接解析本地文件（脚本/测试/手工导入）时文件名是有意义的书名；
        走 backend 导入链路时落盘名是 UUID，此时只能靠文档内容兜底。
        """
        from modules.mineru_tools.office_parser import looks_like_uuid

        name = Path(self.file_path).name
        stem = Path(self.file_path).stem
        if name and stem and not looks_like_uuid(stem):
            return name
        return ""

    def parse(self) -> BookData:
        """解析 DOCX 文件"""
        data = BookData()
        data.format = "docx"
        data.file_path = self.file_path

        original_name = self.original_name or self._guess_original_name()
        # WF-14: 不再用落盘名（导入链路里是 UUID）预置书名；OfficeParser 解析不出可信
        # 书名时保持为空，让 backend import_book 的 `book_data.title or 上传名 stem`
        # 兜底真正生效，UUID 不会进 Book.title。
        data.title = ""

        from modules.mineru_tools.office_parser import OfficeParser

        result = OfficeParser.parse_docx(self.file_path, original_name=original_name)
        if "error" in result:
            raise RuntimeError(result["error"])

        # 从 OfficeParser 结果填充 BookData（title/author 已按 WF-14 回退链清洗）
        data.title = (result.get("title") or "").strip()
        data.metadata = result.get("metadata", {})
        data.author = result.get("metadata", {}).get("author", "")

        # 从章节结构生成 BookData.chapters（复用已解析结果，不再二次打开文档）
        chapters = OfficeParser.get_docx_chapters(
            self.file_path, original_name=original_name, parsed=result)
        if chapters:
            data.chapters = [
                {
                    "title": ch["title"],
                    "index": ch["index"],
                    "content": ch["content"],
                    "char_count": ch["char_count"],
                }
                for ch in chapters
            ]
        else:
            # 无标题分段时，整篇作为一个章节
            data.chapters = [{
                "title": data.title,
                "index": 0,
                "content": result["full_text"],
                "char_count": len(result["full_text"]),
            }]

        data.total_chars = sum(ch["char_count"] for ch in data.chapters)
        data.language = self._detect_language(result["full_text"][:1000])

        return data

    def _detect_language(self, text: str) -> str:
        import re
        chinese_chars = len(re.findall(r'[\u4e00-\u9fff]', text))
        return "zh" if chinese_chars > len(text) * 0.3 else "en"
