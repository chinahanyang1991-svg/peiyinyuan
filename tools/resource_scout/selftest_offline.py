"""Offline self-test for the resource pipeline - no network, no upstream data.

This exists because outbound network access is blocked in this environment
(see BLOCKERS.md), so build_library.py cannot be run for real here. Rather than
ship untested code, this harness drives the *same* functions build_library.py
uses - speech_window, metrics, make_master, ingest_decoded, the SQLite schema
and manifest.jsonl - against locally synthesised speech-like fixtures, and keeps
them in tools/resource_scout/logs/selftest/ so they can never be mistaken for
harvested corpus material or counted in data/resource.db.

Fixtures
  12 positive: varying F0 (male/female), prosody (calm/animated), source rates
   3 negative: digital silence, 2 s clip, hard-clipped square wave
The negatives prove the rejection path works, which is what `verified` is for.

Usage:  python selftest_offline.py
Then:   python verify_samples.py --db logs/selftest/resource.db
        python import_to_voices.py --resource-db logs/selftest/resource.db --dry-run
"""

import json
import os
import shutil
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import build_library as B      # noqa: E402
import rs_audio as A           # noqa: E402
import rs_db as D              # noqa: E402
import rs_net as N             # noqa: E402

SELF = HERE / "logs" / "selftest"
OUT = SELF / "out"
DB = SELF / "resource.db"

SPEC = dict(
    key="selftest_synthetic", kind="selftest", repo="local-synth",
    license="SYNTHETIC-FIXTURE-NOT-REDISTRIBUTABLE", license_url="n/a",
    page="local://tools/resource_scout/selftest_offline.py",
    attribution="locally synthesised fixture, no upstream corpus",
    language="zh", path_in=(), path_skip=(), speaker_cols=(), gender_cols=(),
    age_cols=(), emotion_cols=(), per_source_voices=99, samples_per_voice=1,
    note="pipeline self-test fixture", max_file_mb=0, max_rg_mb=0, max_obj_mb=0)


def synth(seed, f0, dur, sr, style="calm", rms_target=0.09, gaps=True, clip=False,
          silence=False):
    """Source-filter-ish synthetic voice: harmonic stack + syllabic envelope."""
    rng = np.random.default_rng(seed)
    t = np.arange(int(dur * sr)) / float(sr)
    if silence:
        return (rng.standard_normal(t.shape) * 1e-5).astype(np.float32)
    if clip:
        sq = np.sign(np.sin(2 * np.pi * f0 * t)) * 0.999
        return sq.astype(np.float32)
    vib = 1.0 + (0.02 if style == "calm" else 0.10) * np.sin(2 * np.pi * 4.5 * t + rng.random())
    drift = 1.0 + (0.01 if style == "calm" else 0.06) * np.sin(2 * np.pi * 0.6 * t)
    contour = f0 * vib * drift
    phase = 2 * np.pi * np.cumsum(contour) / sr
    x = np.zeros_like(t)
    f1, f2 = (500.0, 1500.0) if f0 < 150 else (650.0, 2100.0)
    for h in range(1, 22):
        fr = h * f0
        if fr > sr / 2.2:
            break
        amp = 1.0 / (h ** 1.3)
        amp *= 1.0 + 2.2 * np.exp(-0.5 * ((fr - f1) / 120.0) ** 2)
        amp *= 1.0 + 1.6 * np.exp(-0.5 * ((fr - f2) / 220.0) ** 2)
        x += amp * np.sin(h * phase + rng.random() * 0.4)
    rate = 3.0 if style == "calm" else 5.2
    syl = 0.55 + 0.45 * np.abs(np.sin(2 * np.pi * rate * t))
    if gaps:                                   # inter-sentence pauses
        pauses = np.ones_like(t)
        for k in range(1, 4):
            c = int(t.shape[0] * k / 4.0)
            w = int(0.22 * sr)
            a = max(0, c - w // 2)
            pauses[a:c + w // 2] *= np.linspace(1, 0.02, pauses[a:c + w // 2].shape[0]) \
                if c + w // 2 < t.shape[0] else 0.02
        syl *= pauses
    x = x * syl
    x += rng.standard_normal(x.shape) * 0.004 * (rms_target or 1.0)
    x = x / (np.max(np.abs(x)) + 1e-9) * min(0.85, rms_target * 3.6)
    return x.astype(np.float32)


def make_ctx():
    class Args:
        dry_run = False
        db = str(DB)
        out = str(OUT)
        max_voices = 400
        samples_per_voice = 1
        min_dur = 5.0
        max_dur = 20.0
        limit = 0
        sources = []
        budget_mb = 500
    N.set_budget(500 * (1 << 20))
    SPEC["_resource"] = B.resource_rec(SPEC)
    return B.Ctx(Args())


def main():
    if SELF.exists():
        shutil.rmtree(SELF)
    OUT.mkdir(parents=True, exist_ok=True)
    ctx = make_ctx()
    cases = [
        ("m_adult_calm", 112, 12.0, 24000, "calm", "male", "adult"),
        ("m_narr_deep", 96, 14.0, 16000, "calm", "male", "senior"),
        ("m_young_anim", 128, 9.0, 44100, "anim", "male", "young"),
        ("m_mid_strong", 105, 11.0, 22050, "anim", "male", "adult"),
        ("f_adult_calm", 196, 12.5, 24000, "calm", "female", "adult"),
        ("f_young_bright", 232, 8.5, 16000, "anim", "female", "young"),
        ("f_senior_soft", 176, 15.0, 48000, "calm", "female", "senior"),
        ("f_mid_warm", 208, 10.0, 22050, "anim", "female", "adult"),
        ("m_child", 285, 7.0, 24000, "anim", "male", "child"),
        ("f_child", 310, 6.5, 16000, "calm", "female", "child"),
        ("m_low_rms", 118, 9.5, 24000, "calm", "male", "adult"),
        ("f_fast", 221, 8.0, 44100, "anim", "female", "young"),
        ("BAD_silence", 120, 9.0, 24000, "silence", "", ""),
        ("BAD_tooshort", 110, 2.0, 24000, "calm", "", ""),
        ("BAD_clipped", 100, 9.0, 24000, "clip", "", ""),
    ]
    results = []
    for i, (name, f0, dur, sr, style, exp_gender, age) in enumerate(cases):
        rms_t = 0.002 if name == "m_low_rms" else 0.09
        x = synth(1000 + i, f0, dur, sr, style="anim" if style == "anim" else "calm",
                  rms_target=rms_t, gaps=(style != "clip"),
                  silence=(style == "silence"), clip=(style == "clip"))
        attrs = dict(gender="unknown", age_band=age or "unknown",
                     language="zh" if i % 2 == 0 else "en",
                     style="unknown", emotion="unknown",
                     attr_source="fixture:none", name_hint=name)
        spec = dict(SPEC)
        spec["_resource"] = B.resource_rec(SPEC)
        ok = B.ingest_decoded(ctx, x, sr, spec, "spk_" + A.safe_slug(name),
                              "%s/%s.wav" % (name, name), attrs, src_fmt="WAV")
        row = ctx.db.sample_exists("selftest_synthetic:%s/%s.wav" % (name, name))
        m = A.metrics(x, sr)
        results.append(dict(case=name, expected=exp_gender or "reject",
                            ingested=bool(ok), in_db=bool(row),
                            f0_measured=m["f0_median"], rms=m["rms"],
                            dur=m["duration_s"]))
    ctx.db.recompute_voice_counts()
    ctx.db.log("selftest_offline", "run", "fixtures", "done", 0,
               "ingested=%d of %d" % (ctx.n_samples, len(cases)))
    print("=" * 78)
    print("离线自检 (synthetic fixtures, 不属于采集资源库)")
    print("db=%s  out=%s" % (DB, OUT))
    print("-" * 78)
    print("%-16s %-8s %-9s %-8s %8s %8s" % ("case", "expected", "ingested", "in_db",
                                             "f0_meas", "rms"))
    for r in results:
        print("%-16s %-8s %-9s %-8s %8.1f %8.5f" % (
            r["case"], r["expected"], r["ingested"], bool(r["in_db"]),
            r["f0_measured"], r["rms"]))
    good = [r for r in results if not r["case"].startswith("BAD")
            and not r["case"] == "m_low_rms"]
    gender_ok = sum(1 for r in good
                    if (ctx.db.rows("SELECT gender FROM voice_samples WHERE uid LIKE ?",
                                    ("%" + r["case"] + "%",))
                       and ctx.db.rows("SELECT gender FROM voice_samples WHERE uid LIKE ?",
                                       ("%" + r["case"] + "%",))[0]["gender"] == r["expected"]))
    rejected = [r for r in results if (r["case"].startswith("BAD")
                                       or r["case"] == "m_low_rms") and not r["ingested"]]
    print("-" * 78)
    print("正向样本入库 %d/%d | 负向样本被拒 %d/%d | F0 性别判定一致 %d/%d" % (
        sum(1 for r in good if r["ingested"]), len(good), len(rejected),
        len(results) - len(good), gender_ok, len(good)))
    print("voices=%d samples=%d skipped=%d transferred=%s" % (
        ctx.n_voices, ctx.n_samples, ctx.skipped, N._fmt(N.transferred())))
    (SELF / "selftest_results.json").write_text(
        json.dumps(dict(results=results, voices=ctx.n_voices, samples=ctx.n_samples,
                        skipped=ctx.skipped), ensure_ascii=False, indent=1),
        encoding="utf-8")
    ctx.db.close()
    print("下一步验证命令:")
    print("  python tools/resource_scout/verify_samples.py --db %s" % DB)
    print("  python tools/resource_scout/import_to_voices.py --resource-db %s --dry-run" % DB)
    return 0


if __name__ == "__main__":
    sys.exit(main())
