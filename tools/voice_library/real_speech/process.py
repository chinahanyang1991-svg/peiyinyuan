# -*- coding: utf-8 -*-
"""raw candidates -> 48k/mono/PCM_16 library samples + per-source manifest shard.

Pipeline per candidate (every number written to the manifest is measured here):
  decode -> record source peak/duration/sample rate -> edge-trim (no content cutting)
  -> hard reject if trimmed length is outside 3.0..10.0 s (the GPT-SoVITS clone band;
     too long is rejected, never truncated) -> resample to 48 kHz mono
  -> refill-compatible peak policy (<0.5 -> 0.70 ; >0.99 -> 0.90) -> full gate
  -> save PCM_16 WAV -> re-decode the saved file and re-run the gate (self check).
Speaker cap: at most MAX_PER_SPEAKER accepted clips per speaker id.
"""
import os, sys, json, time, collections
import numpy as np
import soundfile as sf

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C
import gate as G

SITE = {"aishell3": "openslr_slr93_aishell3", "vctk": "edinburgh_datashare_vctk",
        "commons": "wikimedia_commons"}
CAT_LABEL = "真人声"
MAX_PER_SPEAKER = 5
LIMIT = int(os.environ.get("RS_LIMIT", "0"))


def gender_est(f0, meta=None):
    if meta in ("male", "female"):
        return meta, "语料元数据"
    if f0 <= 0:
        return None, "无法估计"
    return ("male" if f0 < 165.0 else "female"), "F0估计(%.0fHz)" % f0


def process(src):
    idx_path = os.path.join(C.RAW_DIR, src, "index.json")
    idx = C.read_json(idx_path)
    if not idx:
        print("no index for", src)
        return
    ev_src = C.read_json(os.path.join(C.SELF_DIR, "license_evidence_%s.json" % src), [])
    entries, rejected = [], []
    per_spk = collections.Counter()
    reasons = collections.Counter()
    t0 = time.time()
    for c in idx:
        if LIMIT and len(entries) >= LIMIT:
            break
        slug = c["slug"]
        spk = c.get("speaker") or "unknown"
        if per_spk[spk] >= MAX_PER_SPEAKER:
            reasons["speaker_cap_5"] += 1
            rejected.append({"slug": slug, "reasons": ["speaker_cap_5"]})
            continue
        raw = c["raw"]
        if not os.path.exists(raw):
            reasons["raw_missing"] += 1
            rejected.append({"slug": slug, "reasons": ["raw_missing"]})
            continue
        try:
            x, sr = C.load_any(raw)
        except Exception as e:
            reasons["decode_fail"] += 1
            rejected.append({"slug": slug, "reasons": ["decode_fail:%s" % str(e)[:80]]})
            continue
        src_dur = len(x) / float(sr)
        src_peak = float(np.abs(x).max()) if len(x) else 0.0
        x = C.trim_edges(x, sr)
        dur_after_trim = len(x) / float(sr)
        if dur_after_trim > G.THRESH["dur_max"] + 1e-9:
            reasons["over_10s_no_cutting"] += 1
            rejected.append({"slug": slug, "reasons": ["over_10s_no_cutting"],
                             "dur": round(dur_after_trim, 3)})
            continue
        if dur_after_trim < G.THRESH["dur_min"] - 1e-9:
            reasons["under_3s"] += 1
            rejected.append({"slug": slug, "reasons": ["under_3s"], "dur": round(dur_after_trim, 3)})
            continue
        xr, sr2 = C.to_target_sr(x, sr)
        g = C.gain_policy(float(np.abs(xr).max()))
        if g is None:
            reasons["silent"] += 1
            rejected.append({"slug": slug, "reasons": ["silent"]})
            continue
        if g != 1.0:
            xr = (xr * g).astype(np.float32)
        ft, ok, why = G.evaluate(np.asarray(xr, dtype=np.float64), sr2)
        if not ok:
            for r in why:
                reasons[r] += 1
            rejected.append({"slug": slug, "reasons": why, "gate": {k: round(v, 4) for k, v in ft.items()}})
            continue
        out = os.path.join(C.OUT_DIR, slug + ".wav")
        C.save_pcm16(xr, sr2, out)
        # self check from disk
        info = sf.info(out)
        xd, sd = C.load_any(out)
        ft2, ok2, why2 = G.evaluate(np.asarray(xd, dtype=np.float64), sd)
        fmt_ok = (info.format == "WAV" and info.subtype == "PCM_16" and info.channels == 1 and sd == 48000)
        if not (ok2 and fmt_ok):
            reasons["disk_recheck_fail"] += 1
            os.remove(out)
            rejected.append({"slug": slug, "reasons": ["disk_recheck_fail"] + why2})
            continue
        gender, gsrc = gender_est(ft2.get("f0_med", 0.0), c.get("meta_gender"))
        ev = c.get("license_evidence") or ev_src
        ok_lic, lic_reason = C.license_verdict(c.get("license_name_raw") or c.get("license"),
                                               (ev[0].get("license_url") if ev else None),
                                               json.dumps(ev, ensure_ascii=False)[:2000] if ev else "")
        if not ok_lic:
            reasons["license_gate:%s" % lic_reason] += 1
            rejected.append({"slug": slug, "reasons": ["license_gate:%s" % lic_reason]})
            continue
        tags = ["真人声", CAT_LABEL, "speech-gate-pass", "lang:%s" % c.get("lang", "?"),
                "speaker:%s" % spk, "源:%s" % SITE[src],
                "性别:%s(%s)" % (gender or "未知", gsrc),
                "年龄带:%s" % (c.get("meta_age") or "未知(源未提供)"),
                "无ASR判定局限(单人/无BGM按能量-谐波统计)"]
        entries.append({
            "category": "human",
            "name": "%s-%s %s #%s" % (CAT_LABEL, SITE[src].split('_')[-1].upper(), spk,
                                      slug.rsplit("-", 1)[-1]),
            "source_url": c["source_url"],
            "page_url": c["page_url"],
            "license": c["license"],
            "attribution": c["attribution"],
            "local_file": out,
            "duration_s": round(ft2["dur"], 3),
            "peak": round(ft2["peak"], 5),
            "rms": round(ft2["rms"], 5),
            "source_sample_rate": sr,
            "source_bytes": c.get("bytes") or os.path.getsize(raw),
            "source_site": SITE[src],
            "upstream_title": c.get("upstream_title") or os.path.basename(raw),
            "tags": tags,
            "age_group": c.get("meta_age"),
            "speaker_id": spk,
            "language": c.get("lang"),
            "gender": gender,
            "processing": {"decoder": "soundfile/libsndfile", "source_duration_s": round(src_dur, 3),
                            "source_peak": round(src_peak, 5), "trim_pad_ms": 120,
                            "gain_applied": round(g, 5) if g != 1.0 else 1.0,
                            "resampled_from": sr, "target": "48000/mono/PCM_16",
                            "truncated": False},
            "speech_gate": {k: (round(v, 5) if isinstance(v, float) else v) for k, v in ft2.items()},
            "license_evidence": ev,
            "license_verdict": {"ok": True, "reason": lic_reason},
            "disk_recheck": {"redecoded": True, "format": "WAV/PCM_16/mono/48000",
                             "gate_pass_on_disk": True},
        })
        per_spk[spk] += 1
        if len(entries) % 25 == 0:
            print(src, "accepted", len(entries), "rejected", len(rejected),
                  "%.0fs" % (time.time() - t0), flush=True)
        if len(entries) % 50 == 0:            # checkpoint: keep manifest/audio consistent
            C.write_json(os.path.join(C.SELF_DIR, "manifest_%s.json" % src), entries)
            C.write_json(os.path.join(C.SELF_DIR, "rejected_%s.json" % src), rejected)
    C.write_json(os.path.join(C.SELF_DIR, "manifest_%s.json" % src), entries)
    C.write_json(os.path.join(C.SELF_DIR, "gates_%s.json" % src), [
        {"slug": e["name"], "local_file": e["local_file"], "speech_gate": e["speech_gate"]} for e in entries])
    C.write_json(os.path.join(C.SELF_DIR, "rejected_%s.json" % src), rejected)
    stats = {"source": src, "candidates": len(idx), "accepted": len(entries),
             "rejected_records": len(rejected), "reason_hist": dict(reasons),
             "speakers_accepted": len(per_spk),
             "per_speaker_max": max(list(per_spk.values()) or [0]),
             "mean_dur": round(float(np.mean([e["duration_s"] for e in entries])), 3) if entries else 0,
             "wall_s": round(time.time() - t0, 1)}
    C.write_json(os.path.join(C.SELF_DIR, "stats_%s.json" % src), stats)
    print(json.dumps(stats, ensure_ascii=False))


if __name__ == "__main__":
    for s in (sys.argv[1:] or ["aishell3", "vctk", "commons"]):
        process(s)
