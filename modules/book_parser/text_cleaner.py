"""
文本清洗器 v3
- 保守策略：只在高置信度时删除内容
- 只删除明显的广告推广行、水印行、页码/页眉页脚行
- 页码/页眉模式 (PY-106/PY-204)：仅整行匹配才删，宁漏勿误删正文
  (如"第 1 页 共 200 页""- 12 -""3""12/300")
- 保留所有正常小说正文（即使偶然含"打赏""温馨提示"等词，或正文中间含页码字样）
- clean() 设计为幂等：clean(clean(x)) == clean(x)，导入清洗后管线再清洗不产生二次损伤
"""

import re
import logging

logger = logging.getLogger(__name__)


class TextCleaner:
    """文本清洗器 - 保守策略"""

    # 严格的广告模式（每行必须**整体**匹配才删）
    # 只针对明确的推广请求话术，不要匹配普通正文
    AD_PATTERNS = [
        # 明确的求票/求订阅（整行或短句）
        r'^\s*求[票投打][月推打][票荐].{0,10}$',
        r'^\s*求.{0,6}月票.{0,6}$',
        r'^\s*求.{0,6}推荐票.{0,6}$',
        r'^\s*求.{0,6}打赏.{0,6}$',
        r'^\s*求.{0,6}订阅.{0,6}$',
        r'^\s*求.{0,6}收藏.{0,6}$',

        # 经典小说广告站点话术（短行，仅当整行就是广告时才匹配）
        # 不包括"本书由"以避免误删版权声明
        r'^.{0,10}(?:更多精彩|扫描|扫码|关注公众号|微信公众号|书友群|QQ群|加入读者群|关注我们的|打赏支持|支持作者|支持创作|催更.{0,10}|本章未完|下一页|上一页|手机阅读|电脑阅读|阅读APP|请记住.{0,10}网址).{0,20}$',
    ]

    # 页码/页眉/页脚模式 (PY-106/PY-204)：整行匹配才删，正文中间出现不受影响
    PAGE_PATTERNS = [
        # 第 1 页 / 第1页 / 第 1 页 共 200 页 / 第 4 页（共 12 页）/ 第1页/共200页
        r'^第\s*\d+\s*页(?:\s*[/,，、\-–—(（【]?\s*共\s*\d+\s*页\s*[)）】]?)?\s*$',
        r'^共\s*\d+\s*页\s*$',
        # 页码：12 / 页码 3/200
        r'^页\s*码\s*[:：]?\s*\d+(?:\s*[/,，、\-–—]\s*\d+)?\s*$',
        # 页眉式 "1 / 200" "12/300" "3/127页"
        r'^\d{1,4}\s*/\s*\d{1,5}\s*页?\s*$',
        # 页脚 "- 12 -" "— 8 —"
        r'^[-—–]\s*\d{1,4}\s*[-—–]\s*$',
        # 独立纯数字行（<=4 位）：扫描版书页码；正常正文不会整行只有数字
        r'^\d{1,4}\s*$',
    ]

    # 水印特征词：必须在一行中同时出现多个才算水印
    # 单用一个"www.""或"http://"不会整行删除
    WATERMARK_STRONG = [
        ('www.', ('.com', '.net', '.cn', '.org', '.info')),
        ('http://',),
        ('https://',),
    ]

    COMPILED_AD = [re.compile(p, re.IGNORECASE) for p in AD_PATTERNS]
    COMPILED_PAGE = [re.compile(p) for p in PAGE_PATTERNS]

    def __init__(self):
        pass

    def clean(self, text: str) -> str:
        """
        清洗文本（保守策略 v3）：
        1. 只删除明显的广告/推广/水印（短行）
        2. 删除整行页码/页眉/页脚（PAGE_PATTERNS，仅整行匹配；正文句中含数字/页码不删）
        3. 规范化空白
        4. 绝不删除正常中文正文
        5. 幂等：对已清洗文本再 clean 不产生任何二次变化（导入清洗=管线清洗安全重复）
        """
        if not text:
            return text

        lines = text.split('\n')
        cleaned_lines = []
        removed_count = 0

        for line in lines:
            stripped = line.strip()

            # 保留段落间距（但最多连续 1 个空行）
            if not stripped:
                if cleaned_lines and cleaned_lines[-1] != '':
                    cleaned_lines.append('')
                continue

            # === 0. 规范化空白（多个空格/制表符合并为一个，保证整行模式稳定命中）===
            normalized = re.sub(r'[ \t]+', ' ', stripped).strip()
            if not normalized:
                continue

            # === 1. 页码/页眉/页脚检测（整行匹配才删，PY-106/PY-204）===
            if self._is_page_noise(normalized):
                removed_count += 1
                continue

            # === 2. 强广告检测（短行 + 强特征）===
            if self._is_strong_ad(normalized):
                removed_count += 1
                continue

            # === 3. 水印检测（整行是 URL/域名，不是正文）===
            if self._is_pure_watermark(normalized):
                removed_count += 1
                continue

            cleaned_lines.append(normalized)

        # 去除首尾空行，限制连续空行为 1 个
        result = []
        prev_empty = False
        for l in cleaned_lines:
            if l == '':
                if not prev_empty:
                    result.append(l)
                prev_empty = True
            else:
                result.append(l)
                prev_empty = False

        final = '\n'.join(result).strip()
        if removed_count > 0:
            logger.debug(f"[TextCleaner] 清洗前 {len(text)} 字，清洗后 {len(final)} 字，删除 {removed_count} 行")
        return final

    def _is_page_noise(self, text: str) -> bool:
        """
        页码/页眉/页脚检测 (PY-106/PY-204)。
        保守原则：只有**整行**命中 PAGE_PATTERNS 才判为噪声，
        正文句子中出现"第 3 页""200 页"等不受影响。
        """
        if not text:
            return False
        for pattern in self.COMPILED_PAGE:
            if pattern.match(text):
                return True
        return False

    def _is_strong_ad(self, text: str) -> bool:
        """
        严格的广告检测。
        只在高度置信时返回 True，避免误伤正文。
        """
        if not text:
            return False

        # 正则严格模式（整行匹配）
        for pattern in self.COMPILED_AD:
            if pattern.match(text):
                return True

        # 短行 (<= 60 字) + 多个广告特征关键词，才判为广告
        if len(text) <= 60:
            # 至少 2 个特征才判为广告
            ad_terms = ['求票', '求月票', '求推荐票', '求打赏', '求订阅', '求收藏',
                        '推荐票', '月票', '催更', '公众号', '书友群', '读者群',
                        '首发', '整理', '扫码', '扫描', '下载APP', '更多精彩']
            hits = sum(1 for t in ad_terms if t in text)
            if hits >= 2:
                return True

            # 单强特征 + 长度 < 30
            strong_single = ['求月票', '求推荐票', '求打赏', '求订阅', '求收藏',
                             '关注公众号', '扫码', '加入书友群', '加入读者群',
                             '本章未完', '请点击下一页', '阅读提示', '温馨提示',
                             '手机阅读', '电脑阅读', '打赏支持', '支持作者']
            if len(text) <= 30 and any(s in text for s in strong_single):
                return True

        return False

    def _is_pure_watermark(self, text: str) -> bool:
        """
        判断一行是否是纯水印/URL。
        只有整行是域名/URL 或明确的推广语时才删，
        正常版权声明（如"本书由中文在线提供授权"）不删除。
        """
        if not text:
            return False

        lower = text.lower()

        # 纯 URL 行（<= 100 字且主要是 URL）
        if len(text) <= 100:
            if re.match(r'^\s*(?:https?://|www\.)[^\s]+(?:\s|$)', lower):
                return True

            # 只有在明显是推广/整理声明时才删：同时含"本书由/本文由"和"整理/发布/转载"
            if re.search(r'^(?:本书|此文|本文)由.{0,20}(?:整理|发布|制作|转载).{0,10}$', text):
                return True

            # 经典推广短行（含"关注""扫码""更多精彩"等且不含 CJK 内容/仅有少量 CJK）
            # 例："扫描二维码关注我们" "更多精彩内容请访问..."
            ad_like = ['扫描二维码', '扫码关注', '更多精彩', '微信公众号',
                        '关注我们', '微信公众号', '读者群', '加群', 'VIP 章节']
            if any(s in text for s in ad_like) and len(text) <= 30:
                return True

        return False

    def quick_clean(self, text: str) -> str:
        """快速清洗：与 clean 相同逻辑"""
        result = self.clean(text)
        # 对极端超长单行（>5000字符无换行）做简单分句处理
        lines = result.split('\n')
        processed = []
        for line in lines:
            if len(line) > 5000:
                line = re.sub(r'(?<=[。！？!?])', '\n', line)
            processed.append(line)
        return '\n'.join(processed)
