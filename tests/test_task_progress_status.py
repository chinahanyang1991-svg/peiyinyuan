"""PY-331 回归：任务终态由调用方显式交下，不再靠 progress>=1.0 猜。

C4 干净树复跑实测抓到一次「/tasks/{id}/progress 已到 completed，
而 GET /tasks/{id} 的 DB 行还是 running」——三次复跑出现一次。根因两层：
  1) 写序：先发 tracker（内存）再落 DB，两个视图之间隔着一次 to_thread + 提交；
  2) 推断：update_progress 见 progress>=1.0 就写 completed，而失败/取消的
     终态进度同样是 1.0，于是一次 cast 失败的行会先被标成 completed 再改回 failed。
这里锁住第 2 层的语义（第 1 层是顺序，由 cast_service.areport 的注释与 C4 断言守着）：
显式 status 优先，不传时行为与旧版逐字一致（转换主流程从不传）。

设计约束（与仓库既有测试一致）：不打网络、不加载引擎、不写生产库
（backend.models.base 的 _SessionLocal 指向 tmp_path 下的临时 sqlite）。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


@pytest.fixture()
def temp_db(tmp_path, monkeypatch):
    import backend.models.base as base

    db = tmp_path / "p331.db"
    monkeypatch.setattr(base, "get_db_path", lambda: str(db))
    monkeypatch.setattr(base, "_engine", None, raising=False)
    monkeypatch.setattr(base, "_SessionLocal", None, raising=False)
    base.get_session().close()
    return db


@pytest.fixture()
def svc():
    from backend.services.task_service import task_service
    return task_service


def _new(svc, book="b331"):
    return svc.create_task(book_id=book, output_format="cast")["id"]


class TestTerminalStatusIsNotGuessed:

    def test_failure_terminal_does_not_pass_through_completed(self, svc, temp_db):
        """失败终态那一跳 progress 也是 1.0 —— 旧行为先标 completed 再被 fail_task 改回。"""
        tid = _new(svc)
        svc.update_progress(tid, 0.9, "cast_extract", 900, 1000)
        svc.update_progress(tid, 1.0, "cast_failed", 900, 1000,
                            status="failed", error="boom")
        row = svc.get_task(tid)
        assert row["status"] == "failed", "显式 failed 被 progress>=1.0 的猜测覆盖"
        assert row["completed_at"], "终态必须落 completed_at"

    def test_cancelled_terminal_stays_cancelled(self, svc, temp_db):
        """取消的任务在列表里必须还是"已取消"，不能因为收尾进度到 1.0 变 completed。"""
        tid = _new(svc)
        svc.update_progress(tid, 0.3, "cast_extract", 300, 1000)
        svc.update_progress(tid, 1.0, "cast_cancelled", 300, 1000, status="cancelled")
        row = svc.get_task(tid)
        assert row["status"] == "cancelled"
        # 终态后 worker 再把手上的失败写回来，也不得改状态（WF-07 同一条门）
        svc.fail_task(tid, "取消前最后一次抽取报错")
        assert svc.get_task(tid)["status"] == "cancelled"

    def test_running_is_not_treated_as_terminal(self, svc, temp_db):
        """status="running" 不是终态：进度到 1.0 仍按旧规则判 completed。"""
        tid = _new(svc)
        svc.update_progress(tid, 1.0, "cast_done", 1000, 1000, status="running")
        assert svc.get_task(tid)["status"] == "completed"

    def test_callers_that_pass_no_status_keep_old_behavior(self, svc, temp_db):
        """转换主流程（从不传 status）行为不变 —— 这一条是本次改动的兼容门。"""
        tid = _new(svc)
        svc.update_progress(tid, 0.5, "synthesize", 500, 1000)
        assert svc.get_task(tid)["status"] == "running"
        svc.update_progress(tid, 1.0, "finalize", 1000, 1000)
        row = svc.get_task(tid)
        assert row["status"] == "completed"
        assert (row["processed_chars"], row["total_chars"]) == (1000, 1000)


class TestCastServiceForwardsStatus:
    """cast_service 侧的接线：_persist_progress 必须把终态透传给 task_service。"""

    def test_persist_progress_forwards_status_and_total(self, monkeypatch):
        import backend.services.cast_service as cs

        seen = {}

        def fake_update(task_id, progress, current_stage="", processed_chars=0,
                        total_chars=None, message="", error="", status=""):
            seen.update(task_id=task_id, progress=progress, stage=current_stage,
                        total_chars=total_chars, status=status)

        monkeypatch.setattr(cs.task_service, "update_progress", fake_update)
        cs._persist_progress("t1", 1.0, "cast_failed", 900, 1000, status="failed")
        assert seen["status"] == "failed", seen
        assert seen["total_chars"] == 1000, "C4-1 口径：分母必须一并落库"

        seen.clear()
        cs._persist_progress("t1", 0.5, "cast_extract", 500, 1000)
        assert seen["status"] == "", "无终态时不得凭空造状态"

    def test_tracker_never_announces_terminal_before_db(self, svc, temp_db, monkeypatch):
        """竞态的确定性版本：tracker 被告知终态的那一刻，DB 行必须**已经**是终态。

        只把 _analyze_sync 换成桩（不跑真分析、不打网络），收尾走的仍是产品自己的
        areport -> _persist_progress -> tracker.update 链路。旧顺序（先发 tracker
        再落库）会让 tracker 读到 running，这条断言当场红。
        """
        import asyncio

        import backend.services.cast_service as cs

        tid = _new(svc)
        db_status_at_announce = []

        class _FakeTracker:
            async def update(self, frac, stage, chars, total, status="", error=""):
                if status and status != "running":
                    db_status_at_announce.append(
                        (stage, status, svc.get_task(tid)["status"]))

        monkeypatch.setattr(cs, "_register_tracker", lambda task_id: _FakeTracker())
        monkeypatch.setattr(cs, "_unregister_tracker", lambda task_id: None)

        def fake_analyze(self, project_id, book_id, use_llm, report, cancel_requested):
            report(0.5, "cast_extract", 500, 1000)
            return {"roles": [], "chars": 1000, "role_count": 0, "llm": False}

        monkeypatch.setattr(cs.CastService, "_analyze_sync", fake_analyze)
        out = asyncio.run(cs.CastService().run_analysis(tid, "p331", "b331",
                                                        use_llm=False))
        assert out["status"] == "completed", out
        assert db_status_at_announce, "桩没被走到 —— 这条断言就成了恒真"
        assert all(declared == persisted for _, declared, persisted
                   in db_status_at_announce), db_status_at_announce
        assert svc.get_task(tid)["status"] == "completed"

    def test_failed_analysis_does_not_linger_as_completed(self, svc, temp_db,
                                                         monkeypatch):
        """同一链路的失败分支：整条收尾过程里 DB 行不能出现 completed 这一瞬。"""
        import asyncio

        import backend.services.cast_service as cs

        tid = _new(svc)
        observed = []

        class _WatchTracker:
            async def update(self, frac, stage, chars, total, status="", error=""):
                observed.append(svc.get_task(tid)["status"])

        monkeypatch.setattr(cs, "_register_tracker", lambda task_id: _WatchTracker())
        monkeypatch.setattr(cs, "_unregister_tracker", lambda task_id: None)

        def boom(self, project_id, book_id, use_llm, report, cancel_requested):
            raise RuntimeError("抽取器崩了")

        monkeypatch.setattr(cs.CastService, "_analyze_sync", boom)
        out = asyncio.run(cs.CastService().run_analysis(tid, "p331", "b331",
                                                        use_llm=False))
        assert out["status"] == "failed", out
        assert "completed" not in observed, observed
        assert svc.get_task(tid)["status"] == "failed"