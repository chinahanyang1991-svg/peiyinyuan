"""
音色管理模块单元测试
"""
import pytest
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from modules.voice_manager.builtin_voices import BUILTIN_VOICES
from modules.voice_manager.voice_designer import VoiceDesigner


class TestBuiltinVoices:
    """内置音色测试"""

    def test_builtin_voices_exist(self):
        """应存在内置音色"""
        assert len(BUILTIN_VOICES) > 0

    def test_voice_structure(self):
        """每个音色应包含必要字段"""
        for voice in BUILTIN_VOICES:
            assert "name" in voice
            assert "source" in voice
            assert "engine" in voice
            assert "language" in voice

    def test_voice_sources(self):
        """音色来源应为builtin"""
        for voice in BUILTIN_VOICES:
            assert voice["source"] == "builtin"

    def test_genders_covered(self):
        """应包含男女两种性别"""
        genders = set(v["gender"] for v in BUILTIN_VOICES if v.get("gender"))
        assert "male" in genders or "female" in genders

    def test_languages_covered(self):
        """应覆盖中英文"""
        langs = set(v["language"] for v in BUILTIN_VOICES)
        assert "zh" in langs

    def test_voice_uniqueness(self):
        """音色名称应唯一"""
        names = [v["name"] for v in BUILTIN_VOICES]
        assert len(names) == len(set(names))


class TestVoiceDesigner:
    """语音设计师测试"""

    def setup_method(self):
        self.designer = VoiceDesigner()

    def test_parse_description_basic(self):
        """应解析基本描述"""
        result = self.designer.parse_description("温柔女声")
        assert result is not None

    def test_parse_gender_and_style(self):
        """应解析性别和风格"""
        result = self.designer.parse_description("低沉磁性的中年男声")
        assert result.get("gender") == "male"
        assert "低沉" in result.get("style_tags", [])
        assert "磁性" in result.get("style_tags", [])

    def test_parse_language(self):
        """应解析语言信息"""
        result = self.designer.parse_description("说四川话的温柔女声")
        assert "四川" in str(result.get("style_tags", []))

    def test_parse_age_group(self):
        """应解析年龄段"""
        result = self.designer.parse_description("30岁温柔女声")
        assert result.get("age_group") is not None

    def test_empty_description(self):
        """空描述应返回默认值"""
        result = self.designer.parse_description("")
        assert result is not None


class TestVoiceCrawler:
    """音色爬虫测试"""

    def test_hf_crawler_initialization(self):
        """HuggingFace爬虫应能初始化"""
        from modules.voice_manager.voice_crawler.hf_crawler import HuggingFaceCrawler
        crawler = HuggingFaceCrawler()
        assert crawler is not None
        assert hasattr(crawler, "search_tts_models")

    def test_github_crawler_initialization(self):
        """GitHub爬虫应能初始化"""
        from modules.voice_manager.voice_crawler.github_crawler import GitHubCrawler
        crawler = GitHubCrawler()
        assert crawler is not None
        assert hasattr(crawler, "search_tts_projects")