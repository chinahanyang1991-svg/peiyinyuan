"""`DELETE /api/dubbing/projects/{id}` 必须连带清掉该项目的 conversion_tasks。

缺陷（主线与 cast-verify 泳道各自独立发现，形状一致）：项目删除只级联了
roles/voice_assignments，任务行留在 `conversion_tasks` 里指向已不存在的项目，
快检 harness 每跑一轮泄 3 条（生产库实测累积 21 条孤儿）。
这里用临时库钉住"删项目 -> 任务也归零"，并钉住"别的项目的任务不受牵连"。
"""
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import backend.models.base as base
from backend.models.book import Book
from backend.models.project import DubbingProject
from backend.models.task import ConversionTask


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{(tmp_path / 'cascade.db').as_posix()}",
                           connect_args={"check_same_thread": False})
    base.Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(base, "_engine", engine)
    monkeypatch.setattr(base, "_SessionLocal", Session)
    yield Session
    engine.dispose()


def _mk(session, project_id: str, book_id: str) -> None:
    session.add(DubbingProject(id=project_id, name="p-" + project_id[:4],
                               book_id=book_id))
    session.add(Book(id=book_id, title="t", format="epub", total_chars=10))
    for _ in range(3):                       # 一次 analyze-cast = 一条任务
        session.add(ConversionTask(id=str(uuid.uuid4()), project_id=project_id,
                                   book_id=book_id, status="completed",
                                   current_stage="cast_done"))
    session.commit()


def test_delete_project_purges_its_tasks(tmp_db):
    from backend.services.dubbing_service import DubbingService
    s = tmp_db()
    mine, other = str(uuid.uuid4()), str(uuid.uuid4())
    _mk(s, mine, str(uuid.uuid4()))
    _mk(s, other, str(uuid.uuid4()))
    assert s.query(ConversionTask).filter_by(project_id=mine).count() == 3

    assert DubbingService().delete_project(mine) is True

    assert s.query(ConversionTask).filter_by(project_id=mine).count() == 0
    assert s.query(DubbingProject).filter_by(id=mine).first() is None
    # 别的项目的任务不得被牵连删除
    assert s.query(ConversionTask).filter_by(project_id=other).count() == 3
    s.close()


def test_delete_missing_project_returns_false(tmp_db):
    from backend.services.dubbing_service import DubbingService
    assert DubbingService().delete_project(str(uuid.uuid4())) is False
