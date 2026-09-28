"""U-10 变异检验：死代码守卫 + 入库策略守卫，逐枚反向施加必须按预期定罪。

与 `run_u11_mutation.py` / `run_u8_mutation.py` 同一套规矩（那三条是被 U-7 首轮
10 枚 `NOT_APPLIED` 换来的，原样继承）：
  * 替换串必须**逐字命中一次**，命不中报 `NOT_APPLIED`，不算变异通过；
  * 模式里的 `\\n` 按**目标文件实际换行符**展开（`core.autocrlf=true` 的树检出 CRLF）；
  * 读写一律二进制，跑完核对基线，任何字节/索引差异 = 还原失败，非零退出；
  * **每枚撤销后马上核对**（字节 + `git status`），不干净就当场停 —— 首轮就是把这条
    放在收尾，M8/M9 的纯删除型变异还原失败（`new=""` 的反向串是空串，永远命不中），
    于是后面 8 枚的红清单里一直混着那两枚无关测试，归因整轮作废。

比前几轮多加的一条：不只要求"变红"，还要求**被预期的那枚测试变红**。
`900 passed` 的门里塞进 17 枚守卫，只看 rc!=0 会分不清是谁干的 —— 定罪错位说明
这枚变异其实被隔壁更宽的守卫挡了，真实有效性就没证到。

四类施加方式：
  text      —— 改源文件字面量（还原：写回原字节）
  file_new  —— 把已删文件放回盘上（还原：删除，脚本自己创建的才敢删）
  index_add —— 生成 scratch 缓存并 `git add -f`（还原：`git rm --cached` + 删除）
  index_rm  —— 把在册判定记录摘出索引（还原：`git add` 放回，盘上文件从不动）

跑法（干净 worktree 根，勿在产品树/未提交改动上跑）：
    PYTHONUTF8=1 python -X utf8 qa/harness/run_u10_mutation.py --check
    PYTHONUTF8=1 python -X utf8 qa/harness/run_u10_mutation.py
    PYTHONUTF8=1 python -X utf8 qa/harness/run_u10_mutation.py --only M12,M14
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TESTS = ["tests/test_u10_dead_code_gone.py"]

WF_INIT = "modules/workflow/__init__.py"
PIPELINE = "modules/workflow/pipeline.py"
LAUNCHER = "start_all.py"
SETUP_DEPS = "tools/setup_deps.py"
PYPROJ = "pyproject.toml"
COMPOSE = "docker/docker-compose.yml"
UIBARREL = "frontend/src/components/ui/index.ts"
APITS = "frontend/src/services/api.ts"
GUARD = "tests/test_u10_dead_code_gone.py"
CACHE_DIR = "tools/voice_library/real_speech/_tmp"

MUTATIONS = [
    # ---- ① 死代码不复生 ----
    dict(id="M1", kind="file_new", path="modules/workflow/task_queue.py",
         content="class TaskQueue:\n    \"\"\"mutated: 死模块被放回盘上\"\"\"\n    pass\n",
         kills="test_dead_modules_are_gone",
         desc="已删的 task_queue.py 回到盘上（同族 grep 守卫会一起红）"),
    dict(id="M2", kind="text", path=WF_INIT,
         old="from .progress_tracker import ProgressTracker",
         new="from .progress_tracker import ProgressTracker\n\nTaskQueue = None  # mutated: 死名重新导出",
         kills="test_workflow_package_still_imports_the_live_parts",
         desc="modules.workflow 重新导出 TaskQueue"),
    dict(id="M3", kind="text", path=PIPELINE,
         old="import asyncio\nfrom enum import Enum\n",
         new="import asyncio\nfrom enum import Enum\nfrom modules.workflow.task_queue import TaskQueue  # mutated\n",
         kills="test_no_executable_file_references_dead_symbols",
         desc="活模块重新 import 已删模块"),
    dict(id="M4", kind="text", path=LAUNCHER,
         old='        Leg("backend", "PY-Backend", 8000, HERE,\n',
         new='        Leg("celery-worker", "PY-Worker", 8010, HERE,\n'
             '            ["celery -A backend.tasks.celery_app worker"],\n'
             '            signs=("celery",), note="mutated: worker 腿回来了"),\n'
             '        Leg("backend", "PY-Backend", 8000, HERE,\n',
         kills="test_launcher_entries_have_no_worker_leg",
         desc="一键启动器重新拉起 Celery worker 腿（U-1 退役 start.ps1 后，worker 形的守家在 start_all.py）"),
    dict(id="M5", kind="text", path=PYPROJ,
         old='    "backend.services",\n',
         new='    "backend.services",\n    "backend.tasks",\n',
         kills="test_pyproject_does_not_package_deleted_dirs",
         desc="pyproject 又声明已删的包（构建期才炸的那类错）"),
    dict(id="M6", kind="text", path=COMPOSE,
         old="  # ========== Redis 消息队列 ==========\n  redis:",
         new="  celery-worker:\n    build:\n      context: ..\n      dockerfile: docker/Dockerfile.backend\n"
             '    command: celery -A backend.tasks.celery_app worker\n\n'
             "  # ========== Redis 消息队列 ==========\n  redis:",
         kills="test_compose_has_no_worker_service_and_still_resolves",
         desc="compose 里的 celery-worker 服务复活"),
    dict(id="M7", kind="text", path=COMPOSE,
         old="    depends_on:\n      - backend\n    restart: unless-stopped",
         new="    depends_on:\n      - backend\n      - celery-worker\n    restart: unless-stopped",
         kills="test_compose_has_no_worker_service_and_still_resolves",
         desc="depends_on 指向未定义服务（PY-301 同族：up 直接报 undefined service）"),
    dict(id="M8", kind="text", path=UIBARREL,
         old='export * from "./Feedback";\n',
         new="",
         kills="test_ui_kit_still_owns_those_components",
         desc="ui 桶里丢掉 Feedback：能力被删而不只是旧壳"),
    dict(id="M9", kind="text", path=GUARD,
         old='              "frontend/src/**/*.ts", "frontend/src/**/*.tsx",\n',
         new="",
         kills="test_scan_scope_is_wide_enough",
         desc="扫描面被改窄（守卫会静默恒真，靠方向核对抓）"),
    dict(id="M10", kind="text", path=SETUP_DEPS,
         old='        ("celery", "Celery"),\n',
         new='        ("celery", "Celery"),\n        ("celery -A backend.tasks.celery_app worker", "Celery Worker"),  # mutated: 环境清单被写成任务腿\n',
         kills="test_celery_left_is_only_the_setup_deps_indicator",
         desc="U-1 后 celery 的唯一残量（setup_deps.py 环境检查项）被写回成 worker 腿"),
    dict(id="M18", kind="file_new", path="start_gui.py",
         content="import celery  # mutated: 已下线的旧 GUI 入口回到盘上\n",
         kills="test_celery_left_is_only_the_setup_deps_indicator",
         desc="旧入口 start_gui.py 复活（该条守卫的前提就是它必须不在盘上）"),
    dict(id="M11", kind="text", path=APITS,
         old="import axios from 'axios';\n",
         new="import axios from 'axios';\n\n// mutated: 旧组件路径 components/common/Modal 仍在册\n",
         kills="test_no_executable_file_references_dead_symbols",
         desc="前端源码重新引用已删的 components/common"),
    # ---- ④ 入库策略 ----
    dict(id="M12", kind="index_add", path=f"{CACHE_DIR}/mutation_probe_small.json",
         content="{}\n",
         kills="test_regenerable_cache_is_not_tracked",
         desc="gitignore 门被 `git add -f` 绕过，缓存文件回库（只 2 字节，体积守卫须保持绿）"),
    dict(id="M13", kind="index_add", path="tools/voice_library/mutation_probe.flac",
         content="fLaC mutated\n",
         kills="test_regenerable_cache_is_not_tracked",
         desc="音频原始件入库（不在 _tmp 下，走的是第二条断言）"),
    dict(id="M14", kind="index_add", path=f"{CACHE_DIR}/mutation_probe_big.bin",
         bytes_=13 * 1024 * 1024,
         kills="test_tracked_size_within_budget",
         desc="缓存回库把入库体积推过 20 MiB 上界（真实超预算场景）"),
    dict(id="M15", kind="text", path=GUARD,
         old="TRACKED_BYTES_BUDGET = 20 * 1024 * 1024",
         new="TRACKED_BYTES_BUDGET = 1 * 1024 * 1024",
         kills="test_tracked_size_within_budget",
         desc="上界被写歪到当前体积以下（守卫应立刻叫，而不是默默恒真）"),
    dict(id="M16", kind="text", path=GUARD,
         old="TRACKED_BYTES_FLOOR = 6 * 1024 * 1024",
         new="TRACKED_BYTES_FLOOR = 60 * 1024 * 1024",
         kills="test_tracked_size_within_budget",
         desc="下界被抬高：证明它真在挡'把仓库删空来变绿'"),
    dict(id="M17", kind="index_rm", path="qa/harness/fixtures/real/real_sample.mobi",
         kills="test_judgment_records_survive_the_slim",
         desc="真实格式夹具被瘦身波连带摘出索引"),
]

TEXT_FILES = sorted({m["path"] for m in MUTATIONS if m["kind"] == "text"})


def md5(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()


def _eol(raw: bytes) -> bytes:
    return b"\r\n" if b"\r\n" in raw else b"\n"


def _expand(pattern: str, eol: bytes) -> bytes:
    return pattern.replace("\n", "\x00").encode("utf-8").replace(b"\x00", eol)


def _repo_eol() -> bytes:
    return _eol((ROOT / PYPROJ).read_bytes())


def apply_mutation(m: dict) -> tuple[bool, str]:
    """返回 (是否施加成功, 说明)。施加失败必须报 NOT_APPLIED，不能算定罪。"""
    kind, path = m["kind"], ROOT / m["path"]
    if kind == "text":
        raw = path.read_bytes()
        needle = _expand(m["old"], _eol(raw))
        hits = raw.count(needle)
        if hits != 1:
            return False, f"模式命中 {hits} 次"
        path.write_bytes(raw.replace(needle, _expand(m["new"], _eol(raw)), 1))
        return True, ""
    if kind == "file_new":
        if path.exists():
            return False, "目标文件本来就在盘上，不是本脚本造的，拒绝动"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(_expand(m["content"], _repo_eol()))
        return True, ""
    if kind == "index_add":
        if path.exists():
            return False, "scratch 文件残留，先手工清掉再跑"
        path.parent.mkdir(parents=True, exist_ok=True)
        if m.get("bytes_"):
            with open(path, "wb") as fh:
                fh.write(b"\0" * m["bytes_"])
        else:
            path.write_bytes(_expand(m["content"], _repo_eol()))
        git("add", "-f", "--", m["path"])
        return True, ""
    if kind == "index_rm":
        res = git("rm", "--cached", "--quiet", "--", m["path"])
        if res.returncode != 0:
            return False, f"git rm --cached 失败: {res.stderr.strip()[:120]}"
        if not path.exists():
            return False, "盘上文件不见了 —— 立即停止，本轮结果作废"
        return True, ""
    return False, f"未知 kind {kind}"


def undo_mutation(m: dict, base_bytes: dict[str, bytes]) -> str | None:
    kind, path = m["kind"], ROOT / m["path"]
    if kind == "text":
        raw = path.read_bytes()
        if m["new"]:
            needle = _expand(m["new"], _eol(raw))
            if raw.count(needle) != 1:
                return "写回时找不到变异串"
            path.write_bytes(raw.replace(needle, _expand(m["old"], _eol(raw)), 1))
        else:
            # 纯删除型变异（new=""）反向串是空串，count(b"")==0 恒不成立 —— 直接用基线字节回写
            path.write_bytes(base_bytes[m["path"]])
        if md5(path) != hashlib.md5(base_bytes[m["path"]]).hexdigest():
            return "回写后字节仍与基线不符"
        return None
    if kind == "file_new":
        if path.exists():
            path.unlink()
        return None
    if kind == "index_add":
        git("rm", "--cached", "--quiet", "--force", "--", m["path"])
        if path.exists():
            path.unlink()
        return None
    if kind == "index_rm":
        res = git("add", "-f", "--", m["path"])
        return None if res.returncode == 0 else res.stderr.strip()[:120]
    return f"未知 kind {kind}"


def git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(ROOT), capture_output=True)


def status_snapshot() -> set[str]:
    out = git("status", "--porcelain").stdout.decode("utf-8", "replace")
    return {ln for ln in out.splitlines() if ln.strip()}


def run_tests() -> tuple[int, set[str], str]:
    proc = subprocess.run(
        [sys.executable, "-X", "utf8", "-m", "pytest", *TESTS, "-q", "--no-header",
         "--tb=no", "-rf", "-p", "no:cacheprovider"],
        cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=900)
    failed = set()
    for ln in (proc.stdout or "").splitlines():
        if ln.startswith("FAILED ") or ln.startswith("ERROR "):
            failed.add(ln.split(" ")[1].split("::")[-1].split(" ")[0])
    tail = [ln for ln in (proc.stdout or "").splitlines() if ln.strip()][-2:]
    return proc.returncode, failed, " | ".join(tail)


def check_patterns() -> int:
    """只核对命中数：不写盘、不跑测试，先排掉 `NOT_APPLIED`。"""
    bad = 0
    for m in MUTATIONS:
        if m["kind"] != "text":
            continue
        path = ROOT / m["path"]
        if not path.exists():
            bad += 1
            print(f"{m['id']:>4} 目标文件不在盘上  {m['path']}（入口退役后要把这枚重挂到活文件）")
            continue
        raw = path.read_bytes()
        hits = raw.count(_expand(m["old"], _eol(raw)))
        if hits != 1:
            bad += 1
            print(f"{m['id']:>4} 命中 {hits} 次  {m['path']}")
    print(f"模式核对：{len(MUTATIONS)} 枚（text {len([x for x in MUTATIONS if x['kind'] == 'text'])} 枚），"
          f"命中数不为 1 的 {bad} 枚")
    return 1 if bad else 0


def main(argv: list[str]) -> int:
    if "--check" in argv:
        return check_patterns()
    only = set()
    if "--only" in argv:
        only = {s.strip() for s in argv[argv.index("--only") + 1].split(",") if s.strip()}

    base_md5 = {rel: md5(ROOT / rel) for rel in TEXT_FILES}
    base_bytes = {rel: (ROOT / rel).read_bytes() for rel in TEXT_FILES}
    base_status = status_snapshot()
    if base_status:
        print("ABORT: 起始工作树不干净:", sorted(base_status)[:5])
        return 2
    code, _, out = run_tests()
    print(f"[base] 未变异基线 rc={code} :: {out}")
    if code != 0:
        print("ABORT: 基线就是红的，变异检验无意义")
        return 2

    def is_clean() -> str | None:
        """每枚变异撤销后立刻核对：字节 + git 状态两头都要回到基线。
        上一版只在收尾核对，结果 M8/M9 的还原失败污染了后面全部归因（红清单里
        混进了两枚与本枚变异无关的测试），整轮 17 枚作废重跑。"""
        dirty = [rel for rel in TEXT_FILES if md5(ROOT / rel) != base_md5[rel]]
        if dirty:
            return f"字节与基线不符: {dirty}"
        now = status_snapshot()
        if now != base_status:
            return f"git 状态与基线不符: {sorted(now ^ base_status)}"
        return None

    results = []
    for m in MUTATIONS:
        if only and m["id"] not in only:
            continue
        applied, why = apply_mutation(m)
        if not applied:
            results.append((m, "NOT_APPLIED", set(), why))
            print(f"[{m['id']}] NOT_APPLIED :: {why}")
            continue
        try:
            rc, failed, tail = run_tests()
        finally:
            err = undo_mutation(m, base_bytes)
        verdict = "RED" if rc != 0 else "GREEN(守卫无效)"
        if err:
            verdict = f"UNDO_FAIL({err})"
        elif verdict == "RED" and m["kills"] not in failed:
            verdict = "RED(定罪错位)"
        results.append((m, verdict, failed, tail))
        print(f"[{m['id']}] {verdict} :: {m['desc']}\n      红: {sorted(failed) or tail}")
        leak = is_clean()
        if leak:
            print(f"ABORT: [{m['id']}] 撤销后树不干净，继续跑只会污染后面的归因 —— {leak}")
            return 1

    print("\n=== U-10 变异检验 ===")
    ok = 0
    for m, verdict, _failed, _tail in results:
        print(f"{m['id']:>4} {verdict:<16} {m['kills']}")
        if verdict == "RED":
            ok += 1
    print(f"合计 {len(results)} 枚，按预期定罪 {ok}，其余 {len(results) - ok}")

    leak = is_clean()
    if leak:
        print("DIRTY:", leak)
        return 1
    if ok != len(results) or not results:
        print("FAIL: 有变异未被对应测试按预期定罪（或无一枚施加）")
        return 1
    print("PASS: 每枚守卫都有对应变异把它单独钉红，工作树字节与 git 索引回到基线")
    return 0


if __name__ == "__main__":
    os.chdir(ROOT)
    raise SystemExit(main(sys.argv[1:]))
