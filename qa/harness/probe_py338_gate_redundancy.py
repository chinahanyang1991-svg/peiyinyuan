"""PY-338 变异存活归因：M6/M8/M9/M10/M11 五枚没被测试定罪的变异，
到底是"漏网"还是"同层复否"？逐个否决支找出**只有它自己**能拦住的候选串。

判据：对每个候选 raw，逐条门单独判定（其余门视为放行），看是否恰好一条为 False。
若某条门存在这种"独占"候选，说明它承重、该有测试钉住；若在整个词表 + 实测样本里
都找不到独占候选，则该门是同层复否（纵深防御），变异存活不算漏网。

跑法：python -X utf8 qa/harness/probe_py338_gate_redundancy.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from modules.cast_analyzer import cast_extractor as ce  # noqa: E402
from modules.cast_analyzer.cast_extractor import (  # noqa: E402
    LOCATIVE_HEADS, NARRATION_TAILS, PRONOUNS, STOP_WORDS, is_person_shaped,
)


def branches(raw, max_len=6):
    """逐条否决支单独判定：True = 这一支放行。"""
    return {
        "length": not (len(raw) < 2 or len(raw) > max_len),
        "pronoun": raw not in PRONOUNS,
        "stopwords": not any(w in raw for w in STOP_WORDS),
        "locative": not any(raw.startswith(h) for h in LOCATIVE_HEADS),
        "narration_tail": not any(raw.endswith(t) for t in NARRATION_TAILS),
        "person_shape": bool(is_person_shaped(raw)),
    }


def exclusive_hits(cands):
    out = {}
    for raw in cands:
        bs = branches(raw)
        blocked = [k for k, ok in bs.items() if not ok]
        if len(blocked) == 1:
            out.setdefault(blocked[0], []).append(raw)
    return out


def main():
    print("词表规模: PRONOUNS=%d LOCATIVE_HEADS=%d NARRATION_TAILS=%d STOP_WORDS=%d"
          % (len(PRONOUNS), len(LOCATIVE_HEADS), len(NARRATION_TAILS), len(STOP_WORDS)))
    # 1) 词表本身：把每个词类成员当候选喂进去（2..6 字），找独占否决
    vocab = {str(x) for x in PRONOUNS} | {str(x) for x in LOCATIVE_HEADS} \
        | {str(x) for x in NARRATION_TAILS} | {str(x) for x in STOP_WORDS}
    vocab = {v for v in vocab if 2 <= len(v) <= 6}
    ex = exclusive_hits(sorted(vocab))
    print("\n[词表内独占否决]（该支单独拦住了这些串）")
    for k in ["length", "pronoun", "stopwords", "locative", "narration_tail", "person_shape"]:
        hits = ex.get(k, [])
        print(f"  {k:<14} {len(hits):>3} 例  例: {hits[:6]}")

    # 2) 词表 × 常见姓氏/尾字组合：给每支一次"被拼出来"的机会
    surn = "王李张刘陈杨黄赵周吴徐孙马朱胡林何郭高罗梁宋谢唐韩冯董程曹袁邓许傅沈曾彭吕苏卢蒋蔡贾丁魏薛叶阎余潘杜戴夏钟汪任姜范方石姚谭廖邹熊金陆郝孔白崔康毛邱秦江史顾侯邵孟龙万段雷钱汤"
    tails = "主事娘爷哥嫂叔生官儿士人老鬼子手师傅太君某"
    mids = "小大若一之亦且伟强芳娜敏静丽明光建国红云飞龙凤虎豹雷震廷儒卿秀英华"
    combo = set()
    for s in surn:
        for m in mids:
            combo.add(s + m)
            for t in tails:
                combo.add(s + m + t)
    for h in LOCATIVE_HEADS:
        for t in tails:
            combo.add(str(h) + str(t))
    for t in NARRATION_TAILS:
        for s in surn:
            combo.add(str(s) + str(t))
    combo = {c for c in combo if 2 <= len(c) <= 6}
    ex2 = exclusive_hits(sorted(combo))
    print(f"\n[词表×姓氏/尾字组合 {len(combo)} 串] 独占否决")
    for k in ["length", "pronoun", "stopwords", "locative", "narration_tail", "person_shape"]:
        hits = ex2.get(k, [])
        print(f"  {k:<14} {len(hits):>3} 例  例: {hits[:8]}")

    # 3) PY-338 实测样本：五枚假/真角色逐条门判定
    print("\n[实测样本逐条门]（True=放行）")
    for raw in ["林昭", "阿贵", "周主事", "那人", "里头传来", "声音很轻", "身后有人", "他"]:
        bs = branches(raw)
        blocked = [k for k, ok in bs.items() if not ok]
        print(f"  {raw:<6} 放行={all(bs.values())!s:<5} 否决支={blocked}")


if __name__ == "__main__":
    main()
