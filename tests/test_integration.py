"""
端到端集成测试
测试完整的电子书导入→解析→配音→转换流程
"""
import pytest
import os
import sys
import json
import tempfile
import shutil

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


@pytest.fixture(scope="module")
def test_data_dir():
    """创建临时测试数据目录"""
    tmpdir = tempfile.mkdtemp(prefix="peiyinyuan_test_")
    yield tmpdir
    shutil.rmtree(tmpdir, ignore_errors=True)


class TestBookImportIntegration:
    """电子书导入集成测试"""

    def test_txt_import_end_to_end(self, test_data_dir, sample_book_text):
        """测试 TXT 导入完整流程"""
        from modules.book_parser.parser_factory import ParserFactory
        from modules.book_parser.chapter_detector import ChapterDetector
        from modules.book_parser.text_cleaner import TextCleaner
        from modules.book_parser.metadata_extractor import MetadataExtractor

        # Create test TXT file
        txt_path = os.path.join(test_data_dir, "test_book.txt")
        with open(txt_path, "w", encoding="utf-8") as f:
            f.write(sample_book_text)

        # Step 1: Parse
        parser = ParserFactory.get_parser(txt_path)
        assert parser is not None, f"No parser for {txt_path}"

        # Step 2: Detect chapters
        detector = ChapterDetector(sample_book_text)
        chapters = detector.detect()
        assert len(chapters) > 0, "Should detect at least one chapter"

        # Step 3: Clean text
        cleaner = TextCleaner()
        for ch in chapters:
            cleaned = cleaner.clean(ch["content"])
            ch["content"] = cleaned

        # Step 4: Extract metadata
        extractor = MetadataExtractor()
        metadata = extractor.extract(sample_book_text)
        assert metadata is not None

        # Verify sample_book_text fixture is used from conftest
        assert "第一章" in sample_book_text
        assert "第三章" in sample_book_text

    def test_parse_multiple_formats(self):
        """测试解析器工厂支持多格式"""
        from modules.book_parser.parser_factory import ParserFactory

        formats = {
            ".epub": True,
            ".txt": True,
            ".pdf": True,
            ".doc": False,
            ".html": True,
            ".htm": True,
        }
        for ext, expected in formats.items():
            parser = ParserFactory.get_parser(f"test{ext}")
            if expected:
                assert parser is not None, f"Should support {ext}"
            else:
                assert parser is None, f"Should NOT support {ext}"


class TestTextProcessingIntegration:
    """文本处理集成测试"""

    def test_full_text_pipeline(self, sample_chinese_text):
        """测试完整文本处理管线"""
        from modules.book_parser.text_cleaner import TextCleaner
        from modules.text_processor.sentence_splitter import SentenceSplitter
        from modules.text_processor.tone_detector import ToneDetector
        from modules.text_processor.dialogue_detector import DialogueDetector

        # Step 1: Clean
        cleaner = TextCleaner()
        cleaned = cleaner.clean(sample_chinese_text)
        assert len(cleaned) > 0
        assert "www.example.com" not in cleaned  # No ads in sample

        # Step 2: Split into sentences
        splitter = SentenceSplitter()
        sentences = splitter.split(cleaned, "zh")
        assert len(sentences) > 0

        # Step 3: Detect tone for each sentence
        detector = ToneDetector()
        for sent in sentences:
            tone = detector.detect(sent)
            assert "primary" in tone
            assert tone["primary"] in ("neutral", "question", "excited", "sad", "angry", "gentle", "surprised")

        # Step 4: Detect dialogue
        dialogue_detector = DialogueDetector()
        dialogues = dialogue_detector.detect_dialogues(cleaned)
        dialogue_count = sum(1 for d in dialogues if d["type"] == "dialogue")
        narration_count = sum(1 for d in dialogues if d["type"] == "narration")

        # Total should have both dialogues and narrations
        assert dialogue_count > 0 or narration_count > 0

    def test_english_text_pipeline(self, sample_english_text):
        """测试英文文本处理管线"""
        from modules.text_processor.sentence_splitter import SentenceSplitter

        splitter = SentenceSplitter()
        sentences = splitter.split(sample_english_text, "en")
        assert len(sentences) > 0

        # Should detect Chapter markers
        has_chapter = any("Chapter" in s for s in sentences if len(s) < 30)
        assert has_chapter, "Should detect chapter headings"


class TestWorkflowIntegration:
    """工作流集成测试"""

    def test_progress_tracker_broadcast(self):
        """测试进度追踪器广播"""
        from modules.workflow.progress_tracker import ProgressTracker
        import asyncio

        tracker = ProgressTracker("broadcast-test")
        received = []

        def subscriber(data):
            received.append(data)

        tracker.subscribe(subscriber)

        async def test():
            await tracker.update(0.5, "synthesize", 500, 1000, status="running")

        asyncio.run(test())
        assert len(received) == 1
        assert received[0]["progress"] == 0.5
        assert received[0]["current_stage"] == "synthesize"

    def test_pause_resume_cancel_lifecycle(self):
        """测试暂停/恢复/取消生命周期"""
        from modules.workflow.pipeline import Pipeline, PipelineStage

        pipeline = Pipeline()
        for stage in PipelineStage:
            pipeline.register_handler(stage, lambda ctx: ctx)

        # Lifecycle: pause -> resume -> cancel
        pipeline.pause()
        assert pipeline.is_paused()

        pipeline.resume()
        assert not pipeline.is_paused()

        pipeline.cancel()
        assert pipeline.is_cancelled()

        import asyncio
        result = asyncio.run(pipeline.run({"data": "test"}))
        assert result["status"] == "cancelled"


class TestAPIIntegration:
    """API 集成测试"""

    def test_main_app_health(self):
        """测试应用健康检查"""
        from backend.main import app
        from fastapi.testclient import TestClient

        client = TestClient(app)
        response = client.get("/api/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert "version" in data

    def test_system_info(self):
        """测试系统信息端点"""
        from backend.main import app
        from fastapi.testclient import TestClient

        client = TestClient(app)
        response = client.get("/api/system/info")
        assert response.status_code == 200
        data = response.json()
        assert "platform" in data
        assert "python_version" in data

    def test_cors_headers(self):
        """测试CORS头"""
        from backend.main import app
        from fastapi.testclient import TestClient

        client = TestClient(app)
        response = client.options(
            "/api/health",
            headers={
                "Origin": "http://localhost:5173",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert response.status_code == 200