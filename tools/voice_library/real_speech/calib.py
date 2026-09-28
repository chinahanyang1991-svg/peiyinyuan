# -*- coding: utf-8 -*-
"""Calibrate the real-speech gate from measured distributions (no hand-set numbers).

Why a second attempt: the first two calibrations pooled every negative together and asked
each metric to reject the *whole* pool, which neutralised all 11 soft criteria - a bed mix
and a sine tone are different failure modes and no single number separates both at once.
This version is paired: every soft metric is judged only against the failure mode it exists
to detect, and the numbers below are the measured outcome of that judgement.

Positives  real speech from the harvested raw files (AISHELL-3 studio Mandarin, VCTK studio
           English, Commons survivors).  Each is decoded, edge-trimmed, resampled to
           48 kHz and peak-normalised exactly like process.py; only clips whose trimmed
           length is >= 3.0 s are used, so duration never confounds a soft metric.

Negatives  per failure mode, each one what its metric is supposed to catch:
  sfx       raw SFX already in data/voices/library/{animal,electronic,mechanical}
  tone      periodic non-speech tone at the clip's own F0        -> zcr / mod_prom / band
  noisy     band-limited noise at ~3 dB SNR                      -> flat / pitch_sal / voiced
  overlap   two different speakers mixed at 0 dB / -6 dB          -> pitch_cv
  bgm_bed   speech + steady harmonic bed at ~ -8 dBFS             -> quiet_corr / spec_stab
  blip      two short bursts separated by silence (survives trimming) -> silence ratio

Rule per soft metric:
  * prefer the published refill criterion whenever it keeps >= 95 % of the real speech
    measured here (cross-checks the sibling lane instead of reinventing numbers);
  * otherwise take the positive distribution's own p03 / p97 as the cut;
  * keep the metric if that cut rejects >= 75 % of at least ONE failure mode it was
    named for - a criterion aimed at noise is not worthless because the generic SFX pool
    is already caught by other criteria; a published refill cut additionally survives
    when it costs no measured speech at all;
  * if neither holds it is declared non-discriminative and neutralised instead of acting
    as a false gate (the first uncalibrated run showed a guessed gate costs ~80 % yield).
  What is finally reported is the ensemble pass rate of every negative group.
Hard gates fixed by the lane spec (duration 3.0-10.0 s, 0.02 <= peak <= 0.99,
silence <= 0.50) are reported, never relaxed.

Outputs calib_report.json (evidence) and thresholds.json (consumed by gate.py).
"""
import os, sys, json, random, time
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C
import gate as G

SEED = 11
POS_PER_SOURCE = int(os.environ.get("RS_CALIB_POS", "80"))
NEG_SFX_TOTAL = int(os.environ.get("RS_CALIB_NEG", "70"))
SYNTH_PER_SHAPE = int(os.environ.get("RS_CALIB_SYNTH", "40"))
TPR_FLOOR = 0.95          # share of real speech a cut must keep
REJ_FLOOR = 0.75          # share of the targeted failure mode it must reject
SANITY_FLOOR = 0.97       # band sanity checks: keep this much real speech, no negative
SR = 48000

# tkey            feature      direction  targeted failure modes            refill published
SPECS = [
    ("flat_max",        "flat",        "max", ["noisy", "sfx"],        0.18),
    ("zcr_min",         "zcr",         "min", ["tone"],                0.015),
    ("zcr_max",         "zcr",         "max", ["noisy"],               0.280),
    ("mod_prom_min",    "mod_prom",    "min", ["tone", "blip", "sfx"], 8.0),
    ("hnr_db_min",      "hnr_db",      "min", ["noisy"],               None),
    ("pitch_sal_min",   "pitch_sal",   "min", ["noisy"],               None),
    ("voiced_frac_min", "voiced_frac", "min", ["noisy"],               None),
    ("pitch_cv_max",    "pitch_cv",    "max", ["overlap", "overlap6"], None),
    ("spec_stab_max",   "spec_stab",   "max", ["bgm_bed"],              None),
    ("quiet_corr_max",  "quiet_corr",  "max", ["bgm_bed"],              None),
    ("mod_fpeak_min",   "mod_f",       "min", [],                       1.9),
    ("mod_fpeak_max",   "mod_f",       "max", [],                       8.4),
]
FIXED = {"dur_min": 3.0, "dur_max": 10.0, "peak_min": 0.02, "peak_max": 0.99,
         "silence_ratio_max": 0.50}
FIXED_SPECS = [("silence_ratio_max", "sil_ratio", "max", ["blip"])]


def prep(path):
    x, sr = C.load_any(path)
    x = C.trim_edges(x, sr)
    x, sr = C.to_target_sr(x, sr)
    g = C.gain_policy(float(np.abs(x).max()))
    if g is None:
        return None
    return (x * g).astype(np.float32), sr


def load_positives():
    rnd = random.Random(SEED)
    out = []
    for src in ("aishell3", "vctk", "commons"):
        d = os.path.join(C.RAW_DIR, src)
        if not os.path.isdir(d):
            continue
        fs = [os.path.join(d, f) for f in sorted(os.listdir(d))
              if f.lower().endswith((".wav", ".flac", ".ogg", ".oga", ".opus", ".mp3"))]
        rnd.shuffle(fs)
        used = 0
        for p in fs:
            if used >= POS_PER_SOURCE:
                break
            try:
                x, sr = prep(p)
            except Exception:
                continue
            if len(x) / float(sr) < G.THRESH["dur_min"]:
                continue
            out.append({"path": os.path.basename(p), "source": src, "x": x, "sr": sr})
            used += 1
    return out


def library_sfx():
    lib = os.path.join(C.ROOT, "data", "voices", "library")
    rnd = random.Random(SEED)
    files = []
    for d in ("animal", "electronic", "mechanical"):
        p = os.path.join(lib, d)
        if os.path.isdir(p):
            files += [os.path.join(p, f) for f in os.listdir(p) if f.endswith(".wav")]
    rnd.shuffle(files)
    out = []
    for f in files[:NEG_SFX_TOTAL]:
        try:
            x, sr = C.load_any(f)
        except Exception:
            continue
        out.append({"path": os.path.relpath(f, lib), "source": "sfx",
                    "x": np.asarray(x, dtype=np.float32), "sr": sr})
    return out


def _mix(a, b, db):
    b = b[:len(a)] if len(b) >= len(a) else np.tile(b, int(np.ceil(len(a) / len(b))))[:len(a)]
    pa = np.sqrt(np.mean(a ** 2)) + 1e-9
    pb = np.sqrt(np.mean(b ** 2)) + 1e-9
    return (a + b * (pa / pb) * (10 ** (db / 20.0))).astype(np.float32)


def synth_negatives(pos):
    """Failure-mode clips built from the positives (deterministic, seed fixed)."""
    rnd = random.Random(SEED)
    groups = {}
    pairs = [(pos[i], pos[(i + 7) % len(pos)]) for i in range(len(pos))]
    for kind in ("overlap", "overlap6", "bgm_bed", "noisy", "tone", "blip"):
        out = []
        picks = rnd.sample(pairs, min(SYNTH_PER_SHAPE, len(pairs)))
        for a, b in picks:
            x, sr = a["x"], a["sr"]
            if kind.startswith("overlap"):
                db = 0.0 if kind == "overlap" else -6.0
                y = _mix(x, b["x"], db)
            elif kind == "bgm_bed":
                t = np.arange(len(x)) / sr
                bed = sum(np.sin(2 * np.pi * 110.0 * k * t) / k for k in (1, 2, 3, 4, 5, 6))
                bed = (bed / np.abs(bed).max() * 0.18).astype(np.float32)   # ~ -8 dBFS bed
                y = (x + bed[:len(x)]).astype(np.float32)
            elif kind == "noisy":
                rs = np.random.RandomState(SEED)
                n = rs.randn(len(x) + 8).astype(np.float32)
                n = np.convolve(n, np.ones(5, dtype=np.float32) / 5.0, mode="same")[:len(x)]
                n = (n / (np.sqrt(np.mean(n ** 2)) + 1e-9)).astype(np.float32)
                px = np.sqrt(np.mean(x ** 2))
                y = (x + n * px / np.sqrt(2.0)).astype(np.float32)           # ~3 dB SNR
            elif kind == "tone":
                fr = np.asarray(G._frames(x), dtype=np.float64)
                f0arr, _sal = G._pitch_track(fr, sr)
                f0v = float(np.median(f0arr)) if len(f0arr) else 200.0
                f0v = max(80.0, min(350.0, f0v))
                t = np.arange(len(x)) / sr
                y = (0.7 * np.sin(2 * np.pi * f0v * t)).astype(np.float32)
            else:                                                            # blip
                # two bursts with silence BETWEEN them: edge trimming cannot hide it, so
                # this really tests the silence-ratio gate rather than trim_edges().
                y = np.zeros_like(x)
                n = int(len(x) * 0.08)
                y[:n] = x[:n]
                st = int(len(x) * 0.60)
                y[st:st + n] = x[n:2 * n]
            out.append({"path": "%s<-%s|%s" % (kind, a["path"], b["path"]),
                        "source": kind, "x": y, "sr": sr})
        groups[kind] = out
    return groups


def feats(rows):
    for r in rows:
        r.update(G.features(np.asarray(r["x"], dtype=np.float64), r["sr"]))
    return rows


def pct(v, q):
    return float(np.percentile(np.asarray(v, float), q)) if len(v) else 0.0


def passes(v, tkey, direction, th):
    t = th[tkey]
    return (v >= t) if direction == "min" else (v <= t)


def cut_for(direction, pv, ref):
    """Candidate cut: the published refill value if it keeps >= TPR_FLOOR of the speech,
    otherwise the speech distribution's own p03 / p97."""
    arr = np.asarray(pv, float)
    if ref is not None:
        keep = float((arr >= ref).mean() if direction == "min" else (arr <= ref).mean())
        if keep >= TPR_FLOOR:
            return float(ref), "refill published (keeps %0.3f of speech)" % keep
    t = pct(arr, 3.0 if direction == "min" else 97.0)
    return t, "speech p03/p97 percentile"


def calibrate(pos, groups, th):
    """Fill th[tkey] from measurements; return the per-metric evidence block."""
    metrics = {}
    for tkey, key, direction, targets, ref in SPECS:
        pv = [r[key] for r in pos]
        t, how = cut_for(direction, pv, ref)
        tpr = float(np.mean([passes(v, tkey, direction, {tkey: t}) for v in pv]))
        rej = {}
        for g in targets:
            gv = [r[key] for r in groups[g]]
            if gv:
                rej[g] = round(float(np.mean([not passes(v, tkey, direction, {tkey: t})
                                              for v in gv])), 4)
        worst = min(rej.values()) if rej else None
        best = max(rej.values()) if rej else None
        floor = TPR_FLOOR if targets else SANITY_FLOOR
        published_no_cost = how.startswith("refill published") and tpr >= 0.999
        keep = (tpr >= floor) and (not targets or best >= REJ_FLOOR or published_no_cost)
        if not keep:
            dec = "neutralised"
        elif targets and best < REJ_FLOOR:
            dec = "kept_published_no_cost"
        else:
            dec = "kept"
        metrics[tkey] = {
            "threshold_key": tkey, "feature": key, "direction": direction,
            "role": "targeted" if targets else "sanity_band",
            "targets": targets, "published_refill": ref, "cut": round(t, 5), "cut_from": how,
            "speech_kept": round(tpr, 4), "speech_kept_floor": floor,
            "targets_rejected": rej, "worst_target_rejected": worst,
            "best_target_rejected": best, "published_no_cost": published_no_cost,
            "other_groups_rejected": {
                g: round(float(np.mean([not passes(v, tkey, direction, {tkey: t})
                                        for v in [r[key] for r in groups[g]]])), 4)
                for g in groups if g not in targets},
            "percentiles_speech": {str(q): round(pct(pv, q), 4) for q in (0, 3, 50, 97, 100)},
            "percentiles_negatives": {
                g: {str(q): round(pct([r[key] for r in groups[g]], q), 4)
                    for q in (0, 5, 50, 95, 100)} for g in groups},
            "decision": dec,
        }
        if not keep:
            metrics[tkey]["neutralise_reason"] = (
                "speech_kept_%0.3f_below_%0.2f" % (tpr, floor) if tpr < floor else
                "target_leak_%s" % (json.dumps(rej, ensure_ascii=False)))
            t = -G.NEUTRAL if direction == "min" else G.NEUTRAL
        th[tkey] = t
    for tkey, key, direction, targets in FIXED_SPECS:
        pv = [r[key] for r in pos]
        t = FIXED[tkey]
        rej = {g: round(float(np.mean([not passes(v, tkey, direction, {tkey: t})
                                        for v in [r[key] for r in groups[g]]])), 4)
               for g in targets}
        metrics[tkey] = {
            "threshold_key": tkey, "feature": key, "direction": direction,
            "targets": targets, "cut": t, "cut_from": "fixed by lane spec (never relaxed)",
            "speech_kept": round(float(np.mean([passes(v, tkey, direction, {tkey: t})
                                                for v in pv])), 4),
            "targets_rejected": rej, "decision": "fixed_by_spec"}
        th[tkey] = t
    return metrics


def score(rows, th):
    ok, why = 0, {}
    for r in rows:
        bad = []
        if not (th["dur_min"] <= r["dur"] <= th["dur_max"]):
            bad.append("duration_out_of_band")
        if r["peak"] < th["peak_min"] or r["peak"] > th["peak_max"]:
            bad.append("peak")
        for tkey, key, direction, _t in [(s[0], s[1], s[2], s[3]) for s in SPECS] + FIXED_SPECS:
            if direction == "min" and r[key] < th[tkey]:
                bad.append(tkey)
            if direction == "max" and r[key] > th[tkey]:
                bad.append(tkey)
        if not bad:
            ok += 1
        for b in bad:
            why[b] = why.get(b, 0) + 1
    return ok, why


def main():
    pos = feats(load_positives())
    groups = {"sfx": feats(library_sfx())}
    for k, v in synth_negatives(pos).items():
        groups[k] = feats(v)
    neg = [r for g in groups.values() for r in g]
    th = dict(FIXED)
    metrics = calibrate(pos, groups, th)

    pos_ok, pos_why = score(pos, th)
    per_group = {}
    for g, rows in groups.items():
        ok, why = score(rows, th)
        per_group[g] = {"n": len(rows), "pass_gate": ok, "pass_rate": round(ok / max(1, len(rows)), 4),
                        "reject_reasons": why}
    report = {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "seed": SEED,
              "method": ("paired: each soft metric judged against the failure mode it exists "
                         "to reject; published refill cut preferred when it keeps >=95%% of "
                         "the measured speech, else speech p03/p97; kept only if it rejects "
                         ">=%.0f%% of its target group" % (REJ_FLOOR * 100)),
              "acceptance": {"tpr_floor": TPR_FLOOR, "target_reject_floor": REJ_FLOOR,
                             "sanity_band_floor": SANITY_FLOOR},
              "counts": {"pos": len(pos), "negatives": {g: len(r) for g, r in groups.items()}},
              "pos_sources": {}, "fixed_gates": FIXED, "metrics": metrics,
              "result": {"pos_pass": pos_ok, "pos_reject_reasons": pos_why,
                         "negatives": per_group},
              "rows_pos": [{k: (round(v, 4) if isinstance(v, float) else v)
                            for k, v in r.items() if k != "x"} for r in pos],
              "rows_neg": [{k: (round(v, 4) if isinstance(v, float) else v)
                            for k, v in r.items() if k != "x"} for r in neg]}
    for r in pos:
        report["pos_sources"][r["source"]] = report["pos_sources"].get(r["source"], 0) + 1
    C.write_json(os.path.join(C.SELF_DIR, "calib_report.json"), report)
    out = {k: v for k, v in th.items() if k not in FIXED}
    out["_calibrated_at"] = report["generated_at"]
    out["_basis"] = ("pos=%d from %s; speech_pass=%d/%d; negative_pass_rate=%s" % (
        len(pos), report["pos_sources"], pos_ok, len(pos),
        {g: per_group[g]["pass_rate"] for g in per_group}))
    C.write_json(os.path.join(C.SELF_DIR, "thresholds.json"), out)
    print(json.dumps({
        "counts": report["counts"],
        "kept": {k: m["cut"] for k, m in metrics.items() if m["decision"] != "neutralised"},
        "neutralised": {k: m.get("neutralise_reason") for k, m in metrics.items()
                        if m["decision"] == "neutralised"},
        "speech_pass": "%d/%d" % (pos_ok, len(pos)),
        "neg_pass_rate": {g: per_group[g]["pass_rate"] for g in per_group}},
        ensure_ascii=False)[:2000])


if __name__ == "__main__":
    main()
