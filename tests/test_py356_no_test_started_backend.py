"""PY-356 防复发守卫：测试进程里绝不许顺手起出一台真后端（2026-09-26 21:24:52 全量门现场）。

定罪（诊断智能体实跑 + 本轮复证）：`tests/test_launcher_no_reload_argv_exec.py` 的开窗用例
把 `subprocess.run` 打成抛 OSError、`IS_WIN=True`，注释写「走 except 分支也无妨」，
但 `spawn_window()` 的 except 分支当时**没有 return** —— 直接掉进兜底：
`open(lg.log_path, 'ab')` + `subprocess.Popen(lg.resolved_argv(), cwd=lg.cwd)`，
argv 是生产 argv（`legs()` 钉死 `-m uvicorn backend.main:app --host 127.0.0.1 --port 8000`）。
于是全量门里真起了一台后端：抢用户那台 :8000（抢不动、报 10048，但启动钩子已跑完、
往该树 data/peiyinyuan.db 写过库），日志原文 `Started server process [78856]`。

两层判据：
① 静态扫 `tests/*.py`：任何调用 `spawn_window(` 的作用域，若该作用域没把
   `subprocess.Popen` / `start_all.Popen` 桩掉就判红。口径照 `tests/test_py344_locale_safe.py`
   （AST 静态守卫 + 恒真自检 + 扫描面下界）。
② session 级端口归属：开局记 127.0.0.1:8000 的监听 PID 集合，teardown 复采。
   判据是「**变了**」（冒出没见过的 PID / 原 PID 不再在听），**不是「有人在跑」**——
   用户自己那台（本轮 = PID 90376）正常在跑绝不是缺陷，一条测试都不该为此变红。
"""
from __future__ import annotations

import ast
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
TESTS = ROOT / "tests"
PORT = 8000


# ---------------------------------------------------------------- ① 静态扫 tests/
def _scanned_files() -> list:
    return sorted(p for p in TESTS.glob("*.py") if "__pycache__" not in p.parts)


def _call_name(fn):
    if isinstance(fn, ast.Attribute):
        return fn.attr
    if isinstance(fn, ast.Name):
        return fn.id
    return None


def _spawn_calls_under(node: ast.AST) -> list:
    """node 子树里的 spawn_window 调用，**不下钻**嵌套函数定义（那些归它们自己的作用域）。"""
    out = []
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if isinstance(child, ast.Call) and _call_name(child.func) == "spawn_window":
            out.append(child)
        out.extend(_spawn_calls_under(child))
    return out


def _enclosing_scopes(tree: ast.AST):
    """产出 (作用域节点, [该作用域内的 spawn_window 调用])：每个函数体一层，其余归模块。"""
    claimed = set()
    for fn in [n for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
        calls = _spawn_calls_under(fn)
        if calls:
            claimed.update(id(c) for c in calls)
            yield fn, calls
    mod = [c for c in ast.walk(tree)
           if isinstance(c, ast.Call) and _call_name(c.func) == "spawn_window"
           and id(c) not in claimed]
    if mod:
        yield tree, mod


def _popen_patched_in(scope) -> bool:
    """该作用域里是否出现 `monkeypatch.setattr(<subprocess|start_all...>, "Popen", ...)`。

    认三种写法：目标 `subprocess`（模块本体）、`start_all`（属性 Popen 被桩）、
    以及 `start_all.subprocess` / `x.subprocess`（leg_scope 里那种打在模块对象上的写法）。
    """
    for node in ast.walk(scope):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if not (isinstance(fn, ast.Attribute) and fn.attr == "setattr"):
            continue
        if len(node.args) < 3:
            continue
        target, attr = node.args[0], node.args[1]
        if not (isinstance(attr, ast.Constant) and attr.value == "Popen"):
            continue
        if isinstance(target, ast.Attribute) and target.attr in ("subprocess", "start_all"):
            return True
        if isinstance(target, ast.Name) and target.id in ("subprocess", "start_all"):
            return True
    return False


def _violations_in(source: str, relpath: str) -> list:
    """[(relpath, lineno, 归因)]：调了 spawn_window 却没桩 Popen 的作用域。"""
    out = []
    for scope, calls in _enclosing_scopes(ast.parse(source)):
        if _popen_patched_in(scope):
            continue
        for node in calls:
            out.append((relpath, node.lineno,
                        "调用 spawn_window(...) 未在同作用域桩住 subprocess.Popen"))
    return out


def _repo_violations() -> list:
    found = []
    for path in _scanned_files():
        rel = path.relative_to(ROOT).as_posix()
        found.extend(_violations_in(path.read_text(encoding="utf-8"), rel))
    return found


def test_no_test_calls_spawn_window_without_patching_popen():
    """tests/ 里任何直接调 spawn_window 的作用域必须桩住 Popen —— 否则它就真能起一台后端。"""
    bad = _repo_violations()
    assert not bad, "这些测试会绕过桩真的起进程（PY-356 的形状）：\n  " + "\n  ".join(
        f"{f}:{l} {why}" for f, l, why in bad)


# 已知案发文件必须在面内，否则守卫退化成恒真。
PINNED_IN_SCOPE = ("tests/test_launcher_no_reload_argv_exec.py",
                   "tests/test_launcher_leg_scope.py",
                   "tests/test_py356_no_test_started_backend.py")


def test_scan_scope_covers_the_known_scene():
    files = [p.relative_to(ROOT).as_posix() for p in _scanned_files()]
    for needle in PINNED_IN_SCOPE:
        assert needle in files, f"扫描面漏掉了 {needle}"
    assert len(files) >= 60, f"tests/ 只剩 {len(files)} 个文件，扫描面被改窄了（本轮实测量级 ~75）"


CLEAN_CALL = '''
import subprocess
def test_a(monkeypatch):
    monkeypatch.setattr(subprocess, "Popen", _Recorder())
    spawn_window(lg)
'''
CLEAN_CALL_MODULE_ATTR = '''
def test_b(monkeypatch, start_all):
    monkeypatch.setattr(start_all.subprocess, "Popen", stub)
    start_all.spawn_window(lg)
'''
DIRTY_CALL = '''
def test_c():
    start_all.spawn_window(lg)      # 没桩 Popen ⇒ PY-356 的形状
'''
DIRTY_NESTED = '''
def test_d(monkeypatch):
    def inner():
        spawn_window(lg)            # 嵌套函数自己没桩，父作用域桩了也不算
    monkeypatch.setattr(subprocess, "Popen", stub)
    inner()
'''


@pytest.mark.parametrize("source,expected", [
    (CLEAN_CALL, 0),
    (CLEAN_CALL_MODULE_ATTR, 0),
    (DIRTY_CALL, 1),
    (DIRTY_NESTED, 1),
])
def test_detector_fires_on_the_shape_it_guards(source: str, expected: int):
    """恒真自检：四种写法喂进同一个判定函数，脏的红、干净的不红。"""
    got = _violations_in(source, "synthetic.py")
    assert len(got) == expected, (got, source)


# ---------------------------------------------------------------- ② 端口归属账
def _listening_pids_on(port: int = PORT):
    """`port` 的监听者 PID 集合；psutil 不可用返回 None（≠ 空集，不许蒙混）。"""
    try:
        import psutil
    except Exception:
        return None
    pids = set()
    for c in psutil.net_connections(kind="tcp"):
        if c.laddr and c.laddr.port == port and c.status == psutil.CONN_LISTEN and c.pid:
            pids.add(int(c.pid))
    return frozenset(pids)


def _listening_pids_on_8000():
    return _listening_pids_on(PORT)


@pytest.fixture(scope="session")
def port8000_baseline(request):
    pids = _listening_pids_on_8000()
    if pids is None:
        pytest.skip("本机 psutil 不可用 —— 端口归属采不到，诚实跳过而不是判绿")
    yield pids
    now = _listening_pids_on_8000()
    assert now is not None, "teardown 采不到端口归属了"
    new = sorted(now - pids)
    gone = sorted(pids - now)
    assert not new, (
        f"本 pytest 会话期间 127.0.0.1:{PORT} 冒出新监听者 {new}（开局={sorted(pids)}）"
        " —— 有测试起了后端（PY-356），查 logs/backend-8000.log 的 launch / Started server 行")
    assert not gone, (
        f"开局在听的 {sorted(pids)} 里 {gone} 会话结束时不在了 —— 有测试动了在跑的实例，"
        "同样判红（哪怕它自称只是重启了一下）")


def test_no_test_changed_who_listens_on_8000(port8000_baseline):
    """会话收尾由 fixture 复采判「归属有没有变」。

    本枚自己不起服务、不发 HTTP：用户那台正常在跑（基线非空）时它照样绿，
    只有测试会话给 :8000 添了新主人、或弄丢了原主人才会红。
    """
    assert isinstance(port8000_baseline, frozenset), port8000_baseline


def test_port_change_detector_bites_on_a_real_new_listener():
    """反向自证（判据不是恒绿）：空闲端口上真出现新监听者时，采集器必须报出那个 PID。

    口径：只在**自绑的临时端口**上做（拿不到 = 判据坏；残留在 teardown 里判掉），
    绝不碰 :8000 —— 用户那台正占着它，在它上面自绑只会 10048 失败，
    那样测出来的"没变化"是假证（本轮实测过一次：`new=[]` 因为压根没绑上）。
    """
    import socket
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    base = _listening_pids_on(port)
    assert base is not None, "psutil 不可用 ⇒ 本枚无从自证"
    assert base == frozenset(), f"临时端口 {port} 开局不该有人监听: {base}"
    probe.listen(5)
    try:
        now = _listening_pids_on(port)
        assert now - base, f"自绑的新监听者没被差集抓出来（采到 {now}）⇒ ②号判据是恒绿的摆设"
    finally:
        probe.close()
    import time
    time.sleep(0.2)
    assert not _listening_pids_on(port), f"套接字已关仍有残留监听者 ⇒ 归因会误报 {port}"
