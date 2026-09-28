"""
EPUB 电子书解析器 v3 — 诊断增强版
关键修复：
1. _name_to_item 同时索引 basename，加速 href 查找
2. _walk 对所有不认识的 TOC 类型打印警告，不再静默跳过
3. 混合提取：TOC 提取后，再对比 spine 文档数，TOC 缺失的章节从 spine 补回
4. _get_text_by_href 增加详细日志，追踪每个 href 的匹配结果
5. 编码检测链更稳健
6. WF-02: 章题来自目录，但章节页首个 <h1> 与它同名 —— 正文里那一行必须剔除，
   否则配音会把章名再读一遍（样书实测 4 章各多 7 字、成品 +9.046 s）
"""

import os
import re
import tempfile
import logging
from pathlib import Path
from typing import List, Dict, Any, Optional

from modules.book_parser.parser_factory import BookData

logger = logging.getLogger(__name__)

try:
    import ebooklib
    from ebooklib import epub
    from bs4 import BeautifulSoup
    HAS_EBOOKLIB = True
except ImportError:
    HAS_EBOOKLIB = False

try:
    import chardet
    HAS_CHARDET = True
except ImportError:
    HAS_CHARDET = False


def _is_likely_garbage(text: str) -> bool:
    if not text or len(text.strip()) < 20:
        return False
    total = len(text)
    bad = sum(1 for ch in text if ord(ch) < 9 or ch == '\x0b' or ch == '\x0c'
              or (14 <= ord(ch) < 32) or ord(ch) == 127)
    if total > 0 and bad / total > 0.15:
        return True
    cjk = sum(1 for c in text if 0x4e00 <= ord(c) <= 0x9fff)
    if any('\u4e00' <= c <= '\u9fff' for c in text[:500]) and cjk < total * 0.20:
        return True
    meaningful = sum(1 for c in text if c.isalpha() or ('\u4e00' <= c <= '\u9fff'))
    if total > 0 and meaningful / total < 0.10:
        return True
    return False


# ----------------------------------------------------------------------
# WF-02: 章题回灌正文的剥离工具
#   EPUB 的章题来自目录(TOC)条目，但正文章节页的第一个节点往往还是同一个
#   <h1>章题</h1>，soup.get_text() 会把它一并读进正文 —— 配音时表现为
#   "每章开头把章名再念一遍"（样书实测 4 章各多 7 字、成品 +9.046 s）。
# ----------------------------------------------------------------------

# 所有空白类字符（含全角空格 U+3000、不换行空格、零宽字符）
_WS_CHARS = re.compile(
    r"[\s\u00a0\u1680\u180e\u2000-\u200f\u2028\u2029\u202f\u205f\u3000\ufeff]+"
)


def _normalize_heading(text: str) -> str:
    """标题比对用的归一化：NFKC（全角->半角）+ 去全部空白 + 忽略大小写。"""
    import unicodedata
    return _WS_CHARS.sub("", unicodedata.normalize("NFKC", text or "")).casefold()


def strip_leading_duplicate(content: str, title: str) -> str:
    """正文首行与章题同名（去空白比较）时剔除该行，返回去掉重复章题后的正文。

    保守策略：只处理"首个非空行"，且只在完全同名时剔除；剔除后正文为空则
    原样返回（宁可多读一遍章名，也不要产出空章节）。
    """
    if not content or not title:
        return content
    want = _normalize_heading(title)
    if not want:
        return content
    lines = content.split("\n")
    for i, line in enumerate(lines):
        if not line.strip():
            continue
        if _normalize_heading(line) == want:
            kept = lines[:i] + lines[i + 1:]
            stripped = "\n".join(kept).strip("\n").strip()
            return stripped if stripped else content
        break
    return content


class EpubParser:
    """EPUB 格式解析器"""

    def __init__(self, file_path: str):
        if not HAS_EBOOKLIB:
            raise ImportError("请先安装依赖: pip install ebooklib beautifulsoup4")
        self.file_path = file_path
        self.book = None
        self._name_to_item = {}  # 缓存 item_name → item

    def parse(self) -> BookData:
        data = BookData()
        data.format = "epub"
        data.file_path = self.file_path

        try:
            self.book = epub.read_epub(self.file_path)
        except Exception as e:
            raise RuntimeError(f"无法解析 EPUB 文件（可能损坏或加密）: {e}") from e

        # 建立 item name → item 的索引（同时索引 basename）
        doc_items = list(self.book.get_items_of_type(ebooklib.ITEM_DOCUMENT))
        logger.info(f"[EpubParser] EPUB 包含 {len(doc_items)} 个文档项")
        for item in doc_items:
            name = item.get_name()
            self._name_to_item[name] = item
            # 同时用 basename 索引（加速查找）
            basename = os.path.basename(name)
            if basename and basename != name and basename not in self._name_to_item:
                self._name_to_item[basename] = item

        logger.info(f"[EpubParser] 文档项索引: {list(self._name_to_item.keys())}")

        self._extract_metadata(data)

        try:
            data.cover_path = self._extract_cover()
        except Exception:
            data.cover_path = None

        # 章节：TOC 优先，spine 补充
        toc_chapters = self._extract_chapters_via_toc()
        spine_chapters = self._extract_chapters_via_spine()

        logger.info(f"[EpubParser] TOC 提取: {len(toc_chapters)} 章, spine 提取: {len(spine_chapters)} 章")

        # 混合策略：TOC 为主，但 spine 中有且 TOC 中没有的，也补进来
        if toc_chapters:
            chapters = list(toc_chapters)
            # 获取已覆盖的 href 集合
            covered_hrefs = set()
            for ch in toc_chapters:
                href = ch.get('_href', '')
                if href:
                    covered_hrefs.add(href.split('#')[0])

            # 补入 spine 中有但 TOC 中没有的章节
            for sc in spine_chapters:
                shref = sc.get('_href', '')
                if shref and shref.split('#')[0] not in covered_hrefs:
                    sc['index'] = len(chapters)
                    chapters.append(sc)
                    covered_hrefs.add(shref.split('#')[0])
                    logger.info(f"[EpubParser] 从 spine 补入缺失章节: {sc['title']}")

            if len(chapters) > len(toc_chapters):
                logger.info(f"[EpubParser] 混合后: {len(chapters)} 章 (TOC {len(toc_chapters)} + spine 补充 {len(chapters) - len(toc_chapters)})")
        else:
            chapters = spine_chapters

        # 过滤空/乱码章节
        data.chapters = [ch for ch in chapters if ch and ch.get('char_count', 0) > 0]

        for i, ch in enumerate(data.chapters):
            ch['index'] = i

        if data.chapters:
            data.language = self._detect_language(data.chapters[0]["content"][:500])
        data.total_chars = sum(ch["char_count"] for ch in data.chapters)

        if not data.chapters:
            raise RuntimeError(
                "EPUB 解析后未找到任何可读章节。\n"
                f"  文档项数量: {len(doc_items)}\n"
                f"  TOC 条目数: {len(getattr(self.book, 'toc', []) or [])}\n"
                "  可能原因: 文件加密、格式不标准、或内容为纯图片"
            )

        logger.info(f"[EpubParser] 最终: {len(data.chapters)} 章, {data.total_chars} 字")
        return data

    # ------------------------------------------------------------------
    # 元数据
    # ------------------------------------------------------------------
    def _extract_metadata(self, data: BookData):
        titles = self.book.get_metadata('DC', 'title')
        creators = self.book.get_metadata('DC', 'creator')
        languages = self.book.get_metadata('DC', 'language')

        data.title = titles[0][0] if titles else Path(self.file_path).stem
        data.author = creators[0][0] if creators else ""
        if languages:
            data.language = languages[0][0]

        data.metadata = {
            "publisher": self._get_meta_safe('DC', 'publisher'),
            "date": self._get_meta_safe('DC', 'date'),
            "identifier": self._get_meta_safe('DC', 'identifier'),
            "description": self._get_meta_safe('DC', 'description'),
        }

    def _get_meta_safe(self, namespace: str, name: str) -> str:
        try:
            items = self.book.get_metadata(namespace, name)
            return items[0][0] if items else ""
        except Exception:
            return ""

    # ------------------------------------------------------------------
    # 封面
    # ------------------------------------------------------------------
    def _extract_cover(self) -> Optional[str]:
        try:
            cover_id = None
            for meta in self.book.get_metadata('OPF', 'meta'):
                try:
                    if meta and len(meta) > 1 and isinstance(meta[1], dict) and meta[1].get('name') == 'cover':
                        cover_id = meta[1].get('content', '')
                        break
                except Exception:
                    continue
            if not cover_id:
                try:
                    for m in self.book.get_metadata('OPF', 'meta'):
                        try:
                            if m[0] == 'cover':
                                cover_id = m[1].get('content', '')
                                break
                        except Exception:
                            continue
                except Exception:
                    pass
            if cover_id:
                for item in self.book.get_items():
                    if getattr(item, 'id', None) == cover_id or item.get_name().endswith(cover_id):
                        return self._save_cover(item)
                for item in self.book.get_items_of_type(ebooklib.ITEM_IMAGE):
                    if cover_id in item.get_name():
                        return self._save_cover(item)
            for item in self.book.get_items_of_type(ebooklib.ITEM_IMAGE):
                if 'cover' in item.get_name().lower():
                    return self._save_cover(item)
            first_image = next(iter(self.book.get_items_of_type(ebooklib.ITEM_IMAGE)), None)
            if first_image:
                return self._save_cover(first_image)
        except Exception as e:
            logger.debug(f"封面提取失败: {e}")
        return None

    def _save_cover(self, item) -> str:
        ext = os.path.splitext(item.get_name())[1] or '.jpg'
        tmp_path = tempfile.mktemp(suffix=ext, prefix="cover_")
        with open(tmp_path, 'wb') as f:
            f.write(item.get_content())
        return tmp_path

    # ------------------------------------------------------------------
    # 章节提取：TOC 优先
    # ------------------------------------------------------------------
    def _extract_chapters_via_toc(self) -> List[Dict[str, Any]]:
        """通过 TOC 提取章节，递归遍历所有嵌套结构"""
        chapters: List[Dict[str, Any]] = []
        toc = getattr(self.book, 'toc', None)
        if not toc:
            logger.info("[EpubParser] TOC 为空")
            return chapters

        logger.info(f"[EpubParser] TOC 条目数: {len(toc)}")

        def _emit(title: str, href: str, origin: str) -> None:
            """TOC 条目 -> 章节。正文首行若与章题重复则剔除该行 (WF-02)。

            入章门槛仍按"剥离前"的原文长度判定，避免改变既有书的章节取舍；
            char_count 按剥离后的正文重算，字数守恒才对得上。
            """
            content = self._get_text_by_href(href)
            if not content or len(content.strip()) <= 20:
                logger.debug(f"[EpubParser] TOC {origin} 跳过(内容不足): {title} "
                             f"href={href} content_len={len(content) if content else 0}")
                return
            content = strip_leading_duplicate(content, title)
            chapters.append({
                "title": title.strip()[:120],
                "index": len(chapters),
                "content": content,
                "char_count": len(content),
                "_href": href,
            })

        def _walk(node, depth=0):
            if node is None:
                return

            # Link 对象
            if isinstance(node, epub.Link) or type(node).__name__ == 'Link':
                title = getattr(node, 'title', '') or ''
                href = getattr(node, 'href', '') or ''
                if title and href:
                    _emit(title, href, 'Link')
                else:
                    logger.debug(f"[EpubParser] TOC Link 缺少 title/href: title={title!r} href={href!r}")
                return

            # Section 对象
            if isinstance(node, epub.Section) or type(node).__name__ == 'Section':
                title = getattr(node, 'title', '') or ''
                href = getattr(node, 'href', '') or ''
                children = getattr(node, 'children', []) or []

                if children:
                    # 有子节点：Section 本身如果有 href 也加入，然后展开子节点
                    if title and href:
                        _emit(title, href, 'Section')
                    for ch in children:
                        _walk(ch, depth + 1)
                elif title and href:
                    _emit(title, href, 'Section')
                else:
                    logger.debug(f"[EpubParser] TOC Section 无 title/href/children: title={title!r} href={href!r} children={len(children)}")
                return

            # tuple(Section/Link, [children])
            if isinstance(node, tuple):
                if len(node) >= 1:
                    _walk(node[0], depth)
                if len(node) >= 2 and node[1]:
                    children = node[1]
                    if isinstance(children, (list, tuple)):
                        for ch in children:
                            _walk(ch, depth + 1)
                return

            # list
            if isinstance(node, list):
                for sub in node:
                    _walk(sub, depth + 1)
                return

            # 未知类型 — 打印警告
            logger.warning(
                f"[EpubParser] TOC 未知类型: {type(node).__module__}.{type(node).__name__} "
                f"depth={depth} repr={repr(node)[:200]}"
            )

        for i, item in enumerate(toc):
            logger.debug(f"[EpubParser] TOC[{i}] type={type(item).__name__} depth=0")
            _walk(item)

        logger.info(f"[EpubParser] TOC 提取完成: {len(chapters)} 章")
        return chapters

    # ------------------------------------------------------------------
    # 章节提取 fallback: spine 遍历
    # ------------------------------------------------------------------
    def _extract_chapters_via_spine(self) -> List[Dict[str, Any]]:
        """按 spine 顺序遍历文档项，提取章节"""
        chapters: List[Dict[str, Any]] = []
        try:
            doc_items = list(self.book.get_items_of_type(ebooklib.ITEM_DOCUMENT))
        except Exception:
            return chapters

        # 尝试获取 spine 顺序
        spine_order = []
        try:
            spine = getattr(self.book, 'spine', None)
            if spine:
                for item_id, _ in spine:
                    spine_order.append(item_id)
        except Exception:
            pass

        # 按 spine 顺序排列文档项
        ordered_items = []
        if spine_order:
            id_to_item = {}
            for item in doc_items:
                id_to_item[getattr(item, 'id', '')] = item
            for sid in spine_order:
                if sid in id_to_item:
                    ordered_items.append(id_to_item[sid])
            # 把 spine 中没有的也加上
            for item in doc_items:
                if item not in ordered_items:
                    ordered_items.append(item)
        else:
            ordered_items = doc_items

        for item in ordered_items:
            name = item.get_name().lower()
            if 'nav' in name or 'toc' in name:
                continue

            html_text = self._decode_html_bytes(item.get_content())
            if not html_text:
                continue

            soup = BeautifulSoup(html_text, 'html.parser')
            for tag in soup(['script', 'style', 'nav']):
                tag.decompose()

            text = soup.get_text(separator='\n', strip=True)
            lines = [l.strip() for l in text.split('\n') if l.strip()]
            content = '\n'.join(lines)
            content = re.sub(r'\n{3,}', '\n\n', content).strip()

            if len(content) < 50:
                continue
            if _is_likely_garbage(content):
                continue

            title = None
            from_heading = False
            t_tag = soup.find('h1') or soup.find('h2')
            if t_tag and t_tag.get_text(strip=True):
                title = t_tag.get_text(strip=True)
                from_heading = True
            else:
                t_tag = soup.find('title')
                if t_tag and t_tag.get_text(strip=True):
                    title = t_tag.get_text(strip=True)
                elif lines:
                    title = lines[0][:60]
                else:
                    title = f"章节 {len(chapters) + 1}"

            # WF-02: spine 路径的章题就取自正文首个标题节点，同样要把这行剔掉；
            # 但若章题本身就是"正文第一行"（lines[0] 兜底），剔了就是丢内容。
            if from_heading:
                content = strip_leading_duplicate(content, title)

            chapters.append({
                "title": title[:120],
                "index": len(chapters),
                "content": content,
                "char_count": len(content),
                "_href": item.get_name(),
            })

        return chapters

    # ------------------------------------------------------------------
    # 工具：通过 href 找 item → 解码 → 抽取文本
    # ------------------------------------------------------------------
    def _get_text_by_href(self, href: str) -> str:
        if not href:
            return ""
        base_href = href.split('#')[0]
        basename = os.path.basename(base_href)

        item = None
        # 1. 精确匹配
        if base_href in self._name_to_item:
            item = self._name_to_item[base_href]
        elif basename in self._name_to_item:
            item = self._name_to_item[basename]
        else:
            # 2. 后缀匹配
            for name, it in self._name_to_item.items():
                if name.endswith('/' + base_href) or name.endswith(base_href):
                    item = it
                    break
                if name.endswith('/' + basename) or name.endswith(basename):
                    item = it
                    break

        if item is None:
            # 3. 暴力遍历
            for it in self.book.get_items_of_type(ebooklib.ITEM_DOCUMENT):
                it_name = it.get_name()
                if it_name == base_href or it_name.endswith('/' + base_href):
                    item = it
                    break
                if it_name.endswith('/' + basename) or it_name.endswith(basename):
                    item = it
                    break

        if item is None:
            logger.warning(
                f"[EpubParser] 无法匹配 href: {href!r} "
                f"(base={base_href!r}, basename={basename!r})"
            )
            return ""

        try:
            raw = item.get_content()
        except Exception as e:
            logger.warning(f"[EpubParser] 读取 {item.get_name()} 内容失败: {e}")
            return ""

        html = self._decode_html_bytes(raw)
        if not html:
            return ""

        try:
            soup = BeautifulSoup(html, 'html.parser')
        except Exception:
            return html

        for tag in soup(['script', 'style', 'nav']):
            tag.decompose()

        text = soup.get_text(separator='\n', strip=True)
        lines = [l.strip() for l in text.split('\n') if l.strip()]
        content = '\n'.join(lines)
        content = re.sub(r'\n{3,}', '\n\n', content).strip()
        return content

    # ------------------------------------------------------------------
    # 编码检测
    # ------------------------------------------------------------------
    def _decode_html_bytes(self, raw_bytes: bytes) -> str:
        if not raw_bytes:
            return ""

        if HAS_CHARDET:
            try:
                det = chardet.detect(raw_bytes[:4096] if len(raw_bytes) > 4096 else raw_bytes)
                if det and det.get('encoding') and (det.get('confidence') or 0) >= 0.3:
                    try:
                        return raw_bytes.decode(det['encoding'], errors='replace')
                    except Exception:
                        pass
            except Exception:
                pass

        m = re.search(rb'<meta\s+[^>]*charset=["\']?([^"\'>\s]+)', raw_bytes[:4096], re.IGNORECASE)
        if m:
            enc = m.group(1).decode('ascii', errors='ignore').lower()
            try:
                return raw_bytes.decode(enc, errors='replace')
            except (UnicodeDecodeError, LookupError):
                pass

        xml_m = re.search(rb'<\?xml\s+[^>]*encoding=["\']([^"\'>\s]+)', raw_bytes[:1024], re.IGNORECASE)
        if xml_m:
            enc = xml_m.group(1).decode('ascii', errors='ignore').lower()
            try:
                return raw_bytes.decode(enc, errors='replace')
            except (UnicodeDecodeError, LookupError):
                pass

        if raw_bytes[:3] == b'\xef\xbb\xbf':
            return raw_bytes[3:].decode('utf-8', errors='replace')
        for bom, enc in [
            (b'\xff\xfe\x00\x00', 'utf-32-le'), (b'\x00\x00\xfe\xff', 'utf-32-be'),
            (b'\xff\xfe', 'utf-16-le'), (b'\xfe\xff', 'utf-16-be'),
        ]:
            if raw_bytes.startswith(bom):
                return raw_bytes[len(bom):].decode(enc, errors='replace')

        for enc in ['utf-8', 'gb18030', 'gbk', 'big5', 'shift_jis', 'latin-1']:
            try:
                return raw_bytes.decode(enc)
            except UnicodeDecodeError:
                continue

        return raw_bytes.decode('utf-8', errors='replace')

    def _detect_language(self, text: str) -> str:
        if not text:
            return "zh"
        chinese_chars = len(re.findall(r'[\u4e00-\u9fff]', text))
        return "zh" if chinese_chars > len(text) * 0.3 else "en"