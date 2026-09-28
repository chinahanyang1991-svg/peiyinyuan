# -*- coding: utf-8 -*-
"""US-C3 单测：参考音频的引擎分档时长门 + 能量窗裁剪 + 诊断 checks[]

规格：`UPGRADE-SPEC.md` 泳道 C · US-C3
    「引擎分档 3-10s 硬门、过长取 RMS 最高 6-8s 窗另存 `ref_*_fit.wav`（不动原件，
      sha256 幂等不破）、过短判 `needs_longer_sample` 不做时间拉伸；
      checks[]（噪声/时长/多人/长静音）存 meta」
被测实现：`modules/voice_manager/ref_fitting.py`

全部音频用**合成桩**（谐波 + 音节幅度调制 + 可控噪声/静音），不依赖音色库真文件，
也不依赖任何引擎服务在跑；写到 `tmp_path` 下，跑完即弃。

清洁室：只取 ANALYSIS-GPT-SOVITS M4 / ANALYSIS-OPENVOICE OV-P1-3 里记录的**阈值与
判据结论**（数值），未复制上游源码文本或注释。
"""

from __future__ import annotations

import hashlib
import math
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from modules.voice_manager import ref_fitting as rf

SR = 16000
CHECK_NAMES = ["noise_floor", "duration", "single_speaker", "long_silence"]


# --------------------------------------------------------------------------
# 合成桩
# --------------------------------------------------------------------------

def speech(seconds: float, f0: float = 120.0, noise: float = 0.0,
           silence_at: tuple = None) -> np.ndarray:
    """造一段"像人声"的桩：3 次谐波 + 3Hz 音节调制。

    noise      : 叠加的白噪幅度（0.06 ≈ -24 dBFS，可闻底噪档）
    silence_at : (起秒, 止秒) 之间塞**数字静音**，模拟参考里夹的长停顿
    """
    n = int(seconds * SR)
    t = np.arange(n) / SR
    s = sum((1.0 / k) * np.sin(2 * math.pi * f0 * k * t) for k in (1, 2, 3))
    s = s / np.max(np.abs(s)) * 0.35
    am = (np.sin(2 * math.pi * 3.0 * t) + 1) / 2
    s = s * np.where(am < 0.15, 0.0, 1.0)
    if noise:
        s = s + noise * np.random.RandomState(1).randn(n)
    if silence_at:
        lo, hi = int(silence_at[0] * SR), int(silence_at[1] * SR)
        s[lo:hi] = 0.0
    return s.astype("float32")


def write_wav(path, x: np.ndarray, sr: int = SR) -> str:
    sf.write(str(path), x, sr, subtype="PCM_16")
    return str(path)


def sha256(path) -> str:
    return hashlib.sha256(open(str(path), "rb").read()).hexdigest()


# --------------------------------------------------------------------------
# 1. 四条 checks 的形状与各自的判/不判
# --------------------------------------------------------------------------

def test_c3_diagnose_emits_exactly_four_named_checks(tmp_path):
    """checks[] 恰为规格点名的四条：噪声/时长/多人/长静音，且每条带 severity。"""
    path = write_wav(tmp_path / "ok.wav", speech(4.0))
    rep = rf.diagnose(path, engine="voxcpm")
    assert [c["name"] for c in rep["checks"]] == CHECK_NAMES
    for c in rep["checks"]:
        assert isinstance(c["ok"], bool)
        assert c["severity"] in ("block", "warn", "info")
        assert c["detail"]                       # 每条都要能给人看明白为什么
    assert rep["verdict"] == "ok"
    # 度量齐备（这些是要进 voice_profiles.meta["ref_checks"] 的东西）
    for key in ("duration_s", "peak_dbfs", "noise_floor_dbfs", "longest_silence_s",
                "leading_silence_s", "trailing_silence_s", "f0_half_drift_semitones"):
        assert key in rep["metrics"], key


def test_c3_noise_floor_check_fires_on_hiss(tmp_path):
    """全程铺底噪（最静 10% 帧中值 > -38 dBFS）⇒ noise_floor 不 ok，但**只 warn**
    —— 底噪不拒收（可用性优先），拒收权在时长硬门。"""
    path = write_wav(tmp_path / "hiss.wav", speech(4.0, noise=0.06))
    rep = rf.diagnose(path, engine="voxcpm")
    by = {c["name"]: c for c in rep["checks"]}
    assert by["noise_floor"]["ok"] is False
    assert by["noise_floor"]["severity"] == "warn"
    assert rep["metrics"]["noise_floor_dbfs"] > rf.NOISE_FLOOR_DBFS
    # 底噪不改判词：时长合格 ⇒ 仍是 ok（warn 只提示）
    assert rep["verdict"] == "ok"
    assert by["duration"]["ok"] is True


def test_c3_long_silence_check_fires_on_dead_air(tmp_path):
    """参考中段夹 2s 数字静音（> 1s 门限）⇒ long_silence 不 ok。"""
    path = write_wav(tmp_path / "dead.wav", speech(6.0, silence_at=(2.0, 4.0)))
    rep = rf.diagnose(path, engine="voxcpm")
    by = {c["name"]: c for c in rep["checks"]}
    assert by["long_silence"]["ok"] is False
    assert rep["metrics"]["longest_silence_s"] >= 1.9
    assert by["duration"]["ok"] is True          # 6s 本身在 voxcpm 档内


def test_c3_multi_speaker_heuristic_flags_f0_jump(tmp_path):
    """前半/后半 F0 差 15 半音（换人/多人混录的启发式特征）⇒ single_speaker 不 ok。"""
    half = speech(3.0, f0=110.0)
    tail = speech(3.0, f0=330.0)
    x = np.concatenate([half, tail])
    # 换人判据要在**有音高**的段上测，去掉音节调制造成的静音谷以免干扰 pyin
    x = np.where(np.abs(x) < 1e-6, np.float32(0.0), x).astype("float32")
    path = write_wav(tmp_path / "two.wav", x)
    rep = rf.diagnose(path, engine="voxcpm")
    by = {c["name"]: c for c in rep["checks"]}
    drift = rep["metrics"]["f0_half_drift_semitones"]
    assert drift is not None
    assert drift > rf.SPEAKER_DRIFT_SEMITONES
    assert by["single_speaker"]["ok"] is False
    assert by["single_speaker"]["severity"] == "warn"


def test_c3_single_speaker_stays_info_when_f0_unmeasurable(tmp_path):
    """全静音（测不出 F0）时不许编"换人"结论 —— 该条降级为 info/ok。"""
    x = np.zeros(int(4.0 * SR), dtype="float32")
    rep = rf.diagnose(None, engine="voxcpm", x=x, sr=SR)
    by = {c["name"]: c for c in rep["checks"]}
    assert rep["metrics"]["f0_half_drift_semitones"] is None
    assert by["single_speaker"]["severity"] == "info"
    assert by["single_speaker"]["ok"] is True


# --------------------------------------------------------------------------
# 2. 引擎分档硬门（3-10s 那类）
# --------------------------------------------------------------------------

def test_c3_engine_tiers_drive_verdict():
    """同一份 12s 音频：GSV 档（3~10s 硬门）判 needs_fit，voxcpm 档（3~15s）判 ok。

    分档是 US-C3 的核心：`TTS.py:826-829` 越界是**抛错**而不是"效果差一点"，
    所以时长这条 severity 必须是 block。
    """
    x = speech(12.0)
    gsv = rf.diagnose(None, engine="gptsovits", x=x, sr=SR)
    vox = rf.diagnose(None, engine="voxcpm", x=x, sr=SR)
    assert rf.ref_window_for("gptsovits") == (3.0, 10.0)
    assert gsv["verdict"] == "needs_fit" and vox["verdict"] == "ok"
    dur_gsv = [c for c in gsv["checks"] if c["name"] == "duration"][0]
    assert dur_gsv["ok"] is False and dur_gsv["severity"] == "block"
    # 过短径：明确 needs_longer_sample，且明细里写清"不做时间拉伸"
    short = rf.diagnose(None, engine="gptsovits", x=speech(1.5), sr=SR)
    assert short["verdict"] == "needs_longer_sample"
    dur_short = [c for c in short["checks"] if c["name"] == "duration"][0]
    assert "不做时间拉伸" in dur_short["detail"]
    # 未知引擎退回默认档（3~10s），不抛
    assert rf.ref_window_for("no-such-engine") == rf.DEFAULT_REF_WINDOW


# --------------------------------------------------------------------------
# 3. 治疗：能量窗裁剪，**只写新文件**
# --------------------------------------------------------------------------

def test_c3_best_energy_window_picks_the_loud_band():
    """前 8s 安静、中段 8s 响亮：目标窗必须落在响亮段里。"""
    quiet = speech(8.0) * 0.004
    loud = speech(8.0, f0=140.0)
    tail = speech(6.0) * 0.004
    x = np.concatenate([quiet, loud, tail])
    win = rf.best_energy_window(x, SR)
    assert win is not None
    assert rf.FIT_MIN_S <= win["seconds"] <= rf.FIT_MAX_S
    assert win["start_s"] >= 7.5 and win["end_s"] <= 16.5


def test_c3_fit_reference_writes_new_file_and_never_touches_original(tmp_path):
    """fit 另存 `ref_*_fit.wav`，原件字节不变 ⇒ voice_cloner 的 sha256 幂等护栏不破。"""
    x = np.concatenate([speech(8.0) * 0.004, speech(8.0, f0=140.0), speech(6.0) * 0.004])
    src = tmp_path / "ref_original.wav"
    src_path = write_wav(src, x)
    src_hash = sha256(src)
    dst = tmp_path / "ref_deadbeef_fit.wav"

    info = rf.fit_reference(src_path, str(dst))
    assert info is not None
    assert sha256(src) == src_hash, "原件被动过 —— sha256 幂等护栏会被打穿"
    assert Path(dst).exists()
    y, sr = rf.load_mono(str(dst))
    assert sr == SR
    assert rf.FIT_MIN_S - 0.1 <= len(y) / sr <= rf.FIT_MAX_S + 0.1
    assert info["path"] == str(dst) and info["source_path"] == src_path
    # 裁出来的确实是响亮段（RMS 远高于整段平均）
    assert float(np.sqrt(np.mean(y.astype("float64") ** 2))) > 0.05


def test_c3_prepare_reference_three_routes(tmp_path):
    """入库统一入口的三条径：合格直接用 / 过长裁一刀 / 过短退回重录（不产生 fit 副本）。"""
    # (a) 合格 —— 不写任何 fit 文件
    good = write_wav(tmp_path / "a_good.wav", speech(5.0))
    r = rf.prepare_reference(good, engine="voxcpm", dst_dir=str(tmp_path), stem="good")
    assert r["verdict"] == "ok" and r["use_path"] == good and r["fit"] is None
    assert list(tmp_path.glob("ref_*_fit.wav")) == []

    # (b) 过长 —— 写 ref_{stem}_fit.wav 并改指 fit 副本
    long_x = np.concatenate([speech(8.0) * 0.004, speech(8.0, f0=140.0), speech(6.0) * 0.004])
    too_long = write_wav(tmp_path / "b_long.wav", long_x)
    before = sha256(too_long)
    r = rf.prepare_reference(too_long, engine="voxcpm", dst_dir=str(tmp_path), stem="long")
    assert r["verdict"] == "fitted"
    fit = tmp_path / "ref_long_fit.wav"
    assert fit.exists() and r["fit_path"] == str(fit) and r["use_path"] == str(fit)
    assert sha256(too_long) == before
    assert r["fit"]["seconds"] <= rf.FIT_MAX_S + 0.1

    # (c) 过短 —— needs_longer_sample，**绝不做时间拉伸**，也不落 fit 副本
    short = write_wav(tmp_path / "c_short.wav", speech(1.5))
    r = rf.prepare_reference(short, engine="gptsovits", dst_dir=str(tmp_path), stem="short")
    assert r["verdict"] == "needs_longer_sample"
    assert r["use_path"] == short and r["fit"] is None and r["fit_path"] is None
    assert not (tmp_path / "ref_short_fit.wav").exists()

    # (d) 只诊断模式（不给 dst_dir）：过长也只报 needs_fit，不落盘
    r = rf.prepare_reference(too_long, engine="gptsovits")
    assert r["verdict"] == "needs_fit" and r["fit_path"] is None
    assert list(tmp_path.glob("ref_*_fit.wav")) == [fit]     # 没多出新的


def test_c3_undecodable_is_unusable_not_crash(tmp_path):
    """解码不了的文件要 verdict=unusable + 一条 block 的 decodable check，不上抛。"""
    bad = tmp_path / "junk.wav"
    bad.write_bytes(b"not a wav at all")
    rep = rf.diagnose(str(bad), engine="voxcpm")
    assert rep["verdict"] == "unusable"
    assert rep["checks"][0]["name"] == "decodable"
    assert rep["checks"][0]["severity"] == "block"
    r = rf.prepare_reference(str(bad), engine="voxcpm", dst_dir=str(tmp_path), stem="junk")
    assert r["verdict"] == "unusable" and r["fit"] is None


def test_c3_f0_constants_are_defined():
    """护栏：`_f0_halves()` 在 librosa 的 try **之前**就用这三个常量算窗长，
    少定义任意一个都会让 diagnose() 对一切可解码音频抛 NameError（实测踩过一次）。"""
    for name in ("F0_ANALYZE_S", "F0_FRAME_SAMPLES", "F0_HOP_SAMPLES"):
        assert hasattr(rf, name), f"ref_fitting 缺常量 {name} ⇒ diagnose() 必抛 NameError"
    assert rf.F0_FRAME_SAMPLES >= rf.F0_HOP_SAMPLES
    # 短到不足 0.3s 的输入也不能抛，只能"测不出"
    assert rf.diagnose(None, "voxcpm", x=np.zeros(100, dtype="float32"), sr=SR)
