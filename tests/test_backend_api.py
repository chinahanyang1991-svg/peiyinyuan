"""
后端 API 单元测试
"""
import pytest
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class TestConfig:
    """配置管理测试"""

    def test_config_has_required_fields(self):
        from backend.config import Config
        assert hasattr(Config, "DATA_DIR")
        assert hasattr(Config, "DB_PATH")
        assert hasattr(Config, "TTS_ENGINE")
        assert hasattr(Config, "REDIS_URL")

    def test_config_default_values(self):
        """默认值口径（指令#1 改判）：**默认是在线档**，不再是 voxcpm。

        升级前这里是 `Config.TTS_ENGINE == "voxcpm"`。改的是产品默认，不是笔误：
        在线档实测单句 0.3s，本地档在 CPU 机上 1 秒音频要烧 51.4 秒机器时（qa 记账）。
        旧部署只设 `TTS_ENGINE=voxcpm` 时行为一字不变 —— 所以一并钉住那条兼容路。
        """
        from backend.config import Config, derive_tier_and_engine
        assert Config.DEFAULT_AUDIO_FORMAT == "m4b"
        assert (Config.TTS_TIER, Config.TTS_ENGINE) == derive_tier_and_engine()
        assert derive_tier_and_engine() == ("online", "edgetts")
        # 旧部署口径（只设引擎名）：档位由引擎在线性反推，仍是本地档 + 启动预热
        assert derive_tier_and_engine("voxcpm") == ("local", "voxcpm")
        assert derive_tier_and_engine("edgetts") == ("online", "edgetts")
        # 显式档位说了算，引擎取该档默认
        assert derive_tier_and_engine(None, "local") == ("local", "voxcpm")
        assert derive_tier_and_engine(None, "online") == ("online", "edgetts")

    def test_supported_formats(self):
        from backend.config import Config
        assert ".epub" in Config.SUPPORTED_EBOOK_FORMATS
        assert ".m4b" in Config.SUPPORTED_AUDIO_FORMATS


class TestModels:
    """数据模型测试"""

    def test_import_models(self):
        """所有模型应能正常导入"""
        from backend.models.base import Base, get_session
        assert Base is not None
        assert get_session is not None

    def test_book_model_import(self):
        from backend.models.book import Book, Chapter
        assert Book.__tablename__ == "books"
        assert Chapter.__tablename__ == "chapters"

    def test_voice_model_import(self):
        from backend.models.voice import VoiceProfile
        assert VoiceProfile.__tablename__ == "voice_profiles"

    def test_project_model_import(self):
        from backend.models.project import DubbingProject, Role, VoiceAssignment
        assert DubbingProject.__tablename__ == "dubbing_projects"
        assert Role.__tablename__ == "roles"
        assert VoiceAssignment.__tablename__ == "voice_assignments"

    def test_task_model_import(self):
        from backend.models.task import ConversionTask
        assert ConversionTask.__tablename__ == "conversion_tasks"


class TestServices:
    """服务层测试"""

    def test_book_service_import(self):
        from backend.services.book_service import book_service
        assert book_service is not None

    def test_voice_service_import(self):
        from backend.services.voice_service import voice_service
        assert voice_service is not None

    def test_dubbing_service_import(self):
        from backend.services.dubbing_service import dubbing_service
        assert dubbing_service is not None

    def test_task_service_import(self):
        from backend.services.task_service import task_service
        assert task_service is not None


class TestAPI:
    """API路由测试"""

    def test_books_router_import(self):
        from backend.api.books import router
        assert router is not None

    def test_voices_router_import(self):
        from backend.api.voices import router
        assert router is not None

    def test_dubbing_router_import(self):
        from backend.api.dubbing import router
        assert router is not None

    def test_tasks_router_import(self):
        from backend.api.tasks import router
        assert router is not None

    def test_workflows_router_import(self):
        from backend.api.workflows import router
        assert router is not None

    def test_main_app_import(self):
        from backend.main import app
        assert app is not None
        assert app.title == "配音员 API"