"""
HTML 格式电子书解析器
支持 .html / .htm 文件，提取正文并按标题标签拆分章节
"""

import re
from pathlib import Path
from modules.book_parser.parser_factory import BookData


class HtmlParser:
    """HTML 格式解析器"""

    def __init__(self, file_path: str):
        self.file_path = file_path

    def parse(self) -> BookData:
        data = BookData()
        data.format = "html"
        data.file_path = self.file_path
        data.title = Path(self.file_path).stem

        try:
            from bs4 import BeautifulSoup
        except ImportError:
            data.chapters = [{"title": data.title, "index": 0, "content": "（需要安装 beautifulsoup4）", "char_count": 0}]
            return data

        with open(self.file_path, "r", encoding="utf-8", errors="replace") as f:
            html_content = f.read()

        soup = BeautifulSoup(html_content, "html.parser")

        # 提取标题
        title_tag = soup.find("title")
        if title_tag:
            data.title = title_tag.get_text(strip=True)

        # 去除脚本和样式
        for tag in soup(["script", "style", "nav", "header", "footer"]):
            tag.decompose()

        # 提取正文
        body = soup.find("body")
        if not body:
            body = soup

        # 按 h1/h2/h3 拆分章节
        chapters = self._split_by_headings(body)
        if not chapters:
            text = body.get_text()
            lines = [l.strip() for l in text.split("\n") if l.strip()]
            chapters = [{
                "title": data.title,
                "content": "\n".join(lines),
                "char_count": sum(len(l) for l in lines),
            }]

        data.chapters = []
        for i, ch in enumerate(chapters):
            data.chapters.append({
                "title": ch["title"],
                "index": i,
                "content": ch["content"],
                "char_count": ch["char_count"],
            })

        data.total_chars = sum(ch["char_count"] for ch in data.chapters)
        data.language = self._detect_language(
            data.chapters[0]["content"][:500] if data.chapters else ""
        )
        return data

    def _split_by_headings(self, soup) -> list:
        """按 HTML 标题标签拆分章节"""
        chapters = []
        current_title = ""
        current_lines = []

        for elem in soup.descendants:
            if elem.name in ("h1", "h2", "h3") and elem.get_text(strip=True):
                if current_lines and len("\n".join(current_lines)) > 30:
                    chapters.append({
                        "title": current_title or f"章节 {len(chapters) + 1}",
                        "content": "\n".join(current_lines),
                        "char_count": sum(len(l) for l in current_lines),
                    })
                current_title = elem.get_text(strip=True)
                current_lines = []
            elif elem.name is None:
                text = elem.strip()
                if text:
                    current_lines.append(text)

        if current_lines and len("\n".join(current_lines)) > 30:
            chapters.append({
                "title": current_title or f"章节 {len(chapters) + 1}",
                "content": "\n".join(current_lines),
                "char_count": sum(len(l) for l in current_lines),
            })

        return chapters if len(chapters) > 1 else (
            [{"title": "", "content": "\n".join(current_lines),
              "char_count": sum(len(l) for l in current_lines)}] if current_lines else []
        )

    def _detect_language(self, text: str) -> str:
        chinese_chars = len(re.findall(r"[\u4e00-\u9fff]", text))
        return "zh" if chinese_chars > len(text) * 0.3 else "en"