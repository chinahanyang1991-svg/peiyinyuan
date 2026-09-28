# -*- coding: utf-8 -*-
"""#76 S3 收尾单测：**ref_fitting 接线**（clone 入库前 fit + ingest 下载后校验）

对应任务卡"要做的四件事"之 3：
  * `VoiceCloner.clone()` 在参考音频**入库前**跑 ref_fitting ——
    过长裁"6~8s RMS 最高窗"**另存** `ref_*_fit.wav`（原件永不动，故 F-R4-4 的
    sha256 幂等护栏不破），checks 结果进 `voice_profiles.meta["ref_checks"]`；
  * `tools/voice_library/ingest.py` 下载后校验挂 `diagnose()`，
    **阻断级（severity=="block"）不入库**。

全部用合成桩音频 + `tmp_path`，`voice_store` 被替换成记账假件 ——
**不碰生产 sqlite、不碰 data/voices/**。
"""

from __future__ import annotations

import hashlib
import math
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
import soundfile as sf

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules.voice_manager import voice_cloner as vc_mod      # noqa: E402
from modules.voice_manager.voice_cloner import VoiceCloner    # noqa: E402

SR = 16000
CHECK_NAMES = ["noise_floor", "duration", "single_speaker", "long_silence"]


def speech(seconds: float, f0: float = 120.0, level: float = 0.35) -> np.ndarray:
    """谐波 + 3Hz 音节调制的"像人声"桩（同 tests/test_w1c_ref_fitting.py 口径）。"""
    n = int(seconds * SR)
    t = np.arange(n) / SR
    s = sum((1.0 / k) * np.sin(2 * math.pi * f0 * k * t) for k in (1, 2, 3))
    s = s / np.max(np.abs(s)) * level
    am = (np.sin(2 * math.pi * 3.0 * t) + 1) / 2
    return (s * np.where(am < 0.15, 0.0, 1.0)).astype("float32")


def write_wav(path: Path, seconds: float, **kw) -> str:
    sf.write(str(path), speech(seconds, **kw), SR, subtype="PCM_16")
    return str(path)


def sha256(path: str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class RecordingStore:
    """假 voice_store：只记账，不碰 DB。"""

    def __init__(self, voice_dir: Path):
        self.voice_dir = voice_dir
        self.created: list[dict] = []
        self._n = 0

    def create(self, **kw):
        self._n += 1
        self.created.append(kw)
        return SimpleNamespace(id=f"v{self._n}", name=kw.get("name"))




@pytest.fixture
def cloner(tmp_path, monkeypatch):
    """VoiceCloner + 假 store（`cloner.store.created` 记账）+ 假幂等查询。

    幂等护栏本身在 F-R4-4 已有单测，这里替掉它是为了让每条用例都从"未命中"开始。
    """
    vdir = tmp_path / "voices"
    vdir.mkdir(parents=True, exist_ok=True)
    store = RecordingStore(vdir)
    monkeypatch.setattr(vc_mod, "voice_store", store)
    # 引擎一律"未加载" → 不触任何模型，克隆只做到入库为止（F-R4-4 语义）
    monkeypatch.setattr(vc_mod.engine_registry, "_loaded", {}, raising=True)
    c = VoiceCloner()
    c.voice_dir = vdir
    c.store = store
    monkeypatch.setattr(c, "find_existing_clone",
                        lambda name, engine_name, ref_sha256: None)
    return c


class TestEngineTieredFloor:
    """时长地板按引擎分档：GSV/voxcpm 仍是 3s（零行为变化），openvoice 降到 1s。"""

    @pytest.mark.parametrize("engine,want", [
        ("openvoice", 1.0), ("gptsovits", 3.0), ("voxcpm", 3.0),
        ("indextts", 3.0), (None, 3.0), ("不存在的引擎", 3.0)])
    def test_duration_floor(self, engine, want):
        assert VoiceCloner.duration_floor(engine) == want

    def test_short_ref_ok_for_openvoice_but_rejected_for_voxcpm(self, cloner,
                                                                tmp_path):
        """2.27s（库内真实档位）：openvoice 放行、voxcpm 仍判太短 —— 口径分档不是放水。"""
        p = write_wav(tmp_path / "r227.wav", 2.27)
        assert cloner.validate_audio(p, engine_name="openvoice")["valid"] is True
        bad = cloner.validate_audio(p, engine_name="voxcpm")
        assert bad["valid"] is False and "太短" in bad["message"]
        assert bad["engine_floor_s"] == 3.0
        with pytest.raises(ValueError, match="太短"):
            cloner.clone(p, name="档外", engine_name="voxcpm")
        assert cloner.store.created == []          # 没建行


class TestCloneFit:
    def test_long_ref_trimmed_to_fit_copy_original_untouched(self, cloner, tmp_path):
        """13s 参考 → 裁 6~8s 能量窗**另存** `ref_*_fit.wav`；原件字节不变；checks 进 meta。"""
        src = write_wav(tmp_path / "long13.wav", 13.0)
        before = sha256(src)
        res = cloner.clone(src, name="长参考", engine_name="openvoice")

        assert res["ref_verdict"] == "fitted"
        fit = res["reference_audio_path"]
        assert fit.endswith("_fit.wav") and Path(fit).is_file()
        assert fit != src
        x, sr = sf.read(fit, dtype="float32", always_2d=False)
        assert 6.0 <= len(x) / sr <= 8.0 + 1e-6, len(x) / sr
        # 原件不动 ⇒ F-R4-4 的 sha256 幂等护栏仍然量的是同一串字节
        assert sha256(src) == before
        row = cloner.store.created[0]
        assert row["reference_audio_path"] == fit and row["model_path"] == fit
        meta = row["meta"]
        assert meta["reference_sha256"] == before, "指纹必须是原件的，不能被 fit 副本换掉"
        assert [c["name"] for c in meta["ref_checks"]] == CHECK_NAMES
        assert meta["ref_verdict"] == "fitted"
        assert meta["ref_source_path"] == src
        assert meta["ref_window"] == [1.0, 10.0]
        assert 6.0 <= meta["ref_fitted_seconds"] <= 8.0
        assert meta["ref_metrics"]["duration_s"] == pytest.approx(13.0, abs=0.1)

    def test_in_window_ref_keeps_original_copy_path(self, cloner, tmp_path):
        """7.5s（openvoice 档内）→ 不产生 fit 副本，走升级前那条 copy2 路径。"""
        src = write_wav(tmp_path / "mid75.wav", 7.5)
        before = sha256(src)
        res = cloner.clone(src, name="档内", engine_name="openvoice")
        assert res["ref_verdict"] == "ok"
        stored = res["reference_audio_path"]
        assert stored.endswith("mid75.wav") is False and Path(stored).name.startswith("ref_")
        assert sha256(stored) == before          # 复制件与原件同字节
        assert sha256(src) == before
        assert list((cloner.voice_dir).glob("ref_*_fit.wav")) == []
        assert cloner.store.created[0]["meta"]["ref_verdict"] == "ok"
        assert cloner.store.created[0]["meta"]["ref_source_path"] is None

    def test_too_short_ref_raises_and_writes_nothing(self, cloner, tmp_path):
        """0.4s < openvoice 1s 硬底 → 抛错 + 不建行 + 不落任何参考文件。

        这里由**第一道**门（validate_audio 的引擎地板）拦下；ref_fitting 的
        `needs_longer_sample` 是第二道（同档下同一判据，见下一例的直接覆盖）。
        """
        src = write_wav(tmp_path / "tiny.wav", 0.4)
        with pytest.raises(ValueError, match="太短") as ei:
            cloner.clone(src, name="太短", engine_name="openvoice")
        assert "至少需要 1.0 秒" in str(ei.value)
        assert "不做时间拉伸" in str(ei.value)     # 归因要带上"为什么不救"
        assert cloner.store.created == []
        assert list(cloner.voice_dir.glob("ref_*")) == []

    def test_commit_reference_blocks_needs_longer_sample(self, cloner, tmp_path):
        """第二道门（ref_fitting verdict）单独钉：阻断级不入库、错误文案带缺什么。"""
        prep = {"verdict": "needs_longer_sample", "use_path": "D:/x/y.wav",
                "checks": [{"name": "duration", "ok": False, "severity": "block",
                            "detail": "0.80s 短于引擎 'openvoice' 下限 1.0s"}],
                "metrics": {}, "window": [1.0, 10.0], "fit": None, "fit_path": None}
        with pytest.raises(ValueError, match="未通过入库检查") as ei:
            VoiceCloner._commit_reference(prep, "D:/x/y.wav", cloner.voice_dir,
                                          "uid0", "openvoice")
        assert "needs_longer_sample" in str(ei.value)
        assert "不做时间拉伸" in str(ei.value)
        assert cloner.store.created == []
        assert list(cloner.voice_dir.glob("ref_*")) == []

    def test_checks_reported_for_every_clone(self, cloner, tmp_path):
        """不论走哪条分支，meta 里必须有四条 checks（前端/审计同一份口径）。"""
        for secs in (1.88, 2.56, 7.51):
            src = write_wav(cloner.voice_dir / f"src{secs}.wav", secs)
            res = cloner.clone(src, name=f"n{secs}", engine_name="openvoice")
            assert res["ref_verdict"] == "ok", secs
            names = [c["name"] for c in res["ref_checks"]]
            assert names == CHECK_NAMES
            assert all(c["name"] != "duration" or c["ok"] for c in res["ref_checks"])


class TestIngestDownloadGate:
    """ingest.py 下载后校验：阻断级不入库（默认 sample 档不新增拒收）。"""

    @pytest.fixture
    def ing(self):
        sys.path.insert(0, str(ROOT / "tools" / "voice_library"))
        import ingest as ingest_mod
        return ingest_mod

    def test_ref_diagnose_never_raises_on_junk(self, ing, tmp_path):
        bad = tmp_path / "junk.wav"
        bad.write_bytes(b"not a wav at all")
        rep = ing.ref_diagnose(str(bad), "openvoice")
        assert rep["verdict"] == "unusable"
        assert ing.ref_block_reasons(rep), "解不开码必须是阻断级"
        assert "decodable" in ing.ref_block_reasons(rep)[0]

    def test_block_reasons_ignores_warn_and_info(self, ing):
        rep = {"checks": [
            {"name": "noise_floor", "ok": False, "severity": "warn", "detail": "底噪高"},
            {"name": "duration", "ok": False, "severity": "block", "detail": "超窗"},
            {"name": "single_speaker", "ok": False, "severity": "info", "detail": "F0 不可测"},
        ]}
        reasons = ing.ref_block_reasons(rep)
        assert len(reasons) == 1 and reasons[0].startswith("duration:")

    def test_openvoice_tier_rejects_19s_sample_and_writes_nothing(self, ing, tmp_path):
        """19.1s（库内真实最长档）在 openvoice 档 = 超 10s 硬门 ⇒ 拒收且不落盘。"""
        src = write_wav(tmp_path / "src191.wav", 19.1)
        lib = tmp_path / "library"
        items = [{"category": "human", "name": "十九秒", "page_url": "http://x/19",
                  "license": "CC0", "local_file": src}]
        stats = ing.ingest_manifest(items, False, lib, str(tmp_path / "no.db"),
                                    ref_engine="openvoice", verbose=False)
        assert stats["accepted"] == 0 and stats["rejected"] == 1
        assert stats["reject_reasons"]["ref_check_block"] == 1
        assert "duration" in stats["rejected_items"][0]["reason"]
        assert not lib.exists() or list(lib.glob("**/*.wav")) == [], "被拒条目不得留下主样本"

    def test_default_sample_tier_accepts_same_item_with_checks_in_meta(self, ing,
                                                                       tmp_path):
        """同一件在默认 sample 档照常入库，且 checks 已挂进 meta（口径可核）。"""
        src = write_wav(tmp_path / "src191b.wav", 19.1)
        items = [{"category": "human", "name": "十九秒", "page_url": "http://x/19b",
                  "license": "CC0", "local_file": src}]
        stats = ing.ingest_manifest(items, False, tmp_path / "lib2",
                                    str(tmp_path / "no2.db"), verbose=False)
        assert stats["accepted"] == 1 and stats["rejected"] == 0
        assert stats["ref_engine"] == "sample"

    def test_build_profile_meta_shape(self, ing):
        metrics = {"duration_s": 7.5, "peak": 0.4, "rms": 0.07, "sample_rate": 48000,
                   "clip_ratio": 0.0, "bytes": 1000}
        rep = {"checks": [{"name": "duration", "ok": True, "severity": "block",
                           "detail": "在档内"}],
               "metrics": {"duration_s": 7.5}, "verdict": "ok",
               "window": [1.0, 10.0], "duration": 7.5}
        item = {"category": "human", "name": "n", "license": "CC0",
                "page_url": "http://x/1"}
        prof = ing.build_profile(item, "m.wav", "p.wav", 48000, metrics, "uid1",
                                 ref_report=rep, ref_engine="openvoice")
        assert prof["meta"]["ref_engine"] == "openvoice"
        assert prof["meta"]["ref_verdict"] == "ok"
        assert prof["meta"]["ref_window"] == [1.0, 10.0]
        assert [c["name"] for c in prof["meta"]["ref_checks"]] == ["duration"]
        # 不给报告时 meta 形状与升级前一致（老批次 --refresh-existing 不漂移）
        old = ing.build_profile(item, "m.wav", "p.wav", 48000, metrics, "uid1")
        assert not any(k.startswith("ref_") for k in old["meta"])
