"""PY-323(a′) —— 参考音频**语种档**：分类器、分发门与两处接线的单元验证。

判据来源（不是猜的）：`agent-ops/py323/REPORT.md` —— openvoice 的字准瓶颈是跨语种
SE × converter 交叉项（普通话参考 tpsim 0.917~1.000，英文参考 0.500~0.917，
生产 6 枚复判 5 枚不合格），所以按档降级是**分发**修复，不是调参修复。

这里只钉四件事：
  1) 分类器两条来源（声明 / 声学）的档位与置信地板；
  2) 分发门只在"openvoice + 非普通话参考"这一格动手，其余一律原样通过（含 NULL）；
  3) 三处真实接线（试听高档 `_generate_high` / 整书 P5 路由表 / cast 角色试听）走的是
     同一判据，且降级**在产物元数据或响应头里可查**——不许静默换嗓子。
     cast 试听只在"全局 `TTS_ENGINE` 恰为 openvoice"时才会命中那一格；
  4) 克隆建行时语种档随声明写入，且带上 basis=declared（离线工具据此区分）。
"""
import json
import os
import sys
import types

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from modules.voice_manager import language_tier as lt          # noqa: E402
from backend.services import voice_preview as vp               # noqa: E402
from tests.test_voice_preview_fast import FakeEngine, _speech_like, _row  # noqa: E402


def _tier_row(engine="openvoice", tier=None, **kw):
    row = _row(engine=engine, **kw)
    row.audio_language_tier = tier
    return row


@pytest.fixture
def cache_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("VOICE_PREVIEW_DIR", str(tmp_path / "previews_generated"))
    vp.reset_jobs()
    yield tmp_path / "previews_generated"
    vp.reset_jobs()


def _meta_of(path):
    return json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))


# ================================================= 1) 分类器
class TestTierClassifier:
    @pytest.mark.parametrize("declared,want", [
        ("zh", lt.TIER_MANDARIN), ("zh-CN", lt.TIER_MANDARIN),
        ("ZH_HANS", lt.TIER_MANDARIN), ("cmn", lt.TIER_MANDARIN),
        ("zh_CN", lt.TIER_MANDARIN), ("zh-Hant", lt.TIER_MANDARIN),
        ("yue", lt.TIER_NONMANDARIN), ("zh-cantonese", lt.TIER_NONMANDARIN),
        ("en", lt.TIER_NONMANDARIN), ("en-GB", lt.TIER_NONMANDARIN),
        ("ja", lt.TIER_NONMANDARIN),
    ])
    def test_declared_language_maps_to_tier(self, declared, want):
        assert lt.tier_from_language(declared) == want

    def test_blank_declaration_is_unknown_not_mandarin(self):
        """没说过 ≠ 说过普通话：空值不得白捡一个 mandarin 档（那是放行证据）。"""
        assert lt.tier_from_language(None) == lt.TIER_UNKNOWN
        assert lt.tier_from_language("   ") == lt.TIER_UNKNOWN

    def test_low_confidence_detection_is_unknown(self):
        assert lt.tier_from_detection("en", 0.42) == lt.TIER_UNKNOWN
        assert lt.tier_from_detection("", 0.99) == lt.TIER_UNKNOWN
        assert lt.tier_from_detection("zh", "not-a-number") == lt.TIER_UNKNOWN

    def test_confident_detection_overrides_declared(self):
        """声学判 zh/en 与声明无关：判出 en 就是 nonmandarin。"""
        assert lt.tier_from_detection("en", 0.93) == lt.TIER_NONMANDARIN
        assert lt.tier_from_detection("zh-CN", 0.99) == lt.TIER_MANDARIN

    def test_tier_of_accepts_orm_row_and_dict(self):
        row = types.SimpleNamespace(audio_language_tier="nonmandarin_ref")
        assert lt.tier_of(row) == lt.TIER_NONMANDARIN
        assert lt.tier_of({"audio_language_tier": "mandarin_ref"}) == lt.TIER_MANDARIN
        assert lt.tier_of(None) is None
        assert lt.tier_of({"audio_language_tier": "  "}) is None


# ================================================= 2) 分发门
class TestOpenvoiceTierGate:
    def test_only_openvoice_nonmandarin_cell_is_blocked(self):
        bad = _tier_row(tier=lt.TIER_NONMANDARIN)
        engine, reason = lt.openvoice_tier_gate("openvoice", bad, fallback="voxcpm")
        assert engine == "voxcpm" and reason            # 唯一动手的一格

    @pytest.mark.parametrize("engine,tier", [
        ("openvoice", lt.TIER_MANDARIN),
        ("openvoice", lt.TIER_UNKNOWN),
        ("openvoice", None),                            # 从没打过标
        ("voxcpm", lt.TIER_NONMANDARIN),                # 不是 openvoice 就不关本门的事
        ("edgetts", lt.TIER_NONMANDARIN),
        ("", lt.TIER_NONMANDARIN),
    ])
    def test_every_other_cell_passes_through(self, engine, tier):
        got, reason = lt.openvoice_tier_gate(
            engine, _tier_row(engine=engine or "voxcpm", tier=tier), fallback="voxcpm")
        assert got == (engine or None) and reason is None

    def test_fallback_never_stays_openvoice(self):
        """调用方的兜底本身就是 openvoice（整书项目引擎=它）时必须改用药典默认档。"""
        engine, reason = lt.openvoice_tier_gate(
            "openvoice", _tier_row(tier=lt.TIER_NONMANDARIN), fallback="openvoice")
        assert engine == lt.DEFAULT_FALLBACK_ENGINE != "openvoice" and reason

    def test_reason_names_the_tier_and_the_target(self):
        """原因文案是"换嗓子"的唯一对外解释，必须自证（含档名 + 落地引擎）。"""
        _, reason = lt.openvoice_tier_gate("openvoice",
                                           _tier_row(tier=lt.TIER_NONMANDARIN),
                                           fallback="edgetts")
        assert lt.TIER_NONMANDARIN in reason and "edgetts" in reason


# ================================================= 3a) 试听高档接线
class TestPreviewHighTierGate:
    def _drive(self, monkeypatch, row, fail_names=(), project_engine="voxcpm"):
        """`project_engine` = 全局默认档（`Config.TTS_ENGINE`），即兜底发声的引擎。

        指令#1 起默认档会随档位变（在线档=edgetts），所以这里显式钉住"项目档=voxcpm"，
        让本组测试的判据仍是"兜底=项目引擎"本身，而不是被环境里的默认值带着走。
        """
        from backend.config import Config
        engines = {}
        monkeypatch.setattr(Config, "TTS_ENGINE", project_engine)

        def fake_load(name):
            if name in fail_names:
                raise RuntimeError(f"{name} 不可用（桩）")
            if name not in engines:
                engines[name] = FakeEngine(
                    audio=_speech_like(sample_rate=48000), sample_rate=48000)
            return engines[name]

        monkeypatch.setattr(vp, "_load_row", lambda vid: row)
        monkeypatch.setattr(vp, "_native_sample", lambda vid: None)
        monkeypatch.setattr(vp, "_load_engine", fake_load)
        return engines

    def test_openvoice_row_with_foreign_ref_is_downgraded(self, cache_dir, monkeypatch):
        row = _tier_row(tier=lt.TIER_NONMANDARIN, vid="v-foreign")
        engines = self._drive(monkeypatch, row)
        out = vp.resolve_preview("v-foreign", wait_s=15, quality="high")
        assert out["ok"], out
        assert "openvoice" not in engines               # 根本没被要过
        assert list(engines) == ["voxcpm"]
        meta = _meta_of(vp.cache_paths("v-foreign", "high")[0])
        # 降级必须在产物元数据里可查：换了引擎=换了嗓子
        assert meta["declared_engine"] == "openvoice"
        assert meta["voice_binding"] == "tier_downgrade"
        assert lt.TIER_NONMANDARIN in (meta["engine_tier_gate"] or "")

    def test_openvoice_row_with_mandarin_ref_keeps_its_engine(self, cache_dir,
                                                              monkeypatch):
        row = _tier_row(tier=lt.TIER_MANDARIN, vid="v-native")
        engines = self._drive(monkeypatch, row)
        assert vp.resolve_preview("v-native", wait_s=15, quality="high")["ok"]
        assert list(engines) == ["openvoice"]
        meta = _meta_of(vp.cache_paths("v-native", "high")[0])
        assert meta["voice_binding"] == "own_engine"
        assert meta["engine_tier_gate"] is None

    def test_untagged_row_is_not_guessed_about(self, cache_dir, monkeypatch):
        """没档 = 没证据改路由：宁可照原样跑，也不拿一次猜测换嗓子。"""
        row = _tier_row(tier=None, vid="v-untagged")
        engines = self._drive(monkeypatch, row)
        assert vp.resolve_preview("v-untagged", wait_s=15, quality="high")["ok"]
        assert list(engines) == ["openvoice"]
        assert _meta_of(vp.cache_paths("v-untagged", "high")[0])["voice_binding"] \
            == "own_engine"

    def test_load_failure_fallback_is_labelled_apart_from_tier_downgrade(
            self, cache_dir, monkeypatch):
        """两态不得互相顶替：声明引擎加载失败导致的回落仍标 engine_fallback。"""
        row = _tier_row(engine="kokoro", tier=None, vid="v-loadfail")
        engines = self._drive(monkeypatch, row, fail_names=("kokoro",))
        out = vp.resolve_preview("v-loadfail", wait_s=15, quality="high")
        assert out["ok"], out
        assert list(engines) == ["voxcpm"]
        meta = _meta_of(vp.cache_paths("v-loadfail", "high")[0])
        assert meta["voice_binding"] == "engine_fallback"
        assert meta["declared_engine"] == "kokoro"
        assert meta["engine_tier_gate"] is None

    def test_fallback_engine_follows_the_configured_tier(self, cache_dir, monkeypatch):
        """指令#1：兜底嗓 = 当前默认档的引擎，不再是硬编码 voxcpm。

        在线档（默认）下降级必须落在 edgetts，且**不去加载 5.2GB 的本地模型** ——
        这正是"选在线就不该被本地引擎拖住"这条承诺的落点。
        """
        row = _tier_row(tier=lt.TIER_NONMANDARIN, vid="v-foreonline")
        engines = self._drive(monkeypatch, row, project_engine="edgetts")
        out = vp.resolve_preview("v-foreonline", wait_s=15, quality="high")
        assert out["ok"], out
        assert list(engines) == ["edgetts"]
        assert "voxcpm" not in engines
        meta = _meta_of(vp.cache_paths("v-foreonline", "high")[0])
        assert meta["declared_engine"] == "openvoice"
        assert meta["voice_binding"] == "tier_downgrade"


# ================================================= 3b) 整书 P5 路由表接线
class _RouteEngine:
    def __init__(self, name, sample_rate=24000):
        self.engine_name = name
        self.sample_rate = sample_rate
        self.online = True                          # 走 get_loaded，不进预热队列
        self.calls = []

    def synthesize(self, text=None, voice_id=None, **kw):
        self.calls.append(voice_id)
        return np.full(12000, 0.4, dtype=np.float32)


async def _drive_p5(monkeypatch, row):
    from modules.workflow import conversion_workflow as wmod
    from modules.tts_engine.engine_registry import engine_registry
    from modules.voice_manager import voice_store as vs_mod
    from modules.workflow import cast_pipeline_hooks as hooks_mod

    fakes = {name: _RouteEngine(name) for name in ("openvoice", "voxcpm")}
    monkeypatch.setattr(engine_registry, "get", lambda name: fakes.get(name))
    monkeypatch.setattr(engine_registry, "get_loaded",
                        lambda name, strict=True: fakes[name])
    monkeypatch.setattr(vs_mod.voice_store, "get", lambda vid: row)
    monkeypatch.setattr(hooks_mod, "synth_params_for_sentence",
                        lambda sent, settings: {"speed": 1.0, "pitch": 0.0})

    cw = wmod.ConversionWorkflow()
    monkeypatch.setattr(cw, "_append_task_log", lambda *a, **k: None)
    monkeypatch.setattr(cw, "is_cancelled", lambda tid: False)

    async def _noop_emit(*a, **k):
        return None

    monkeypatch.setattr(cw, "_emit", _noop_emit)
    ctx = {"task_id": "t-tier", "sentences": [
        {"text": "第一句念白内容", "voice_id": "v1", "chapter_id": "c1"}],
        "settings": {}}
    out = await cw._stage_synthesize(ctx, {"engine": "openvoice"})
    return out, fakes


class TestSynthStageTierGate:
    async def test_foreign_ref_voice_is_not_routed_to_openvoice(self, monkeypatch):
        out, fakes = await _drive_p5(monkeypatch,
                                     _tier_row(tier=lt.TIER_NONMANDARIN, vid="v1"))
        assert len(out["audio_arrays"]) == 1
        assert fakes["openvoice"].calls == []           # 不合格的那一格没被叫到
        assert fakes["voxcpm"].calls == ["v1"]
        assert [a["index"] for a in out["audio_arrays"]] == [0]

    async def test_mandarin_ref_voice_stays_on_openvoice(self, monkeypatch):
        out, fakes = await _drive_p5(monkeypatch,
                                     _tier_row(tier=lt.TIER_MANDARIN, vid="v1"))
        assert fakes["openvoice"].calls == ["v1"]
        assert fakes["voxcpm"].calls == []


# ================================================= 3c) cast 角色试听接线
class TestCastPreviewTierGate:
    """cast 试听吃 voice_id 的参考音频、引擎名却来自 `Config.TTS_ENGINE`：
    全局档恰为 openvoice 时，同一格失效模式必须与 P5 同判据降级。"""

    def _drive(self, monkeypatch, tier, tts_engine):
        import backend.api.cast as cast_api
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from backend.config import Config
        from modules.tts_engine.engine_registry import engine_registry
        from modules.voice_manager.voice_store import voice_store

        asked = []
        eng = FakeEngine()
        eng.engine_name = tts_engine
        monkeypatch.setattr(engine_registry, "is_loaded", lambda n: True)
        monkeypatch.setattr(engine_registry,
                            "get_loaded", lambda n, **kw: asked.append(n) or eng)
        monkeypatch.setattr(Config, "TTS_ENGINE", tts_engine)
        monkeypatch.setattr(cast_api, "_require_project", lambda pid: {
            "project_id": pid,
            "roles": [{"id": "r1", "name": "甲", "voice_id": "v1",
                       "cast": {"sample_quotes": ["你好，我是甲。"]},
                       "extra_data": {}}]})
        monkeypatch.setattr(voice_store, "get",
                            lambda vid: _tier_row(engine=tts_engine, tier=tier))
        app = FastAPI()
        app.include_router(cast_api.router, prefix="/api")
        client = TestClient(app)
        resp = client.post("/api/dubbing/projects/p1/cast/preview",
                           json={"role_id": "r1"})
        return resp, asked, eng

    def test_foreign_ref_row_is_downgraded_and_labelled(self, monkeypatch):
        resp, asked, eng = self._drive(monkeypatch, lt.TIER_NONMANDARIN, "openvoice")
        assert resp.status_code == 200, resp.text
        assert asked == ["voxcpm"]                     # 一次都没向 openvoice 要引擎
        assert resp.headers["x-preview-voice-binding"] == "tier_downgrade"
        assert eng.calls[-1]["voice_id"] == "v1"       # 真合成过，不是空头 200

    def test_mandarin_ref_row_keeps_openvoice(self, monkeypatch):
        resp, asked, _ = self._drive(monkeypatch, lt.TIER_MANDARIN, "openvoice")
        assert resp.status_code == 200, resp.text
        assert asked == ["openvoice"]
        assert "x-preview-voice-binding" not in resp.headers

    def test_untagged_row_is_not_guessed_about(self, monkeypatch):
        resp, asked, _ = self._drive(monkeypatch, None, "openvoice")
        assert resp.status_code == 200, resp.text
        assert asked == ["openvoice"]
        assert "x-preview-voice-binding" not in resp.headers

    def test_gate_does_not_touch_other_engines(self, monkeypatch):
        """全局 voxcpm + 非普通话行：门只管 openvoice 那一格，别的一律原样。"""
        resp, asked, _ = self._drive(monkeypatch, lt.TIER_NONMANDARIN, "voxcpm")
        assert resp.status_code == 200, resp.text
        assert asked == ["voxcpm"]
        assert "x-preview-voice-binding" not in resp.headers


# ================================================= 4) 打标工具的三方契约
class TestTaggerDetectContract:
    """`detect_one` 与 faster-whisper 的返回值契约（首跑 4/4 失败抓到的那条）。"""

    def _import_tagger(self):
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..",
                                        "tools", "voice_library"))
        import tag_language_tier as tagger
        return tagger

    def test_detect_one_handles_three_tuple_return(self, tmp_path):
        import soundfile as sf
        tagger = self._import_tagger()
        wav = tmp_path / "ref.wav"
        sf.write(str(wav), np.full(8000, 0.05, dtype=np.float32), 16000)

        class Stub:
            def detect_language(self, audio):
                # 真机契约：(language, probability, all_language_probs)
                assert len(audio) > 0
                return "en", 0.98, [("en", 0.98), ("zh", 0.015)]

        out = tagger.detect_one(Stub(), str(wav))
        assert out["tier"] == lt.TIER_NONMANDARIN
        assert out["lang"] == "en" and out["prob"] == 0.98
        assert out["runner_up"] == "zh"

    def test_detect_one_low_confidence_lands_unknown(self, tmp_path):
        import soundfile as sf
        tagger = self._import_tagger()
        wav = tmp_path / "ref2.wav"
        sf.write(str(wav), np.full(8000, 0.05, dtype=np.float32), 16000)

        class Stub:
            def detect_language(self, audio):
                return "zh", 0.31, [("zh", 0.31), ("en", 0.28)]

        assert tagger.detect_one(Stub(), str(wav))["tier"] == lt.TIER_UNKNOWN

    def _pick_conn(self, tagger):
        """一张够 pick_rows 用的最小 voice_profiles（列名必须与真库一致）。"""
        import sqlite3
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("""CREATE TABLE voice_profiles (id TEXT PRIMARY KEY,
                        name TEXT, engine TEXT, source TEXT, language TEXT,
                        category TEXT, meta TEXT, audio_language_tier TEXT,
                        reference_audio_path TEXT)""")
        rows = [("1", "aishell-01", "voxcpm", "D:/lib/real_speech/aishell/S1.wav", None),
                ("2", "aishell-02", "voxcpm", "D:/lib/real_speech/aishell/S2.wav", None),
                ("3", "refill-ani", "voxcpm", "D:/lib/refill/animal/A1.wav", None),
                ("4", "cv-en", "sample", "D:/resources/cv_fixie_en/B2.wav", None),
                ("5", "vctk", "voxcpm", "D:/lib/library/vctk_voices/C3.wav",
                 lt.TIER_NONMANDARIN)]
        conn.executemany("INSERT INTO voice_profiles (id,name,engine,"
                         "reference_audio_path,audio_language_tier) VALUES (?,?,?,?,?)",
                         rows)
        return conn

    def _args(self, tagger, **kw):
        base = dict(engine="", ref_like="", ref_not_like="",
                    only_untagged=False, limit=0)
        base.update(kw)
        return types.SimpleNamespace(**base)

    def test_ref_not_like_excludes_mandarin_families(self):
        tagger = self._import_tagger()
        conn = self._pick_conn(tagger)
        ids = lambda ns: [r["id"] for r in tagger.pick_rows(conn, self._args(tagger, **ns))]
        # 不排除：5 行全在候选里（含已打标的 5 号）
        assert ids({}) == ["1", "2", "3", "4", "5"]
        # 排除两个"必为普通话"的族 + 只挑未打标 => 只剩 cv-en 一行
        assert ids({"ref_not_like": "refill,aishell", "only_untagged": True}) == ["4"]
        # 排除项与 --ref-like 是"与"关系：只留英文族且再排除它 => 空集（不静默放行）
        assert ids({"ref_like": "fixie_en", "ref_not_like": "fixie_en"}) == []
        # --limit 仍受排除约束（不是"先截断再筛"）
        assert ids({"ref_not_like": "cv_fixie_en", "limit": 2}) == ["1", "2"]
        # 空白/大小写/尾逗号都不该改变结果
        assert ids({"ref_not_like": " REFILL , ,aishell,"}) == ["4", "5"]
        conn.close()

    def test_csv_lower_helper_drops_blanks(self):
        tagger = self._import_tagger()
        assert tagger._csv_lower(" A , ,b,") == ["a", "b"]
        assert tagger._csv_lower(None) == []


# ================================================= 5) 克隆建行带档
class TestCloneWritesDeclaredTier:
    def _clone_with_stub_io(self, monkeypatch, tmp_path, language):
        from modules.voice_manager import voice_cloner as vc_mod
        from modules.voice_manager.voice_store import voice_store

        captured = {}
        # clone() 第一步就是对参考音频算 sha256，所以必须真给一个文件
        src = tmp_path / "ref.wav"
        src.write_bytes(b"RIFF" + b"\0" * 64)

        def fake_create(**kwargs):
            captured.update(kwargs)
            return types.SimpleNamespace(id="v-new", name=kwargs.get("name"))

        cloner = vc_mod.VoiceCloner()
        monkeypatch.setattr(cloner, "validate_audio",
                            lambda path, engine_name=None: {"valid": True,
                                                             "message": "",
                                                             "duration": 3.0})
        monkeypatch.setattr(cloner, "find_existing_clone",
                            lambda **kw: None)
        monkeypatch.setattr(vc_mod, "prepare_reference",
                            lambda path, engine, dst_dir, stem: {"metrics": {},
                                                                 "window": {},
                                                                 "checks": [],
                                                                 "fit": {}})
        monkeypatch.setattr(cloner, "_commit_reference",
                            lambda prep, src, dst, uid, ename: (
                                os.path.join(str(dst), f"ref_{uid}.wav"), [], "as_is"))
        monkeypatch.setattr(cloner, "_register_if_loaded",
                            lambda *a, **k: False)
        monkeypatch.setattr(voice_store, "create", fake_create)
        out = cloner.clone(audio_path=str(src), name="测试克隆",
                           language=language, engine_name="openvoice")
        return out, captured

    def test_foreign_clone_is_tagged_nonmandarin(self, monkeypatch, tmp_path):
        out, kwargs = self._clone_with_stub_io(monkeypatch, tmp_path, "en")
        assert kwargs["audio_language_tier"] == lt.TIER_NONMANDARIN
        lt_meta = kwargs["meta"]["language_tier"]
        assert lt_meta["basis"] == "declared"
        assert lt_meta["tier"] == lt.TIER_NONMANDARIN
        assert lt_meta["declared_language"] == "en"
        assert out["voice_id"] == "v-new"

    def test_mandarin_clone_is_tagged_mandarin(self, monkeypatch, tmp_path):
        _, kwargs = self._clone_with_stub_io(monkeypatch, tmp_path, "zh-CN")
        assert kwargs["audio_language_tier"] == lt.TIER_MANDARIN

    def test_clone_row_round_trips_through_the_gate(self, monkeypatch, tmp_path):
        """建行的档与分发的门必须咬合：同一条 kwargs 喂给门，要能判出降级。"""
        _, kwargs = self._clone_with_stub_io(monkeypatch, tmp_path, "en")
        row = types.SimpleNamespace(**{k: v for k, v in kwargs.items()
                                       if k in ("engine", "audio_language_tier")})
        engine, reason = lt.openvoice_tier_gate(row.engine, row, fallback="voxcpm")
        assert (engine, bool(reason)) == ("voxcpm", True)
