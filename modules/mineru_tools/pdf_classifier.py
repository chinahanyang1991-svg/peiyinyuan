"""
PDF 分类器
智能判断 PDF 是文本型还是扫描型(OCR)，提取自 MinerU
"""

import re
from io import BytesIO

try:
    import pypdfium2 as pdfium
    import pypdfium2.raw as pdfium_c
    HAS_PDFIUM = True
except ImportError:
    HAS_PDFIUM = False

try:
    from pypdf import PdfReader
    HAS_PYPDF = True
except ImportError:
    HAS_PYPDF = False


MAX_SAMPLE_PAGES = 10
CHARS_THRESHOLD = 50
HIGH_IMAGE_COVERAGE_THRESHOLD = 0.8
TEXT_QUALITY_MIN_CHARS = 300
TEXT_QUALITY_BAD_THRESHOLD = 0.03
UNICODE_MAP_ERROR_RATIO_THRESHOLD = 0.04
CID_FONT_USAGE_RATIO_THRESHOLD = 0.01
CID_FONT_USAGE_COUNT_THRESHOLD = 30
MAX_PAGE_ASPECT_RATIO = 10.0
SUSPICIOUS_CJK_72XX_START = 0x7280
SUSPICIOUS_CJK_72XX_END = 0x72DF
SUSPICIOUS_CJK_72XX_COUNT_THRESHOLD = 30
SUSPICIOUS_CJK_72XX_CJK_RATIO_THRESHOLD = 0.026
ASCII_PUNCT_CHARS = set("!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~")
ASCII_PUNCT_RUN_MIN_LENGTH = 4
SUSPICIOUS_ASCII_PUNCT_MIN_TEXT_CHARS = 100
SUSPICIOUS_ASCII_PUNCT_RATIO_THRESHOLD = 0.25
SUSPICIOUS_ASCII_PUNCT_RUN_RATIO_THRESHOLD = 0.10
_ALLOWED_CONTROL_CODES = {9, 10, 13}
_PRIVATE_USE_AREA_START = 0xE000
_PRIVATE_USE_AREA_END = 0xF8FF


class PdfClassifier:
    """
    PDF 分类器

    自动判断 PDF 文件类型：
    - "txt": 文本型 PDF，可直接提取文本
    - "ocr": 扫描型 PDF，需要 OCR 识别
    - "unknown": 无法判断

    用法:
        result = PdfClassifier.classify(pdf_bytes)
        # 返回 "txt" 或 "ocr"
    """

    @classmethod
    def classify(cls, pdf_bytes: bytes) -> str:
        """分类 PDF 文件"""
        if not HAS_PDFIUM or not HAS_PYPDF:
            return cls._fallback_classify(pdf_bytes)

        try:
            pdf = pdfium.PdfDocument(pdf_bytes)
            try:
                page_count = len(pdf)
                if page_count == 0:
                    return "ocr"

                page_indices = cls._get_sample_page_indices(page_count)
                if not page_indices:
                    return "ocr"

                # 检测异常页面比例
                extreme_idx, _ = cls._get_extreme_aspect_ratio(pdf, page_indices)
                if extreme_idx is not None:
                    return "ocr"

                # 收集文本样本
                text_samples = cls._collect_text_samples(pdf, page_indices)
                avg_chars = cls._avg_cleaned_chars(text_samples)
                if avg_chars < CHARS_THRESHOLD:
                    return "ocr"

                # Unicode 映射错误检测
                unicode_signal = cls._unicode_map_error_signal(text_samples)
                if unicode_signal["ratio"] >= UNICODE_MAP_ERROR_RATIO_THRESHOLD:
                    return "ocr"

                # CID 字体检测（乱码 PDF）
                cid_signal = cls._get_cid_font_signal(pdf_bytes, page_indices)
                if cls._cid_font_triggered(text_samples, cid_signal):
                    return "ocr"

                # 文本质量检测
                quality = cls._text_quality_signal(text_samples)
                if (quality["total_chars"] >= TEXT_QUALITY_MIN_CHARS
                        and quality["abnormal_ratio"] >= TEXT_QUALITY_BAD_THRESHOLD):
                    return "ocr"

                # 可疑 CJK 字符检测
                u72xx = cls._u72xx_signal(text_samples)
                if (u72xx["count"] >= SUSPICIOUS_CJK_72XX_COUNT_THRESHOLD
                        and u72xx["cjk_ratio"] >= SUSPICIOUS_CJK_72XX_CJK_RATIO_THRESHOLD):
                    return "ocr"

                # ASCII 标点密集度检测
                punct = cls._ascii_punct_signal(text_samples)
                if punct["triggered"]:
                    return "ocr"

                # 高图片覆盖率检测
                if cls._high_image_coverage(pdf, page_indices):
                    return "ocr"

            finally:
                pdf.close()

        except Exception:
            return cls._fallback_classify(pdf_bytes)

        return "txt"

    @classmethod
    def _fallback_classify(cls, pdf_bytes: bytes) -> str:
        """回退分类：纯 PyMuPDF 文本检测"""
        try:
            import fitz
            doc = fitz.open(stream=pdf_bytes, filetype="pdf")
            try:
                total_text = 0
                total_chars = 0
                for page_num in range(min(doc.page_count, MAX_SAMPLE_PAGES)):
                    page = doc[page_num]
                    text = page.get_text("text")
                    total_text += len(text.strip())
                    total_chars += len(text)

                avg = total_text / min(doc.page_count, MAX_SAMPLE_PAGES)
                return "txt" if avg >= CHARS_THRESHOLD else "ocr"
            finally:
                doc.close()
        except Exception:
            return "unknown"

    @staticmethod
    def _get_sample_page_indices(page_count: int, max_pages: int = MAX_SAMPLE_PAGES):
        """获取采样页码"""
        if page_count <= 0:
            return []
        sample_count = min(page_count, max_pages)
        if sample_count == page_count:
            return list(range(page_count))
        if sample_count == 1:
            return [0]
        indices = []
        for i in range(sample_count):
            idx = round(i * (page_count - 1) / (sample_count - 1))
            indices.append(max(0, min(page_count - 1, idx)))
        return sorted(set(indices))

    @staticmethod
    def _get_extreme_aspect_ratio(pdf, page_indices):
        """检测异常宽高比的页面"""
        for page_idx in page_indices:
            try:
                page = pdf[page_idx]
                w, h = page.get_size()
                if w <= 0 or h <= 0:
                    continue
                ratio = max(w / h, h / w)
                if ratio > MAX_PAGE_ASPECT_RATIO:
                    return page_idx, ratio
            finally:
                try:
                    page.close()
                except Exception:
                    pass
        return None, None

    @staticmethod
    def _collect_text_samples(pdf, page_indices):
        """收集抽样页文本统计"""
        samples = []
        for page_idx in page_indices:
            try:
                page = pdf[page_idx]
                text_page = page.get_textpage()
                try:
                    text = text_page.get_text_bounded()
                    char_count = text_page.count_chars()
                    null_count = replacement_count = control_count = private_count = unicode_err = 0
                    font_counts = {}

                    for ci in range(char_count):
                        code = pdfium_c.FPDFText_GetUnicode(text_page, ci)
                        if code == 0:
                            null_count += 1
                        elif code == 0xFFFD:
                            replacement_count += 1
                        elif code not in _ALLOWED_CONTROL_CODES and (code < 32 or 127 <= code <= 159):
                            control_count += 1
                        elif _PRIVATE_USE_AREA_START <= code <= _PRIVATE_USE_AREA_END:
                            private_count += 1
                        if pdfium_c.FPDFText_HasUnicodeMapError(text_page, ci):
                            unicode_err += 1

                    samples.append({
                        "page_index": page_idx,
                        "cleaned_text": re.sub(r"\s+", "", text),
                        "char_count": char_count,
                        "null_count": null_count,
                        "replacement_count": replacement_count,
                        "control_count": control_count,
                        "private_count": private_count,
                        "unicode_err": unicode_err,
                    })
                finally:
                    text_page.close()
            finally:
                try:
                    page.close()
                except Exception:
                    pass
        return samples

    @staticmethod
    def _avg_cleaned_chars(samples):
        """平均有效字符数"""
        if not samples:
            return 0.0
        total = sum(len(s["cleaned_text"]) for s in samples)
        return total / len(samples)

    @staticmethod
    def _text_quality_signal(samples):
        """文本质量信号"""
        total = null_c = repl_c = ctrl_c = priv_c = 0
        for s in samples:
            total += s["char_count"]
            null_c += s["null_count"]
            repl_c += s["replacement_count"]
            ctrl_c += s["control_count"]
            priv_c += s["private_count"]
        abnormal = null_c + repl_c + ctrl_c + priv_c
        return {"total_chars": total, "abnormal_ratio": abnormal / total if total > 0 else 0}

    @staticmethod
    def _unicode_map_error_signal(samples):
        """Unicode 映射错误信号"""
        total = errors = 0
        for s in samples:
            total += s["char_count"]
            errors += s["unicode_err"]
        return {"ratio": errors / total if total > 0 else 0}

    @staticmethod
    def _get_cid_font_signal(pdf_bytes, page_indices):
        """检测 CID 字体（乱码 PDF 特征）"""
        if not HAS_PYPDF:
            return {"triggered": False}
        try:
            reader = PdfReader(BytesIO(pdf_bytes))
            for page_idx in page_indices:
                page = reader.pages[page_idx]
                resources = page.get("/Resources")
                if not resources:
                    continue
                fonts = resources.get("/Font")
                if not fonts:
                    continue
                for font_key in fonts:
                    font = fonts[font_key]
                    if hasattr(font, "get_object"):
                        font = font.get_object()
                    subtype = str(font.get("/Subtype", ""))
                    encoding = str(font.get("/Encoding", ""))
                    has_to_unicode = "/ToUnicode" in font
                    if (subtype == "/Type0"
                            and encoding in ("/Identity-H", "/Identity-V")
                            and not has_to_unicode):
                        return {"triggered": True}
            return {"triggered": False}
        except Exception:
            return {"triggered": False}

    @staticmethod
    def _cid_font_triggered(samples, cid_signal):
        """CID 字体触发检测"""
        return cid_signal.get("triggered", False)

    @staticmethod
    def _u72xx_signal(samples):
        """U+7280-U+72DF 可疑 CJK 字符检测"""
        cjk_chars = 0
        u72xx_count = 0
        for s in samples:
            for ch in s["cleaned_text"]:
                code = ord(ch)
                if 0x4E00 <= code <= 0x9FFF:
                    cjk_chars += 1
                if SUSPICIOUS_CJK_72XX_START <= code <= SUSPICIOUS_CJK_72XX_END:
                    u72xx_count += 1
        return {
            "count": u72xx_count,
            "cjk_ratio": u72xx_count / cjk_chars if cjk_chars > 0 else 0,
        }

    @staticmethod
    def _ascii_punct_signal(samples):
        """ASCII 标点密集度检测"""
        for s in samples:
            text = s["cleaned_text"]
            if not text:
                continue
            punct_count = sum(1 for ch in text if ch in ASCII_PUNCT_CHARS)
            run_chars = 0
            current_run = 0
            for ch in text:
                if ch in ASCII_PUNCT_CHARS:
                    current_run += 1
                else:
                    if current_run >= ASCII_PUNCT_RUN_MIN_LENGTH:
                        run_chars += current_run
                    current_run = 0
            if current_run >= ASCII_PUNCT_RUN_MIN_LENGTH:
                run_chars += current_run

            if (len(text) >= SUSPICIOUS_ASCII_PUNCT_MIN_TEXT_CHARS
                    and punct_count / len(text) >= SUSPICIOUS_ASCII_PUNCT_RATIO_THRESHOLD
                    and run_chars / len(text) >= SUSPICIOUS_ASCII_PUNCT_RUN_RATIO_THRESHOLD):
                return {"triggered": True}
        return {"triggered": False}

    @staticmethod
    def _high_image_coverage(pdf, page_indices):
        """高图片覆盖率检测"""
        high_img_pages = 0
        for page_idx in page_indices:
            try:
                page = pdf[page_idx]
                bbox = page.get_bbox()
                page_area = abs((bbox[2] - bbox[0]) * (bbox[3] - bbox[1]))
                if page_area <= 0:
                    continue
                image_area = 0.0
                try:
                    for obj in page.get_objects(filter=[pdfium_c.FPDF_PAGEOBJ_IMAGE], max_depth=3):
                        try:
                            l, b, r, t = obj.get_pos()
                            image_area += max(0.0, r - l) * max(0.0, t - b)
                        finally:
                            try:
                                obj.close()
                            except Exception:
                                pass
                except Exception:
                    pass
                if image_area / page_area >= HIGH_IMAGE_COVERAGE_THRESHOLD:
                    high_img_pages += 1
            finally:
                try:
                    page.close()
                except Exception:
                    pass

        if not page_indices:
            return False
        return high_img_pages / len(page_indices) >= 0.5