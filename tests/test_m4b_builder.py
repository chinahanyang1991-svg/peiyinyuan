"""M4B 构建器测试"""
import io
import os
import sys
import tempfile
import wave

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest


def _silence_wav(ms: int, sample_rate: int = 48000) -> bytes:
    """真静音 WAV（16-bit 单声道），让章节时间轴由 WAV 真实时长决定（PY-126 同源）。"""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(b"\x00" * (int(sample_rate * ms / 1000) * 2))
    return buf.getvalue()


class TestM4BBuilder:
    def test_import(self):
        from modules.tts_engine.m4b_builder import M4BBuilder
        b = M4BBuilder()
        assert isinstance(b.available, bool)

    def test_metadata_writing(self):
        """[2H 裁定: 契约已变更 → 按新契约改写测试，产品码不动] PY-125 + PY-126

        失败是 KeyError: 'start_ms'，但根因**不是**"测试还在用旧键"：
        段字典侧的三键(audio_data/chapter_title/duration_ms)本用例早已是新的。
        真正变更的是**职责拆分**（commit a63c5d9）：
          旧: `_write_ffmetadata(audio_segments, ...)` 自己从 duration_ms 累加时间轴
              → 旧测试可以直投段字典。
          新: PY-126 要求"章节时间轴与 concat 列表同源"，累加移出渲染器：
              段字典 --build_chapters()/``_build_chapters()``-->
              chapters{title,start_ms,end_ms,duration_ms,segment_index}
              --_write_ffmetadata(chapters, ...)--> 只做 ffmetadata 文本渲染。
        测试把"段"直投给"渲染器"才 KeyError → 属测试未随契约更新，非产品回归
        （P9 端到端章节真写盘已由 run_p9_package + verify-round3 §PY-125/126 真产物复核）。
        另按 PY-125 补两条契约锚: 真实时长以 WAV 为准(duration_ms 仅兜底)、
        旧键段字典(title/start_time/duration)不再被接受(不产出章节→build() 抛 ValueError)。
        """
        from modules.tts_engine.m4b_builder import M4BBuilder
        b = M4BBuilder()
        segments = [
            {"chapter_title": "第一章", "duration_ms": 5000, "audio_data": _silence_wav(500)},
            {"chapter_title": "第二章", "duration_ms": 3000, "audio_data": _silence_wav(300)},
        ]
        metadata = {"title": "测试书", "author": "测试作者", "year": "2024"}

        # 步骤1 (PY-126): 段字典 -> 章节时间轴，时长以 WAV 真实时长为准（声明值 5000 被忽略）
        chapters = b.build_chapters(segments, gap_ms=0)
        assert [c["title"] for c in chapters] == ["第一章", "第二章"]
        assert chapters[0]["start_ms"] == 0.0
        assert abs(chapters[0]["duration_ms"] - 500.0) < 1.0, "时间轴未取 WAV 真实时长"
        assert abs(chapters[1]["start_ms"] - 500.0) < 1.0, "第二章起点未累加第一章"

        # 步骤2 (PY-125/PY-126 契约): 渲染器只吃 chapters
        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False, mode="w", encoding="utf-8") as f:
            b._write_ffmetadata(chapters, metadata, f.name)
            content = open(f.name, encoding="utf-8").read()
        os.unlink(f.name)
        assert "title=测试书" in content
        assert "artist=测试作者" in content
        assert "date=2024" in content
        assert "第一章" in content

        lines = content.splitlines()
        starts = [int(l.split("=", 1)[1]) for l in lines if l.startswith("START=")]
        ends = [int(l.split("=", 1)[1]) for l in lines if l.startswith("END=")]
        assert len(starts) == len(ends) == 2, "章节块数量与时间轴不一致"
        assert starts == [0, 500], f"START 非累加(旧缺陷'所有章节起点都是 0'): {starts}"
        assert ends == [500, 800], f"END 与 WAV 时长不符: {ends}"
        assert all(e > s for s, e in zip(starts, ends)), "存在零/负宽章节"
        assert lines[0] == ";FFMETADATA1"

        # PY-125: 旧键段字典（title/start_time/duration）被显式拒绝，不再静默产出无章节文件
        legacy = [{"title": "第一章", "start_time": 0, "duration": 3.2},
                  {"title": "第二章", "start_time": 3.2, "duration": 2.1}]
        assert b.build_chapters(legacy) == [], "旧键段字典仍被当成有效章节"
        if b.available:
            with pytest.raises(ValueError):
                b.build(legacy, {"title": "t"}, "_should_not_be_written.m4b")

    def test_empty_segments_raises(self):
        from modules.tts_engine.m4b_builder import M4BBuilder
        b = M4BBuilder()
        if b.available:
            with pytest.raises(ValueError):
                b.build([], {"title": "test"}, "output.m4b")