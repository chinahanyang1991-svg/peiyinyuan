"""
引擎可用性口径（KOKORO 波）

旧口径把"能实例化"当作"可用"，而真权重适配器的依赖全在 `load_model()` 里才
import —— kokoro 因此被报成可用引擎，用户点它名下的音色必失败（红线④「人声
可应用」+ PY-117「不得用静音冒充成功」）。本文件钉住三件事：

  1. 可用性判据 = `unavailable_reason()`，`get_info()` / `list_engines()` 都出口；
  2. 两处内置音色种子都不得指向不可用引擎（新库不得造出不会说话的音色）；
  3. 判据随依赖变化，不是写死的字符串（装上 kokoro 后自动转绿）。

PY-354（09-24 试听事故）把口径推广到**双档主力引擎**：`edgetts` / `voxcpm` 当时
没有覆盖本方法，于是缺 `edge_tts` / `torch` 的解释器照样被启动器健康门放行成
"可用"，用户点试听才看到 RuntimeError。见 `TestShippedTiersDeclareDeps`。
"""

import importlib
import importlib.util
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent / ".."))

from modules.tts_engine.engine_registry import EngineRegistry
from modules.tts_engine.engines.base_tts import BaseTTSEngine
from modules.tts_engine.engines.edgetts_adapter import EdgeTTSAdapter
from modules.tts_engine.engines.kokoro_adapter import KokoroAdapter
from modules.tts_engine.engines.voxcpm_adapter import VoxCPMAdapter
from modules.voice_manager.builtin_voices import BUILTIN_VOICES

REPO_ROOT = Path(__file__).parent.parent
KOKORO_DEPS = ("kokoro", "misaki", "phonemizer")
# 双档主力引擎各自的"说话前提"依赖：在线档只要 edge_tts，本地档要 torch + 随仓源码
EDGE_DEPS = ("edge_tts",)
VOXCPM_DEPS = ("torch", "voxcpm")


def _adapter_for(engine_name: str) -> BaseTTSEngine:
    registry = EngineRegistry()
    inst = registry.get(engine_name)
    assert inst is not None, f"{engine_name} 未注册"
    return inst


def _kokoro_missing_deps() -> list:
    """按适配器自己的口径复算缺失依赖，不硬编码"必然缺"。"""
    return [d for d in KOKORO_DEPS if importlib.util.find_spec(d) is None]


class TestAvailabilityJudge:
    """判据本身"""

    def test_base_engine_defaults_to_available(self):
        class _NoDep(BaseTTSEngine):
            engine_name = "nodep"

            def load_model(self, model_path: str = None) -> None:
                pass

            def synthesize(self, text, **kwargs):
                return b""

        inst = _NoDep()
        assert inst.unavailable_reason() == ""
        info = inst.get_info()
        assert info["available"] is True
        assert info["unavailable_reason"] == ""

    def test_kokoro_reason_matches_dependency_truth(self):
        reason = KokoroAdapter.unavailable_reason()
        missing = _kokoro_missing_deps()
        assert bool(reason) is bool(missing), (
            f"判据与依赖实况不符: reason={reason!r} missing={missing}"
        )
        for dep in missing:
            assert dep in reason, f"原因里没有点名缺失依赖 {dep}: {reason!r}"

    def test_reason_flips_when_dependency_present(self, monkeypatch):
        """变异检验的另一半：把 find_spec 全打成"存在"，判据必须转为可用。"""
        monkeypatch.setattr(
            importlib.util, "find_spec", lambda *a, **k: object()
        )
        assert KokoroAdapter.unavailable_reason() == ""

    def test_reason_flips_when_dependency_absent(self, monkeypatch):
        monkeypatch.setattr(
            importlib.util, "find_spec", lambda *a, **k: None
        )
        reason = KokoroAdapter.unavailable_reason()
        assert reason and "依赖库未安装" in reason
        assert _adapter_for("kokoro").get_info()["available"] is False


def _find_spec_excluding(monkeypatch, missing: tuple):
    """只把 `missing` 里的模块打成"取不到 spec"，其余保持真实可得性。

    比"全打成缺"更严格：这样判据若把不该报的依赖也写进原因，会当场被下面的
    `dep not in reason` 类断言抓住。
    """
    real = importlib.util.find_spec

    def fake(name, *a, **k):
        return None if name in missing else real(name, *a, **k)

    monkeypatch.setattr(importlib.util, "find_spec", fake)


SHIPPED_TIERS = [
    pytest.param("edgetts", EdgeTTSAdapter, EDGE_DEPS, id="online-edgetts"),
    pytest.param("voxcpm", VoxCPMAdapter, VOXCPM_DEPS, id="local-voxcpm"),
]


class TestShippedTiersDeclareDeps:
    """PY-354：双档主力引擎必须自陈依赖，否则健康门就是在给哑巴实例盖章放行。"""

    @pytest.mark.parametrize("name,cls,deps", SHIPPED_TIERS)
    def test_reason_matches_dependency_truth(self, name, cls, deps):
        reason = cls.unavailable_reason()
        missing = [d for d in deps if importlib.util.find_spec(d) is None]
        assert bool(reason) is bool(missing), (
            f"{name}: reason={reason!r} 与依赖实况 missing={missing} 不符")
        for dep in missing:
            assert dep in reason, f"{name}: 原因没点名缺失依赖 {dep}: {reason!r}"

    @pytest.mark.parametrize("name,cls,deps", SHIPPED_TIERS)
    def test_reason_flips_when_deps_absent(self, monkeypatch, name, cls, deps):
        _find_spec_excluding(monkeypatch, deps)
        reason = cls.unavailable_reason()
        assert reason, f"{name}: 依赖全缺却仍报可用"
        assert "依赖库未安装" in reason, reason
        for dep in deps:
            assert dep in reason, f"{name}: 原因没点名 {dep}: {reason!r}"
        assert _adapter_for(name).get_info()["available"] is False

    @pytest.mark.parametrize("name,cls,deps", SHIPPED_TIERS)
    def test_reason_clears_when_deps_present(self, monkeypatch, name, cls, deps):
        real = importlib.util.find_spec

        def fake(name_, *a, **k):
            return object() if name_ in deps else real(name_, *a, **k)

        monkeypatch.setattr(importlib.util, "find_spec", fake)
        assert cls.unavailable_reason() == "", f"{name}: 依赖齐了还报不可用"
        assert _adapter_for(name).get_info()["available"] is True

    @pytest.mark.parametrize("name,cls,deps", SHIPPED_TIERS)
    def test_registry_row_follows_the_judge(self, monkeypatch, name, cls, deps):
        """启动器健康门读的是 `list_engines()`，判据必须一路出口到那一行。"""
        _find_spec_excluding(monkeypatch, deps)
        row = {r["name"]: r for r in EngineRegistry().list_engines()}[name]
        assert row["available"] is False
        assert row["reason"] == cls.unavailable_reason()

    def test_criterion_is_import_only_not_weights(self, monkeypatch, tmp_path):
        """判据只查 import 可得性（base_tts 的既定口径）：权重目录不存在时，
        依赖齐的解释器仍应报可用 —— 否则装完包没下模型的机器会被误判成死档。"""
        real = importlib.util.find_spec

        def fake(name_, *a, **k):
            return object() if name_ in VOXCPM_DEPS else real(name_, *a, **k)

        monkeypatch.setattr(importlib.util, "find_spec", fake)
        import modules.tts_engine.engines.voxcpm_adapter as vox_mod
        monkeypatch.setattr(vox_mod, "_DEFAULT_MODEL_DIR", tmp_path / "无权重")
        assert not (tmp_path / "无权重").exists()
        assert VoxCPMAdapter.unavailable_reason() == ""


class TestRegistrySurfacesReason:
    def test_list_engines_carries_availability_and_reason(self):
        rows = EngineRegistry().list_engines()
        by_name = {r["name"]: r for r in rows}
        assert "kokoro" in by_name, "kokoro 引擎仍需注册（适配器与测试依赖它）"
        row = by_name["kokoro"]
        reason = KokoroAdapter.unavailable_reason()
        assert row["available"] is (not reason)
        assert row["reason"] == reason

    def test_unavailable_engine_still_listed_not_hidden(self):
        """可用=false 也要出现在清单里：隐藏它等于让用户以为没有这个引擎，
        而真问题是"缺依赖"，可见才能修。"""
        rows = EngineRegistry().list_engines()
        for row in rows:
            assert "available" in row
            assert "reason" in row
            if not row["available"]:
                assert row["reason"], "标记不可用却不给原因"


class TestNoSilentVoices:
    """内置音色种子：新库不得造出不会说话的音色"""

    def _builtin_seed_block(self) -> str:
        """`_init_builtin_voices` 的函数体文本（新库音色的唯一入口）。"""
        src = (REPO_ROOT / "backend" / "models" / "base.py").read_text(
            encoding="utf-8"
        )
        block = src.split("def _init_builtin_voices", 1)[1]
        return block.split("def _init_edge_voices", 1)[0]

    def test_builtin_list_engines_all_available(self):
        for voice in BUILTIN_VOICES:
            engine = voice["engine"]
            reason = _adapter_for(engine).unavailable_reason()
            assert not reason, f"{voice['name']} 指向不可用引擎 {engine}: {reason}"

    def test_db_seed_shares_the_single_voice_list(self):
        """生产种子**只准**引用 BUILTIN_VOICES：这里曾并存第二份硬编码名单，
        11 条 vs 4 条、标签描述各说各话，而单测只测没人用的那份。"""
        block = self._builtin_seed_block()
        assert "BUILTIN_VOICES" in block, "种子不再引用唯一真值清单"
        assert 'engine="' not in block, "种子函数体里又出现硬编码引擎名单（两份名单必然漂移）"

    def _temp_session(self, monkeypatch, tmp_path):
        """把种子函数的会话工厂指到临时库（生产 data/peiyinyuan.db 一个字节都不碰）。"""
        import backend.models.base as base_mod
        from backend.models.voice import VoiceProfile
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        engine = create_engine(f"sqlite:///{(tmp_path / 'seed.db').as_posix()}")
        VoiceProfile.__table__.create(bind=engine)
        session_factory = sessionmaker(bind=engine)
        monkeypatch.setattr(base_mod, "get_session", session_factory)
        return base_mod, VoiceProfile, session_factory

    def test_fresh_db_seeds_every_listed_voice(self, monkeypatch, tmp_path):
        """同源不等于生效：真跑一遍种子函数，逐条落库的必须就是清单本身。"""
        base_mod, VoiceProfile, session_factory = self._temp_session(monkeypatch, tmp_path)

        base_mod._init_builtin_voices()

        s = session_factory()
        try:
            rows = s.query(VoiceProfile).filter_by(source="builtin").all()
        finally:
            s.close()
        assert [r.name for r in rows] == [v["name"] for v in BUILTIN_VOICES]
        assert {r.engine for r in rows} == {v["engine"] for v in BUILTIN_VOICES}
        assert {"zh", "en"} <= {r.language for r in rows}, "种子语种覆盖漂回单语"

    def test_seed_skips_when_builtin_rows_already_exist(self, monkeypatch, tmp_path):
        """老库必然已有 builtin 行：'存在即跳过' 一旦被摘掉，每次重启都会翻倍。"""
        base_mod, VoiceProfile, session_factory = self._temp_session(monkeypatch, tmp_path)
        base_mod._init_builtin_voices()
        base_mod._init_builtin_voices()

        s = session_factory()
        try:
            n = s.query(VoiceProfile).filter_by(source="builtin").count()
        finally:
            s.close()
        assert n == len(BUILTIN_VOICES)

    def test_kokoro_is_not_seeded_as_a_voice(self):
        """kokoro 在本构建里不可用（依赖与权重都不在），因此任何"内置音色"
        清单里出现它都会造出必然失败的行。装上依赖并实测过一条中文合成后，
        连同本用例一起解禁。"""
        assert "kokoro" not in {v["engine"] for v in BUILTIN_VOICES}
        assert "kokoro" not in self._builtin_seed_block().lower()

    def test_base_py_seed_names_no_kokoro_row(self):
        src = (REPO_ROOT / "backend" / "models" / "base.py").read_text(
            encoding="utf-8"
        )
        assert "Kokoro-English-Female" not in src

    def test_online_download_refuses_unavailable_engine(self, monkeypatch):
        """注册表里有这个键 ≠ 能合成：把在线样本登记到不可用引擎，
        等于再造一条永远说不出话的音色行。必须在联网前就拒。

        本用例自带断网闸门：一旦判据被摘掉、代码往下走去下载，
        download_sample 会立刻抛错而不是真连 HF 镜像（变异检验时它确实
        下成过一个 4.95s 样本并写进生产库，故这条闸门是必须的）。
        """
        from backend.services import voice_online
        from backend.services.voice_service import voice_service as svc
        from modules.tts_engine.engine_registry import engine_registry

        class _Stub:
            @staticmethod
            def unavailable_reason():
                return "依赖库未安装: kokoro"

        def _tripwire(*a, **k):
            # 不抛异常（外层有宽 try 会把它重新包装成普通失败），而是回一个
            # 带标记的失败：本用例随后断言 reason 必须是 engine_unavailable。
            return {"ok": False, "reason": "tripwire_no_network_allowed",
                    "http_code": 0}

        monkeypatch.setattr(engine_registry, "known_engine_names",
                            lambda: {"voxcpm", "kokoro"})
        monkeypatch.setattr(engine_registry, "get", lambda name=None: _Stub())
        monkeypatch.setattr(svc.hf_crawler, "download_sample", _tripwire)
        monkeypatch.setattr(svc.github_crawler, "download_sample", _tripwire)
        res = voice_online.download_and_register("hexgrad/Kokoro-82M",
                                                 engine="kokoro")
        assert res.get("ok") is False
        assert res.get("reason") == "engine_unavailable", res
        assert res.get("unavailable_reason") == "依赖库未安装: kokoro"


class TestKokoroExtraCoversChineseVoices:
    """PY-367（2026-09-27 实测）：`pip install peiyinyuan[kokoro]` 只写 kokoro 一枚时，
    英文音色能出声，而 BUILTIN_VOICES 里 6 枚中文音色（zf_/zm_）在合成期
    ModuleNotFoundError: ordered_set —— 中文支路的 G2P 由 misaki[zh] 带进来。
    可用性判据的口径本身没改（import-only、不查权重，仍是 test_criterion_is_import_only_not_weights 的设计）。"""

    def test_adapter_advertises_chinese_voices(self):
        zh = [v for v in KokoroAdapter.BUILTIN_VOICES if v[0] == "z"]
        assert len(zh) >= 6, f"中文音色清单漂了: {zh}"

    def test_kokoro_extra_declares_the_chinese_chain(self):
        import tomllib

        text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        reqs = [r.lower() for r in
                tomllib.loads(text)["project"]["optional-dependencies"]["kokoro"]]
        blob = " ".join(reqs)
        assert "misaki[zh]" in blob, f"kokoro extra 没声明中文支路: {reqs}"
        assert "espeakng-loader" in blob, f"kokoro extra 没自带 espeak-ng 音库: {reqs}"
