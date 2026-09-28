"""
智能章节检测器
基于正则表达式模式匹配，自动识别中英文章节标题

分章粒度裁定 (PY-203, 2026-09-20 智能体2 留痕):
- **保留章级合并**: _merge_close_titles 把间距<=1 行的相邻标题合并进第一个标题,
  即 `第一节/第X节` 等节级标题不单独成章, 而是作为所属章内容的一部分。
- 理由: 有声书合成以"章"为进度/重试/音色分段单元, 节级拆分会产出大量 <500 字
  微章拖慢 P7~P9 并打散段落韵律; 多人有声书 spec (P5/P6 复用) 同样按章分段。
- 代价: 章内节边界对后期分段编辑不可见, 需在前端按正文空行手动切分, 属可接受。
"""

import re


def is_copyright_artifact(content: str) -> bool:
    """
    版权页/目录残行伪章判定 (PY-201/PY-202)。
    真实案例(盐战 全二册 MOBI): 目录尾行(如"四十一""八十")被当成章标题,
    其后紧跟的"返回总目录/版权信息/ISBN"前言块成为 75~116 字的伪章。
    保守标准——同时满足才算:
      a) 整块内容 <= 250 字;
      b) 含"返回总目录", 或同时含"版权信息"与"ISBN"。
    正常正文/章内提及 ISBN 的长段落不会命中。
    """
    if not content or len(content) > 250:
        return False
    return ("返回总目录" in content
            or ("版权信息" in content and "ISBN" in content))


class ChapterDetector:
    """智能章节检测器"""

    # 中文章节标题匹配模式（按优先级排列）
    PATTERNS = [
        # 标准章节
        r'^第[零一二三四五六七八九十百千万\d]+章\s*[^\n]*',      # 第一章 xxx
        r'^第[零一二三四五六七八九十百千万\d]+节\s*[^\n]*',      # 第一节 xxx
        r'^第[零一二三四五六七八九十百千万\d]+回\s*[^\n]*',      # 第一回 xxx
        r'^第[零一二三四五六七八九十百千万\d]+卷\s*[^\n]*',      # 第一卷 xxx

        # 数字序号
        r'^[零一二三四五六七八九十百千万\d]+[、，,.\s]+[^\n]{1,40}$',   # 一、xxx
        r'^第[零一二三四五六七八九十百千万\d]+[、，,.\s]+[^\n]{1,40}$', # 第一、xxx

        # 卷/部/篇/编
        r'^卷[零一二三四五六七八九十百千万\d]+\s*[^\n]*',
        r'^第[零一二三四五六七八九十百千万\d]+部\s*[^\n]*',
        r'^第[零一二三四五六七八九十百千万\d]+篇\s*[^\n]*',
        r'^第[零一二三四五六七八九十百千万\d]+分?编\s*[^\n]*',

        # 序言/前言/楔子/尾声/后记/附录
        r'^序[言章]?\s*$',
        r'^前言\s*$',
        r'^楔子\s*$',
        r'^引[子言]\s*$',
        r'^尾声\s*$',
        r'^后记\s*$',
        r'^[跋附]+\s*$',
        r'^附录[^\n]*$',
        r'^番外[^\n]*$',
        r'^终章[^\n]*$',

        # 英文模式
        r'^Chapter\s+\d+[^\n]*',
        r'^Part\s+\d+[^\n]*',
        r'^Volume\s+\d+[^\n]*',
        r'^Book\s+\d+[^\n]*',
        r'^Act\s+\d+[^\n]*',
        r'^Section\s+\d+[^\n]*',
        r'^Prologue\s*$',
        r'^Epilogue\s*$',

        # 章节分隔符
        r'^[=－\-—]{3,}\s*[^\n]*',   # === 标题 ===
        r'^[#]{1,3}\s+[^\n]{1,40}$',  # ### 标题 (Markdown 风格)
    ]

    # 编译正则
    COMPILED = [re.compile(p) for p in PATTERNS]

    # 单独中文数字标题支持（如网文常用 "一" "二" "三" 做章名）
    CN_DIGITS = {'零': 0, '一': 1, '二': 2, '两': 2, '三': 3, '四': 4,
                 '五': 5, '六': 6, '七': 7, '八': 8, '九': 9}
    CN_UNITS = {'十': 10, '百': 100, '千': 1000}

    @classmethod
    def _cn_to_int(cls, s: str):
        """'三'->3, '二十一'->21, '一百零五'->105；非纯中文数字返回 None"""
        if not s or len(s) > 6:
            return None
        total = num = 0
        for ch in s:
            if ch in cls.CN_DIGITS:
                num = cls.CN_DIGITS[ch]
            elif ch in cls.CN_UNITS:
                total += (num if num else 1) * cls.CN_UNITS[ch]
                num = 0
            else:
                return None
        value = total + num
        return value if value > 0 else None

    def __init__(self, text: str):
        self.text = text
        self.lines = text.split('\n')

    def detect(self, min_content_length: int = 3) -> list:
        """
        检测章节并返回列表
        [{"title": str, "index": int, "content": str, "char_count": int}, ...]
        """
        # 步骤0: 剔除"标题行+纯页码行"重复出现的目录块（PDF/书前目录页噪声）
        self._strip_toc_blocks()

        # 步骤1: 找出所有疑似章节标题行
        chapter_starts = self._find_chapter_starts()

        if not chapter_starts:
            # 没有检测到章节，整个文本作为一个章节
            return [{
                "title": "正文",
                "index": 0,
                "content": self.text.strip(),
                "char_count": len(self.text.strip()),
            }]

        # 步骤2: 合并过近的标题（连续的标题行只保留第一个）
        chapter_starts = self._merge_close_titles(chapter_starts)

        # 步骤3: 构建章节区间
        chapters = []
        for idx, (line_num, title) in enumerate(chapter_starts):
            # 内容起始行（跳过标题行本身）
            start_line = line_num + 1
            # 内容结束行（下一个章节标题前）
            if idx + 1 < len(chapter_starts):
                end_line = chapter_starts[idx + 1][0]
            else:
                end_line = len(self.lines)

            content = '\n'.join(self.lines[start_line:end_line]).strip()

            # 过滤版权页/目录残行伪章 (PY-201/PY-202, 保守判定见 is_copyright_artifact)
            if is_copyright_artifact(content):
                continue

            # 过滤内容过少的伪章节
            if len(content) < min_content_length:
                continue

            chapters.append({
                "title": title.strip()[:100],
                "index": len(chapters),
                "content": content,
                "char_count": len(content),
            })

        # 步骤4: 如果检测结果太少（< 2章），整个文本作为一个章节
        if len(chapters) < 2 and len(self.text) > 5000:
            return [{
                "title": "正文",
                "index": 0,
                "content": self.text.strip(),
                "char_count": len(self.text.strip()),
            }]

        return chapters

    def _find_chapter_starts(self) -> list:
        """找出所有疑似章节标题行 [(line_index, title), ...]"""
        starts = []
        for i, line in enumerate(self.lines):
            line = line.strip()
            if not line or len(line) > 80:  # 太长的行不可能是标题
                continue

            for pattern in self.COMPILED:
                if pattern.match(line):
                    starts.append((i, line))
                    break

        # 追加"一/二/三"式纯中文数字标题（须构成递增序列，防诗歌等散点误伤）
        starts.extend(self._find_numeric_heading_lines())
        starts.sort()
        return starts

    def _find_numeric_heading_lines(self, min_run: int = 5) -> list:
        """识别整体为中文数字的独立标题行，只采纳数值 +1 连续且长度 >= min_run 的段"""
        candidates = []
        for i, line in enumerate(self.lines):
            s = line.strip()
            if s and len(s) <= 4 and all(ch in self.CN_DIGITS or ch in self.CN_UNITS for ch in s):
                v = self._cn_to_int(s)
                if v:
                    candidates.append((i, s, v))

        adopted = []
        run = []
        for c in candidates:
            if run and c[2] == run[-1][2] + 1:
                run.append(c)
            else:
                if len(run) >= min_run:
                    adopted.extend(run)
                run = [c]
        if len(run) >= min_run:
            adopted.extend(run)
        return [(i, s) for i, s, v in adopted]

    def _strip_toc_blocks(self) -> None:
        """
        剔除目录块：连续(间距<=4行)的标题行团簇中，若 >=80% 的标题后 2 行内
        紧跟纯数字页码行且页码递增，则删除这些标题行与页码行。
        """
        lines = self.lines
        heading_idx = [
            i for i, l in enumerate(lines)
            if l.strip() and len(l.strip()) <= 80
            and any(p.match(l.strip()) for p in self.COMPILED)
        ]
        if len(heading_idx) < 6:
            return

        keep = [True] * len(lines)
        removed_any = False
        group = []
        groups = []
        for h in heading_idx:
            if group and h - group[-1] > 6:
                groups.append(group)
                group = []
            group.append(h)
        groups.append(group)

        for grp in groups:
            if len(grp) < 6:
                continue
            page_nums = []
            marked = []
            prev_val = 0
            for h in grp:
                val = self._followed_by_page_number(lines, h)
                if val is not None and val >= prev_val:
                    marked.append(h)
                    page_nums.append(val)
                    prev_val = val
            if len(marked) / len(grp) >= 0.8:
                for h in marked:
                    keep[h] = False
                    removed_any = True
                    for j in range(h + 1, min(len(lines), h + 3)):
                        t = lines[j].strip()
                        if t and re.fullmatch(r'\d{1,4}', t):
                            keep[j] = False
                            break

        if removed_any:
            self.lines = [l for l, k in zip(lines, keep) if k]

    @staticmethod
    def _followed_by_page_number(lines: list, i: int):
        """标题行后 2 行内是否存在纯页码行，返回该数字，否则 None"""
        for j in range(i + 1, min(len(lines), i + 3)):
            t = lines[j].strip()
            if re.fullmatch(r'\d{1,4}', t):
                return int(t)
        return None

    def _merge_close_titles(self, starts: list) -> list:
        """合并间距小于3行的连续标题 (章级合并策略, 裁定见模块 docstring / PY-203)"""
        if not starts:
            return starts

        merged = [starts[0]]
        for i in range(1, len(starts)):
            prev_line = merged[-1][0]
            curr_line = starts[i][0]

            # 如果两个标题行间距 <= 1（连续两行），认为是同一个标题的续行，跳过
            if curr_line - prev_line <= 1:
                continue
            merged.append(starts[i])

        return merged