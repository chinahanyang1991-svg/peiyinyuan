"""Audio decoding, speech slicing and measurement for the resource library.

Every clip that reaches data/resources/ has been through this module:
  decode (wav/flac/mp3/ogg) -> mono float32 -> energy based speech window
  -> resample -> 16 bit PCM WAV at 48 kHz and 22.05 kHz -> measured metrics

Attribute vocabulary is kept aligned with backend/models/voice.py:
  gender    male | female | neutral | unknown
  age_band  child | young | adult | senior | unknown     (VoiceProfile.age_group)
  language  zh | en | ja | ko | fr | de | es | ru | other
  style     平静 | 开心 | 悲伤 | 激昂 | 严肃 | 温柔 | 叙述 | unknown
"""

import io
import math
import os

import numpy as np
import soundfile as sf

MASTER_SR = 48000
PREVIEW_SR = 22050
MIN_DUR = 5.0
MAX_DUR = 20.0
RMS_FLOOR = 0.0045      # ~ -47 dBFS: below this a file counts as silence
PEAK_MIN = 0.02         # audible level check

GENDER_VOCAB = ("male", "female", "neutral", "unknown")
AGE_VOCAB = ("child", "young", "adult", "senior", "unknown")
STYLE_VOCAB = ("平静", "开心", "悲伤", "激昂", "严肃", "温柔", "叙述", "unknown")

_AGE_MAP = {
    "child": "child", "kid": "child", "children": "child", "teens": "child",
    "teen": "child", "under_nineteen": "child", "nineteen-or-less": "child",
    "young": "young", "twenty": "young", "20s": "young", "teens (13-19)": "young",
    "twenties": "young", "young adult": "young",
    "adult": "adult", "thirty": "adult", "30s": "adult", "thirties": "adult",
    "fourties": "adult", "forties": "adult", "40s": "adult", "fifties": "adult",
    "50s": "adult",
    "senior": "senior", "sixties": "senior", "60s": "senior", "seventies": "senior",
    "eighties": "senior", "over_sixty": "senior", "elderly": "senior", "sen": "senior",
    "not_said": "unknown", "other": "unknown", "unknown": "unknown", "na": "unknown",
    "prefer-not-to-say": "unknown",
}
_GENDER_MAP = {
    "male": "male", "m": "male", "man": "male", "boy": "male", "1": "male",
    "female": "female", "f": "female", "woman": "female", "girl": "female", "0": "female",
    "trans": "unknown", "transgender": "unknown", "non-binary": "unknown",
    "neutral": "neutral", "unknown": "unknown", "other": "unknown",
    "not_said": "unknown", "na": "unknown", "": "unknown",
}
_LANG_MAP = {
    "zh": "zh", "cmn": "zh", "zho": "zh", "chinese": "zh", "mandarin": "zh",
    "zh-cn": "zh", "zh-hans": "zh", "cmn_hans_cn": "zh", "yue": "zh", "zh-hk": "zh",
    "en": "en", "eng": "en", "english": "en", "en_us": "en", "en_gb": "en",
    "ja": "ja", "jpn": "ja", "ko": "ko", "kor": "ko", "fr": "fr", "de": "de",
    "es": "es", "ru": "ru", "it": "it", "pt": "pt", "ar": "ar", "th": "th", "vi": "vi",
}
# upstream emotion / style label -> our style vocabulary
_STYLE_MAP = {
    "neutral": "平静", "calm": "平静", "平和": "平静", "平静": "平静",
    "happy": "开心", "joy": "开心", "cheerful": "开心", "开心": "开心",
    "sad": "悲伤", "sadness": "悲伤", "depressed": "悲伤", "悲伤": "悲伤",
    "angry": "激昂", "anger": "激昂", "angry_emphatic": "激昂", "angry-emphatic": "激昂",
    "激昂": "激昂", "excited": "激昂", "hot": "激昂",
    "fear": "严肃", "fearful": "严肃", "surprise": "激昂", "surprised": "激昂",
    "serious": "严肃", "solemn": "严肃", "严肃": "严肃",
    "disgust": "悲伤", "disgustted": "悲伤",
    "gentle": "温柔", "soft": "温柔", "warm": "温柔", "温柔": "温柔",
    "narration": "叙述", "narrative": "叙述", "reading": "叙述", "read": "叙述",
    "unknown": "unknown",
}


def norm_gender(v):
    if v is None:
        return "unknown"
    key = str(v).strip().lower()
    return _GENDER_MAP.get(key, "unknown" if key not in GENDER_VOCAB else key)


def norm_age(v):
    if v is None:
        return "unknown"
    key = str(v).strip().lower().replace(" ", "")
    out = _AGE_MAP.get(key) or _AGE_MAP.get(str(v).strip().lower())
    if out:
        return out
    if key in AGE_VOCAB:
        return key
    # numeric ages ("32")
    try:
        n = int(float("".join(c for c in key if c.isdigit() or c == ".") or 0))
    except ValueError:
        return "unknown"
    if n <= 0:
        return "unknown"
    if n < 14:
        return "child"
    if n < 27:
        return "young"
    if n < 55:
        return "adult"
    return "senior"


def norm_language(v):
    if v is None:
        return "other"
    key = str(v).strip().lower()
    if key in _LANG_MAP:
        return _LANG_MAP[key]
    base = key.split("-")[0].split("_")[0]
    return _LANG_MAP.get(base, "other" if base not in ("zh", "en", "ja", "ko", "fr",
                                                       "de", "es", "ru", "it", "pt")
                         else base)


def norm_style(v):
    if v is None:
        return "unknown"
    key = str(v).strip().lower()
    out = _STYLE_MAP.get(key) or _STYLE_MAP.get(str(v).strip())
    if out:
        return out
    return v if v in STYLE_VOCAB else "unknown"


# ------------------------------------------------------------------ decode/audio
def decode_bytes(data, hint=""):
    """Decode in-memory audio. Returns (float32 mono samples, sr, format)."""
    with sf.SoundFile(io.BytesIO(data)) as fh:
        x = fh.read(dtype="float32", always_2d=True)
        sr = fh.samplerate
        fmt = fh.format or ""
    if x.shape[1] > 1:                      # downmix stereo -> mono
        x = x.mean(axis=1)
    else:
        x = x[:, 0]
    return np.ascontiguousarray(x, dtype=np.float32), int(sr), str(fmt), os.path.splitext(hint)[1].lstrip(".").lower()


def decode_file(path):
    with sf.SoundFile(path) as fh:
        x = fh.read(dtype="float32", always_2d=True)
        sr, fmt = fh.samplerate, fh.format
    x = x.mean(axis=1) if x.shape[1] > 1 else x[:, 0]
    return np.ascontiguousarray(x, dtype=np.float32), int(sr), str(fmt)


def resample(x, sr, target):
    if int(sr) == int(target) or x.size == 0:
        return x.astype(np.float32, copy=False), int(sr)
    try:
        import soxr
        return soxr.resample(x, sr, target).astype(np.float32, copy=False), int(target)
    except Exception:  # noqa: BLE001
        pass
    try:
        import librosa
        return librosa.resample(x, orig_sr=sr, target_sr=target).astype(np.float32, copy=False), int(target)
    except Exception:  # noqa: BLE001
        pass
    n = int(round(x.shape[0] * float(target) / float(sr)))
    idx = np.linspace(0, x.shape[0] - 1, n)
    return np.interp(idx, np.arange(x.shape[0]), x).astype(np.float32), int(target)


def write_wav(path, x, sr):
    """16 bit PCM mono WAV."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    peak = float(np.max(np.abs(x))) if x.size else 0.0
    y = x
    if peak > 0.99:                          # keep headroom, never clip on write
        y = x * (0.99 / peak)
    tmp = path + ".tmp.wav"
    sf.write(tmp, y, sr, subtype="PCM_16", format="WAV")
    if os.path.exists(path):
        os.remove(path)
    os.replace(tmp, path)
    return path


# ------------------------------------------------------------------- measurement
def frames(x, sr, win=0.025, hop=0.010):
    n = max(1, int(win * sr))
    s = max(1, int(hop * sr))
    if x.shape[0] < n:
        return np.array([float(np.sqrt(np.mean(x ** 2)) + 1e-12)])
    idx = 1 + (x.shape[0] - n) // s
    strided = np.lib.stride_tricks.as_strided(
        x, shape=(idx, n), strides=(x.strides[0] * s, x.strides[0]))
    return np.sqrt(np.mean(strided ** 2, axis=1) + 1e-12)


def noise_floor_of(e):
    if e.size == 0:
        return 0.0
    return float(np.percentile(e, 10))


def speech_thresh(e):
    """Frame-energy threshold separating speech from pauses/hiss.

    Anchored on the loud frame level: a clip with no true silence must still be
    detected as speech, so the noise floor may only raise the cut-off up to
    45% of the 90th percentile level.
    """
    if e.size == 0:
        return 0.006
    hi = float(np.percentile(e, 90))
    nf = float(np.percentile(e, 10))
    return max(min(nf * 3.0, hi * 0.45), hi * 0.15, 0.006)


def _voiced_mask(x, sr):
    e = frames(x, sr)
    return e, speech_thresh(e), noise_floor_of(e)


def metrics(x, sr):
    """Objective measurements used for verification and inferred attributes."""
    m = {"duration_s": round(float(x.shape[0]) / float(sr), 3) if sr else 0.0,
         "rms": 0.0, "peak": 0.0, "clipping": 0, "noise_floor": 0.0,
         "voiced_ratio": 0.0, "f0_median": 0.0, "f0_iqr": 0.0}
    if x.size == 0 or not sr:
        return m
    m["rms"] = round(float(np.sqrt(np.mean(x.astype(np.float64) ** 2))), 6)
    m["peak"] = round(float(np.max(np.abs(x))), 6)
    m["clipping"] = int(np.count_nonzero(np.abs(x) >= 0.999))
    e, thresh, nf = _voiced_mask(x, sr)
    m["noise_floor"] = round(nf, 6)
    m["voiced_ratio"] = round(float((e > thresh).mean()), 4)
    f0 = _f0_track(x, sr)
    if f0.size:
        m["f0_median"] = round(float(np.median(f0)), 1)
        m["f0_iqr"] = round(float(np.percentile(f0, 75) - np.percentile(f0, 25)), 1)
    return m


def _f0_track(x, sr, frame=0.05):
    """Median fundamental frequency via autocorrelation on voiced frames."""
    try:
        n = int(frame * sr)
        if x.shape[0] < n * 2:
            return np.array([])
        lo, hi = max(2, int(sr / 400.0)), min(n - 1, int(sr / 70.0))
        if hi <= lo:
            return np.array([])
        step = int(0.02 * sr)
        out = []
        for start in range(0, x.shape[0] - n, step):
            seg = x[start:start + n]
            seg = seg - float(np.mean(seg))
            e = float(np.dot(seg, seg))
            if e < 1e-6:
                continue
            ac = np.correlate(seg, seg, mode="full")[n - 1:]
            if ac[0] <= 0:
                continue
            norm = ac[lo:hi] / ac[0]
            k = int(np.argmax(norm)) + lo
            if norm[k - lo] > 0.30:
                out.append(sr / float(k))
        return np.array(out, dtype=np.float64)
    except Exception:  # noqa: BLE001
        return np.array([])


def estimate_gender_from_f0(f0):
    """Acoustic fallback when the corpus carries no gender column."""
    if not f0 or f0 <= 0:
        return "unknown", 0.0
    if f0 < 148.0:
        return "male", min(1.0, (148.0 - f0) / 45.0 + 0.5)
    if f0 > 172.0:
        return "female", min(1.0, (f0 - 172.0) / 60.0 + 0.5)
    return "unknown", 0.0


def estimate_style(m, lang=""):
    """Prosody proxy for speaking style when the source has no emotion label.

    Loudness + pitch dynamics: flat and quiet reads as 平静, wide and loud as
    激昂, low energy with falling pitch as 悲伤, bright and fast as 开心.
    Always tagged through attr_source so it is never mistaken for a label.
    """
    rms = m.get("rms") or 0.0
    dy = m.get("f0_iqr") or 0.0
    f0 = m.get("f0_median") or 0.0
    vr = m.get("voiced_ratio") or 0.0
    if rms <= 0 or vr <= 0:
        return "unknown"
    loud = rms > 0.09
    if dy >= 42.0 and (loud or vr > 0.72):
        return "激昂"
    if loud and dy >= 26.0:
        return "开心"
    if dy < 10.0 and rms < 0.045:
        return "悲伤"
    if dy < 16.0:
        return "平静"
    return "叙述"


def speech_window(x, sr, min_s=MIN_DUR, max_s=MAX_DUR, pad=0.06):
    """Pick the longest contiguous speech run between min_s and max_s.

    Returns (start_sample, end_sample, info) or (0, 0, info) when nothing
    speech-like is present (silence, pure noise, too short).
    """
    dur = x.shape[0] / float(sr) if sr else 0.0
    info = {"dur": round(dur, 3), "reason": ""}
    if sr and dur < min_s:
        info["reason"] = "shorter than %.1fs" % min_s
        return 0, 0, info
    e = frames(x, sr)
    nf = noise_floor_of(e)
    thresh = speech_thresh(e)
    voiced = e > thresh
    ratio = float(voiced.mean()) if voiced.size else 0.0
    info.update(noise_floor=round(nf, 6),
                rms_peak=round(float(np.percentile(e, 90)) if e.size else 0.0, 6),
                voiced_ratio=round(ratio, 4))
    if voiced.size == 0 or ratio < 0.12:
        info["reason"] = "no speech energy (voiced_ratio=%.3f)" % ratio
        return 0, 0, info
    hop = int(0.010 * sr)
    padf = max(1, int(pad / 0.010))
    minf = max(1, int(min_s / 0.010))
    maxf = max(minf, int(max_s / 0.010))
    best_len, best_j = 0, -1
    j = 0
    nfr = voiced.shape[0]
    while j < nfr:
        if not voiced[j]:
            j += 1
            continue
        k = j
        gap = 0
        run_start = j
        while k < nfr:
            if voiced[k]:
                gap = 0
                k += 1
            else:
                gap += 1
                if gap > int(0.35 / 0.010):
                    break
                k += 1
        length = min(k, run_start + maxf) - run_start
        if length > best_len:
            best_len, best_j = length, run_start
        j = k + 1 if k > j else j + 1
    if best_j < 0 or best_len < minf:
        info["reason"] = "no speech run >= %.1fs" % min_s
        return 0, 0, info
    a = max(0, (best_j - padf) * hop)
    b = min(x.shape[0], (best_j + best_len + padf) * hop)
    if (b - a) / float(sr) < min_s:
        b = min(x.shape[0], a + int(min_s * sr))
    if (b - a) / float(sr) > max_s:
        b = a + int(max_s * sr)
    info["reason"] = "ok"
    info["window_s"] = round((b - a) / float(sr), 3)
    return int(a), int(b), info


def make_master(x, sr, out_dir, stem, target_rates=(MASTER_SR, PREVIEW_SR)):
    """Slice-free helper: resample + write both WAVs. Returns dict of paths."""
    os.makedirs(out_dir, exist_ok=True)
    paths = {}
    for i, rate in enumerate(target_rates):
        y, got = resample(x, sr, rate)
        suffix = "" if i == 0 else "_22k"
        p = os.path.join(out_dir, "%s%s.wav" % (stem, suffix))
        write_wav(p, y, got)
        paths["master" if i == 0 else "preview"] = p
        paths["sr_%s" % ("master" if i == 0 else "preview")] = got
    return paths


def safe_slug(text, maxlen=64):
    s = "".join(ch if (ch.isalnum() or ch in "-_.") else "_" for ch in str(text))
    s = s.strip("_.") or "x"
    return s[:maxlen]


def dbfs(rms):
    return -120.0 if not rms else round(20.0 * math.log10(max(rms, 1e-12)), 2)
