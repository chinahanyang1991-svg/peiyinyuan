"""一键确认（PUT /cast/confirm）走的是另一条分配路 —— 它曾经绕过 PY-329/330 两道门。

取证（读码 + 本文件第一版实测）：`_analyze_sync` 用的是带去重和门槛的
`auto_assign(min_score=40, taken=手工档)`，而 `confirm_cast` 的 `auto_assign_rest`
回退分支用的是 `suggest(..., top_n=1)`：**逐角色独立取 Top1、无分数门槛、无跨角色
去重**。打分是纯函数，同画像角色算出的排序完全相同，于是用户在向导里点一次
「一键确认」，前面刚修好的塌缩就在落库这一步原样重演（《谍影重重》60 角色
只剩 5 枚音色、48 枚共用同一个"台式男声"的那个形状）。

本文件锁的是确认路径这条出口，不重复测分析路径（那部分在
tests/test_cast_voice_pool.py / tests/test_voice_matcher_diversity.py）。

fixture 全走临时库，绝不碰 data/peiyinyuan.db。
"""
import os
import sys

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import backend.services.cast_service as cs                    # noqa: E402
from backend.models.project import DubbingProject, Role        # noqa: E402
from backend.models.voice import VoiceProfile                 # noqa: E402
from modules.cast_analyzer.voice_matcher import score_voice    # noqa: E402


MIN_SCORE = cs.AUTO_ASSIGN_MIN_SCORE


@pytest.fixture
def db(tmp_path, monkeypatch):
    """整表建齐，不手挑。

    第一版只建了 voice_profiles/dubbing_projects/roles 三张，`_load_project`
    里 `project.to_dict()` 会顺 assignments 关系去查 voice_assignments ->
    no such table。红在夹具不在产品，按仓库口径一次性 create_all 建齐。
    """
    from backend.models.base import Base
    import backend.models  # noqa: F401  确保所有模型都注册进 metadata

    engine = create_engine(f"sqlite:///{(tmp_path / 'confirm.db').as_posix()}")
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(cs, "get_session", Session)
    yield Session
    engine.dispose()


def _wav(tmp_path, name):
    """非全零确定字节：夹具里也不留"静音文件"这个 PY-117 禁止的形状。"""
    p = tmp_path / name
    p.write_bytes(bytes([(i * 37) % 251 + 1 for i in range(4096)]))
    return p.as_posix()


def _voice(session, tmp_path, vid, gender="male", age="adult", engine="voxcpm"):
    session.add(VoiceProfile(
        id=vid, name=vid, source="downloaded", category="human", engine=engine,
        language="zh", gender=gender, age_group=age,
        sample_audio_path=_wav(tmp_path, f"{vid}.wav")))
    session.commit()


def _project(session, names, bound=None, profile=None):
    """建 multi 项目 + 若干角色；bound = {角色名: (voice_id, voice_source)}。"""
    proj = DubbingProject(id="p1", name="确认去重测试", mode="multi", status="ready")
    session.add(proj)
    base = {"gender": "male", "age_group": "adult"}
    base.update(profile or {})
    for i, n in enumerate(names):
        got = (bound or {}).get(n)
        cast = dict(base)
        cast["lines_count"] = 100 - i
        session.add(Role(id=f"r-{n}", project_id="p1", name=n,
                         role_type="narrator" if n == "旁白" else "supporting",
                         voice_id=got[0] if got else None,
                         extra_data={"cast": cast,
                                     "voice_source": got[1] if got else None}))
    session.commit()
    return "p1"


def _bound(session, pid="p1"):
    return {r.name: r.voice_id for r in session.query(Role).filter_by(project_id=pid)}


class TestConfirmDedup:

    def test_one_click_confirm_gives_same_profile_roles_distinct_voices(self, db, tmp_path):
        """5 个同画像角色 + 5 枚同质音色 -> 一键确认必须一人一枚。"""
        s = db()
        for i in range(5):
            _voice(s, tmp_path, f"v{i}")
        pid = _project(s, [f"甲{i}" for i in range(5)])
        cs.CastService().confirm_cast(pid, {})
        got = _bound(s, pid)
        s.close()
        assert all(v for v in got.values()), got
        assert len(set(got.values())) == 5, (
            f"塌缩复发：5 个角色只分到 {len(set(got.values()))} 枚音色 {got}")

    def test_manual_binding_is_never_lent_out_on_confirm(self, db, tmp_path):
        """用户手工绑给甲的那枚，一键确认不得再分给乙丙（taken 语义）。"""
        s = db()
        for i in range(4):
            _voice(s, tmp_path, f"v{i}")
        pid = _project(s, ["甲", "乙", "丙"], bound={"甲": ("v0", "manual")})
        cs.CastService().confirm_cast(pid, {})
        got = _bound(s, pid)
        s.close()
        assert got["甲"] == "v0", "手工绑定被改写"
        assert got["乙"] != "v0" and got["丙"] != "v0", got
        assert len({got["甲"], got["乙"], got["丙"]}) == 3, got

    def test_confirm_rejects_sub_threshold_voice(self, db, tmp_path):
        """池里只有不合格档位时留空由 UI 提示手选，不硬塞（PY-329 同口径）。

        角色 male/child vs 音色 female/senior：性别 0 + 年龄 0（child→senior 隔两级）
        + 无风格标签 6 + 语种同 10 = 16，远低于门槛 40。刻意用 supporting 而非旁白 ——
        role_attrs 对旁白会强制 adult 并补风格标签，分数就不是这个数了。
        """
        s = db()
        _voice(s, tmp_path, "v_wrong", gender="female", age="senior")
        pid = _project(s, ["甲"], profile={"age_group": "child"})
        svc = cs.CastService()
        cand = svc._cast_candidates(svc._load_project(pid))[0]
        pool = cs.CastService._voice_dicts()
        assert [v["id"] for v in pool] == ["v_wrong"], pool
        # 前置自证用产品自己喂给打分器的那两份，不是我另搓的字典：
        # 分数确实低于门槛，而这枚音色本身可用且非回放档
        # （否则这条测的是 PY-330 的池门，不是分数门）
        assert cand["name"] == "甲" and cand["is_narrator"] is False, cand
        assert score_voice(cand, pool[0])["score"] < MIN_SCORE, "夹具失效"
        assert cs.voice_is_usable(pool[0]) and not cs.voice_is_replay_only(pool[0])

        svc.confirm_cast(pid, {})
        got = _bound(s, pid)
        s.close()
        assert not got["甲"], f"低于门槛的音色被硬塞进库：{got}"

    def test_confirm_pool_excludes_replay_only(self, db, tmp_path):
        """PY-330 的门也覆盖确认路径：回放档即便分数最高也不得自动绑定。"""
        s = db()
        _voice(s, tmp_path, "v_best", gender="male", age="adult")
        _voice(s, tmp_path, "v_replay", gender="male", age="adult", engine="sample")
        _voice(s, tmp_path, "v_worse", gender="unknown", age="adult")
        pid = _project(s, ["甲"])
        cs.CastService().confirm_cast(pid, {})
        got = _bound(s, pid)
        s.close()
        assert got["甲"] != "v_replay", "回放档经一键确认进了库"
        assert got["甲"] == "v_best", got


class TestConfirmKeepsUserChoice:

    def test_explicit_payload_voice_wins_over_pool(self, db, tmp_path):
        """payload 里显式带 voice_id 的角色：不参与自动分配，也不占别人的坑。"""
        s = db()
        for i in range(3):
            _voice(s, tmp_path, f"v{i}")
        pid = _project(s, ["甲", "乙"])
        cs.CastService().confirm_cast(
            pid, {"roles": [{"name": "甲", "voice_id": "v2"},
                            {"name": "乙", "voice_id": "v1"}]})
        got = _bound(s, pid)
        row = s.query(Role).filter_by(name="甲").first()
        s.close()
        assert got == {"甲": "v2", "乙": "v1"}, got
        assert (row.extra_data or {}).get("voice_source") == "manual"

    def test_auto_assign_rest_false_leaves_unbound_roles_alone(self, db, tmp_path):
        """关掉回退开关时不得私自分配（用户要自己挑）。"""
        s = db()
        _voice(s, tmp_path, "v0")
        pid = _project(s, ["甲"])
        cs.CastService().confirm_cast(pid, {"auto_assign_rest": False})
        got = _bound(s, pid)
        s.close()
        assert not got["甲"], got
