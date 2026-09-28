"""PY-359 回归锁：`POST /api/tasks/{id}/resume|pause` 的两道门。

现场证据（2026-09-24 巡检，全部读自盘上原文）：
- 41 条任务里 5 条 failed，其中 4 条 `error_message='服务重启导致中断'`，进度停在 67%/74%/74%/67%；
- 旧实现（`backend/api/tasks.py:206-215`）只看 `task_service.resume_task()` 的返回值，
  而它（`task_service.py:233`）走的是无状态门的 `_update_status()`（`task_service.py:531-540`）
  —— 任何一条存在的任务都能被写成 `running`；
- 真正能恢复的流水线在 `modules/workflow/conversion_workflow.py:665-671`：pipeline 不在册就返回
  False，而**这个 False 在旧实现里被丢掉**。后端重启后 `_active_pipelines` 必为空
  （同文件 :283-292 的注释已自陈），于是"恢复"= 把数据库改成 running + 什么都不跑；
- 而清理只在下次启动做一次（`backend/main.py:104`），进程内没有任何地方会把这行僵尸改回终态。

本文件钉住四件事：① 状态门（只有 paused 能被恢复、只有 running 能被暂停）；
② 流水线门（本进程没有在册流水线就不许改状态，并给出可读原因）；
③ 两门的先后与回滚（流水线已暂停但库里没改成 ⇒ 必须把它放回去）；
④ 任务不存在仍是 404，不许被状态门掩盖。

设计与仓库既有测试一致：不打网络、不加载引擎、不起 TestClient（那会跑 lifespan 去装载模型）、
不写生产库（`backend.models.base` 的 get_db_path 指向 tmp_path 下的临时 sqlite）。
"""
from __future__ import annotations

import asyncio
import os
import sys
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import HTTPException  # noqa: E402


@pytest.fixture()
def temp_db(tmp_path, monkeypatch):
    import backend.models.base as base

    db = tmp_path / "p359.db"
    monkeypatch.setattr(base, "get_db_path", lambda: str(db))
    monkeypatch.setattr(base, "_engine", None, raising=False)
    monkeypatch.setattr(base, "_SessionLocal", None, raising=False)
    base.get_session().close()
    return db


@pytest.fixture()
def svc():
    from backend.services.task_service import task_service
    return task_service


def _new(svc, status=None):
    tid = svc.create_task(book_id="b359", output_format="m4b")["id"]
    if status:
        svc._update_status(tid, status)          # 原语：测试用它造任意起点状态
    return tid


def _status(svc, tid):
    return svc.get_task(tid)["status"]


def _api():
    import backend.api.tasks as api_mod
    return api_mod


# ============================================================ ① 服务层状态门
class TestStateGate:
    def test_resume_refuses_a_queued_task(self, svc, temp_db):
        """没暂停过的任务不该能被"恢复"——旧实现照样把它写成 running。"""
        tid = _new(svc)                                    # 建行默认 queued
        assert svc.resume_task(tid) is False, "queued -> running 不是恢复"
        assert _status(svc, tid) == "queued", "状态被无门改写"

    @pytest.mark.parametrize("status", ["failed", "cancelled", "completed"])
    def test_resume_refuses_terminal_states(self, svc, temp_db, status):
        """failed / cancelled / completed 全是终态：恢复它们等于造僵尸。"""
        tid = _new(svc, status)
        assert svc.resume_task(tid) is False, f"{status} 是终态却允许恢复"
        assert _status(svc, tid) == status

    def test_pause_refuses_a_terminal_task(self, svc, temp_db):
        tid = _new(svc, "failed")
        assert svc.pause_task(tid) is False
        assert _status(svc, tid) == "failed"

    def test_paused_task_resumes_to_running(self, svc, temp_db):
        """合法链路必须照常走得通，别把门做成死门。"""
        tid = _new(svc, "running")
        assert svc.pause_task(tid) is True
        assert _status(svc, tid) == "paused"
        assert svc.resume_task(tid) is True
        assert _status(svc, tid) == "running"


# ============================================================ ② 端点层的流水线门
class _Workflow:
    """只替 `pause/resume` 两枚，并记账——用来证明"门没命中时谁都没被改"。"""

    def __init__(self, ok):
        self.ok = ok
        self.calls = []

    def pause(self, tid):
        self.calls.append(("pause", tid))
        return self.ok

    def resume(self, tid):
        self.calls.append(("resume", tid))
        return self.ok


def _install(monkeypatch, api_mod, workflow):
    monkeypatch.setattr(api_mod, "conversion_workflow", workflow)


def test_resume_without_live_pipeline_leaves_status_paused(svc, temp_db, monkeypatch):
    """**本案的核心**：重启之后点"继续"，旧实现会把行改成 running 且没有任何东西在跑。"""
    api = _api()
    tid = _new(svc, "paused")
    wf = _Workflow(ok=False)
    _install(monkeypatch, api, wf)

    with pytest.raises(HTTPException) as ei:
        asyncio.run(api.resume_task(tid))
    assert ei.value.status_code == 409, ei.value
    assert _status(svc, tid) == "paused", f"流水线不在册却把行改成了 {_status(svc, tid)}（僵尸行）"
    assert wf.calls == [("resume", tid)], "必须先问过流水线再决定改不改状态"
    assert "重启" in str(ei.value.detail), f"原因要说清怎么办：{ei.value.detail}"


def test_resume_with_live_pipeline_moves_to_running(svc, temp_db, monkeypatch):
    api = _api()
    tid = _new(svc, "paused")
    _install(monkeypatch, api, _Workflow(ok=True))
    out = asyncio.run(api.resume_task(tid))
    assert out["status"] == "ok" and out["resumed"] is True
    assert _status(svc, tid) == "running"


def test_resume_on_failed_task_is_refused_before_touching_the_pipeline(
        svc, temp_db, monkeypatch):
    """状态门在前：一条 failed 的行根本不该去惊动流水线。"""
    api = _api()
    tid = _new(svc, "failed")
    wf = _Workflow(ok=True)
    _install(monkeypatch, api, wf)
    with pytest.raises(HTTPException) as ei:
        asyncio.run(api.resume_task(tid))
    assert ei.value.status_code == 409
    assert wf.calls == [], f"状态不合时不该碰流水线：{wf.calls}"
    assert _status(svc, tid) == "failed"


def test_pause_without_live_pipeline_does_not_write_paused(svc, temp_db, monkeypatch):
    api = _api()
    tid = _new(svc, "running")
    _install(monkeypatch, api, _Workflow(ok=False))
    with pytest.raises(HTTPException) as ei:
        asyncio.run(api.pause_task(tid))
    assert ei.value.status_code == 409
    assert _status(svc, tid) == "running", "没有流水线可暂停时不该留下 paused 的行"


def test_pause_and_resume_are_symmetric_on_success(svc, temp_db, monkeypatch):
    api = _api()
    tid = _new(svc, "running")
    _install(monkeypatch, api, _Workflow(ok=True))
    out = asyncio.run(api.pause_task(tid))
    assert out["paused"] is True and _status(svc, tid) == "paused"


def test_db_write_failure_rolls_the_pipeline_back(svc, temp_db, monkeypatch):
    """流水线已暂停、库里却改不动 ⇒ 必须把它放回运行，否则是"没人知道的暂停"。"""
    api = _api()
    tid = _new(svc, "running")
    wf = _Workflow(ok=True)
    _install(monkeypatch, api, wf)
    monkeypatch.setattr(api.task_service, "pause_task", lambda *a, **k: False)
    with pytest.raises(HTTPException) as ei:
        asyncio.run(api.pause_task(tid))
    assert ei.value.status_code == 409
    assert ("resume", tid) in wf.calls, f"没回滚：{wf.calls}"
    assert _status(svc, tid) == "running"


def test_missing_task_is_still_404(temp_db, monkeypatch):
    """不存在 ≠ 状态不合：404 不能被状态门吞成 409。"""
    api = _api()
    _install(monkeypatch, api, _Workflow(ok=True))
    with pytest.raises(HTTPException) as ei:
        asyncio.run(api.resume_task("nope-359"))
    assert ei.value.status_code == 404


def test_reason_names_the_current_status(svc, temp_db, monkeypatch):
    """拒绝的理由要带上"现在是什么态"，否则用户与排查者都只能猜。"""
    api = _api()
    tid = _new(svc, "completed")
    _install(monkeypatch, api, _Workflow(ok=True))
    with pytest.raises(HTTPException) as ei:
        asyncio.run(api.resume_task(tid))
    assert "completed" in str(ei.value.detail), ei.value.detail
