"""
MOBI/AZW3 电子书解析器（乱码修复版）

问题分析（已修复）：
1. 旧版解析器 _try_mobi_lib() 的 _decode_html() 不做编码检测，硬猜导致乱码
2. mobi.extract() 异常时直接回退到二进制扫描，产出不可读文本
3. 乱码章节被直接写入数据库，没有二次校验

修复方案：
1. chardet 先做字节级编码检测 → 然后再尝试 meta charset / 固定回退
2. 乱码检测从严：CJK 比例 / 不可打印字符比例 / 长串符号
3. 解析结果返回前做一次整体质量检查，严重乱码时抛异常
4. 二进制扫描作为最后手段，且做严格乱码过滤
5. WF-03: _split_by_headings 切章时跳过标题元素自身的文本节点，正文不再回灌章名
   （样书实测：修复前 4 章各多 7 字、成品 +9.046 s）
"""

import re
import struct
import shutil
import tempfile
import logging
from pathlib import Path
from typing import Optional, Tuple, List

logger = logging.getLogger(__name__)


# ======================================================================
# 乱码检测 & 编码
# ======================================================================

def _is_likely_garbage(text: str) -> bool:
    """判断一段文本是否是乱码（二进制垃圾 / 未正确解码的压缩记录）。
    返回 True = 疑似乱码，应当拒绝入库。
    """
    if not text or len(text.strip()) < 20:
        return False  # 太短不判定，交给上层过滤

    # 指标 1：不可打印字符比例（过高 = 二进制）
    total = len(text)
    # 允许的不可打印字符：普通空白 \n\r\t；其他控制字符算可疑
    bad = 0
    for ch in text:
        o = ord(ch)
        if o < 9 or o == 11 or o == 12 or (14 <= o < 32) or o == 127:
            bad += 1
    if total > 0 and bad / total > 0.15:
        return True

    # 指标 2：CJK 字符比例（对中文电子书，低于 20% 就是异常）
    cjk = sum(1 for c in text if 0x4E00 <= ord(c) <= 0x9FFF)
    has_cjk_hint = any('\u4e00' <= c <= '\u9fff' for c in text[:500])
    if has_cjk_hint and cjk < total * 0.20:
        return True

    # 指标 3：长串无意义符号 / ASCII 垃圾（没有字母也没有 CJK）
    meaningful = sum(1 for c in text if c.isalpha() or ('\u4e00' <= c <= '\u9fff'))
    if total > 0 and meaningful / total < 0.10:
        return True

    # 指标 4：经典 MOBI artifact 长串（REPLACEMENT CHARACTER U+FFFD 过量）
    if text.count('\ufffd') > max(3, total // 500):
        return True

    return False


# ======================================================================
# WF-03: 章题回灌正文的剥离工具（与 WF-02 同类缺陷，见 epub_parser）
# ======================================================================

# 所有空白类字符（含全角空格 U+3000、不换行空格、零宽字符）
_WS_CHARS = re.compile(
    r"[\s\u00a0\u1680\u180e\u2000-\u200f\u2028\u2029\u202f\u205f\u3000\ufeff]+"
)


def _normalize_heading(text: str) -> str:
    """标题比对用的归一化：NFKC（全角->半角）+ 去全部空白 + 忽略大小写。"""
    import unicodedata
    return _WS_CHARS.sub("", unicodedata.normalize("NFKC", text or "")).casefold()


def strip_leading_duplicate(content: str, title: str) -> str:
    """正文首行与章题同名（去空白比较）时剔除该行，避免配音把章名再读一遍。

    保守策略：只看"首个非空行"，完全同名才剔除；剔除后正文为空则原样返回。
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


def _decode_html_with_chardet(raw_bytes: bytes) -> str:
    """优先 chardet → meta charset → 回退链。返回 unicode 文本。"""
    if not raw_bytes:
        return ""

    # 策略 1: chardet 字节级检测（对 MOBI 内部 HTML 最有效）
    try:
        import chardet
        det = chardet.detect(raw_bytes[:4096] if len(raw_bytes) > 4096 else raw_bytes)
        if det and det.get('encoding') and (det.get('confidence') or 0) >= 0.3:
            try:
                return raw_bytes.decode(det['encoding'], errors='replace')
            except (UnicodeDecodeError, LookupError):
                pass
    except ImportError:
        pass

    # 策略 2: <meta charset="xxx"> 声明
    match = re.search(
        rb'<meta\s+[^>]*charset=["\']?([^"\'>\s]+)',
        raw_bytes[:4096], re.IGNORECASE
    )
    if match:
        enc = match.group(1).decode('ascii', errors='ignore').lower()
        try:
            return raw_bytes.decode(enc, errors='replace')
        except (UnicodeDecodeError, LookupError):
            pass

    # 策略 3: XML 声明
    xml_match = re.search(
        rb'<\?xml\s+[^>]*encoding=["\']([^"\'>\s]+)',
        raw_bytes[:1024], re.IGNORECASE
    )
    if xml_match:
        enc = xml_match.group(1).decode('ascii', errors='ignore').lower()
        try:
            return raw_bytes.decode(enc, errors='replace')
        except (UnicodeDecodeError, LookupError):
            pass

    # 策略 4: BOM
    for bom, enc in [
        (b'\xef\xbb\xbf', 'utf-8'),
        (b'\xff\xfe\x00\x00', 'utf-32-le'),
        (b'\x00\x00\xfe\xff', 'utf-32-be'),
        (b'\xff\xfe', 'utf-16-le'),
        (b'\xfe\xff', 'utf-16-be'),
    ]:
        if raw_bytes.startswith(bom):
            return raw_bytes[len(bom):].decode(enc, errors='replace')

    # 策略 5: 编码回退链（中文电子书最常见）
    for enc in ['utf-8', 'gb18030', 'gbk', 'big5', 'shift_jis', 'utf-16', 'latin-1']:
        try:
            return raw_bytes.decode(enc)
        except UnicodeDecodeError:
            continue

    return raw_bytes.decode('utf-8', errors='replace')


# ======================================================================
# MobiParser 主体
# ======================================================================

class MobiParser:
    """MOBI/AZW3 格式解析器 - 中文编码友好版"""

    def __init__(self, file_path: str):
        self.file_path = file_path

    def parse(self) -> 'BookData':
        from modules.book_parser.parser_factory import BookData
        data = BookData()

        ext = Path(self.file_path).suffix.lower()
        data.format = ext.lstrip('.')
        data.file_path = self.file_path
        data.title = Path(self.file_path).stem

        # 先做基础文件校验
        self._validate_file()

        # 策略 1：mobi.extract 解压 + chardet 解码
        result, err_info = self._try_mobi_lib()
        if result:
            chapters, title = result
            data.chapters = chapters or []
            if title:
                data.title = title
            data.total_chars = sum(ch['char_count'] for ch in data.chapters)
            data.language = self._detect_language(data)
            self._sanity_check(data)
            return data

        # 策略 2：智能二进制扫描（仅作为最后手段 + 严格过滤）
        logger.warning(f"[MobiParser] mobi 库无法解析: {err_info}, 尝试二进制文本提取")
        content = self._extract_utf8_text_safe()
        if content and not _is_likely_garbage(content):
            data.chapters = [{
                "title": data.title,
                "index": 0, "content": content,
                "char_count": len(content),
            }]
            data.total_chars = len(content)
            data.language = 'zh' if any('\u4e00' <= c <= '\u9fff' for c in content[:1000]) else 'en'
            return data

        # 全部失败 - 分情况抛出更具体的错误信息
        raise RuntimeError(
            "无法解析 MOBI 文件内容。\n"
            f"  文件: {self.file_path}\n"
            f"  错误详情: {err_info}\n"
            "  建议:\n"
            "    1) 若这是 Kindle 电子书（.azw / .azw3 / .kfx）, 通常有 DRM 保护,\n"
            "       需要先用 Calibre + DeDRM 插件解密后再导入\n"
            "    2) 推荐方案: 安装 Calibre, 用 `ebook-convert input.mobi output.epub`\n"
            "       转换为 EPUB 格式后再导入本书库\n"
            "    3) 若文件非常旧（MobiPocket 格式）, 可尝试更新 mobi 库:\n"
            "       `pip install --upgrade mobi`"
        )

    def _validate_file(self):
        """基础文件校验"""
        p = Path(self.file_path)
        if not p.exists():
            raise RuntimeError(f"文件不存在: {self.file_path}")
        if p.stat().st_size == 0:
            raise RuntimeError("文件为空")

    # ------------------------------------------------------------------
    # 策略 1：通过 mobi 库解压
    # ------------------------------------------------------------------
    def _try_mobi_lib(self) -> Tuple[Optional[Tuple[List[dict], str]], str]:
        """返回 ( (chapters, title) | None, 错误信息 )"""
        try:
            import mobi
        except ImportError:
            return None, "mobi 库未安装（请运行: pip install mobi beautifulsoup4 chardet）"

        tempdir = None
        try:
            try:
                tempdir, filepath = mobi.extract(self.file_path)
            except Exception as e:
                return None, f"mobi.extract 失败: {type(e).__name__}: {e}"

            try:
                from bs4 import BeautifulSoup
            except ImportError:
                return None, "beautifulsoup4 未安装（请运行: pip install beautifulsoup4）"

            # 递归搜索所有 HTML 文件
            html_files = sorted(
                list(Path(tempdir).rglob("*.html")) +
                list(Path(tempdir).rglob("*.htm")) +
                list(Path(tempdir).rglob("*.xhtml"))
            )
            if not html_files:
                fp = Path(filepath)
                if fp.exists() and fp.suffix.lower() in ('.html', '.htm', '.xhtml'):
                    html_files = [fp]

            if not html_files:
                return None, f"解压后未找到 HTML 文件（tempdir: {tempdir}）"

            title = Path(self.file_path).stem
            chapters: List[dict] = []
            idx = 0

            # 从 content.opf 提取标题
            opf_files = list(Path(tempdir).rglob("content.opf"))
            for opf in opf_files:
                try:
                    opf_text = _decode_html_with_chardet(opf.read_bytes())
                    # PY-205: content.opf 是 XML 文档, 用 lxml-xml 后端解析,
                    # 避免 html.parser 对实体/命名空间/属性边界的误读; lxml 缺失时回退
                    try:
                        opf_soup = BeautifulSoup(opf_text, 'xml')
                    except Exception:
                        opf_soup = BeautifulSoup(opf_text, 'html.parser')
                    dc_title = opf_soup.find(['dc:title', 'title'])
                    if dc_title and dc_title.get_text(strip=True):
                        title = dc_title.get_text(strip=True)
                        break
                except Exception:
                    pass

            for html_file in sorted(html_files):
                raw_bytes = html_file.read_bytes()
                html_text = _decode_html_with_chardet(raw_bytes)
                soup = BeautifulSoup(html_text, 'html.parser')

                # 从 HTML title 提取
                if title == Path(self.file_path).stem:
                    title_tag = soup.find('title')
                    if title_tag and title_tag.get_text(strip=True):
                        title = title_tag.get_text(strip=True)

                for tag in soup(['script', 'style', 'nav']):
                    tag.decompose()

                # 按 h1/h2/h3 拆分
                sub_chapters = self._split_by_headings(soup)
                if sub_chapters:
                    for ch in sub_chapters:
                        if ch and not _is_likely_garbage(ch.get('content', '')):
                            ch['index'] = idx
                            idx += 1
                            chapters.append(ch)
                else:
                    text = soup.get_text(separator='\n', strip=True)
                    lines = [l.strip() for l in text.split('\n') if l.strip()]
                    clean_text = '\n'.join(lines)
                    clean_text = self._clean_content(clean_text)
                    if clean_text and len(clean_text) > 50 and not _is_likely_garbage(clean_text):
                        chapters.append({
                            "title": f"章节 {idx + 1}",
                            "index": idx,
                            "content": clean_text,
                            "char_count": len(clean_text),
                        })
                        idx += 1

            chapters = self._drop_copyright_artifacts(chapters)
            if chapters:
                return (chapters, title), ""
            return None, "mobi 库成功但未提取到可读章节（可能内容为空或全乱码）"
        finally:
            if tempdir:
                try:
                    shutil.rmtree(tempdir)
                except Exception:
                    pass

    @staticmethod
    def _drop_copyright_artifacts(chapters: List[dict]) -> List[dict]:
        """
        PY-201/PY-202: 剔除 MOBI 目录尾行/分册前言页产生的伪章。
        真实案例(盐战 全二册): 版权信息页被目录最后一个标题("四十一"/"八十")命名,
        成为 75/116 字的伪章插队。判定标准保守——同时满足才删:
          a) 整章内容 <= 250 字;
          b) 含"返回总目录", 或同时含"版权信息"与"ISBN"。
        正常正文/独立目录块不会同时命中; 已验证不影响 AZW3(赢家法则)68 章结构。
        """
        def _is_artifact(ch: dict) -> bool:
            from modules.book_parser.chapter_detector import is_copyright_artifact
            return is_copyright_artifact(ch.get("content") or "")

        kept = [ch for ch in chapters if not _is_artifact(ch)]
        dropped = len(chapters) - len(kept)
        if dropped:
            logger.info(f"[MobiParser] 剔除版权页/目录残行伪章 {dropped} 个 (PY-201/PY-202)")
        return kept

    # ------------------------------------------------------------------
    # 策略 2：安全的二进制文本提取
    # ------------------------------------------------------------------
    def _extract_utf8_text_safe(self) -> str:
        """从二进制中提取可读文本 - 只保留连续的 UTF-8 / ASCII 可读序列。"""
        try:
            raw = Path(self.file_path).read_bytes()
        except Exception:
            return ""

        if not raw:
            return ""

        # 先尝试整文件 chardet 解码，若成功直接走那个
        try:
            import chardet
            det = chardet.detect(raw[:8192])
            if det and det.get('encoding') and (det.get('confidence') or 0) >= 0.5:
                try:
                    text = raw.decode(det['encoding'], errors='replace')
                    if not _is_likely_garbage(text):
                        return text
                except Exception:
                    pass
        except ImportError:
            pass

        # 再试 UTF-8 严格解码（允许少量替换字符）
        try:
            text = raw.decode('utf-8', errors='replace')
            # 过滤掉大量 U+FFFD 的情况
            if text.count('\ufffd') < len(text) // 200:
                cleaned = re.sub(r'\s+', ' ', text).strip()
                if not _is_likely_garbage(cleaned):
                    return cleaned
        except Exception:
            pass

        return ""

    # ------------------------------------------------------------------
    # 章节拆分 & 内容清理
    # ------------------------------------------------------------------
    def _split_by_headings(self, soup) -> List[dict]:
        """按 HTML 标题标签拆分章节。

        WF-03: `target.descendants` 会先给出标题元素本身，再给出它的文本子节点；
        旧实现在同一轮迭代里把这些文本当正文回灌，于是每章开头都多读一遍章名
        （样书实测 4 章各多 7 字、成品 +9.046 s）。这里记录当前标题的文本后代
        并跳过它们；只跳过文本节点，嵌套在标题里的 h2 仍能正常开新章。
        """
        chapters = []
        current_title = ""
        current_lines = []
        heading_text_nodes = set()

        target = soup.body if soup.body else soup

        for elem in target.descendants:
            if elem.name in ('h1', 'h2', 'h3') and elem.get_text(strip=True):
                self._flush_chapter(chapters, current_title, current_lines)
                current_title = elem.get_text(strip=True)[:80]
                current_lines = []
                heading_text_nodes = {id(d) for d in elem.descendants if d.name is None}
            elif elem.name is None:
                if id(elem) in heading_text_nodes:
                    continue
                text = elem.strip()
                if text:
                    current_lines.append(text)

        self._flush_chapter(chapters, current_title, current_lines)

        # 如果 HTML 内没有 h1/h2，再用正则找 "第X章" 模式
        if len(chapters) < 2:
            full_text = soup.get_text(separator='\n', strip=True)
            chapters = self._regex_split(full_text) or chapters

        return chapters

    def _flush_chapter(self, chapters: List[dict], title: str,
                       lines: List[str]) -> None:
        """收束一章：清洗正文并入库；正文首行若与章题重复则剔除 (WF-03 兜底)。"""
        if not lines:
            return
        raw_content = '\n'.join(lines)
        raw_content = strip_leading_duplicate(raw_content, title)
        cleaned = self._clean_content(raw_content)
        if cleaned and len(cleaned) > 30:
            chapters.append({
                "title": title or f"章节 {len(chapters) + 1}",
                "content": cleaned,
                "char_count": len(cleaned),
            })

    def _regex_split(self, text: str) -> List[dict]:
        """在全文中用正则模式寻找章节标题。"""
        chapter_pattern = re.compile(
            r'(?:^|\n)\s*((?:第[零一二三四五六七八九十百千万\d]+[章回节卷篇部])|'
            r'(?:Chapter\s+\d+)|(?:Volume\s+\d+)|(?:Part\s+\d+))',
            re.IGNORECASE | re.MULTILINE
        )
        matches = list(chapter_pattern.finditer(text))
        if len(matches) < 2:
            return []

        chapters = []
        for i, match in enumerate(matches):
            start = match.end()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            content = text[start:end].strip()
            cleaned = self._clean_content(content)
            if cleaned and len(cleaned) > 50:
                chapters.append({
                    "title": match.group(1).strip()[:80],
                    "content": cleaned,
                    "char_count": len(cleaned),
                })
        return chapters if len(chapters) >= 2 else []

    def _clean_content(self, text: str) -> Optional[str]:
        if not text or len(text) < 10:
            return None

        # 去除 HTML 残留属性 / MOBI artifacts
        text = re.sub(r'height=["\'][^"\']*["\']', ' ', text)
        text = re.sub(r'width=["\'][^"\']*["\']', ' ', text)
        text = re.sub(r'<[^>]+>', ' ', text)

        # 规范化空白
        text = re.sub(r'[ \t]+', ' ', text)
        text = re.sub(r'\n\s*\n+', '\n\n', text)
        text = text.strip()

        if _is_likely_garbage(text):
            return None

        # CJK 内容必须足够多，否则拒绝
        if len(text) > 200:
            cjk_count = sum(1 for c in text if '\u4e00' <= c <= '\u9fff')
            alpha_count = sum(1 for c in text if c.isalpha())
            if alpha_count > 0 and cjk_count / alpha_count < 0.05:
                return None

        return text if text and len(text) >= 10 else None

    def _detect_language(self, data) -> str:
        sample = ""
        for ch in data.chapters[:3]:
            sample += ch['content'][:300]
            if len(sample) > 800:
                break
        cjk = sum(1 for c in sample if '\u4e00' <= c <= '\u9fff')
        return 'zh' if cjk > len(sample) * 0.3 else 'en'

    def _sanity_check(self, data) -> None:
        """解析结果健康检查 - 全乱码则抛异常，避免入库。"""
        if not data.chapters:
            raise RuntimeError("解析 MOBI 未找到任何章节，文件可能损坏或加密。")

        total = data.total_chars
        if total <= 0:
            raise RuntimeError("MOBI 解析后内容为空。")

        # 全章乱码检测
        garbage = sum(1 for ch in data.chapters if _is_likely_garbage(ch.get('content', '')))
        if garbage == len(data.chapters):
            raise RuntimeError(
                "解析结果全部疑似乱码。请检查电子书是否：\n"
                "  1) 受 Kindle DRM 保护（KFX/AZW 需先解密）\n"
                "  2) 用 Calibre 转成 EPUB 后再导入"
            )
        if garbage > 0:
            logger.warning(f"[MobiParser] {garbage}/{len(data.chapters)} 个章节疑似乱码，已过滤。")
