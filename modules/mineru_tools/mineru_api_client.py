"""
MinerU API HTTP 客户端
封装 MinerU fast_api.py 的 REST API 调用（文件解析/任务管理/结果获取）
"""

import json
import os
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import httpx


class MinerUApiClient:
    """
    MinerU 远程 API 客户端

    通过 HTTP 协议与 MinerU FastAPI 服务通信，支持：
    - 同步文件解析 (POST /file_parse)
    - 异步任务提交 (POST /tasks)
    - 任务状态查询 (GET /tasks/{task_id})
    - 结果获取 (GET /tasks/{task_id}/result)
    - 健康检查 (GET /health)
    """

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8001",
        timeout: float = 120.0,
        verify_ssl: bool = False,
    ):
        """
        Args:
            base_url: MinerU 服务地址
            timeout: 请求超时（秒）
            verify_ssl: 是否验证 SSL
        """
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._verify_ssl = verify_ssl

    # ──────────────────────────────────────────────
    # 健康检查
    # ──────────────────────────────────────────────

    def health_check(self) -> Dict[str, Any]:
        """检查 MinerU 服务状态"""
        with httpx.Client(verify=self._verify_ssl, timeout=10) as client:
            resp = client.get(f"{self._base_url}/health")
            resp.raise_for_status()
            return resp.json()

    # ──────────────────────────────────────────────
    # 同步文件解析
    # ──────────────────────────────────────────────

    def parse_file(
        self,
        file_path: str,
        output_dir: str = None,
        options: Dict[str, Any] = None,
    ) -> Dict[str, Any]:
        """
        同步解析文件

        POST /file_parse

        Args:
            file_path: 要解析的文件路径
            output_dir: 输出目录（可选，默认临时目录）
            options: 额外选项（language, page_range, ocr等）

        Returns:
            API 返回的解析结果
        """
        if not os.path.isfile(file_path):
            raise FileNotFoundError(f"文件不存在: {file_path}")

        with httpx.Client(verify=self._verify_ssl, timeout=self._timeout) as client:
            # 准备表单数据
            files = {"file": (os.path.basename(file_path), open(file_path, "rb"))}
            data = {}
            if output_dir:
                data["output_dir"] = output_dir
            if options:
                data["options"] = json.dumps(options)

            try:
                resp = client.post(
                    f"{self._base_url}/file_parse",
                    files=files,
                    data=data,
                )
                resp.raise_for_status()
                return resp.json()
            finally:
                files["file"][1].close()

    # ──────────────────────────────────────────────
    # 异步任务管理
    # ──────────────────────────────────────────────

    def submit_task(
        self,
        file_path: str,
        output_dir: str = None,
        options: Dict[str, Any] = None,
    ) -> Dict[str, Any]:
        """
        提交异步解析任务

        POST /tasks

        Returns:
            {"task_id": "...", "status": "pending", ...}
        """
        if not os.path.isfile(file_path):
            raise FileNotFoundError(f"文件不存在: {file_path}")

        with httpx.Client(verify=self._verify_ssl, timeout=30) as client:
            data = {}
            if output_dir:
                data["output_dir"] = output_dir
            if options:
                data["options"] = json.dumps(options)

            with open(file_path, "rb") as f:
                files = {"file": (os.path.basename(file_path), f, "application/octet-stream")}
                resp = client.post(
                    f"{self._base_url}/tasks",
                    files=files,
                    data=data,
                )
            resp.raise_for_status()
            return resp.json()

    async def submit_task_async(
        self,
        file_path: str,
        output_dir: str = None,
        options: Dict[str, Any] = None,
    ) -> Dict[str, Any]:
        """异步提交任务"""
        if not os.path.isfile(file_path):
            raise FileNotFoundError(f"文件不存在: {file_path}")

        async with httpx.AsyncClient(verify=self._verify_ssl, timeout=30) as client:
            data = {}
            if output_dir:
                data["output_dir"] = output_dir
            if options:
                data["options"] = json.dumps(options)

            with open(file_path, "rb") as f:
                files = {"file": (os.path.basename(file_path), f, "application/octet-stream")}
                resp = await client.post(
                    f"{self._base_url}/tasks",
                    files=files,
                    data=data,
                )
            resp.raise_for_status()
            return resp.json()

    def get_task_status(self, task_id: str) -> Dict[str, Any]:
        """查询任务状态"""
        with httpx.Client(verify=self._verify_ssl, timeout=10) as client:
            resp = client.get(f"{self._base_url}/tasks/{task_id}")
            resp.raise_for_status()
            return resp.json()

    async def get_task_status_async(self, task_id: str) -> Dict[str, Any]:
        """异步查询任务状态"""
        async with httpx.AsyncClient(verify=self._verify_ssl, timeout=10) as client:
            resp = await client.get(f"{self._base_url}/tasks/{task_id}")
            resp.raise_for_status()
            return resp.json()

    def get_task_result(self, task_id: str) -> Dict[str, Any]:
        """获取任务解析结果"""
        with httpx.Client(verify=self._verify_ssl, timeout=30) as client:
            resp = client.get(f"{self._base_url}/tasks/{task_id}/result")
            resp.raise_for_status()
            return resp.json()

    async def get_task_result_async(self, task_id: str) -> Dict[str, Any]:
        """异步获取任务结果"""
        async with httpx.AsyncClient(verify=self._verify_ssl, timeout=30) as client:
            resp = await client.get(f"{self._base_url}/tasks/{task_id}/result")
            resp.raise_for_status()
            return resp.json()

    # ──────────────────────────────────────────────
    # 便捷方法：提交并等待完成
    # ──────────────────────────────────────────────

    def submit_and_wait(
        self,
        file_path: str,
        output_dir: str = None,
        poll_interval: float = 2.0,
        max_wait: float = 300.0,
        options: Dict[str, Any] = None,
    ) -> Dict[str, Any]:
        """
        提交任务并轮询等待完成

        Args:
            file_path: 文件路径
            output_dir: 输出目录
            poll_interval: 轮询间隔（秒）
            max_wait: 最大等待时间（秒）
            options: 额外选项

        Returns:
            任务结果
        """
        import time

        task_resp = self.submit_task(file_path, output_dir, options)
        task_id = task_resp.get("task_id") or task_resp.get("id")

        if not task_id:
            raise RuntimeError(f"提交任务失败: {task_resp}")

        deadline = time.monotonic() + max_wait
        while time.monotonic() < deadline:
            status_resp = self.get_task_status(task_id)
            status = status_resp.get("status", "unknown")

            if status in ("done", "completed", "success"):
                return self.get_task_result(task_id)
            elif status in ("failed", "error"):
                raise RuntimeError(
                    f"任务失败: {status_resp.get('error', '未知错误')}"
                )

            time.sleep(poll_interval)

        raise TimeoutError(f"任务 {task_id} 在 {max_wait}s 内未完成")
