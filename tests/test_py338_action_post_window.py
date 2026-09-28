"""PY-338：引号后「动作窗」归因通道恢复，候选必须过 cast 的判名门。

背景（`qa/issues.md` PY-338）：WF-01（`0cb418b`）把后窗收紧成"只认说话动词"，
连带把整条**动作主语**归因关掉——同夹具并排实测 PRE 命中 5/5、HEAD 命中 0/5
（探针 `qa/harness/probe_py338_post_window.py`）。但当初收紧是有理由的：泛动作窗
会造出两枚假角色（`里头传来`、`声音很轻`）。

本文件钉的是"两全"那一版正解：
  * 后窗动作主语**恢复**；
  * 候选除 detector 自己的清洗门外，还必须过 cast 侧**判名门**（词类否决 + 正向
    姓名形态），于是两枚假角色仍被拦；
  * 上限因此从 4/4 降到 3/4——`那人` 是指示代词开头，判名门不认。这是设计结论，
    不是漏修，见 `test_deictic_hint_stays_rejected_by_design`。

门是**注入**的（`DialogueDetector.name_gate`，默认 None = 通道关闭）：cast 抽取器
已 import dialogue_detector，反向 import 会成环，所以门由 cast 侧在构造时传进来。

另钉两条不是 PY-338 本体、但被这次改动逼出来的账：后窗**双子句取哪一句**（组 6 的
`test_post_window_two_clause_takes_the_clause_touching_the_quote`，口径而非唯一解）、
以及"模块级函数插进类中间会把整批方法掉出类外"的自伤形态（组 9）。
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest                                     # noqa: E402

from modules.text_processor.dialogue_detector import DialogueDetector   # noqa: E402
from modules.cast_analyzer.cast_extractor import looks_like_name        # noqa: E402

GATE = DialogueDetector(name_gate=looks_like_name)
PLAIN = DialogueDetector()

# 前三枚里，前两枚是 PY-338 要找回的召回，第三枚是"找不回"的那一枚；
# 后两枚是 WF-01 的账单——必须继续拦着。
RECOVER = [
    ("“……”林昭给自己倒了杯茶。", "林昭"),
    ("“你当真要走？”阿贵蹲在门槛上，头也不抬。", "阿贵"),
]
DEICTIC = ("“你来了。”那人没有回头。", None)
BLOCKED = [
    "“这么晚了，谁啊？”里头传来拖长的问话。",
    "“我姓纪。”她的声音很轻。",
]


def first_hint(det, text):
    hints = [s.get("role_hint") for s in det.detect_dialogues(text)
             if s["type"] == "dialogue"]
    return hints[0] if hints else None


# ---- 1. 通道默认关：不注入判名门时，行为与 WF-01 之后逐字一致 --------------

@pytest.mark.parametrize("text, want", RECOVER)
def test_channel_is_off_without_an_injected_gate(text, want):
    """安全默认：少传一道门就静默退回 WF-01 之前的假角色状态，代价太大。"""
    assert first_hint(PLAIN, text) is None


# ---- 2. 注入判名门后，动作后窗找回真名 ------------------------------------

@pytest.mark.parametrize("text, want", RECOVER)
def test_action_post_window_recovers_real_names(text, want):
    assert first_hint(GATE, text) == want


# ---- 3. WF-01 拦下的两枚假角色必须仍然拦得住 ------------------------------

@pytest.mark.parametrize("text", BLOCKED)
def test_narration_fragments_stay_blocked(text):
    assert first_hint(GATE, text) is None


def test_the_gate_is_the_only_thing_rejecting_the_two_false_roles():
    """这两枚 detector 自己的 `_accept_role` 是**放行**的（PY-338 实采），拦下它们的
    只有判名门。不单独钉这条，将来有人放宽 `_accept_role` 会让假角色静默回流。"""
    assert PLAIN._accept_role("里头传来") == "里头传来"
    assert PLAIN._accept_role("声音很轻") == "声音很轻"
    assert looks_like_name("里头传来") is False
    assert looks_like_name("声音很轻") is False


# ---- 4. 判名门不认指示代词头 => 上限 3/4（设计结论，防"顺手放宽"）----------

def test_deictic_hint_stays_rejected_by_design():
    """`那人` 走动作后窗时被判名门否决（这/那 开头不属于任何姓名形态），
    所以 PY-109 的 4/4 **不该**被本修复原样恢复。若将来为了让这句变绿而放宽
    判名门，本条会红——那是名单级改动，得回 issues.md 重开 PY-338。"""
    assert looks_like_name("那人") is False
    assert first_hint(GATE, DEICTIC[0]) is None


# ---- 5. 说话动词通道（WF-01 那一档）与前窗通道不受影响 --------------------

@pytest.mark.parametrize("det", [PLAIN, GATE], ids=["plain", "gated"])
def test_speech_verb_channel_unchanged(det):
    assert first_hint(det, "“知道了。”林昭说。") == "林昭"
    assert first_hint(det, "“走吧。”周主事道。") == "周主事"


@pytest.mark.parametrize("det", [PLAIN, GATE], ids=["plain", "gated"])
def test_pre_window_channel_unchanged(det):
    assert first_hint(det, "周主事放下茶盏，压低声音：“这么晚了？”") == "周主事"


# ---- 6. 跨行不归因（后窗必须同一行紧跟引号）------------------------------

def test_action_hint_on_next_line_is_not_used():
    assert first_hint(GATE, "“……”\n林昭给自己倒了杯茶。") is None


TWO_CLAUSE = [
    "“……”林昭给自己倒了杯茶。阿贵说。",
    "“走不走？”林昭给自己倒了杯茶，阿贵说。",
]


@pytest.mark.parametrize("text", TWO_CLAUSE)
def test_post_window_two_clause_takes_the_clause_touching_the_quote(text):
    """后窗里出现两个子句时，取**紧贴引号的那一句**（这里是动作主语 林昭），
    不是窗内更远处带说话动词的那一句（阿贵）。与前窗"只取最后一句"对称。

    这条钉的是**答案**，不是实现：`_action_hint_after_quote` 先按句读把窗口收敛成
    首句，但把收敛整条摘掉（变异模型 4）实测仍得 林昭——因为 `_role_from_head` 的
    锚定头 `^\\s*([\\u4e00-\\u9fa5A-Za-z·]{2,6}?)(?=动作字)` 里那个字符类不含句读，
    自己就在第一个动作字处收尾。所以分句是**等价的再收窄一层**，不为它另造夹具。
    """
    assert first_hint(GATE, text) == "林昭"


def test_speech_verb_channel_beats_action_channel_in_the_same_clause():
    """同一子句里两种线索并存时，说话动词优先（WF-01 那一档不能被新通道顶掉）。"""
    assert first_hint(GATE, "“……”林昭说着给自己倒了杯茶。") == "林昭"
    assert first_hint(GATE, "“……”阿贵蹲在门槛上，头也不抬地说。") == "阿贵"



# ---- 7. 起决定作用的是"门"本身，不是巧合 ----------------------------------

def test_injected_gate_is_what_decides():
    """换成"什么都不认"的门 => 动作后窗一枚都不该出；换成"什么都认"的门
    => 假角色回来。两向一起钉，排掉"结果恰好与门的取值相同"的假绿。"""
    deny = DialogueDetector(name_gate=lambda raw: False)
    allow = DialogueDetector(name_gate=lambda raw: True)
    assert first_hint(deny, RECOVER[0][0]) is None
    assert first_hint(allow, BLOCKED[0]) == "里头传来"
    assert first_hint(allow, RECOVER[0][0]) == "林昭"


# ---- 8. 接线：产品路径（P5 `_stage_analyze`）真的开着这条通道 -------------

SENT = "“走不走？”林昭给自己倒了杯茶。"


def test_p5_stage_produces_speaker_from_action_post_window():
    """走**真实调用路径**，不是查源码文本：`DialogueDetector` 是在 `_stage_analyze`
    内部就地构造的（局部 import），所以"有没有传门"只能在出口 `speaker` 上看。
    这一枚红 = 通道在产品里没开，而 1–7 会全绿（它们自己构造了带门实例）。"""
    from modules.workflow.conversion_workflow import ConversionWorkflow
    out = ConversionWorkflow()._stage_analyze({"sentences": [{"text": SENT}]})
    assert out["sentences"][0]["speaker"] == "林昭"


def test_p5_stage_attribution_is_discriminative():
    """同一条路径上的反向钉：假角色不能因为"通道开了"就混进 `speaker`。"""
    from modules.workflow.conversion_workflow import ConversionWorkflow
    bad = "“这么晚了，谁啊？”里头传来拖长的问话。"
    out = ConversionWorkflow()._stage_analyze({"sentences": [{"text": bad}]})
    assert out["sentences"][0]["speaker"] == ""


def test_cast_extractor_wires_the_gate_into_its_detector():
    """抽取器构造 detector 时必须把门传下去；漏传 = 名单侧整条通道没开。"""
    from modules.cast_analyzer.cast_extractor import CastExtractor
    assert CastExtractor().detector.name_gate is looks_like_name


# ---- 9. 自伤护栏：类不能被从中间劈开 ----------------------------------------

_SELF_CALL = re.compile(r"(?:self|cls)\.(_[A-Za-z0-9_]+)\(")


@pytest.mark.parametrize("qualified", [
    "modules.cast_analyzer.cast_extractor.CastExtractor",
    "modules.text_processor.dialogue_detector.DialogueDetector",
])
def test_every_self_call_resolves_on_the_class(qualified):
    """把判名门从 `CastExtractor` 里提到模块级时，我一度把它插在了类中间——返回语句
    之后的整批方法（`_rightmost`/`_tail_clause`/…）于是掉出类外，而本文件 1–8 全绿：
    它们走不到那些路径。静态扫类体，凡是 `self._x()` / `cls._x()` 调用而类上不存在，
    一律钉红。

    """
    import importlib
    import inspect
    module_name, cls_name = qualified.rsplit(".", 1)
    cls = getattr(importlib.import_module(module_name), cls_name)
    used = set(_SELF_CALL.findall(inspect.getsource(cls)))
    missing = sorted(n for n in used if not hasattr(cls, n))
    assert not missing, f"{cls_name} 调用了类上不存在的方法: {missing}（类体被截断？）"

