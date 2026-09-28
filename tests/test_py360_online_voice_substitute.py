"""PY-360：在线档换引擎发声时，喂给引擎的 voice_id 必须一起换。

定罪现场（2026-09-26 整书实测，任务 e066debe-2df4-4e3f-bbe4-203acf54b84e，第 1/5070 句）：

    [P7] 在线档下 1 枚音色绑的是本地引擎（['voxcpm']），已改由 'edgetts' 发声
    第 1/5070 句合成失败(engine=edgetts, text='第一章 严重的时刻'):
    RuntimeError: voice_id 'ed868964-ddd5-5406-8f05-a1381a2573c8' 不是 Edge 音色库中的
    有效音色（uuid 映射缺失且不是 Edge shortName）—— 拒绝静默退化为默认音色

降级门（`conversion_workflow._stage_synthesize`）只改了 `cond["engine"]`，句子上的
voice_id 仍是本地音色行的 uuid；而 Edge 适配器按 PY-117"拒绝静默退化为默认音色"的
纪律对认不出的 uuid 直接抛错 —— 于是整本书在第一句就死。

既有单测 `test_dual_mode_tier.py` 没抓到这件事，原因是它的引擎桩对任何 voice_id 都
照收 —— 桩比真引擎宽容，等于没测。本文件的桩复刻 `edgetts_adapter._resolve_voice`
的判据（只认自己库里的 uuid 或 None），所以这条线是真的能红。

并发路径下引擎桩的记录顺序不保证（句子在线程池里交错），所以断言一律用集合/计数与
`context["tier_voice_substitutions"]` 的映射，不按 calls 的位置。

代打的触发判据是"改派后的引擎自己说认不出这枚 uuid"（`BaseTTSEngine.resolves_voice`，
Edge 覆盖之）。所以 `test_dual_mode_tier.py` / `test_language_tier.py` 里那些不实现这个
口的宽松桩不会被换嗓子 —— 它们继续按原样绿，本文件才是那条真红线的所在地。
"""
import collections
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from backend.config import Config                                   # noqa: E402
from modules.tts_engine.engine_registry import engine_registry      # noqa: E402
from modules.voice_manager import voice_store as vs_mod             # noqa: E402
from modules.workflow import cast_pipeline_hooks as hooks_mod       # noqa: E402
from modules.workflow import conversion_workflow as wmod            # noqa: E402
from tests.test_voice_preview_fast import _row                      # noqa: E402

ONLINE_POOL = [
    _row(vid="e-male-1", name="Edge-云希(男·青年)", engine="edgetts", gender="male"),
    _row(vid="e-male-2", name="Edge-云扬(男·新闻)", engine="edgetts", gender="male"),
    _row(vid="e-female-1", name="Edge-晓晓(女·温暖)", engine="edgetts",
         gender="female"),
    # 一枚"本来就绑在线音色"的行：它既是 edgetts 库的成员，也是 passthrough 用例的主角
    _row(vid="v-edge-real", name="Edge-晓伊(女·活泼)", engine="edgetts",
         gender="female"),
]
# voice_store.get 的假库（按 id 取行）：既有本地行也有在线行，与真库一样混着
ROWS_BY_ID = {
    "v-mature-m": _row(vid="v-mature-m", name="人声语音-成熟男声 #011",
                       engine="voxcpm", gender=None),
    "v-male-2": _row(vid="v-male-2", name="本地男声二", engine="voxcpm",
                     gender="male"),
    "v-male-3": _row(vid="v-male-3", name="本地男声三", engine="voxcpm",
                     gender="male"),
    "v-warm-f": _row(vid="v-warm-f", name="本地温柔女声", engine="voxcpm",
                     gender="female"),
    "v-edge-real": next(v for v in ONLINE_POOL if v.id == "v-edge-real"),
}
POOL_IDS = {v.id for v in ONLINE_POOL}


class _EngineStub:
    """只认自己库里的 voice_id（uuid 或 None），否则抛错 —— 与真 Edge 适配器同一条纪律。"""

    def __init__(self, name, online, library, sample_rate=24000):
        self.engine_name = name
        self.online = online
        self.sample_rate = sample_rate
        self.library = set(library)
        self.calls = []

    def resolves_voice(self, voice_id=None):
        return voice_id is None or voice_id in self.library

    def synthesize(self, text=None, voice_id=None, **kw):
        if voice_id is not None and voice_id not in self.library:
            raise RuntimeError(
                f"voice_id {voice_id!r} 不是 {self.engine_name} 音色库中的有效音色"
                f"（uuid 映射缺失且不是 {self.engine_name} shortName）"
                f" —— 拒绝静默退化为默认音色")
        self.calls.append(voice_id)
        return np.full(12000, 0.4, dtype=np.float32)


async def _drive(monkeypatch, settings, vids, pool=ONLINE_POOL, reject=(), rows=None):
    """跑一次真实的 P7（`_stage_synthesize`），返回 (context, 引擎桩表, 任务日志)。

    `reject` 里的 id 算"引擎库里有这行、但这台引擎认不出"（真实现里对应
    meta.edge_voice 缺失那一格），用来验代打候选会二次过引擎判据。
    `rows` 覆盖 voice_store.get 的假库（PY-361 用真书那一枚旁白行作案发现场）。
    """
    store = rows if rows is not None else ROWS_BY_ID
    fakes = {
        "edgetts": _EngineStub("edgetts", True,
                               [v.id for v in pool if v.id not in set(reject)]),
        # 本地引擎按其真实现口径：所有音色行都恢复得进来（voxcpm._restore_voices）
        "voxcpm": _EngineStub("voxcpm", False, sorted(store) + [v.id for v in pool]),
    }
    monkeypatch.setattr(engine_registry, "get", lambda name: fakes.get(name))
    monkeypatch.setattr(engine_registry, "get_loaded",
                        lambda name, strict=True: fakes[name])
    monkeypatch.setattr(engine_registry, "is_known_engine",
                        lambda name: bool(name) and name in fakes)
    monkeypatch.setattr(vs_mod.voice_store, "get", lambda vid: store.get(vid))
    monkeypatch.setattr(vs_mod.voice_store, "list_all",
                        lambda page_size=500, **filters: [
                            v for v in pool
                            if filters.get("engine") in (None, v.engine)])
    monkeypatch.setattr(hooks_mod, "synth_params_for_sentence",
                        lambda sent, s: {"speed": 1.0, "pitch": 0.0})
    monkeypatch.setattr(Config, "TTS_TIER", "online", raising=False)
    monkeypatch.setattr(Config, "TTS_ENGINE", "edgetts", raising=False)

    cw = wmod.ConversionWorkflow()
    logged = []
    monkeypatch.setattr(cw, "_append_task_log",
                        lambda tid, stage, message="", extra=None, **k:
                        logged.append((stage, message, extra)))
    monkeypatch.setattr(cw, "is_cancelled", lambda tid: False)

    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(cw, "_emit", _noop)
    ctx = {"task_id": "t-py360",
           "sentences": [{"text": f"第{i}句念白内容够长可以理解",
                          "voice_id": vid, "chapter_id": "c1"}
                         for i, vid in enumerate(vids)],
           "settings": dict(settings)}
    await cw._stage_synthesize(ctx, dict(settings))
    return ctx, fakes, logged


def _subs(ctx):
    return {d["from"]: d["to"] for d in ctx.get("tier_voice_substitutions", [])}


# ------------------------------------------------ 1) 主案：在线档 + 本地音色不再炸
class TestOnlineTierSubstitutesVoice:
    async def test_local_voice_no_longer_crashes_the_online_tier(self, monkeypatch):
        """整书第一句的定罪用例：本地 uuid 不许再交给在线引擎。"""
        ctx, fakes, logged = await _drive(monkeypatch, {"tier": "online"},
                                          ["v-mature-m"])
        assert fakes["voxcpm"].calls == []
        assert len(fakes["edgetts"].calls) == 1
        voiced = fakes["edgetts"].calls[0]
        assert voiced != "v-mature-m"                  # 换引擎=连嗓子一起换
        assert voiced in POOL_IDS                      # 必须在目标引擎的库里
        assert len(ctx["audio_arrays"]) == 1

    async def test_substitution_is_reported_with_who_replaced_whom(self, monkeypatch):
        """换了谁的嗓子必须写进任务日志（红线②：降级要可归因），且对得上代打行。"""
        ctx, fakes, logged = await _drive(monkeypatch, {"tier": "online"},
                                          ["v-mature-m"])
        msgs = [m for _, m, _ in logged]
        assert any("音色身份未保留" in m for m in msgs)
        sub_id = _subs(ctx)["v-mature-m"]
        sub_name = next(v.name for v in ONLINE_POOL if v.id == sub_id)
        assert any(sub_name in m for m in msgs), msgs
        assert ctx["tier_voice_downgraded"] == 1
        assert ctx["tier_voice_substituted"] == 1

    async def test_genuine_online_voice_passes_through_untouched(self, monkeypatch):
        """本来就绑在线音色的角色：不许被"代打"逻辑误伤。"""
        ctx, fakes, logged = await _drive(monkeypatch, {"tier": "online"},
                                          ["v-edge-real"])
        assert fakes["edgetts"].calls == ["v-edge-real"]
        assert ctx["tier_voice_substituted"] == 0
        assert ctx["tier_voice_downgraded"] == 0
        assert logged == []

    async def test_local_tier_keeps_the_local_uuid(self, monkeypatch):
        ctx, fakes, logged = await _drive(monkeypatch, {"tier": "local"},
                                          ["v-mature-m"])
        assert fakes["voxcpm"].calls == ["v-mature-m"]
        assert fakes["edgetts"].calls == []
        assert ctx["tier_voice_substituted"] == 0


# ------------------------------------------------ 2) 多角色不能全变成同一把嗓子
class TestRolesStayDistinct:
    async def test_two_downgraded_roles_get_two_different_voices(self, monkeypatch):
        ctx, fakes, logged = await _drive(monkeypatch, {"tier": "online"},
                                          ["v-mature-m", "v-warm-f"])
        assert set(fakes["edgetts"].calls) <= POOL_IDS
        assert len(set(fakes["edgetts"].calls)) == 2
        assert ctx["tier_voice_substituted"] == 2

    async def test_gender_matches_when_the_pool_has_it(self, monkeypatch):
        """女角色代打女声（性别缺位的行按轮转兜底，见 test_two_downgraded…）。"""
        ctx, fakes, logged = await _drive(monkeypatch, {"tier": "online"},
                                          ["v-warm-f"])
        assert fakes["edgetts"].calls == ["e-female-1"]

    async def test_same_gender_roles_do_not_collapse_to_one_voice(self, monkeypatch):
        """三枚男角色 + 库里两枚男声：先各给一把，用完才复用。"""
        ctx, fakes, logged = await _drive(monkeypatch, {"tier": "online"},
                                          ["v-mature-m", "v-male-2", "v-male-3"])
        male_ids = {"e-male-1", "e-male-2"}
        used = collections.Counter(fakes["edgetts"].calls)
        assert set(used) <= male_ids
        assert len(used) == 2, used
        assert sum(used.values()) == 3

    async def test_mapping_is_deterministic_across_runs(self, monkeypatch):
        """同一本书两次跑必须得到同一批嗓子：代打选择不得依赖集合迭代顺序。"""
        vids = ["v-mature-m", "v-warm-f", "v-male-2", "v-male-3"]
        ctx1, _, _ = await _drive(monkeypatch, {"tier": "online"}, vids)
        ctx2, _, _ = await _drive(monkeypatch, {"tier": "online"}, list(reversed(vids)))
        assert _subs(ctx1) == _subs(ctx2)


# ------------------------------------------------ 3) 代打候选本身也要过引擎判据
class TestEmptyOnlinePool:
    async def test_empty_pool_falls_back_to_engine_default_and_says_so(self, monkeypatch):
        """库里一枚在线音色都没有：交 None（引擎默认音色），不许把本地 uuid 递过去。"""
        ctx, fakes, logged = await _drive(monkeypatch, {"tier": "online"},
                                          ["v-mature-m"], pool=[])
        assert fakes["edgetts"].calls == [None]
        assert ctx["tier_voice_substituted"] == 1
        assert _subs(ctx)["v-mature-m"] is None
        msgs = [m for _, m, _ in logged]
        assert any("无可选音色" in m for m in msgs), msgs

    async def test_candidate_the_engine_rejects_is_skipped(self, monkeypatch):
        """音色行在库里、引擎却认不出（meta.edge_voice 缺失那一格）：换下一枚候选。"""
        ctx, fakes, logged = await _drive(monkeypatch, {"tier": "online"},
                                          ["v-warm-f"], reject=("e-female-1",))
        assert fakes["edgetts"].calls == ["v-edge-real"]
        assert _subs(ctx)["v-warm-f"] == "v-edge-real"


# ------------------------------------------------ 4) 真适配器自己得答得出这句话
class TestEdgeAdapterAnswersForItself:
    """工作流问的是引擎的 `resolves_voice`，所以真 Edge 适配器必须答得和它实际
    会抛错的 `_resolve_voice` 同一条判据 —— 两条判据分叉时，换完嗓子还是会死。"""

    def _adapter(self, monkeypatch, rows):
        from modules.tts_engine.engines import edgetts_adapter as ea
        monkeypatch.setattr(vs_mod.voice_store, "list_all",
                            lambda page_size=500, **filters: rows)
        return ea.EdgeTTSAdapter()

    def test_the_two_arms_never_disagree(self, monkeypatch):
        mapped = _row(vid="u-mapped", engine="edgetts",
                      meta={"edge_voice": "zh-CN-XiaoxiaoNeural"})
        orphan = _row(vid="u-orphan", engine="edgetts", meta={})   # 有行但没映射
        eng = self._adapter(monkeypatch, [mapped])
        assert eng.resolves_voice("u-orphan") is False
        assert eng.resolves_voice("u-mapped") is True
        assert eng.resolves_voice("zh-CN-YunxiNeural") is True
        assert eng.resolves_voice(None) is True
        for vid in ("u-mapped", "zh-CN-YunxiNeural", None):
            assert eng._resolve_voice(vid)                 # 认的都不抛
        for vid in ("u-orphan", "00000000-0000-0000-0000-000000000000"):
            with pytest.raises(RuntimeError):
                eng._resolve_voice(vid)


# ------------------------------------------------ 5) PY-361 代打选的是哪一把嗓子
"""PY-361（2026-09-26 整书实测第二格，任务 3fbb2fbe-de73-4ef3-a73a-f2fc29e9d487）：

不炸了，但选错了嗓子。那一本书只有 1 个角色（旁白），绑的是本地音色
`ed868964-ddd5-5406-8f05-a1381a2573c8`「人声语音-成熟男声 #011」（voxcpm，
gender 列为空，audio_language_tier=mandarin_ref）。在线档代打时日志记的是：

    代打嗓子: 人声语音-成熟男声 #011->Edge-zh-HK-HiuMaanNeural

也就是**普通话男旁白换成了粤语女声**，5070 句全书照此念完。两条根因：
① 性别优先门读的是 `gender` 列，而那列在生产库里 1007 枚 voxcpm 行有 539 枚是空的
   （其中 73 枚名字里明写男/女）—— 空值让"同性别优先"整段失效；
② 池子顺序取的是 `voice_store.list_all` 的库序，里面混着 3 枚粤语行
   （zh-HK-* / 圓龍），一旦落到无性别偏好的分支就可能被选中。

修法口径：代打候选按 (非粤语, 性别对上, vid) 排序后轮转 —— 是**排序不是过滤**，
所以整池只有粤语时仍能选出嗓子（见 test_cantonese_…when_it_is_all_the_pool_has）。
"""

# 逐行抄自 2026-09-26 生产库 voice_profiles(engine='edgetts')，14 枚（id 取前 8 位）。
# **顺序也照抄**：voice_store.list 是 `created_at desc, id desc`，所以 2026-09-22 那两枚
# zh-HK 目录导入行排在整池最前 —— 这就是代打抓错嗓子的直接原因（不排序=抓最新导入的那枚）。
PROD_EDGE_POOL = [
    _row(vid="f8ba139a", name="Edge-zh-HK-HiuMaanNeural", engine="edgetts",
         gender="female", meta={"edge_voice": "zh-HK-HiuMaanNeural"}),
    _row(vid="b4342575", name="Edge-zh-HK-HiuGaaiNeural", engine="edgetts",
         gender="female", meta={"edge_voice": "zh-HK-HiuGaaiNeural"}),
    _row(vid="a1ff4dfb", name="Edge-晓北(东北女声)", engine="edgetts", gender="female",
         meta={"edge_voice": "zh-CN-liaoning-XiaobeiNeural"}),
    _row(vid="77cf7b16", name="Edge-圓龍(粵語男声)", engine="edgetts", gender="male",
         meta={"edge_voice": "zh-HK-WanLungNeural"}),
    _row(vid="4d2fd55e", name="Edge-曉雨(台式女声)", engine="edgetts", gender="female",
         meta={"edge_voice": "zh-TW-HsiaoYuNeural"}),
    _row(vid="2f232cbe", name="Edge-晓妮(陕西女声)", engine="edgetts", gender="female",
         meta={"edge_voice": "zh-CN-shaanxi-XiaoniNeural"}),
    _row(vid="e5052e22", name="Edge-云健(男·磁性)", engine="edgetts", gender="male",
         meta={"edge_voice": "zh-CN-YunjianNeural"}),
    _row(vid="a33b7033", name="Edge-晓伊(女·活泼)", engine="edgetts", gender="female",
         meta={"edge_voice": "zh-CN-XiaoyiNeural"}),
    _row(vid="8a8ee3d7", name="Edge-晓晓(女·温暖)", engine="edgetts", gender="female",
         meta={"edge_voice": "zh-CN-XiaoxiaoNeural"}),
    _row(vid="7b8aa1df", name="Edge-云希(男·青年)", engine="edgetts", gender="male",
         meta={"edge_voice": "zh-CN-YunxiNeural"}),
    _row(vid="6f391802", name="Edge-云夏(童声)", engine="edgetts", gender="male",
         meta={"edge_voice": "zh-CN-YunxiaNeural"}),
    _row(vid="4f822ae6", name="Edge-曉臻(台式女声)", engine="edgetts", gender="female",
         meta={"edge_voice": "zh-TW-HsiaoChenNeural"}),
    _row(vid="2599ce18", name="Edge-云扬(男·新闻)", engine="edgetts", gender="male",
         meta={"edge_voice": "zh-CN-YunyangNeural"}),
    _row(vid="12196a93", name="Edge-雲澤(台式男声)", engine="edgetts", gender="male",
         meta={"edge_voice": "zh-TW-YunJheNeural"}),
]
CANTONESE_IDS = {"77cf7b16", "b4342575", "f8ba139a"}
# 真书那一枚旁白行：gender 列为空，只有名字写着"男声"
NARRATOR_ROW = _row(vid="ed868964", name="人声语音-成熟男声 #011",
                    engine="voxcpm", gender=None)
HEROINE_ROW = _row(vid="aa11aa11", name="人声语音-温柔女声 #077",
                   engine="voxcpm", gender=None)
AMBIGUOUS_ROW = _row(vid="bb22bb22", name="人声语音-磁性 #123",
                     engine="voxcpm", gender=None)
PROD_ROWS = {r.id: r for r in (NARRATOR_ROW, HEROINE_ROW, AMBIGUOUS_ROW)}


def _row_by_id(vid, pool=PROD_EDGE_POOL):
    return next(v for v in pool if v.id == vid)


class TestSubstituteVoiceChoice:
    async def test_mandarin_narrator_is_not_handed_a_cantonese_voice(self, monkeypatch):
        """定罪用例：普通话男旁白的代打嗓子不许是粤语行，也不许是女声。"""
        ctx, fakes, logged = await _drive(monkeypatch, {"tier": "online"},
                                          [NARRATOR_ROW.id],
                                          pool=PROD_EDGE_POOL, rows=PROD_ROWS)
        chosen = _subs(ctx)[NARRATOR_ROW.id]
        assert chosen is not None and chosen not in CANTONESE_IDS, chosen
        row = _row_by_id(chosen)
        assert row.gender == "male"
        assert fakes["edgetts"].calls == [chosen]

    async def test_gender_is_read_from_the_name_when_the_column_is_empty(self, monkeypatch):
        """gender 列为空不等于没有证据：名字里的"男声/女声"要用上，两枚角色不能同音。"""
        ctx, fakes, logged = await _drive(monkeypatch, {"tier": "online"},
                                          [NARRATOR_ROW.id, HEROINE_ROW.id],
                                          pool=PROD_EDGE_POOL, rows=PROD_ROWS)
        m = _subs(ctx)
        assert _row_by_id(m[NARRATOR_ROW.id]).gender == "male"
        assert _row_by_id(m[HEROINE_ROW.id]).gender == "female"
        assert m[NARRATOR_ROW.id] != m[HEROINE_ROW.id]

    async def test_name_without_gender_claim_still_gets_a_voice(self, monkeypatch):
        """名字里既无男也无女：不猜性别，但仍要选出一把能发声的嗓子（非粤语优先）。"""
        ctx, fakes, logged = await _drive(monkeypatch, {"tier": "online"},
                                          [AMBIGUOUS_ROW.id],
                                          pool=PROD_EDGE_POOL, rows=PROD_ROWS)
        chosen = _subs(ctx)[AMBIGUOUS_ROW.id]
        assert chosen is not None and chosen not in CANTONESE_IDS

    async def test_cantonese_is_still_used_when_it_is_all_the_pool_has(self, monkeypatch):
        """排序不是过滤：整池只剩粤语行时照样出嗓子，不许退化成默认音色。"""
        pool = [_row_by_id("f8ba139a")]
        ctx, fakes, logged = await _drive(monkeypatch, {"tier": "online"},
                                          [NARRATOR_ROW.id], pool=pool, rows=PROD_ROWS)
        assert _subs(ctx)[NARRATOR_ROW.id] == "f8ba139a"
        assert fakes["edgetts"].calls == ["f8ba139a"]

    async def test_locale_prefix_disqualifies_a_row_whose_name_says_nothing(self, monkeypatch):
        """名字里没有"粤"字的那两枚目录行，靠 `meta.edge_voice` 的 zh-HK 前缀出局。

        两枚候选性别都缺、vid 顺序又把粤语行排在前面 —— 只有读 locale 才会选对，
        所以这条用例钉的是"locale 这一路判据真的在起作用"。
        """
        pool = [
            _row(vid="a-aaa", name="Edge-A", engine="edgetts", gender=None,
                 meta={"edge_voice": "zh-HK-HiuMaanNeural"}),
            _row(vid="b-bbb", name="Edge-B", engine="edgetts", gender=None,
                 meta={"edge_voice": "zh-CN-XiaoxiaoNeural"}),
        ]
        ctx, fakes, logged = await _drive(monkeypatch, {"tier": "online"},
                                          [AMBIGUOUS_ROW.id], pool=pool, rows=PROD_ROWS)
        assert _subs(ctx)[AMBIGUOUS_ROW.id] == "b-bbb"

    async def test_choice_is_the_same_book_twice(self, monkeypatch):
        vids = [NARRATOR_ROW.id, HEROINE_ROW.id, AMBIGUOUS_ROW.id]
        c1, _, _ = await _drive(monkeypatch, {"tier": "online"}, vids,
                                pool=PROD_EDGE_POOL, rows=PROD_ROWS)
        c2, _, _ = await _drive(monkeypatch, {"tier": "online"}, list(reversed(vids)),
                                pool=PROD_EDGE_POOL, rows=PROD_ROWS)
        assert _subs(c1) == _subs(c2)

    async def test_rotation_counts_per_gender_not_across_the_whole_pool(self, monkeypatch):
        """各性别的轮转各数各的：女角色吃掉一个位次，不许把男角色挤回同一把嗓子。

        定罪形态（变异体 M9：把轮转键从 `(引擎, 性别)` 改成 `(引擎,)`）：
        池子 2 男 1 女，三个角色按 男-女-男 顺序分配 —— 共用计数器时第三枚男角色
        数到的是位次 2（%2=0），与第一枚男角色撞成同一把嗓子，而 m-2 还空着。
        """
        pool = [
            _row(vid="f-1", name="池女一", engine="edgetts", gender="female"),
            _row(vid="m-1", name="池男一", engine="edgetts", gender="male"),
            _row(vid="m-2", name="池男二", engine="edgetts", gender="male"),
        ]
        rows = {
            "s1": _row(vid="s1", name="本地-稳健男声", engine="voxcpm", gender=None),
            "s2": _row(vid="s2", name="本地-清亮女声", engine="voxcpm", gender=None),
            "s3": _row(vid="s3", name="本地-低沉男声", engine="voxcpm", gender=None),
        }
        ctx, fakes, logged = await _drive(monkeypatch, {"tier": "online"},
                                          ["s1", "s2", "s3"], pool=pool, rows=rows)
        m = _subs(ctx)
        assert m["s1"] != m["s3"], m
        assert {m["s1"], m["s3"]} == {"m-1", "m-2"}, m
        assert m["s2"] == "f-1", m

