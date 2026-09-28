"""cast 推荐/自动绑定池必须剔掉"只会回放自己那条样本"的音色 (PY-330)。

取证（《谍影重重》4a92f0a4 / 173 章真书，修完音色塌缩后的第二次自动分配实测）：
    引擎分布 {'edgetts': 6, 'voxcpm': 52, 'sample': 2}
    卡克特斯 -> 人声-儿童声 Child-implant-users-imitation-of-happy--and-
    法官     -> 人声-Child-implant-users-imitation-of-happy--and-sad-sounding (2)
这两个角色的台词在全书每一句都会被回放成同一条英文样本
（SamplePlaybackEngine.synthesize 收下 text 后不看它，原样返回 master wav），
属于红线 8「人声可应用」的违例，且与被剔除过的 builtin 同类：听起来"能出声"，
但不是这句话说这句话。

判据与 C4-2 的 voice_is_usable 刻意分开：sample 行**是**自有嗓子、试听放得出来
（C4 的试听口径实测自有=盘上有主样本=570 里含这 7 枚 human sample），所以不能靠
改 usable 来挡，必须加一条"能不能按台词合成"的门。手工绑定仍然允许（固定台词/
音效段是它的正业），故门只作用于推荐池。

fixture 全部走临时库，绝不碰 data/peiyinyuan.db。
"""
import os
import sys

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import backend.services.cast_service as cs                    # noqa: E402
from backend.models.voice import VoiceProfile                 # noqa: E402


@pytest.fixture
def pool(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{(tmp_path / 'pool.db').as_posix()}")
    VoiceProfile.__table__.create(bind=engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(cs, "get_session", Session)
    yield Session, tmp_path
    engine.dispose()


def _wav(tmp_path, name):
    """voice_is_usable 只看扩展名 + isfile + >1KB，从不解码音频。

    内容故意写成非全零的确定字节：全零文件=静音，而"拿静音冒充产物"是 PY-117
    明令禁止的形状，即使在本夹具里也不留这个样子。
    """
    p = tmp_path / name
    p.write_bytes(bytes([(i * 37) % 251 + 1 for i in range(4096)]))
    return p.as_posix()


def _seed(session, tmp_path, vid, engine_name, with_sample=True, category="human"):
    session.add(VoiceProfile(
        id=vid, name=vid, source="downloaded", category=category,
        engine=engine_name, language="zh", gender="male", age_group="adult",
        sample_audio_path=_wav(tmp_path, f"{vid}.wav") if with_sample else None))
    session.commit()


def _ids(rows):
    return {r["id"] for r in rows}


# ---------------------------------------------------------------- 判据本身
def test_replay_only_predicate_is_engine_based():
    """判定只看 engine 档位（大小写/空格无关），不看有没有样本。"""
    assert cs.voice_is_replay_only({"engine": "Sample"})
    assert cs.voice_is_replay_only({"engine": " sample "})
    assert not cs.voice_is_replay_only({"engine": "voxcpm"})
    assert not cs.voice_is_replay_only({"engine": None})
    assert not cs.voice_is_replay_only(None)


def test_replay_gate_is_independent_of_usability(tmp_path):
    """同一枚 sample 行：试听可用（usable=True），但不可自动绑定（replay_only=True）。

    这两把尺子必须各判各的——把它们合成一把就会把 C4 的"自有嗓子"实测口径带偏。
    """
    p = tmp_path / "s.wav"
    p.write_bytes(bytes([(i * 37) % 251 + 1 for i in range(4096)]))
    d = {"engine": "sample", "sample_audio_path": p.as_posix()}
    assert cs.voice_is_usable(d) is True
    assert cs.voice_is_replay_only(d) is True


# ---------------------------------------------------------------- 推荐池
def test_auto_pool_excludes_replay_only_voices(pool):
    """默认池（自动分配/推荐吃的那份）里不许有回放档，其余可用音色照常保留。"""
    Session, tmp_path = pool
    s = Session()
    _seed(s, tmp_path, "v_voxcpm", "voxcpm")
    _seed(s, tmp_path, "v_edgetts", "edgetts")
    _seed(s, tmp_path, "v_sample", "sample")            # 可用，但只能回放
    _seed(s, tmp_path, "v_nosample", "voxcpm", with_sample=False)
    s.close()

    auto = cs.CastService._voice_dicts()
    assert _ids(auto) == {"v_voxcpm", "v_edgetts"}

    full = cs.CastService._voice_dicts(require_sample=False)
    by_id = {d["id"]: d for d in full}
    assert by_id["v_sample"]["usable"] is True          # 试听口径不变
    assert by_id["v_sample"]["replay_only"] is True     # 自动绑定口径另判
    assert by_id["v_nosample"]["usable"] is False
    assert by_id["v_nosample"]["replay_only"] is False


def test_sound_effect_category_still_out(pool):
    """VA-2 的类别门与 PY-329 的档位门互不遮蔽：音效行本来就不进池。"""
    Session, tmp_path = pool
    s = Session()
    _seed(s, tmp_path, "v_animal", "sample", category="animal")
    _seed(s, tmp_path, "v_human_sample", "sample", category="human")
    _seed(s, tmp_path, "v_ok", "voxcpm", category=None)   # NULL 视为 human
    s.close()
    assert _ids(cs.CastService._voice_dicts()) == {"v_ok"}


def test_auto_assign_gets_no_replay_voice_from_the_pool(pool):
    """整条链路的收口：`_voice_dicts()` 是 auto_assign 唯一的音色来源，
    所以只要池子干净，自动分配就不可能分到回放档（不再需要各调用点各自设防）。
    """
    Session, tmp_path = pool
    s = Session()
    _seed(s, tmp_path, "v_sample", "sample")
    _seed(s, tmp_path, "v_ok", "voxcpm")
    s.close()
    from modules.cast_analyzer import auto_assign
    voices = cs.CastService._voice_dicts()
    cast = [{"name": f"角色{i}", "lines_count": 10 - i,
             "profile": {"gender": "male", "age_group": "adult"}} for i in range(6)]
    assign = auto_assign(cast, voices, min_score=cs.AUTO_ASSIGN_MIN_SCORE)
    assert len(assign) == 6
    assert set(assign.values()) <= {"v_ok", None}, assign
