"""PY-338 变异检验：动作后窗这一档归因的每道门，都要有测试能为它变红。

沿用 `run_u11_mutation.py` 的三条规矩（那三条是被 U-7 首轮 10 枚 `NOT_APPLIED`
换来的）：替换串逐字命中一次、模式里的换行按目标文件实际行尾展开、读写一律二进制，
跑完核对基线 md5，任何文件字节不同即还原失败并非零退出。

本文件的特别处：每枚变异**预先声明预期**（`RED` 或 `SURVIVE`），并且 RED 必须点名
是哪条测试为它定罪——只 `rc!=0` 不算数。`SURVIVE` 不是"放过"，而是有出处的结论：
`qa/harness/probe_py338_gate_redundancy.py` 逐条门算过**独占否决**（词表全集 +
词表×姓氏/尾字组合 64,700 串），代词门与长度下限的独占计数为 0，即"摘掉它没有
任何串会因此放行"，属同层复否；把这两枚写成"由某某测试定罪"才是假账。

额外盯一类事故：**把 `looks_like_name` 从类里提到模块级时，一度插在了类中间**，
返回语句之后的整批方法（`_rightmost`/`_tail_clause`/…）当场掉出类外，而 PY-338 的
单测全绿——那些方法只在测试走不到的路径里被调用。模型 14 专门重现这个形态。

跑法（产品树允许带未提交改动：基线 md5 开跑时采集，结束时逐文件比对）：
    python -X utf8 qa/harness/run_py338_mutation.py --check      # 只核对命中数
    python -X utf8 qa/harness/run_py338_mutation.py
    python -X utf8 qa/harness/run_py338_mutation.py --only 9,15
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DET = "modules/text_processor/dialogue_detector.py"
EXT = "modules/cast_analyzer/cast_extractor.py"
WF = "modules/workflow/conversion_workflow.py"
TESTS = [
    "tests/test_py338_action_post_window.py",
    "tests/test_cast_extractor.py",
    "tests/test_cast_name_shape.py",
    "tests/test_text_processor.py",
]

RED = "RED"
SURVIVE = "SURVIVE"

# (编号, 预期, 文件, 原文, 变异后, 定罪的测试 / 复否出处, 说明)
MUTATIONS = [
    # ---- detector：通道本体 + 接线 ----
    ("1", RED, DET,
     "        self.name_gate = name_gate\n",
     "        self.name_gate = name_gate or (lambda raw: True)\n",
     "test_channel_is_off_without_an_injected_gate",
     "忘传门=默认放行（少传一道门静默放宽名单）"),
    ("2", RED, DET,
     "        role = self._role_from_head(clause, anchored=True)\n"
     "        if role and self.name_gate(role):\n",
     "        role = self._role_from_head(clause, anchored=True)\n"
     "        if role:\n",
     "test_narration_fragments_stay_blocked",
     "后窗动作主语取消逐词读判名门（= 回到 WF-01 之前的假角色）"),
    ("3", RED, DET,
     "        if role and self.name_gate(role):\n",
     "        if role and not self.name_gate(role):\n",
     "test_action_post_window_recovers_real_names",
     "门的极性写反"),
    ("4", SURVIVE, DET,
     "        clause = re.split(r'[。！？!?；;，,、]', post_raw)[0].strip()\n",
     "        clause = post_raw.strip()\n",
     "等价出处: 两枚双子句夹具（。与，各一枚）实测同解 林昭——`_role_from_head` 的锚定头"
     "`^\\s*([\\u4e00-\\u9fa5A-Za-z·]{2,6}?)(?=动作字)` 字符类不含句读，自己就在第一个动作字"
     "处收尾；分句只是等价的再收窄一层，不为它另造夹具",
     "取消首句收敛 = 等价变异（首轮曾按 RED 申报，实测改判）"),
    ("5", RED, DET,
     r"        if post_raw and not post_raw[:1] in ('\n', '\r'):" "\n",
     "        if post_raw:\n",
     "test_action_hint_on_next_line_is_not_used",
     "取消跨行守卫：下一行动作也能归因"),
    ("6", RED, DET,
     "            # 1b) PY-338: 动作主语后窗（不要求说话动词），候选过 cast 的判名门\n"
     "            role = self._action_hint_after_quote(post_raw)\n"
     "            if role:\n"
     "                return role\n",
     "            # mutated: 后窗动作主语这一格不再存在\n",
     "test_action_post_window_recovers_real_names",
     "1b 整格摘掉（= 回到 WF-01 的收宽）"),
    ("7", RED, WF,
     "        dialogue_detector = DialogueDetector(name_gate=looks_like_name)\n",
     "        dialogue_detector = DialogueDetector()\n",
     "test_p5_stage_produces_speaker_from_action_post_window",
     "产品路径 P5 丢门（1–7 组全绿也照样没有这条通道，故必须走真实调用路径）"),
    ("8", RED, EXT,
     "        self.detector = detector or DialogueDetector(name_gate=looks_like_name)\n",
     "        self.detector = detector or DialogueDetector()\n",
     "test_cast_extractor_wires_the_gate_into_its_detector",
     "名单路径丢门"),
    # ---- cast 判名门：承重者必须红，复否者按独占计数判定 ----
    ("9", RED, EXT,
     "    return is_person_shaped(raw)\n",
     "    return True\n",
     "test_deictic_hint_stays_rejected_by_design",
     "最里层的正向姓名形态门摘掉（实测 5 条测试一起红 = 承重者）"),
    ("12", RED, EXT,
     "    if any(w in raw for w in STOP_WORDS):\n        return False\n",
     "    # mutated: 虚词表一律放行\n",
     "test_each_veto_branch_has_an_exclusive_veto",
     "虚词表摘掉（大声/小声/许多 只有它拦得住）"),
    ("10", RED, EXT,
     "    if any(raw.startswith(head) for head in LOCATIVE_HEADS):\n        return False\n",
     "    # mutated: 方位词头一律放行\n",
     "test_each_veto_branch_has_an_exclusive_veto",
     "方位头摘掉（墙根/堂中士 只有它拦得住）"),
    ("11", RED, EXT,
     "    if any(raw.endswith(tail) for tail in NARRATION_TAILS):\n        return False\n",
     "    # mutated: 叙述尾巴一律放行\n",
     "test_each_veto_branch_has_an_exclusive_veto",
     "叙述尾巴摘掉（丁传来 只有它拦得住）"),
    ("13", RED, EXT,
     "        if (i > 0 and len(s) >= 3 and s[0] in COMMON_SURNAMES\n"
     "                and ch in NAME_INTERNAL_STOP_CHARS):\n"
     "            continue\n",
     "        if False:\n            continue\n",
     "test_stop_chars_are_legal_inside_a_full_name",
     "PY-335 的姓名内部语素豁免退回（沈若兰/王羲之 再被杀）"),
    ("14", RED, EXT,
     "        return looks_like_name(raw, max_len=self.max_name_len)\n"
     "\n"
     "    @staticmethod\n"
     "    def _rightmost(regex, window: str):\n",
     "        return looks_like_name(raw, max_len=self.max_name_len)\n"
     "\n"
     "\n"
     "def _py338_gate_here(raw: str, max_len: int = 6) -> bool:\n"
     "    return is_person_shaped(raw)\n"
     "\n"
     "    @staticmethod\n"
     "    def _rightmost(regex, window: str):\n",
     "test_every_self_call_resolves_on_the_class",
     "开发期真实事故：模块级函数插在类中间，整批方法掉出类外"),
    ("15", SURVIVE, EXT,
     "    if raw in PRONOUNS:\n        return False\n",
     "    # mutated: 代词表一律放行\n",
     "复否出处: probe_py338_gate_redundancy 独占计数 pronoun=0（18 枚代词无一被它单独拦下；"
     "'那人'由形态门否、'他'同时被长度门否）",
     "代词门摘掉 = 同层复否，预期存活"),
    ("16", SURVIVE, EXT,
     "    if len(raw) < 2 or len(raw) > max_len:\n",
     "    if len(raw) > max_len:\n",
     "复否出处: probe_py338_gate_redundancy 独占计数 length=0（词表 + 64,700 组合）",
     "长度下限放宽 = 同层复否，预期存活"),
]

WATCH = [DET, EXT, WF]


def md5(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()


def _eol(raw: bytes) -> bytes:
    return b"\r\n" if b"\r\n" in raw else b"\n"


def _expand(pattern: str, eol: bytes) -> bytes:
    """把模式里手写的 `\\n` 换成目标文件真正用的换行符。"""
    return pattern.replace("\n", "\x00").encode("utf-8").replace(b"\x00", eol)


def hits_for(rel: str, old: str) -> int:
    raw = (ROOT / rel).read_bytes()
    return raw.count(_expand(old, _eol(raw)))


def run_tests() -> tuple[int, list[str], str]:
    """rc + **具体哪几条**红 + 最后一行结论。只 `rc!=0` 不足以说明是**对应**那条。"""
    proc = subprocess.run(
        [sys.executable, "-X", "utf8", "-m", "pytest", *TESTS, "-q", "--no-header",
         "-p", "no:cacheprovider", "--tb=no", "-rf"],
        cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=900)
    out = proc.stdout or ""
    names = sorted({ln.split("::")[-1].split("[")[0] for ln in out.splitlines()
                    if ln.startswith("FAILED")})
    tail = [ln for ln in out.splitlines() if ln.strip()][-1:]
    return proc.returncode, names, " | ".join(tail)


def check_patterns() -> int:
    bad = 0
    for mid, _exp, rel, old, _new, _pin, _desc in MUTATIONS:
        hits = hits_for(rel, old)
        if hits != 1:
            bad += 1
            print(f"{mid:>4} 命中 {hits} 次  {rel}  {old.splitlines()[0][:60]}")
    print(f"模式核对：{len(MUTATIONS)} 枚，命中数不为 1 的 {bad} 枚")
    return 1 if bad else 0


def main(argv: list[str]) -> int:
    if "--check" in argv:
        return check_patterns()
    only = set()
    if "--only" in argv:
        only = {s.strip() for s in argv[argv.index("--only") + 1].split(",") if s.strip()}

    baseline = {rel: md5(ROOT / rel) for rel in WATCH}
    code, _bn, out = run_tests()
    print(f"[base] 未变异基线 rc={code} :: {out}")
    if code != 0:
        print("ABORT: 基线就是红的，变异检验无意义")
        return 2

    rows = []
    for mid, exp, rel, old, new, pin, desc in MUTATIONS:
        if only and mid not in only:
            continue
        path = ROOT / rel
        if hits_for(rel, old) != 1:
            rows.append((mid, exp, "NOT_APPLIED", "", pin, desc))
            print(f"[{mid}] NOT_APPLIED :: {desc}")
            continue
        raw = path.read_bytes()
        path.write_bytes(raw.replace(_expand(old, _eol(raw)), _expand(new, _eol(raw)), 1))
        try:
            rc, names, summary = run_tests()
        finally:
            path.write_bytes(raw)
        verdict = "RED" if rc != 0 else "GREEN"
        if exp == RED:
            ok = rc != 0 and pin in names
            tag = "OK" if ok else ("WRONG-TEST" if rc != 0 else "UNCAUGHT")
        else:
            ok = rc == 0
            tag = "OK(复否)" if ok else "UNCAUGHT-GATE-LOOSE"
        rows.append((mid, exp, verdict, ", ".join(names), pin, desc))
        print(f"[{mid}] {tag} :: {desc}\n      {summary}\n      红测: {names or '-'}")

    still = [rel for rel in WATCH if md5(ROOT / rel) != baseline[rel]]
    print("\n=== PY-338 变异检验 ===")
    mismatch = []
    for mid, exp, verdict, names, pin, desc in rows:
        ok = (exp == RED and verdict == "RED" and pin in names.split(", ")) or \
             (exp == SURVIVE and verdict == "GREEN")
        print(f"{mid:>4} 预期{exp:<8} 实测{verdict:<6} {'✓' if ok else '✗'} {desc}")
        print(f"     {'定罪测试' if exp == RED else '复否出处'}: {pin}")
        if names:
            print(f"     红测清单: {names}")
        if not ok:
            mismatch.append(mid)
    print(f"合计 {len(rows)} 枚，与预期不符 {len(mismatch)} 枚 {mismatch or ''}")
    if still:
        print("DIRTY（字节与基线不符）:", still)
        return 1
    if mismatch or not rows:
        print("FAIL: 有变种的定罪测试与声明不符（或无一枚施加）")
        return 1
    print("PASS: RED 枚各有点名测试定罪，SURVIVE 枚有独占计数出处，工作树字节回到基线")
    for rel in WATCH:
        print(f"  md5 {md5(ROOT / rel)}  {rel}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
