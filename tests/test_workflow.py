"""
工作流引擎单元测试
"""
import pytest
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from modules.workflow.pipeline import Pipeline, PipelineStage
from modules.workflow.progress_tracker import ProgressTracker


class TestPipeline:
    """Pipeline 编排器测试"""

    def test_pipeline_creation(self):
        """应能创建 Pipeline"""
        pipeline = Pipeline()
        assert pipeline is not None
        assert len(pipeline.stages) == 9

    def test_register_handler(self):
        """应能注册阶段处理器"""
        pipeline = Pipeline()

        def handler(ctx):
            ctx["processed"] = True
            return ctx

        pipeline.register_handler(PipelineStage.IMPORT, handler)
        assert PipelineStage.IMPORT in pipeline._handlers

    def test_run_complete_pipeline(self):
        """完整流水线应执行所有阶段"""
        pipeline = Pipeline()
        executed = []

        for stage in PipelineStage:
            def make_handler(s):
                def handler(ctx):
                    executed.append(s.value)
                    return ctx
                return handler
            pipeline.register_handler(stage, make_handler(stage))

        import asyncio
        result = asyncio.run(pipeline.run({"data": "test"}))
        assert result["status"] == "completed"
        assert len(executed) == 9

    def test_cancel(self):
        """应能取消流水线"""
        pipeline = Pipeline()

        def slow_handler(ctx):
            import time
            time.sleep(10)
            return ctx

        pipeline.register_handler(PipelineStage.SYNTHESIZE, slow_handler)

        # Cancel before run
        pipeline.cancel()
        import asyncio
        result = asyncio.run(pipeline.run({}))
        assert result["status"] == "cancelled"

    def test_pause_and_resume(self):
        """应能暂停和恢复"""
        pipeline = Pipeline()
        pipeline.pause()
        assert pipeline.is_paused()
        pipeline.resume()
        assert not pipeline.is_paused()

    def test_progress_callback(self):
        """进度回调应被调用"""
        pipeline = Pipeline()
        progress_updates = []

        def on_progress(stage, progress, msg, error=None):
            progress_updates.append((stage, progress))

        pipeline.on_progress(on_progress)
        for stage in PipelineStage:
            pipeline.register_handler(stage, lambda ctx: ctx)

        import asyncio
        asyncio.run(pipeline.run({}))
        assert len(progress_updates) > 0


class TestProgressTracker:
    """进度追踪器测试"""

    def setup_method(self):
        self.tracker = ProgressTracker("test-task-1")
        import asyncio
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)

    def test_initial_state(self):
        """初始状态应为queued"""
        assert self.tracker.status == "queued"
        assert self.tracker.progress == 0.0

    def test_update_progress(self):
        """应能更新进度"""
        self.loop.run_until_complete(
            self.tracker.update(0.5, "synthesize", 1000, 2000, status="running")
        )
        assert self.tracker.progress == 0.5
        assert self.tracker.current_stage == "synthesize"
        assert self.tracker.status == "running"

    def test_progress_bounds(self):
        """进度应在0-1范围"""
        self.loop.run_until_complete(
            self.tracker.update(1.5)  # Exceeds 1.0
        )
        assert self.tracker.progress == 1.0

    def test_to_dict(self):
        """应输出结构化字典"""
        self.loop.run_until_complete(
            self.tracker.update(0.75, "synthesize", 1500, 2000, status="running")
        )
        data = self.tracker.to_dict()
        assert data["task_id"] == "test-task-1"
        assert data["progress"] == 0.75
        assert data["current_stage"] == "synthesize"
        assert data["processed_chars"] == 1500
        assert data["total_chars"] == 2000

    def test_stage_history(self):
        """应记录阶段历史"""
        stages = ["import", "parse", "clean", "split"]
        for i, stage in enumerate(stages):
            self.loop.run_until_complete(
                self.tracker.update((i + 1) / len(stages), stage)
            )
        assert len(self.tracker.stage_history) == len(stages)
        assert self.tracker.stage_history[-1]["stage"] == "split"

    def test_subscribe(self):
        """应支持订阅回调"""
        received = []

        def callback(data):
            received.append(data)

        self.tracker.subscribe(callback)
        self.loop.run_until_complete(
            self.tracker.update(0.5, "test_stage")
        )
        assert len(received) == 1
        assert received[0]["progress"] == 0.5

    def test_status_timing(self):
        """状态转换应记录时间"""
        assert self.tracker.started_at is None
        self.loop.run_until_complete(
            self.tracker.update(0.1, status="running")
        )
        assert self.tracker.started_at is not None

        self.loop.run_until_complete(
            self.tracker.update(1.0, status="completed")
        )
        assert self.tracker.completed_at is not None

    def teardown_method(self):
        self.loop.close()