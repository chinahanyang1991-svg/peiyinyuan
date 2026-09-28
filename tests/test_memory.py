"""记忆模块测试"""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class TestMemoryModel:
    def test_model_import(self):
        from backend.models.memory import Memory
        assert Memory.__tablename__ == "memories"

    def test_to_dict(self):
        from backend.models.memory import Memory
        m = Memory(id="test-1", title="测试记忆", content="测试内容", memory_type="general")
        d = m.to_dict()
        assert d["id"] == "test-1"
        assert d["title"] == "测试记忆"
        assert d["memory_type"] == "general"


class TestMemoryService:
    def test_service_import(self):
        from backend.services.memory_service import memory_service
        assert memory_service is not None


class TestTranscriptFormatter:
    def test_format_transcript(self):
        from modules.memory.transcript_formatter import format_transcript
        raw = "你好。你好啊！"
        formatted = format_transcript(raw, ["张三", "李四"])
        assert "张三" in formatted
        assert "李四" in formatted

    def test_clean_transcript(self):
        from modules.memory.transcript_formatter import clean_transcript
        noisy = "[00:01:23] 测试  文本  "
        cleaned = clean_transcript(noisy)
        assert "[00:01:23]" not in cleaned
        assert "  " not in cleaned

    def test_extract_timestamps(self):
        from modules.memory.transcript_formatter import extract_timestamps
        text = "[00:01:23] Hello [00:02:45] World"
        stamps = extract_timestamps(text)
        assert len(stamps) == 2
        assert "00:01:23" in stamps
        assert "00:02:45" in stamps