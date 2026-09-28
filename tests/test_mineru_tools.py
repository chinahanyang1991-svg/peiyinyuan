"""
MinerU 工具模块单元测试
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# 查找实际 MinerU 路径用于测试
_TEST_MINERU_ROOT = None
_candidates = [
    os.path.join(os.path.dirname(__file__), "..", "..", "MinerU-master", "MinerU-master"),
    os.path.join(os.path.dirname(__file__), "..", "..", "MinerU-master"),
]
for _c in _candidates:
    if os.path.isfile(os.path.join(_c, "mineru", "cli", "fast_api.py")):
        _TEST_MINERU_ROOT = os.path.abspath(_c)
        break


class TestMinerUSubprocessManager:
    """MinerU 子进程管理器测试"""

    def test_initial_state(self):
        """初始状态应为未运行"""
        if not _TEST_MINERU_ROOT:
            import pytest
            pytest.skip("MinerU 项目目录未找到，跳过")
        from modules.mineru_tools.mineru_subprocess import MinerUSubprocessManager
        manager = MinerUSubprocessManager(
            mineru_root=_TEST_MINERU_ROOT,
            host="127.0.0.1",
            port=18001,
        )
        assert manager.is_running is False
        assert manager.is_healthy is False
        # 清理
        manager.stop()

    def test_base_url(self):
        """base_url 应正确构建"""
        if not _TEST_MINERU_ROOT:
            import pytest
            pytest.skip("MinerU 项目目录未找到，跳过")
        from modules.mineru_tools.mineru_subprocess import MinerUSubprocessManager
        manager = MinerUSubprocessManager(
            mineru_root=_TEST_MINERU_ROOT,
            host="127.0.0.1",
            port=18001,
        )
        assert manager.base_url == "http://127.0.0.1:18001"
        manager.stop()

    def test_resolve_mineru_root_not_found(self):
        """路径不存在时应抛异常"""
        import pytest
        with pytest.raises(FileNotFoundError):
            from modules.mineru_tools.mineru_subprocess import MinerUSubprocessManager
            MinerUSubprocessManager(mineru_root="/definitely/not/exists")

    def test_check_health_when_not_started(self):
        """未启动时健康检查应返回 False"""
        if not _TEST_MINERU_ROOT:
            import pytest
            pytest.skip("MinerU 项目目录未找到，跳过")
        from modules.mineru_tools.mineru_subprocess import MinerUSubprocessManager
        manager = MinerUSubprocessManager(
            mineru_root=_TEST_MINERU_ROOT,
            host="127.0.0.1",
            port=18001,
        )
        assert manager.check_health() is False
        manager.stop()


class TestMinerUApiClient:
    """MinerU API 客户端测试"""

    def setup_method(self):
        from modules.mineru_tools.mineru_api_client import MinerUApiClient
        self.client = MinerUApiClient(
            base_url="http://127.0.0.1:18002",
            timeout=5,
        )

    def test_health_check_fails_gracefully(self):
        """服务不可用时应优雅失败"""
        try:
            self.client.health_check()
            assert False, "应抛出异常"
        except Exception:
            pass

    def test_parse_file_not_found(self):
        """文件不存在时应抛异常"""
        import pytest
        with pytest.raises(FileNotFoundError):
            self.client.parse_file("/nonexistent/file.pdf")

    def test_submit_task_file_not_found(self):
        """提交不存在的文件任务应抛异常"""
        import pytest
        with pytest.raises(FileNotFoundError):
            self.client.submit_task("/nonexistent/file.pdf")