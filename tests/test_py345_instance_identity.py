# -*- coding: utf-8 -*-
"""PY-345：`/api/health` 必须说得出"我到底是哪台实例"（静态 + 端点行为）。

定罪现场（2026-09-24 02:2x 本机实测，非推导）：默认端口 :8000 上挂着一台
**从 git 工作树里起出来**的后端 —— `psutil.Process(36504).cwd()` =
`D:\\程序开发\\PYY\\wt_u11b`（detached @`77d90f9`，起于 4.8 小时前）。它的
`Config.BASE_DIR` 跟着 `__file__` 走，于是两件事同时发生：
  ① voxcpm 权重目录被解析成 `wt_u11b\\data\\models\\voxcpm2`（工作树里没有
     `data/`），`GET /api/system/model_status` 实测
     `state=failed, attempts=3`，报的就是那句 `FileNotFoundError: 权重目录不存在`；
  ② 它连的是**工作树自己那本空库**（`wt_u11b/data/peiyinyuan.db`，4,096 字节、
     21:41 由 init_db 现造），`GET /api/books` 实测 `total=0`，而同一分钟主检出
     的库里是 90 本书。
最坏的不是这两条，而是同刻 `GET /api/health` 回的是
`{"status":"ok", "tts_engine":"voxcpm", ...}` —— **一台既不能出声也没有书的实例，
在健康检查里和正常实例长得一模一样**（PY-117 同族："拿 200 冒充成功"）。
所以本文件守的是"身份可见"，不是"权重必须在某处存在"。

口径：`instance` 里只做字符串拼接 + 一次目录 stat，**不得碰数据库** ——
`/api/health` 是前端轮询端点，导入大书期间它必须保持轻量
（那条约束的原文在 `backend/api/books.py` 里，PY-340 的并发消融就是为它跑的）。
"不碰库"这条用 `test_health_never_touches_the_database` 钉住：把取引擎/取会话
两条入口都换成抛异常，健康检查仍须 200 —— 谁以后往 health 里塞一条 `COUNT(*)`，
这一枚会红。
"""
import os
import sys
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import main  # noqa: E402
from backend.config import Config  # noqa: E402


IDENTITY_KEYS = {"base_dir", "cwd", "db_path", "weights_dir", "weights_present"}


def _client():
    # 不进上下文管理器 => 不触发 lifespan（既不 init_db 也不预热模型）
    return TestClient(main.app)


def test_health_payload_carries_instance_identity():
    """/api/health 的 `instance` 必须报出 BASE_DIR/cwd/DB/权重目录与其存在性。"""
    body = _client().get("/api/health").json()

    assert body["status"] == "ok", body
    ident = body.get("instance")
    assert isinstance(ident, dict), f"health 里没有 instance 块：{list(body)}"
    assert IDENTITY_KEYS <= set(ident), IDENTITY_KEYS - set(ident)
    assert ident["base_dir"] == str(Config.BASE_DIR), ident
    assert ident["db_path"] == str(Config.DB_PATH), ident
    # cwd 报的是进程真实工作目录（定罪那台报的就是 wt_u11b，这才看得见问题）
    assert ident["cwd"] == os.getcwd(), ident
    assert isinstance(ident["weights_present"], bool), ident
    assert Path(ident["weights_dir"]).is_dir() == ident["weights_present"], ident


def test_weights_dir_is_the_one_the_adapter_actually_loads():
    """health 报的权重目录必须等于适配器真正拿去 `os.path.isdir` 的那一份。

    两处各写一条路径就会漂：漂了之后 health 说"权重在"，引擎却按另一条路径
    判"不存在"（或反过来），这台实例又变回"健康检查会说谎"。
    """
    from modules.tts_engine.engines.voxcpm_adapter import VoxCPMAdapter

    adapter = VoxCPMAdapter(model_path=None, device="cpu")
    assert str(main._voxcpm_weights_dir()) == adapter.model_path, (
        main._voxcpm_weights_dir(), adapter.model_path)


def test_health_never_touches_the_database(monkeypatch):
    """健康检查不得取会话/取引擎（导入大书期间它必须还是那枚轻量端点）。"""
    from backend.models import base as db_base

    def boom(*a, **k):
        raise AssertionError("health 不该碰数据库")

    monkeypatch.setattr(db_base, "get_engine", boom)
    monkeypatch.setattr(db_base, "get_session", boom)

    res = _client().get("/api/health")
    assert res.status_code == 200, res.status_code
    assert res.json()["instance"]["base_dir"] == str(Config.BASE_DIR)


def test_missing_weights_dir_is_logged_by_name(monkeypatch, caplog, tmp_path):
    """权重目录缺失时，启动留痕必须点名"是哪条目录不存在 + 后果是红线④"。

    定罪那台的 `failed` 只在 model_status 里看得到，日志上一句身份都没留，
    所以事后无从判断"起来的是哪棵树"。这里验证的是"说得出来"，
    不是"权重必须存在"（工作树/CI 里不存在是合法的，红的是沉默）。
    """
    missing = tmp_path / "wt_probe" / "data" / "models" / "voxcpm2"
    monkeypatch.setattr(main, "_voxcpm_weights_dir", lambda: missing)

    with caplog.at_level("INFO", logger="peiyinyuan"):
        ident = main._log_instance_identity()

    assert ident["weights_present"] is False, ident
    text = "\n".join(f"{rec.levelname}|{rec.message}" for rec in caplog.records)
    assert f"权重目录={missing}" in text, text
    warns = [r for r in caplog.records if r.levelno >= 30]
    assert warns, f"权重缺失只留了 info，没有告警：{text}"
    w = warns[-1].getMessage()
    assert str(missing) in w, w
    assert "红线④" in w and "工作树" in w, w
