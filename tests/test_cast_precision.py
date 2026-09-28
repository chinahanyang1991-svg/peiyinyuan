"""
#91 CAST-FIX 回归测试：角色分析"出错误角色 / 多出一堆不该有的角色"

取证依据（Phase 1，只读探针 agent-ops/castfix/probe_cast_precision.py 对被测真书
4a92f0a4（杰森·伯恩之谍影重重，173 章 / 155 万字）的逐条归因，真书生产结果
project=51621d95 role_count=844，extractor.stats.raw_candidates=1266）：

  噪声来源 A  闭类片段被当人名：时候/现在/时间/应该/觉得/或者/仿佛/似乎/一边/
              明天/第二/语气/眼睛/电话/柜台/昏暗/剩下/坦白/开口/老板/老实
  噪声来源 B  代词框架残片：我知/他知/她知/天知/雷知/该知/底想/想知/跟我/听我/
              照我/问他/别再/他故意/她终于/我再/他用法语/她轻声地/康克林冷冷地
  噪声来源 C  5-6 字窗口残片：个职员客气地/她睡意浓浓地/他抽出一叠大/
              几句话告诉他/各博士冷冷地/伯特边读边
  放大器 D    _most_recent_name 的 90 字回溯把大量无提示对白记到"曾经被登记过的
              任意写法"上（时候 B=131 / 卡洛斯 B=114 / 应该 B=66 / 眼睛 B=32），
              而 _reject 只看 lines（含回溯行），一条坏提示因此滚成几百行"戏份"
  错名 E      _reconcile 允许"已知写法是候选的子串"即归并，方向可以是残片吞全名：
              已知 克林 -> 康克林 的台词记到 克林；已知 蒂文斯 -> 史蒂文斯 同理，
              于是列表里出现"克林/蒂文斯/维耶/计师"这类假角色名（角色是错误的）

fixture 全部为**自造文本**（措辞与真书不同），证明修的是通用规则而不是样本串。
"""
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from modules.cast_analyzer.cast_extractor import NARRATOR, CastExtractor  # noqa: E402

# 新符号（本次修复引入）在用例内导入：让"未实现"与"行为不符"两类红都能单独暴露


# ---------------------------------------------------------------- 噪声语料
# 与真书同类句式（自造）：心理/情态动词 + 道/说、时间名词、连词、状语残片
NOISE_CHAPTERS = [
    (0, "“我不知道那封信是谁寄来的。”她低声说，手指还按在信封上。\n"
        "“他知道的事，从来不肯多说一句。”陈默把火钳递给她，“炉子要灭了。”\n"
        "“或者，我们该等等看。”陈默说完，觉得有些不妥，又补了一句：“明天再说吧。”\n"),
    (1, "“你先止血。”医生问这句话的时候，声音有些发抖，眼睛却没有离开伤口。\n"
        "“不碍事。”陈默笑了笑，仿佛并不觉得疼，“电话先别接。”\n"
        "柜台后面的老板娘探出头来，“面要坨了。”\n"),
    (2, "“他一定要跟她商量半天才肯定。”陈默转过头，语气很平，“时候到了你自然知道。”\n"
        "“现在说这些还太早。”她说着，把窗帘拉开一半，外头是昏暗的巷子。\n"),
]

# 真书里被拆成多个角色的同类写法：全名 + "全名+状语" 残片
FRAGMENT_CHAPTERS = [
    (0, "“这笔放款的金额太庞大了。”柯尼希说道，他把文件夹推过桌面。\n"
        "康克林冷冷地看着他，“金额不是我定的。”\n"
        "“我知道。”康克林冷冷地说了一句，“你去问局长。”\n"),
    (1, "“我要看保管人的书面说明。”史蒂文斯说，他转向柯尼希，“你签字了吗？”\n"
        "“签了。”柯尼希回答。史蒂文斯点了点头，史蒂文斯说：“那就归档。”\n"),
]

# 召回护栏：姓氏名 / 称谓前缀 / 职务后缀通称 / 汉译外名 / 间隔号复合译名都要能抽到；
# 老周 与 周建国 是同一人的两种写法（单姓归并），必须合成一条而不是两条角色。
RECALL_CHAPTERS = [
    (0, "“账在这里，你自己数。”老周把算盘推过去，“昨夜库房也有人。”\n"
        "“周队长还没回来。”小满说道，她把灯放在桌上。\n"
        "“灯油快尽了。”小满又小声说了一句。\n"
        "“别声张。”周建国把柜门合上，“这事我盯着。”\n"),
    (1, "“我需要一杯咖啡。”卡洛斯说，他向吧台靠过去。\n"
        "“明早再来一趟。”卡洛斯又吩咐了一句。\n"
        "“那明早再来。”玛莉·圣雅各说，她放下酒杯。\n"
        "“我陪您去。”玛莉·圣雅各又道，她拢了拢外套。\n"
        "“医生已经睡了。”司机在门口低声回答，“船长得另找时间。”\n"
        "“船长此刻不在港上。”司机补充了一句。\n"
        "“这事与阿贵无关。”林晚晴道，她把手插进口袋。\n"
        "林晚晴转过身，“阿贵他不敢来。”\n"),
]

JUNK_NAMES = {
    # A 类：闭类词（时间/情态/连词/名词性通名残片）
    "时候", "现在", "时间", "应该", "觉得", "或者", "仿佛", "似乎", "一边", "明天",
    "第二", "语气", "眼睛", "电话", "柜台", "昏暗", "剩下", "坦白", "开口", "老实",
    "声音", "心里",
    # B 类：代词框架
    "我知", "他知", "她知", "天知", "雷知", "该知", "底想", "想知", "跟我", "听我",
    "照我", "问他", "别再", "他故意", "她终于", "我再", "听你", "们俩", "你要",
    "我想", "他在", "给你",
    # C 类：窗口残片
    "个职员客气地", "她睡意浓浓地", "他抽出一叠大", "几句话告诉他", "各博士冷冷地",
    "伯特边读边", "他用法语", "她轻声地", "康克林冷冷", "史蒂文斯说", "柯尼希说道",
    "老周敲了", "小满端着", "林晚晴转过", "卡洛斯说",
    # E 类：残片吞全名
    "克林", "蒂文斯", "维耶", "计师", "尼希",
}


def build_cast(chapters, **kwargs):
    ex = CastExtractor(**kwargs)
    for idx, text in chapters:
        ex.process_chapter(text, idx)
    return ex, ex.finalize()


def names(cast):
    return [c.name for c in cast]


# ------------------------------------------------------- A/B/C: 噪声不得成角色
def test_closed_class_fragments_are_not_roles():
    """时间词/情态动词/连词/身体与物件名词（真书 844 条里的主要噪声类）不得成角色。"""
    _ex, cast = build_cast(NOISE_CHAPTERS)
    got = set(names(cast))
    hit = got & JUNK_NAMES
    assert not hit, f"闭类片段被当成角色: {sorted(hit)} / 全表 {sorted(got)}"


def test_only_real_speakers_survive_noise_text():
    """噪声语料里唯一真正的说话人是 陈默/她(代词不入表)/医生/老板娘 -> 角色数须很小。"""
    _ex, cast = build_cast(NOISE_CHAPTERS)
    got = [n for n in names(cast) if n != NARRATOR]
    assert "陈默" in got, f"真主角缺席: {got}"
    assert len(got) <= 4, f"噪声语料仍产出 {len(got)} 个角色: {sorted(got)}"


def test_no_parenthetical_adverb_tail_roles():
    """候选名不得以状语尾 地 或说话动词收尾（"康克林冷冷地/史蒂文斯说"式残片）。"""
    _ex, cast = build_cast(FRAGMENT_CHAPTERS + NOISE_CHAPTERS)
    bad = [c.name for c in cast
           if c.name != NARRATOR and (c.name.endswith("地") or c.name.endswith(("说", "道")))]
    assert not bad, f"状语/动词尾巴残片成角色: {bad}"


def test_long_window_fragment_not_a_role():
    """无间隔号的 5-6 字窗口残片不得成为角色（真书 844 条中 C 类的统一特征）。"""
    _ex, cast = build_cast(FRAGMENT_CHAPTERS + NOISE_CHAPTERS + RECALL_CHAPTERS)
    bad = [c.name for c in cast
           if c.name != NARRATOR and len(c.name) > 4 and "·" not in c.name]
    assert not bad, f"超长窗口残片成角色: {bad}"


# ---------------------------------------------------------------- E: 残片吞名
def test_reconcile_is_prefix_only():
    """已知残片不得吞掉全名：_reconcile 只允许"已知写法是候选的前缀"。"""
    ex = CastExtractor()
    ex._surface_key["克林"] = "克林"
    assert ex._reconcile("康克林") is None, "残片 克林 仍在吞全名 康克林（角色名出错）"
    ex._surface_key["蒂文斯"] = "蒂文斯"
    assert ex._reconcile("史蒂文斯") is None, "残片 蒂文斯 仍在吞全名 史蒂文斯"
    # 正向能力不回退：前缀式脏提示仍须对齐到已知名
    ex._surface_key["小满"] = "小满"
    assert ex._reconcile("小满小声说") == ("小满", "小满")
    ex._surface_key["林晚晴"] = "林晚晴"
    assert ex._reconcile("林晚晴没接话") is None      # 否定式提示仍不作证据


def test_full_name_wins_over_fragment():
    """同一人全名/残片混说时，列表里只能有一条，且名字是全名。"""
    _ex, cast = build_cast(FRAGMENT_CHAPTERS)
    got = names(cast)
    assert "克林" not in got and "蒂文斯" not in got, f"残片独立成角色: {got}"
    assert "康克林" in got, f"全名缺席: {got}"
    assert "史蒂文斯" in got, f"全名缺席: {got}"


# --------------------------------------------------------- D: 回溯放大器不得造角
def test_backsearch_alone_cannot_sustain_a_role():
    """只靠 90 字回溯（无一条直连引号归属证据）的写法不得成为角色。"""
    text = ("“明天见。”\n"
            "巷子里的老板收拾着摊子，嘴里哼着小调，声音拖得很长。\n"
            "“真的明天见。”\n"
            "老板把布帘放下，他又哼了一句，雨开始落下来。\n"
            "“这回该收摊了。”\n")
    ex = CastExtractor(min_lines=2)
    ex.process_chapter(text, 0)
    cast = {c.name: c for c in ex.finalize()}
    junk = [n for n in cast if n in ("声音", "他又", "布帘")]
    assert not junk, f"回溯把叙述词升格成角色: {junk}"
    for cand in cast.values():
        if cand.name == NARRATOR:
            continue
        assert (cand.to_dict().get("direct_hints", 0)) >= 1, \
            f"{cand.name} 无直连归属证据仍在角色表"


# ------------------------------------------------------------- 召回护栏（不回退）
def test_person_shapes_still_extracted():
    """姓氏名/称谓前缀/职务通称/汉译外名/复合译名 五类形态都必须抽到，且不重复成角。"""
    _ex, cast = build_cast(RECALL_CHAPTERS)
    got = set(names(cast))
    missing = {"小满", "卡洛斯", "玛莉·圣雅各", "林晚晴", "司机"} - got
    assert not missing, f"真角色被误杀: {sorted(missing)} / 全表 {sorted(got)}"
    zhou = [n for n in got if n.startswith("周")]
    assert len(zhou) == 1, f"同一单姓的多种写法裂成多条角色: {sorted(zhou)}"
    assert zhou[0] == "周建国", f"归并后未取全名写法: {zhou}"
    assert "老周" in next(c for c in cast if c.name == "周建国").aliases
    assert len(got - {NARRATOR}) <= 10, f"召回语料角色数失控: {sorted(got)}"


def test_is_person_shaped_vocabulary_contract():
    """形态判定的两侧都要有护栏：既认姓氏/职务/译名尾字，也拒闭类词。"""
    from modules.cast_analyzer.cast_extractor import TRANSLIT_TAIL_CHARS, is_person_shaped
    assert is_person_shaped("林晚晴") is True
    assert is_person_shaped("老周") is True
    assert is_person_shaped("周队长") is True
    assert is_person_shaped("卡洛斯") is True
    assert is_person_shaped("玛莉·圣雅各") is True
    assert is_person_shaped("医生") is True
    for junk in ("时候", "眼睛", "电话", "语气", "或者", "应该", "仿佛", "我知",
                 "康克林冷冷地", "昏暗", "柜台", "老板的"):
        assert is_person_shaped(junk) is False, f"{junk} 被判成人名形态"
    assert "斯" in TRANSLIT_TAIL_CHARS and "候" not in TRANSLIT_TAIL_CHARS


# --------------------------------------------------------------- 落库规模上限
def _rows(n, start=0):
    return [{"name": f"甲{i}", "role_type": "supporting", "aliases": [],
             "cast": {"lines_count": (n - i) * 3, "direct_hints": max(1, (n - i) // 2)}}
            for i in range(n)]


def test_limit_role_dicts_keeps_narrator_and_top_roles():
    """超过上限时按戏份截断，旁白必留，且返回被裁掉的条数（可观测，不静默）。"""
    from backend.services.cast_service import MAX_PERSISTED_ROLES, limit_role_dicts
    rows = _rows(MAX_PERSISTED_ROLES + 12)
    rows.append({"name": "旁白", "role_type": "narrator", "aliases": [],
                 "cast": {"lines_count": 9999, "direct_hints": 0}})
    kept, dropped = limit_role_dicts(rows, MAX_PERSISTED_ROLES)
    assert len(kept) == MAX_PERSISTED_ROLES, len(kept)
    assert dropped == 13, dropped
    assert any(r["name"] == "旁白" for r in kept), "旁白被裁掉了"
    top = {r["name"] for r in kept}
    assert "甲0" in top and "甲1" in top
    assert "甲" + str(len(rows) - 2) not in top          # 低戏份先出局
    # 顺序保持：列表顺序 = 戏份降序（前端展示顺序与 DB 一致）
    lines = [r["cast"]["lines_count"] for r in kept if r["name"] != "旁白"]
    assert lines == sorted(lines, reverse=True), lines


def test_limit_role_dicts_noop_below_cap():
    from backend.services.cast_service import MAX_PERSISTED_ROLES, limit_role_dicts
    rows = _rows(5)
    kept, dropped = limit_role_dicts(rows, 40)
    assert kept == rows and dropped == 0


def test_extractor_reports_direct_hints():
    """candidate.to_dict() 暴露 direct_hints（置信阈值可被服务层与前端读到）。"""
    _ex, cast = build_cast(RECALL_CHAPTERS)
    by = {c.name: c for c in cast}
    assert by["卡洛斯"].to_dict()["direct_hints"] >= 2, by["卡洛斯"].to_dict()


# ------------------------------------------------- F: 窗口残片归并（跨章别名合并）
FRAGMENT_MERGE_CHAPTERS = [
    (0, "“火钳在炉子边。”陈默把铁盒推到桌角，“你先坐下。”\n"
        "在陈默的注视下，她揭开了封漆。“这东西不能让第二个人看见。”\n"
        "看见陈默伸手，她先把盒子按住。“你自己打开。”\n"
        "陈默转身去拨炉灰，“我明天再问一次。”\n"),
    (1, "当陈默再次伸手时，她把盒子推了回去。“别在我面前装。”\n"
        "“明天早上八点。”陈默说，他把火钳插回炉膛。\n"),
]


def test_fragment_writings_collapse_into_the_full_name():
    """同一人在不同引号窗口里被截出的写法（在陈默/看见陈默/当陈默）不得另立角色。"""
    ex = CastExtractor()
    for idx, text in FRAGMENT_MERGE_CHAPTERS:
        ex.process_chapter(text, idx)
    cast = {c.name: c for c in ex.finalize()}
    junk = [n for n in cast if n not in (NARRATOR, "陈默") and "陈默" in n]
    assert not junk, f"窗口残片仍独立成角色: {junk}"
    assert "陈默" in cast, f"归并后全名缺席: {list(cast)}"
    assert len(cast) == 2, f"归并后角色表不该出现第二行: {list(cast)}"


def test_fragment_merge_conserves_lines():
    """归并是"并表"不是"丢行"：残片名下的句数必须并进全名（句数守恒）。"""
    only_full = CastExtractor()
    for idx, text in FRAGMENT_MERGE_CHAPTERS:
        only_full.process_chapter(re.sub("(在|看见|当)(?=陈默)", "", text), idx)
    merged = CastExtractor()
    for idx, text in FRAGMENT_MERGE_CHAPTERS:
        merged.process_chapter(text, idx)
    a = next(c.lines_count for c in only_full.finalize() if c.name == "陈默")
    b = next(c.lines_count for c in merged.finalize() if c.name == "陈默")
    assert b >= a, f"归并反而丢了戏份: 残片并入后 {b} < 纯全名 {a}"


def test_generic_titles_do_not_participate_in_fragment_merge():
    """通称/职务是普通名词："副局长"不是"局长"的别名，两条必须各占一行。"""
    chapters = [
        (0, "“笔录留下。”局长把文件夹合上，“明天补签。”\n"
            "副局长敲了两下门框，“他不见客。”\n"
            "“让他等着。”局长说，他把茶碗搁回桌面。\n"
            "副局长转过身，“外面冷，进来谈。”\n"),
    ]
    _ex, cast = build_cast(chapters)
    got = {c.name for c in cast}
    assert "局长" in got and "副局长" in got, f"职务通称被误并成一条: {sorted(got)}"


# ------------------------------------------------- F: 长书戏份显著度下限（非 top-N）
def _lead_in_lines(n: int, name: str) -> str:
    return "".join(f'{name}说：“第{i}句。”\n' for i in range(n))


def test_prominence_floor_is_derived_from_the_book_not_from_top_n():
    """门限 = ceil(占比 x 全书对白行数)：同一份文本换占比就换结果，名额不固定。"""
    text = _lead_in_lines(60, "陈默") + _lead_in_lines(3, "小满")
    ex = CastExtractor(min_line_share=0.1)
    ex.process_chapter(text, 0)
    cast = {c.name: c for c in ex.finalize()}
    assert ex._lines_floor() == 7, ex._lines_floor()
    assert "陈默" in cast and "小满" not in cast, f"门限未生效: {list(cast)}"
    stats = ex.stats()
    assert stats["lines_floor"] == 7 and stats["rejected_total"] >= 1, stats
    audit = {r["name"]: r for r in stats["rejected"]}
    assert "小满" in audit and audit["小满"]["reason"] == "rare", audit.get("小满")
    # 关掉占比（短文/默认门限）时小满必须回来——证明这不是硬砍名额
    ex2 = CastExtractor(min_line_share=0.0)
    ex2.process_chapter(text, 0)
    assert "小满" in {c.name for c in ex2.finalize()}, "占比=0 应退化为 min_lines 门限"


def test_prominence_floor_noop_for_short_texts():
    """既有夹具（对白行数少）下门限恒等于 min_lines，C1/C2/C4/C6 行为不变。"""
    ex = CastExtractor()
    for idx, text in RECALL_CHAPTERS:
        ex.process_chapter(text, idx)
    assert ex._lines_floor() == ex.min_lines, (ex._lines_floor(), ex.min_lines,
                                               ex.stats()["dialogue_lines"])


# ------------------------------------------------- G: 语料常见词不得凭回溯成角
def test_corpus_common_word_is_not_a_role():
    """全书高频出现、说话位证据不足 2% 的写法（"房间"式）必须被语料占比门限剔除。"""
    noise = "房间很小，他坐在房间角落，房间里的灯忽明忽暗。\n" * 40
    text = noise + "房间低声说：“别出声。”\n“我知道。”\n" + noise
    ex = CastExtractor()
    ex.process_chapter(text, 0)
    agg = ex._aggs.get("房间")
    assert agg is not None and agg.hinted >= 2, agg           # 确实拿到了直连提示
    assert agg.occ > agg.hinted / ex.min_name_share, (agg.occ, agg.hinted)
    cast = {c.name: c for c in ex.finalize()}
    assert "房间" not in cast, f"高频普通词仍被当成角色: {sorted(cast)}"
    audit = {r["name"]: r for r in ex.stats()["rejected"]}
    assert audit.get("房间", {}).get("reason") == "common_word", audit.get("房间")
    assert audit["房间"]["occurrences"] >= ex.min_occurrences, audit["房间"]


def test_rare_proper_name_survives_common_word_gate():
    """一次性专名（出现次数低于分母门限）不受语料判定影响——误杀护栏。"""
    _ex, cast = build_cast(RECALL_CHAPTERS)
    got = {c.name for c in cast}
    assert "玛莉·圣雅各" in got and "林晚晴" in got, f"稀有专名被语料门限误杀: {sorted(got)}"


def test_occurrences_exposed_for_audit():
    """每条保留角色都带 occurrences（全书出现次数）——保留项可解释的凭据。"""
    _ex, cast = build_cast(FRAGMENT_MERGE_CHAPTERS)
    cand = next(c for c in cast if c.name == "陈默")
    assert cand.to_dict()["occurrences"] >= cand.lines_count, cand.to_dict()


# ------------------------------------------------------- 复合译名的分段形态契约
def test_compound_transliterated_name_needs_two_syllables_per_side():
    """· 开头/结尾或单字分段 = 窗口残片；两侧各 >=2 字才是真译名写法。"""
    from modules.cast_analyzer.cast_extractor import is_person_shaped
    assert is_person_shaped("玛莉·圣雅各") is True
    assert is_person_shaped("杰森·伯恩") is True
    for frag in ("·伯恩", "多米尼克·", "卫·韦伯", "·圣雅各", "伊里奇·"):
        assert is_person_shaped(frag) is False, f"{frag} 是窗口残片，不该过形态门"



# ------------------------------------------------- H: CAST-RESIDUAL 别名合并残留
# 真书残留取证（project=51621d95，173 章 / 155 万字《谍影重重》；
# 探针 agent-ops/cast-residual/run_cast.py 用当前代码复算，三条全部仍在角色表）：
#   老实  64 行 —— "老+性质语素"的**话语状语**（= honestly speaking）。它恰恰长在引号
#         前面，detector 每次都把它当 role_hint 递回来，故 hinted/occ ≈ 30%，
#         语料常见词门限（min_name_share 2%）天生抓不到。
#   圣雅  93 行 —— _shape_name 从 "圣雅各平静地" 的 地 尾巴救回的**中段**半截译名：
#         它既不是 约翰·圣雅各 也不是 玛莉·圣雅各 的前缀/后缀，第 4 层够不着，
#         而且戏份比两个主机都多，合并后若照"戏份最多者取名"会把假名字留下。
# fixture 全部自造（另造一个同形态写法 老成、一组自造译名 科拉/伊万·科拉夫），
# 证明生效的是规则而不是那两个样本串。


def _adverbial_corpus(surface: str, quoted: int = 30, plain: int = 6) -> str:
    """造一份 "surface 的现身几乎全挤在 surface+说 框架里" 的语料。

    quoted 条：状语紧贴引号（每一次都能被当成分句开头的说话人提示）；
    plain  条：同词的普通用法（"算…的"），把状语占比压到 1.0 以下，
              这样"判据命中"靠的是**分布**，不是夹具只写了一种句子。
    """
    body = "".join(f'“第{i}回。”他停了停，{surface}说，“下回再议。”\n'
                   for i in range(quoted))
    body += "".join(f"这人在伙计里算{surface}的，从不抢先开口。\n" for _k in range(plain))
    return body


def test_honorific_prefixed_adverbial_is_not_a_role():
    """形态同 老实 的自造写法（老成）不得凭"贴着引号"的提示升格成角色。"""
    ex = CastExtractor()
    ex.process_chapter(_adverbial_corpus("老成"), 0)
    agg = ex._aggs.get("老成")
    assert agg is not None and agg.hinted >= 2, agg              # 确实拿到直连提示
    assert agg.occ >= ex.min_occurrences, (agg.occ, ex.min_occurrences)
    assert agg.hinted > ex.min_name_share * agg.occ, \
        "夹具的说话位占比必须高于 common_word 门限，否则测不到新判据"
    assert agg.mod_speech * 2 >= agg.occ, (agg.mod_speech, agg.occ)
    cast = {c.name: c for c in ex.finalize()}
    assert "老成" not in cast, f"话语状语仍被当成角色: {sorted(cast)}"
    audit = {r["name"]: r for r in ex.stats()["rejected"]}
    assert audit.get("老成", {}).get("reason") == "honorific_adverb", audit.get("老成")


def test_honorific_adverb_gate_reaches_the_recorded_residual():
    """账本点名的 老实 走同一条规则（回归锚；判据本身不含样本串）。"""
    ex = CastExtractor()
    ex.process_chapter(_adverbial_corpus("老实"), 0)
    cast = {c.name: c for c in ex.finalize()}
    assert "老实" not in cast, f"真书残留 老实 仍在角色表: {sorted(cast)}"
    audit = {r["name"]: r for r in ex.stats()["rejected"]}
    assert audit.get("老实", {}).get("reason") == "honorific_adverb", audit.get("老实")


def test_adverbial_gate_is_silent_below_the_rarity_floor():
    """稀有写法（occ < min_occurrences）一律放过：短文夹具与一次性专名不受影响。"""
    ex = CastExtractor()
    ex.process_chapter(_adverbial_corpus("老成", quoted=5, plain=0), 0)
    agg = ex._aggs.get("老成")
    assert agg is not None and agg.occ < ex.min_occurrences, (agg and agg.occ)
    assert "老成" in {c.name for c in ex.finalize()}, \
        "出现次数没到语料门限就被杀，说明新判据绕开了 min_occurrences 护栏"


def test_adverbial_shape_exempt_for_real_speaker_forms():
    """豁免面分两层：单姓简称与职务通称**形态上就被排除**；
    真译名昵称（大卫/阿布）形态命中，只能靠"现身分布"活下来（见下一条）。"""
    for name in ("老周", "小陈", "老板", "老头", "老人", "周队长", "圣雅", "科拉"):
        assert CastExtractor._is_adverbial_shape(name) is False, \
            f"{name} 被划进状语形态（真角色会被语料占比门限误杀）"
    for name in ("阿布", "大卫"):
        assert CastExtractor._is_adverbial_shape(name) is True, \
            f"{name} 形态本应命中，才有下一条的分布护栏可测"


def test_same_shape_nickname_survives_when_usage_is_nominative():
    """召回护栏：同形态昵称 阿布 的现身散布在各句法位置（状语占比低）-> 必须保留。"""
    text = ("".join(f'“记下了。”阿布说，他抱起胳膊，“明早开工。”\n' for _i in range(4))
            + "".join("账房先问阿布的来历，又翻阿布的契书，阿布自己并不答话。\n"
                      for _j in range(26)))
    ex = CastExtractor()
    ex.process_chapter(text, 0)
    agg = ex._aggs.get("阿布")
    assert agg is not None and agg.occ >= ex.min_occurrences, (agg and agg.occ)
    assert agg.mod_speech * 2 < agg.occ, (agg.mod_speech, agg.occ)
    cast = {c.name: c for c in ex.finalize()}
    assert "阿布" in cast, f"同形态真角色被误杀: {sorted(cast)}"


# ---- 第 5 层：中段包含式归并 -------------------------------------------------
MALE_FULL = "伊万·科拉夫"      # 6 字复合译名（汉译外名的长度上限）
FEMALE_FULL = "伊莲·科拉夫"
FRAG = "科拉"                   # 只作为两个全名的**中段**出现，从不单独成词


def _speaker_line(name: str, pronoun: str, i: int) -> str:
    """"名字说 + 代词小句 + 续句"：既给直连归属提示，又给 他/她 性别计数。"""
    return f'“第{i}句。”{name}说，{pronoun}把杯子放下，“第{i}句的下半句。”\n'


CONTAINMENT_CHAPTERS = [(0, "".join(
    [_speaker_line(FRAG, "他", i) for i in range(9)]
    + [_speaker_line(MALE_FULL, "他", 100 + i) for i in range(4)]
    + [_speaker_line(FEMALE_FULL, "她", 200 + i) for i in range(4)]))]


def _containment_text() -> str:
    return CONTAINMENT_CHAPTERS[0][1]


def _pre_merge_lines(text: str):
    """归并**前**每个写法的自有行数（同一段文本跑两遍是确定性的）。"""
    ex = CastExtractor()
    ex.process_chapter(text, 0)
    return {k: a.lines for k, a in ex._aggs.items()}


def test_middle_fragment_merges_into_gender_agreeing_full_name():
    """中段残片并入"性别信号一致的唯一全名主机"，显示名钉在全名而不是残片上。"""
    text = _containment_text()
    pre = _pre_merge_lines(text)
    assert FRAG in pre and MALE_FULL in pre, sorted(pre)
    assert pre[FRAG] > pre[MALE_FULL], f"夹具未复现残片戏份多于主机: {pre}"
    ex = CastExtractor()
    ex.process_chapter(text, 0)
    cast = {c.name: c for c in ex.finalize()}
    assert FRAG not in cast, f"中段残片仍独立成角色: {sorted(cast)}"
    assert MALE_FULL in cast, f"主机缺席/显示名被残片抢走: {sorted(cast)}"
    assert FRAG in cast[MALE_FULL].aliases, cast[MALE_FULL].to_dict()
    assert FEMALE_FULL in cast, f"异性主机被连带吞并: {sorted(cast)}"
    # 并表不丢行：两种写法的行数完整相加
    assert cast[MALE_FULL].lines_count == pre[FRAG] + pre[MALE_FULL], \
        (cast[MALE_FULL].lines_count, pre)


def test_containment_merge_is_recorded_with_confidence():
    """归并必须可复核：alias_candidates 记下 残片->主机 + 置信度；
    被否掉的异性主机同样留痕（合并不是静默的）。"""
    ex = CastExtractor()
    ex.process_chapter(_containment_text(), 0)
    cast = {c.name: c for c in ex.finalize()}
    entries = cast[MALE_FULL].to_dict()["alias_candidates"]
    chosen = [e for e in entries if e["merged"]]
    assert len(chosen) == 1, entries
    assert chosen[0]["surface"] == FRAG and chosen[0]["host"] == MALE_FULL, chosen[0]
    assert chosen[0]["confidence"] >= 0.75, chosen[0]
    assert chosen[0]["surface_lines"] > chosen[0]["host_lines"], chosen[0]
    vetoed = [e for e in entries if not e["merged"]]
    assert vetoed and all(e["surface"] == FRAG for e in vetoed), entries
    assert {e["host"] for e in vetoed} == {FEMALE_FULL}, vetoed
    assert all(0.0 < e["confidence"] < 0.75 for e in vetoed), vetoed


def test_containment_conflict_is_recorded_not_silently_dropped():
    """性别信号冲突（残片像女性、唯一主机是男性）时**不归并**，
    但判定连置信度一起留痕——账本要的"冲突记候选别名，不静默丢弃"。"""
    text = "".join([_speaker_line(FRAG, "她", i) for i in range(6)]
                   + [_speaker_line(MALE_FULL, "他", 100 + i) for i in range(4)])
    ex = CastExtractor()
    ex.process_chapter(text, 0)
    cast = {c.name: c for c in ex.finalize()}
    assert FRAG in cast, f"冲突判定被静默吞并: {sorted(cast)}"
    assert MALE_FULL in cast and FRAG not in cast[MALE_FULL].aliases
    entries = cast[FRAG].to_dict()["alias_candidates"]
    assert entries, "冲突未留痕（等于静默丢弃）"
    assert all(e["merged"] is False for e in entries), entries
    assert all(0.0 < e["confidence"] < 0.75 for e in entries), entries


def test_pronoun_gender_signal_requires_strict_majority():
    """主机选择的唯一依据是 他/她 严格多数（口径同指代档案）；
    持平与零命中都算"没有信号"，不能冒充性别证据。"""
    from modules.cast_analyzer.cast_extractor import _Agg

    def agg(he: int, she: int) -> _Agg:
        a = _Agg("probe")
        a.he, a.she = he, she
        return a

    assert CastExtractor._pronoun_gender(agg(3, 1)) == "male"
    assert CastExtractor._pronoun_gender(agg(1, 3)) == "female"
    assert CastExtractor._pronoun_gender(agg(2, 2)) is None
    assert CastExtractor._pronoun_gender(agg(0, 0)) is None
    assert CastExtractor._pronoun_gender(None) is None


def test_alias_candidates_reach_role_extra_data():
    """判定要能被服务层与前端看到：profiles_to_role_extra_data 透传 alias_candidates。"""
    from modules.cast_analyzer.cast_profiler import profiles_to_role_extra_data
    _ex, cast = build_cast(CONTAINMENT_CHAPTERS)
    rows = {r["name"]: r for r in profiles_to_role_extra_data(cast)}
    assert FRAG not in rows, sorted(rows)
    entries = rows[MALE_FULL]["cast"]["alias_candidates"]
    assert entries and entries[0]["surface"] == FRAG, entries
    # 角色 dict 形状（cast_service 落库前用的就是它）也带得上
    assert "alias_candidates" in cast[0].to_dict(), cast[0].to_dict()


if __name__ == "__main__":                                          # pragma: no cover
    sys.exit(pytest.main([__file__, "-v"]))
