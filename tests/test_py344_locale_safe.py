"""PY-344 的可建守卫：凡走文本模式的 subprocess，解码 encoding 必须显式钉死（静态扫描）。

定罪现场（2026-09-24 01:0x 本机实测，非推导）：`tests/test_u10_dead_code_gone.py` 的
`git_root` fixture 用 `text=True`（在 Windows 上＝本机 GBK）去解 git 的 utf-8 输出，
仓库路径含中文时读线程抛异常、`stdout` 变 `None`，纯树全量门于是
920 passed + 4 errors。最坏的不是那 4 枚 error，而是它原先那句
`if not out: pytest.skip("not a git checkout")`——四条引用面/体积守卫会伪装成
"离线复制的树被跳过"而**靠"没跑"变绿**，量具从此永不说话。

本文件守的是同类别，不是那一枚具体调用点：`text=True` / `universal_newlines=True` /
`encoding=None` 三种写法都会把解码交给 `locale.getpreferredencoding()`，而产品里传进去
的字符串（书名、路径、角色名）是中文的。扫描面＝`tests/ + backend/ + modules/ + qa/harness/`
（**`qa/harness/tmp/` 明确排除**：那是 gitignored 的变异草稿树，纳进来会让守卫随本机残档
漂移——本轮实测该目录里有 2 枚命中，而面内 43 枚 harness 脚本 0 命中），并配一条下界
（见 `test_scan_scope_is_wide_enough`）——扫描面被改窄时守卫会静默失去意义，所以那一枚
测的正是"扫描面还够不够宽、排除的那棵草稿树有没有被误纳"。

口径说明：`errors=` 不算过门（它只换乱码的方式，不改默认编码）；纯二进制调用
（只有 `capture_output=True` 而没有 `text=True`）本身安全，不判。反过来只给
`encoding=` 而不给 `text=` 也是合法的 —— encoding 本身就把它切成文本模式，
这正是 PY-344 修后 `git_root` fixture 的写法。守卫的边界因此是"文本模式 ＋ 没钉 encoding"，
不是"必须写满两个参数"。
"""
import ast
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCAN_FACES = ("tests", "backend", "modules", "qa/harness")
# 草稿树整棵排除：`qa/harness/tmp/` 被 gitignore（.gitignore:65），里面是历轮变异脚本的残档。
# 纳进来 ⇒ 守卫会随本机残档漂移（有人清一次 tmp 就少两枚命中，不清就多两枚），那不是量具该有的性质。
# 排除规则本身由 `test_scratch_tree_is_excluded_on_purpose` 钉住，免得日后被人"顺手扩面"扩回去。
EXCLUDE_DIR_PARTS = {"__pycache__", "tmp"}
SUBPROCESS_FUNCS = {"run", "Popen", "call", "check_call", "check_output", "getoutput", "getstatusoutput"}
# 已知案发现场与已知干净样本各一枚：它们必须在扫描面里，否则本守卫当场退化成恒真。
PINNED_IN_SCOPE = ("tests/test_u10_dead_code_gone.py",
                   "modules/voice_manager/voice_crawler/online_net.py",
                   "qa/harness/run_p5_analyze.py")
PINNED_OUT_OF_SCOPE = ("qa/harness/tmp/u5_mutation.py",)


def _scanned_files() -> list:
    files = []
    for face in SCAN_FACES:
        base = ROOT / face
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            if EXCLUDE_DIR_PARTS & set(path.relative_to(ROOT).parts):
                continue
            files.append(path)
    return files


def _calls(tree: ast.AST):
    """产出 (函数名, 节点)，同时认 `subprocess.run(...)` 与 `from subprocess import run`。"""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if isinstance(fn, ast.Attribute):
            owner = fn.value
            owner_name = owner.id if isinstance(owner, ast.Name) else None
            if owner_name == "subprocess":
                yield fn.attr, node
        elif isinstance(fn, ast.Name) and fn.id in SUBPROCESS_FUNCS:
            yield fn.id, node


def _wants_text(kwargs: dict) -> bool:
    for key in ("text", "universal_newlines"):
        node = kwargs.get(key)
        if node is not None and isinstance(node, ast.Constant) and node.value is True:
            return True
    return False


def _violations_in(source: str, relpath: str) -> list:
    """返回 [(relpath, lineno, 归因)] —— 本文件既扫仓库，也扫喂进来的合成片段。"""
    out = []
    tree = ast.parse(source)
    for name, node in _calls(tree):
        if name not in SUBPROCESS_FUNCS:
            continue
        kwargs = {k.arg: k.value for k in node.keywords if k.arg}
        if not _wants_text(kwargs):
            continue
        enc = kwargs.get("encoding")
        pinned = isinstance(enc, ast.Constant) and isinstance(enc.value, str) and enc.value
        if not pinned:
            how = "encoding 未给出" if enc is None else "encoding=None（＝交回 locale）"
            out.append((relpath, node.lineno, f"{name}(..., text/universal_newlines=True) {how}"))
    return out


def _repo_violations() -> list:
    found = []
    for path in _scanned_files():
        rel = path.relative_to(ROOT).as_posix()
        found.extend(_violations_in(path.read_text(encoding="utf-8"), rel))
    return found


def test_no_undecoded_subprocess_text_call():
    """扫描面里不得有"文本模式却没钉 encoding"的 subprocess 调用。"""
    bad = _repo_violations()
    assert not bad, "这些调用点会把解码交给 locale（Windows=GBK），中文路径/书名必炸：\n  " + "\n  ".join(
        f"{f}:{l} {why}" for f, l, why in bad)


def test_scan_scope_is_wide_enough():
    """方向核对：扫描面一旦被改窄，上一条守卫就静默恒真 —— 所以先钉"扫到了谁"。"""
    files = [p.relative_to(ROOT).as_posix() for p in _scanned_files()]
    for needle in PINNED_IN_SCOPE:
        assert needle in files, f"扫描面漏掉了已知案发文件 {needle}"
    assert len(files) >= 250, f"扫描面只剩 {len(files)} 个文件，守卫失去判别力"


def test_scratch_tree_is_excluded_on_purpose():
    """排除面核对：`qa/harness/tmp/`（gitignored 变异草稿）**必须**不在面内。

    这一枚与上一枚是一对：一条防"改窄"，一条防"顺手扩"。本轮实测该目录里有 2 枚
    `text=True` 未钉 encoding 的残档 —— 它们不是产品代码，一旦被算进违例，守卫就变成
    "看本机有没有人忘了清草稿"，红绿灯不再说代码的事。
    """
    files = [p.relative_to(ROOT).as_posix() for p in _scanned_files()]
    for needle in PINNED_OUT_OF_SCOPE:
        assert needle not in files, f"草稿树 {needle} 被纳进了扫描面（守卫会随本机残档漂移）"
    assert not [f for f in files if "qa/harness/tmp/" in f], files[:5]


def test_exclusion_rule_actually_bites(monkeypatch):
    """排除规则得**真的在排除**，不能只是写了个没人走的集合。

    口径说清楚：残档树在不在是本机状态，不是代码性质。所以本枚**总是**先跑那条结构断言
    （`"tmp"` 还在 `EXCLUDE_DIR_PARTS` 里），只有草稿树非空时才继续跑"摘掉规则后面确实变大"
    的效果断言；树为空时效果那一半**不作数也不谎称已证**——这与 PY-344 的教训一致：
    一条守卫可以因为环境而少测一半，但那一半必须被写明"本轮没测到"，不许靠 skip 变绿。
    """
    assert "tmp" in EXCLUDE_DIR_PARTS, "排除规则被摘了：残档会开始决定红绿灯"
    scratch = ROOT / "qa" / "harness" / "tmp"
    py = sorted(p for p in scratch.rglob("*.py") if "__pycache__" not in p.parts) if scratch.is_dir() else []
    if not py:
        return
    with_rule = len(_scanned_files())
    monkeypatch.setattr(sys.modules[__name__], "EXCLUDE_DIR_PARTS", {"__pycache__"})
    without_rule = len(_scanned_files())
    assert without_rule == with_rule + len(py), (
        f"摘掉排除规则只多出 {without_rule - with_rule} 枚，而草稿树里有 {len(py)} 枚 ⇒ 排除规则名不副实"
    )


# ============================ 双向核验：守卫不是"正好没样本"才绿的
GOOD = '''
import subprocess
r = subprocess.run(["git", "log"], capture_output=True, text=True, encoding="utf-8")
'''
RAW_BYTES = '''
from subprocess import run
r = run(["ffmpeg", "-version"], capture_output=True)   # 没有 text=，拿到的是 bytes，安全
'''
BAD_TEXT = 'import subprocess\nsubprocess.run(["x"], text=True)\n'
BAD_UNL = 'import subprocess\nsubprocess.run(["x"], universal_newlines=True)\n'
BAD_NONE = 'import subprocess\nsubprocess.Popen(["x"], stdout=-1, text=True, encoding=None)\n'
BAD_ERRORS_ONLY = 'import subprocess\nsubprocess.run(["x"], text=True, errors="replace")\n'


@pytest.mark.parametrize("source,expected", [
    (GOOD, 0),
    (RAW_BYTES, 0),
    (BAD_TEXT, 1),
    (BAD_UNL, 1),
    (BAD_NONE, 1),
    (BAD_ERRORS_ONLY, 1),   # errors 只换乱码方式，不改默认编码 ⇒ 仍判违规
])
def test_detector_fires_on_the_shape_it_guards(source: str, expected: int):
    """恒真自检：把六种写法喂进同一个判定函数，红的必须红、干净的必须干净。"""
    got = _violations_in(source, "synthetic.py")
    assert len(got) == expected, (got, source)
