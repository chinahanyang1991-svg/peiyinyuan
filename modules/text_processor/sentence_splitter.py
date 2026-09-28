"""
多语言智能分句引擎
支持中/英/日/韩等语言的分句，优化 TTS 断句
"""

import re
from dataclasses import dataclass
from typing import Optional


# 引号保护占位符（__QUOTE_n__）：语义上是一个不可切分的原子单元
_PLACEHOLDER_RE = re.compile(r"__QUOTE_\d+__")
# 切分基本单元 = 一个完整占位符 或 单个字符（无占位符时逐字节等价于 list(text)）
_UNIT_RE = re.compile(r"__QUOTE_\d+__|[\s\S]")


def _snap_out_of_placeholder(text: str, cut: int) -> int:
    """把字符切点推到所在占位符之外，保证永不从 __QUOTE_n__ 中间切开。"""
    if cut <= 0 or cut >= len(text):
        return cut
    for m in _PLACEHOLDER_RE.finditer(text):
        if m.start() < cut < m.end():
            return m.end()
    return cut


@dataclass(frozen=True)
class SplitPolicy:
    """分句长度策略（US-B2 / M7：由引擎注入，取代散写的三个硬编码常量）。

    默认值 **逐字等价于现状**（max_len=150 / min_len=2 / max_en_len=300 /
    tail_merge=False / upper_bound=0），因此单引擎项目、未指定 policy 的调用方
    得到与改造前完全一致的分句结果。

    - max_len:     单句上限（原 MAX_SENTENCE_LENGTH）
    - min_len:     最短片段保底长度（原 MIN_SENTENCE_LENGTH）
    - max_en_len:  拉丁语系单句上限（原 MAX_EN_SENTENCE）
    - tail_merge:  _chunk_overlong 的尾巴是否无条件回并到前一片（照 cut2:135-137
                   的"最后一片不足 max_len 回并"）；False 时仅在 tail < min_len 才回并（现状）
    - upper_bound: 额外硬兜底上限（>0 生效，对应对方 TextPreprocessor 的 510 字硬切）；
                   0 表示不启用（现状：唯一防线就是 max_len）
    """
    max_len: int = 150
    min_len: int = 2
    max_en_len: int = 300
    tail_merge: bool = False
    upper_bound: int = 0


# 现状等价策略（voxcpm / 其它引擎 / 单引擎项目的默认）—— 与改造前的三个常量一致
DEFAULT_SPLIT_POLICY = SplitPolicy()
# GPT-SoVITS 档：cut0 下服务端不再逐逗号硬切，句子长度必须落在 T2S 可稳发区间（M2/M7）
GPTSOVITS_SPLIT_POLICY = SplitPolicy(max_len=45, min_len=6, max_en_len=300,
                                     tail_merge=True, upper_bound=500)


class SentenceSplitter:
    """多语言智能分句引擎"""

    # 句子末尾标点（强分割点）- 不含括号，split 时会自动包裹 []
    CN_STRONG_BREAKS = r'。！？!?…～~——'
    # 句中弱分割点（仅对超长且无句末标点的句子做二次拆分）
    CN_WEAK_BREAKS = r'，,；;、'
    # 句末标点（用于判断句子是否已有自然终止符）
    CN_SENTENCE_END = '。！？!?…'
    # 引号包裹的内容不应被拆分
    # PY-108 修复: 必须显式包含中文弯引号 U+201C/U+201D 配对，
    # 否则对白内部的 。/！？ 会被强切，导致引号破对、对白归属丢失
    QUOTE_CONTENT = re.compile(
        '\u201c[^\u201d]{0,200}\u201d'          # “……” 中文弯引号
        '|「[^」]{0,200}」'
        '|『[^』]{0,200}』'
        '|\u0022[^\u0022\\n]{0,200}\u0022'      # ASCII 直引号成对
        '|\u2018[^\u2019\\n]{0,60}\u2019'        # 弯单引号成对（限制长度避开缩写撇号）
    )
    # 英文缩写（不应在此处分句）
    ABBREVIATIONS = {
        'Mr.', 'Mrs.', 'Ms.', 'Dr.', 'Prof.', 'St.', 'Jr.', 'Sr.',
        'e.g.', 'i.e.', 'etc.', 'vs.', 'U.S.', 'U.K.',
    }

    # 类级默认 = 现状常量（供 `SentenceSplitter.MAX_SENTENCE_LENGTH` 这类
    # 直接读取，也是 DEFAULT_SPLIT_POLICY 的取值）；实例上的同名属性由注入的
    # policy 派生，保持向后兼容（run_p4_split / test_text_processor 直读 sp.*）。
    MAX_SENTENCE_LENGTH = 150   # 中文最大句子长度
    MIN_SENTENCE_LENGTH = 2     # 最小句子长度
    MAX_EN_SENTENCE = 300       # 英文最大句子长度

    def __init__(self, policy: Optional[SplitPolicy] = None):
        # 未注入 -> 现状等价策略；单引擎项目零变化 (US-B2 / M7)
        self.policy = policy or DEFAULT_SPLIT_POLICY
        self.MAX_SENTENCE_LENGTH = self.policy.max_len
        self.MIN_SENTENCE_LENGTH = self.policy.min_len
        self.MAX_EN_SENTENCE = self.policy.max_en_len

    def split(self, text: str, language: str = "zh") -> list:
        """将文本分割为句子列表"""
        if not text or not text.strip():
            return []

        lang = language.lower()
        if lang in ("zh", "ja", "ko"):
            return self._split_cjk(text)
        elif lang in ("en", "fr", "de", "es", "pt", "it", "ru"):
            return self._split_latin(text)
        else:
            return self._split_cjk(text)  # 默认按 CJK 处理

    WEAK_SPLIT_THRESHOLD = 80   # PY-107: 超过此长度且无句末标点的句子才考虑逗号弱拆
    WEAK_MERGE_TARGET = 100     # 逗号片段回并目标长度（保证 TTS 韵律单元足够长）

    def _split_cjk(self, text: str) -> list:
        """CJK（中日韩）文本分句

        PY-107 修复: 旧实现对每个句子无条件在逗号处二次拆分（36/36 句以逗号结尾，
        TTS 调用放大 36 倍且韵律破碎）。现在只在"超长(>80字)且完全无句末标点"的
        退化长句上做逗号弱拆，并把逗号片段回并到 ~100 字的韵律单元；
        普通句子仅在超过 MAX_SENTENCE_LENGTH 时按标点边界分段。
        """
        # 保护引号内的内容
        protected = {}
        def _protect(m):
            key = f"__QUOTE_{len(protected)}__"
            protected[key] = m.group(0)
            return key

        text = self.QUOTE_CONTENT.sub(_protect, text)

        # 步骤1: 按强分割点分句（换行=段落边界，也是硬分割点：
        # 一个 TTS 单元不跨段，保证多人有声书 P5 的 speaker 归属不串行）
        raw_sentences = re.split(f'(?<=[{self.CN_STRONG_BREAKS}]|\n)', text)
        raw_sentences = [s.strip() for s in raw_sentences if s.strip()]

        # 步骤2: 逐句处理（条件弱拆 + 超长兜底分段）
        result = []
        for sent in raw_sentences:
            has_end = any(c in self.CN_SENTENCE_END for c in sent)
            if (not has_end) and len(sent) > self.WEAK_SPLIT_THRESHOLD:
                # 无句末标点的超长退化长句: 逗号弱拆后回并成韵律单元
                parts = re.split(f'(?<=[{self.CN_WEAK_BREAKS}])', sent)
                parts = [p.strip() for p in parts if p.strip()]
                chunks = self._merge_to_target(parts, self.WEAK_MERGE_TARGET)
                for chunk in chunks:
                    chunk = self._normalize_tail(chunk)
                    result.extend(self._chunk_overlong(chunk))
            else:
                sent = self._normalize_tail(sent) if not has_end else sent
                result.extend(self._chunk_overlong(sent))

        # 合并过短碎片（保底）
        result = self._merge_short_parts(result, min_len=self.policy.min_len)

        # 引擎侧硬兜底：upper_bound>0 时对仍超长的片段做无标点硬切（默认 0=不启用，
        # 现状零变化；gptsovits 档 500 兜住 cut0 下我方分句被放宽后的 BERT 512 上限）
        result = self._apply_upper_bound(result)

        # 还原引号保护
        for key, val in protected.items():
            result = [s.replace(key, val) for s in result]

        return result

    def _normalize_tail(self, part: str) -> str:
        """将以逗号结尾的韵律单元归一为句号结尾（利于 TTS 收尾韵律，不丢字符）"""
        if part and part[-1] in self.CN_WEAK_BREAKS:
            return part[:-1] + '。'
        return part

    def _merge_to_target(self, parts: list, target: int) -> list:
        """把逗号片段按顺序回并到不超过 target 长度的 chunk"""
        chunks = []
        cur = ""
        for p in parts:
            if cur and len(cur) + len(p) > target:
                chunks.append(cur)
                cur = p
            else:
                cur += p
        if cur:
            chunks.append(cur)
        return chunks

    def _chunk_overlong(self, sent: str) -> list:
        """超过 max_len 的句子按弱标点边界分段，无边界则硬截断。

        切分基本单元 = 一个完整的引号保护占位符 或 单个字符。
        旧实现按字符下标硬切，max_len 变小（US-B2 的 gptsovits 档 45）后会从
        __QUOTE_n__ 中间劈开 -> 该占位符在 _split_cjk 末尾还原失败 ->
        引号内正文从成品里静默消失（实测 MIXED 语料丢 3 个汉字，属"缺字"红线）。
        无占位符时 units 逐字节等价于 list(sent)，行为与改造前完全一致。
        """
        max_len = self.policy.max_len
        min_len = self.policy.min_len
        if len(sent) <= max_len:
            return [sent]
        units = _UNIT_RE.findall(sent)
        # offsets[k] = 单元 k 的字符起点；offsets[len(units)] = len(sent)
        offsets = []
        acc = 0
        for u in units:
            offsets.append(acc)
            acc += len(u)
        offsets.append(acc)

        out = []
        start = 0                     # 单元下标（非字符下标）
        n = len(units)
        # 剩余"字符"长度（非单元数）才与 max_len 同量纲；无占位符时 offsets[i]==i
        while offsets[n] - offsets[start] > max_len:
            # 窗口 = 从 start 起、字符跨度不超过 max_len 的最长单元序列
            end = start
            while end < n and offsets[end + 1] - offsets[start] <= max_len:
                end += 1
            w = end - start
            cut = -1
            for r in range(w - 1, max_len // 2, -1):
                u = units[start + r]
                if len(u) == 1 and (u in self.CN_WEAK_BREAKS or u in self.CN_SENTENCE_END):
                    cut = r + 1
                    break
            if cut == -1:
                cut = w if w > 0 else 1
            out.append(sent[offsets[start]:offsets[start + cut]].strip())
            start += cut
        tail = sent[offsets[start]:].strip() if start < n else ""
        if tail:
            fits = bool(out) and (len(out[-1]) + len(tail) <= max_len)
            # tail_merge=True: 尾巴只要放得下就回并到前一片(cut2:135-137);
            # False(现状): 仅当尾巴短于 min_len 才回并, 否则独立成片
            if fits and (self.policy.tail_merge or len(tail) < min_len):
                out[-1] += tail
            else:
                out.append(tail)
        return [o for o in out if o]

    def _apply_upper_bound(self, parts: list) -> list:
        """upper_bound>0 时对仍超长的片段做无标点硬切（BERT 长度兜底）。
        默认 upper_bound=0 时原样返回，零变化。
        切点用 _snap_out_of_placeholder 推出占位符之外，避免硬切劈开引号。"""
        ub = self.policy.upper_bound
        if not ub or ub <= 0:
            return parts
        out = []
        for s in parts:
            while len(s) > ub:
                cut = _snap_out_of_placeholder(s, ub)
                out.append(s[:cut])
                s = s[cut:]
            if s:
                out.append(s)
        return out

    def _split_latin(self, text: str) -> list:
        """拉丁语系文本分句"""
        # 保护缩写
        for abbr in self.ABBREVIATIONS:
            placeholder = abbr.replace('.', '@DOT@')
            text = text.replace(abbr, placeholder)

        # 按句末标点 + 大写字母开头分句
        sentences = re.split(r'(?<=[.!?])\s+(?=[A-Z])', text)
        sentences = [s.strip() for s in sentences if s.strip()]

        # 还原缩写
        sentences = [s.replace('@DOT@', '.') for s in sentences]

        # 长句二次拆分
        result = []
        for sent in sentences:
            if len(sent) > self.MAX_EN_SENTENCE:
                # 在分号处拆分
                parts = re.split(r'(?<=;)\s+', sent)
                parts = [p.strip() for p in parts if p.strip()]
                result.extend(parts)
            else:
                result.append(sent)

        return result

    def _merge_short_parts(self, parts: list, min_len: int = 2) -> list:
        """合并过短的句子片段"""
        if not parts:
            return parts

        merged = []
        buffer = ""

        for part in parts:
            if len(part) < min_len:
                buffer += part
            else:
                if buffer:
                    merged.append(buffer + part)
                    buffer = ""
                else:
                    merged.append(part)

        if buffer:
            if merged:
                merged[-1] += buffer
            else:
                merged.append(buffer)

        return merged
