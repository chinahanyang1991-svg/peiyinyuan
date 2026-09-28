# -*- coding: utf-8 -*-
"""U-10（CODE-SLIM 第二轮）死代码不复生守卫。

为什么删完还要留一枚测试：这一类删除的失效方式是「回来得很安静」——某次重构把
`celery -A backend.tasks.celery_app` 那条腿重新写进启动器，或把
`from .task_queue import TaskQueue` 补回 `__init__.py`，运行时不会立刻炸（celery
装了就能 import），但任务实际走进程内 asyncio，两条腿同时存在时才见真分歧。

三段（① 死代码不复生，② 删的是旧壳不是能力，③ 打包与部署口径跟着收口，④ 入库策略）：
  ① 死模块/死文件确实不在，且全仓零引用（源码/脚本/配置三层各扫一遍，文档与 qa 产物不在射程内）
  ② `modules.workflow` 与 `components/ui` 仍按名可导可渲染（证明能力没跟着一起删）
  ③ pyproject 不打包已删目录、compose 无 worker 服务且仍可解析
  ④ 可再生缓存（下载件/分片/license 原文/转写 dump）不回库，判定记录与真实格式夹具不许被一起删掉

第④段额外带一枚**下界**断言：体积守卫的正确失效方向是"悄悄变胖"，但如果有人靠
`git rm -r --cached` 掉整棵 `tools/` 或删夹具来变绿，上界会一起变绿 —— 所以上界和下界
成对写，且判定记录另列白名单单独守。
"""
import re
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]

DEAD_PATHS = [
    "backend/tasks/__init__.py",
    "backend/tasks/celery_app.py",
    "backend/tasks/convert_task.py",
    "modules/workflow/task_queue.py",
    "modules/workflow/task_scheduler.py",
    "frontend/src/components/common/index.ts",
    "frontend/src/components/common/Modal.tsx",
    "frontend/src/components/common/EmptyState.tsx",
    "frontend/src/components/common/Loading.tsx",
    "frontend/src/components/common/ConfirmDialog.tsx",
    "frontend/src/components/animations/useAnime.ts",
]

# 死符号：这些名字只应出现在本守卫与文档里，绝不该再被代码引用
DEAD_SYMBOLS = re.compile(
    r"backend\.tasks|backend/tasks|celery_app|run_conversion_task|cleanup_old_tasks"
    r"|TaskQueue|TaskScheduler|task_queue|task_scheduler|Start-CeleryWorker"
    r"|components/common|components/animations|useAnime"
)

# 扫描范围 = 可执行面（Python 源码 + 前端源码 + 启动器 + 打包/部署配置）
# start*.bat / start*.ps1 留着：旧入口若从 git 历史被复活回来，仍在射程内（U-1，2026-09-27）。
SCAN_GLOBS = ["backend/**/*.py", "modules/**/*.py", "tests/**/*.py", "tools/**/*.py",
              "frontend/src/**/*.ts", "frontend/src/**/*.tsx",
              "start*.py", "start*.ps1", "start*.bat", "start*.cmd", "docker/*.yml",
              "一键启动.bat", "pyproject.toml"]

# 本文件自己把死符号当字面量写着用，扫它会必然自指命中
SELF = Path(__file__).name

# 可再生缓存层：抓取下载件、range 分片、license 原文、说话人转写 dump。
# 逐轮漂移且体积以百 MB 计，只入判定摘要（manifest/gates/verdicts）与台账。
CACHE_DIR_MARKER = "/_tmp/"
RAW_AUDIO_SUFFIXES = (".flac", ".wav", ".mp3", ".ogg")

# 上界：2026-09-23 摘掉 `_tmp`（220 文件 / 67.59 MiB）后实测 12.90 MiB，留 ~70% 余量
TRACKED_BYTES_BUDGET = 20 * 1024 * 1024
# 下界：低于此值说明"变绿"是靠删东西而不是靠不入库
TRACKED_BYTES_FLOOR = 6 * 1024 * 1024

# 判定记录与真实格式夹具：回测的依据，绝不允许跟着瘦身一起消失
MUST_BE_TRACKED = [
    "qa/harness/fixtures/real/real_sample.mobi",
    "qa/harness/fixtures/real/real_sample.azw3",
    "qa/harness/fixtures/sample.epub",
    "tools/voice_library/real_speech/manifest_vctk.json",
    "tools/voice_library/real_speech/manifest_aishell3.json",
    "tools/voice_library/real_speech/real_speech_manifest.json",
    "tools/voice_library/real_speech/gates_vctk.json",
    "tools/voice_library/asr_purge/verdicts_asr.json",
    "tools/voice_library/edge_catalog/edge_manifest.json",
]


def _scanned_files():
    seen = set()
    for pattern in SCAN_GLOBS:
        for p in ROOT.glob(pattern):
            if p.is_file() and "__pycache__" not in p.parts and p.name != SELF:
                seen.add(p)
    return sorted(seen)


# ==================================== ① 死模块已删，活模块仍可用
def test_dead_modules_are_gone():
    for rel in DEAD_PATHS:
        p = ROOT / rel
        assert not p.exists(), f"{rel} 又回到盘上：Celery 双轨会复活"


def test_workflow_package_still_imports_the_live_parts():
    import modules.workflow as wf

    assert {"Pipeline", "PipelineStage", "ProgressTracker"} <= set(dir(wf))
    for dead in ("TaskQueue", "TaskScheduler"):
        assert not hasattr(wf, dead), f"modules.workflow 仍导出 {dead}"


# ==================================== ② 可执行面零引用
def test_no_executable_file_references_dead_symbols():
    offenders = []
    for p in _scanned_files():
        text = p.read_text(encoding="utf-8", errors="replace")
        for m in DEAD_SYMBOLS.finditer(text):
            line = text[: m.start()].count("\n") + 1
            offenders.append(f"{p.relative_to(ROOT)}:{line}")
    assert not offenders, "死符号被重新引用: " + ", ".join(offenders)


def test_celery_left_is_only_the_setup_deps_indicator():
    """U-1（2026-09-27）：`start_gui.py` 那枚 `import celery` 指示器随旧入口一起退役；
    celery 在可执行面的全部残量只剩 `setup_deps.py` 的环境检查清单 —— 它不得再被
    写成任务腿（腿的形状由 test_launcher_entries_have_no_worker_leg 守着）。"""
    assert not (ROOT / "start_gui.py").exists(), \
        "start_gui.py 回到盘上：本条的前提（指示器已随旧入口退役）不再成立"
    src = (ROOT / "tools" / "setup_deps.py").read_text(encoding="utf-8")
    assert "celery_app" not in src and "-m celery" not in src and "celery -A" not in src


def test_launcher_entries_have_no_worker_leg():
    for name in ("start_all.py", "start-all.cmd", "一键启动.bat"):
        src = (ROOT / name).read_text(encoding="utf-8", errors="replace")
        assert not re.search(r"celery\s+-A|-m celery|Start-CeleryWorker", src), (
            f"{name} 又拉起 Celery worker：删掉的 backend.tasks 会立刻 ImportError"
        )


# ==================================== ③ 打包 / 部署口径
def test_ui_kit_still_owns_those_components():
    """删的是**旧壳**，不是能力：同名组件必须由 `components/ui` 继续提供并被真实页面用到。
    只断言"文件没了"会漏掉另一种回退 —— 有人把 common 里那套再抄回来。"""
    ui = ROOT / "frontend" / "src" / "components" / "ui"
    assert ui.is_dir()
    barrel = (ui / "index.ts").read_text(encoding="utf-8")
    for mod in ("Modal", "Feedback"):
        assert f"./{mod}" in barrel, f"ui 桶里不见了 {mod}"
    pages = [ROOT / "frontend" / "src" / "routes" / name
             for name in ("ConvertPage.tsx", "DubbingPage.tsx")]
    for page in pages:
        src = page.read_text(encoding="utf-8")
        assert "EmptyState" in src and re.search(r"from ['\"]\.\./components", src), (
            f"{page.name} 不再用活的那套空态组件"
        )



def test_pyproject_does_not_package_deleted_dirs():
    """③ 打包口径：09-28 起 `packages` 改成 `packages.find` 自动发现（手写清单曾既漏
    在用包又列着已删空壳）。改判据的动机不是凑绿——旧写法读的是**清单原文**，换成
    find 表后 `for pkg in packages` 迭代到的是字典键，第一条断言直接恒真、第二条拿
    "find" 去 joinpath 当场红。现在调 setuptools 自己的 `find_packages`，拿**构建期
    真正会入包的那份名单**来判，判三件事：

      1. 已删的目录（backend.tasks 那套 Celery 死腿 + 三枚空壳）不得出现在里面；
      2. 里面每一枚都落在盘上（include 写错 = 静默少打包，构建期才炸）；
      3. 在用包必须在里面 —— 这条专防"自动发现捞空"：include 漂了会让清单变成空集，
         那时 1、2 都成了空转的真话，守卫靠"没包东西"变绿比靠"包了死目录"变红危险。
    """
    import tomllib
    from setuptools import find_packages

    with open(ROOT / "pyproject.toml", "rb") as fh:
        cfg = tomllib.load(fh)["tool"]["setuptools"]["packages"]
    assert "find" in cfg, "packages 不再是 packages.find 自动发现（回到手写清单就是漂移源头）"
    find_cfg = cfg["find"]
    include = find_cfg.get("include") or []
    assert include, "packages.find 的 include 是空的 —— 那样等于什么都不打包"

    found = set()
    for where in find_cfg.get("where", ["."]):
        found |= set(find_packages(str(ROOT / where), include=include))
    found = sorted(found)
    assert found, "按 pyproject 的 where/include 发现出来是空集：打包出去就是一台哑巴安装"

    for dead in ("backend.tasks", "backend.utils",
                 "modules.dubbing_studio", "modules.workflow_engine"):
        assert dead not in found, f"{dead} 已删除却仍会被打进包里"

    missing = [pkg for pkg in found if not (ROOT.joinpath(*pkg.split("."))).is_dir()]
    assert not missing, f"发现出了盘上不存在的包: {missing}"

    for live in ("backend", "backend.api", "backend.models", "backend.services",
                 "modules", "modules.workflow", "modules.tts_engine",
                 "modules.cast_analyzer", "modules.mixer", "modules.memory",
                 "modules.text_processor", "modules.voice_manager", "modules.book_parser"):
        assert live in found, f"在用的 {live} 没被打进包（include 漂了，装上去就是哑巴包）"

    for junk in ("tests", "frontend", "qa", "tools", "docker", "data"):
        leaked = [p for p in found if p == junk or p.startswith(junk + ".")]
        assert not leaked, f"非源码树被打包: {leaked}"


def test_compose_has_no_worker_service_and_still_resolves():
    spec = yaml.safe_load((ROOT / "docker" / "docker-compose.yml").read_text(encoding="utf-8"))
    services = spec["services"]
    assert "celery-worker" not in services, sorted(services)
    for name, svc in services.items():
        deps = svc.get("depends_on") or []
        deps = list(deps) if not isinstance(deps, dict) else list(deps)
        undefined = [d for d in deps if d not in services]
        assert not undefined, f"{name} 依赖未定义服务 {undefined}（PY-301 同族回退）"


@pytest.fixture(scope="module")
def git_root() -> Path:
    # encoding 必须显式钉 utf-8：仓库路径含中文，git 按 utf-8 输出，而 Windows 上
    # text=True 走的是 GBK —— 解码线程一崩，stdout 就成了 None（本机实测 plain 跑
    # 4 条用例 ERROR）。原先那句 `if not out: skip` 会把这种环境错误伪装成
    # "离线复制的树"而静默跳过，守卫于是靠"没跑"变绿，所以这里改成先归因再判。
    res = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=ROOT,
                         capture_output=True, encoding="utf-8", errors="replace")
    if res.returncode != 0:  # 无 git 环境（如离线复制的树）时跳过引用面自检
        pytest.skip("not a git checkout")
    assert res.stdout and res.stdout.strip(), \
        f"git 返回 0 却没给出 root：stdout={res.stdout!r} stderr={res.stderr!r}"
    return Path(res.stdout.strip())


def test_scan_scope_is_wide_enough(git_root: Path):
    """方向核对：扫描面若被改窄，本守卫会静默恒真 —— 至少覆盖到已知的那几类文件。"""
    files = [p.relative_to(ROOT).as_posix() for p in _scanned_files()]
    for needle in ("backend/main.py", "start_all.py", "start-all.cmd", "一键启动.bat",
                   "docker/docker-compose.yml", "pyproject.toml",
                   "frontend/src/App.tsx", "modules/workflow/__init__.py"):
        assert needle in files, f"扫描面漏掉了 {needle}"
    assert len(files) > 200, f"扫描面只剩 {len(files)} 个文件，守卫失去意义"


# ==================================== ④ 入库策略
def _tracked_files(git_root: Path) -> list:
    res = subprocess.run(["git", "ls-files", "-z"], cwd=git_root, capture_output=True)
    return [p.decode() for p in res.stdout.split(b"\0") if p]


def test_regenerable_cache_is_not_tracked(git_root: Path):
    """抓取缓存只入判定摘要，不入 git：`_tmp` 那 220 枚下载件曾占全仓 67.59 MiB / 80.30 MiB。
    这一类的回退方式是"某次 `git add -A` 顺手把缓存带回来"，不会报错，只会让仓库越来越clone不动。"""
    files = _tracked_files(git_root)
    cached = [f for f in files if CACHE_DIR_MARKER in "/" + f]
    assert not cached, f"{len(cached)} 个可再生缓存文件又入库了，例如 {cached[:3]}"
    audio = [f for f in files
             if f.startswith("tools/") and f.lower().endswith(RAW_AUDIO_SUFFIXES)]
    assert not audio, f"tools/ 下出现入库音频原始件，例如 {audio[:3]}"


def test_judgment_records_survive_the_slim(git_root: Path):
    """反脆弱断言：上一枚守卫可以靠"把东西全删了"变绿。判定记录与真实格式夹具是回测
    本身的依据，必须在库里 —— 少了它们，体积门就成了"删得越狠越绿"的错误激励。"""
    files = set(_tracked_files(git_root))
    missing = [rel for rel in MUST_BE_TRACKED if rel not in files]
    assert not missing, f"判定记录/夹具被一起删掉了: {missing}"


def test_tracked_size_within_budget(git_root: Path):
    """体积上界 + 下界成对：只有上界会被"删空仓库"变绿，加下界后两头都堵。"""
    total = 0
    absent = []
    for rel in _tracked_files(git_root):
        try:
            total += (git_root / rel).stat().st_size
        except OSError:
            absent.append(rel)
    assert not absent, f"git 认为在册但盘上没有: {absent[:5]}"
    assert total <= TRACKED_BYTES_BUDGET, (
        f"入库体积 {total / 1048576:.2f} MiB 超过预算 "
        f"{TRACKED_BYTES_BUDGET // 1048576} MiB：逐轮原始产物应走 gitignore，只入判定摘要"
    )
    assert total >= TRACKED_BYTES_FLOOR, (
        f"入库体积只剩 {total / 1048576:.2f} MiB，低于下界 —— 体积守卫是被删没的，不是被守住的"
    )
