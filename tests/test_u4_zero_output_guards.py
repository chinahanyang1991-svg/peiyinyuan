"""U-4/M-5：P2–P6 零产出守卫（回写 2026-09-23）

审核报告定罪：`conversion_workflow` 的 P7/P8/P9 有 PY-117 的「零产出即失败」语义，
P2–P6 却一路绿着穿过——空章节书 / 只有页眉页脚的书在 P2 拿到 0 章、P4 切出 0 句时
只写日志不报错，任务一直 running 到 P7 才可能失败（或干脆 0 句"成功"）。

本文件锁两件事：
1. 零产出必须抛 RuntimeError，且原因里带阶段名 + 输入规模 + 期望最小值（可直接进
   task.error_message 给用户看，不接受"出错了但不知道为什么"）；
2. 非空输入不得误伤——尤其 WF-08 那串 pathological 文本（大量句号但确有可发音句）
   必须一路通过。

P6 的边界记在源码注释里，这里同时钉住：「有句但无音色」不是零产出，不拦。
"""

import pytest

from modules.workflow.conversion_workflow import conversion_workflow as wf

# 与 tests/test_wf_fix4.py::TestWF08PunctuationOnly 同源的真实文本：
# 逐字加句号，但确有可发音内容——守卫若误伤它，红线就是"正常书也跑不了"。
PATHOLOGICAL = ("他。走。在。长。长。的。街。道。上。，。路。灯。把。影。子。"
                "拉。得。很。长。。。风。从。巷。口。灌。进。来。，。吹。落。了。"
                "几。片。枯。叶。。。")


def _ch(text, cid="c1", title="第一章"):
    return {"id": cid, "book_id": "b1", "title": title, "index": 0,
            "content": text, "char_count": len(text),
            "content_preview": "", "extra_data": {}}


# --------------------------------------------------------------- P2 章节校验
def test_parse_raises_when_no_usable_chapter():
    ctx = {"task_id": "u4-p2-empty", "book_id": None, "chapters": []}
    with pytest.raises(RuntimeError) as ei:
        wf._stage_parse(ctx)
    msg = str(ei.value)
    assert "[P2 Parse]" in msg, msg
    assert "0 章" in msg, msg


def test_parse_raises_when_only_blank_chapters():
    ctx = {"task_id": "u4-p2-blank", "book_id": None,
           "chapters": [_ch(""), _ch("   \n  ")]}
    with pytest.raises(RuntimeError, match=r"\[P2 Parse\]"):
        wf._stage_parse(ctx)


def test_parse_still_passes_real_content():
    text = "这是一个正常的章节正文，有足够多的可发音字符用于分句。"
    ctx = {"task_id": "u4-p2-ok", "book_id": None, "chapters": [_ch(text)]}
    out = wf._stage_parse(ctx)
    assert out["chapters"], out


# --------------------------------------------------------------- P3 清洗
def test_clean_raises_when_nothing_survives():
    ctx = {"task_id": "u4-p3", "chapters": [_ch(""), _ch("   ")]}
    with pytest.raises(RuntimeError) as ei:
        wf._stage_clean(ctx)
    assert "[P3 Clean]" in str(ei.value), ei.value


def test_clean_passes_on_text():
    ctx = {"task_id": "u4-p3-ok", "chapters": [_ch("她说：今天的风很大。")]}
    out = wf._stage_clean(ctx)
    assert out["total_chars"] >= 1, out


# --------------------------------------------------------------- P4 分句
def test_split_raises_when_nothing_is_pronounceable():
    ctx = {"task_id": "u4-p4", "settings": {}, "book_language": "zh",
           "chapters": [_ch("，。、；！？—…\n，。")]}
    with pytest.raises(RuntimeError) as ei:
        wf._stage_split(ctx)
    msg = str(ei.value)
    assert "[P4 Split]" in msg, msg
    # 原因里必须能看到规模：章节数 + 切出段数 + 丢弃段数
    assert "章" in msg and "段" in msg, msg


def test_split_does_not_harm_the_pathological_book():
    """WF-08 的病理文本仍须通过（它确有可发音句）——守卫不得变成第二道 WF-08。"""
    ctx = {"task_id": "u4-p4-ok", "settings": {}, "book_language": "zh",
           "chapters": [_ch(PATHOLOGICAL)]}
    wf._stage_clean(ctx)
    out = wf._stage_split(ctx)
    assert out["split_stats"]["sentences"] >= 1, out["split_stats"]
    assert ctx["sentences"], ctx


# --------------------------------------------------------------- P5 分析
def test_analyze_raises_on_empty_sentence_list():
    ctx = {"task_id": "u4-p5", "sentences": [], "total_chars": 0}
    with pytest.raises(RuntimeError, match=r"\[P5 Analyze\]"):
        wf._stage_analyze(ctx)


def test_analyze_passes_on_sentences():
    ctx = {"task_id": "u4-p5-ok", "sentences": [{"text": "他走了。", "char_count": 4}],
           "project_id": None}
    out = wf._stage_analyze(ctx)
    assert out["sentences"], out


# --------------------------------------------------------------- P6 分配
def test_assign_raises_on_empty_sentence_list():
    ctx = {"task_id": "u4-p6", "sentences": []}
    with pytest.raises(RuntimeError, match=r"\[P6 Assign\]"):
        wf._stage_assign(ctx, None)


def test_assign_without_project_still_produces_sentences():
    """无项目 = 走默认音色那条合法出厂路径，守卫不得把它判成零产出。"""
    ctx = {"task_id": "u4-p6-ok", "sentences": [{"text": "她回来了。", "char_count": 5}]}
    out = wf._stage_assign(ctx, None)
    assert len(out["sentences"]) == 1, out


# ------------------------------------------------------- 全链 happy path（P2→P6）
def test_full_pre_synthesis_chain_completes_on_a_real_book():
    body = ("周建国把伞收起来，抖了抖水。\n"
            "“雨停了。”她说，语气里带着点如释重负。\n"
            "街灯把两个人的影子拉得很长，风从巷口灌进来，吹落几片枯叶。")
    ctx = {"task_id": "u4-chain", "book_id": None, "settings": {},
           "book_language": "zh", "project_id": None, "chapters": [_ch(body)]}
    wf._stage_parse(ctx)
    wf._stage_clean(ctx)
    wf._stage_split(ctx)
    wf._stage_analyze(ctx)
    out = wf._stage_assign(ctx, None)
    assert len(out["sentences"]) >= 3, out["sentences"]
    assert all((s.get("text") or "").strip() for s in out["sentences"])


# --------------------------------------------- 静态守卫：五道门不得被重构悄悄拆掉
GUARD_TAGS = ("[P2 Parse]", "[P3 Clean]", "[P4 Split]", "[P5 Analyze]", "[P6 Assign]")


def test_all_five_stage_guards_are_present_in_source():
    import inspect

    src = inspect.getsource(wf.__class__)
    for tag in GUARD_TAGS:
        assert src.count(f'f"{tag}') >= 1, f"{tag} 守卫从源码里消失了"
    assert src.count("raise RuntimeError(") >= len(GUARD_TAGS)


def test_guards_do_not_swallow_cancel_semantics():
    """守卫只能新增 raise，不得改动 WF-07 的取消优先（P7 里 cancel 判在 PY-117 前）。"""
    import inspect

    src = inspect.getsource(wf.__class__)
    assert "取消优先级高于 PY-117" in src, "WF-07 的取消优先注释/语义被移走了"
