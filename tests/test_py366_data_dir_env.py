"""
PY-366 回归测试②：`data` 根必须可被 `PEIYINYUAN_DATA_DIR` 整体重定向。

为什么要这个开关：`backend/config.py` 过去把 `DATA_DIR = BASE_DIR / "data"` 写死在代码
旁边（文件头注释却声称"支持环境变量覆盖"），于是**任何一次测试都在离代码最近的那个
data 目录里初始化**：在隔离工作树里跑全量回归门时，它会在检出树里建 `peiyinyuan.db*`，
并因占位素材缺失而改写受版本控制的 `data/bgm/meta.json`。
门禁/测试把整棵 data 根指到 scratch 后，检出树就保持干净。

红线：默认行为（不设变量）必须与改动前逐字节一致 —— 生产实例与启动器都不设该变量。

(c) 用子进程跑：`Config` 的类体在 import 时求值，同进程里改 `os.environ` 不会重算，
    所以必须起干净解释器；取文本显式 `encoding='utf-8'`（本项目硬规矩）。
(d) 一条 AST 守卫：`backend/config.py` 里除 `DATA_DIR` 那一行外不得再出现
    写死的 `BASE_DIR / "data"`，防止有人新增第二处硬编码绕过开关。
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PY = REPO_ROOT / "backend" / "config.py"

PYTHON = os.getenv("PY366_PYTHON") or sys.executable

_PRINT_PATHS = r"""
import json, sys
sys.path.insert(0, {repo!r})
from pathlib import Path
from backend.config import Config
from backend.models import base as db_base
import modules.mixer.bgm_store as bs
store = bs.BgmStore()
print(json.dumps({{
    "DATA_DIR": str(Config.DATA_DIR),
    "VOICE_DIR": str(Config.VOICE_DIR),
    "OUTPUT_DIR": str(Config.OUTPUT_DIR),
    "DB_PATH": str(Config.DB_PATH),
    "BASE_DIR": str(Config.BASE_DIR),
    "bgm_store_DEFAULT_ROOT": str(bs.DEFAULT_ROOT),
    "bgmstore_root": str(store.root),
    "meta_path": str(store.meta_path),
    "get_db_path": str(db_base.get_db_path()),
}}))
"""

# 只有确认默认根已经被指到 scratch 之后才允许写素材：否则本测试就会去污染仓库 data/，
# 在旧实现（无 env 开关）下它选择"拒绝并上报"，父进程据此判红。
_ENSURE_IN_SCRATCH = r"""
import json, sys
sys.path.insert(0, {repo!r})
from pathlib import Path
from backend.config import Config
from modules.mixer.bgm_store import BgmStore

scratch = Path({scratch!r}).resolve()
store = BgmStore()
report = {{"scratch": str(scratch), "bgmstore_root": str(store.root),
          "refused": None, "created": None, "repo_meta_md5_changed": None}}
if Path(store.root).resolve() != scratch:
    report["refused"] = ("默认根没有指向 scratch，拒绝在检出树里造素材："
                         + str(store.root))
else:
    before = (Path({repo!r}) / "data" / "bgm" / "meta.json")
    import hashlib
    md5_before = hashlib.md5(before.read_bytes()).hexdigest() if before.exists() else None
    result = store.ensure_builtin()
    report["created"] = result["created"]
    report["scratch_meta_exists"] = store.meta_path.exists()
    report["repo_meta_md5_changed"] = (
        md5_before != (hashlib.md5(before.read_bytes()).hexdigest()
                       if before.exists() else None))
    report["scratch_wavs"] = sorted(p.name for p in (store.bgm_dir).glob("*.wav"))
print(json.dumps(report))
"""


def _run(script: str, env_extra: dict, unset: tuple = ()) -> dict:
    env = dict(os.environ)
    for key in unset:
        env.pop(key, None)
    env.update(env_extra)
    proc = subprocess.run([PYTHON, "-X", "utf8", "-c", script],
                          capture_output=True, text=True, encoding="utf-8",
                          env=env, cwd=str(REPO_ROOT), timeout=300)
    assert proc.returncode == 0, (
        "子进程退出码 %s\nSTDOUT:\n%s\nSTDERR:\n%s"
        % (proc.returncode, proc.stdout, proc.stderr))
    return json.loads(proc.stdout.strip().splitlines()[-1])


# ---------------------------------------------------------------------------
# c. PEIYINYUAN_DATA_DIR 生效
# ---------------------------------------------------------------------------
class TestDataDirOverride:

    def test_env_var_redirects_every_data_path(self, tmp_path):
        scratch = tmp_path / "data_scratch"
        out = _run(_PRINT_PATHS.format(repo=str(REPO_ROOT)),
                   {"PEIYINYUAN_DATA_DIR": str(scratch)})

        assert Path(out["DATA_DIR"]) == scratch, out
        assert Path(out["VOICE_DIR"]) == scratch / "voices", out
        assert Path(out["OUTPUT_DIR"]) == scratch / "output", out
        assert Path(out["DB_PATH"]) == scratch / "peiyinyuan.db", out
        # get_db_path() 是 SQLAlchemy 引擎真正取路径的地方：过去它硬写 <repo>/data，
        # 跑一次门就在检出树里 mkdir + 建出 peiyinyuan.db(-wal/-shm)（PY-366 写痕之一）。
        assert Path(out["get_db_path"]) == scratch / "peiyinyuan.db", out
        # 派生路径全在 scratch 之下 —— 一个都不许回落到检出树
        for key in ("VOICE_DIR", "OUTPUT_DIR", "DB_PATH", "get_db_path",
                    "bgm_store_DEFAULT_ROOT", "bgmstore_root", "meta_path"):
            path = Path(out[key])
            assert path.is_absolute()
            assert path == scratch or scratch in path.parents, \
                "%s 没落在 scratch 下: %s" % (key, path)

    def test_env_var_makes_ensure_builtin_stay_out_of_checkout_tree(self, tmp_path):
        scratch = tmp_path / "data_scratch"
        report = _run(_ENSURE_IN_SCRATCH.format(repo=str(REPO_ROOT),
                                                scratch=str(scratch)),
                      {"PEIYINYUAN_DATA_DIR": str(scratch)})
        assert report["refused"] is None, (
            "PEIYINYUAN_DATA_DIR 未生效，BgmStore 默认根仍在检出树里: %s"
            % report["bgmstore_root"])
        assert len(report["created"]) == 5, report
        assert report["scratch_meta_exists"] is True
        assert report["repo_meta_md5_changed"] is False, \
            "跑到 scratch 时，仓库 data/bgm/meta.json 必须一字节不动"
        assert len(report["scratch_wavs"]) == 3

    def test_without_env_var_default_is_unchanged(self):
        """默认行为逐字节一致：不设变量时路径仍是 <repo>/data 及其派生。"""
        out = _run(_PRINT_PATHS.format(repo=str(REPO_ROOT)), {},
                   unset=("PEIYINYUAN_DATA_DIR",))
        expected_data = REPO_ROOT / "data"

        assert Path(out["DATA_DIR"]) == expected_data, out
        assert Path(out["VOICE_DIR"]) == expected_data / "voices", out
        assert Path(out["OUTPUT_DIR"]) == expected_data / "output", out
        assert Path(out["DB_PATH"]) == expected_data / "peiyinyuan.db", out
        assert Path(out["get_db_path"]) == expected_data / "peiyinyuan.db", out
        assert Path(out["BASE_DIR"]) == REPO_ROOT, out
        assert Path(out["bgm_store_DEFAULT_ROOT"]) == expected_data, out
        assert Path(out["bgmstore_root"]) == expected_data, out


# ---------------------------------------------------------------------------
# d. 守卫：backend/config.py 只允许 DATA_DIR 那一处 `BASE_DIR / "data"`
# ---------------------------------------------------------------------------
class TestNoSecondHardcodedDataRoot:

    @staticmethod
    def _is_data_bake(node) -> bool:
        """形状：`BASE_DIR / "data"`（可能是更大表达式的一部分）。"""
        return (isinstance(node, ast.BinOp)
                and isinstance(node.op, ast.Div)
                and isinstance(node.left, ast.Name) and node.left.id == "BASE_DIR"
                and isinstance(node.right, ast.Constant) and node.right.value == "data")

    @staticmethod
    def _assign_targets(node) -> list:
        if isinstance(node, ast.Assign):
            return [t.id for t in node.targets if isinstance(t, ast.Name)]
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            return [node.target.id]
        return []

    def test_only_data_dir_line_bakes_base_dir_data(self):
        """DATA_DIR 那一行是唯一允许把 data 根写死的入口，且它必须走 env 开关。"""
        source = CONFIG_PY.read_text(encoding="utf-8")
        tree = ast.parse(source)

        allowed = set()          # id(node) -> 属于 `DATA_DIR = <含 getenv>` 的形状节点
        data_dir_reads_env = False
        for holder in ast.walk(tree):
            if "DATA_DIR" not in self._assign_targets(holder):
                continue
            value_nodes = list(ast.walk(holder.value))
            if any(isinstance(n, ast.Call)
                   and isinstance(getattr(n, "func", None), ast.Attribute)
                   and n.func.attr == "getenv"
                   and any(isinstance(a, ast.Constant)
                           and a.value == "PEIYINYUAN_DATA_DIR" for a in n.args)
                   for n in value_nodes):
                data_dir_reads_env = True
            for sub in value_nodes:
                if self._is_data_bake(sub):
                    allowed.add(id(sub))

        offenders = [(n.lineno, source.splitlines()[n.lineno - 1].strip())
                     for n in ast.walk(tree)
                     if self._is_data_bake(n) and id(n) not in allowed]
        assert not offenders, (
            "backend/config.py 出现了 DATA_DIR 之外的第二处硬编码 data 根"
            "（绕过 PEIYINYUAN_DATA_DIR 开关），应改为从 Config.DATA_DIR 派生: %s"
            % offenders)
        assert data_dir_reads_env, (
            "DATA_DIR 那一行自己也没有读取 PEIYINYUAN_DATA_DIR —— "
            "唯一入口被写死，整个开关失效")

    def test_data_dir_reads_the_env_switch(self):
        source = CONFIG_PY.read_text(encoding="utf-8")
        assert "PEIYINYUAN_DATA_DIR" in source, (
            "backend/config.py 不再读取 PEIYINYUAN_DATA_DIR，"
            "门禁/测试无法把 data 根指到 scratch")

