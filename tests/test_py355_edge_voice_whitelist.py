"""PY-355：Edge 适配器的"合法 shortName"判据不能是写死的 12 名清单。

定罪现场（GET /api/voices/{uuid}/preview?regenerate=1，两枚粤语音色必 502）：

    RuntimeError: voice_id 'zh-HK-HiuMaanNeural' 不是 Edge 音色库中的有效音色
    （uuid 映射缺失且不是 Edge shortName）—— 拒绝静默退化为默认音色

两层根因（`modules/tts_engine/engines/edgetts_adapter.py` `_try_resolve_voice`）：
① 试听速度档先把音色行解析成 Edge shortName 再交给引擎
   （`backend/services/voice_preview.py:528-531`，`select_edge_voice(row)` →
   `engine.synthesize(voice_id=edge_voice)`），而合法名判定只认 uuid 映射的键 +
   写死的 `EDGE_ZH_VOICES` 12 名 ⇒ DB `meta.edge_voice` 里清单外的合法名
   （zh-HK-HiuGaaiNeural / zh-HK-HiuMaanNeural，均在 edge 真目录 322 名内）被自己拒了；
② "直传合法 shortName"放行分支要求 `":" in voice_id`，而真目录 322 名含冒号的有 0 名
   ⇒ 该支对任何真实 Edge 音色名恒不命中（死分支，须删）。

修法口径：第二张合法表 = `_uuid_to_edge` 的 **value 集合**（`_restore_edge_voices`
从 DB `meta.edge_voice` 全量读入 —— 离线、零网络、与 uuid 同源）；`EDGE_ZH_VOICES`
保留为种子目录兜底（PY-360 的桩库只有 1 行时 12 名仍须可解析，行为不漂）；
两张表都不认的名字**照旧抛错**（PY-117"拒绝静默退化为默认音色"不许拆）。

本文件不打网络：`_fetch_mp3`/`_decode_mp3` 全部桩掉；DB 用 `voice_store.list_all`
打桩（复刻生产库 14 行 edgetts 的映射，id/shortName 抄自 2026-09-26 只读实测）。
"""
import json
import os
import sys
import types

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from backend.services import voice_preview as vp                  # noqa: E402
from modules.tts_engine.engines import edgetts_adapter as ea      # noqa: E402
from modules.voice_manager import voice_store as vs_mod           # noqa: E402
from tests.test_voice_preview_fast import _row, _speech_like      # noqa: E402

# 生产库里落在 12 名清单之外的两行（uuid -> meta.edge_voice，只读 URI 实测抄录）
HIU_GAAI_UUID = "b4342575-7e64-585a-bbce-ca3166241bd5"
HIU_MAAN_UUID = "f8ba139a-2a94-534b-8076-bc16b70f8503"
OUTSIDE_LIST = {HIU_GAAI_UUID: "zh-HK-HiuGaaiNeural",
                HIU_MAAN_UUID: "zh-HK-HiuMaanNeural"}


def _mk_row(vid, edge_voice):
    return types.SimpleNamespace(id=vid, name=f"Edge-{edge_voice}",
                                 engine="edgetts", gender="female",
                                 meta={"edge_voice": edge_voice})


def _stub_store(monkeypatch, rows):
    """voice_store.list_all 的桩：只回 engine='edgetts' 的行（与引擎侧调用口径一致）。"""
    def list_all(page_size=500, **filters):
        eng = filters.get("engine")
        return [r for r in rows if eng in (None, r.engine)]
    monkeypatch.setattr(vs_mod.voice_store, "list_all", list_all)
    return rows


@pytest.fixture
def prod_like_store(monkeypatch):
    """复刻生产库 14 行：12 名清单行 + 2 名清单外粤语行（uuid -> shortName 同源映射）。"""
    rows = ([_mk_row(vid, ev) for vid, ev in OUTSIDE_LIST.items()]
            + [_mk_row(f"u-{i:02d}", v["voice"])
               for i, v in enumerate(ea.EDGE_ZH_VOICES)])
    return _stub_store(monkeypatch, rows)


@pytest.fixture
def silent_probe(monkeypatch, tmp_path):
    """把试听的缓存目录指到 tmp_path，并记录真正送进 edge 通讯层的音色名。"""
    monkeypatch.setenv("VOICE_PREVIEW_DIR", str(tmp_path / "previews_generated"))
    vp.reset_jobs()
    yield []
    vp.reset_jobs()


def _offline_adapter(monkeypatch, voice_log):
    """真 EdgeTTSAdapter，但网络层被桩死：_fetch_mp3 只记 voice，_decode_mp3 出类语音。"""
    eng = ea.EdgeTTSAdapter()
    eng.load_model()

    async def fake_fetch(text, voice, rate, pitch):
        voice_log.append(voice)
        return b"stub-mp3"

    monkeypatch.setattr(eng, "_fetch_mp3", fake_fetch)
    monkeypatch.setattr(eng, "_decode_mp3",
                        lambda mp3: (_speech_like(), 24000))
    return eng


def _drive_fast(job_row, eng, monkeypatch):
    """走 regenerate 速度档的真实解析路径：_generate_fast → select_edge_voice → synthesize。"""
    monkeypatch.setattr(vp, "_load_engine", lambda name: eng)
    monkeypatch.setattr(vp, "_load_row", lambda vid: job_row)
    job = vp._Job(job_row.id, "fast", vp.FAST_BUDGET_S)
    return vp._generate_fast(job, job_row)


# ================================================= ① 定罪主案：两名清单外粤语行
class TestRegenerateFastPathAcceptsNativeVoiceOutsideWhitelist:
    @pytest.mark.parametrize("uuid,short", sorted(OUTSIDE_LIST.items()))
    def test_generate_fast_no_longer_raises_for_outside_list_voice(
            self, uuid, short, prod_like_store, silent_probe, monkeypatch):
        row = _row(vid=uuid, name=f"Edge-{short}", engine="edgetts",
                   meta={"edge_voice": short})
        eng = _offline_adapter(monkeypatch, silent_probe)
        out = _drive_fast(row, eng, monkeypatch)
        assert out.get("ok") is True, out                       # 修复前: synthesis_failed
        assert out["voice"] == short                            # 元数据如实标出真嗓子
        assert short in silent_probe                            # 送进通讯层的不是默认声
        assert ea.DEFAULT_EDGE_VOICE not in silent_probe

    def test_resolve_voice_accepts_value_set_shortname(self, prod_like_store):
        """第二张表（uuid 映射的 value 集合）本身必须放行，不退化成默认。"""
        eng = ea.EdgeTTSAdapter()
        for short in OUTSIDE_LIST.values():
            assert eng._resolve_voice(short) == short
            assert eng.resolves_voice(short) is True


# ================================================= ② 清单内 12 名行为不漂
class TestBuiltinTwelveNamesUnchanged:
    def test_all_twelve_resolve_to_themselves(self, prod_like_store):
        eng = ea.EdgeTTSAdapter()
        for spec in ea.EDGE_ZH_VOICES:
            name = spec["voice"]
            assert eng.resolves_voice(name) is True
            assert eng._resolve_voice(name) == name

    def test_all_twelve_resolve_even_with_empty_store(self, monkeypatch):
        """种子目录兜底：库里一行都没有（进程启动早于 seed）也不许漂。"""
        _stub_store(monkeypatch, [])
        eng = ea.EdgeTTSAdapter()
        for spec in ea.EDGE_ZH_VOICES:
            assert eng._resolve_voice(spec["voice"]) == spec["voice"]


# ================================================= ③ 守卫不拆：谁都不在必须抛错
class TestGuardRejectsUnknownNames:
    BAD = ["zh-CN-NotAVoiceNeural",            # 定罪要求的探针名
           "zh-HK-NonsenseNeural",             # 粤语前缀也蒙不住
           "zh-CN:NotReal", "Hello:Neural",    # 冒号形 = 原死分支曾放行的畸形名
           " "]                                 # 空串是"显式默认档"口径，见 ④ 用例

    def test_unknown_names_raise_with_no_silent_degrade(self, prod_like_store):
        eng = ea.EdgeTTSAdapter()
        for bad in self.BAD:
            assert eng.resolves_voice(bad) is False, bad
            with pytest.raises(RuntimeError) as ei:
                eng._resolve_voice(bad)
            msg = str(ei.value)
            assert "不是 Edge 音色库中的有效音色" in msg
            assert "拒绝静默退化" in msg
            assert ea.DEFAULT_EDGE_VOICE not in msg             # 错误里也不许偷给默认声

    def test_dead_colon_branch_removed(self, prod_like_store):
        """`":" in voice_id and endswith("Neural")` 曾把畸形名当合法直传 —— 必须已删。"""
        eng = ea.EdgeTTSAdapter()
        with pytest.raises(RuntimeError):
            eng._resolve_voice("zh-CN-XiaoxiaoNeural:Neural")

    def test_generate_fast_surfaces_rejection_not_default_voice(
            self, prod_like_store, silent_probe, monkeypatch):
        """脏 meta（两表都不认）在试听路径必须报 synthesis_failed，且引擎根本没被叫。"""
        row = _row(vid="v-dirty", name="坏映射行", engine="edgetts",
                   meta={"edge_voice": "zh-CN-NotAVoiceNeural"})
        eng = _offline_adapter(monkeypatch, silent_probe)
        out = _drive_fast(row, eng, monkeypatch)
        assert out.get("ok") is not True
        assert out["code"] == "synthesis_failed"
        assert "拒绝静默退化" in out["message"]
        assert silent_probe == []                               # 没拿任何嗓子去发声


# ================================================= ④ uuid 传法（工作流路）不漂
class TestUuidPathUnchanged:
    def test_mapped_uuids_still_win(self, prod_like_store):
        eng = ea.EdgeTTSAdapter()
        assert eng._resolve_voice(HIU_MAAN_UUID) == "zh-HK-HiuMaanNeural"
        assert eng._resolve_voice(HIU_GAAI_UUID) == "zh-HK-HiuGaaiNeural"
        for i, spec in enumerate(ea.EDGE_ZH_VOICES):
            assert eng._resolve_voice(f"u-{i:02d}") == spec["voice"]

    def test_unknown_uuid_still_rejected(self, prod_like_store):
        eng = ea.EdgeTTSAdapter()
        assert eng.resolves_voice("00000000-0000-0000-0000-000000000000") is False
        with pytest.raises(RuntimeError):
            eng._resolve_voice("00000000-0000-0000-0000-000000000000")

    def test_none_falls_to_engine_default_explicitly(self, prod_like_store):
        """None/空 = 上层**显式**要默认档（与 PY-360 空池代打同一条口径），不是守卫破口。"""
        eng = ea.EdgeTTSAdapter()
        assert eng._resolve_voice(None) == ea.DEFAULT_EDGE_VOICE
        assert eng._resolve_voice("") == ea.DEFAULT_EDGE_VOICE
