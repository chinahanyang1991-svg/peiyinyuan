# -*- coding: utf-8 -*-
"""Objective speech gate for refill candidates.

All numbers are measured per candidate with soundfile/numpy/scipy; nothing is
hand-edited. Thresholds THRESH are calibrated by calib.py against:
  positives: edge-tts raw + every DSP family output (post-process),
  negatives: existing raw-SFX files in data/voices/library/{animal,electronic,
             mechanical} (the very files the audit lane rejects).
"""
import numpy as np
import soundfile as sf
from scipy.signal import welch

SR_FRAME = 48000
FRAME, HOP = 2048, 512

# calibrated thresholds (see calib_report.json produced by calib.py)
THRESH = {
    "dur_min": 1.2, "dur_max": 10.0,
    "peak_min": 0.02, "peak_max": 0.99,
    "silence_ratio_max": 0.50,
    "flat_max": 0.18,          # speech observed max 0.084 (calib 2026-09-23)
    "zcr_min": 0.015, "zcr_max": 0.280,   # speech observed [0.026,0.160]
    "mod_prom_min": 8.0,       # speech observed min 16.7 (calib 2026-09-23)
    "mod_fpeak_min": 1.9, "mod_fpeak_max": 8.4,
}

def _frames(x, frame=FRAME, hop=HOP):
    n = 1 + max(0, (len(x) - frame)) // hop
    idx = np.arange(frame)[None, :] + hop * np.arange(n)[:, None]
    return x[idx.astype(np.intp)]

def features(x, sr):
    """Return dict of measured gate features for mono float signal x."""
    x = np.asarray(x, dtype=np.float64).ravel()
    dur = len(x) / sr
    peak = float(np.abs(x).max()) if len(x) else 0.0
    rms = float(np.sqrt(np.mean(x ** 2))) if len(x) else 0.0
    f = _frames(x)
    fr = np.sqrt((f ** 2).mean(axis=1)) + 1e-12
    fr_max = fr.max() if len(fr) else 0.0
    active = fr > 0.06 * fr_max
    if active.sum() < 8:  # almost nothing active
        return dict(dur=dur, peak=peak, rms=rms, sil_ratio=1.0, flat=1.0,
                    zcr=1.0, mod_prom=0.0, mod_f=0.0, n_act=int(active.sum()))
    # silence ratio on trimmed span (ignore file-edge padding)
    idx = np.where(active)[0]; a, b = idx[0], idx[-1]
    sub = active[a:b + 1]
    sil_ratio = float(1.0 - sub.mean())
    # spectral flatness + ZCR over active frames
    W = np.hanning(FRAME)
    F = np.abs(np.fft.rfft(f[active] * W, axis=1)) + 1e-12
    geo = np.exp(np.log(F).mean(axis=1)); ari = F.mean(axis=1)
    flat = float((geo / ari).mean())
    zc = ((np.diff(np.sign(f[active]), axis=1) != 0).sum(axis=1)) / 2.0
    zcr = float((zc / (FRAME / 2)).mean())
    # syllabic modulation spectrum of amplitude envelope
    env = fr[a:b + 1]
    fs_env = sr / HOP
    tt = np.arange(len(env), dtype=float)
    env = env - np.polyval(np.polyfit(tt, env, 1), tt)   # remove slow drift
    nper = min(len(env), int(5 * fs_env))
    if nper >= 32:
        freqs, psd = welch(env, fs=fs_env, nperseg=nper, noverlap=nper // 2)
    else:
        freqs = np.fft.rfftfreq(max(len(env), 2), 1.0 / fs_env)
        psd = np.abs(np.fft.rfft(env)) ** 2
    band = (freqs >= 2.0) & (freqs <= 8.0)
    hi = (freqs >= 10.0) & (freqs <= 14.0)
    floors = [float(np.median(psd[m])) for m in (hi,) if m.any()]
    fmin = max(floors) if floors else 1e-12
    bmax = float(psd[band].max()) if band.any() else 0.0
    mod_prom = bmax / (fmin + 1e-12)
    mod_f = float(freqs[band][np.argmax(psd[band])]) if band.any() else 0.0
    return dict(dur=dur, peak=peak, rms=rms, sil_ratio=sil_ratio, flat=flat,
                zcr=zcr, mod_prom=mod_prom, mod_f=mod_f, n_act=int(active.sum()))

def evaluate(x, sr, th=THRESH):
    ft = features(x, sr)
    reasons = []
    if not (th["dur_min"] <= ft["dur"] <= th["dur_max"]): reasons.append("duration_out_of_band")
    if ft["peak"] < th["peak_min"]: reasons.append("peak_too_low")
    if ft["peak"] > th["peak_max"]: reasons.append("peak_too_high")
    if ft["sil_ratio"] > th["silence_ratio_max"]: reasons.append("too_much_silence")
    if ft["flat"] >= th["flat_max"]: reasons.append("spectral_flatness_high")
    if not (th["zcr_min"] <= ft["zcr"] <= th["zcr_max"]): reasons.append("zcr_out_of_band")
    if ft["mod_prom"] < th["mod_prom_min"]: reasons.append("no_syllabic_modulation")
    if not (th["mod_fpeak_min"] <= ft["mod_f"] <= th["mod_fpeak_max"]):
        reasons.append("mod_peak_freq_out_of_band")
    return ft, (len(reasons) == 0), reasons

def evaluate_file(path):
    x, sr = sf.read(path, dtype="float32")
    if x.ndim > 1: x = x.mean(axis=1)
    return evaluate(np.asarray(x, dtype=np.float64), sr)
