"""
文件类型与语言检测器
自动识别文件格式和文本语言，提取自 MinerU 的 guess_suffix_or_lang
"""

from io import BytesIO
from pathlib import Path
from zipfile import BadZipFile, ZipFile
from xml.etree import ElementTree


try:
    from magika import Magika
    HAS_MAGIKA = True
except ImportError:
    HAS_MAGIKA = False


# OOXML 格式检测常量
OOXML_ROOT_RELS = "_rels/.rels"
OOXML_CONTENT_TYPES = "[Content_Types].xml"
OOXML_PACKAGE_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
OOXML_CONTENT_TYPES_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
OOXML_OFFICE_DOCUMENT_REL = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"
)
OOXML_MAIN_CONTENT_TYPES = {
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml": "docx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml": "pptx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml": "xlsx",
}

# 支持的文件格式映射
SUPPORTED_FORMATS = {
    ".pdf": "pdf",
    ".docx": "docx",
    ".pptx": "pptx",
    ".xlsx": "xlsx",
    ".epub": "epub",
    ".mobi": "mobi",
    ".azw3": "mobi",
    ".txt": "txt",
    ".chm": "chm",
    ".html": "html",
    ".htm": "html",
    ".md": "markdown",
    ".png": "image",
    ".jpg": "image",
    ".jpeg": "image",
    ".gif": "image",
    ".bmp": "image",
    ".svg": "image",
    ".wav": "audio",
    ".mp3": "audio",
    ".flac": "audio",
}

# 可读文档格式（配音员关心的）
READABLE_FORMATS = {".pdf", ".docx", ".pptx", ".epub", ".mobi", ".azw3", ".txt", ".chm", ".html", ".htm", ".md"}


class FileTypeDetector:
    """
    文件类型与语言检测器

    用法:
        # 检测文件类型
        fmt = FileTypeDetector.detect_type("book.docx")     # "docx"
        fmt = FileTypeDetector.detect_type_bytes(byte_data)  # 自动识别

        # 检测语言
        lang = FileTypeDetector.detect_language("这是一段中文文本")  # "zh"
    """

    _magika = None

    @classmethod
    def _get_magika(cls):
        if cls._magika is None and HAS_MAGIKA:
            cls._magika = Magika()
        return cls._magika

    @classmethod
    def detect_type(cls, file_path: str) -> str:
        """根据文件路径检测类型"""
        path = Path(file_path)
        ext = path.suffix.lower()

        # 先检查扩展名
        if ext in SUPPORTED_FORMATS:
            return SUPPORTED_FORMATS[ext]

        # OOXML 子类型检测（docx/pptx/xlsx）
        ooxml = cls._detect_ooxml_subtype(path)
        if ooxml:
            return ooxml

        # Magika 深度检测
        magika = cls._get_magika()
        if magika:
            try:
                result = magika.identify_path(path)
                label = result.prediction.output.label
                # 将 Magika 标签映射到内部类型
                return cls._map_magika_label(label, ext)
            except Exception:
                pass

        return ext.lstrip(".") if ext else "unknown"

    @classmethod
    def detect_type_bytes(cls, file_bytes: bytes, file_path: str = None) -> str:
        """根据文件字节内容检测类型"""
        # OOXML 子类型检测
        ooxml = cls._detect_ooxml_subtype_bytes(file_bytes)
        if ooxml:
            return ooxml

        # Magika 检测
        magika = cls._get_magika()
        if magika:
            try:
                result = magika.identify_bytes(file_bytes)
                label = result.prediction.output.label
                ext = Path(file_path).suffix.lower() if file_path else ""
                return cls._map_magika_label(label, ext)
            except Exception:
                pass

        # 回退：检查魔数签名
        return cls._detect_by_signature(file_bytes) or "unknown"

    @classmethod
    def detect_language(cls, text: str) -> str:
        """
        检测文本语言
        返回 ISO 639-1 语言代码（如 "zh", "en", "ja"）
        """
        if not text or not text.strip():
            return "unknown"

        # 使用 Magika 检测
        magika = cls._get_magika()
        if magika:
            try:
                code = text.encode("utf-8", errors="replace")
                result = magika.identify_bytes(code)
                lang = result.prediction.output.label
                return lang if lang != "unknown" else cls._simple_lang_detect(text)
            except Exception:
                pass

        return cls._simple_lang_detect(text)

    @staticmethod
    def _detect_ooxml_subtype(path: Path) -> str | None:
        """检测 OOXML 子类型（docx/pptx/xlsx）"""
        try:
            with ZipFile(path) as pkg:
                return FileTypeDetector._ooxml_subtype_from_zip(pkg)
        except (BadZipFile, KeyError, ElementTree.ParseError, OSError):
            return None

    @staticmethod
    def _detect_ooxml_subtype_bytes(file_bytes: bytes) -> str | None:
        """从字节数据检测 OOXML 子类型"""
        try:
            with ZipFile(BytesIO(file_bytes)) as pkg:
                return FileTypeDetector._ooxml_subtype_from_zip(pkg)
        except (BadZipFile, KeyError, ElementTree.ParseError, OSError):
            return None

    @staticmethod
    def _ooxml_subtype_from_zip(pkg: ZipFile) -> str | None:
        """从 ZIP 包内结构识别 OOXML 子类型"""
        rels_root = ElementTree.fromstring(pkg.read(OOXML_ROOT_RELS))
        ct_root = ElementTree.fromstring(pkg.read(OOXML_CONTENT_TYPES))

        # 读取 Content-Type 覆盖
        overrides = {}
        for override in ct_root:
            if override.tag in (f"{{{OOXML_CONTENT_TYPES_NS}}}Override", "Override"):
                part_name = override.get("PartName", "").lstrip("/")
                content_type = override.get("ContentType", "")
                if part_name and content_type:
                    overrides[part_name] = content_type

        # 找到主文档关系目标
        for rel in rels_root:
            if rel.tag in (f"{{{OOXML_PACKAGE_REL_NS}}}Relationship", "Relationship"):
                if rel.get("Type") == OOXML_OFFICE_DOCUMENT_REL:
                    target = rel.get("Target", "").lstrip("/")
                    suffix = OOXML_MAIN_CONTENT_TYPES.get(overrides.get(target, ""))
                    if suffix:
                        return suffix
        return None

    @staticmethod
    def _detect_by_signature(file_bytes: bytes) -> str | None:
        """通过文件魔数签名检测类型"""
        if file_bytes[:4] == b"%PDF":
            return "pdf"
        if file_bytes[:4] == b"PK\x03\x04":
            return "zip"
        if file_bytes[:4] == b"\x89PNG":
            return "png"
        if file_bytes[:2] in (b"\xff\xd8",):
            return "jpg"
        if file_bytes[:4] == b"BOOK":
            return "mobi"
        if file_bytes[:4] == b"OggS":
            return "ogg"
        if file_bytes[:4] == b"RIFF":
            return "wav"
        return None

    @staticmethod
    def _simple_lang_detect(text: str) -> str:
        """简单的语言检测（不依赖 Magika 时的回退）"""
        import re
        chinese = len(re.findall(r'[\u4e00-\u9fff]', text))
        japanese = len(re.findall(r'[\u3040-\u309f\u30a0-\u30ff]', text))
        korean = len(re.findall(r'[\uac00-\ud7af]', text))
        russian = len(re.findall(r'[\u0400-\u04ff]', text))

        total = len(text.strip())
        if total == 0:
            return "unknown"

        if chinese / total > 0.2:
            return "zh"
        if japanese / total > 0.1:
            return "ja"
        if korean / total > 0.1:
            return "ko"
        if russian / total > 0.1:
            return "ru"
        # 默认检测英文
        ascii_chars = len(re.findall(r'[a-zA-Z]', text))
        if ascii_chars / total > 0.5:
            return "en"
        return "unknown"

    @staticmethod
    def _map_magika_label(label: str, ext: str) -> str:
        """将 Magika 标签映射到内部类型"""
        label_lower = label.lower()
        mapping = {
            "pdf": "pdf",
            "epub": "epub",
            "mobi": "mobi",
            "text": "txt",
            "plain": "txt",
            "html": "html",
            "markdown": "markdown",
            "docx": "docx",
            "pptx": "pptx",
            "xlsx": "xlsx",
            "png": "image",
            "jpeg": "image",
            "gif": "image",
            "bmp": "image",
            "svg": "image",
        }
        for key, value in mapping.items():
            if key in label_lower:
                return value

        # 如果 Magika 返回 "excel" 但扩展名是 xlsx
        if ext in SUPPORTED_FORMATS:
            return SUPPORTED_FORMATS[ext]
        return label_lower