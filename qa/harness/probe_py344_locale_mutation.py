"""PY-344 守卫的变异检验：把 encoding 从两处已知案发调用点拿掉，守卫必须当场红。

跑法：`python -X utf8 qa/harness/probe_py344_locale_mutation.py`（在仓库根或任意 cwd 都行）。
三枚变异各自"改字节 → 跑那一枚测 → 复原并核 md5"，最后回干净树再跑一次坐实"绿是复原出来的"。

模型：
  M1 产品面 `online_net.py` 的 ffmpeg 转码调用退回 `text=True` 无 encoding
  M2 测试面 `test_u10_dead_code_gone.py` 的 `git_root` fixture 退回原样（PY-344 定罪现场）
  M3 把扫描面从 ("tests","backend","modules") 改窄成只剩 ("tests",) —— 证明
     `test_scan_scope_is_wide_enough` 不是摆设：M1 仍在面内、M3 会让"面窄"这件事本身被定罪
"""
import hashlib
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
PROD = ROOT / "modules" / "voice_manager" / "voice_crawler" / "online_net.py"
FIXTURE = ROOT / "tests" / "test_u10_dead_code_gone.py"
GUARD = ROOT / "tests" / "test_py344_locale_safe.py"

# M1：产品面现字节是 `text=True, timeout=120, encoding="utf-8", errors="replace"`，拿掉尾部两枚即退回现场
ENC_KW = re.compile(r',\s*\n?\s*encoding="utf-8",\s*errors="replace"')
# M2：测试夹具修后是 `capture_output=True, encoding="utf-8", errors="replace"`（给了 encoding 就不必再给
#     text=True）。忠实回退＝换回 PY-344 的原文 `capture_output=True, text=True`，不是简单删参数。
FIXTURE_REVERT = re.compile(r'capture_output=True, encoding="utf-8", errors="replace"')
SCOPE = re.compile(r'^SCAN_FACES = \(.*?\)$', re.M)
EXCLUDE = re.compile(r'^EXCLUDE_DIR_PARTS = \{.*?\}$', re.M)
# 草稿树＝gitignored 的历轮变异残档；M4 只有在它非空时才"可判"
SCRATCH = ROOT / "qa" / "harness" / "tmp"

TARGETS = [PROD, FIXTURE, GUARD]
SNAP: dict = {}


def md5(path: pathlib.Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()


def run_guard(node_id: str) -> tuple:
    res = subprocess.run([sys.executable, "-X", "utf8", "-m", "pytest", node_id, "-q", "--no-header", "-p", "no:cacheprovider"],
                         cwd=str(ROOT), capture_output=True, text=True,
                         encoding="utf-8", errors="replace")
    return res.returncode, (res.stdout or "") + (res.stderr or "")


def apply(mutations) -> None:
    for path, pattern, repl in mutations:
        text = path.read_text(encoding="utf-8")
        new, n = pattern.subn(repl, text, count=1)
        assert n == 1, f"变异没打上：{path.name} 里找不到 {pattern.pattern!r}"
        path.write_text(new, encoding="utf-8", newline="")


CASES = [
    ("M1 产品面退回未钉 encoding", [(PROD, ENC_KW, "")], "tests/test_py344_locale_safe.py::test_no_undecoded_subprocess_text_call", "online_net.py"),
    ("M2 测试夹具退回 PY-344 现场", [(FIXTURE, FIXTURE_REVERT, 'capture_output=True, text=True')], "tests/test_py344_locale_safe.py::test_no_undecoded_subprocess_text_call", "test_u10_dead_code_gone.py"),
    ("M3 扫描面被改窄成只剩 tests/", [(GUARD, SCOPE, 'SCAN_FACES = ("tests",)')], "tests/test_py344_locale_safe.py::test_scan_scope_is_wide_enough", "扫描面漏掉了已知案发文件"),
    # M4 与前三枚不同：它判的是"排除规则有没有真在排除"，前提是本机 `qa/harness/tmp/` 里
    # 确有 gitignored 残档（本轮实测 2 枚命中）。草稿树被清过的那台机器上它**不可判**，
    # 不可判要如实打出来，不许当"过了"计入分子。
    ("M4 摘掉草稿树排除规则", [(GUARD, EXCLUDE, 'EXCLUDE_DIR_PARTS = {"__pycache__"}')], "tests/test_py344_locale_safe.py::test_no_undecoded_subprocess_text_call", "qa/harness/tmp/",
     SCRATCH),
]


def restore(before: dict) -> list:
    """从内存快照按字节复原，并逐枚核 md5 —— 磁盘上跑完变异必然与快照不同。"""
    dirty = [p for p, digest in before.items() if md5(p) != digest]
    for path, digest in before.items():
        if md5(path) != digest:
            path.write_bytes(SNAP[path])
    return [(p.name, md5(p) == digest) for p, digest in before.items()]


def main() -> int:
    global SNAP
    before = {p: md5(p) for p in TARGETS}
    SNAP = {p: p.read_bytes() for p in TARGETS}
    code, out = run_guard("tests/test_py344_locale_safe.py")
    assert code == 0, "基线不绿，变异检验无意义"
    tail = [ln for ln in out.splitlines() if "passed" in ln or "failed" in ln]
    print(f"BASELINE: tests/test_py344_locale_safe.py exit={code} :: {tail[-1] if tail else out[-80:]}")

    judged, fired, undecided = [], [], []
    try:                             # 中途抛异常也不把变异留在树上（本轮实测被这条救过一次）
        for case in CASES:
            label, muts, node, needle = case[:4]
            precondition = case[4] if len(case) > 4 else None
            if precondition is not None and not any(precondition.rglob("*.py")):
                print(f"{label}: **不可判**（{precondition} 里没有残档，本机这一枚测不到东西）")
                undecided.append(label)
                continue
            apply(muts)
            code, out = run_guard(node)
            ok = code != 0 and needle in out
            print(f"{label}: exit={code} 定罪含『{needle}』={ok}")
            if not ok:
                print(out[-800:])
            judged.append(label)
            if ok:
                fired.append(label)
            restore(before)
    finally:
        restore(before)

    clean_again = all(md5(p) == d for p, d in before.items())
    code, out = run_guard("tests/test_py344_locale_safe.py")
    print(f"RESTORE: 三枚文件按 md5 全部复原={clean_again}")
    tail = [ln for ln in out.splitlines() if "passed" in ln or "failed" in ln]
    print(f"CLEAN TREE RE-RUN: exit={code} :: {tail[-1] if tail else out[-80:]}")
    all_green = clean_again and code == 0 and len(fired) == len(judged)
    verdict = "PY344_MUTATION_OK" if all_green else "PY344_MUTATION_INCOMPLETE"
    print(f"VERDICT: {verdict} 可判 {len(judged)} 枚 / 定罪 {len(fired)} 枚"
          + (f" / 不可判 {len(undecided)} 枚（{'、'.join(undecided)}）" if undecided else ""))
    return 0 if all_green else 1


if __name__ == "__main__":
    sys.exit(main())
