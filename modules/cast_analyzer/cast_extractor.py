"""
cast 抽取（多人有声书批次 A）

设计依据: docs/superpowers/specs/2026-09-20-multi-cast-audiobook-design.md 4.1

要点:
- 流式逐章: 调用方按章喂文本, 内部只保留聚合状态, 内存峰值 = 1 章 (红线: 不卡顿)
- 信号源: 复用 DialogueDetector.detect_dialogues 的引号/提示词归属启发,
  并补充 "对白后缀" ("……"张三说) 与 代词回溯 (他/她 -> 最近提及的专名)
- 聚合字段: {name, lines_count, chapters_touched(<=8 个章号样本), chapters_count(精确章数),
  sample_quotes[:8], co_speakers} —— 章号只留有界样本，长书下聚合内存不随章数增长
- 噪声过滤: 出现 <2 次 / 纯代词 / 长度 >6 字 的候选丢弃
- 噪声过滤 (WF-01 新增): 候选写法须出现在**说话动词邻域**且非叙述句式——
  方位/处所词开头（里头/外面…）、叙述趋向动词尾巴（传来/响起…）一律剔除，
  来自引号后窗的提示还要求其后 3 字内出现说话动词，杜绝 "里头传来拖长的问话"
  被当成角色并绑音色
- 指代档案 (WF-01 新增): 叙述段里 "名字作小句主语 + 同段代词作小句主语"
  登记 名字->性别；对白的 "她/他 + 说话线索(说/道/问/声音…)" 归到回溯窗口内
  最近提及的同性别角色，使只有代词提示的主角句不再被丢弃
- 短输入救援 (5e, C6 实证): 总字数 < short_text_chars(默认 2000) 且严格过滤后
  一个具名角色都不剩时, "出现次数" 门限降为 1, 但仅当该候选至少有一条
  引号归属证据(前缀 "XX："/"XX说" 或后缀 "……"XX说 / detector role_hint,
  纯代词回溯不计) 才放行——裁定依据见 qa/issues.md CAST-E2E 段与
  run_c6_e2e_multicast.py 注释; 长文/有角文本行为与旧实现逐字节一致 (C1/C2/C5 不回归)
- 别名合并: 老周/周队长/周 -> 戏份最多的全名; 职务称谓记入 titles 与 aliases;
  只是某个全名**中段片段**的短写法（圣雅 ⊂ 约翰·圣雅各）并入"性别信号一致的唯一
  全名主机"，显示名钉死在全名上；主机有歧义或性别互相冲突时**不归并**，但记成
  alias_candidates（surface/host/merged/confidence/reason）留痕可复核，绝不静默丢弃
- 纯逻辑、无 IO、无 DB, 可离线单测 (qa/harness/run_c1_cast_extract.py)
"""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple

from modules.text_processor.dialogue_detector import DialogueDetector

# 旁白固定项名称（对应 Role.role_type='narrator'）
NARRATOR = "旁白"

# 纯代词：不作为角色候选
PRONOUNS = {
    "他", "她", "它", "我", "你", "您", "我们", "你们", "他们", "她们",
    "咱们", "这", "那", "谁", "自己", "本人", "大家", "众人",
}

# 第三人称代词 -> 性别（WF-01: 叙述段的 "名字主语 + 同句代词" 指代档案）
GENDER_PRONOUN_CHARS = {"他": "male", "她": "female", "它": "neuter"}

# 代词后的名词性说话线索（"她的声音很轻" 与 "她说" 同等强度）
PRONOUN_CUE_NOUNS = ["声音", "嗓音", "口气", "语调", "话", "回应"]

# 否定词首字：名字之后紧跟这些字 = "林晚晴没接话" 式否定叙述，不作归属证据
NEGATION_HEADS = {"没", "不", "未", "别", "莫", "无", "非", "甭"}

# 方位/处所词：以这些词开头的候选是叙述状语，不是人名（WF-01 "里头传来…"）
LOCATIVE_HEADS = [
    "里头", "里面", "里外", "外头", "外面", "远处", "近处", "深处", "身后", "身前",
    "暗处", "角落", "门后", "门外", "窗内", "屋内", "屋外", "空中", "半空", "脚下",
    "头顶", "墙根", "院中", "堂中", "帐中", "水底", "此时", "那时", "眼前", "身边",
]

# 叙述/趋向动词尾巴：以这些词结尾的候选是叙述残片（"…传来拖长的问话"），不是人名
NARRATION_TAILS = [
    "传来", "传出", "响起", "响过", "响成", "飘来", "飘过", "掠过", "闪过",
    "出现", "浮现", "回荡", "溢出", "冒出", "滚来", "漫开", "散开", "袭来", "迫来",
]

# 常见姓氏子集（用于简称/别名归并）
SURNAMES = set(
    "王李张刘陈杨黄赵吴周徐孙马朱胡郭何林罗高郑梁谢宋唐许韩冯邓曹彭曾肖田董潘袁蔡蒋余于杜叶程苏魏吕丁"
    "任沈姚卢姜崔钟谭陆汪范金石廖贾夏韦付方白邹孟熊秦邱江尹薛闫段雷侯龙史陶黎贺顾毛郝龚邵万钱严覃武戴莫孔"
)

# 职务/身份称谓（"张队长" -> core "张"；单独出现的 "掌柜" 视为称谓别名）
TITLE_SUFFIXES = [
    "队长", "副队长", "老师", "医生", "大夫", "护士", "船长", "掌柜", "老板", "经理",
    "董事长", "局长", "站长", "部长", "处长", "科长", "教授", "馆长", "警官", "警察",
    "侦探", "记者", "律师", "师傅", "师父", "阿姨", "大爷", "大妈", "大哥", "大姐",
    "小姐", "先生", "女士", "夫人", "太太", "将军", "长老", "少爷", "丫头", "同学",
    "政委", "连长", "排长", "司令", "团长", "参谋", "主编", "主任", "院长", "校长",
]

# 称谓前缀（"老周" / "小满" / "阿福"）
HONORIFIC_PREFIXES = ("老", "小", "大", "阿")

# CAST-RESIDUAL: "老/小/大/阿 + 单字" 除了简称人名，还是汉语**性质副词**的构词形态——
# "老" 头 + 一个性质语素（诚实/老练/老成…一类）能直接贴在说话动词前作**话语状语**
# （"…，老实说，您…" = honestly speaking），而 is_person_shaped 只认得到前缀，
# 形态上分不清它和昵称。老板/老头/阿布/老周 又是同一形态的**真说话人**，
# 所以判据只能落在语料分布上：真名的出现散布在主语/宾语/定语各处，
# 状语写法的出现几乎全部挤在 "写法+说话动词" 这一个框架里。
# 下列两项都是词类常量（说话动词子集 + 占比门限），不是样本里出现过的字符串。
ADVERBIAL_SPEECH_MODS = ("说", "道")
ADVERBIAL_MOD_SHARE = 0.5        # 落在该框架的次数 >= 全书出现次的 50% 即判为状语

# 说话动词（在 DialogueDetector.SPEECH_VERBS 之上补充后缀归属常见写法）
EXTRA_SPEECH_VERBS = [
    "说道", "说", "道", "问道", "问", "答道", "回道", "回答", "笑道", "冷笑", "怒道",
    "喝道", "吼道", "叹道", "嘟囔", "低声道", "喃喃", "补充", "摇头", "点头", "开口",
    "接话", "追问", "提醒", "警告", "解释", "重复", "念叨", "嚷", "讲",
]

NAME_CHARS = r"[\u4e00-\u9fa5A-Za-z\u00b7]"
PUNCT_STRIP = " \t\r\n，。！？、；：,.!?;:\"“”‘’「」『』（）()【】《》…"

# 单字功能词/语气词：候选名里出现即判为误截片段
STOP_CHARS = set("的了着过是很都也就才但又还没不这那之其等吗呢吧啊呀哦嗯哈与和把被让向对从"
                 "之所以而且然后未如若所")

# 多字噪声片段：副词、代称、以及 "许久没有说" 这类被正则误截的动词短语
STOP_WORDS = [
    "没有", "许多", "一个", "两个", "这个", "那个", "但是", "于是", "然后", "可是",
    "什么", "怎么", "自己", "有人", "众人", "大家", "听见", "看到", "想到", "知道",
    "突然", "忽然", "这才", "连忙", "赶紧", "继续", "再次", "低头", "抬头", "转身",
    "皱眉", "苦笑", "冷笑", "摇头", "点头", "小声", "低声", "大声", "沉声", "厉声",
    "咬牙", "轻轻", "慢慢", "悄悄", "愣了", "想了", "笑了", "点了", "看了", "问道",
    "说道", "笑道", "答道", "一声", "一句", "半天", "半晌", "好一会儿",
]

# ============================================================ 人名形态（CAST-FIX）
# 以下三类都是**词类闭集**（成员由语言学类别给定，不是样本里出现过的字符串）：
# 出现即判为"叙述残片"。代词/状语标记字任何位置都不许进姓名；虚词字里
# 有一批同时是中文名用字，那部分豁免写在下面的 NAME_INTERNAL_STOP_CHARS。
PRONOUN_NAME_CHARS = set("我你他她它咱您们谁")     # 人称/疑问代词（"我知/跟我/他故意"）
ADVERB_NAME_CHARS = set("地")                    # 状语标记（"她轻声地/康克林冷冷地"）
BANNED_NAME_CHARS = STOP_CHARS | PRONOUN_NAME_CHARS | ADVERB_NAME_CHARS
# 中文名**内部**合法语素：这些字做虚词时是「他向着/之所以」，做名字用字时是
# 沈若兰/王羲之/李从文/周之才。豁免只给「首字是常见姓氏 + 全长>=3」的候选，
# 两字残片（温和/从容/这才/之前）走不到这条门。成员须是 STOP_CHARS 的子集，
# 否则查不到（不在 BANNED_NAME_CHARS 里的字本来就不会被拦）。
NAME_INTERNAL_STOP_CHARS = set("若之才和从其然")

# 汉译外文姓名的**末音节**常用字。刻意排除 佛/天/白/音/里/得/台/实/口/气/话/睛 等
# 高频中文语素字，否则 "仿佛/明天/老实/声音/心里/电话/柜台/眼睛/口气" 会被当成译名。
# 收 林/希 是为 康克林/斯大林/柯尼希 这类译名尾音节；中文语素里以 林/希 收尾的
# 词只有 森林/树林/碑林 一类处所名词，它们进不了"贴着说话动词"的提示位。
TRANSLIT_TAIL_CHARS = set(
    "斯尔亚马德克特丁诺拉夫森顿兰宁尼雅丽莉琳珊莎姬芙琴松恩维耶伊尤扎巴本姆敦塔泰坦"
    "赫洛雷勒列露鲁约瑟塞绍曼班丹凯基歌根戈冈哈豪龙福娜莎玫慕缪芬菲林希"
)

# 量词/泛指指示词：汉语人名不可能以这些字开头（真书噪声 "个职员客气地/各博士冷冷地"
# 正是窗口正则从 "一介职员/各位博士" 前缀误截的产物）。
QUANT_HEAD_CHARS = set("个各每某些只条块件位尊匹盏顶双截样种")

# 人物通称（职业/军衔/警衔/宗教/职务/性别年龄/仆役等"能开口的通名"）。
# 它们不是姓名，但在有声书里确实是需要独立音色的说话人（"司机说""上校回答"）。
PERSON_GENERIC = [
    # 职业/职务
    "医生", "大夫", "护士", "产婆", "司机", "驾驶员", "船长", "水手", "乘务员", "店员",
    "伙计", "掌柜", "老板", "东家", "领班", "管家", "仆人", "侍女", "差役", "狱卒",
    "狱警", "侦探", "警探", "警长", "探长", "法官", "律师", "法医", "刑警", "书记官",
    "翻译", "向导", "设计师", "工程师", "程序员", "教师", "教授", "老师", "学生",
    "同学", "师傅", "师父", "裁缝", "铁匠", "木匠", "厨子", "伙夫", "跑堂", "门房",
    "樵夫", "渔夫", "农夫", "牧民", "商人", "摊贩", "学徒", "局长", "站长", "部长",
    "处长", "科长", "主任", "主编", "院长", "校长", "议员", "官员", "经理", "总裁",
    # 军衔/武职
    "上校", "中校", "少校", "大校", "上尉", "中尉", "少尉", "上士", "中士", "下士",
    "士官", "军官", "士兵", "副官", "参谋", "司令", "将军", "团长", "连长", "排长",
    "政委", "侍卫", "骑士", "武士", "枪手", "狙击手", "绑匪", "劫匪", "凶手", "杀手",
    # 宗教
    "神父", "牧师", "教士", "修士", "修女", "尼姑", "和尚", "僧人", "道士", "巫师",
    "巫婆", "香客", "太监", "宫女", "酋长", "长老", "族长", "村长", "支书", "县委书记",
    # 性别/年龄/亲属通称
    "女人", "男人", "老人", "老头", "老太太", "老婆子", "姑娘", "女郎", "少年",
    "小伙子", "中年人", "陌生人", "旅客", "乘客", "乞丐", "少爷", "丫头", "阿姨",
    "大爷", "大妈", "大哥", "大姐", "先生", "女士", "小姐", "太太", "夫人", "员外",
]

PERSON_TITLES = tuple(list(TITLE_SUFFIXES) + list(PERSON_GENERIC))

# 姓名长度基线：汉语姓名 2-4 字，汉译外名 2-4 音节；带间隔号的复合译名放到 6。
# 真书 844 条噪声的 C 类特征正是"5-6 字窗口残片"（NAME_CHARS{2,6} 的贪婪上界）。
MAX_PLAIN_NAME_LEN = 4
MAX_COMPOUND_NAME_LEN = 6


# 姓名形态判定用**通行姓氏全表**（百家姓常见单姓）。既有的 SURNAMES 是保守子集，
# 只服务"单姓归并"（宁可少并不误并）；用它判形态会误杀 纪/孔/邹/舒 这类真姓氏
# ——回归样本里的 纪云舟 正是被漏掉的主角形态。
# 刻意不收 明/常/齐/谈/解/花/方 等"首先作虚词/形容词/动词语素"的字：
# "明说/常说/齐声说/解释/花说" 这类窗口残片会借姓氏之名混进角色表。
COMMON_SURNAMES = SURNAMES | set(
    "纪孔邹喻柏章葛奚郎鲁韦昌苗俞滕殷毕邬傅伍顾萧尹邵湛祁狄臧伏茅庞舒屈项祝阮蓝闵季路娄童颜梅盛"
    "刁骆樊凌霍虞柯管房裘缪宗宣郁杭洪包古景詹束饶岳尚屠迟云单雍邝绍"
)
# 复姓（双字）：整体作前缀命中即可判为姓名形态，长度上限仍是姓名区间的 4 字。
COMPOUND_SURNAMES = (
    "欧阳", "司马", "上官", "诸葛", "夏侯", "皇甫", "尉迟", "公孙", "长孙", "慕容",
    "司徒", "司空", "东方", "独孤", "南宫", "呼延", "赫连", "拓跋", "宇文", "鲜于",
    "东郭", "左丘", "梁丘", "东门", "仲孙", "公西",
)


def is_person_shaped(raw: str) -> bool:
    """候选写法是否具备**姓名形态**（正向判定，取代旧的"不像停用词就算人名"）。

    成立路径（任一）：
      a) 常见姓氏开头（COMMON_SURNAMES）或 老/小/大/阿 称谓前缀 —— 中文名形态；
      a') 复姓开头（COMPOUND_SURNAMES，如 欧阳娜娜）；
      b) 以职务/身份/军衔/宗教/性别年龄通称结尾 —— "张队长/上校/老医生"；
      c) 含间隔号 · 的复合译名（"玛莉·圣雅各"）；
      d) 末字是汉译外名末音节用字 —— "卡洛斯/康克林/诺尔顿/阿伯特"。
    同时必须通过三道负向门：不以量词/泛指指示词开头；不含代词/状语标记字（虚词字仅在
    "首字是常见姓氏 + 全长>=3"时允许做名字内部语素，见 NAME_INTERNAL_STOP_CHARS / PY-335）；
    长度在姓名区间内。
    """
    s = str(raw or "").strip()
    if len(s) < 2:
        return False
    if s[0] in QUANT_HEAD_CHARS:                 # 个/各/每… 开头 = 窗口误截
        return False
    compound = ("·" in s) or ("．" in s)
    limit = MAX_COMPOUND_NAME_LEN if compound else MAX_PLAIN_NAME_LEN
    if len(s) > limit:
        return False
    for i, ch in enumerate(s):
        if ch not in BANNED_NAME_CHARS:
            continue
        if i == 0 and ch in COMMON_SURNAMES:     # 于/万/毛/白… 既是虚词也是姓氏
            continue
        if (i > 0 and len(s) >= 3 and s[0] in COMMON_SURNAMES
                and ch in NAME_INTERNAL_STOP_CHARS):
            continue
        return False
    if compound:
        # 真汉译外名每个间隔号分段至少 2 音节；"·伯恩/多米尼克·/卫·韦伯" 是
        # 窗口正则从 杰森·伯恩 / 亚历山大·多米尼克 里截出来的残片，不是写法。
        parts = re.split(r"[·．]", s)
        if any(len(p.strip(PUNCT_STRIP)) < 2 for p in parts):
            return False
        return True
    if s[0] in COMMON_SURNAMES or s[0] in HONORIFIC_PREFIXES:
        return True
    if any(s.startswith(c) for c in COMPOUND_SURNAMES):
        return True
    if any(s.endswith(t) for t in PERSON_TITLES):
        return True
    return s[-1] in TRANSLIT_TAIL_CHARS


@dataclass
class CastCandidate:
    """一个角色候选（统计 + 画像输入 + profiler 回填的 profile）。"""

    name: str
    role_type: str = "supporting"
    aliases: List[str] = field(default_factory=list)
    # 第 5 层包含式归并的**可复核判定**（含"未归并"的歧义情况）：
    # [{surface, host, merged, confidence, reason, surface_lines, host_lines}, ...]
    alias_candidates: List[Dict] = field(default_factory=list)
    lines_count: int = 0
    occurrences: int = 0                                        # 全书出现次数(语料常见词判定分母)
    chapters_touched: List[int] = field(default_factory=list)   # 有界样本（最多 MAX_CHAPTER_SAMPLES 个章号）
    chapters_count: int = 0                                     # 精确出场章数
    sample_quotes: List[str] = field(default_factory=list)
    co_speakers: List[str] = field(default_factory=list)
    he_count: int = 0
    she_count: int = 0
    it_count: int = 0
    titles: List[str] = field(default_factory=list)
    pronoun_resolved: int = 0
    direct_hints: int = 0
    is_narrator: bool = False
    profile: Dict = field(default_factory=dict)

    def to_dict(self) -> Dict:
        return {
            "name": self.name,
            "role_type": self.role_type,
            "aliases": list(self.aliases),
            "alias_candidates": [dict(c) for c in self.alias_candidates],
            "lines_count": self.lines_count,
            "occurrences": self.occurrences,
            "chapters_touched": list(self.chapters_touched),
            "chapters_count": self.chapters_count,
            "sample_quotes": list(self.sample_quotes),
            "co_speakers": list(self.co_speakers),
            "he_count": self.he_count,
            "she_count": self.she_count,
            "it_count": self.it_count,
            "titles": list(self.titles),
            "pronoun_resolved": self.pronoun_resolved,
            "direct_hints": self.direct_hints,
            "is_narrator": self.is_narrator,
            "profile": dict(self.profile),
        }


MAX_CHAPTER_SAMPLES = 8    # 每角色保留的章号样本上限（保证聚合内存与书的长度无关）
MAX_REJECTED_AUDIT = 60    # 被排除项可审计清单的条数上限（CAST-FIX 验收规则 2）
MAX_MENTIONS = 256         # 名字->性别 指代档案上限（跨章保留，超出后不再登记新名字）
MAX_CHAPTER_MENTIONS = 64  # 单章内登记的提及条数上限（每章重置，内存与章数无关）
MAX_SUBJECT_KEYS = 512     # 叙述小句主语计数表的键数上限（与书长无关）
MAX_SUBJECT_SEEN = 8       # 单个主语的计数上限（门限只需 >=2，不存精确大数）
MAX_ALIAS_CANDIDATES = 8   # 每角色留痕的"候选别名"条数上限（CAST-RESIDUAL，与书长无关）


class _Agg:
    """单角色内部累加器（finalize 时转 CastCandidate）。"""

    def __init__(self, name: str):
        self.name = name
        self.lines = 0
        self.hinted = 0                           # 其中"引号归属证据"句数(前/后缀或 role_hint)
        self.mention_backed = 0                   # 其中"名字主语+同句代词"最近提及归属句数(WF-01)
        self.occ = 0                              # 全书出现次数(语料常见词判定, CAST-FIX G)
        self.mod_speech = 0                       # 其中落在 "写法+说/道" 状语框架的次数
        self.chapter_count = 0                      # 出场章数（精确值）
        self.chapter_samples: List[int] = []        # 出场章号样本（有界）
        self._last_chapter: Optional[int] = None    # 流式按章喂入，同章连续命中只计一次
        self.quotes: List[str] = []
        self.co: Counter = Counter()
        self.he = 0
        self.she = 0
        self.it = 0
        # 已计过代词的最右绝对偏移（逐章复位）：同一人连着说几句时，窗口会重复
        # 覆盖同一段叙述，不挡就会按句数平方级膨胀（PY-336）。
        self.pronoun_floor = -1
        self.titles: Counter = Counter()
        self.pronoun_resolved = 0
        self.surfaces: Counter = Counter()    # 原始写法 -> 行数（老周/周队长/周）
        self.final_co: Counter = Counter()    # 归并后的对手戏（规范名）
        # CAST-RESIDUAL: 包含式归并（短写法是长全名的**中段**）落位后，
        # 显示名固定为全名而非戏份更多的残片；歧义判定一律留痕。
        self.display_override: Optional[str] = None
        self.alias_candidates: List[Dict] = []

    def touch(self, chapter_index: int) -> None:
        """记一次出场章。O(1) 内存：只留章数与有界样本，不留整书章号集合。"""
        if chapter_index == self._last_chapter:
            return
        self._last_chapter = chapter_index
        self.chapter_count += 1
        if len(self.chapter_samples) < MAX_CHAPTER_SAMPLES:
            self.chapter_samples.append(chapter_index)

    def canonical_name(self) -> str:
        """规范显示名：包含式归并指定的全名优先，否则出现次数最多者、并列取更长写法。

        CAST-RESIDUAL: 短残片（圣雅）并入全名（约翰·圣雅各）后，戏份最多的写法
        恰是那个残片，若照旧规则取名就会把合并说成"换了个名字继续存在"，
        故 display_override 优先——它只影响**显示名**，不影响任何计数。
        """
        if self.display_override and self.display_override in self.surfaces:
            return self.display_override
        if not self.surfaces:
            return self.name
        return max(self.surfaces, key=lambda s: (self.surfaces[s], len(s), s))

    def other_surfaces(self) -> List[str]:
        head = self.canonical_name()
        ordered = sorted(self.surfaces, key=lambda s: (-self.surfaces[s], -len(s), s))
        return [s for s in ordered if s and s != head]

    def absorb(self, other: "_Agg", max_quotes: int) -> None:
        self.lines += other.lines
        self.hinted += other.hinted
        self.mention_backed += other.mention_backed
        self.occ += other.occ
        self.mod_speech += other.mod_speech
        # 别名并入：同一人的不同写法出场章高度重叠，章数取较大值即可
        # （不保留整书章号集合，保证流式内存有界）
        self.chapter_count = max(self.chapter_count, other.chapter_count)
        for ch in other.chapter_samples:
            if len(self.chapter_samples) >= MAX_CHAPTER_SAMPLES:
                break
            if ch not in self.chapter_samples:
                self.chapter_samples.append(ch)
        self.chapter_samples.sort()
        self.co.update(other.co)
        self.titles.update(other.titles)
        self.surfaces.update(other.surfaces)
        self.he += other.he
        self.she += other.she
        self.it += other.it
        self.pronoun_resolved += other.pronoun_resolved
        for q in other.quotes:
            if len(self.quotes) >= max_quotes:
                break
            if q not in self.quotes:
                self.quotes.append(q)
        seen = {(c.get("surface"), c.get("host")) for c in self.alias_candidates}
        for cand in other.alias_candidates:
            if len(self.alias_candidates) >= MAX_ALIAS_CANDIDATES:
                break
            if (cand.get("surface"), cand.get("host")) in seen:
                continue
            self.alias_candidates.append(cand)
            seen.add((cand.get("surface"), cand.get("host")))

    def add_quote(self, text: str, max_quotes: int) -> None:
        if len(self.quotes) >= max_quotes:
            return
        self.quotes.append(text)


class CastExtractor:
    """全书文本 -> 角色候选表（流式逐章）。"""

    def __init__(self,
                 detector: Optional[DialogueDetector] = None,
                 min_lines: int = 2,
                 max_name_len: int = 6,
                 max_quotes: int = 8,
                 window: int = 40,
                 backsearch: int = 90,
                 short_text_chars: int = 2000,
                 min_evidence: int = 1,
                 min_line_share: float = 0.002,
                 min_occurrences: int = 30,
                 min_name_share: float = 0.02):
        """
        Args:
            min_lines: 对白句数下限，低于该值判为噪声
            max_name_len: 候选名最大长度，超过判为噪声（引文误截）
            window: 前/后缀归属启发扫描窗口（字符）
            backsearch: 代词回溯窗口（字符）
            short_text_chars: "短输入救援" 的字数上限——总字数低于该值且严格
                过滤后具名角色为空时，出现次数门限降为 1（仍要求引号归属证据，
                见模块 docstring 与 finalize 注释）。0 = 关闭救援。
            min_evidence: 一个候选至少要有多少条 **非回溯** 归属证据（引号直连
                提示 hint 或 "名字主语+同句代词" 最近提及）。0 = 关闭该门限。
                CAST-FIX(D) 放大器：90 字回溯会把无提示对白整片记到"曾经被登记过
                的任意写法"上，只靠 lines 过门限，故必须另设证据门限。
            min_line_share: CAST-FIX(F) 显著度下限——一个角色至少要占全书对白行数
                的该比例（lines >= ceil(min_line_share * dialogue_lines)）才配一条
                独立音色。**不是 top-N**：门限由书自身的对白总量算出，逐条可解释，
                短文（对白 < ~500 行）时该式子恒 <= min_lines，行为与旧实现一致。
                0 = 关闭。
            min_occurrences: 语料常见词判定（G）的"全书出现次数"下限；低于该值
                的稀有写法一律不判为普通词（避免误杀 谢尔盖/罗琴科 这类一次性专名）。
            min_name_share: 出现 >= min_occurrences 次的写法，须有至少
                `hinted / occurrences >= min_name_share` 的比例落在**说话位**
                （贴着引号的归属提示），否则判为普通词/副词（房间/马上/万一/老实
                在 155 万字里出现数千次却几乎没有说话位证据）。0 = 关闭。
        """
        # PY-338: 动作后窗归因要用**同一道**判名门，故在这里注入（dialogue_detector
        # 反过来 import 本模块会成环，门只能注）。
        self.detector = detector or DialogueDetector(name_gate=looks_like_name)
        self.min_lines = max(1, int(min_lines))
        self.min_evidence = max(0, int(min_evidence))
        self.min_line_share = max(0.0, float(min_line_share))
        self.min_occurrences = max(2, int(min_occurrences))
        self.min_name_share = max(0.0, float(min_name_share))
        self.short_text_chars = max(0, int(short_text_chars))
        self.max_name_len = int(max_name_len)
        self.max_quotes = int(max_quotes)
        self.window = int(window)
        self.backsearch = int(backsearch)

        verbs = sorted(set(list(self.detector.SPEECH_VERBS) + EXTRA_SPEECH_VERBS),
                       key=len, reverse=True)
        self._verbs = verbs
        verbs_alt = "|".join(re.escape(v) for v in verbs)
        self._pre_name_verb = re.compile(
            rf"({NAME_CHARS}{{2,{self.max_name_len}}})\s*(?:{verbs_alt})")
        self._pre_colon = re.compile(
            rf"({NAME_CHARS}{{2,{self.max_name_len}}})\s*[：:]\s*$")
        self._post_name_verb = re.compile(
            rf"^[”\"」』）)，,、。\s]*({NAME_CHARS}{{2,{self.max_name_len}}})\s*(?:{verbs_alt})")
        self._post_verb_name = re.compile(
            rf"^(?:{verbs_alt})[了过]?[，,、]?\s*(?:的)?\s*({NAME_CHARS}{{2,{self.max_name_len}}})")

        # ---- WF-01: 说话动词邻域 + 代词归属信号 ----
        # 任一说话动词（用于判定提示词两侧是否"贴着"说话动词）
        self._verbs_re = re.compile(verbs_alt)
        # CAST-FIX(C'): 单个名字字符（判定"窗口左边缘是否落在名字中间"用）
        self._name_char_re = re.compile(NAME_CHARS)
        # "她/他 + 说话线索"：她说 / 他，问道 / 她的声音 / 他开口的口气
        cue_nouns = "|".join(re.escape(n) for n in PRONOUN_CUE_NOUNS)
        self._pronoun_cue_re = re.compile(
            rf"(他|她|它)[，、,]?\s*(?:的\s*)?(?:{verbs_alt}|{cue_nouns})")
        # 叙述段小句切分：连续的字（不含标点/空白/引号）
        self._clause_token = re.compile(
            r"[^，。、；：！？,;:!?\s\n\r“”‘’\"'（）()【】《》…—\-·]+")
        self._sentence_break = re.compile(r"[。！？!?…\n]")

        self._aggs: Dict[str, _Agg] = {}
        self._surface_key: Dict[str, str] = {}     # 原始写法 -> 聚合 key（脏提示对齐用）
        self._mention_gender: Dict[str, str] = {}  # key -> male/female/neuter（有界）
        self._chapter_mentions: List[Tuple[int, str, str]] = []  # (章内位置, key, 写法)
        self._subject_seen: Counter = Counter()    # 叙述小句主语候选 -> 次数（有界）
        self._narration = _Agg(NARRATOR)
        self._chapters_seen = 0
        self._chars_seen = 0
        self._dialogue_lines = 0
        self._unresolved = 0
        self._mention_resolved = 0            # 靠"同性别最近提及"归属成功的句数
        self._relaxed_rescued = 0             # 短输入救援放行的候选数(finalize 时更新)
        self._dropped: Counter = Counter()
        self._rejected: List[Dict] = []          # 被排除项可审计清单（finalize 填充）
        self._rejected_total: int = 0

    # --------------------------------------------------------------- 流式入口
    def process_chapter(self, text: str, chapter_index: int = 0) -> int:
        """喂入一章（P3 清洗后的）文本；聚合后不持有该章字符串。返回对白句数。"""
        if not text:
            return 0
        self._chapters_seen = max(self._chapters_seen, chapter_index + 1)
        self._chars_seen += len(text)
        segments = self._dialogue_segments(text)
        self._chapter_mentions = []           # 指代档案按章滚动，内存与章数无关
        for agg in self._aggs.values():       # 偏移量逐章重来，跨章不复用去重位
            agg.pronoun_floor = -1

        previous_speaker: Optional[str] = None
        dialogue_lines = 0
        for seg in segments:
            if seg.get("type") == "narration":
                body = (seg.get("content") or "").strip()
                if len(body) >= 8:
                    self._narration.lines += 1
                    self._narration.touch(chapter_index)
                    self._narration.add_quote(self._clip(body), self.max_quotes)
                self._scan_mentions(body, int(seg.get("start") or 0))
                continue

            content = (seg.get("content") or "").strip()
            if not content:
                continue
            dialogue_lines += 1
            resolved = self._resolve_speaker(text, seg)
            if not resolved:
                self._unresolved += 1
                self._dropped["unresolved"] += 1
                continue
            key, surface, direct, evidence = resolved
            agg = self._aggs.setdefault(key, _Agg(key))
            agg.lines += 1
            if direct:
                agg.hinted += 1
            elif evidence == "mention":
                agg.mention_backed += 1
            agg.surfaces[surface] += 1
            self._surface_key[surface] = key
            agg.touch(chapter_index)
            agg.add_quote(self._clip(content), self.max_quotes)
            self._count_pronouns(agg, text, seg, surface)
            for title in TITLE_SUFFIXES:
                if surface.endswith(title) and len(surface) > len(title):
                    agg.titles[title] += 1
            if previous_speaker and previous_speaker != key:
                agg.co[previous_speaker] += 1
                prev = self._aggs.get(previous_speaker)
                if prev is not None:
                    prev.co[key] += 1
            previous_speaker = key

        self._count_occurrences(text)
        self._dialogue_lines += dialogue_lines
        del segments
        return dialogue_lines

    def _count_occurrences(self, text: str) -> None:
        """CAST-FIX(G): 统计**已登记写法**在本章的出现次数 -> _Agg.occ。

        语料常见词判定的分母来自书自身：房间/马上/万一 这类写法在 155 万字里
        出现数千次，却几乎不落在说话位；真名的说话位占比高一个量级。
        只对已注册写法做 C 层 str.count（键数 = 全书候选数，实测 359 条），
        不引入分词器/外部词典，成本与书长成线性、与候选数有界相乘。

        CAST-RESIDUAL: 其中形态上可能是"称谓前缀+性质语素"的那一小撮写法
        （`_is_adverbial_shape` 命中，155 万字真书实测 <= 10 条）额外统计
        "写法+说/道" 框架次数 -> _Agg.mod_speech，供 `_reject` 的状语判据用；
        其余写法一次 count 都不多花。
        """
        if not text or not self._surface_key:
            return
        for surface, key in list(self._surface_key.items()):
            agg = self._aggs.get(key)
            if agg is None or len(surface) < 2:
                continue
            n = text.count(surface)
            if n:
                agg.occ += n
            if self._is_adverbial_shape(surface):
                agg.mod_speech += sum(text.count(surface + m)
                                      for m in ADVERBIAL_SPEECH_MODS)

    def extract(self, chapters: Iterable[Tuple[int, str]]) -> List[CastCandidate]:
        """按 (chapter_index, text) 迭代流式抽取，返回 finalize 结果。"""
        for idx, text in chapters:
            self.process_chapter(text, idx)
        return self.finalize()

    def finalize(self) -> List[CastCandidate]:
        """别名合并 -> 噪声过滤(含短输入救援) -> 按戏份排序；旁白固定为最后一项。

        短输入救援(5e): 总字数 < short_text_chars 且严格过滤后具名角色为 0 时，
        "出现次数" 门限降为 1 —— 但仅放行至少有一条归属证据(_evidence>=1: 引号直连
        提示或"名字主语+同句代词"最近提及)的候选。
        触发条件包含"严格过滤全灭"，长文或本就有角色的短文本行为与旧实现一致
        （C1 用例4 的 阿贵/rare 过滤、C4 幂等断言均不受影响）。
        """
        heads, key_to_name = self._merge_aliases()
        strict_survivors = [agg for agg in heads if self._reject(agg) is None]
        relax = (bool(self.short_text_chars)
                 and self._chars_seen < self.short_text_chars
                 and not strict_survivors)
        self._relaxed_rescued = 0
        rejected: List[Dict] = []
        out: List[CastCandidate] = []
        for agg in heads:
            reason = self._reject(agg)
            if reason:
                if not (relax and reason == "rare" and self._evidence(agg) >= 1):
                    self._dropped[reason] += 1
                    rejected.append({
                        "name": agg.name, "lines": agg.lines,
                        "direct_hints": self._evidence(agg), "occurrences": agg.occ,
                        "chapters": agg.chapter_count, "reason": reason,
                    })
                    continue
                self._relaxed_rescued += 1
            for other, cnt in agg.co.items():
                mapped = key_to_name.get(other, other)
                if mapped and mapped != agg.name:
                    agg.final_co[mapped] += cnt
            out.append(self._to_candidate(agg))
        rejected.sort(key=lambda r: (-r["lines"], r["name"]))
        self._rejected_total = len(rejected)
        self._rejected = rejected[:MAX_REJECTED_AUDIT]
        out.sort(key=lambda c: (-c.lines_count, c.name))
        for i, cand in enumerate(out):
            cand.role_type = "hero" if i == 0 else "supporting"
        out.append(self._narrator_candidate())
        return out

    # --------------------------------------------------------------- 归属启发
    def _dialogue_segments(self, text: str) -> List[Dict]:
        """DialogueDetector 结果按 (type,start,end) 去重（多引号对会重复命中）。"""
        seen = set()
        out: List[Dict] = []
        for seg in self.detector.detect_dialogues(text):
            key = (seg.get("type"), seg.get("start"), seg.get("end"))
            if key in seen:
                continue
            seen.add(key)
            out.append(seg)
        out.sort(key=lambda s: (int(s.get("start") or 0), int(s.get("end") or 0)))
        return out

    def _resolve_speaker(self, text: str, seg: Dict) -> Optional[Tuple[str, str, bool, str]]:
        """一条对白 -> (聚合 key, 原始写法, 是否引号直连, 证据级别)。None = 无法归属，计为噪声。

        证据级别 evidence（CAST-FIX D 放大器修复的核心）：
        - "hint"      说话人来自 role_hint/前缀/后缀这类"贴着引号"的归属启发；
        - "mention"   靠 "名字主语 + 同句代词" 的性别指代档案归属（WF-01 主角只被
                      代词指代也能落位，档案里必须是叙述段真做过主语的人名形态词）；
        - "backsearch" 90 字窗口里最后一次出现的**任意已登记写法**——纯字符串命中，
                      不含任何"这个人在说话"的证据。
        direct=True 仅当 evidence=="hint"。只有 backsearch 证据的写法不得成为角色
        （见 _reject 的 min_evidence 门限）：一条坏提示 + 回溯放大 = 数百行"戏份"。
        """
        start = int(seg.get("start") or 0)
        end = int(seg.get("end") or 0)
        # 只看离对白最近的分句，避免上一句的动词短语被误截成人名
        pre = self._tail_clause(text[max(0, start - self.window):start])
        post = self._head_clause(text[end:end + self.window])

        hinted: List[str] = []
        hint = self._norm_hint(seg.get("role_hint"))
        if hint:
            if not self._is_post_hint(hint, post) or self._speech_neighborhood(hint, post):
                hinted.append(hint)
        m = self._rightmost(self._pre_name_verb, pre)      # 张三说 / 张三笑了笑问
        if m:
            hinted.append(self._norm_hint(m.group(1)))
        m = self._pre_colon.search(pre)                    # 张三：
        if m:
            hinted.append(self._norm_hint(m.group(1)))
        m = self._post_name_verb.match(post)               # “……”，张三说道
        if m:
            hinted.append(self._norm_hint(m.group(1)))
        m = self._post_verb_name.match(post)               # “……”说道，张三
        if m:
            hinted.append(self._norm_hint(m.group(1)))

        for raw in hinted:
            accepted = self._accept(raw)
            if accepted:
                return (accepted[0], accepted[1], True, "hint")

        # 只有代词提示或完全没有提示 -> 先按性别找"最近提及"，再退回到已知写法
        gender = self._pronoun_cue(post) or self._pronoun_cue(pre)
        if gender:
            mentioned = self._recent_mention(gender, start)
            if mentioned:
                agg = self._aggs.get(mentioned[0])
                if agg is not None:
                    agg.pronoun_resolved += 1
                self._mention_resolved += 1
                return (mentioned[0], mentioned[1], False, "mention")

        back = text[max(0, start - self.backsearch):start]
        resolved = self._most_recent_name(back)
        if resolved:
            agg = self._aggs.get(resolved[0])
            if agg is not None:
                agg.pronoun_resolved += 1
            return (resolved[0], resolved[1], False, "backsearch")

        # 弱档: 引号**后窗**首小句以 他/她/它 作主语（"她把纸角举到眼前"=同一人续讲）
        # 前窗不作弱档: "她抬手叩了三扇门环。“谁啊？”" 里前窗主语不是答话人
        weak = self._pronoun_subject(post)
        if weak:
            mentioned = self._recent_mention(weak, start)
            if mentioned:
                self._mention_resolved += 1
                return (mentioned[0], mentioned[1], False, "mention")
        return None

    # ------------------------------------------------------- WF-01 归属信号
    @staticmethod
    def _is_post_hint(raw: str, post: str) -> bool:
        """提示是否就贴在引号后窗开头（后窗是叙述残片误截的高发位置）。"""
        if not raw or not post:
            return False
        return post.lstrip(PUNCT_STRIP).startswith(raw)

    def _speech_neighborhood(self, raw: str, clause: str, ahead: int = 3) -> bool:
        """候选写法之后 ahead 字内是否出现说话动词（"小满小声说" 是，"里头传来拖长" 否）。"""
        idx = (clause or "").find(raw or "")
        if idx < 0:
            return False
        tail = clause[idx + len(raw): idx + len(raw) + max(1, int(ahead))]
        return bool(self._verbs_re.search(tail or ""))

    def _pronoun_cue(self, clause: str) -> Optional[str]:
        """小句里 "他/她/它 + 说话线索" 的性别信号（她说 / 她的声音 / 他问道）。"""
        if not clause:
            return None
        m = self._pronoun_cue_re.search(clause)
        if not m:
            return None
        return GENDER_PRONOUN_CHARS.get(m.group(1))

    def _pronoun_subject(self, clause: str) -> Optional[str]:
        """弱信号：小句直接以 他/她/它 作主语（"她把纸角举到眼前"）-> 性别。

        复数/近指（他们/这他…）与 "他的" 所有格之外的情况一律不认。
        """
        head = (clause or "").lstrip(PUNCT_STRIP)
        gender = GENDER_PRONOUN_CHARS.get(head[:1])
        if not gender:
            return None
        if head[1:2] in ("们", "俩", "个", "这", "那"):
            return None
        return gender

    def _is_person_like(self, raw: str, key: str) -> bool:
        """指代档案专用门限：候选写法须像"人名形态"（比 _looks_like_name 更严）。

        叙述段的小句主语大量是时间/处所/普通名词（第二天、后院堆着、雨点），
        只凭"后面出现她/他"就把它们升格成角色太危险。故要求：
        - 首字是常见姓氏或 老/小/大/阿 称谓前缀；或
        - 该写法本就已由直连线索登记过（已知名/_reconcile 命中）；或
        - 它作为叙述小句主语**重复出现**（>=2 次）——生僻姓氏不在 SURNAMES
          子集里，但真名会反复作主语，状语短语通常一次性出现。
        """
        if not raw:
            return False
        if raw[:1] in COMMON_SURNAMES or raw[:1] in HONORIFIC_PREFIXES:
            return True
        if any(raw.startswith(c) for c in COMPOUND_SURNAMES):
            return True
        if key in self._aggs or raw in self._surface_key:
            return True
        if any(surface and surface in raw for surface in self._surface_key):
            return True
        return self._subject_seen[raw] >= 2

    def _scan_mentions(self, body: str, base: int = 0) -> None:
        """叙述段：登记 "小句主语(名字) + 同句代词作小句主语" 的性别指代档案。

        只对叙述段生效（引号内文本不参与），因此 "林先生……" 这类对白里的
        称谓不会被当成新角色。方位词/叙述动词尾巴由 _looks_like_name 剔除，
        时间/处所类主语由 _is_person_like 剔除。
        """
        if not body:
            return
        pending: Optional[Tuple[int, str, str]] = None    # (位置, key, 写法)
        for m in self._clause_token.finditer(body):
            token = m.group(0)
            pos = base + m.start()
            head = token[:1]
            if head in GENDER_PRONOUN_CHARS:
                if token[1:2] in ("们", "俩", "个"):        # 他们/她俩：复数，不作单指线索
                    continue
                if pending and pos - pending[0] <= self.backsearch:
                    self._remember_mention(pending[1], pending[2], pending[0],
                                           GENDER_PRONOUN_CHARS[head])
                continue
            subj = self.detector.clause_subject(token)
            if not subj:
                continue
            accepted = self._accept(subj)
            if not accepted:
                continue
            if (accepted[1] not in self._subject_seen
                    and len(self._subject_seen) >= MAX_SUBJECT_KEYS):
                continue
            if self._subject_seen[accepted[1]] < MAX_SUBJECT_SEEN:
                self._subject_seen[accepted[1]] += 1
            if self._is_person_like(accepted[1], accepted[0]):
                pending = (pos, accepted[0], accepted[1])

    def _remember_mention(self, key: str, surface: str, pos: int, gender: str) -> None:
        """写入性别档案与本章提及序列（两处都有界，长书内存不随章数增长）。"""
        if not key:
            return
        if key not in self._mention_gender and len(self._mention_gender) < MAX_MENTIONS:
            self._mention_gender[key] = gender
        # 按扫描顺序追加：位置天然递增，_recent_mention 逆序取到的即最近一次提及
        if len(self._chapter_mentions) < MAX_CHAPTER_MENTIONS:
            self._chapter_mentions.append((pos, key, surface))

    def _recent_mention(self, gender: str, pos: int) -> Optional[Tuple[str, str]]:
        """回溯窗口内最近提及、性别一致的写法 -> (key, surface)。"""
        for entry_pos, key, surface in reversed(self._chapter_mentions):
            if entry_pos > pos:
                continue
            if pos - entry_pos > self.backsearch:
                break
            if self._mention_gender.get(key) == gender:
                return (key, surface)
        return None

    def _most_recent_name(self, window_text: str) -> Optional[Tuple[str, str]]:
        """回溯窗口里最后出现（离对白最近）的已知角色写法。"""
        best_pos = -1
        best: Optional[Tuple[str, str]] = None
        if not window_text:
            return None
        for surface, key in self._surface_key.items():
            pos = window_text.rfind(surface)
            if pos < 0:
                continue
            end = pos + len(surface)
            if end > best_pos:
                best_pos = end
                best = (key, surface)
        return best

    # --------------------------------------------------------------- 写法过滤
    def _accept(self, raw: Optional[str]) -> Optional[Tuple[str, str]]:
        """候选写法 -> (key, surface)；脏提示优先对齐已知角色名。"""
        if not raw or raw in PRONOUNS:
            return None
        matched = self._reconcile(raw)
        if matched:
            return matched
        name = self._shape_name(raw)
        if not name:
            return None
        return self._canonical_key(name), name

    def _shape_name(self, raw: str) -> Optional[str]:
        """写法裁剪为姓名形态（None = 不是人名，宁缺毋滥）。

        CAST-FIX(A/B/C): 旧实现只有"不像停用词就算人名"的负向判定，
        闭类片段（时候/眼睛/我知/他用法语）与 5-6 字窗口残片全部过门。
        现要求正向形态判定 is_person_shaped。

        唯一的回退：整串以状语标记 地 收尾时（"康克林冷冷地/柯尼希客气地"），
        这类串只可能是窗口正则把 "姓名 + 状语" 整块截走的产物——汉语姓名不可能
        以 地 收尾，故此时允许取"最长的人名形态前缀"；前缀之外一律不试，
        避免把 "伯特边读边" 这类无标记残片硬削成 伯特。
        """
        if not raw:
            return None
        if self._looks_like_name(raw):
            return raw
        if not raw.endswith("地"):
            return None
        for i in range(len(raw) - 1, 1, -1):
            head = self._clean_name(raw[:i]).strip(PUNCT_STRIP)
            if len(head) >= 2 and self._looks_like_name(head):
                return head
        return None

    def _reconcile(self, raw: str) -> Optional[Tuple[str, str]]:
        """已知名写法是 raw 的**前缀**时归到已知名（"小满小声" -> "小满"）。

        WF-01: 写法之后紧跟否定词（"林晚晴**没**接话"）说明该句明示此人没说话，
        这类提示不作归属证据，跳过对齐后继续走代词/最近提及回溯。
        CAST-FIX(E): 旧口径允许已知写法出现在 raw 的**任意位置**，于是"残片吞全名"
        ——先被登记的 克林 会把 康克林 的台词全记到 克林 名下，角色表里就出现
        书里根本不存在的名字。故只认前缀（写法本身是完整姓名开头的脏尾巴）。
        """
        best: Optional[Tuple[str, str]] = None
        for surface, key in self._surface_key.items():
            if not surface or surface == raw or not raw.startswith(surface):
                continue
            tail = raw[len(surface):]
            if tail[:1] in NEGATION_HEADS:
                continue
            if best is None or len(surface) > len(best[0]):
                best = (key, surface)
        return best

    def _looks_like_name(self, raw: str) -> bool:
        """候选写法是否像人名（WF-01 叙述句式门限 + CAST-FIX 正向姓名形态门限）。

        剔除的是**通用词类**（方位/处所词开头、叙述趋向动词尾巴、代词/虚词/状语
        标记字、量词开头、姓名区间外的长度），并要求正向姓名形态（姓氏/称谓前缀/
        职务通称结尾/译名尾字/间隔号复合名）——都不是样本里出现过的具体字符串。

        实体在模块级 `looks_like_name`：dialogue_detector 的"动作后窗"通道要用同一道
        门（PY-338），而这个文件已经 import 了 dialogue_detector，反向 import 会成环。
        """
        return looks_like_name(raw, max_len=self.max_name_len)

    @staticmethod
    def _rightmost(regex, window: str):
        last = None
        for m in regex.finditer(window or ""):
            last = m
        return last

    _CLAUSE_SPLIT = re.compile(r"[。！？!?；;]\s*")

    @classmethod
    def _tail_clause(cls, chunk: str) -> str:
        """窗口尾部：只保留最后一个分句（离对白最近）。"""
        parts = [p for p in cls._CLAUSE_SPLIT.split(chunk or "") if p.strip()]
        return parts[-1] if parts else ""

    @classmethod
    def _head_clause(cls, chunk: str) -> str:
        """窗口头部：只保留第一个分句（紧跟对白）。"""
        parts = [p for p in cls._CLAUSE_SPLIT.split(chunk or "") if p.strip()]
        return parts[0] if parts else ""

    _QUOTE_SPAN = re.compile(r"[“\"「『][^\u201d”」』]*[\u201d”」』]?")
    _PRONOUN_ONE = re.compile(r"[他她它]")
    # 小句边界：只有紧跟其后的 他/她/它 才算"这句话的主语"。
    # 逗号算边界（"玛莉说，她转过身"），宾语位（"看着他""拿给他"）因前一个字是
    # 动词/介词而天然被排除。
    _CLAUSE_BOUNDARY = set("。！？!?；;…，,：:\n\r\t （）()【】《》—-…")

    @classmethod
    def _mask_quoted(cls, window: str) -> str:
        """把引号里的台词换成等长空格：绝对偏移不变，窗口里只剩叙述。"""
        if not window:
            return ""
        chars = list(window)
        for m in cls._QUOTE_SPAN.finditer(window):
            for i in range(m.start(), m.end()):
                chars[i] = " "
        return "".join(chars)

    def _subject_pronoun_counts(self, masked: str, base: int, floor: int):
        """掩好引号的窗口 -> (he, she, it, new_floor)。

        base = 窗口首字符在本章里的偏移；floor 之前的位置视为"已被前一条对白计过"。
        """
        counts = {"他": 0, "她": 0, "它": 0}
        for m in self._PRONOUN_ONE.finditer(masked):
            pos = base + m.start()
            if pos <= floor:
                continue
            prev = masked[m.start() - 1] if m.start() > 0 else ""
            if prev and prev not in self._CLAUSE_BOUNDARY:
                continue                       # 宾语位：说的是别人
            if masked[m.end():m.end() + 1] in ("们", "俩", "个", "这", "那"):
                continue                       # 复数/近指不是单指性别证据
            counts[m.group(0)] += 1
            floor = pos
        return counts["他"], counts["她"], counts["它"], floor

    def _count_pronouns(self, agg: _Agg, text: str, seg: Dict, name: str) -> None:
        """统计该角色归属句附近的 他/她/它，供画像与代词归属兜底推断性别。

        PY-336 前的口径是"名字之后到引号之前的**所有** 他/她/它"，三条系统性偏置
        把跟男主对手戏最多的女主打成了男声（实测《谍影重重》玛莉 he=400/she=316）：
        宾语位（"她正看着他"）计的是他、台词里（引号内）的代词计给了说话人、
        同一段叙述窗口又被此人后续每句对白重复计一次。现只数小句主语位、
        屏蔽引号、并按绝对偏移去重。
        """
        start = int(seg.get("start") or 0)
        pre = text[max(0, start - self.backsearch):start]
        if name not in pre:
            return
        offset = max(0, start - self.backsearch) + pre.rfind(name) + len(name)
        masked = self._mask_quoted(text[offset:start])
        he, she, it, floor = self._subject_pronoun_counts(masked, offset, agg.pronoun_floor)
        agg.he += he
        agg.she += she
        agg.it += it
        agg.pronoun_floor = floor
        # WF-01: 已归属过对白的写法也进指代档案（画像性别口径同上，取多数）
        if agg.name and agg.name not in self._mention_gender:
            genders = {"male": agg.he, "female": agg.she, "neuter": agg.it}
            best = max(genders, key=lambda g: genders[g])
            if genders[best] and genders[best] > (agg.he + agg.she + agg.it) / 2:
                if len(self._mention_gender) < MAX_MENTIONS:
                    self._mention_gender[agg.name] = best

    def _norm_hint(self, raw: Optional[str]) -> Optional[str]:
        if not raw:
            return None
        name = raw.strip(PUNCT_STRIP)
        if not name:
            return None
        name = self._clean_name(name).strip(PUNCT_STRIP)
        if not name or len(name) > self.max_name_len:
            return None
        return name

    def _clean_name(self, name: str) -> str:
        """剥掉动词尾巴：'王掌柜笑道' -> '王掌柜'。"""
        changed = True
        while changed and name:
            changed = False
            for verb in self._verbs:
                if name.endswith(verb) and len(name) > len(verb):
                    name = name[: -len(verb)]
                    changed = True
                    break
        return name

    # --------------------------------------------------------------- 别名合并
    def _core(self, name: str) -> str:
        """老周->周, 周队长->周, 小满->满, 周建国->周建国。"""
        for title in TITLE_SUFFIXES:
            if name.endswith(title) and len(name) > len(title):
                return name[: -len(title)]
        if name[:1] in HONORIFIC_PREFIXES and len(name) >= 2:
            return name[1:]
        return name

    def _canonical_key(self, name: str) -> str:
        """聚合 dict 的 key：姓氏简称形态（周/老周/周队长）统一落到单姓 key。"""
        core = self._core(name)
        if len(core) == 1 and core in SURNAMES:
            return core
        return name

    @staticmethod
    def _is_generic_title(name: str) -> bool:
        """写法本身就是"职务/身份通称"（局长/医生/军官/上校…）？

        通称是**普通名词**，"修饰语+通称"（副局长/外科医生/海军军官）指另一个人，
        因此通称不参与 CAST-FIX(F) 的窗口残片归并。
        """
        if not name:
            return False
        if name in PERSON_GENERIC or name in TITLE_SUFFIXES:
            return True
        return any(name.endswith(t) and len(name) > len(t) for t in PERSON_TITLES)

    @staticmethod
    def _is_person_head(head: str) -> bool:
        """职务后缀前面的那一截是否**像人名**（王掌柜/张队长的 王/张 是，副/外/前任 不是）。

        只认三类：单字常见姓氏、老/小/大/阿 称谓前缀、以姓氏开头的多字写法。
        副/代/前/总/外/高级 这类修饰语一律排除——"修饰语+职务"指另一个人。
        """
        if not head:
            return False
        if head in SURNAMES or head in HONORIFIC_PREFIXES:
            return True
        return len(head) > 1 and head[0] in SURNAMES

    @staticmethod
    def _is_adverbial_shape(name: str) -> bool:
        """写法是否**可能**是"称谓前缀 + 性质语素"的话语状语（只看形态，不看分布）。

        三条负向豁免，宁可放过也不误杀真角色：
          - 第二字是常见姓氏 -> 老周/小陈/阿顾 是人名简称；
          - 写法本身是职务/身份通称 -> 老板/老头/老人 是能开口的说话人；
          - 长度不是 2 -> 该形态不成立。
        形态命中不等于判死：真昵称（阿布/大卫）也是这副模样，
        最终定性要看 `_reject` 里的 "写法+说话动词" 语料占比。
        """
        if len(name) != 2 or name[0] not in HONORIFIC_PREFIXES:
            return False
        if name[1] in COMMON_SURNAMES:
            return False
        return not CastExtractor._is_generic_title(name)

    def _lines_floor(self) -> int:
        """CAST-FIX(F): 戏份下限 = max(min_lines, ceil(min_line_share x 全书对白行数))。

        长书才抬门限（对白 <500 行时该式子 <= min_lines），短输入与既有 C1/C2/C4/C6
        夹具行为逐字节不变；返回的是"每一条保留项都能解释"的门限，不是 top-N 名额。
        """
        if not self.min_line_share or self._dialogue_lines <= 0:
            return self.min_lines
        return max(self.min_lines,
                   int(math.ceil(self.min_line_share * self._dialogue_lines)))

    def _merge_aliases(self) -> Tuple[List[_Agg], Dict[str, str]]:
        """五层归并 + 结果聚合：
        1) core 相同（老周/周队长/周）2) 姓氏单字 -> 同姓戏份最多的全名
        3) 唯一持有者的职务称谓 4) 前缀/后缀窗口残片 5) 中段包含式截断写法
        （CAST-RESIDUAL，主机按性别信号唯一确定，歧义只留痕不归并）。
        """
        aggs = self._aggs
        keys = list(aggs)
        parent = {k: k for k in keys}

        def find(k: str) -> str:
            while parent[k] != k:
                parent[k] = parent[parent[k]]
                k = parent[k]
            return k

        def union(a: str, b: str) -> None:
            ra, rb = find(a), find(b)
            if ra == rb:
                return
            best = max((ra, rb), key=lambda k: (aggs[k].lines, len(k), k))
            other = rb if best == ra else ra
            parent[other] = best

        # 1) core 相同（老周 / 周队长 / 周 -> 同组）
        by_core: Dict[str, List[str]] = defaultdict(list)
        for k in keys:
            by_core[self._core(k)].append(k)
        for group in by_core.values():
            for k in group[1:]:
                union(group[0], k)

        # 2) 姓氏单字组 -> 同姓氏戏份最多的全名（周 -> 周建国）
        by_surname: Dict[str, List[str]] = defaultdict(list)
        for k in keys:
            core = self._core(k)
            if len(core) == 1 and core in SURNAMES:
                by_surname[core].append(k)
        for surname, bare_keys in by_surname.items():
            fulls = [k for k in keys
                     if k not in bare_keys and len(self._core(k)) > 1
                     and self._core(k)[0] == surname]
            if not fulls:
                continue
            target = max(fulls, key=lambda k: (aggs[k].lines, len(k), k))
            for b in bare_keys:
                union(b, target)

        # 3) 单独出现的职务称谓（"掌柜"）-> 全书唯一持有者才归并。
        #    CAST-FIX(F): 持有者写法必须是**姓氏/称谓前缀 + 职务**（王掌柜/张队长 =
        #    同一个人）；"修饰语 + 职务"（副局长/外科医生/前任法官）是**另一个人**，
        #    不参与归并——否则真角色被并掉一条（句数守恒回退）。
        for k in keys:
            if k not in TITLE_SUFFIXES:
                continue
            owners = {o for o in keys if o != k and o.endswith(k) and len(o) > len(k)
                      and self._is_person_head(o[: len(o) - len(k)])}
            if len(owners) == 1:
                union(k, owners.pop())

        # 4) CAST-FIX(F): 窗口残片归并（跨章别名合并缺失项）。
        #    长写法 a **以 b 开头或以 b 结尾**（b 长度 >=2）时，a 只可能是同一个
        #    人在不同引号窗口里被截出来的残片：在伯恩/·伯恩/看见伯恩/伯恩警觉 -> 伯恩，
        #    山大·康克林/·康克林 -> 康克林，蒂文斯 -> 史蒂文斯，玛莉·圣雅各 -> 玛莉。
        #    通称/职务词一律跳过：副局长/局长、外科医生/医生、海军军官/军官 是**不同人**，
        #    "X+职务" 是修饰短语而不是别名（真凶案例里的 个职员客气地 同属此类）。
        for a in keys:
            if self._is_generic_title(a):
                continue
            for b in keys:
                if a == b or len(b) < 2 or len(a) <= len(b):
                    continue
                if not (a.startswith(b) or a.endswith(b)):
                    continue
                if self._is_generic_title(b):
                    continue
                union(a, b)

        # 5) CAST-RESIDUAL: 中段包含式归并——第 4 层的 startswith/endswith 天生够不着
        #    "夹在全名中间"的截断写法。_shape_name 从 "圣雅各平静地" 的 地 尾巴救回
        #    半截译名 圣雅，它既不是 约翰·圣雅各 的前缀也不是后缀，于是 93 行戏挂在
        #    一个全书从不单独成词（242 次命中全部落在 圣雅各 之内）的人名上。
        #    规则：短写法 b **完整出现在**长写法 a 的中间（两端都不是边界）、
        #    两者都不是职务通称（副局长/局长 是两个人）时，b 只是 a 的一个截断写法。
        #    主机由"性别信号一致"唯一确定；一致的主机有多个、或性别互相冲突时
        #    **不做归并**，但每一次判定（含被否掉的候选主机）都留痕成 alias_candidates
        #    并带置信度——归并与否都可复核，绝不静默丢弃（模块 docstring 别名合并条）。
        def stage_top(cands: List[str]) -> str:
            return max(cands, key=lambda k: (aggs[k].lines, len(k), k))

        forced: Dict[str, str] = {}          # b -> 归并后指定的显示名主机
        for b in keys:
            if len(b) < 2 or self._is_generic_title(b):
                continue
            hosts = [a for a in keys
                     if a != b and len(a) > len(b) and not self._is_generic_title(a)
                     and 0 < a.find(b) and a.find(b) + len(b) < len(a)]
            if not hosts:
                continue
            gb = self._pronoun_gender(aggs[b])
            agree = [a for a in hosts if gb and self._pronoun_gender(aggs[a]) == gb]
            host: Optional[str] = None
            if len(agree) == 1:
                host, conf, why = agree[0], 0.9, "containment_unique_gender_host"
            elif agree:
                host, conf, why = stage_top(agree), 0.6, "containment_gender_hosts_reranked"
            elif gb is None and len(hosts) == 1:
                host, conf, why = hosts[0], 0.75, "containment_unique_host_no_gender_signal"
            else:
                conf, why = 0.4, "containment_ambiguous_gender_hosts"
            ranked = sorted(hosts, key=lambda k: (-aggs[k].lines, -len(k), k))
            for a in ranked[:MAX_ALIAS_CANDIDATES]:
                chosen = host is not None and a == host
                aggs[b].alias_candidates.append({
                    "surface": b,
                    "host": a,
                    "merged": chosen,
                    "confidence": conf if (host is None or chosen) else 0.3,
                    "reason": why if (host is None or chosen) else "containment_alternative_host",
                    "surface_lines": int(aggs[b].lines),
                    "host_lines": int(aggs[a].lines),
                })
            if host:
                forced[b] = host
                union(b, host)

        members: Dict[str, List[str]] = defaultdict(list)
        for k in keys:
            members[find(k)].append(k)

        heads: List[_Agg] = []
        key_to_name: Dict[str, str] = {}
        for root, group in members.items():
            ordered = sorted(group, key=lambda k: (-aggs[k].lines, -len(k), k))
            head = aggs[ordered[0]]
            for other in ordered[1:]:
                head.absorb(aggs[other], self.max_quotes)
            # CAST-RESIDUAL: 中段包含式归并的落位显示名 = 全名主机（残片戏份再多也不改名）
            pinned = sorted({forced[k] for k in group if k in forced},
                            key=lambda s: (-head.surfaces.get(s, 0), -len(s), s))
            if pinned:
                head.display_override = pinned[0]
            head.name = head.canonical_name()
            for surface in head.surfaces:
                for title in TITLE_SUFFIXES:
                    if surface.endswith(title) and len(surface) > len(title):
                        head.titles[title] += 1
            for k in group:
                key_to_name[k] = head.name
            heads.append(head)
        return heads, key_to_name

    @staticmethod
    def _evidence(agg: "_Agg") -> int:
        """非回溯归属证据数：引号直连提示 + "名字主语+同句代词"最近提及。"""
        return int(agg.hinted) + int(agg.mention_backed)

    @staticmethod
    def _pronoun_gender(agg: Optional["_Agg"]) -> Optional[str]:
        """写法自身的性别信号：他/她 计数的**严格多数**，口径同 _count_pronouns 写
        指代档案那一段（它/its 不参与——中性代词决定不了人的性别）。
        无多数或零命中一律返回 None = "没有信号"，与"确定是男/女"区分开，
        供第 5 层包含式归并判定主机是否唯一。
        """
        if agg is None:
            return None
        total = int(agg.he) + int(agg.she)
        if total <= 0:
            return None
        if agg.he * 2 > total:
            return "male"
        if agg.she * 2 > total:
            return "female"
        return None

    def _reject(self, agg: _Agg) -> Optional[str]:
        """噪声过滤：返回丢弃原因（None = 保留）。"""
        name = agg.name
        if not name:
            return "empty"
        if name in PRONOUNS:
            return "pronoun"
        if len(name) > (MAX_COMPOUND_NAME_LEN if "·" in name else MAX_PLAIN_NAME_LEN):
            return "too_long"
        if not re.search(r"[\u4e00-\u9fa5A-Za-z]", name):
            return "non_name"
        if agg.lines < self._lines_floor():
            return "rare"
        # CAST-FIX(G): 全书高频出现、却几乎不落在说话位 = 普通词/副词，不是人名
        # （房间/马上/万一/老实/严格/方言 在 155 万字里出现数千次，说话位证据 <1%）
        if (self.min_name_share and agg.occ >= self.min_occurrences
                and agg.hinted < self.min_name_share * agg.occ):
            return "common_word"
        # CAST-RESIDUAL: common_word 抓不到"贴着引号的话语状语"——detector 的 role_hint
        # 直接计入 hinted，而状语恰恰就长在引号前面（真凶案例 13/43 ≈ 30% >> 2% 门限）。
        # 能区分它和真名的不是"有多少次落在说话位"，而是"它**全部**的语料现身是否
        # 挤在同一个状语框架里"：真名散布在主语/宾语/定语各处，状语写法不会。
        # 稀有写法（occ < min_occurrences）一律放过，短文夹具与一次性专名不受影响。
        if (self._is_adverbial_shape(name) and agg.occ >= self.min_occurrences
                and agg.mod_speech >= ADVERBIAL_MOD_SHARE * agg.occ):
            return "honorific_adverb"
        # CAST-FIX(D): 戏份(lines)含 90 字回溯放大，只有"回溯过一次"的写法
        # 不能凭 lines 过门限——必须至少有一条直连/最近提及证据。
        if self._evidence(agg) < self.min_evidence:
            return "no_evidence"
        return None

    def _to_candidate(self, agg: _Agg) -> CastCandidate:
        co_names = [n for n, _c in sorted(agg.final_co.items(), key=lambda kv: (-kv[1], kv[0]))]
        aliases = agg.other_surfaces()
        titles = [t for t, _n in agg.titles.most_common()]
        display = agg.canonical_name()
        # 归并进来的主机写法改写成"本角色的最终显示名"，读起来才是"残片并给了我"
        alias_candidates = [dict(c, host=(display if c.get("host") in agg.surfaces
                                          else c.get("host")))
                            for c in agg.alias_candidates]
        return CastCandidate(
            name=display,
            role_type="supporting",
            aliases=aliases,
            alias_candidates=alias_candidates,
            lines_count=agg.lines,
            occurrences=agg.occ,
            chapters_touched=list(agg.chapter_samples),
            chapters_count=agg.chapter_count,
            sample_quotes=list(agg.quotes),
            co_speakers=co_names,
            he_count=agg.he,
            she_count=agg.she,
            it_count=agg.it,
            titles=titles,
            pronoun_resolved=agg.pronoun_resolved,
            direct_hints=self._evidence(agg),
            is_narrator=False,
        )

    def _narrator_candidate(self) -> CastCandidate:
        return CastCandidate(
            name=NARRATOR,
            role_type="narrator",
            lines_count=self._narration.lines,
            chapters_touched=list(self._narration.chapter_samples),
            chapters_count=self._narration.chapter_count,
            sample_quotes=list(self._narration.quotes),
            is_narrator=True,
        )

    # --------------------------------------------------------------- 统计
    def stats(self) -> Dict:
        return {
            "chapters": self._chapters_seen,
            "chars": self._chars_seen,
            "raw_candidates": len(self._aggs),
            "dialogue_lines": self._dialogue_lines,
            "narration_lines": self._narration.lines,
            "unresolved_lines": self._unresolved,
            "mention_resolved": self._mention_resolved,
            "short_text_relaxed": self._relaxed_rescued,
            "dropped": dict(self._dropped),
            # ---- CAST-FIX: 本轮生效判据 + 被排除项可审计清单（验收规则 2）----
            "lines_floor": self._lines_floor(),
            "min_line_share": self.min_line_share,
            "min_occurrences": self.min_occurrences,
            "min_name_share": self.min_name_share,
            "rejected_total": self._rejected_total,
            "rejected": list(self._rejected),
        }

    @staticmethod
    def _clip(text: str, limit: int = 60) -> str:
        return re.sub(r"\s+", " ", text).strip()[:limit]


def looks_like_name(raw: str, max_len: int = 6) -> bool:
    """判名门（词类否决 + 正向姓名形态）。默认长度上限与抽取器的 `max_name_len` 一致。

    放在类外文件末尾：dialogue_detector 的"动作后窗"通道要用同一道门（PY-338），
    而这个文件已经 import 了 dialogue_detector，反向 import 会成环——所以只能由
    cast 侧把门**注入**出去，而插在类中间会把后面的方法整批掉出类外。
    """
    if len(raw) < 2 or len(raw) > max_len:
        return False
    if raw in PRONOUNS:
        return False
    # 虚词字不在这里否决：`is_person_shaped`（最后一道门）已按位置扫过同一批字，
    # 并带"姓氏开头 + 名字内部语素"的豁免（PY-335）。这里再抄一份严格的会把豁免顶掉。
    if any(w in raw for w in STOP_WORDS):
        return False
    if any(raw.startswith(head) for head in LOCATIVE_HEADS):
        return False
    if any(raw.endswith(tail) for tail in NARRATION_TAILS):
        return False
    return is_person_shaped(raw)

