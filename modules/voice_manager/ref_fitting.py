"""参考音频的"适配（fit）"与"诊断（checks）"—— 引擎分档时长门 + 能量窗裁剪

对应规格 `UPGRADE-SPEC.md` 泳道 C 的 **US-C3**，机制依据：
  * `ANALYSIS-GPT-SOVITS.md` M4：GSV 的参考音频是**硬门 3~10s**
    （`GPT_SoVITS/TTS.py:826-829` 越界直接抛错，不是"效果差一点"），
    `api_v2.py:444-445` 同样按秒数拒绝；
  * `ANALYSIS-OPENVOICE.md` OV-P1-3：官方 QA 把克隆失败归成四类
    —— 有底噪 / 太短 / 多人混录 / 长静音 —— 但**只诊断不改写**。

本模块把"判 + 治"合在一处，并且严格区分两者：

诊断（`diagnose()`，只读，不改文件）
    四条 checks：noise_floor / duration / single_speaker / long_silence，
    结果连同度量一起进 `voice_profiles.meta["ref_checks"]`。

治疗（`fit_reference()`，只写**新文件**，绝不改原件）
    * 过长 → 取"30ms 帧 RMS 能量最高的连续 6~8s 窗"另存
      `ref_{uuid}_fit.wav`，并记 `meta["ref_source_path"]` = 原件路径；
      原件的 sha256 幂等护栏（F-R4-4）因此**完全不受影响**；
    * 过短 → `needs_longer_sample`：**不做时间拉伸**（倍速/变调把"这个人的音色"
      改成"另一个人的听感"，克隆参考宁可退回让用户重录）；
    * 合格 → 直接用原件，不产生 fit 副本。

清洁室声明：只取上述文档里记录的**阈值与判据结论**（数值），代码为本仓库独立实现；
未复制 GPT-SoVITS / OpenVoice / ChatTTS（AGPL）的任何源码文本或注释。
"""

from __future__ import annotations

import logging
import math
from pathlib import Path
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

# ---------- 引擎分档：参考音频可用时长 (min_s, max_s) ----------
# min 以下 = 采不到稳定音色特征；max 以上 = 对多数引擎**没有收益**（GSV 直接拒绝），
# 且编码/推理成本随时长上升。`sample` 例外：它回放 master 本身，不参与克隆。
REF_WINDOW_SECONDS: dict = {
    "gptsovits": (3.0, 10.0),    # 硬门：越界即 GSV 侧抛错（M4）
    "openvoice": (1.0, 10.0),    # 说话人嵌入片段口径（OV se_extractor）
    "voxcpm": (3.0, 15.0),       # 上游建议 5~30s；>15s 信息增益趋零、VAE 编码成本线性涨
    "indextts": (3.0, 12.0),
    "mosstts": (3.0, 12.0),
    "kokoro": (3.0, 10.0),
    "edgetts": (3.0, 10.0),
    "sample": (0.3, 60.0),       # 回放档：不裁剪
}
DEFAULT_REF_WINDOW: tuple = (3.0, 10.0)

# ---------- 裁剪目标：能量最高的连续 FIT_TARGET_S 秒 ----------
FIT_TARGET_S: float = 7.0            # 目标窗长（6~8s 区间的中值）
FIT_MIN_S: float = 6.0
FIT_MAX_S: float = 8.0
FRAME_MS: float = 30.0               # 帧长：30ms（能量统计的常规语音帧）

# ---------- 诊断阈值（可断言、可复核）----------
SILENCE_FLOOR_DBFS: float = -45.0    # 帧低于此电平视为"静音帧"
NOISE_FLOOR_DBFS: float = -38.0      # 最静 10% 帧的中值高于此 ⇒ 判定有可闻底噪
LONG_SILENCE_S: float = 1.0          # 连续静音超过 1s ⇒ 参考里夹了停顿/前后空录
SPEAKER_DRIFT_SEMITONES: float = 4.0 # 前半/后半 F0 中位差 > 4 半音 ⇒ 换人风险（启发式）

# ---------- F0（换人启发式）的取窗与 pyin 网格 ----------
# 这三个常量**必须**存在：`_f0_halves()` 在 librosa 的 try 之前就用它们算窗长，
# 缺一个 diagnose() 就会对任何可解码音频抛 NameError（US-C3 实测踩过，见报告）。
F0_ANALYZE_S: float = 1.5            # 前半/后半各取 1.5s 测 F0（换人判据不需要整段）
F0_FRAME_SAMPLES: int = 2048         # pyin 帧长（128ms @16k，低音高需要长窗）
F0_HOP_SAMPLES: int = 512            # 抬高 4 倍帧移降帧率：pyin 是纯感知算法，整段跑要 7~17s

__all__ = [
    "REF_WINDOW_SECONDS", "DEFAULT_REF_WINDOW", "FIT_TARGET_S", "FRAME_MS",
    "ref_window_for", "load_mono", "frame_dbfs", "best_energy_window",
    "diagnose", "fit_reference", "prepare_reference",
]


# ============================== 读盘与帧统计 ==============================

def load_mono(path: str) -> tuple:
    """解成 (float32 单声道样本, 采样率)。解码失败上抛（调用方决定判不判）。"""
    import soundfile as sf
    data, sr = sf.read(str(path), always_2d=False, dtype="float32")
    arr = np.asarray(data, dtype=np.float32)
    if arr.ndim > 1:
        arr = arr.mean(axis=1).astype(np.float32)
    return arr.reshape(-1), int(sr)


def frame_dbfs(x: np.ndarray, sr: int, frame_ms: float = FRAME_MS) -> tuple:
    """分帧 RMS 电平（dBFS，满量程=1.0）。返回 (每帧 dBFS, 帧样本数, 帧移样本数)。"""
    frame = max(1, int(round(sr * frame_ms / 1000.0)))
    hop = frame                                   # 不重叠：能量窗扫描要的是"位移不重不漏"
    n_frames = max(0, len(x) // hop)
    if n_frames == 0:
        return np.zeros(0, dtype=np.float32), frame, hop
    trimmed = x[: n_frames * hop].reshape(n_frames, hop)
    rms = np.sqrt(np.mean(trimmed.astype(np.float64) ** 2, axis=1))
    db = 20.0 * np.log10(np.maximum(rms, 1e-10))
    return db.astype(np.float32), frame, hop


def ref_window_for(engine: str = None) -> tuple:
    """取该引擎的参考音频时长档 (min_s, max_s)；未知引擎走默认档。"""
    return REF_WINDOW_SECONDS.get((engine or "").strip().lower(), DEFAULT_REF_WINDOW)


# ============================== 诊断 checks[] ==============================

def _max_run(db: np.ndarray, hop: int, sr: int, threshold: float) -> float:
    """连续"低于 threshold 的帧"最长有多长（秒）。"""
    if db.size == 0:
        return 0.0
    silent = db < threshold
    best = run = 0
    for flag in silent:
        run = run + 1 if flag else 0
        best = max(best, run)
    return best * hop / float(sr)


def _leading_trailing_silence(db: np.ndarray, hop: int, sr: int,
                              threshold: float) -> tuple:
    if db.size == 0:
        return 0.0, 0.0
    lead = 0
    while lead < db.size and db[lead] < threshold:
        lead += 1
    trail = 0
    while trail < db.size - lead and db[db.size - 1 - trail] < threshold:
        trail += 1
    return lead * hop / float(sr), trail * hop / float(sr)


def _f0_halves(x: np.ndarray, sr: int) -> Optional[tuple]:
    """前半/后半的 F0 中位（Hz）；测不出/库缺失返回 None（不编结论）。

    成本约束：`librosa.pyin` 是纯感知算法，整段跑一次要 7~17s（首调还含 numba 编译），
    而 clone/ingest 是同步请求 —— 所以只在**两半各取 `F0_ANALYZE_S` 秒**的窗上测，
    并把 `hop_length` 抬到 `F0_HOP_SAMPLES`（默认 8 倍帧移）降帧率。
    换人判据要的是"前半和后半是不是同一个人"，取有代表性的 1.5s 足够。
    """
    win = max(1, int(round(F0_ANALYZE_S * sr)))
    quarters = []
    for base in (0, max(0, len(x) // 2)):
        seg = x[base:base + win]
        if len(seg) < int(0.3 * sr):
            return None
        quarters.append(seg)

    try:
        import librosa
        out = []
        for seg in quarters:
            f0, _, _ = librosa.pyin(seg.astype(np.float64), fmin=50.0, fmax=400.0,
                                    sr=sr, frame_length=F0_FRAME_SAMPLES,
                                    hop_length=F0_HOP_SAMPLES)
            voiced = np.asarray([v for v in (f0 if f0 is not None else [])
                                 if v and np.isfinite(v)])
            if voiced.size < 8:
                return None
            out.append(float(np.median(voiced)))
        return out[0], out[1]
    except Exception as e:                        # pragma: no cover - 依赖/环境
        logger.debug(f"[ref_fitting] F0 不可测: {type(e).__name__}: {e}")
        return None


def diagnose(path: str, engine: str = None, x: np.ndarray = None,
             sr: int = None) -> dict:
    """跑四条 checks（**只读，不改写文件**），返回可入库的 dict。

    返回:
        {"checks": [{"name","ok","severity","detail"}...],
         "metrics": {...}, "verdict": "ok"|"needs_longer_sample"|"needs_fit"|"unusable",
         "duration": float, "window": [min, max]}
    `severity`：`"block"`（该拒）/`"warn"`（能用但有风险，只提示）/`"info"`。
    """
    min_s, max_s = ref_window_for(engine)
    try:
        if x is None or sr is None:
            x, sr = load_mono(path)
    except Exception as e:
        return {"checks": [{"name": "decodable", "ok": False, "severity": "block",
                            "detail": f"{type(e).__name__}: {e}"}],
                "metrics": {}, "verdict": "unusable", "duration": None,
                "window": [min_s, max_s]}

    duration = len(x) / float(sr) if sr else 0.0
    db, frame, hop = frame_dbfs(x, sr)
    loud = float(db.max()) if db.size else float("-inf")
    quiet = float(np.percentile(db, 10)) if db.size >= 10 else float("-inf")
    long_silence = _max_run(db, hop, sr, SILENCE_FLOOR_DBFS)
    lead, trail = _leading_trailing_silence(db, hop, sr, SILENCE_FLOOR_DBFS)
    snr = loud - quiet if math.isfinite(loud) and math.isfinite(quiet) else None
    f0_halves = _f0_halves(x, sr)
    drift = None
    if f0_halves:
        lo, hi = f0_halves
        if lo > 0 and hi > 0:
            drift = abs(1200.0 * math.log2(hi / lo))   # 半音差（cent 口径 /100）
            drift = drift / 100.0

    checks = []
    # 1) 底噪
    checks.append({
        "name": "noise_floor",
        "ok": bool(quiet < NOISE_FLOOR_DBFS),
        "severity": "warn",
        "detail": (f"最静10%帧中值 {quiet:.1f} dBFS（门限 {NOISE_FLOOR_DBFS}），"
                   f"最响帧 {loud:.1f} dBFS，动/噪比 {snr if snr is None else round(snr, 1)} dB"),
    })
    # 2) 时长（引擎分档；这条同时决定 verdict）
    if duration < min_s:
        dur_ok, dur_detail = False, (f"{duration:.2f}s 短于引擎 '{engine or 'default'}' "
                                     f"下限 {min_s}s ⇒ needs_longer_sample（不做时间拉伸）")
    elif duration > max_s:
        dur_ok, dur_detail = False, (f"{duration:.2f}s 长于上限 {max_s}s ⇒ 需裁剪能量最高窗")
    else:
        dur_ok, dur_detail = True, f"{duration:.2f}s 在 {min_s}~{max_s}s 档内"
    checks.append({"name": "duration", "ok": dur_ok,
                   "severity": "block", "detail": dur_detail})
    # 3) 单人（启发式：F0 前半/后半漂移；测不出时 ok=True 但标 info）
    if drift is None:
        checks.append({"name": "single_speaker", "ok": True, "severity": "info",
                       "detail": "F0 不可测（无声调段/缺 librosa），不下换人结论"})
    else:
        checks.append({"name": "single_speaker", "ok": drift <= SPEAKER_DRIFT_SEMITONES,
                       "severity": "warn",
                       "detail": (f"F0 前半/后半中位差 {drift:.2f} 半音"
                                  f"（阈值 {SPEAKER_DRIFT_SEMITONES}）"
                                  f"{'，疑似换人/多人混录' if drift > SPEAKER_DRIFT_SEMITONES else ''}")})
    # 4) 长静音
    checks.append({
        "name": "long_silence",
        "ok": long_silence <= LONG_SILENCE_S,
        "severity": "warn",
        "detail": (f"最长连续静音 {long_silence:.2f}s（门限 {LONG_SILENCE_S}s），"
                   f"首尾静音 {lead:.2f}s/{trail:.2f}s"),
    })

    if duration < min_s:
        verdict = "needs_longer_sample"
    elif duration > max_s:
        verdict = "needs_fit"
    else:
        verdict = "ok"
    return {
        "checks": checks,
        "metrics": {
            "duration_s": round(duration, 3), "sample_rate": int(sr),
            "peak_dbfs": round(loud, 2), "noise_floor_dbfs": round(quiet, 2),
            "dynamic_range_db": round(snr, 2) if snr is not None else None,
            "longest_silence_s": round(long_silence, 3),
            "leading_silence_s": round(lead, 3), "trailing_silence_s": round(trail, 3),
            "f0_half_drift_semitones": round(drift, 3) if drift is not None else None,
            "frame_ms": FRAME_MS,
        },
        "verdict": verdict,
        "duration": duration,
        "window": [min_s, max_s],
    }


# ============================== 治疗：能量窗裁剪 ==============================

def best_energy_window(x: np.ndarray, sr: int, target_s: float = FIT_TARGET_S,
                       min_s: float = FIT_MIN_S, max_s: float = FIT_MAX_S,
                       frame_ms: float = FRAME_MS) -> Optional[dict]:
    """取"连续 target_s 秒、帧 RMS 总能量最高"的那一窗。

    判据用 **RMS 之和**（等价于该窗能量最大），实现上对 dBFS 域求和会低估响段，
    所以这里回到线性功率上比。返回 {start_s, end_s, seconds, score}；
    音频比目标窗还短时返回 None（调用方按 needs_longer_sample 处理）。
    """
    target = int(round(target_s * sr))
    lower = int(round(min_s * sr))
    upper = int(round(max_s * sr))
    if len(x) < lower:
        return None
    target = max(lower, min(target, max(upper, lower)))   # 夹进 [min,max]
    frame = max(1, int(round(sr * frame_ms / 1000.0)))
    n_frames = max(0, len(x) // frame)
    if n_frames == 0:
        return None
    power = np.mean(x[: n_frames * frame].reshape(n_frames, frame).astype(np.float64) ** 2,
                    axis=1)
    win_frames = max(1, target // frame)
    if win_frames > n_frames:
        win_frames = n_frames
    csum = np.concatenate(([0.0], np.cumsum(power)))
    best_idx = int(np.argmax(csum[win_frames:] - csum[:-win_frames]))
    start = best_idx * frame
    end = min(start + target, len(x))
    return {"start_s": round(start / float(sr), 3),
            "end_s": round(end / float(sr), 3),
            "seconds": round((end - start) / float(sr), 3),
            "score": float(csum[best_idx + win_frames] - csum[best_idx])}


def fit_reference(src_path: str, dst_path: str, target_s: float = FIT_TARGET_S,
                  x: np.ndarray = None, sr: int = None) -> Optional[dict]:
    """把过长的参考音频裁剪成能量最高的连续窗，**另存** dst_path（原件不动）。

    返回 {"path","start_s","end_s","seconds","source_path"}；
    没的可裁（比目标窗还短/解不开码）返回 None —— 调用方据此回退用原件。
    """
    try:
        if x is None or sr is None:
            x, sr = load_mono(src_path)
    except Exception as e:
        logger.warning(f"[ref_fitting] 解码失败，不裁剪 {src_path}: {type(e).__name__}: {e}")
        return None
    win = best_energy_window(x, sr, target_s=target_s)
    if not win:
        return None
    lo = int(round(win["start_s"] * sr))
    hi = int(round(win["end_s"] * sr))
    seg = x[lo:hi]
    # 窗两端各留 5ms 淡入淡出，避免硬切在帧边界留下.click（不改内容，只改边界包络）
    edge = max(1, int(0.005 * sr))
    if len(seg) > 2 * edge:
        ramp_in = np.linspace(0.0, 1.0, edge, dtype=np.float32)
        ramp_out = np.linspace(1.0, 0.0, edge, dtype=np.float32)
        seg = seg.copy()
        seg[:edge] *= ramp_in
        seg[-edge:] *= ramp_out
    import soundfile as sf
    Path(dst_path).parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(dst_path), seg, sr, subtype="PCM_16")
    out = dict(win)
    out.update({"path": str(dst_path), "source_path": str(src_path)})
    logger.info(f"[ref_fitting] 已裁剪参考音频 {win['start_s']}~{win['end_s']}s "
                f"({win['seconds']}s) -> {Path(dst_path).name}")
    return out


def prepare_reference(audio_path: str, engine: str = None,
                      dst_dir: str = None, stem: str = None) -> dict:
    """**入库/克隆的统一入口**：先诊断，再决定"直接用 / 裁一刀 / 退回重录"。

    Args:
        audio_path: 待入库的参考音频（原件，永不被改）。
        engine: 引擎名（决定时长档）。
        dst_dir/stem: 给定时过长副本写成 `dst_dir/ref_{stem}_fit.wav`；
            不给则只诊断不落盘（审计模式）。
    Returns:
        {"verdict","use_path","checks","metrics","duration","window",
         "fit": {...}|None, "fit_path": str|None}
        `verdict`：`ok` / `needs_fit`（已裁好，用 fit 副本）/
        `needs_longer_sample`（**必须退回让用户重录**，不做时间拉伸）。
    """
    min_s, max_s = ref_window_for(engine)
    try:
        x, sr = load_mono(audio_path)
    except Exception as e:
        logger.warning(f"[ref_fitting] 无法解码 {audio_path}: {e}")
        return {"verdict": "unusable", "use_path": str(audio_path),
                "checks": [{"name": "decodable", "ok": False, "severity": "block",
                            "detail": f"{type(e).__name__}: {e}"}],
                "metrics": {}, "duration": None, "window": [min_s, max_s],
                "fit": None, "fit_path": None}

    report = diagnose(None, engine, x=x, sr=sr)
    duration = report["duration"]
    out = {"verdict": report["verdict"], "use_path": str(audio_path),
           "checks": report["checks"], "metrics": report["metrics"],
           "duration": duration, "window": report["window"],
           "fit": None, "fit_path": None}

    if duration > max_s and dst_dir and stem:
        fit_path = str(Path(dst_dir) / f"ref_{stem}_fit.wav")
        fit = fit_reference(audio_path, fit_path, x=x, sr=sr)
        if fit:
            out["fit"] = fit
            out["fit_path"] = fit["path"]
            out["use_path"] = fit["path"]
            out["verdict"] = "fitted"
    return out
