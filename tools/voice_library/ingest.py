"""通用音色入库器 (VA-3) —— 质检 + 归一 + VoiceProfile 落库。

输入: manifest.json, 条目形如
    {category, name, source_url, page_url, license, attribution,
     local_file, duration_s, ...}

流程 (任何一步失败 -> 记 rejected 原因, 绝不入库坏样本):
    1. QC 门  : 可解码 / 0.3s <= 时长 <= 30s / peak >= 0.02 (非静音) / 采样率记录
    2. 归一    : 48 kHz 单声道 PCM16 主样本 -> data/voices/library/<category>/<uuid>.wav
                <=5s 试听副本          -> data/voices/library/previews/<uuid>.wav
    3. 入库    : VoiceProfile(source="sample", engine="sample", category=...)
    4. 幂等    : 按 meta.page_url (缺失时 source_url) 去重, 重复条目 skip

CLI:
    python tools/voice_library/ingest.py --manifest m.json            # dry-run, 只打印
    python tools/voice_library/ingest.py --manifest m.json --apply    # 真实写库
    python tools/voice_library/ingest.py --manifest m.json --apply --limit 5

红线: 只写 data/voices/library/** 与 voice_profiles 表; 不碰 backend/ modules/ 代码。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid as uuidlib
from datetime import datetime
from pathlib import Path

import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_LIBRARY = ROOT / "data" / "voices" / "library"
DEFAULT_DB = ROOT / "data" / "peiyinyuan.db"

TARGET_SR = 48000            # 主样本统一采样率
MIN_DURATION_S = 0.3         # QC: 短于此判为碎片/点击噪声
MAX_DURATION_S = 30.0        # QC: 长于此判为整曲/环境长录音, 不是"音色"
MIN_PEAK = 0.02              # QC: 低于此判为静音
# ---- WF-19: 削顶(爆音)验收门 —— 超阈素材不得标 verified ----
MAX_CLIP_RATIO = 0.001       # 贴边采样点占比上限 (0.1%); 3e31b26f 实测 0.0169
MAX_PEAK_ACCEPT = 0.999      # 峰值上界; 7c04a138 实测 1.0449(整数字域溢出)
PREVIEW_MAX_S = 5.0          # 试听副本长度上限
MAX_DECODE_BYTES = 80 * 1024 * 1024   # 防御性上限: 80MB 以上直接拒收(避免拖死内存)

# ---- #76 S3: 下载后校验挂 ref_fitting.diagnose()（US-C3 的四条 checks） ----
# 默认 "sample" 档 = 0.3~60s，比本脚本 QC 的 0.3~30s 更宽 ⇒ 既有批次重跑**不新增
# 一条拒收**；要把这批素材当**克隆参考**收，就显式传 --ref-engine openvoice（1~10s）
# / gptsovits（3~10s 硬门），此时超窗即阻断级 ⇒ 不入库。
REF_ENGINE_DEFAULT = "sample"


def ref_diagnose(path: str, engine: str = REF_ENGINE_DEFAULT) -> dict:
    """对**下载下来的原件**跑 ref_fitting.diagnose()（只读，不改写文件）。

    永不抛异常：ref_fitting 自己解不开码时会返回 `decodable` 阻断项，
    这里再兜一层，保证任何意外都退化成"记一条 block 判据"而不是让整批崩掉。
    """
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    try:
        from modules.voice_manager.ref_fitting import diagnose
        rep = diagnose(str(path), engine)
    except Exception as e:
        rep = {"checks": [{"name": "decodable", "ok": False, "severity": "block",
                           "detail": "ref_fitting_error:%s:%s" % (type(e).__name__,
                                                                  str(e)[:120])}],
               "metrics": {}, "verdict": "unusable", "duration": None,
               "window": [None, None]}
    rep.setdefault("checks", [])
    rep.setdefault("verdict", "unusable")
    return rep


def ref_block_reasons(report: dict) -> list:
    """只挑**阻断级**未过项 (severity=="block" 且 ok==False)。

    warn/info 一律不拦 —— 底噪略高、F0 漂移这类是"能用但有风险"，
    拒收等于静默丢数据（与 WF-19 的 verified 标记同一套取径）。
    """
    out = []
    for c in (report or {}).get("checks") or []:
        if not c.get("ok") and c.get("severity") == "block":
            out.append("%s:%s" % (c.get("name", "?"), str(c.get("detail", ""))[:120]))
    return out


def clip_gate_reason(metrics: dict) -> str:
    """削顶判定单点函数 (WF-19)。返回空串=通过, 非空=未通过原因(含指标名)。

    背景: `build_profile` 过去把 meta["verified"] 恒置 True, 于是
    qc_file 已经量出来的 clip_ratio/peak 超标（例: 3e31b26f clip_ratio=0.0169、
    7c04a138 peak=1.0449）在库里仍是"已验收"，前端据此直接可选。
    本函数只改**验收标记**，不参与收拒（拒收会静默丢数据）——
    超标素材继续入库，但 verified=False + verify_note 说明原因。
    """
    if not isinstance(metrics, dict):
        return "no_metrics"
    clip = metrics.get("clip_ratio")
    peak = metrics.get("peak")
    try:
        clip_f = None if clip is None else float(clip)
    except (TypeError, ValueError):
        clip_f = None
    try:
        peak_f = None if peak is None else float(peak)
    except (TypeError, ValueError):
        peak_f = None
    if clip_f is not None and clip_f > MAX_CLIP_RATIO:
        return "clip_ratio=%.6f>%.6f" % (clip_f, MAX_CLIP_RATIO)
    if peak_f is not None and peak_f > MAX_PEAK_ACCEPT:
        return "peak=%.6f>%.6f" % (peak_f, MAX_PEAK_ACCEPT)
    return ""


CATEGORIES = {
    "human": "人声",
    "animal": "动物",
    "electronic": "电子",
    "anime": "动漫",
    "mechanical": "机械数码",
}
# 许可 -> 中文标签 (进 style_tags)
LICENSE_TAG = {
    "CC0": "公有领域",
    "Public domain": "公有领域",
    "PD": "公有领域",
    "CC BY 4.0": "CCBY音效",
    "CC BY 3.0": "CCBY音效",
    "CC BY 2.5": "CCBY音效",
    "CC BY 2.0": "CCBY音效",
    "CC BY": "CCBY音效",
}
INGESTED_BY = "voice_library_v1"
_FFMPEG_CACHE = {}


# --------------------------------------------------------------------------- #
# 解码
# --------------------------------------------------------------------------- #
def resolve_ffmpeg() -> str | None:
    """PATH 里的 ffmpeg -> imageio_ffmpeg 自带静态二进制 (只探测一次并缓存)。"""
    if "exe" in _FFMPEG_CACHE:
        return _FFMPEG_CACHE["exe"]
    exe = shutil.which("ffmpeg")
    if not exe:
        try:
            import imageio_ffmpeg
            exe = imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            exe = None
    _FFMPEG_CACHE["exe"] = exe
    return exe


def _decode_with_ffmpeg(path: str):
    """soundfile 读不了时的兜底: ffmpeg 解成临时 wav 再读。"""
    exe = resolve_ffmpeg()
    if not exe:
        raise RuntimeError("soundfile 无法解码且环境内没有可用 ffmpeg")
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as fh:
        tmp = fh.name
    try:
        res = subprocess.run(
            [exe, "-hide_banner", "-loglevel", "error", "-y", "-i", path,
             "-ac", "1", "-f", "wav", tmp],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=180)
        if res.returncode != 0 or not os.path.exists(tmp) or os.path.getsize(tmp) < 44:
            raise RuntimeError("ffmpeg 解码失败: %s" % (res.stderr or "")[:180])
        return _read_wav(tmp)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _read_wav(path: str):
    data, sr = sf.read(path, dtype="float32", always_2d=True)
    return data, sr


def decode_audio(path: str):
    """-> (float32 mono ndarray, sample_rate). 解不了抛异常。"""
    try:
        data, sr = _read_wav(path)
    except Exception:
        data, sr = _decode_with_ffmpeg(path)
    if data.ndim > 1 and data.shape[1] > 1:
        data = data.mean(axis=1)
    else:
        data = data[:, 0]
    return np.ascontiguousarray(data, dtype=np.float32), int(sr)


# --------------------------------------------------------------------------- #
# 质检
# --------------------------------------------------------------------------- #
def qc_file(path: str) -> dict:
    """跑完所有质检门, 永不抛异常。

    返回 {ok, reason, metrics:{duration_s, peak, rms, sample_rate, channels_src,
                               clip_ratio, bytes}}
    reason 为空串表示通过。
    """
    out = {"ok": False, "reason": "", "metrics": {}}
    p = Path(path)
    if not p.exists() or not p.is_file():
        out["reason"] = "missing_file"
        return out
    size = p.stat().st_size
    if size < 44:
        out["reason"] = "too_small_bytes(%d)" % size
        return out
    if size > MAX_DECODE_BYTES:
        out["reason"] = "too_large_bytes(%d)" % size
        return out
    try:
        x, sr = decode_audio(str(p))
    except Exception as e:
        out["reason"] = "undecodable:%s:%s" % (type(e).__name__, str(e)[:120])
        return out
    if x.size == 0 or sr <= 0:
        out["reason"] = "empty_stream"
        return out
    dur = x.size / float(sr)
    peak = float(np.max(np.abs(x))) if x.size else 0.0
    rms = float(np.sqrt(np.mean(np.square(x, dtype=np.float64))))
    clipped = float(np.mean(np.abs(x) >= 0.999))
    try:
        info = sf.info(str(p))
        channels = int(getattr(info, "channels", 0) or 0) or None
    except Exception:
        channels = None
    m = {"duration_s": round(dur, 3), "peak": round(peak, 4), "rms": round(rms, 5),
         "sample_rate": sr, "channels_src": channels,
         "clip_ratio": round(clipped, 4), "bytes": int(size)}
    out["metrics"] = m
    if dur < MIN_DURATION_S:
        out["reason"] = "too_short:%.3fs<%.1fs" % (dur, MIN_DURATION_S)
        return out
    if dur > MAX_DURATION_S:
        out["reason"] = "too_long:%.2fs>%.1fs" % (dur, MAX_DURATION_S)
        return out
    if not np.isfinite(peak) or peak < MIN_PEAK:
        out["reason"] = "silent:peak=%.4f<%.2f" % (peak, MIN_PEAK)
        return out
    out["ok"] = True
    return out


# --------------------------------------------------------------------------- #
# 归一 / 落盘
# --------------------------------------------------------------------------- #
def resample_mono(x: np.ndarray, sr_in: int, sr_out: int = TARGET_SR) -> np.ndarray:
    """-> float32 单声道, 限幅到 [-1, 1]。"""
    x = np.asarray(x, dtype=np.float32)
    if sr_in != sr_out:
        try:
            import soxr
            x = soxr.resample(x, sr_in, sr_out)
        except Exception:
            # 没有 soxr 时退回 numpy 线性插值 (仅兜底, 正常路径不会走到)
            n = int(round(x.size * sr_out / float(sr_in)))
            xi = np.linspace(0.0, x.size - 1, num=max(n, 1))
            x = np.interp(xi, np.arange(x.size, dtype=np.float64),
                          x.astype(np.float64)).astype(np.float32)
    return np.clip(x, -1.0, 1.0).astype(np.float32)


def best_preview_window(x: np.ndarray, sr: int, max_s: float = PREVIEW_MAX_S):
    """取 RMS 最大的 max_s 秒窗口作试听, 保证试听段有声且代表整条样本。"""
    win = max(int(round(sr * max_s)), 1)
    if x.size <= win:
        return x
    # 非重叠分窗求 RMS, 取最大者 (重叠步长=窗口一半, 代价很小)
    step = max(win // 2, 1)
    starts = range(0, x.size - win + 1, step)
    best_s, best_v = 0, -1.0
    for s in starts:
        seg = x[s:s + win].astype(np.float64)
        v = float(np.dot(seg, seg))
        if v > best_v:
            best_v, best_s = v, s
    return x[best_s:best_s + win]


def store_sample(x: np.ndarray, sr_in: int, category: str, key: str,
                 library_root: Path, apply: bool):
    """写主样本 + 试听副本, 返回 (master, preview, sample_rate_out)。

    dry-run(apply=False) 时只算路径不写盘。
    """
    uid = str(uuidlib.uuid5(uuidlib.NAMESPACE_URL, key))
    cat_dir = library_root / category
    prev_dir = library_root / "previews"
    master = cat_dir / ("%s.wav" % uid)
    preview = prev_dir / ("%s.wav" % uid)
    if not apply:
        return str(master), str(preview), TARGET_SR, uid
    cat_dir.mkdir(parents=True, exist_ok=True)
    prev_dir.mkdir(parents=True, exist_ok=True)
    y = resample_mono(x, sr_in, TARGET_SR)
    sf.write(str(master), y, TARGET_SR, subtype="PCM_16", format="WAV")
    sf.write(str(preview), best_preview_window(y, TARGET_SR), TARGET_SR,
             subtype="PCM_16", format="WAV")
    return str(master), str(preview), TARGET_SR, uid


# --------------------------------------------------------------------------- #
# 入库
# --------------------------------------------------------------------------- #
def norm_url(item: dict) -> str:
    """去重键: 优先 page_url, 退化 source_url。"""
    return (item.get("page_url") or item.get("source_url") or "").strip()


def license_tag(license_: str) -> str:
    lic = (license_ or "").strip()
    if lic in LICENSE_TAG:
        return LICENSE_TAG[lic]
    up = lic.upper()
    if up.startswith("CC0") or "PUBLIC DOMAIN" in up or up in ("PD", "PDM"):
        return "公有领域"
    if up.startswith("CC BY") and "SA" not in up and "NC" not in up:
        return "CCBY音效"
    return "开放授权"


def build_profile(item: dict, master: str, preview: str, sr_out: int,
                  metrics: dict, uid: str,
                  ref_report: dict | None = None,
                  ref_engine: str = REF_ENGINE_DEFAULT) -> dict:
    category = item["category"]
    name = (item.get("name") or Path(master).stem)[:70]
    lic = (item.get("license") or "unknown").strip()
    tags = [CATEGORIES.get(category, category), license_tag(lic), "音效库"]
    extra = (item.get("tags") or [])[:3]
    for t in extra:
        if t and t not in tags:
            tags.append(str(t)[:12])
    desc = ("%s｜类别:%s｜出处:%s｜许可:%s｜署名:%s｜QC: 时长 %.2fs, 峰值 %.3f, RMS %.4f, "
            "原采样率 %dHz -> 主样本 %dHz 单声道 PCM16" % (
                name, CATEGORIES.get(category, category),
                norm_url(item) or item.get("source_site", "n/a"), lic,
                (item.get("attribution") or "n/a")[:80],
                metrics["duration_s"], metrics["peak"], metrics["rms"],
                metrics["sample_rate"], sr_out))
    meta = {
        "license": lic,
        "attribution": item.get("attribution") or "",
        "page_url": item.get("page_url") or "",
        "source_url": item.get("source_url") or "",
        "duration_s": metrics["duration_s"],
        "peak": metrics["peak"],
        "rms": metrics["rms"],
        "sample_rate": sr_out,
        "source_sample_rate": metrics["sample_rate"],
        "clip_ratio": metrics.get("clip_ratio"),
        "source_bytes": metrics.get("bytes"),
        "category": category,
        "source_site": item.get("source_site") or "",
        "upstream_title": item.get("upstream_title") or "",
        "declared_duration_s": item.get("duration_s"),
        "ingested_by": INGESTED_BY,
        # WF-19: 旧实现恒 True —— 已量到削顶也报"已验收"。现由同一套阈值判定，
        # 超标素材照常入库（不静默丢数据）但 verified=False + verify_note 留因。
        "verified": clip_gate_reason(metrics) == "",
        "verify_note": clip_gate_reason(metrics) or "clip_gate_passed",
        "clip_gate": {"max_clip_ratio": MAX_CLIP_RATIO,
                      "max_peak_accept": MAX_PEAK_ACCEPT},
        "ingested_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    # ---- #76 S3: ref_fitting (US-C3) 的四条 checks 进 meta，与 clone 同一套口径 ----
    if ref_report is not None:
        meta["ref_engine"] = ref_engine
        meta["ref_checks"] = ref_report.get("checks") or []
        meta["ref_verdict"] = ref_report.get("verdict")
        meta["ref_window"] = ref_report.get("window")
        meta["ref_metrics"] = ref_report.get("metrics") or {}
    if not meta["verified"]:
        desc += "｜QC: 未验收(爆音 %s)" % meta["verify_note"]
    return {
        "id": uid,
        "name": name,
        "source": "sample",
        "category": category,
        "engine": "sample",
        "language": None,
        "gender": None,
        "age_group": item.get("age_group"),
        "style_tags": tags,
        "model_path": master,
        "reference_audio_path": master,      # 与 model_path 同值
        "sample_audio_path": preview,
        "description": desc,
        "is_downloaded": 1,
        "download_url": item.get("source_url") or item.get("page_url") or "",
        "meta": meta,
    }


def open_session(db_path: str):
    """把 ORM 绑到 db_path 并给一个 session (不修改 backend/, 只在进程内改指向)。"""
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    import backend.models.base as base
    want = os.path.realpath(db_path)
    if os.path.realpath(base.get_db_path()) != want:
        base.get_db_path = lambda: str(db_path)
        base._engine = None
        base._SessionLocal = None
    return base.get_session()


def existing_keys(session, VoiceProfile) -> set:
    rows = session.query(VoiceProfile).filter_by(source="sample").all()
    keys = set()
    for r in rows:
        meta = r.meta or {}
        for k in (meta.get("page_url"), meta.get("source_url"), r.download_url):
            if k:
                keys.add(str(k).split("?utm_")[0].strip())
    return keys


def load_manifest(path: str) -> list:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(raw, dict):
        raw = raw.get("items") or raw.get("entries") or []
    return list(raw)


def ingest_manifest(items: list, apply: bool, library_root: Path, db_path: str,
                    limit: int = 0, verbose: bool = True,
                    refresh_existing: bool = False,
                    ref_engine: str = REF_ENGINE_DEFAULT) -> dict:
    """QC -> **ref_fitting 下载后校验** -> 归一 -> 入库。返回统计 dict (selftest 也用它)。

    `ref_engine` 决定 ref_fitting 的时长档（默认 "sample"，即不因这条门新增拒收；
    收"克隆参考"时传 "openvoice"/"gptsovits" 才吃到引擎硬门）。
    refresh_existing=True 时, 已入库条目不再 skip, 而是按同一 uuid5 主键原地刷新
    (用于命名/描述口径升级后的一致性回写; 文件路径同值, 不产生新文件)。
    """
    stats = {"accepted": 0, "rejected": 0, "skipped": 0, "updated": 0,
             "dry_run": not apply, "refresh_existing": bool(refresh_existing),
             "ref_engine": ref_engine,
             "by_category": {}, "reject_reasons": {}, "accepted_items": [],
             "rejected_items": [], "skipped_items": []}
    session = None
    db_keys = set()
    seen = set()
    name_counts = {}
    try:
        if apply:
            session = open_session(db_path)
            db_keys = existing_keys(session, _VoiceProfile())
            seen = set(db_keys)
        for item in items:
            if limit and (stats["accepted"] + stats["rejected"]) >= limit:
                break
            cat = (item.get("category") or "").strip()
            key = norm_url(item)
            if cat not in CATEGORIES:
                stats["rejected"] += 1
                code = "bad_category"
                stats["reject_reasons"][code] = stats["reject_reasons"].get(code, 0) + 1
                stats["rejected_items"].append({"name": item.get("name"),
                                                "category": cat,
                                                "reason": "bad_category:%s" % (cat or "none")})
                if verbose:
                    print("[reject] %-34s %-11s bad_category:%s" %
                          (str(item.get("name"))[:34], cat or "none", cat or "none"))
                continue
            is_refresh = False
            if key and key in seen:
                if not (refresh_existing and key in db_keys):
                    stats["skipped"] += 1
                    stats["skipped_items"].append({"name": item.get("name"), "key": key,
                                                   "why": "duplicate_in_db_or_batch"})
                    continue
                stats["updated"] += 1
                is_refresh = True
            if key:
                seen.add(key)
            local = item.get("local_file") or ""
            if local and not os.path.isabs(local):
                local = str(ROOT / local)
            qc = qc_file(local) if local else {"ok": False, "reason": "no_local_file",
                                               "metrics": {}}
            if not qc["ok"]:
                code = qc["reason"].split(":")[0]
                stats["rejected"] += 1
                stats["reject_reasons"][code] = stats["reject_reasons"].get(code, 0) + 1
                stats["rejected_items"].append({"name": item.get("name"),
                                                "category": cat, "reason": qc["reason"]})
                if verbose:
                    print("[reject] %-34s %-11s %s" %
                          (str(item.get("name"))[:34], cat, qc["reason"][:90]))
                continue
            # ---- 下载后校验（US-C3 四 checks 挂在 QC 之后、落盘入库之前）----
            # 顺序要紧：判不过就在**写盘之前**退出，被拒条目不留 master/preview 孤儿文件。
            ref_report = ref_diagnose(local, ref_engine)
            blocked = ref_block_reasons(ref_report)
            if blocked:
                code = "ref_check_block"
                stats["rejected"] += 1
                stats["reject_reasons"][code] = stats["reject_reasons"].get(code, 0) + 1
                reason = "ref_check_block:" + " | ".join(blocked)
                stats["rejected_items"].append({"name": item.get("name"),
                                                "category": cat, "reason": reason})
                if verbose:
                    print("[reject] %-34s %-11s %s" %
                          (str(item.get("name"))[:34], cat, reason[:90]))
                continue
            master, preview, sr_out, uid = store_sample(
                *_decode_for_store(local), cat, key or local, library_root, apply)
            prof = build_profile(item, master, preview, sr_out, qc["metrics"], uid,
                                 ref_report=ref_report, ref_engine=ref_engine)
            base_name = prof["name"]
            n = name_counts.get(base_name, 0) + 1
            name_counts[base_name] = n
            if n > 1:
                prof["name"] = ("%s (%d)" % (base_name, n))[:70]
            if not is_refresh:
                stats["accepted"] += 1
                stats["by_category"][cat] = stats["by_category"].get(cat, 0) + 1
                stats["accepted_items"].append({
                    "name": prof["name"], "category": cat, "id": uid,
                    "license": prof["meta"]["license"], "master": master,
                    "preview": preview, "duration_s": qc["metrics"]["duration_s"],
                    "peak": qc["metrics"]["peak"], "src_sr": qc["metrics"]["sample_rate"],
                    "page_url": prof["meta"]["page_url"] or prof["meta"]["source_url"]})
            if verbose:
                tag = "UPD" if is_refresh else ("DRY" if not apply else "SAVE")
                print("[%-5s] %-34s %-11s %.2fs peak=%.3f %dHz->%dHz lic=%s" % (
                    tag, prof["name"][:34], cat,
                    qc["metrics"]["duration_s"], qc["metrics"]["peak"],
                    qc["metrics"]["sample_rate"], sr_out, prof["meta"]["license"]))
            if apply:
                _write_profile(session, _VoiceProfile(), prof)
        if apply and session is not None:
            session.commit()
    except Exception:
        if session is not None:
            session.rollback()
        raise
    finally:
        if session is not None:
            session.close()
    return stats


def _VoiceProfile():
    from backend.models.voice import VoiceProfile
    return VoiceProfile


def _write_profile(session, VoiceProfile, prof: dict):
    cols = set(VoiceProfile.__table__.columns.keys())
    payload = {k: v for k, v in prof.items() if k in cols}
    existing = session.query(VoiceProfile).filter_by(id=prof["id"]).first()
    if existing:
        for k, v in payload.items():
            if k != "id":
                setattr(existing, k, v)
    else:
        session.add(VoiceProfile(**payload))
        session.flush()
        # voice.py 给 language 列带了 Python 端默认值 "zh"，SQLAlchemy 在 INSERT 时
        # 会把显式传入的 None 也回填成 "zh"。样本类音色 language 必须是 NULL
        # (非语音)，这里 flush 后显式改回 None，走 UPDATE 落 NULL。
        obj = session.query(VoiceProfile).filter_by(id=prof["id"]).first()
        if obj is not None and obj.language is not None:
            obj.language = None
            session.flush()


def _decode_for_store(path: str):
    x, sr = decode_audio(path)
    return x, sr


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--manifest", required=True, help="条目清单 json 路径")
    ap.add_argument("--apply", action="store_true", help="真正写盘写库; 不给则只 dry-run")
    ap.add_argument("--limit", type=int, default=0, help="最多处理前 N 条 (0=不限)")
    ap.add_argument("--library-root", default=str(DEFAULT_LIBRARY))
    ap.add_argument("--db", default=str(DEFAULT_DB), help="目标 voice_profiles 库")
    ap.add_argument("--refresh-existing", action="store_true",
                    help="已入库条目按同一主键原地刷新名称/描述(默认 skip)")
    ap.add_argument("--ref-engine", default=REF_ENGINE_DEFAULT,
                    help="ref_fitting 下载后校验的时长档: sample(默认, 0.3~60s, 不新增拒收) "
                         "/ openvoice(1~10s) / gptsovits(3~10s 硬门) / voxcpm(3~15s); "
                         "阻断级 checks 未过 => 不入库")
    ap.add_argument("--report", default="", help="把本次统计写成 json")
    args = ap.parse_args(argv)

    items = load_manifest(args.manifest)
    print("[ingest] manifest=%s 条目=%d apply=%s library=%s db=%s ref_engine=%s" % (
        args.manifest, len(items), args.apply, args.library_root, args.db,
        args.ref_engine))
    print("-" * 96)
    stats = ingest_manifest(items, args.apply, Path(args.library_root), args.db,
                            args.limit, refresh_existing=args.refresh_existing,
                            ref_engine=args.ref_engine)
    print("-" * 96)
    print("[ingest] 新入库 %d / 原地刷新 %d / 拒收 %d / 跳过(幂等) %d" % (
        stats["accepted"], stats["updated"], stats["rejected"], stats["skipped"]))
    print("[ingest] 按类: %s" % json.dumps(stats["by_category"], ensure_ascii=False))
    print("[ingest] 拒因: %s" % json.dumps(stats["reject_reasons"], ensure_ascii=False))
    if not args.apply:
        print("[ingest] dry-run: 未写任何文件、未写任何数据库。加 --apply 才会落库")
    if args.report:
        Path(args.report).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report).write_text(json.dumps(stats, ensure_ascii=False, indent=1),
                                     encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
