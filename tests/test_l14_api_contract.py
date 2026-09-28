# -*- coding: utf-8 -*-
"""L14 泳道回归测试：后端 API 微修 5 项。

  WF-21 GET /api/tasks/  status 枚举过滤 + 未知查询参数 422 + total 说实话
  WF-22 PUT /api/books/{id}/chapters  收进 Pydantic 请求体
  WF-23 GET /api/books/  total = COUNT(*) + offset
  US-B3 GET /api/voices/ 列表侧 prompt_mode 打标（prompt-free 可见）
  US-C4 dubbing/cast 试听成功路径 -> record_last_successful_seed 落库 +
        响应头 X-Last-Successful-Seed

隔离约定（与 tests/test_wf_fix4.py / test_wffix3_api_defects.py 同一手法）：
  * backend.models.base.get_db_path 指向 tmp_path 下的临时 sqlite —— 不碰生产库；
  * 不加载真引擎：engine_registry 的 get_loaded/is_loaded 用假引擎顶替；
  * 不打网络、不重启任何服务；控制台仅 ASCII（Windows GBK 安全）。
"""
import os
import sys
import uuid

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

SR = 24000


def _tone(seconds: float = 0.15, peak_: float = 0.5, sr: int = SR) -> np.ndarray:
    """过 P0-4 双门的假音频。

    门有两道（`modules/voice_manager/audio_quality.py`）：
      * 静音地板 -50 dBFS —— 峰值要够响；
      * 分帧谱平坦度 >= 1e-7 —— **纯正弦会被这道门判成"嗡嗡声退化"**，
        所以这里在 220Hz 基音上叠均匀噪声，得到语音样的平坦谱。
    """
    n = max(int(sr * seconds), 256)
    rng = np.random.default_rng(20260921)
    t = np.arange(n, dtype=np.float32) / sr
    tone = np.sin(2 * np.pi * 220.0 * t).astype(np.float32) * np.float32(0.25)
    noise = rng.uniform(-0.25, 0.25, n).astype(np.float32)
    data = tone + noise
    peak = float(np.max(np.abs(data))) or 1.0
    return (data / np.float32(peak) * np.float32(peak_)).astype(np.float32)


class _SeedEngine:
    """supports_seed 可指定的假引擎；记录每次收到的 seed，证明"不发字段"纪律。"""

    def __init__(self, supports_seed=True, engine_name="faketest"):
        self.supports_seed = supports_seed
        self.engine_name = engine_name
        self.sample_rate = SR
        self.calls = []

    # `seed` 必须是**显式形参**：`audio_quality._accepts_seed()` 按签名判定
    # "这个引擎收不收 seed"，只写 **kwargs 会被判为收不到。
    def synthesize(self, text, voice_id=None, speed=1.0, pitch=0.0, emotion=None,
                   seed=None, **kw):
        self.calls.append({"text": text, "voice_id": voice_id,
                           "kw": dict(kw, **({"seed": seed} if seed is not None
                                             else {}))})
        return _tone()


@pytest.fixture()
def temp_db(tmp_path, monkeypatch):
    """ORM 全局引擎指向临时 sqlite（结束后由 monkeypatch 自动还原）。"""
    import backend.models.base as base

    db = tmp_path / "l14.db"
    monkeypatch.setattr(base, "get_db_path", lambda: str(db))
    monkeypatch.setattr(base, "_engine", None, raising=False)
    monkeypatch.setattr(base, "_SessionLocal", None, raising=False)
    base.get_session().close()          # 触发建表
    return str(db)


@pytest.fixture()
def client(temp_db):
    """只挂本次改动涉及的路由；不加 `with` => 跳过 lifespan 预热。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from backend.api import books as books_api
    from backend.api import tasks as tasks_api
    from backend.api import voices as voices_api

    app = FastAPI()
    app.include_router(books_api.router, prefix="/api/books", tags=["books"])
    app.include_router(tasks_api.router, prefix="/api/tasks", tags=["tasks"])
    app.include_router(voices_api.router, prefix="/api/voices", tags=["voices"])
    return TestClient(app)


# ------------------------------------------------------------------ 建模助手
def _make_task(status, created_offset=0):
    from datetime import datetime, timedelta

    from backend.models.base import get_session
    from backend.models.task import ConversionTask

    session = get_session()
    try:
        task = ConversionTask(
            id=str(uuid.uuid4()), status=status, progress=1.0,
            output_format="m4b", settings={},
            created_at=datetime.utcnow() - timedelta(minutes=created_offset))
        session.add(task)
        session.commit()
        return task.id
    finally:
        session.close()


def _make_book(chapters=2):
    from backend.models.base import get_session
    from backend.models.book import Book, Chapter

    session = get_session()
    try:
        book = Book(id=str(uuid.uuid4()), title=f"书-{uuid.uuid4().hex[:6]}",
                    author="测试", format="txt", file_path="C:/x/in.txt",
                    total_chars=100)
        session.add(book)
        ids = []
        for i in range(chapters):
            ch = Chapter(id=str(uuid.uuid4()), book_id=book.id, index=i,
                         title=f"第{i + 1}章", content="正文" * 30, char_count=60)
            session.add(ch)
            ids.append(ch.id)
        session.commit()
        return book.id, ids
    finally:
        session.close()


def _make_voice(engine="gptsovits", transcript=None, ref="C:/x/ref.wav",
                name=None):
    from backend.models.base import get_session
    from backend.models.voice import VoiceProfile

    meta = {"prompt_lang": "zh"}
    if transcript is not None:
        meta["transcript"] = transcript
    session = get_session()
    try:
        prof = VoiceProfile(
            id=str(uuid.uuid4()), name=name or f"v-{uuid.uuid4().hex[:6]}",
            source="cloned", category="human", engine=engine, language="zh",
            gender="female", style_tags=[], description=None,
            reference_audio_path=(ref if ref else None), meta=meta)
        session.add(prof)
        session.commit()
        return prof.id
    finally:
        session.close()


def _row_seed(voice_id):
    from backend.models.base import get_session
    from backend.models.voice import VoiceProfile

    session = get_session()
    try:
        row = session.query(VoiceProfile.seed).filter(
            VoiceProfile.id == voice_id).first()
        return None if row is None else row[0]
    finally:
        session.close()


def _row_chapter(chapter_id):
    from backend.models.base import get_session
    from backend.models.book import Chapter

    session = get_session()
    try:
        ch = session.get(Chapter, chapter_id)
        return None if ch is None else (ch.title, ch.index, ch.content)
    finally:
        session.close()


# ============================================================ WF-21 tasks
class TestWF21TaskFilter:

    def test_status_filter_returns_only_matching_rows(self, client):
        for status, n in (("completed", 3), ("failed", 2), ("cancelled", 1)):
            for i in range(n):
                _make_task(status, created_offset=i)
        resp = client.get("/api/tasks/", params={"status": "failed"})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["total"] == 2
        assert {t["status"] for t in body["tasks"]} == {"failed"}
        assert body["returned"] == 2

    def test_unknown_status_value_is_422(self, client):
        _make_task("completed")
        resp = client.get("/api/tasks/", params={"status": "processing"})
        assert resp.status_code == 422
        locs = [d["loc"] for d in resp.json()["detail"]]
        assert ["query", "status"] in locs

    def test_unknown_query_param_is_422_not_silently_ignored(self, client):
        """缺陷本体：FastAPI 丢掉未声明的查询串 -> ?stats=failed 曾返回全量 200。"""
        _make_task("completed")
        _make_task("failed")
        for junk in ("stats", "page", "statuses"):
            resp = client.get("/api/tasks/", params={junk: "completed"})
            assert resp.status_code == 422, f"{junk} 被静默忽略了"
            detail = resp.json()["detail"]
            assert any(d["loc"][1] == junk for d in detail)

    def test_declared_params_still_accepted(self, client):
        _make_task("completed")
        resp = client.get("/api/tasks/", params={"limit": 10, "offset": 0,
                                                "status": "completed"})
        assert resp.status_code == 200, resp.text

    def test_total_is_count_not_page_size_and_offset_pages(self, client):
        for i in range(5):
            _make_task("completed", created_offset=i)
        first = client.get("/api/tasks/", params={"limit": 2}).json()
        assert first["total"] == 5, "total 仍是本页条数（未改 COUNT(*)）"
        assert first["returned"] == 2 and first["limit"] == 2 and first["offset"] == 0
        ids_page1 = {t["id"] for t in first["tasks"]}
        page2 = client.get("/api/tasks/", params={"limit": 2, "offset": 2}).json()
        ids_page2 = {t["id"] for t in page2["tasks"]}
        assert ids_page1 and not (ids_page1 & ids_page2), "两页重叠 => offset 没生效"
        assert page2["total"] == 5

    def test_negative_offset_rejected(self, client):
        assert client.get("/api/tasks/", params={"offset": -1}).status_code == 422

    def test_status_enum_matches_model_documented_values(self):
        from backend.api.tasks import TASK_STATUSES

        assert TASK_STATUSES == {"queued", "running", "paused",
                                 "completed", "failed", "cancelled"}

    def test_service_layer_count_helpers(self, temp_db):
        from backend.services.task_service import task_service

        _make_task("completed")
        _make_task("completed")
        _make_task("failed")
        assert task_service.count_tasks() == 3
        assert task_service.count_tasks(status="completed") == 2
        assert task_service.count_tasks(status="queued") == 0
        assert len(task_service.list_tasks(10, status="failed")) == 1
        # 旧位置参数调用形态（list_tasks(5)）必须继续可用
        assert len(task_service.list_tasks(1)) == 1


# ============================================================ WF-22 chapters body
class TestWF22ChaptersBody:

    def test_bare_array_body_renames_chapter(self, client):
        """前端 api.updateChapters 发的就是裸数组（复测里它 422）。"""
        book_id, ch_ids = _make_book(2)
        resp = client.put(f"/api/books/{book_id}/chapters",
                          json=[{"id": ch_ids[0], "title": "新章题"}])
        assert resp.status_code == 200, resp.text
        title, index, content = _row_chapter(ch_ids[0])
        assert title == "新章题"
        assert content == "正文" * 30, "未提供的字段被写坏了"

    def test_wrapped_object_body_accepted(self, client):
        book_id, ch_ids = _make_book(2)
        resp = client.put(f"/api/books/{book_id}/chapters",
                          json={"chapters_data": [
                              {"id": ch_ids[1], "title": "包装形态", "index": 7}]})
        assert resp.status_code == 200, resp.text
        title, index, _ = _row_chapter(ch_ids[1])
        assert (title, index) == ("包装形态", 7)

    def test_reordered_and_retyped_rows_all_persist(self, client):
        book_id, ch_ids = _make_book(2)
        resp = client.put(f"/api/books/{book_id}/chapters",
                          json=[{"id": ch_ids[0], "index": 1, "content": "新内容X"},
                                {"id": ch_ids[1], "index": 0}])
        assert resp.status_code == 200, resp.text
        assert _row_chapter(ch_ids[0])[2] == "新内容X"
        assert _row_chapter(ch_ids[0])[1] == 1
        assert _row_chapter(ch_ids[1])[1] == 0

    def test_empty_list_is_explicit_422_not_silent_noop(self, client):
        book_id, _ = _make_book(1)
        resp = client.put(f"/api/books/{book_id}/chapters", json=[])
        assert resp.status_code == 422
        resp2 = client.put(f"/api/books/{book_id}/chapters",
                           json={"chapters_data": []})
        assert resp2.status_code == 422

    def test_item_without_id_is_422(self, client):
        book_id, _ = _make_book(1)
        resp = client.put(f"/api/books/{book_id}/chapters",
                          json=[{"title": "缺 id"}])
        assert resp.status_code == 422, resp.text

    def test_multipart_or_json_string_is_422_not_500(self, client):
        """复测实测：改 multipart 曾 500（"422/500 全家"）。"""
        book_id, _ = _make_book(1)
        resp = client.put(f"/api/books/{book_id}/chapters",
                          files={"file": ("x.txt", b"hello")})
        assert resp.status_code in (415, 422), resp.text
        resp2 = client.put(f"/api/books/{book_id}/chapters",
                           json="not-a-list")
        assert resp2.status_code == 422

    def test_missing_book_is_404(self, client):
        resp = client.put(f"/api/books/{uuid.uuid4()}/chapters",
                          json=[{"id": str(uuid.uuid4()), "title": "x"}])
        assert resp.status_code == 404

    def test_service_signature_still_takes_plain_dicts(self, temp_db):
        """服务层没改签名：非 HTTP 调用方（工作流/脚本）不受影响。"""
        from backend.services.book_service import book_service

        book_id, ch_ids = _make_book(1)
        book_service.update_chapters(book_id, [{"id": ch_ids[0],
                                               "title": "直连服务层"}])
        assert _row_chapter(ch_ids[0])[0] == "直连服务层"


# ============================================================ WF-23 books list
class TestWF23BooksTotal:

    def test_total_is_sql_count_not_page_size(self, client):
        for _ in range(3):
            _make_book(1)
        for limit in (1, 2, 3, 200):
            body = client.get("/api/books/", params={"limit": limit}).json()
            assert body["total"] == 3, f"limit={limit} 时 total={body['total']}"
            assert body["returned"] == min(limit, 3)
            assert body["limit"] == limit

    def test_offset_pages_are_disjoint_and_cover_all(self, client):
        ids = {book_id for book_id, _ in (_make_book(1) for _ in range(5))}
        seen = set()
        offset = 0
        while True:
            body = client.get("/api/books/",
                              params={"limit": 2, "offset": offset}).json()
            got = [b["id"] for b in body["books"]]
            assert len(set(got) & seen) == 0, "翻页出现重叠 => offset 没生效"
            seen.update(got)
            if not got:
                break
            offset += 2
            assert offset < 20, "翻页不收敛"
        assert seen == ids

    def test_negative_offset_is_422(self, client):
        assert client.get("/api/books/", params={"offset": -1}).status_code == 422

    def test_list_shape_backwards_compatible(self, client):
        _make_book(2)
        body = client.get("/api/books/").json()
        assert body["status"] == "ok" and isinstance(body["books"], list)
        assert isinstance(body["total"], int)
        row = body["books"][0]
        assert "chapter_count" in row or "total_chapters" in row

    def test_count_books_helper_matches_orm(self, temp_db):
        from backend.services.book_service import book_service

        _make_book(1)
        _make_book(1)
        assert book_service.count_books() == 2
        assert book_service.list_books(1, offset=1) and \
            len(book_service.list_books(1, offset=1)) == 1


# ============================================================ US-B3 prompt_mode
class TestUSB3PromptModeTag:

    def test_transcriptless_clone_is_tagged_prompt_free(self, client):
        vid = _make_voice(engine="gptsovits", transcript=None)
        rows = client.get("/api/voices/").json()["voices"]
        mine = [r for r in rows if r["id"] == vid]
        assert len(mine) == 1
        assert mine[0]["prompt_mode"] == "prompt_free"

    def test_blank_transcript_also_counts_as_prompt_free(self, client):
        vid = _make_voice(engine="gptsovits", transcript="   ")
        rows = client.get("/api/voices/").json()["voices"]
        assert [r for r in rows if r["id"] == vid][0]["prompt_mode"] == \
            "prompt_free"

    def test_transcribed_clone_is_tagged_prompt(self, client):
        vid = _make_voice(engine="gptsovits", transcript="这是一句参考转写")
        rows = client.get("/api/voices/").json()["voices"]
        assert [r for r in rows if r["id"] == vid][0]["prompt_mode"] == "prompt"

    def test_no_reference_row_tagged_no_reference(self, client):
        vid = _make_voice(engine="gptsovits", transcript=None, ref=None)
        rows = client.get("/api/voices/").json()["voices"]
        assert [r for r in rows if r["id"] == vid][0]["prompt_mode"] == \
            "no_reference"

    def test_engines_without_prompt_text_get_null(self, client):
        """不给 edgetts/voxcpm/sample 打 prompt 标签 —— 它们不读 prompt_text。"""
        ids = [_make_voice(engine=eng, transcript=None)
               for eng in ("edgetts", "kokoro", "voxcpm", "sample")]
        rows = client.get("/api/voices/").json()["voices"]
        picked = {r["id"]: r["prompt_mode"] for r in rows if r["id"] in ids}
        assert set(picked) == set(ids)
        assert all(v is None for v in picked.values())

    def test_detail_endpoint_carries_same_key(self, client):
        vid = _make_voice(engine="gptsovits", transcript=None)
        voice = client.get(f"/api/voices/{vid}").json()["voice"]
        assert voice["prompt_mode"] == "prompt_free"
        assert "seed" in voice, "US-C4 详情侧 seed 键是前端⑧的出口"

    def test_tagging_does_not_stat_filesystem(self, client, monkeypatch):
        """列表一页 500 行，绝不允许每行去 os.path.isfile。"""
        import backend.api.voices as voices_api

        calls = []
        monkeypatch.setattr(os.path, "isfile",
                            lambda p: calls.append(p) or False)
        monkeypatch.setattr(os.path, "exists",
                            lambda p: calls.append(p) or False)
        client.get("/api/voices/", params={"limit": 500})
        assert calls == [], "列表路径里出现了文件系统探测"
        assert voices_api.PROMPT_TEXT_ENGINES == frozenset({"gptsovits"})


# ============================================================ US-C4 seed 生产者
class TestUSC4SeedProducer:

    def test_make_preview_seed_only_for_seed_honoring_engines(self):
        from backend.services.voice_service import make_preview_seed

        seeded = make_preview_seed(_SeedEngine(supports_seed=True))
        assert isinstance(seeded, int) and 0 <= seeded < 1_000_000
        assert make_preview_seed(_SeedEngine(supports_seed=False)) is None

    def test_dubbing_preview_records_seed_and_echoes_header(self, client, monkeypatch,
                                                            temp_db):
        """真路径：POST /api/dubbing/projects/{id}/preview -> seed 落库 + 响应头。"""
        from backend.api import dubbing as dubbing_api
        from backend.models.base import get_session
        from backend.models.project import DubbingProject, Role
        from modules.tts_engine import engine_registry

        vid = _make_voice(engine="voxcpm", transcript=None)
        session = get_session()
        try:
            proj = DubbingProject(id=str(uuid.uuid4()), name="L14-seed",
                                  mode="single", status="draft",
                                  global_settings={}, extra_data={})
            session.add(proj)
            session.add(Role(id=str(uuid.uuid4()), project_id=proj.id,
                                    name="旁白", role_type="narrator",
                                    voice_id=vid, extra_data={}))
            session.commit()
            pid = proj.id
        finally:
            session.close()

        fake = _SeedEngine(supports_seed=True)
        monkeypatch.setattr(engine_registry.engine_registry, "get_loaded",
                            lambda name=None, *a, **k: fake)
        app = client.app
        app.include_router(dubbing_api.router, prefix="/api/dubbing")

        resp = client.post(f"/api/dubbing/projects/{pid}/preview")
        assert resp.status_code == 200, resp.text
        assert fake.calls, "试听没有真的调用引擎合成"
        assert "seed" in fake.calls[0]["kw"], "引擎兑现 seed 却没播种 = 生产者没接上"
        recorded = _row_seed(vid)
        assert recorded is not None and recorded == fake.calls[0]["kw"]["seed"]
        assert resp.headers.get("X-Last-Successful-Seed") == str(recorded)

        detail = client.get(f"/api/voices/{vid}").json()["voice"]
        assert detail["seed"] == recorded

    def test_seedless_engine_receives_no_seed_field(self, client, monkeypatch,
                                                    temp_db):
        """不支持 seed 的引擎：连 seed 字段都不发（后端会 400），也不落库。"""
        from backend.api import dubbing as dubbing_api
        from backend.models.base import get_session
        from backend.models.project import DubbingProject, Role
        from modules.tts_engine import engine_registry

        vid = _make_voice(engine="edgetts", transcript=None)
        session = get_session()
        try:
            proj = DubbingProject(id=str(uuid.uuid4()), name="L14-noseed",
                                  mode="single", status="draft",
                                  global_settings={}, extra_data={})
            session.add(proj)
            session.add(Role(id=str(uuid.uuid4()), project_id=proj.id,
                                    name="旁白", role_type="narrator",
                                    voice_id=vid, extra_data={}))
            session.commit()
            pid = proj.id
        finally:
            session.close()

        fake = _SeedEngine(supports_seed=False)
        monkeypatch.setattr(engine_registry.engine_registry, "get_loaded",
                            lambda name=None, *a, **k: fake)
        client.app.include_router(dubbing_api.router, prefix="/api/dubbing")
        resp = client.post(f"/api/dubbing/projects/{pid}/preview")
        assert resp.status_code == 200, resp.text
        assert "seed" not in fake.calls[0]["kw"]
        assert _row_seed(vid) is None
        assert "X-Last-Successful-Seed" not in resp.headers

    def test_cast_preview_wires_the_same_producer(self, client, monkeypatch,
                                                  temp_db):
        from backend.api import cast as cast_api
        from backend.models.base import get_session
        from backend.models.project import DubbingProject, Role
        from modules.tts_engine import engine_registry

        vid = _make_voice(engine="voxcpm", transcript="有转写的克隆音色")
        session = get_session()
        try:
            proj = DubbingProject(id=str(uuid.uuid4()), name="L14-cast",
                                  mode="multi", status="ready",
                                  global_settings={},
                                  extra_data={"cast": {"analyzed": True}})
            session.add(proj)
            session.add(Role(id=str(uuid.uuid4()), project_id=proj.id,
                                    name="甲", role_type="hero", voice_id=vid,
                                    extra_data={}))
            session.commit()
            pid = proj.id
        finally:
            session.close()

        fake = _SeedEngine(supports_seed=True)
        monkeypatch.setattr(engine_registry.engine_registry, "is_loaded",
                            lambda name: True)
        monkeypatch.setattr(engine_registry.engine_registry, "get_loaded",
                            lambda name=None, *a, **k: fake)
        client.app.include_router(cast_api.router, prefix="/api")
        resp = client.post(f"/api/dubbing/projects/{pid}/cast/preview",
                           json={"text": "角色试听样本。", "max_sentences": 1})
        assert resp.status_code == 200, resp.text
        assert fake.calls and "seed" in fake.calls[0]["kw"]
        assert _row_seed(vid) == fake.calls[0]["kw"]["seed"]
        assert resp.headers.get("X-Last-Successful-Seed") == str(_row_seed(vid))
