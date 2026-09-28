"""PY-338 探针：只看"动作后窗"这一条通道，逐句打 PRE(0cb418b^) vs HEAD 的归因。

只读：不改产品码、不写库。PRE 侧从 git 历史取文件按模块加载。
"""
import importlib.util
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from modules.text_processor.dialogue_detector import DialogueDetector  # noqa: E402

CASES = [
    # (文本, 期望真名, 类别)
    ("“你来了。”那人没有回头。", "那人", "应归因(指示代词，判名门不认)"),
    ("“……”林昭给自己倒了杯茶。", "林昭", "应归因"),
    ("“你当真要走？”阿贵蹲在门槛上，头也不抬。", "阿贵", "应归因"),
    ("“这么晚了，谁啊？”里头传来拖长的问话。", None, "假角色，必须仍拦"),
    ("“我姓纪。”她的声音很轻。", None, "假角色，必须仍拦"),
]


def load_pre():
    """把 WF-01 之前的 dialogue_detector.py 取出来，装成一个独立模块。"""
    src = subprocess.run(
        ["git", "show", "0cb418b^:modules/text_processor/dialogue_detector.py"],
        cwd=ROOT, capture_output=True, check=True).stdout.decode("utf-8")
    tmp = Path(tempfile.mkdtemp()) / "dd_pre.py"
    tmp.write_text(src, encoding="utf-8")
    spec = importlib.util.spec_from_file_location("dd_pre", tmp)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.DialogueDetector(), src


def hint(det, text):
    segs = det.detect_dialogues(text)
    return [s.get("role_hint") for s in segs if s["type"] == "dialogue"]


def main():
    pre, src = load_pre()
    head = DialogueDetector()
    print("PRE 模块行数:", src.count("\n"))
    print("%-4s %-14s %-10s %-10s %s" % ("#", "期望", "PRE", "HEAD", "类别"))
    for i, (text, want, note) in enumerate(CASES, 1):
        p = hint(pre, text)
        h = hint(head, text)
        print("%-4d %-14s %-10s %-10s %s" % (
            i, want or "-", ",".join(x or "-" for x in p) or "-",
            ",".join(x or "-" for x in h) or "-", note))
        print("      原文: %s" % text)

    # 后窗候选在 HEAD 侧的清洗门走向：逐层看它死在哪一道
    print("\n--- _accept_role 逐层（HEAD 实现，针对两句假角色）---")
    for raw in ["里头传来", "声音很轻", "那人", "林昭", "阿贵"]:
        print("%-8s -> %r" % (raw, head._accept_role(raw)))


if __name__ == "__main__":
    main()
