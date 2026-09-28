# -*- coding: utf-8 -*-
"""PY-365 回归锁：删任务不碰产物（红线）+ 孤儿产物"看得见(甲)/对账门(丙)"。

定罪现场（2026-09-27，读自盘上原文）：
- `backend/services/task_service.py:525-536` 的 `delete_task()` 只删库行，
  从不看 `task.output_path` —— 产物文件永远留在 `data/output/` 占空间，界面上
  再也点不到它（实测 33 枚孤儿 / 170,192,864 字节）；
- 反方向同样没有门：库里 completed 行的文件可能已被移走，行与文件之间零对账。

本文件钉死五件事（顺序与修复判据一致）：
  ① 孤儿识别精确到文件名集合：含 `<书名>_<uuid>.<ext>` 正则抓书名；
     **别的任务行还引用着的路径不许算孤儿**；
  ② 盘点 `orphan_total_bytes` 必须 = 逐枚 `os.path.getsize` 累加；
  ③ prune 三重门：缺 confirm → 拒；路径不在孤儿清单/越出 output 目录（`../`、
     指向目录外的绝对路径）→ 拒且**一个都不删**；合法清单 → 删成功，且
     **删前**落盘的删单（每条路径+字节）逐条对得上（红线②：删了什么都要有账）；
  ④ 文件被移走 → 既有详情端点 `output_size_bytes` 为 None（`1aa0c4f` 的口径），
     新端点 `/{id}/output-state` 给同源可判定的 `output_missing: true`；
  ⑤ `delete_task()` 行为**不变**：删行之后文件仍在盘上 —— 这枚是防回归的门，
     证明我们没有偷偷改成级联删（用户红线：数据不丢）。

隔离手法与 `tests/test_task_output_size_field.py` / `tests/test_py359_task_resume_gate.py`
同一先例：`get_db_path` 指向 tmp_path 临时 sqlite，`Config.OUTPUT_DIR`/`DATA_DIR`
重定向到 tmp_path，绝不碰生产库、生产 output 目录、:8000 端口。
"""
from __future__ import annotations

import json
import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture()
def temp_db(tmp_path, monkeypatch):
    import backend.models.base as base

    db = tmp_path / "p365.db"
    monkeypatch.setattr(base, "get_db_path", lambda: str(db))
    monkeypatch.setattr(base, "_engine", None, raising=False)
    monkeypatch.setattr(base, "_SessionLocal", None, raising=False)
    base.get_session().close()
    return db


@pytest.fixture()
def env(tmp_path, monkeypatch, temp_db):
    """临时 output 目录 + 只挂 tasks 路由的 TestClient（不跑 lifespan，不加载引擎）。"""
    from types import SimpleNamespace
    from backend.config import Config

    data_dir = tmp_path / "data"
    out_dir = data_dir / "output"
    out_dir.mkdir(parents=True)
    (out_dir / ".gitkeep").write_bytes(b"")          # 仓库既有占位文件，不是孤儿
    monkeypatch.setattr(Config, "DATA_DIR", data_dir, raising=False)
    monkeypatch.setattr(Config, "OUTPUT_DIR", out_dir, raising=False)

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from backend.api import tasks as tasks_api

    app = FastAPI()
    app.include_router(tasks_api.router, prefix="/api/tasks", tags=["tasks"])
    return SimpleNamespace(out=out_dir, data=data_dir, client=TestClient(app))


def _make_task(output_path=None, status="completed", book_id=None):
    from backend.models.base import get_session
    from backend.models.task import ConversionTask

    session = get_session()
    try:
        task = ConversionTask(
            id=str(uuid.uuid4()), status=status, progress=1.0,
            output_format="m4b", output_path=output_path, book_id=book_id,
            settings={})
        session.add(task)
        session.commit()
        return task.id
    finally:
        session.close()


def _artifact(out_dir, name, size):
    """在临时 output 里造一枚指定字节数的产物文件。"""
    p = out_dir / name
    p.write_bytes(b"x" * size)
    return str(p)


def _tid_uuid():
    return str(uuid.uuid4())


# ==================================================== ① 孤儿识别（精确到名字集合）
class TestOrphanIdentification:

    def _inventory(self, env):
        resp = env.client.get("/api/tasks/orphan-files")
        assert resp.status_code == 200, resp.text
        return resp.json()

    def test_deleted_task_file_is_the_only_orphan(self, env):
        from backend.services.task_service import task_service

        a = _artifact(env.out, f"张三的书_{_tid_uuid()}.m4b", 100)
        b = _artifact(env.out, f"另一本_{_tid_uuid()}.mp3", 200)
        _make_task(a)
        tid_b = _make_task(b)
        task_service.delete_task(tid_b)

        inv = self._inventory(env)
        names = {o["name"] for o in inv["orphans"]}
        assert names == {os.path.basename(b)}, \
            f"孤儿集合必须精确到文件名：{names}"
        assert inv["referenced_count"] == 1
        # .gitkeep（隐藏文件）与仍被引用的产物都不许进孤儿清单
        assert ".gitkeep" not in names
        assert os.path.basename(a) not in names

    def test_book_title_extracted_from_uuid_filename(self, env):
        """无人引用的 `<书名>_<uuid>.<ext>`：孤儿清单要能从文件名还原书名。"""
        _artifact(env.out, f"三体_死神永生_{_tid_uuid()}.m4b", 10)
        inv = self._inventory(env)
        assert inv["orphans"][0]["book_title"] == "三体_死神永生", inv["orphans"]

    def test_shared_path_referenced_by_another_row_is_not_orphan(self, env):
        """两行引用同一文件，删掉一行 —— 另一行还引用着，不许算孤儿。"""
        from backend.services.task_service import task_service

        path = _artifact(env.out, f"共享_{_tid_uuid()}.m4b", 42)
        t1 = _make_task(path)
        _make_task(path)
        task_service.delete_task(t1)
        inv = self._inventory(env)
        assert inv["orphans"] == [], "另一任务还引用同一路径，不得判孤儿"

    def test_non_matching_name_has_no_book_title(self, env):
        _artifact(env.out, "随手拖进来的录音.wav", 8)
        inv = self._inventory(env)
        assert inv["orphans"][0]["book_title"] is None


# ==================================================== ② 盘点字节 = 逐枚 getsize 累加
class TestInventoryByteAccounting:

    def test_total_bytes_equals_getsize_sum(self, env):
        sizes = [111, 2222, 33333]
        for i, s in enumerate(sizes):
            _artifact(env.out, f"孤儿{i}_{_tid_uuid()}.m4b", s)
        (env.out / ".hidden").write_bytes(b"1234567")   # 隐藏件不进清单（.gitkeep 同理）
        resp = env.client.get("/api/tasks/orphan-files")
        inv = resp.json()
        per_file = sum(o["bytes"] for o in inv["orphans"])
        disk_sum = sum(
            os.path.getsize(os.path.join(env.out, n))
            for n in os.listdir(env.out) if not n.startswith(".")
        )
        assert inv["orphan_total_bytes"] == per_file == disk_sum == sum(sizes)


# ==================================================== ③ prune 三重门 + 删前落盘删单
class TestPruneGates:

    def _one_orphan(self, env):
        path = _artifact(env.out, f"待清理_{_tid_uuid()}.m4b", 4096)
        return path

    def test_prune_without_confirm_is_refused_and_deletes_nothing(self, env):
        path = self._one_orphan(env)
        resp = env.client.post("/api/tasks/orphan-files/prune",
                               json={"paths": [path]})
        assert resp.status_code == 400, resp.text
        assert os.path.exists(path), "缺 confirm 也必须一个都不删"
        resp2 = env.client.post("/api/tasks/orphan-files/prune",
                                json={"confirm": False, "paths": [path]})
        assert resp2.status_code == 400
        assert os.path.exists(path)

    def test_prune_path_not_in_inventory_is_refused(self, env):
        # 仍被任务行引用的文件不在孤儿清单里 → 拒绝
        keep = _artifact(env.out, f"还有人引用_{_tid_uuid()}.m4b", 512)
        _make_task(keep)
        resp = env.client.post("/api/tasks/orphan-files/prune",
                               json={"confirm": True, "paths": [keep]})
        assert resp.status_code == 400, resp.text
        assert os.path.exists(keep)

    @pytest.mark.parametrize("evil", ["../outside.bin", "..\\outside.bin"])
    def test_prune_directory_traversal_is_refused(self, env, evil):
        secret = env.data / "outside.bin"          # output 目录之外的用户文件
        secret.write_bytes(b"do not touch me")
        self._one_orphan(env)
        resp = env.client.post("/api/tasks/orphan-files/prune",
                               json={"confirm": True, "paths": [str(env.out / evil)]})
        assert resp.status_code == 400, f"目录穿越没拦住: {resp.text}"
        assert secret.exists(), "越界路径必须整体拒绝且不删任何东西"
        assert len(list(env.out.iterdir())) > 1    # 合法孤儿也还在（原子拒绝）

    def test_prune_absolute_path_outside_output_is_refused(self, env):
        elsewhere = _artifact(env.data, f"库外文件_{_tid_uuid()}.m4b", 64)
        self._one_orphan(env)
        resp = env.client.post("/api/tasks/orphan-files/prune",
                               json={"confirm": True, "paths": [elsewhere]})
        assert resp.status_code == 400, resp.text
        assert os.path.exists(elsewhere)

    def test_containment_gate_holds_even_against_tampered_inventory(self, env, monkeypatch):
        """纵深防御：落点校验是独立于"清单成员"的第二层 —— 就算清单被篡改/竞态
        混进一条 output 之外的路径，前缀门也必须拦住（M1 变异钉死这一层）。"""
        from backend.services import output_reconcile as orc

        sneaky = env.data / "sneaky.m4b"           # output 目录之外
        sneaky.write_bytes(b"must survive")
        monkeypatch.setattr(orc, "build_inventory", lambda d=None: {
            "orphans": [{"name": "sneaky.m4b", "path": str(sneaky), "bytes": 13,
                         "mtime": "", "book_title": None}]})
        resp = env.client.post("/api/tasks/orphan-files/prune",
                               json={"confirm": True, "paths": [str(sneaky)]})
        assert resp.status_code == 400, f"越出 output 的路径必须被前缀门拦住: {resp.text}"
        assert sneaky.exists(), "前缀门被绕过时真的删了用户文件"

    def test_prune_legitimate_flow_writes_manifest_before_deleting(self, env, monkeypatch):
        p1 = _artifact(env.out, f"甲_{_tid_uuid()}.m4b", 1000)
        p2 = _artifact(env.out, f"乙_{_tid_uuid()}.m4b", 2000)
        keep = _artifact(env.out, f"丙_{_tid_uuid()}.m4b", 3000)
        _make_task(keep)
        sizes = {os.path.basename(p1): 1000, os.path.basename(p2): 2000}

        # 时序探针：每一次真删发生**前**，删单必须已经躺在盘上（"删前落盘"而不是补写）
        from backend.services import output_reconcile as orc
        mdir = env.data / "output_prune_manifests"
        manifest_present_at_each_removal = []
        real_remove = os.remove

        def spy_remove(path):
            manifest_present_at_each_removal.append(
                mdir.is_dir() and any(f.name.endswith(".json") for f in mdir.iterdir()))
            real_remove(path)

        monkeypatch.setattr(orc.os, "remove", spy_remove)
        resp = env.client.post("/api/tasks/orphan-files/prune",
                               json={"confirm": True, "paths": [p1, p2]})
        assert resp.status_code == 200, resp.text
        assert manifest_present_at_each_removal and all(manifest_present_at_each_removal), \
            "删单必须在删除动作之前落盘（红线②：先记账再动手）"
        body = resp.json()
        assert not os.path.exists(p1) and not os.path.exists(p2)
        assert os.path.exists(keep), "被引用文件绝不许被动到"
        assert body["freed_bytes"] == 3000

        manifest_path = body["manifest_path"]
        assert os.path.exists(manifest_path), "删单必须落盘（红线：删了什么要有账）"
        with open(manifest_path, encoding="utf-8") as fh:
            manifest = json.load(fh)
        entries = manifest["entries"]
        assert {os.path.basename(e["path"]) for e in entries} == set(sizes)
        for e in entries:                       # 逐条路径+字节对得上
            assert e["bytes"] == sizes[os.path.basename(e["path"])]


# ==================================================== ④ 行在文件不在 → output_missing
class TestOutputMissingFlag:

    def test_moved_file_reports_missing_on_same_authority(self, env):
        path = _artifact(env.out, f"被移走_{_tid_uuid()}.m4b", 700)
        tid = _make_task(path)
        # 详情端点既有口径（1aa0c4f）：stat 失败 = None
        assert env.client.get(f"/api/tasks/{tid}").json()["task"]["output_size_bytes"] == 700
        os.remove(path)
        detail = env.client.get(f"/api/tasks/{tid}").json()["task"]
        assert detail["output_size_bytes"] is None
        st = env.client.get(f"/api/tasks/{tid}/output-state")
        assert st.status_code == 200, st.text
        body = st.json()
        assert body["output_missing"] is True
        assert body["output_size_bytes"] is None, "必须与 output_size_bytes 同源，不造第二套真值"

    def test_existing_file_is_not_missing(self, env):
        path = _artifact(env.out, f"健在_{_tid_uuid()}.m4b", 9)
        tid = _make_task(path)
        body = env.client.get(f"/api/tasks/{tid}/output-state").json()
        assert body["output_missing"] is False and body["output_size_bytes"] == 9

    def test_analysis_task_without_path_is_not_missing(self, env):
        """cast_analysis 类任务本就不产音频：output_path 为空 ≠ 产物丢失。"""
        tid = _make_task(None)
        body = env.client.get(f"/api/tasks/{tid}/output-state").json()
        assert body["output_missing"] is False


# ==================================================== ⑤ delete_task 行为不变（防级联删）
class TestDeleteTaskNeverTouchesFiles:

    def test_service_delete_keeps_the_file_on_disk(self, env):
        from backend.services.task_service import task_service

        path = _artifact(env.out, f"成品_{_tid_uuid()}.m4b", 4096)
        tid = _make_task(path)
        assert task_service.delete_task(tid) is True
        assert os.path.exists(path), "红线：delete_task 绝不允许级联删产物文件"
        assert os.path.getsize(path) == 4096
        assert task_service.get_task(tid) is None

    def test_api_delete_keeps_the_file_and_it_shows_as_orphan(self, env):
        path = _artifact(env.out, f"接口删_{_tid_uuid()}.m4b", 2048)
        tid = _make_task(path)
        resp = env.client.delete(f"/api/tasks/{tid}")
        assert resp.status_code == 200, resp.text
        assert os.path.exists(path), "红线：DELETE 端点同样不许动文件"
        inv = env.client.get("/api/tasks/orphan-files").json()
        assert {o["name"] for o in inv["orphans"]} == {os.path.basename(path)}


# ==================================================== 丙：启动对账函数（只报不删）
class TestReconcileAtStartupDeletesNothing:

    def test_reconcile_reports_counts_and_keeps_every_file(self, env):
        from backend.services.output_reconcile import reconcile_output_dir

        orphan = _artifact(env.out, f"孤儿_{_tid_uuid()}.m4b", 123)
        gone = _artifact(env.out, f"将被移走_{_tid_uuid()}.m4b", 45)
        _make_task(gone)
        os.remove(gone)

        summary = reconcile_output_dir()
        assert summary["orphan_count"] == 1
        assert summary["orphan_total_bytes"] == 123
        assert summary["missing_output_count"] == 1
        assert os.path.exists(orphan), "启动对账绝不删任何东西"
