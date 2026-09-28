"""U-5：工作流模板做实（播种 + run 出口 + 前端入口）

审核矩阵里最低的一块（45%）：`GET /api/workflows/templates` 200 但 **0 行**，
UI 有入口、数据面永远什么都没有；`POST /create` 已是明确拒绝位（WF-17）。
本文件把"做实"这条路的三件事钉住：

1. 三条官方模板随 `init_db` 幂等播种，且字段只用 workflow 真读的 settings 键；
2. `POST /templates/{id}/run` 复用 `/api/tasks/convert` 那条生产路径（不另起一份实现），
   缺 book_id / 模板不存在 在**状态码层面**可见（不接受"200 + 什么都没发生"）；
3. 前端模板卡真的有运行入口，且没选书时不发请求。
"""

import asyncio
import inspect

import pytest
from fastapi import HTTPException

from backend.api import tasks as tasks_api
from backend.api import workflows as wf_api
from modules.workflow.conversion_workflow import SUPPORTED_OUTPUT_FORMATS
from modules.workflow.workflow_service import (
    OFFICIAL_TEMPLATES,
    seed_official_templates,
    workflow_template_service,
)

# workflow 侧真正消费的 settings 键（grep 自 conversion_workflow 的 settings.get(...)）。
# 模板里出现别的键 = 骗 UI 的装饰字段，故列为红线。
CONSUMED_SETTINGS_KEYS = {"engine", "max_sentences", "mock_tts"}


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- 播种
def test_three_official_templates_exist_and_are_idempotent():
    names = [t["name"] for t in OFFICIAL_TEMPLATES]
    assert len(names) == 3 == len(set(names)), names

    seed_official_templates()          # 首次可能新建，也可能库里已有
    seed_official_templates()          # 第二次一枚都不该新建
    assert seed_official_templates() == [], "同名跳过失效：播种会重复建行"
    stored = {t["name"] for t in workflow_template_service.list_all(limit=200)}
    assert set(names) <= stored, sorted(set(names) - stored)


def test_official_templates_only_use_settings_the_workflow_reads():
    for tpl in OFFICIAL_TEMPLATES:
        assert set(tpl["settings"].keys()) <= CONSUMED_SETTINGS_KEYS, tpl
        assert tpl["output_format"] in SUPPORTED_OUTPUT_FORMATS, tpl
        assert tpl["engine"], tpl
    # 快速抽检那条必须真被 max_sentences 限制住，否则它与整本模板没有区别
    quick = [t for t in OFFICIAL_TEMPLATES if "max_sentences" in t["settings"]]
    assert quick and quick[0]["settings"]["max_sentences"] <= 50, quick


def test_list_templates_route_returns_seeded_rows():
    seed_official_templates()
    body = _run(wf_api.list_templates(limit=200))
    assert body["status"] == "ok"
    assert body["total"] >= 3, body["total"]
    assert any(t["name"].startswith("官方 ·") for t in body["templates"]), body["templates"]


# ---------------------------------------------------------------- run 出口
def _stub_start_conversion(sink):
    async def _fake(req):
        sink.append(req)
        return {"status": "ok", "task": {"id": "stub-task",
                                         "output_format": req.output_format}}
    return _fake


def test_run_reuses_the_production_convert_path(monkeypatch):
    seed_official_templates()
    tpl = next(t for t in workflow_template_service.list_all(limit=200)
               if t["name"].startswith("官方 ·"))
    captured = []
    monkeypatch.setattr(tasks_api, "start_conversion", _stub_start_conversion(captured))

    body = _run(wf_api.run_template(tpl["id"], wf_api.RunTemplateRequest(
        book_id=" book-1 ", settings={"max_sentences": 3})))

    assert len(captured) == 1, "run 必须把请求交给 /api/tasks/convert 那一条实现"
    req = captured[0]
    assert req.book_id == "book-1", req.book_id          # 两端空白都被夹过
    assert req.output_format == tpl["output_format"]      # 模板决定格式
    assert req.settings.get("engine") == tpl["engine"]
    assert req.settings.get("max_sentences") == 3          # 调用方可覆盖/追加
    assert body["task"]["id"] == "stub-task"
    assert body["template"]["id"] == tpl["id"]


def test_run_requires_an_explicit_book(monkeypatch):
    seed_official_templates()
    tpl = workflow_template_service.list_all(limit=200)[0]
    captured = []
    monkeypatch.setattr(tasks_api, "start_conversion", _stub_start_conversion(captured))
    with pytest.raises(HTTPException) as ei:
        _run(wf_api.run_template(tpl["id"], wf_api.RunTemplateRequest(book_id="   ")))
    assert ei.value.status_code == 422, ei.value
    assert "book_id" in str(ei.value.detail)
    assert captured == [], "缺输入时不得已经起任务"


def test_run_404s_on_unknown_template(monkeypatch):
    captured = []
    monkeypatch.setattr(tasks_api, "start_conversion", _stub_start_conversion(captured))
    with pytest.raises(HTTPException) as ei:
        _run(wf_api.run_template("no-such-template-id",
                                 wf_api.RunTemplateRequest(book_id="b1")))
    assert ei.value.status_code == 404
    assert captured == []


# ---------------------------------------------------------------- 静态守卫
def test_init_db_hooks_the_seeder():
    from backend.models import base

    src = inspect.getsource(base.init_db)
    # 只查标识符会被 import 行蒙过（变异 M5 实测仍绿），故查调用式。
    assert "seed_official_templates()" in src, "播种没挂进 init_db，老库永远等不到模板"


def test_frontend_has_a_real_run_entry():
    import io

    src = io.open("frontend/src/routes/WorkflowPage.tsx", encoding="utf-8").read()
    assert "`/workflows/templates/${tpl.id}/run`" in src, "前端没接 run 出口"
    assert "if (!bookId)" in src, "前端在未选书时仍会发请求"
    assert "navigate(`/convert/" in src, "起任务后没跳进度页"
    assert "res.data?.task?.id" in src, "没有从响应里取 task id"
