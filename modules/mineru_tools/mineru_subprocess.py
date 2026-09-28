"""
MinerU 子进程管理器
封装 MinerU FastAPI 服务作为子进程启动/停止/健康检查
"""

import asyncio
import os
import platform
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

import httpx


class MinerUSubprocessManager:
    """
    MinerU 子进程生命周期管理器

    通过 subprocess.Popen 启动 MinerU fast_api.py 作为独立子进程，
    提供 HTTP API 通信。支持自动重试启动、健康检查和优雅关闭。
    """

    def __init__(
        self,
        mineru_root: str = None,
        host: str = "127.0.0.1",
        port: int = 8001,
        backend: str = "pipeline",
        models_dir: str = None,
        auto_start: bool = False,
    ):
        """
        Args:
            mineru_root: MinerU 项目根目录（包含 pyproject.toml 的目录）
            host: 监听地址
            port: 监听端口
            backend: 后端模式 (pipeline / vlm-auto-engine / hybrid-auto-engine)
            models_dir: 模型目录（可选，默认使用 MinerU 内置路径）
            auto_start: 初始化时自动启动
        """
        self.mineru_root = self._resolve_mineru_root(mineru_root)
        self.host = host
        self.port = port
        self.backend = backend
        self.models_dir = models_dir
        self._process: Optional[subprocess.Popen] = None
        self._base_url = f"http://{host}:{port}"
        self._health_ok = False

        if auto_start:
            self._start_process()

    # ──────────────────────────────────────────────
    # 属性
    # ──────────────────────────────────────────────

    @property
    def is_running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def is_healthy(self) -> bool:
        return self._health_ok

    # ──────────────────────────────────────────────
    # 生命周期
    # ──────────────────────────────────────────────

    def start(self, timeout: float = 30.0) -> bool:
        """启动 MinerU 子进程，等待健康检查通过"""
        if self.is_running:
            print(f"[MinerUProc] 子进程已在运行 (PID={self._process.pid})")
            return True

        self._start_process(timeout)
        return self.is_running

    async def start_async(self, timeout: float = 45.0) -> bool:
        """异步启动，等待健康检查通过"""
        if self.is_running:
            return True

        self._start_process()
        return await self._wait_healthy_async(timeout)

    def stop(self):
        """优雅停止 MinerU 子进程"""
        if self._process is None:
            return

        if self.is_running:
            pid = self._process.pid
            print(f"[MinerUProc] 正在停止子进程 PID={pid}...")

            if platform.system() == "Windows":
                self._process.terminate()
            else:
                os.killpg(os.getpgid(pid), signal.SIGTERM)

            try:
                self._process.wait(timeout=10)
                print(f"[MinerUProc] 子进程已停止")
            except subprocess.TimeoutExpired:
                print(f"[MinerUProc] 强制终止子进程...")
                self._process.kill()
                self._process.wait(timeout=5)

        self._process = None
        self._health_ok = False

    async def stop_async(self):
        """异步优雅停止"""
        self.stop()
        await asyncio.sleep(0)

    def restart(self):
        """重启子进程"""
        self.stop()
        return self.start()

    # ──────────────────────────────────────────────
    # 健康检查
    # ──────────────────────────────────────────────

    def check_health(self) -> bool:
        """同步健康检查"""
        try:
            resp = httpx.get(f"{self._base_url}/health", timeout=5)
            ok = resp.status_code == 200
            self._health_ok = ok
            return ok
        except Exception:
            self._health_ok = False
            return False

    async def check_health_async(self) -> bool:
        """异步健康检查"""
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(f"{self._base_url}/health")
                ok = resp.status_code == 200
                self._health_ok = ok
                return ok
        except Exception:
            self._health_ok = False
            return False

    # ──────────────────────────────────────────────
    # 内部方法
    # ──────────────────────────────────────────────

    def _resolve_mineru_root(self, mineru_root: str = None) -> str:
        """自动定位 MinerU 根目录

        搜索优先级: 环境变量 > 显式参数 > 项目相邻目录 > 当前工作目录
        """
        # 优先级 1: 环境变量
        if not mineru_root:
            env_root = os.environ.get("MINERU_ROOT")
            if env_root and os.path.isdir(env_root):
                return os.path.abspath(env_root)

        # 优先级 2: 显式参数
        if mineru_root:
            if os.path.isdir(mineru_root):
                return os.path.abspath(mineru_root)
            raise FileNotFoundError(
                f"指定的 MinerU 目录不存在: {mineru_root}。"
                f"请检查路径或设置 MINERU_ROOT 环境变量"
            )

        # 优先级 3: 从 peiyinyuan 项目目录向上搜索
        peiyinyuan_root = Path(__file__).parent.parent.parent  # → peiyinyuan/
        candidates = [
            peiyinyuan_root.parent / "MinerU-master",
            peiyinyuan_root.parent / "MinerU",
            peiyinyuan_root.parent / "MinerU-master" / "MinerU-master",
            Path.cwd() / "MinerU-master",
        ]

        for cand in candidates:
            if (cand / "mineru" / "cli" / "fast_api.py").exists():
                return str(cand.resolve())

        raise FileNotFoundError(
            "未找到 MinerU 项目目录。请执行以下任一操作:\n"
            "1. 设置环境变量: $env:MINERU_ROOT='D:\\path\\to\\MinerU-master'\n"
            "2. 在 MinerU 目录同级放置 peiyinyuan 项目\n"
            "3. 传递 mineru_root 参数指向包含 fast_api.py 的目录"
        )

    def _start_process(self, timeout: float = 30.0):
        """启动子进程"""
        fast_api_path = Path(self.mineru_root) / "mineru" / "cli" / "fast_api.py"
        if not fast_api_path.exists():
            raise FileNotFoundError(f"MinerU FastAPI 入口不存在: {fast_api_path}")

        python_exe = sys.executable
        cmd = [
            python_exe,
            "-m", "mineru.cli.fast_api",
            "--host", str(self.host),
            "--port", str(self.port),
        ]

        if self.backend:
            cmd.extend(["--backend", self.backend])

        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        if self.models_dir:
            env["MINERU_MODELS_DIR"] = self.models_dir

        startupinfo = None
        if platform.system() == "Windows":
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = subprocess.SW_HIDE

        print(f"[MinerUProc] 启动命令: {' '.join(cmd)}")
        print(f"[MinerUProc] 工作目录: {self.mineru_root}")

        self._process = subprocess.Popen(
            cmd,
            cwd=self.mineru_root,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            startupinfo=startupinfo,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP
            if platform.system() == "Windows"
            else 0,
        )

        # 等待健康检查通过
        self._health_ok = self._wait_healthy(timeout)

        if self._health_ok:
            print(f"[MinerUProc] MinerU 服务已就绪 (PID={self._process.pid})")
        else:
            print(f"[MinerUProc] MinerU 服务启动超时或失败")

    def _wait_healthy(self, timeout: float = 30.0) -> bool:
        """等待服务健康检查通过"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.is_running:
                return False
            if self.check_health():
                return True
            time.sleep(1.5)
        return False

    async def _wait_healthy_async(self, timeout: float = 45.0) -> bool:
        """异步等待健康检查"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.is_running:
                return False
            if await self.check_health_async():
                return True
            await asyncio.sleep(1.5)
        return False

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop()

    async def __aenter__(self):
        await self.start_async()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.stop_async()