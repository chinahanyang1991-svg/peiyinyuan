"""
配音员 - 环境依赖检查与一键安装工具
运行: python tools/setup_deps.py
"""

import os
import sys
import subprocess
import platform
from pathlib import Path

# 本文件在 tools/ 下，仓库根是它的上一级
ROOT_DIR = Path(__file__).resolve().parents[1]
FRONTEND_DIR = ROOT_DIR / "frontend"

# ============================================================
# 颜色工具
# ============================================================
if platform.system() == "Windows":
    os.system("color")
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
    except:
        pass


def c(text, color):
    colors = {
        "green": "\033[92m", "yellow": "\033[93m", "red": "\033[91m",
        "cyan": "\033[96m", "white": "\033[97m", "bold": "\033[1m",
        "reset": "\033[0m",
    }
    return f"{colors.get(color, '')}{text}{colors['reset']}"


def print_header(text):
    print(f"\n{c('▸', 'cyan')} {c(text, 'bold')}")


def print_ok(text):
    print(f"  {c('✓', 'green')} {text}")


def print_fail(text):
    print(f"  {c('✗', 'red')} {text}")


def print_warn(text):
    print(f"  {c('⚠', 'yellow')} {text}")


def print_info(text):
    print(f"    {text}")


# ============================================================
# 检测
# ============================================================
def check_cmd(name, args=None):
    """检查命令是否可用"""
    try:
        cmd = [name] + (args or ["--version"])
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if r.returncode == 0:
            ver = r.stdout.strip().split("\n")[0][:60] or r.stderr.strip().split("\n")[0][:60]
            return True, ver
        return False, ""
    except:
        return False, ""


def run_cmd(cmd, cwd=None, desc=""):
    """运行命令并输出"""
    if desc:
        print(f"  {desc}...")
    try:
        r = subprocess.run(cmd, cwd=cwd or str(ROOT_DIR),
                          capture_output=True, text=True, timeout=300)
        if r.returncode == 0:
            print_ok("完成")
            if r.stdout.strip():
                for line in r.stdout.strip().split("\n")[-3:]:
                    print_info(line.strip())
            return True
        else:
            print_fail(f"失败 (code={r.returncode})")
            if r.stderr.strip():
                for line in r.stderr.strip().split("\n")[-3:]:
                    print_info(c(line.strip(), "red"))
            return False
    except subprocess.TimeoutExpired:
        print_fail("超时")
        return False
    except FileNotFoundError:
        print_fail("命令未找到")
        return False


# ============================================================
# 主程序
# ============================================================
def main():
    print()
    print(c("╔══════════════════════════════════════════════════╗", "cyan"))
    print(c("║     配音员 - 环境检测与依赖安装工具            ║", "cyan"))
    print(c("╚══════════════════════════════════════════════════╝", "cyan"))
    print()

    all_ok = True
    has_errors = []

    # ---- 系统信息 ----
    print_header("系统信息")
    print_info(f"操作系统: {platform.system()} {platform.release()}")
    print_info(f"架构: {platform.machine()}")
    print_info(f"Python: {sys.version.split()[0]}")
    print_info(f"项目路径: {ROOT_DIR}")

    # ---- Python 依赖 ----
    print_header("Python 环境")
    py_ok, py_ver = check_cmd("python")
    if py_ok:
        print_ok(f"Python {py_ver}")
    else:
        print_fail("Python 未找到 (需要 Python 3.10+)")
        has_errors.append("python")

    pip_ok, pip_ver = check_cmd("pip")
    if pip_ok:
        print_ok(f"pip {pip_ver}")
    else:
        print_fail("pip 未找到")
        has_errors.append("pip")

    # 检查关键 Python 包
    packages = [
        ("fastapi", "FastAPI"),
        ("uvicorn", "uvicorn"),
        ("sqlalchemy", "SQLAlchemy"),
        ("celery", "Celery"),
        ("ebooklib", "ebooklib"),
        ("numpy", "numpy"),
        ("aiofiles", "aiofiles"),
    ]
    for pkg_name, display in packages:
        try:
            mod = __import__(pkg_name.split(".")[0])
            ver = getattr(mod, "__version__", "?")
            print_ok(f"{display} v{ver}")
        except ImportError:
            print_fail(f"{display} 未安装")
            has_errors.append(pkg_name)

    # ---- Node.js 环境 ----
    print_header("Node.js 环境")
    node_ok, node_ver = check_cmd("node")
    if node_ok:
        print_ok(f"Node.js {node_ver}")
    else:
        print_warn("Node.js 未找到 (前端需要)")
        has_errors.append("node")

    npm_ok, npm_ver = check_cmd("npm")
    if npm_ok:
        print_ok(f"npm {npm_ver}")
    else:
        print_warn("npm 未找到")
        has_errors.append("npm")

    # ---- 可选工具 ----
    print_header("可选工具")
    for cmd_name in ["git", "docker", "ffmpeg", "redis-cli"]:
        ok, ver = check_cmd(cmd_name)
        if ok:
            print_ok(f"{cmd_name}: {ver[:40]}")
        else:
            print_warn(f"{cmd_name} 未找到")

    # ---- 数据目录 ----
    print_header("数据目录")
    dirs = [
        "data", "data/voices", "data/output", "data/db",
        "logs", "data/voices/builtin",
    ]
    for d in dirs:
        p = ROOT_DIR / d
        p.mkdir(parents=True, exist_ok=True)
        print_ok(f"{d}/")

    # ---- 汇总报告 ----
    print()
    print(c("═" * 50, "cyan"))
    print()

    if not has_errors:
        print(c("✓ 所有依赖检查通过！可以启动应用。", "green"))
        print()
        print_info("唯一入口: 双击 一键启动.bat（或 python start_all.py / 双击 start-all.cmd）—— 起之前会先检查 Python 环境")
        print_info("旧三枚 start.bat / start.ps1 / start_gui.py 已于 2026-09-27 下线，git 历史里还能找回")
        print()
    else:
        print(c(f"⚠ 发现 {len(has_errors)} 个问题，是否自动修复？", "yellow"))
        print()
        choice = input("  输入 Y 自动安装缺失依赖，N 退出 (Y/N): ").strip().upper()
        print()

        if choice == "Y":
            auto_fix(has_errors)
        else:
            print_info("请手动安装缺失的依赖后重试。")
            print_info("  pip install -e '.[dev]'")
            print_info("  cd frontend && npm install")


def auto_fix(errors):
    """自动修复依赖问题"""
    
    # Python 包安装
    print_header("安装 Python 依赖")
    run_cmd(
        [sys.executable, "-m", "pip", "install", "-e", ".[dev]"],
        desc="安装全部 Python 依赖",
    )

    # 前端依赖
    print_header("安装前端依赖")
    if FRONTEND_DIR.exists() and (FRONTEND_DIR / "package.json").exists():
        run_cmd(["npm", "install"], cwd=str(FRONTEND_DIR), desc="安装前端依赖")
    else:
        print_warn("前端目录未找到，跳过")

    print()
    print(c("✓ 修复完成！请重新运行本程序验证。", "green"))
    print()
    print_info("启动应用（唯一入口）:")
    print_info("  一键启动.bat               # Windows 双击即用")
    print_info("  python start_all.py        # 命令行，同一实现")
    print()


if __name__ == "__main__":
    main()