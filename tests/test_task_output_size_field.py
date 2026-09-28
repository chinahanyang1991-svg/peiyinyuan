# -*- coding: utf-8 -*-
"""作品库体积失真的后端出口：任务字典必须给**真实字节数**，不给估算。

定罪的两处展示（#147）：
  ① 卡片与详情把"字数×系数"的估算当成品体积展示（连标签都不带"预估"）；
  ② 没解析过正文的任务显示"0 字 · 0 B"，读起来像"产出了一部零个字的书"。
②是纯展示问题（前端 `sizeText`/`charText` 的 `"—"` 兜底），①需要后端先有真值可读：
`ConversionTask.to_dict()` 此前只给 `output_path`，体积由前端猜。

隔离约定与 tests/test_l14_api_contract.py 同一手法：`get_db_path` 指向 tmp_path 下的
临时 sqlite，绝不碰生产库；产物文件写在 tmp_path 里；不打网络。
"""
import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


@pytest.fixture()
def temp_db(tmp_path, monkeypatch):
    import backend.models.base as base

    db = tmp_path / "size.db"
    monkeypatch.setattr(base, "get_db_path", lambda: str(db))
    monkeypatch.setattr(base, "_engine", None, raising=False)
    monkeypatch.setattr(base, "_SessionLocal", None, raising=False)
    base.get_session().close()          # 触发建表
    return str(db)


@pytest.fixture()
def client(temp_db):
    """不加 `with` => 跳过 lifespan 预热（不加载真引擎、不重启任何服务）。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from backend.api import tasks as tasks_api

    app = FastAPI()
    app.include_router(tasks_api.router, prefix="/api/tasks", tags=["tasks"])
    return TestClient(app)


def _make_task(output_path, total_chars=0):
    from backend.models.base import get_session
    from backend.models.task import ConversionTask

    session = get_session()
    try:
        task = ConversionTask(
            id=str(uuid.uuid4()), status="completed", progress=1.0,
            output_format="m4b", output_path=output_path,
            total_chars=total_chars, settings={})
        session.add(task)
        session.commit()
        return task.id
    finally:
        session.close()


# ---------------------------------------------------------------- 1) 模型出口
class TestToDictExposesRealSize:
    def _dict(self, path):
        from backend.models.task import ConversionTask
        return ConversionTask(id="t-1", status="completed", output_format="m4b",
                              output_path=path, settings={}).to_dict()

    def test_existing_file_reports_exact_bytes(self, tmp_path):
        f = tmp_path / "book.m4b"
        f.write_bytes(b"x" * 12345)
        assert self._dict(str(f))["output_size_bytes"] == 12345

    def test_missing_file_reports_none_not_zero(self, tmp_path):
        """文件被移走 != 0 字节成品：0 会被前端当成"这本书 0 B"展示出去。"""
        g = self._dict(str(tmp_path / "gone.m4b"))
        assert g["output_size_bytes"] is None

    def test_no_path_reports_none(self):
        assert self._dict(None)["output_size_bytes"] is None

    def test_unreadable_path_does_not_raise(self, tmp_path, monkeypatch):
        """stat 失败（权限/竞态）不许把整个列表接口带崩。"""
        import backend.models.task as task_mod

        def boom(_p):
            raise OSError("denied")
        monkeypatch.setattr(task_mod.os.path, "getsize", boom)
        assert self._dict("C:/whatever.m4b")["output_size_bytes"] is None


# ---------------------------------------------------------------- 2) HTTP 契约
class TestTasksApiCarryTheField:
    def test_detail_and_list_both_carry_the_size(self, client, tmp_path):
        f = tmp_path / "out.m4b"
        f.write_bytes(b"y" * 4096)
        tid = _make_task(str(f), total_chars=100)
        detail = client.get(f"/api/tasks/{tid}").json()["task"]
        assert detail["output_size_bytes"] == 4096
        listed = client.get("/api/tasks/").json()
        rows = listed["tasks"] if isinstance(listed, dict) else listed
        row = next(r for r in rows if r["id"] == tid)
        assert row["output_size_bytes"] == 4096

    def test_task_without_artifact_says_so_explicitly(self, client):
        tid = _make_task(None)
        assert client.get(f"/api/tasks/{tid}").json()["task"]["output_size_bytes"] is None


# ---------------------------------------------------------------- 3) 口径一致
def test_size_uses_the_same_path_authority_as_the_download_endpoint():
    """下载门判 `os.path.exists(output_path)`，体积也必须按同一条路径口径 stat。

    两条口径分叉时会出现"下载 404 但库里显示 12 MB"—— 正是本次要消灭的那类失真。
    """
    src = open(os.path.join(os.path.dirname(__file__), "..",
                            "backend", "api", "tasks.py"), encoding="utf-8").read()
    assert "os.path.exists(output_path)" in src
    msrc = open(os.path.join(os.path.dirname(__file__), "..",
                             "backend", "models", "task.py"), encoding="utf-8").read()
    assert 'os.path.getsize(str(self.output_path))' in msrc
