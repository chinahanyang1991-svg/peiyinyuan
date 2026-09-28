# -*- coding: utf-8 -*-
"""Speech gate for the real-speech lane.

Criterion families copied from tools/voice_library/refill/gate.py (read-only sibling; we
do NOT import or edit it) and extended with speech-specific measurements:

  reused   dur / peak / rms / silence ratio / spectral flatness / ZCR /
           syllabic modulation prominence + peak frequency
  added    hnr_db        harmonic-to-noise ratio from autocorrelation on voiced frames
           pitch_sal      mean autocorrelation pitch saliency (0..1)
           pitch_cv       dispersion of F0 across voiced frames (overlap speech -> high)
           voiced_frac    share of active frames that are periodic (0.5-8kHz f0 range)
           spec_stab      frame-to-frame spectral-bin correlation; steady BGM/music -> high
           quiet_corr     the same correlation on the quietest active frames only, i.e. the
                          gaps where a background bed is all that remains

Duration band is the GPT-SoVITS clone hard gate: 3.0s .. 10.0s inclusive.
Soft (non-hard) cuts are MEASURED, never guessed: calib.py calibrates them from positives
(real corpus clips read from disk) against per-failure-mode negatives and writes
thresholds.json + calib_report.json.  Without that file the added criteria are NEUTRAL,
so an uncalibrated run falls back to the refill-copied criteria only.
"""
import os, json
import numpy as np
import soundfile as sf
from scipy.signal import welch

SR_FRAME = 48000
FRAME, HOP = 2048, 512
# a threshold set to NEUTRAL can never reject (JSON-safe stand-in for +/-inf)
NEUTRAL = 1e9

# Refill-copied criteria (tools/voice_library/refill/gate.py, read-only sibling) keep their
# published values here; the lane's own additions start NEUTRAL because a hand-guessed
# cut is worse than no cut: the first uncalibrated run rejected 286/350 genuine AISHELL-3
# clips on `hnr_db_min 2.0`.  calib.py measures the real cuts -> thresholds.json.
THRESH = {
    "dur_min": 3.0, "dur_max": 10.0,          # hard clone-reference band
    "peak_min": 0.02, "peak_max": 0.99,
    "silence_ratio_max": 0.50,
    "flat_max": 0.18,
    "zcr_min": 0.015, "zcr_max": 0.280,
    "mod_prom_min": 8.0,
    "mod_fpeak_min": 1.9, "mod_fpeak_max": 8.4,
    # speech-specific (calibrated, see calib_report.json)
    "hnr_db_min": -NEUTRAL,
    "pitch_sal_min": -NEUTRAL,
    "pitch_cv_max": NEUTRAL,
    "voiced_frac_min": -NEUTRAL,
    "spec_stab_max": NEUTRAL,
    "quiet_corr_max": NEUTRAL,
}

# calib.py rewrites thresholds.json from measured distributions; when present it wins.
THRESH_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "thresholds.json")
THRESH_SOURCE = "gate.py defaults (uncalibrated)"
if os.path.exists(THRESH_PATH):
    try:
        _t = json.load(open(THRESH_PATH, encoding="utf-8"))
        _fixed = {k: THRESH[k] for k in ("dur_min", "dur_max", "peak_min", "peak_max",
                                         "silence_ratio_max")}
        THRESH.update(_t)
        THRESH.update(_fixed)          # hard gates are never relaxed by calibration
        THRESH_SOURCE = "thresholds.json (calib.py, %s)" % _t.get("_calibrated_at", "?")
    except Exception:
        pass


def _frames(x, frame=FRAME, hop=HOP):
    n = 1 + max(0, (len(x) - frame)) // hop
    idx = np.arange(frame)[None, :] + hop * np.arange(n)[:, None]
    return x[idx.astype(np.intp)]


def _pitch_track(f, sr):
    """Autocorrelation pitch period + saliency per frame (vectorised over lag window)."""
    lagmin, lagmax = int(sr / 400.0), int(sr / 75.0)          # 75..400 Hz
    f = f - f.mean(axis=1, keepdims=True)
    e = (f ** 2).sum(axis=1) + 1e-12
    n = f.shape[1]
    spec = np.fft.rfft(f, n=2 * n, axis=1)
    ac = np.fft.irfft(np.abs(spec) ** 2, n=2 * n, axis=1)[:, :n]
    lags = np.arange(lagmin, lagmax)
    a = ac[:, lags]
    best = a.argmax(axis=1)
    lag = lags[best]
    sal = np.clip(a[np.arange(len(a)), best] / e, 0, 1)
    f0 = sr / lag.astype(float)
    return f0, sal


def features(x, sr):
    x = np.asarray(x, dtype=np.float64).ravel()
    dur = len(x) / float(sr)
    peak = float(np.abs(x).max()) if len(x) else 0.0
    rms = float(np.sqrt(np.mean(x ** 2))) if len(x) else 0.0
    f = _frames(x)
    fr = np.sqrt((f ** 2).mean(axis=1)) + 1e-12
    fr_max = fr.max() if len(fr) else 0.0
    active = fr > 0.06 * fr_max
    if active.sum() < 8:
        return dict(dur=dur, peak=peak, rms=rms, sil_ratio=1.0, flat=1.0, zcr=1.0,
                    mod_prom=0.0, mod_f=0.0, n_act=int(active.sum()), hnr_db=-99.0,
                    pitch_sal=0.0, pitch_cv=1.0, voiced_frac=0.0, spec_stab=1.0,
                    quiet_corr=0.0, f0_med=0.0)
    idx = np.where(active)[0]
    a, b = idx[0], idx[-1]
    sil_ratio = float(1.0 - active[a:b + 1].mean())

    W = np.hanning(FRAME)
    fw = f[active] * W
    F = np.abs(np.fft.rfft(fw, axis=1)) + 1e-12
    geo = np.exp(np.log(F).mean(axis=1))
    ari = F.mean(axis=1)
    flat = float((geo / ari).mean())
    zc = ((np.diff(np.sign(f[active]), axis=1) != 0).sum(axis=1)) / 2.0
    zcr = float((zc / (FRAME / 2)).mean())

    # syllabic modulation of the amplitude envelope
    env = fr[a:b + 1]
    fs_env = sr / float(HOP)
    tt = np.arange(len(env), dtype=float)
    env = env - np.polyval(np.polyfit(tt, env, 1), tt)
    nper = min(len(env), int(5 * fs_env))
    if nper >= 32:
        freqs, psd = welch(env, fs=fs_env, nperseg=nper, noverlap=nper // 2)
    else:
        freqs = np.fft.rfftfreq(max(len(env), 2), 1.0 / fs_env)
        psd = np.abs(np.fft.rfft(env)) ** 2
    band = (freqs >= 2.0) & (freqs <= 8.0)
    hi = (freqs >= 10.0) & (freqs <= 14.0)
    fmin = float(np.median(psd[hi])) if hi.any() else 1e-12
    bmax = float(psd[band].max()) if band.any() else 0.0
    mod_prom = bmax / (fmin + 1e-12)
    mod_f = float(freqs[band][np.argmax(psd[band])]) if band.any() else 0.0

    # ---- speech-specific: periodicity / HNR / overlap / steady-background -----
    fa = f[active]
    f0, sal = _pitch_track(fa, sr)
    voiced = sal > 0.35
    voiced_frac = float(voiced.mean())
    pitch_sal = float(sal.mean())
    f0_med = 0.0
    if voiced.sum() >= 6:
        mu = float(np.median(f0[voiced]))
        f0_med = mu
        pitch_cv = float(np.std(f0[voiced]) / (mu + 1e-9))
        # HNR in the conventional 80-4500 Hz band: energy in a harmonic comb
        # (+-12% around f0 and its 2 first harmonics) vs the rest of the same band
        frq = np.fft.rfftfreq(FRAME, 1.0 / sr)
        band_mask = (frq >= 80.0) & (frq <= 4500.0)
        comb = np.zeros(len(frq), dtype=bool)
        for k in (1, 2, 3):
            comb |= np.abs(frq - k * mu) <= 0.12 * k * mu
        comb &= band_mask
        noise = band_mask & ~comb
        P = (F ** 2)
        h = P[:, comb].sum(axis=1) / (P[:, comb].sum(axis=1) + P[:, noise].sum(axis=1) + 1e-12)
        h = float(np.clip(np.median(h), 1e-6, 1 - 1e-6))
        hnr_db = 10.0 * np.log10(h / (1 - h))
    else:
        pitch_cv = 1.0
        hnr_db = -99.0
    # frame-to-frame spectral stability in 100-4000 Hz (steady music bed -> ~1.0)
    M = F[:, (np.fft.rfftfreq(FRAME, 1.0 / sr) >= 100) & (np.fft.rfftfreq(FRAME, 1.0 / sr) <= 4000)]
    Mn = M / (np.linalg.norm(M, axis=1, keepdims=True) + 1e-12)
    step = max(1, len(Mn) // 200)
    A, B = Mn[:-step], Mn[step:]
    spec_stab = float((A * B).sum(axis=1).mean()) if len(A) else 1.0
    # ... but restricted to the quietest quarter of the active frames: those are the
    # intra-utterance gaps where a background bed is all that is left, so a steady bed
    # drives this towards 1.0 while a clean recording stays decorrelated.
    e_act = np.asarray(fr[active], dtype=float)
    order = np.argsort(e_act)
    sel = np.sort(order[:max(8, int(0.25 * len(order)))])
    Q = Mn[sel]
    qstep = max(1, len(Q) // 40)
    if len(Q) > 2 * qstep:
        quiet_corr = float((Q[:-qstep] * Q[qstep:]).sum(axis=1).mean())
    else:
        quiet_corr = 0.0

    return dict(dur=dur, peak=peak, rms=rms, sil_ratio=sil_ratio, flat=flat, zcr=zcr,
                mod_prom=mod_prom, mod_f=mod_f, n_act=int(active.sum()),
                hnr_db=float(hnr_db), pitch_sal=pitch_sal, pitch_cv=pitch_cv,
                voiced_frac=voiced_frac, spec_stab=spec_stab, quiet_corr=quiet_corr,
                f0_med=round(f0_med, 1))


def evaluate(x, sr, th=THRESH):
    ft = features(x, sr)
    reasons = []
    if not (th["dur_min"] <= ft["dur"] <= th["dur_max"]):
        reasons.append("duration_out_of_band(3.0-10.0)")
    if ft["peak"] < th["peak_min"]:
        reasons.append("peak_too_low")
    if ft["peak"] > th["peak_max"]:
        reasons.append("peak_too_high")
    if ft["sil_ratio"] > th["silence_ratio_max"]:
        reasons.append("too_much_silence")
    if ft["flat"] >= th["flat_max"]:
        reasons.append("spectral_flatness_high")
    if not (th["zcr_min"] <= ft["zcr"] <= th["zcr_max"]):
        reasons.append("zcr_out_of_band")
    if ft["mod_prom"] < th["mod_prom_min"]:
        reasons.append("no_syllabic_modulation")
    if not (th["mod_fpeak_min"] <= ft["mod_f"] <= th["mod_fpeak_max"]):
        reasons.append("mod_peak_freq_out_of_band")
    if ft["hnr_db"] < th["hnr_db_min"]:
        reasons.append("low_harmonicity(no_voice)")
    if ft["pitch_sal"] < th["pitch_sal_min"]:
        reasons.append("weak_pitch_saliency")
    if ft["pitch_cv"] > th["pitch_cv_max"]:
        reasons.append("pitch_dispersion(overlap_suspect)")
    if ft["voiced_frac"] < th["voiced_frac_min"]:
        reasons.append("too_little_voiced_speech")
    if ft["spec_stab"] > th["spec_stab_max"]:
        reasons.append("steady_background_bed(music_suspect)")
    if ft["quiet_corr"] > th["quiet_corr_max"]:
        reasons.append("steady_bed_in_gaps(music_suspect)")
    return ft, (len(reasons) == 0), reasons


def evaluate_file(path):
    x, sr = sf.read(path, dtype="float32")
    if x.ndim > 1:
        x = x.mean(axis=1)
    return evaluate(np.asarray(x, dtype=np.float64), sr)
