# -*- coding: utf-8 -*-
"""
WFFIX-3 回归测试：WF-REPORT 第②节 8 条 API/服务层缺陷。

  WF-04 解析失败 500 + 泄漏存储 UUID   -> TestWF04*
  WF-05 /api/voices/statistics 被路由抢 -> TestWF05*
  WF-06 BGM 库只增不减（无删除端点）    -> TestWF06*
  WF-07/09 书籍列表 N+1、整章正文下发   -> TestWF09*
  WF-10 删书留下悬空配音项目            -> TestWF10*
  WF-11 项目设置无值域校验              -> TestWF11*
  WF-12 音色列表无分页且 total 说谎     -> TestWF12*
  WF-16 不存在书籍的章节返回 200 空数组 -> TestWF16*

隔离约定（与 tests/test_wf_fix4.py 同一手法）：
  * 不碰生产库：backend.models.base.get_db_path 指向 tmp_path 下的临时 sqlite；
  * 不碰仓库 data/：Config.DATA_DIR / VOICE_DIR / OUTPUT_DIR 重定向到 tmp_path；
  * 不碰 data/bgm 真库：modules.mixer.BgmStore 换成 root=tmp_path 的子类；
  * 不加载真引擎、不打网络（TestClient 不加 with -> 跳过 lifespan 预热）；
  * 控制台仅 ASCII（Windows GBK 安全），断言消息里的中文由 pytest 自行转码。
"""
import re
import sys
import uuid
import wave
import struct
import math
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
                     r"[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)

# 一本可解析、可过清洗门限的最小样书（两章，正文 > 20 字）
BOOK_TXT = (
    "第一章 测试开场。\n\n"
    "这是一本用于 WFFIX-3 回归的样书，正文足够长以便通过解析器的最小长度门限。"
    "主角说：“你好。”旁白继续叙述了一段足以被清洗器保留的文字内容。\n\n"
    "第二章 测试收尾。\n\n"
    "第二段正文同样超过二十个字符，确保章节检测器至少产出两个可读章节用于后续断言。"
)


class _Env:
    """测试沙箱：临时库 + 临时数据目录 + 临时 BGM 曲库 + TestClient。"""

    def __init__(self, client, engine, app, data_dir, bgm_root):
        self.client = client
        self.engine = engine
        self.app = app
        self.data_dir = data_dir
        self.bgm_root = bgm_root
        self.stmts = []
        self._attach()

    # ---- SQL 计数（WF-09 N+1 判据） ----
    def _attach(self):
        from sqlalchemy import event

        stmts = self.stmts

        def _collect(conn, cursor, statement, params, context, many):
            stmts.append(statement)

        event.listen(self.engine, "before_cursor_execute", _collect)
        self._listener = _collect

    def detach(self):
        from sqlalchemy import event
        try:
            event.remove(self.engine, "before_cursor_execute", self._listener)
        except Exception:
            pass

    def reset(self):
        self.stmts.clear()

    def count_like(self, *needles):
        return sum(1 for s in self.stmts
                   if all(n.lower() in s.lower() for n in needles))

    # ---- 会话 ----
    def session(self):
        from backend.models.base import get_session
        return get_session()

    def query(self, model):
        s = self.session()
        try:
            return s.query(model).all()
        finally:
            s.close()

    def one(self, model, pk):
        s = self.session()
        try:
            return s.get(model, pk)
        finally:
            s.close()

    def to_dict(self, model, pk):
        """在会话内序列化（to_dict 会碰懒加载关系，脱离会话调用会炸）。"""
        s = self.session()
        try:
            obj = s.get(model, pk)
            return obj.to_dict() if obj else None
        finally:
            s.close()

    # ---- 便捷 API ----
    def upload(self, filename, content: bytes, path="/api/books/import"):
        return self.client.post(path,
                                files={"file": (filename, content, "text/plain")})

    def seed_book(self, title, chapters=3, chars=900, with_chapters=True):
        """直接落库造书（避开解析器，只为造 N+1 场景）。"""
        from backend.models.book import Book, Chapter
        book_id = str(uuid.uuid4())
        body = ("甲段正文。" * (chars // 5 + 1))[:chars]
        s = self.session()
        try:
            s.add(Book(id=book_id, title=title, author="WF", format="txt",
                       file_path="", cover_path="", total_chars=chars * chapters,
                       language="zh"))
            if with_chapters:
                for i in range(chapters):
                    s.add(Chapter(id=str(uuid.uuid4()), book_id=book_id,
                                  title="第%d章" % (i + 1), index=i,
                                  content=body, char_count=len(body)))
            s.commit()
        finally:
            s.close()
        return book_id


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """整套隔离环境；monkeypatch 自动还原全局引擎/配置，不污染同库其它用例。"""
    import backend.models.base as base
    from backend.config import Config

    data_dir = tmp_path / "data"
    (data_dir / "books").mkdir(parents=True)
    bgm_root = tmp_path / "bgmlib"
    bgm_root.mkdir(parents=True)
    db = tmp_path / "wffix3.db"

    monkeypatch.setattr(base, "get_db_path", lambda: str(db))
    monkeypatch.setattr(base, "_engine", None, raising=False)
    monkeypatch.setattr(base, "_SessionLocal", None, raising=False)
    monkeypatch.setattr(Config, "DATA_DIR", data_dir, raising=False)
    monkeypatch.setattr(Config, "VOICE_DIR", data_dir / "voices", raising=False)
    monkeypatch.setattr(Config, "OUTPUT_DIR", data_dir / "output", raising=False)

    # 曲库根目录隔离（BgmStore 在端点函数内 import，故 patch 包属性即可生效）
    import modules.mixer as mixer_pkg
    from modules.mixer import BgmStore as _RealBgmStore

    class _ScratchBgmStore(_RealBgmStore):
        def __init__(self, root=None):
            super().__init__(root=bgm_root)

    monkeypatch.setattr(mixer_pkg, "BgmStore", _ScratchBgmStore)

    from fastapi.testclient import TestClient
    from backend.main import app

    base.get_session().close()          # 建表 + 内置/Edge 音色种子
    client = TestClient(app, raise_server_exceptions=False)
    sandbox = _Env(client, base.get_engine(), app, data_dir, bgm_root)
    try:
        yield sandbox
    finally:
        sandbox.detach()


# =====================================================================
# WF-04 解析失败：400 + 回显用户上传文件名（不泄漏存储 UUID）
# =====================================================================
class TestWF04ParseFailure:

    @pytest.mark.parametrize("filename,content", [
        ("WF-04 空样本.txt", b""),
        ("WF-04 损坏书.epub", b"this is definitely not a zip/epub payload"),
        ("WF-04 只有空白.txt", "     \n\t  \n".encode("utf-8")),
    ])
    def test_parse_failure_is_400_and_echoes_upload_name(self, env, filename, content):
        resp = env.upload(filename, content)

        assert resp.status_code == 400, "用户改文件即可解决的问题不该报 500: %s" % resp.text
        detail = resp.json()["detail"]
        assert UUID_RE.search(detail) is None, "detail 泄漏存储 UUID: %s" % detail
        assert Path(filename).stem in detail, "detail 未回显上传文件名: %s" % detail

        # 失败导入不留残渣：既没入库也没留下命名文件
        from backend.models.book import Book
        assert env.query(Book) == []
        assert list((env.data_dir / "books").iterdir()) == []

    def test_error_class_is_a_valueerror_subclass(self):
        """路由只有 except ValueError -> 400 分支，异常类型即契约。"""
        from backend.services.book_service import BookImportError, BookParseUserError

        assert issubclass(BookParseUserError, BookImportError)
        assert issubclass(BookImportError, ValueError)
        err = BookParseUserError("坏了", original_filename="我的书.txt")
        assert err.original_filename == "我的书.txt"

    def test_storage_uuid_scrub_helper(self):
        from backend.services.book_service import _scrub_storage_names
        from pathlib import Path

        stored = Path(r"C:\x\data\books\aeb14b12-aeb4-49a4-9883-555361036709.epub")
        msg = _scrub_storage_names("无法解析 %s" % stored, stored, "用户上传.epub")
        assert UUID_RE.search(msg) is None
        assert "用户上传.epub" in msg

    def test_unsupported_extension_still_400_and_ok_import_still_200(self, env):
        bad = env.upload("WF-04 不支持.md", b"# hi")
        assert bad.status_code == 400

        ok = env.upload("WF-04 正常书.txt", BOOK_TXT.encode("utf-8"))
        assert ok.status_code == 200, ok.text
        assert ok.json()["book"]["id"]
        assert UUID_RE.search(ok.json()["book"]["title"]) is None


# =====================================================================
# WF-05 /api/voices/statistics 被 /{voice_id} 抢先匹配
# =====================================================================
class TestWF05RouteOrder:

    def test_statistics_endpoint_returns_payload(self, env):
        resp = env.client.get("/api/voices/statistics")
        assert resp.status_code == 200, resp.text
        stats = resp.json()["statistics"]
        assert "total" in stats and "by_source" in stats

    def test_static_path_declared_before_path_param(self, env):
        """能被 /{voice_id} 吞掉的，只有"同一 GET 方法的单一静态段"路由。"""
        paths, methods = [], []
        for r in env.app.routes:
            paths.append(getattr(r, "path", ""))
            methods.append(set(getattr(r, "methods", None) or []))
        param = paths.index("/api/voices/{voice_id}")

        def single_static(i):
            return bool(re.fullmatch(r"/api/voices/[a-z_]+", paths[i])) and "GET" in methods[i]

        statics = [paths[i] for i in range(param) if single_static(i)]
        assert "/api/voices/statistics" in statics, sorted(statics)
        assert "/api/voices/search" in statics, sorted(statics)
        after = [paths[i] for i in range(param + 1, len(paths)) if single_static(i)]
        assert after == [], "这些单段静态 GET 路由会永远被 /{voice_id} 抢占: %s" % after

    def test_voice_detail_still_404s(self, env):
        resp = env.client.get("/api/voices/no-such-voice-id")
        assert resp.status_code == 404


# =====================================================================
# WF-06 BGM 库无删除端点
# =====================================================================
class TestWF06BgmDelete:

    def _store(self, env):
        import modules.mixer as mixer_pkg
        store = mixer_pkg.BgmStore(root=env.bgm_root)
        return store

    def _seed_library(self, env):
        import numpy as np
        import soundfile as sf
        from modules.mixer import LIBRARY_SR

        store = self._store(env)
        store.ensure_builtin()
        src = env.bgm_root / "upload_src.wav"
        n = int(LIBRARY_SR * 0.25)
        t = np.arange(n, dtype=np.float32) / LIBRARY_SR
        tone = (np.sin(2 * math.pi * 220 * t) * 0.3).astype(np.float32)
        sf.write(str(src), np.stack([tone, tone], axis=1), LIBRARY_SR)
        record = store.add_audio_file(src, name="WF-06 回归素材", moods=["测试"])
        return store, record

    def test_delete_route_registered(self, env):
        routes = {(getattr(r, "path", ""), tuple(sorted(getattr(r, "methods", []) or [])))
                  for r in env.app.routes}
        assert ("/api/bgm/{bgm_id}", ("DELETE",)) in routes, sorted(routes)

    def test_uploaded_asset_deletable(self, env):
        store, record = self._seed_library(env)
        bgm_id = record["id"]
        listed = {b["id"] for b in env.client.get("/api/bgm").json()["bgm"]}
        assert bgm_id in listed

        resp = env.client.delete("/api/bgm/%s" % bgm_id)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["deleted"] == bgm_id and body["kind"] == "bgm"
        assert body["file_removed"] is True, body

        # 记录 + 文件都消失，预览 404
        assert bgm_id not in {b["id"] for b in env.client.get("/api/bgm").json()["bgm"]}
        assert not Path(body["file_path"]).exists()
        assert store.load_meta() and all(
            r.get("id") != bgm_id for r in store.load_meta()["bgm"])
        assert env.client.get("/api/bgm/%s/preview" % bgm_id).status_code == 404

    def test_builtin_placeholder_refused_403(self, env):
        store, _ = self._seed_library(env)
        resp = env.client.delete("/api/bgm/bgm_warm_pad")
        assert resp.status_code == 403, resp.text
        assert "内置" in resp.json()["detail"]
        # 占位素材仍在库里（ensure_builtin 每次列表都会重建）
        assert "bgm_warm_pad" in {b["id"] for b in env.client.get("/api/bgm").json()["bgm"]}
        assert "bgm_warm_pad" in {r["id"] for r in store.load_meta()["bgm"]}

    def test_unknown_id_404(self, env):
        self._seed_library(env)
        assert env.client.delete("/api/bgm/bgm_never_exists").status_code == 404

    def test_sfx_bucket_deletable(self, env):
        """sfx 桶同样可删（用户上传的音效）。"""
        store, record = self._seed_library(env)
        sfx = store.add_audio_file(env.bgm_root / "upload_src.wav",
                                   name="WF-06 回归音效", kind="sfx")
        resp = env.client.delete("/api/bgm/%s" % sfx["id"])
        assert resp.status_code == 200, resp.text
        assert resp.json()["kind"] == "sfx"
        assert sfx["id"] not in {s["id"] for s in env.client.get("/api/bgm").json()["sfx"]}
        assert record["id"] in {b["id"] for b in env.client.get("/api/bgm").json()["bgm"]}


# =====================================================================
# WF-09 书籍列表 N+1 + 章节整段正文
# =====================================================================
class TestWF09BookListNPlusOne:

    def test_book_list_uses_group_by_not_per_book_lazy_load(self, env):
        for i in range(12):
            env.seed_book("WF-09 书 %02d" % i, chapters=3)

        assert env.client.get("/api/books/?limit=50").status_code == 200  # 预热连接
        env.reset()
        resp = env.client.get("/api/books/?limit=50")
        assert resp.status_code == 200, resp.text
        books = resp.json()["books"]
        assert len(books) == 12

        # 1 条书列表 + 1 条 GROUP BY 章节计数 + 1 条 COUNT(*) 总数；旧实现是 1 + 12 条 chapters SELECT
        # L14③/WF-23 起 total 走 COUNT(*)（与 voice_store WF-12 同一分页口径），
        # 因此 "FROM books" 合理地从 1 条变 2 条（列表 + 计数）——本测试守的是
        # "不得逐书查询"，不是"不得有多余一条聚合"。
        chapters_sql = env.count_like("FROM chapters")
        books_sql = env.count_like("FROM books")
        assert books_sql == 2, "书列表应只有 列表+COUNT(*) 两条: %s" % env.stmts
        assert chapters_sql == 1, "章节计数仍是逐书查询: %s" % env.stmts
        assert len(env.stmts) <= 3, env.stmts

    def test_book_list_field_names_unchanged_and_counts_correct(self, env):
        from backend.models.book import Book

        book_id = env.seed_book("WF-09 字段契约", chapters=4)
        item = env.client.get("/api/books/?limit=50").json()["books"][0]
        reference = env.to_dict(Book, book_id)

        assert sorted(item.keys()) == sorted(reference.keys()), \
            "列表序列化改动了字段名: %s vs %s" % (sorted(item), sorted(reference))
        assert item["chapter_count"] == 4
        assert item["id"] == book_id

    def test_chapter_list_ships_preview_only(self, env):
        from backend.models.book import Chapter, PREVIEW_CHARS

        book_id = env.seed_book("WF-09 长正文", chapters=2, chars=900)
        marker = "MARKER-ONLY-IN-FULL-BODY-" + uuid.uuid4().hex
        s = env.session()
        try:
            rows = s.query(Chapter).filter_by(book_id=book_id).order_by(
                Chapter.index).all()
            for ch in rows:
                ch.content = ("前段可读正文。" * 60) + marker  # 头 420 字 > 预览窗口
                ch.char_count = len(ch.content)
            s.commit()
            first_id = rows[0].id
        finally:
            s.close()

        env.reset()
        resp = env.client.get("/api/books/%s/chapters" % book_id)
        assert resp.status_code == 200, resp.text
        out = resp.json()["chapters"]
        assert out
        assert marker not in resp.text, "整段正文仍被下发"
        assert all(r["content"] is None for r in out)
        assert all(len(r["content_preview"] or "") <= PREVIEW_CHARS + 3 for r in out)
        assert sorted(out[0].keys()) == sorted(Chapter(
            id="x", book_id=book_id, title="t", index=0, content="c").to_dict().keys())
        assert len(env.stmts) <= 3, env.stmts

        # 需要全文时走懒加载端点（前端契约不变）
        full = env.client.get("/api/books/chapters/%s/content" % first_id)
        assert full.status_code == 200
        assert marker in full.json()["content"]

    def test_service_get_chapters_still_returns_full_content(self, env):
        """工作流 conversion_workflow 依赖整章正文，get_chapters 不能被瘦身。"""
        from backend.services.book_service import book_service

        book_id = env.seed_book("WF-09 全文依赖", chapters=2, chars=1200)
        rows = book_service.get_chapters(book_id)
        assert len(rows) == 2
        assert all(len(r["content"] or "") > 500 for r in rows)


# =====================================================================
# WF-10 删书后项目悬空
# =====================================================================
class TestWF10BookDeletionInvalidatesProjects:

    def _project_for(self, env, book_id, status="ready"):
        resp = env.client.post("/api/dubbing/projects",
                               json={"name": "WF-10 项目", "book_id": book_id,
                                     "mode": "single"})
        assert resp.status_code == 200, resp.text
        pid = resp.json()["project"]["id"]
        from backend.models.project import DubbingProject
        s = env.session()
        try:
            p = s.get(DubbingProject, pid)
            p.status = status
            s.commit()
        finally:
            s.close()
        return pid

    def test_delete_book_marks_projects_book_missing(self, env):
        from backend.models.project import DubbingProject, STATUS_BOOK_MISSING

        book_id = env.seed_book("WF-10 待删书", chapters=2)
        pid = self._project_for(env, book_id, status="ready")

        resp = env.client.delete("/api/books/%s" % book_id)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "ok" and body["deleted"] is True
        assert pid in body["projects_marked"]

        project = env.one(DubbingProject, pid)
        assert project.status == STATUS_BOOK_MISSING
        audit = (project.extra_data or {}).get("book_missing")
        assert audit and audit["previous_status"] == "ready"
        assert audit["book_id"] == book_id
        assert env.client.get("/api/books/%s" % book_id).status_code == 404
        # 项目本身仍可读（用户能看到并处置），不是被级联删掉
        assert env.client.get("/api/dubbing/projects/%s" % pid).status_code == 200

    def test_delete_book_without_projects_and_missing_book(self, env):
        book_id = env.seed_book("WF-10 无项目", chapters=1)
        body = env.client.delete("/api/books/%s" % book_id).json()
        assert body["deleted"] is True and body["projects_marked"] == []

        missing = env.client.delete("/api/books/%s" % uuid.uuid4()).json()
        assert missing["deleted"] is False and missing["projects_marked"] == []

    def test_status_constant_documented_in_model(self, env):
        from backend.models.project import PROJECT_STATUSES, STATUS_BOOK_MISSING

        assert STATUS_BOOK_MISSING == "book_missing"
        assert "book_missing" in PROJECT_STATUSES
        assert "draft" in PROJECT_STATUSES and "done" in PROJECT_STATUSES


# =====================================================================
# WF-11 项目设置值域校验
# =====================================================================
class TestWF11SettingsValidation:

    def _pid(self, env):
        r = env.client.post("/api/dubbing/projects",
                            json={"name": "WF-11 项目", "mode": "single"})
        return r.json()["project"]["id"]

    def _read_back(self, env, pid):
        return (env.client.get("/api/dubbing/projects/%s" % pid).json()
                .get("project") or {}).get("global_settings") or {}

    def test_out_of_range_values_are_clamped_and_persisted(self, env):
        pid = self._pid(env)
        resp = env.client.put("/api/dubbing/projects/%s/settings" % pid,
                              json={"settings": {"speed": 999, "pitch": -99}})
        assert resp.status_code == 200, resp.text
        stored = self._read_back(env, pid)
        assert stored["speed"] == pytest.approx(2.0)
        assert stored["pitch"] == pytest.approx(-12.0)

    def test_clamp_bounds_come_from_conditioning(self, env):
        from modules.voice_manager.conditioning import (MIN_SPEED, MAX_SPEED,
                                                        MIN_PITCH, MAX_PITCH)
        from backend.services.dubbing_service import GLOBAL_SETTING_SPECS

        assert GLOBAL_SETTING_SPECS["speed"]["min"] == MIN_SPEED
        assert GLOBAL_SETTING_SPECS["speed"]["max"] == MAX_SPEED
        assert GLOBAL_SETTING_SPECS["pitch"]["min"] == MIN_PITCH
        assert GLOBAL_SETTING_SPECS["pitch"]["max"] == MAX_PITCH

    @pytest.mark.parametrize("settings,keyword", [
        ({"bogus_key": {"a": 1}}, "未知设置项"),
        ({"speed": "很快"}, "需要数字"),
        ({"emotion": 123}, "需要字符串"),
        ({"emotion": ""}, "不能为空"),
        ({"engine": "x" * 99}, "过长"),
        ({}, "不能为空对象"),
    ])
    def test_illegal_settings_rejected_422(self, env, settings, keyword):
        pid = self._pid(env)
        before = self._read_back(env, pid)
        resp = env.client.put("/api/dubbing/projects/%s/settings" % pid,
                              json={"settings": settings})
        assert resp.status_code == 422, "%s -> %s %s" % (settings, resp.status_code, resp.text)
        assert keyword in resp.json()["detail"], resp.json()
        assert self._read_back(env, pid) == before, "非法请求必须一个字都不落库"

    @pytest.mark.parametrize("settings,keyword", [
        ({"speed": float("nan")}, "有限数字"),      # NaN 不能进 JSON，只能在服务层拦
        ({"pitch": float("inf")}, "有限数字"),
        ({"speed": True}, "需要数字"),
        ({"mix": "on"}, "需要对象"),
        (None, "必须是对象"),
    ])
    def test_illegal_settings_rejected_at_service_layer(self, settings, keyword):
        """NaN/inf/bool 这类"JSON 里到不了、脚本调用却会传"的脏值同样被拒。"""
        from backend.services.dubbing_service import sanitize_global_settings

        with pytest.raises(ValueError) as ei:
            sanitize_global_settings(settings)
        assert keyword in str(ei.value), str(ei.value)

    def test_valid_settings_round_trip(self, env):
        pid = self._pid(env)
        resp = env.client.put("/api/dubbing/projects/%s/settings" % pid,
                              json={"settings": {"speed": 1.25, "pitch": 2,
                                                 "emotion": "gentle"}})
        assert resp.status_code == 200, resp.text
        stored = self._read_back(env, pid)
        assert stored["speed"] == pytest.approx(1.25)
        assert stored["pitch"] == pytest.approx(2.0)
        assert stored["emotion"] == "gentle"

    def test_mix_key_delegated_to_mixer_validator(self, env):
        pid = self._pid(env)
        ok = env.client.put("/api/dubbing/projects/%s/settings" % pid,
                            json={"settings": {"mix": {"enabled": True,
                                                       "bgm": {"ref": "bgm_warm_pad"}}}})
        assert ok.status_code == 200, ok.text
        bad = env.client.put("/api/dubbing/projects/%s/settings" % pid,
                             json={"settings": {"mix": {"enabled": True,
                                                        "bgm": {"gain_db": 999}}}})
        assert bad.status_code == 422, bad.text

    def test_missing_project_still_404(self, env):
        resp = env.client.put("/api/dubbing/projects/nope/settings",
                              json={"settings": {"speed": 1.0}})
        assert resp.status_code == 404

    def test_service_validation_error_is_both_http_and_valueerror(self):
        from backend.services.dubbing_service import sanitize_global_settings
        from backend.services.dubbing_service import SettingsValidationError
        from fastapi import HTTPException

        clean, adj = sanitize_global_settings({"speed": 3.5})
        assert clean["speed"] == 2.0 and adj[0]["key"] == "speed"
        with pytest.raises(ValueError) as ei:          # 老调用方按 ValueError 兜
            sanitize_global_settings({"nope": 1})
        assert isinstance(ei.value, HTTPException)
        assert ei.value.status_code == 422
        assert issubclass(SettingsValidationError, (HTTPException, ValueError))

    def test_service_accepts_numeric_strings_and_reports_no_clamp(self):
        from backend.services.dubbing_service import sanitize_global_settings

        clean, adj = sanitize_global_settings({"speed": "1.5", "pitch": -3})
        assert clean == {"speed": 1.5, "pitch": -3.0}
        assert adj == [], "区间内的值不该被记成夹紧: %s" % adj


# =====================================================================
# WF-12 音色列表分页 + total
# =====================================================================
class TestWF12VoicePagination:

    def _seed_voices(self, env, n, source="sample"):
        from backend.models.voice import VoiceProfile
        s = env.session()
        try:
            for i in range(n):
                s.add(VoiceProfile(id=str(uuid.uuid4()), name="WF-12 音色 %03d" % i,
                                   source=source, engine="voxcpm", language="zh",
                                   gender="female", age_group="adult",
                                   style_tags=["测试"], description="回归用",
                                   category="human"))
            s.commit()
        finally:
            s.close()

    def _db_rows(self, env, **filters):
        from backend.models.voice import VoiceProfile
        s = env.session()
        try:
            return s.query(VoiceProfile).filter_by(**filters).count()
        finally:
            s.close()

    def test_total_is_sql_count_not_page_size(self, env):
        self._seed_voices(env, 30)
        total = self._db_rows(env)
        body = env.client.get("/api/voices/?limit=10").json()

        assert len(body["voices"]) == 10
        assert body["total"] == total != 10, "total 仍是本页条数 (WF-12)"
        assert body["returned"] == 10 and body["limit"] == 10 and body["offset"] == 0
        assert body["total"] == self._db_rows(env)

    def test_pages_are_disjoint_and_cover_everything(self, env):
        self._seed_voices(env, 35)
        seen, offset, guard = set(), 0, 0
        while guard < 20:
            body = env.client.get("/api/voices/?limit=10&offset=%d" % offset).json()
            page = {v["id"] for v in body["voices"]}
            assert len(page) == len(body["voices"]), "页内出现重复行 -> 排序缺稳定键"
            assert not (seen & page), "翻页与前一页重叠: %s" % (seen & page)
            seen |= page
            if not body["voices"]:
                break
            offset += 10
            guard += 1
        assert len(seen) == self._db_rows(env)

    def test_filtered_total_matches_filtered_rows(self, env):
        self._seed_voices(env, 12, source="sample")
        body = env.client.get("/api/voices/?source=sample&limit=5").json()
        assert body["total"] == 12, body["total"]
        assert len(body["voices"]) == 5
        assert all(v["source"] == "sample" for v in body["voices"])

    def test_pagination_is_two_sql_at_most(self, env):
        self._seed_voices(env, 8)
        env.client.get("/api/voices/?limit=5")            # 预热
        env.reset()
        env.client.get("/api/voices/?limit=5&offset=5&source=sample")
        assert env.count_like("FROM voice_profiles") <= 2, env.stmts
        assert env.count_like("count(") == 1, env.stmts

    def test_negative_offset_rejected_and_fields_compatible(self, env):
        self._seed_voices(env, 3)
        assert env.client.get("/api/voices/?limit=10&offset=-1").status_code == 422
        body = env.client.get("/api/voices/").json()
        assert {"status", "voices", "total"} <= set(body.keys())
        for key in ("id", "name", "source", "engine", "language"):
            assert key in body["voices"][0], key


# =====================================================================
# WF-16 不存在书籍的章节列表
# =====================================================================
class TestWF16Chapters404:

    def test_missing_book_returns_404(self, env):
        resp = env.client.get("/api/books/%s/chapters" % uuid.uuid4())
        assert resp.status_code == 404, resp.text
        assert resp.json()["detail"] == "书籍不存在"

    def test_book_without_chapters_returns_200_empty(self, env):
        book_id = env.seed_book("WF-16 零章节", chapters=0, with_chapters=False)
        resp = env.client.get("/api/books/%s/chapters" % book_id)
        assert resp.status_code == 200, resp.text
        assert resp.json()["chapters"] == [] and resp.json()["total"] == 0

    def test_book_with_chapters_returns_rows(self, env):
        book_id = env.seed_book("WF-16 有章节", chapters=3)
        body = env.client.get("/api/books/%s/chapters" % book_id).json()
        assert body["total"] == 3
