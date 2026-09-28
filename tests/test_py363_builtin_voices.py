"""PY-363：4 枚 source='builtin' 的 voxcpm 音色在装载时被无视（挑了等于没挑）。

现场（本地音色档全检取证）：voice_profiles 里 engine='voxcpm' 的行按 source 分为
    builtin=4  cloned=1  designed=21  downloaded=981   （合计 1007）
后端装载日志原文自陈 `[VoxCPM] 从 DB 恢复了 1002 个音色 (参考音频: 981 [cloned+downloaded], 设计: 21)`，
即 1007 里有 5 枚根本没进内存字典 —— 其中 4 枚就是这 4 个 builtin 内置音色
（标准女声-温柔 / 标准男声-磁性 / 活泼女声-少女 / 沉稳男声-中年），它们各自带着真实
的 description（现采文案），在选声器里可见可绑，但 `_restore_voices` 的两条分支：
    :787  source in ("cloned","downloaded") and reference_audio_path   → 走克隆参考音频
    :793  source == "designed" and description                          → 走文本前缀
都不认 'builtin'，于是这四枚在合成时塌成模型自己的默认声。

修复口径：装载时把 `source='builtin' 且 description 非空` 的行并入 `_designed_voices`，
走与 designed **完全相同**的文本前缀路径，让四枚的 description 各自生效。

夹具全部走 tmp_path 上的临时 sqlite，绝不碰生产库 data/peiyinyuan.db；
隔离手法照抄仓内既有先例 tests/test_cast_voice_pool.py（create_engine + VoiceProfile.__table__.create + 打桩 get_session）。
"""
import os
import sys

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from backend.models.voice import VoiceProfile                 # noqa: E402
from modules.voice_manager import voice_store as vs_mod       # noqa: E402
from modules.tts_engine.engines.voxcpm_adapter import VoxCPMAdapter  # noqa: E402


# 现采自生产库的四条 builtin description（逐字对齐，两两不同）
BUILTIN_DESCS = {
    "std_female_wenrou": "温柔知性的标准女声，适合大多数场景",
    "std_male_cixing": "低沉磁性的标准男声",
    "lively_female_shaonv": "元气满满的少女声线",
    "steady_male_zhongnian": "沉稳大气的中年男声，适合旁白",
}


@pytest.fixture
def db(tmp_path, monkeypatch):
    """临时 sqlite 库 + 把 voice_store 的会话改指过去（绕过生产库）。"""
    engine = create_engine(f"sqlite:///{(tmp_path / 'py363.db').as_posix()}")
    VoiceProfile.__table__.create(bind=engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(vs_mod, "get_session", Session)
    yield Session
    engine.dispose()


def _wav(tmp_path, name):
    """造一个盘上真实存在的文件：装载器只 os.path.exists 判定，不解码音频。
    内容非全零（全零=静音，PY-117 禁止拿静音冒充，即使夹具里也不留这个样子）。"""
    p = tmp_path / name
    p.write_bytes(bytes([(i * 37) % 251 + 1 for i in range(4096)]))
    return p.as_posix()


def _seed_all(Session, tmp_path):
    """插入 4 builtin + 1 空描述 builtin + 对照的 cloned/downloaded/designed 行。"""
    s = Session()
    try:
        # 4 枚 builtin（本缺陷主角）
        for vid, desc in BUILTIN_DESCS.items():
            s.add(VoiceProfile(id=vid, name=vid, source="builtin", engine="voxcpm",
                               language="zh", description=desc))
        # 空描述 builtin：必须不得混进 _designed_voices（判据 ④）
        s.add(VoiceProfile(id="builtin_empty", name="空描述内置", source="builtin",
                           engine="voxcpm", language="zh", description=""))
        # 对照：cloned / downloaded（参考音频存在才装载）
        s.add(VoiceProfile(id="cloned_ok", name="克隆-有音频", source="cloned",
                           engine="voxcpm", language="zh",
                           reference_audio_path=_wav(tmp_path, "cloned_ok.wav"),
                           meta={"transcript": "克隆参考转写"}))
        s.add(VoiceProfile(id="cloned_missing", name="克隆-音频丢失", source="cloned",
                           engine="voxcpm", language="zh",
                           reference_audio_path=(tmp_path / "无此文件.wav").as_posix()))
        s.add(VoiceProfile(id="downloaded_ok", name="下载-有音频", source="downloaded",
                           engine="voxcpm", language="zh",
                           reference_audio_path=_wav(tmp_path, "downloaded_ok.wav")))
        # 对照：designed（原有文本前缀路径）+ 一条空描述 designed（本就不该装载）
        s.add(VoiceProfile(id="designed_ok", name="设计-有描述", source="designed",
                           engine="voxcpm", language="zh",
                           description="清亮的女主播声"))
        s.add(VoiceProfile(id="designed_empty", name="设计-空描述", source="designed",
                           engine="voxcpm", language="zh", description=""))
        s.commit()
    finally:
        s.close()


@pytest.fixture
def restored(tmp_path, db, monkeypatch):
    """装载一次，返回 (adapter, 会话工厂)。"""
    _seed_all(db, tmp_path)
    eng = VoxCPMAdapter(model_path=str(tmp_path), device="cpu")  # 不加载模型，__init__ 很轻
    eng._restore_voices()
    return eng


# ------------------------------------------------------------------ 判据本身

def test_builtin_voices_enter_designed_dict(restored):
    """判据 ①：4 枚 builtin 全部并入 _designed_voices，描述逐字对齐。"""
    for vid, desc in BUILTIN_DESCS.items():
        assert vid in restored._designed_voices, f"builtin {vid} 未进设计档"
        assert restored._designed_voices[vid] == desc


def test_builtin_prompts_pairwise_distinct(restored):
    """判据 ②：走 _compose_request 的文本前缀，四枚 prompt 两两不同（没塌成同一个）。"""
    prompts = {}
    for vid in BUILTIN_DESCS:
        full_text, ref_wav = restored._compose_request("同一句正文", vid, "neutral")
        assert ref_wav is None, f"{vid} 竟走了克隆参考音频入口（错入口）"
        assert full_text.startswith("（"), f"{vid} 未走文本前缀: {full_text!r}"
        prompts[vid] = full_text
    assert len(set(prompts.values())) == len(BUILTIN_DESCS), \
        f"builtin prompt 发生塌缩: {prompts}"


def test_cloned_and_designed_do_not_drift(restored):
    """判据 ③：cloned/downloaded/designed 原有行为不漂（数量与内容不变）。"""
    # 克隆档只认存在参考音频的行：cloned_ok + downloaded_ok，missing 不进来
    assert set(restored._cloned_voices) == {"cloned_ok", "downloaded_ok"}, \
        restored._cloned_voices
    assert restored._cloned_voices["cloned_ok"][1] == "克隆参考转写"
    # 原 designed 行仍在设计档，且走参考音频为 None（文本前缀路径）
    assert "designed_ok" in restored._designed_voices
    assert restored._designed_voices["designed_ok"] == "清亮的女主播声"
    # 空描述 designed 本就不装载 —— 修复后也不该被 builtin 逻辑误收
    assert "designed_empty" not in restored._designed_voices
    # builtin 不得污染克隆档
    assert not (set(BUILTIN_DESCS) & set(restored._cloned_voices))


def test_empty_description_builtin_excluded(restored):
    """判据 ④：description 为空的 builtin 行不得混进 _designed_voices。"""
    assert "builtin_empty" not in restored._designed_voices
    assert "builtin_empty" not in restored._cloned_voices
