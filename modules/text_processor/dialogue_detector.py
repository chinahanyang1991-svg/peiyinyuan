"""
对话角色自动识别模块
基于引号模式、提示词等特征识别对话并尝试分配角色
"""

import re
from typing import Callable, Optional


class DialogueDetector:
    """对话检测与角色识别"""

    def __init__(self, name_gate: Optional[Callable[[str], bool]] = None):
        """`name_gate` = cast 侧的判名门（词类否决 + 正向姓名形态）。

        PY-338: 引号后的**动作主语**归因通道只有在拿到这道门时才打开。默认 None
        等于保持 WF-01 之后的行为——这不是保守，而是"少传一道门就静默退回假角色
        名单"太便宜，所以让能力显式注入。
        """
        self.name_gate = name_gate

    # 说话动词列表
    SPEECH_VERBS = [
        '说', '道', '问', '回答', '答道', '笑道', '哭道', '怒道',
        '叹道', '问道', '叫道', '吼道', '低声说', '大声说',
        '回答', '大喊', '喊道', '插话道', '点点头', '摆了摆手',
        '重复', '强调', '解释', '回应',
        '笑着说', '谦虚地说', '解释道', '回应道',
        '笑着', '说着', '哭着', '喊道',
    ]

    # 中文引号对
    CN_QUOTES = [
        ('"', '"'),
        ('"', '"'),
        ('「', '」'),
        ('『', '』'),
        ('\u2018', '\u2019'),  # ' '
        ('\u201c', '\u201d'),  # " "
    ]

    # 对话提示词（冒号前是角色名）- 使用非原始字符串使 \u201c 正确转义为 Unicode
    ROLE_PROMPTS = re.compile(
        "(?:^|\n)\\s*([^\\s：:，。！？\u201c\u201d]{1,8})\\s*[：:]\\s*[\u201c\"\u2018「『](.+?)[\u201d\"\u2019」』]",
        re.DOTALL,
    )

    # 旁白/叙述标识
    NARRATION_INDICATORS = [
        r'(?:他|她|它)(?:想|说|道|问|回答|答道|笑道|哭道|怒道)',
        r'(?:只见|这时|忽然|突然|然后|接着|于是)',
        r'(?:天空中|外面|远处|地上|心中|心里)',
    ]

    # ---- WF-01: 引号"后置"提示的高精度模式 ----
    # 说话动词候选串（长动词优先，避免 "说道" 被 "说" 截断）
    _VERBS_ALT = '|'.join(sorted(map(re.escape, SPEECH_VERBS), key=len, reverse=True))

    # 紧跟引号的 "名字+说话动词+标点/边界"。
    # 只认说话动词（旧实现用泛动作字符集，把 "里头传来拖长的问话" 里的
    # 里头传来 当成角色名），且动词后必须是标点/空白/行末，防止
    # "里头传来拖长 + 问话" 这种把叙述动词当说话动词的误截。
    _SPEECH_HEAD_RE = re.compile(
        r'^\s*([\u4e00-\u9fa5A-Za-z·]{2,6}?)\s*(?:' + _VERBS_ALT + r')'
        r'\s*(?=[，。！？、,;:;!?；\s　”’」』]|$)'
    )

    # "说话的是户部的周主事，" 结构：的 之前若干字内必须出现说话动词，
    # 否则 "她的声音很轻，" 这类叙述片段会被当成角色提示。
    _SPEECH_OF_RE = re.compile(
        r'(?:' + _VERBS_ALT + r')[\u4e00-\u9fa5]{0,8}?的(?:是|了)?'
        r'([\u4e00-\u9fa5]{2,4})(?=[，。！？、,;:;!?；\s　]|$)'
    )

    def detect_dialogues(self, text: str) -> list:
        """
        检测文本中的所有对话片段
        返回:
        [{
            "type": "dialogue" | "narration",
            "content": str,
            "start": int,
            "end": int,
            "role_hint": str | None,  # 推测的角色名
            "quote_style": str | None,
        }]
        """
        segments = []

        # 方法1: 找所有引号包裹的对话
        for open_q, close_q in self.CN_QUOTES:
            pattern = re.escape(open_q) + r'(.+?)' + re.escape(close_q)
            for match in re.finditer(pattern, text, re.DOTALL):
                content = match.group(1).strip()
                if len(content) > 0:
                    # 尝试从前文/后文找角色提示 (PY-109: 传入引号结束位置以启用后窗)
                    role_hint = self._find_role_hint(text, match.start(), match.end())

                    segments.append({
                        "type": "dialogue",
                        "content": content,
                        "start": match.start(),
                        "end": match.end(),
                        "role_hint": role_hint,
                        "quote_style": f"{open_q}{close_q}",
                    })

        # 方法2: 找 "角色名：xxx" 模式的对话
        for match in self.ROLE_PROMPTS.finditer(text):
            role = match.group(1).strip()
            content = match.group(2).strip()
            # 避免与引号检测重复
            is_duplicate = any(
                abs(s["start"] - match.start()) < 10 for s in segments
            )
            if not is_duplicate:
                segments.append({
                        "type": "dialogue",
                        "content": content,
                        "start": match.start(),
                        "end": match.end(),
                        "role_hint": self._clean_role_name(role),
                        "quote_style": "prompt",
                    })

        # 按位置排序
        segments.sort(key=lambda x: x["start"])

        # 方法3: 给未检测到的段落标记为旁白
        segments_with_narration = []
        last_end = 0
        for seg in segments:
            if seg["start"] > last_end:
                narration_text = text[last_end:seg["start"]].strip()
                if len(narration_text) > 0:
                    segments_with_narration.append({
                        "type": "narration",
                        "content": narration_text,
                        "start": last_end,
                        "end": seg["start"],
                        "role_hint": None,
                        "quote_style": None,
                    })
            segments_with_narration.append(seg)
            last_end = seg["end"]

        # 末尾旁白
        if last_end < len(text):
            remaining = text[last_end:].strip()
            if len(remaining) > 0:
                segments_with_narration.append({
                    "type": "narration",
                    "content": remaining,
                    "start": last_end,
                    "end": len(text),
                    "role_hint": None,
                    "quote_style": None,
                })

        return segments_with_narration

    def _clean_role_name(self, name: str) -> str:
        """从角色名中去除说话动词后缀"""
        # 逆序匹配，优先移除最长的动词
        verbs = sorted(self.SPEECH_VERBS, key=len, reverse=True)
        for verb in verbs:
            if name.endswith(verb):
                return name[:-len(verb)].strip()
        return name.strip()

    # ---- PY-109: role_hint 前后缀窗扩展 ----
    # 纯代词（不是具体角色名）
    PRONOUNS = {"他", "她", "它", "我", "你", "您", "这", "那", "谁", "自己",
                "对方", "别人", "众人", "大家"}
    # 动作/说话提示字符（角色名后紧跟这些字 => 前面大概率是角色名）
    ACTION_CHARS = (
        '说道问答笑叹喊叫吼喝骂嘀咕嗯哼咳抬举放站坐转回点头摇皱瞪盯瞅看望瞥瞟眯眨'
        '抿努噘吹喘搓揉摸挠抓握掐拧拖拎提端架挎搭踩踱跨奔扑闪缩扭晃蹲靠搂扶推拉拽扯'
        '抖闭竖指比划挥摊耸嗤低高慢急沉接递给从把将用向朝向对与边连又才就也还便'
        '在是的着'
    )
    # 候选名结尾可剥离的虚词
    TRAILING_PARTICLES = '没有不才又就也还便似的了着在'

    # 紧贴候选名的"动作字"集合：剔除动态助词 的/了/着（WF-01 前会把
    # "纪云舟收了伞" 截成 "纪云舟收"、"阿福抱着药篓" 截成 "阿福抱"——
    # 惰性量词一直吞到助词前才停下），助词结构改由 _role_from_head 的
    # 的/了着 两个专分支处理；判断动词 是 保留。
    ACTION_HEAD_CHARS = ACTION_CHARS.replace('的', '').replace('了', '').replace('着', '')

    def _accept_role(self, cand: str) -> Optional[str]:
        """候选角色名清洗与过滤; 不合格返回 None"""
        if not cand:
            return None
        cand = cand.strip().strip('《》“”"\'‘’「」『』 　')
        cand = self._clean_role_name(cand)
        while cand and cand[-1] in self.TRAILING_PARTICLES:
            cand = cand[:-1]
        if len(cand) < 2 or len(cand) > 6:
            return None
        if cand in self.PRONOUNS or '《' in cand or '》' in cand:
            return None
        if cand[0] in '他她它我你您谁' or cand[0] in '说道问答喊叫叹笑':
            return None
        if '个' in cand or '些' in cand:  # "一个女声" 之类数量短语
            return None
        # 那/这 开头仅允许人指代式: 那人/那女子/那老者...
        if cand[0] in '这那' and not re.match(r'^(那|这)(人|女子|男子|老者|少年|汉子|妇人|孩子)', cand):
            return None
        return cand

    def _role_from_head(self, clause: str, anchored: bool = True) -> Optional[str]:
        """从子句开头提取 "名字+动作/说话动词" 中的名字"""
        if not clause:
            return None
        flags_pat = r'([\u4e00-\u9fa5A-Za-z·]{2,6}?)(?=[%s])' % re.escape(self.ACTION_HEAD_CHARS)
        if anchored:
            m = re.match(r'^\s*' + flags_pat, clause)
        else:
            m = re.search(flags_pat, clause)
        cand = m.group(1) if m else None
        if cand:
            role = self._accept_role(cand)
            if role:
                return role
        # 形如 "那女子/周主事" + 的 开头（的 之前整段是名词：周主事的弟子 -> 周主事）
        m2 = re.match(r'^\s*([\u4e00-\u9fa5]{2,4})(?=的)', clause)
        if m2:
            return self._accept_role(m2.group(1))
        # 形如 "林晚晴收了伞"/"小满抱着药篓"：了/着 前还夹着一个谓语字，
        # WF-01 前会被贪婪 {2,4} 连动词一起吃进候选（林晚晴收 / 小满抱）。
        m3 = re.match(r'^\s*([\u4e00-\u9fa5]{2,4}?)([\u4e00-\u9fa5]?)(?=[了着])', clause)
        if m3:
            return self._accept_role(m3.group(1))
        return None

    def _last_sentence(self, pre: str) -> str:
        """取前文的最后一个叙述句（截断到最近的句末标点/换行/引号之后）"""
        parts = re.split(r'[。！？!?\n\u201c\u201d\u300d\u300f\"]+', pre)
        for p in reversed(parts):
            if p.strip():
                return p.strip()
        return ""

    def clause_subject(self, clause: str) -> Optional[str]:
        """公开入口：取叙述小句开头 "名字+动作/说话动词" 里的名字。

        cast 抽取器用它扫描叙述段的主语，配合"同句代词主语"登记 名字->性别
        指代档案（WF-01 代词归属回溯的信号源）。不合格返回 None。
        """
        return self._role_from_head(clause, anchored=True)

    def _action_hint_after_quote(self, post_raw: str) -> Optional[str]:
        """PY-338: 后窗"动作主语"归因——"“……”林昭给自己倒了杯茶。"

        与 1) 的区别只在**不要求说话动词**，因此候选必须过 cast 的判名门：
        方位/处所词开头（里头…）、叙述趋向尾巴（…传来）、非姓名形态一律否掉。
        没注入判名门时本通道整条不开（见 `__init__`）。
        """
        if self.name_gate is None or not post_raw:
            return None
        clause = re.split(r'[。！？!?；;，,、]', post_raw)[0].strip()
        # "林昭给自己倒了杯茶" 这种整句没有句读，_role_from_head 只看开头，够用；
        # 但 "阿贵蹲在门槛上，头也不抬" 的第一分句同样在开头。
        role = self._role_from_head(clause, anchored=True)
        if role and self.name_gate(role):
            return role
        return None

    def _find_role_hint(self, text: str, quote_pos: int,
                        quote_end: Optional[int] = None) -> Optional[str]:
        """查找引号对应的角色提示

        PY-109 修复: 旧实现只在引号前 30 字里找说话动词，导致命中 0/4
        （"压低声音"/"她叹了口气" 等残片反而成为 hint）。现在按优先级:
        1) 后窗 "XX说/道/动作"（同行紧跟引号后）
        2) 后窗 "……的是XX，" 别名结构
        3) 前文冒号/逗号归因子句的开头主语（"周主事放下茶盏，压低声音："）
        4) 旧版说话动词正则（兜底）
        书名号内文本不作为角色名（_accept_role 过滤）。

        WF-01 收紧: 后窗两步（1/2）必须落在**说话动词邻域**——旧实现用泛动作
        字符集 + 裸 "的XX，" 结构，把 "里头传来拖长的问话" 截成角色名 "里头传来"、
        把 "她的声音很轻" 截成 "声音很轻"，下游因此凭空造出假角色并绑音色。
        前窗（3/4）是叙述主语归因，保持 PY-109 的宽松口径不变。

        PY-338 恢复: WF-01 顺手把整条**动作后窗**关掉了（"“……”林昭给自己倒了杯茶。"
        不再归因，实测同夹具 PRE 命中 5/5 → HEAD 0/5）。恢复的是 1b 那一格，代价是
        候选必须过注入的判名门 ⇒ 两枚假角色仍拦得住，而 `那人` 这类指示代词头也不认，
        所以这一档的上限是 3/4，不是 PY-109 原账的 4/4。
        """
        if quote_end is None:
            quote_end = quote_pos

        # 1) 后窗: 紧跟引号后、同一行内的 "名字+说话动词"（WF-01: 只认说话动词）
        post_raw = text[quote_end:quote_end + 16]
        if post_raw and not post_raw[:1] in ('\n', '\r'):
            m = self._SPEECH_HEAD_RE.match(post_raw)
            if m:
                role = self._accept_role(m.group(1))
                if role:
                    return role
            # 2) "说话的是户部的周主事，" 结构（的 前须有说话动词）
            m = self._SPEECH_OF_RE.search(post_raw)
            if m:
                role = self._accept_role(m.group(1))
                if role:
                    return role
            # 1b) PY-338: 动作主语后窗（不要求说话动词），候选过 cast 的判名门
            role = self._action_hint_after_quote(post_raw)
            if role:
                return role

        # 前文窗口从 30 扩到 40 字
        pre_text = text[max(0, quote_pos - 40):quote_pos]
        pre_stripped = pre_text.rstrip()

        # 3) 冒号/逗号归因: "XX（动作），……：" "XX蹲在门槛上，头也不抬，"
        if pre_stripped.endswith(('：', ':', '，', ',')):
            sent = self._last_sentence(pre_stripped[:-1])
            clause = re.split(r'[，、,]', sent)[0].strip() if sent else ""
            role = self._role_from_head(clause) if clause else None
            if role:
                return role
            if sent and 2 <= len(sent) <= 4:
                role = self._accept_role(sent)
                if role:
                    return role

        # 4) 兜底: 旧版动词正则（引号字符不再混入捕获组）
        verbs_pattern = '|'.join(sorted(map(re.escape, self.SPEECH_VERBS), key=len, reverse=True))
        excl = r'[^\s：:，。！？"\'""''《》]'
        role_patterns = [
            rf'({excl}{{1,8}})\s*(?:{verbs_pattern})',
            rf'({excl}{{1,8}})\s*[：:]\s*$',
        ]
        for pattern in role_patterns:
            match = re.search(pattern, pre_stripped)
            if match:
                raw = match.group(1)
                if len(raw) == 1 and raw in self.PRONOUNS:
                    return raw  # 单字代词原样返回, 由外部决定(保持旧行为)
                role = self._accept_role(raw)
                if role:
                    return role

        return None

    def extract_roles(self, dialogues: list) -> list:
        """
        从检测结果中提取角色列表
        返回: [{"name": str, "count": int}, ...]
        """
        from collections import Counter

        roles = Counter()
        for d in dialogues:
            if d["type"] == "dialogue" and d["role_hint"]:
                roles[d["role_hint"]] += 1

        return [
            {"name": name, "count": count}
            for name, count in roles.most_common(20)
        ]