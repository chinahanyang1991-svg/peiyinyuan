# -*- coding: utf-8 -*-
"""依赖口径三方对齐守卫：pyproject extras ↔ 启动器 BACKEND_DEPS ↔ README 装机命令。

三方曾经互斥：README:34 写 `pip install -e ".[dev]"`，照它装的机器没有 torch/edge-tts，
而 `start_all.py` 的解释器预检把 torch 列为必需（引擎注册表在启动期就要 import torch，
见 PY-306 的无条件预热）—— 新用户照文档装完**必然**被启动器拒起。本条守卫钉住三方口径。
"""

import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import start_all  # noqa: E402

# README 那条装机命令声明的 extras（改 README 就要改这里，双向盯着）
README_INSTALL_EXTRAS = ("dev", "edgetts", "voxcpm", "codecs")


def _norm(name: str) -> str:
    """PEP 503 名称归一：edge-tts / edge_tts / Edge.TTS 视作同一个包。"""
    return re.sub(r"[-_.]+", "-", name).lower()


def _dist_of(spec: str) -> str:
    """从 "torch>=2.3.0" / "uvicorn[standard]>=0.29" 里取出裸分发名。"""
    m = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)", spec)
    assert m, f"依赖声明无法解析: {spec!r}"
    return _norm(m.group(1))


def _project():
    with open(ROOT / "pyproject.toml", "rb") as f:
        return tomllib.load(f)["project"]


def test_backend_deps_are_covered_by_readme_install_command():
    """启动器判定"缺则拒起后端"的每一枚，都必须被 README 那条命令装进来。"""
    proj = _project()
    available = {_dist_of(s) for s in proj["dependencies"]}
    extras = proj["optional-dependencies"]
    for name in README_INSTALL_EXTRAS:
        assert name in extras, f"README 装的 extras 在 pyproject 里不存在: {name}"
        available |= {_dist_of(s) for s in extras[name]}
    missing = [m for m in start_all.BACKEND_DEPS if _norm(m) not in available]
    assert not missing, (
        "BACKEND_DEPS 里的这些包 README 的装机命令装不到: "
        + ", ".join(missing)
        + f"（README 口径 extras={README_INSTALL_EXTRAS}）"
    )


def test_backend_install_names_are_real_distributions():
    """`edge_tts` 的 pip 名是 `edge-tts`：两列必须一一对应且都归一化同名。"""
    assert len(start_all.BACKEND_DEPS) == len(start_all.BACKEND_INSTALL_NAMES)
    for mod, dist in zip(start_all.BACKEND_DEPS, start_all.BACKEND_INSTALL_NAMES):
        assert _norm(mod) == _norm(dist), f"{mod} 的 pip 名不是 {dist}"


def test_readme_still_advertises_that_exact_command():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    quoted = ",".join(README_INSTALL_EXTRAS)
    assert f'pip install -e ".[{quoted}]"' in readme, \
        f"README 的装机命令不再是 extras=[{quoted}] 那一版，三方口径漂了"


def test_pyproject_has_no_broken_console_script():
    """`peiyinyuan = "backend.main:app"` 指向的是 ASGI 实例而非 callable，装出来必炸。"""
    assert "scripts" not in _project(), "[project.scripts] 已下线，不得回到盘上"


def test_package_list_is_autodiscovered():
    """手写 packages 清单会漂（曾缺 cast_analyzer/mixer/memory、又列着已删的空壳包）。"""
    with open(ROOT / "pyproject.toml", "rb") as f:
        setuptools_cfg = tomllib.load(f)["tool"]["setuptools"]
    assert "packages" not in setuptools_cfg or "find" in setuptools_cfg["packages"], \
        "packages 必须是 packages.find 自动发现，不允许回到手写清单"


def test_empty_shell_packages_stay_gone():
    for dead in ("modules/dubbing_studio", "modules/workflow_engine", "backend/utils"):
        assert not (ROOT / dead).exists(), f"{dead} 是只有 __init__.py 的空壳，已删不得复现"
