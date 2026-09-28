"""WF-REPORT 第②节修复回归测试 (WFFIX-4)。

覆盖 6 条缺陷的"复现 -> 修复 -> 复测"断言：
  WF-07 取消不中止 / 终态被 failed 覆盖  -> TestWF07*
  WF-08 纯标点句中止整本书 (PY-117 语义保留) -> TestWF08*
  WF-13 mix_applied 不可观测               -> TestWF13*
  WF-15 /api/bgm ffmpeg_available 假阴性   -> TestWF15*
  WF-17 /api/workflows 恒桩                -> TestWF17*
  WF-19 削顶素材标 verified:true           -> TestWF19*

设计约束（与仓库既有测试一致）：
  * 不加载真引擎（_FakeEngine 顶替 engine_registry）、不打网络；
  * 不写生产库 —— task_service 相关用例把 backend.models.base.get_db_path 指向
    tmp_path 下的临时 sqlite（与 tools/voice_library/ingest.open_session 同一手法）；
  * 全程无 emoji（Windows GBK 控制台安全）。
"""
import asyncio
import json
import os
import sys
import types

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
# WF-19 直接复用入库/复检脚本本体（tools/voice_library 不是包，按脚本目录导入）
sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "tools", "voice_library")))

SR = 24000


# --------------------------------------------------------------------- fixtures
@pytest.fixture()
def temp_db(tmp_path, monkeypatch):
    """把 ORM 全局引擎指向临时 sqlite，测试结束后由 monkeypatch 自动还原。"""
    import backend.models.base as base

    db = tmp_path / "wf4.db"
    monkeypatch.setattr(base, "get_db_path", lambda: str(db))
    monkeypatch.setattr(base, "_engine", None, raising=False)
    monkeypatch.setattr(base, "_SessionLocal", None, raising=False)
    base.get_session().close()          # 触发建表
    return db


# --------------------------------------------------------------------- 假引擎
def _tone(seconds: float = 0.1, peak: float = 0.5, sr: int = SR) -> np.ndarray:
    n = max(int(sr * seconds), 1)
    t = np.arange(n, dtype=np.float32) / sr
    return (np.sin(2 * np.pi * 220.0 * t).astype(np.float32) * np.float32(peak))


class _FakeEngine:
    """按需在第 cancel_at 次调用时触发取消 / 在第 fail_at 次调用时抛错。"""

    def __init__(self, online=True, cancel_at=None, on_cancel=None, fail_at=None,
                 raises=RuntimeError("NoAudioReceived")):
        self.engine_name = "faketest"
        self.online = online
        self.sample_rate = SR
        self.calls = []
        self.cancel_at = cancel_at
        self.on_cancel = on_cancel
        self.fail_at = fail_at
        self.raises = raises

    def synthesize(self, text, voice_id=None, speed=1.0, pitch=0.0, emotion=None,
                   **kw):
        i = len(self.calls)
        self.calls.append(text)
        # 取消先落地，再决定是否抛错 —— 覆盖"取消请求与在途合成同时发生"(WF-07)
        if self.cancel_at is not None and i == self.cancel_at and self.on_cancel:
            self.on_cancel()
        if self.fail_at is not None and i == self.fail_at:
            raise self.raises
        return _tone()


def _synth_ctx(n=30, texts=None):
    texts = texts or ["第%d句，有内容的文本。" % i for i in range(n)]
    sentences = [{"chapter_id": "c1", "chapter_title": "第一章", "text": t,
                  "char_count": len(t)} for t in texts]
    return {"task_id": "wf4-task", "project_id": None, "sentences": sentences,
            "processed_chars": 0, "total_chars": sum(len(t) for t in texts),
            "sample_rate": SR, "status": "running", "chapters": []}


@pytest.fixture()
def no_emit(monkeypatch):
    """掐掉 _emit 的 DB/WS 写入（本文件只验证控制流，不验证进度落库）。"""
    from modules.workflow.conversion_workflow import ConversionWorkflow

    calls = []

    async def fake_emit(self, task_id, context, stage, progress, message="",
                        error="", status=""):
        calls.append((stage, round(progress, 4), message))

    monkeypatch.setattr(ConversionWorkflow, "_emit", fake_emit)
    return calls


@pytest.fixture()
def registry_stub(monkeypatch):
    """engine_registry.get / get_loaded 顶替（在线/本地两条路径共用）。"""
    from modules.tts_engine.engine_registry import engine_registry
    from modules.voice_manager.voice_store import voice_store

    holder = {}

    def _get(name=None):
        return holder.get("engine")

    def _get_loaded(name=None, strict=True):
        return holder["engine"]

    monkeypatch.setattr(engine_registry, "get", _get)
    monkeypatch.setattr(engine_registry, "get_loaded", _get_loaded)
    monkeypatch.setattr(voice_store, "get", lambda vid: None, raising=False)
    return holder


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


@pytest.fixture(autouse=True)
def _isolate_cancel_intent():
    """conversion_workflow 是进程级单例 —— 逐用例清空取消意图集合，
    避免上一用例的 wf.cancel("wf4-task") 泄漏到后续用例（真实运行时每次
    转换都是新的 uuid task_id，且 run_conversion 收尾会 clear）。"""
    from modules.workflow.conversion_workflow import conversion_workflow as wf

    saved = set(wf._cancelled_tasks)
    wf._cancelled_tasks.clear()
    yield
    wf._cancelled_tasks.clear()
    wf._cancelled_tasks.update(saved)


# ----------------------------------------------------------------------- WF-07
class TestWF07Cancel:
    """取消必须在句循环内生效，且终态不得被 failed 覆盖。"""

    def test_cancel_stops_online_concurrent_loop(self, no_emit, registry_stub,
                                                 monkeypatch):
        from modules.workflow.conversion_workflow import conversion_workflow as wf

        engine = _FakeEngine(online=True, cancel_at=5,
                             on_cancel=lambda: wf.cancel("wf4-task"))
        registry_stub["engine"] = engine
        ctx = _synth_ctx(30)
        out = _run(wf._stage_synthesize(ctx, {"engine": "faketest"}))
        # 取消发生在第 6 次调用 -> 并发块 x6，只可能再多跑 0 个块
        assert len(engine.calls) <= 12, "取消后仍在合成整本书: calls=%d" % len(engine.calls)
        assert len(engine.calls) < 30
        assert out.get("status") == "cancelled" or ctx.get("status") == "cancelled"

    def test_cancel_stops_sequential_loop(self, no_emit, registry_stub, monkeypatch):
        from modules.workflow.conversion_workflow import ConversionWorkflow
        from modules.workflow.conversion_workflow import conversion_workflow as wf

        engine = _FakeEngine(online=False, cancel_at=3,
                             on_cancel=lambda: wf.cancel("wf4-task"))
        registry_stub["engine"] = engine

        async def fake_acquire(self, task_id, context, ename):
            return engine

        monkeypatch.setattr(ConversionWorkflow, "_acquire_engine", fake_acquire)
        ctx = _synth_ctx(30)
        _run(wf._stage_synthesize(ctx, {"engine": "faketest"}))
        assert len(engine.calls) <= 4, "顺序路径未在中途响应取消: calls=%d" % len(engine.calls)
        assert ctx.get("status") == "cancelled"

    def test_cancel_flag_works_without_pipeline(self):
        """取消先于 pipeline 注册（POST /convert 刚返回就被 cancel）也必须被记住。"""
        from modules.workflow.conversion_workflow import conversion_workflow as wf

        tid = "wf4-late-pipeline"
        assert wf.cancel(tid) is False          # 无 pipeline：旧实现直接丢失取消意图
        assert wf.is_cancelled(tid) is True     # 取消意图仍被记录
        wf.clear_cancelled(tid)

    def test_fail_task_does_not_overwrite_cancelled(self, temp_db):
        from backend.services.task_service import task_service

        t = task_service.create_task(book_id="b1", output_format="wav")
        tid = t["id"]
        task_service.update_progress(tid, 0.667, "synthesize", 40, 60)
        assert task_service.cancel_task(tid) is True
        task_service.fail_task(tid, "第 10/40 句合成失败: NoAudioReceived")
        row = task_service.get_task(tid)
        assert row["status"] == "cancelled", "取消终态被 failed 覆盖 (WF-07)"
        assert row["completed_at"], "取消应写 completed_at 终态时间"
        assert any("合成失败" in (e.get("message") or "") or
                   "第 10/40" in (e.get("error") or "") for e in row["logs"]), row["logs"]
        task_service.delete_task(tid)

    def test_cancelled_task_progress_is_frozen(self, temp_db):
        """已取消的行不得再被 update_progress 推进度/改状态（进度回跳 0.683->0.778 的根因）。"""
        from backend.services.task_service import task_service

        t = task_service.create_task(book_id="b1", output_format="wav")
        tid = t["id"]
        task_service.update_progress(tid, 0.667, "synthesize", 40, 60)
        task_service.cancel_task(tid)
        task_service.update_progress(tid, 0.778, "synthesize", 50, 60, message="合成中")
        row = task_service.get_task(tid)
        assert row["status"] == "cancelled"
        assert row["progress"] == pytest.approx(0.667), "取消后进度仍被推进"

    def test_finalize_cancelled_sets_terminal(self, temp_db):
        from backend.services.task_service import task_service

        t = task_service.create_task(book_id="b1", output_format="wav")
        tid = t["id"]
        task_service.update_progress(tid, 0.5, "synthesize", 30, 60)
        out = task_service.finalize_cancelled(tid, "用户取消，已停止合成")
        assert out["status"] == "cancelled"
        row = task_service.get_task(tid)
        assert row["status"] == "cancelled" and row["completed_at"]
        assert row["logs"][-1]["stage"] == "cancelled"
        task_service.delete_task(tid)

    def test_cancel_endpoint_notifies_workflow_first(self, temp_db, monkeypatch):
        """路由层顺序：先置内存取消标志，再落 DB —— 反向顺序会丢掉早到的取消。"""
        import backend.api.tasks as tasks_api

        seen = {}

        def fake_cancel(task_id):
            seen["db_at_notify"] = tasks_api.task_service.get_task(task_id)["status"]
            seen["notified"] = task_id
            return True

        monkeypatch.setattr(tasks_api.conversion_workflow, "cancel", fake_cancel)
        t = tasks_api.task_service.create_task(book_id="b1", output_format="wav")
        tid = t["id"]
        resp = _run(tasks_api.cancel_task(tid))
        assert seen["notified"] == tid
        assert seen["db_at_notify"] == "queued", "取消意图先于 DB 写状态 (WF-07)"
        assert resp["status"] == "ok" and resp["cancelled"] is True
        assert resp["task"]["status"] == "cancelled" and resp["task"]["completed_at"]
        tasks_api.task_service.delete_task(tid)

    def test_run_conversion_lands_cancelled_terminal(self, temp_db, registry_stub,
                                                     monkeypatch):
        """任务级复现/复测：整条 run_conversion 在取消后必须落 cancelled 终态。

        旧实现三处同时错：①句循环不看取消标志（整本书照跑完）；②收尾走 failed
        分支 -> KeyError 'output_path'（context 已 clear 却仍按 result 取键）；
        ③task_service.fail_task 无条件把 cancelled 覆成 failed。
        """
        import backend.services.book_service as bs_mod
        import backend.services.dubbing_service as ds_mod
        from backend.services.task_service import task_service
        from modules.tts_engine.model_preloader import model_preloader
        from modules.workflow.conversion_workflow import conversion_workflow as wf

        text = "".join("第%d句，这是有内容的正文。" % i for i in range(40))
        monkeypatch.setattr(
            bs_mod.book_service, "get_book",
            lambda bid: {"id": bid, "title": "WF07 测试书", "total_chars": len(text),
                         "language": "zh"})
        monkeypatch.setattr(
            bs_mod.book_service, "get_chapters",
            lambda bid: [{"id": "c1", "title": "第一章", "content": text,
                          "char_count": len(text), "content_preview": ""}])
        monkeypatch.setattr(ds_mod.dubbing_service, "get_project",
                            lambda pid: None, raising=False)
        monkeypatch.setattr(model_preloader, "start",
                            lambda *a, **kw: None, raising=False)

        holder = {}
        engine = _FakeEngine(online=True, cancel_at=1,
                             on_cancel=lambda: wf.cancel(holder["tid"]))
        registry_stub["engine"] = engine
        t = task_service.create_task(book_id="b1", output_format="wav")
        holder["tid"] = tid = t["id"]

        out = _run(wf.run_conversion(tid, None, "b1", "wav", {"engine": "faketest"}))
        assert out["status"] == "cancelled", out
        assert "output_path" in out and "error_message" in out, \
            "收尾分支不再 KeyError (WF-07)"
        assert len(engine.calls) < 12, "取消后仍在合成整本书: calls=%d" % len(engine.calls)

        row = task_service.get_task(tid)
        assert row["status"] == "cancelled", "终态被覆盖 (WF-07)"
        assert row["completed_at"], "取消未写终态时间"
        assert row["progress"] < 1.0
        assert any(e.get("stage") == "cancelled" for e in row["logs"]), row["logs"]
        assert wf.is_cancelled(tid) is False, "收尾未清理取消意图集合"
        task_service.delete_task(tid)

        # ---- 场景二（原始证据复测）：取消在途 + 手上最后一句合成失败 ----
        # eb0af35b 的真实形态就是"取消请求已到，最后一次 NoAudioReceived 把 stage
        # 打成 failed"，旧收尾据此调 fail_task -> 终态被覆成 failed。
        holder2 = {}
        engine2 = _FakeEngine(online=True, cancel_at=1, fail_at=1,
                              raises=RuntimeError("NoAudioReceived"),
                              on_cancel=lambda: wf.cancel(holder2["tid"]))
        registry_stub["engine"] = engine2
        t2 = task_service.create_task(book_id="b1", output_format="wav")
        holder2["tid"] = tid2 = t2["id"]

        out2 = _run(wf.run_conversion(tid2, None, "b1", "wav",
                                      {"engine": "faketest"}))
        assert out2["status"] == "cancelled", \
            "取消在途时的失败把收尾状态改写成 failed (WF-07): %r" % (out2,)
        assert len(engine2.calls) < 12, engine2.calls
        row2 = task_service.get_task(tid2)
        assert row2["status"] == "cancelled" and row2["completed_at"], row2
        assert not row2.get("error_message"), \
            "cancelled 行不应写入 failed 的 error_message: %r" % row2.get("error_message")
        assert any(e.get("stage") == "cancelled" for e in row2["logs"]), row2["logs"]
        assert wf.is_cancelled(tid2) is False
        task_service.delete_task(tid2)


# ----------------------------------------------------------------------- WF-08
class TestWF08PunctuationOnly:
    PATHOLOGICAL = ("他。走。在。长。长。的。街。道。上。，。路。灯。把。影。子。"
                    "拉。得。很。长。。。风。从。巷。口。灌。进。来。，。吹。落。了。"
                    "几。片。枯。叶。。。")

    def test_split_drops_unpronounceable_segments(self, monkeypatch):
        from modules.workflow.conversion_workflow import conversion_workflow as wf

        ctx = {"task_id": "wf4-08", "chapters": [{"id": "c1", "title": "第一章",
                                                  "content": self.PATHOLOGICAL,
                                                  "char_count": len(self.PATHOLOGICAL),
                                                  "content_preview": ""}],
               "settings": {}, "book_language": "zh"}
        wf._stage_clean(ctx)
        out = wf._stage_split(ctx)
        sents = ctx["sentences"]
        bad = [s["text"] for s in sents
               if not any(ch.isalnum() for ch in s["text"])]
        assert not bad, "纯标点句仍进入合成清单: %s" % bad[:5]
        assert out["dropped_empty_segments"] >= 1, out
        assert out["split_stats"]["dropped_empty_segments"] >= 1
        assert len(sents) > 0, "把好句也一起丢了"

    def test_helper_treats_punctuation_digits_as_empty(self):
        from modules.workflow.conversion_workflow import has_pronounceable_content

        assert not has_pronounceable_content("，。")
        assert not has_pronounceable_content("  ——  \u3000 ")
        assert not has_pronounceable_content("...!?;,")
        assert has_pronounceable_content("雨点砸在青石板上")
        assert has_pronounceable_content("Hello, world!")
        assert has_pronounceable_content("第2章")   # 数字算可发音

    def test_real_engine_failure_still_aborts_book(self, no_emit, registry_stub,
                                                   monkeypatch):
        """PY-117 语义保留：真实合成故障必须中止整本书，不许静音假成功。"""
        from modules.workflow.conversion_workflow import conversion_workflow as wf
        from modules.workflow.conversion_workflow import ConversionWorkflow

        engine = _FakeEngine(online=False, fail_at=1)
        registry_stub["engine"] = engine

        async def fake_acquire(self, task_id, context, ename):
            return engine

        monkeypatch.setattr(ConversionWorkflow, "_acquire_engine", fake_acquire)
        ctx = _synth_ctx(6)
        with pytest.raises(RuntimeError) as exc:
            _run(wf._stage_synthesize(ctx, {"engine": "faketest"}))
        assert "合成失败" in str(exc.value) and "NoAudioReceived" in str(exc.value)

    def test_silence_from_engine_still_aborts(self, no_emit, registry_stub, monkeypatch):
        """引擎返回全零 -> 依旧判失败（未被 WF-08 的过滤放宽）。"""
        from modules.workflow.conversion_workflow import ConversionWorkflow
        from modules.workflow.conversion_workflow import conversion_workflow as wf

        engine = _FakeEngine(online=False)
        engine.synthesize = lambda **kw: np.zeros(SR // 10, dtype=np.float32)
        registry_stub["engine"] = engine

        async def fake_acquire(self, task_id, context, ename):
            return engine

        monkeypatch.setattr(ConversionWorkflow, "_acquire_engine", fake_acquire)
        with pytest.raises(RuntimeError) as exc:
            _run(wf._stage_synthesize(_synth_ctx(4), {"engine": "faketest"}))
        assert "静音" in str(exc.value)


# ----------------------------------------------------------------------- WF-13
class TestWF13MixObservability:
    def _postprocess(self, monkeypatch, applied=True, stats=None):
        import backend.services.task_service as ts_mod
        import modules.workflow.conversion_workflow as cw

        logs = []

        def fake_append_log(task_id, stage, message, extra=None):
            logs.append({"task_id": task_id, "stage": stage, "message": message,
                         "extra": extra})
            return True

        monkeypatch.setattr(ts_mod.task_service, "append_log", fake_append_log,
                            raising=False)
        monkeypatch.setattr(cw, "_project_mix_settings",
                            lambda pid, task_id=None: {"bgm": {"ref": "bgm_warm_pad"}})

        def fake_mix(ctx, mix_settings, processed):
            ctx["mix_stats"] = stats if stats is not None else {
                "chapters": 2, "bgm_gain_db": -21.0, "ducked": True}
            return {"audio_arrays": processed, "applied": applied, "chapters": 2}

        import modules.workflow.cast_pipeline_hooks as hooks
        monkeypatch.setattr(hooks, "postprocess_with_mix", fake_mix)
        wf = cw.ConversionWorkflow()
        ctx = {"task_id": "wf4-13", "project_id": "p1", "sample_rate": SR,
               "audio_arrays": [{"index": 0, "chapter_id": "c1",
                                 "chapter_title": "第一章", "audio": _tone(0.4),
                                 "duration_ms": 400.0, "peak": 0.5, "text": "正文"}]}
        out = wf._stage_postprocess(ctx)
        return out, ctx, logs

    def test_mix_applied_written_to_task_logs(self, monkeypatch):
        out, ctx, logs = self._postprocess(monkeypatch, applied=True)
        assert out["mix_applied"] is True
        hits = [e for e in logs if "mix" in (e["message"] + str(e["extra"])).lower()]
        assert hits, "mix_applied 未写入 task logs (WF-13): %s" % logs
        assert hits[0]["stage"] == "postprocess"
        assert "mix_applied=True" in hits[0]["message"]
        assert "bgm_gain_db" in hits[0]["message"] or hits[0]["extra"]

    def test_mix_not_applied_also_logged(self, monkeypatch):
        _out, _ctx, logs = self._postprocess(monkeypatch, applied=False)
        assert any("mix_applied=False" in e["message"] for e in logs), logs

    def test_append_log_writes_logs_column_and_api(self, temp_db):
        from backend.services.task_service import task_service
        import backend.api.tasks as tasks_api
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        t = task_service.create_task(book_id="b1", output_format="wav")
        tid = t["id"]
        assert task_service.append_log(tid, "postprocess", "mix_applied=True",
                                      extra={"mix_stats": {"chapters": 2}})
        row = task_service.get_task(tid)
        assert row["logs"][-1]["message"].startswith("mix_applied=True")
        assert row["logs"][-1]["mix_stats"] == {"chapters": 2}

        app = FastAPI()
        app.include_router(tasks_api.router, prefix="/api/tasks")
        client = TestClient(app)
        resp = client.get(f"/api/tasks/{tid}")
        assert resp.status_code == 200
        body = resp.json()["task"]
        assert any("mix_applied" in (e.get("message") or "") for e in body["logs"]), body
        task_service.delete_task(tid)


# ----------------------------------------------------------------------- WF-15
class TestWF15FfmpegProbe:
    @pytest.fixture(autouse=True)
    def _no_path_ffmpeg(self, monkeypatch):
        """模拟本次取证环境：PATH 里没有 ffmpeg，但 imageio-ffmpeg 自带可执行文件。"""
        def fake_which(name, *a, **kw):
            return None if "ffmpeg" in str(name) else None
        import shutil as _shutil
        monkeypatch.setattr(_shutil, "which", fake_which)
        from modules.tts_engine import ffmpeg_utils
        ffmpeg_utils.reset_cache()
        yield
        ffmpeg_utils.reset_cache()

    def test_probe_finds_imageio_fallback(self):
        from modules.tts_engine import ffmpeg_utils

        p = ffmpeg_utils.probe(force=True)
        assert p["path"], "环境里本应有 imageio-ffmpeg 兜底: %s" % p["error"]
        assert p["source"] in ("imageio-ffmpeg", "config")

    def test_cast_ffmpeg_path_uses_shared_probe(self, monkeypatch):
        import backend.api.cast as cast_api
        from modules.tts_engine import ffmpeg_utils

        monkeypatch.setattr(cast_api, "_FFMPEG_PROBE", {"checked_at": 0.0, "path": None})
        got = cast_api.ffmpeg_path()
        assert got == ffmpeg_utils.resolve_ffmpeg(), "cast 仍只看 PATH (WF-15)"
        assert got, "ffmpeg_available 假阴性复现"

    def test_bgm_store_uses_shared_probe(self):
        from modules.mixer import bgm_store
        from modules.tts_engine import ffmpeg_utils

        assert bgm_store.ffmpeg_executable() == ffmpeg_utils.resolve_ffmpeg()

    def test_upload_transcodes_with_imageio_ffmpeg(self, tmp_path):
        """PATH 无 ffmpeg 时上传仍走 ffmpeg 转码（converter=ffmpeg），而非 soundfile。"""
        import soundfile as sf
        from modules.mixer import BgmStore

        src = tmp_path / "in.wav"
        sf.write(str(src), _tone(0.2, 0.4, 22050), 22050, subtype="PCM_16")
        store = BgmStore(root=tmp_path / "lib")
        rec = store.add_audio_file(src, name="WF15 测试", kind="bgm")
        assert rec["source"] == "ffmpeg", rec
        assert rec["ffmpeg_source"] in ("imageio-ffmpeg", "config", "path")

    def test_bgm_endpoint_reports_true_availability(self, monkeypatch):
        import backend.api.cast as cast_api

        monkeypatch.setattr(cast_api, "_FFMPEG_PROBE", {"checked_at": 0.0, "path": None})
        app = __import__("fastapi").FastAPI()
        app.include_router(cast_api.router, prefix="/api")
        from fastapi.testclient import TestClient

        resp = TestClient(app).get("/api/bgm")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["ffmpeg_available"] is True, body
        assert body.get("ffmpeg_source"), "应上报探测来源，便于区分 PATH/imageio 兜底"


# ----------------------------------------------------------------------- WF-17
class TestWF17StubEndpoints:
    def _client(self, monkeypatch):
        import backend.api.workflows as wf_api
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        templates = [{"id": "t1", "name": "整书转换", "engine": "voxcpm"}]
        monkeypatch.setattr(wf_api.workflow_template_service, "list_all",
                            lambda limit=50: list(templates))
        app = FastAPI()
        app.include_router(wf_api.router, prefix="/api/workflows")
        return TestClient(app)

    def test_get_root_returns_real_templates(self, monkeypatch):
        client = self._client(monkeypatch)
        resp = client.get("/api/workflows/")
        assert resp.status_code == 200
        body = resp.json()
        assert body["workflows"] and body["workflows"][0]["id"] == "t1", \
            "GET /api/workflows/ 仍返回恒空桩 (WF-17)"
        assert body["total"] == 1
        assert "templates" in json.dumps(body, ensure_ascii=False)

    def test_create_is_405_with_allow(self, monkeypatch):
        client = self._client(monkeypatch)
        resp = client.post("/api/workflows/create", json={"name": "x"})
        assert resp.status_code == 405, resp.text
        assert "/api/workflows/templates" in resp.headers.get("allow", ""), \
            resp.headers
        assert resp.json()["detail"]


# ----------------------------------------------------------------------- WF-19
class TestWF19ClipGate:
    @pytest.fixture(autouse=True)
    def _restore_db_globals(self):
        """ingest.open_session 会全局改指向临时库 —— 逐用例快照/还原。"""
        import backend.models.base as base

        snap = (base.get_db_path, base._engine, base._SessionLocal)
        yield
        base.get_db_path, base._engine, base._SessionLocal = snap

    def _ingest_items(self, tmp_path):
        import ingest as IN

        wav_dir = tmp_path / "wav"
        wav_dir.mkdir(parents=True, exist_ok=True)
        import soundfile as sf

        clean = wav_dir / "clean.wav"
        clipped = wav_dir / "clipped.wav"
        sr = 22050
        t = np.arange(int(sr * 2.0), dtype=np.float32) / sr
        sig = np.sin(2 * np.pi * 220 * t).astype(np.float32)
        sf.write(str(clean), sig * np.float32(0.7), sr, subtype="PCM_16")
        hard = np.sign(sig) * np.float32(0.99999)
        sf.write(str(clipped), hard.astype(np.float32), sr, subtype="PCM_16")
        return IN, [
            {"category": "electronic", "name": "干净样本", "local_file": str(clean),
             "page_url": "https://wf4.local/clean", "license": "CC0",
             "duration_s": 2.0},
            {"category": "electronic", "name": "削顶样本", "local_file": str(clipped),
             "page_url": "https://wf4.local/clipped", "license": "CC0",
             "duration_s": 2.0},
        ]

    def test_gate_thresholds(self):
        import ingest as IN

        assert IN.clip_gate_reason({"clip_ratio": 0.0, "peak": 0.89}) == ""
        assert "clip_ratio" in IN.clip_gate_reason({"clip_ratio": 0.016875, "peak": 1.0})
        assert "peak" in IN.clip_gate_reason({"clip_ratio": 0.0002, "peak": 1.0449})
        assert IN.MAX_CLIP_RATIO == pytest.approx(0.001)
        assert IN.MAX_PEAK_ACCEPT == pytest.approx(0.999)

    def test_ingest_marks_clipped_row_unverified(self, tmp_path):
        IN, items = self._ingest_items(tmp_path)
        db = tmp_path / "voices.db"
        st = IN.ingest_manifest(items, apply=True, library_root=tmp_path / "lib",
                               db_path=str(db))
        assert st["accepted"] == 2, st            # 不拒收（不静默丢数据）
        session = IN.open_session(str(db))
        VP = IN._VoiceProfile()
        rows = {r.name: r for r in session.query(VP).filter_by(source="sample").all()}
        session.close()
        assert rows["干净样本"].meta["verified"] is True
        assert rows["削顶样本"].meta["verified"] is False, \
            "削顶素材仍被标已验收 (WF-19)"
        assert "clip" in (rows["削顶样本"].meta.get("verify_note") or "").lower()

    def test_verify_library_demotes_without_deleting(self, tmp_path):
        import ingest as IN
        import verify_library as VL

        db = tmp_path / "voices.db"
        IN, items = self._ingest_items(tmp_path)
        st = IN.ingest_manifest(items, apply=True, library_root=tmp_path / "lib",
                               db_path=str(db))
        assert st["accepted"] == 2
        lib = tmp_path / "lib"

        summary = VL.verify_db(str(db), lib, apply=False, demote=True)
        assert summary["checked"] == 2
        assert summary["demoted"] == 1, summary
        assert summary["deleted"] == 0 and summary["files_removed"] == 0
        session = IN.open_session(str(db))
        VP = IN._VoiceProfile()
        rows = {r.name: r for r in session.query(VP).filter_by(source="sample").all()}
        kept_files = [(r.name, os.path.isfile(r.model_path),
                       os.path.isfile(r.sample_audio_path)) for r in rows.values()]
        flags = {n: r.meta.get("verified") for n, r in rows.items()}
        session.close()
        assert all(m and p for _n, m, p in kept_files), "降级模式不得删文件"
        assert flags == {"干净样本": True, "削顶样本": False}, flags

        # 复检必须幂等，且不得把已降级的行覆写回 True
        again = VL.verify_db(str(db), lib, apply=False, demote=True)
        assert again["demoted"] == 0 and again["still_failing"] == 1
