# -*- coding: utf-8 -*-
"""Merge per-source shards -> real_speech_manifest.json and independently re-verify.

Everything is recomputed from the ON-DISK wav, exactly like refill/merge.py, plus the
licence-evidence audit this lane requires.  Two decoders have to agree on every file:
soundfile (which feeds the gate) and the stdlib `wave` reader (container facts and
duration), so no published number rests on a single library's parsing.  The speech_gate /
duration_s / peak / rms an entry ships with ARE the values measured in this run.
An entry survives only if:
  1. the file exists and decodes twice, format is WAV / PCM_16 / mono / 48000 Hz
  2. the speech gate (incl. the hard 3.0-10.0 s band) passes on the disk bytes
  3. |disk duration - manifest duration| < 0.05 s and the wave decode agrees within 0.02 s
  4. license, license_url, captured_at, quote and attribution are all non-empty in
     license_evidence, and the licence name passes the allow/deny gate (no NC, no SA)
  5. the speaker id stays within MAX_PER_SPEAKER (over-cap entries are dropped, not kept)
"""
import os, sys, json, wave, collections
import numpy as np
import soundfile as sf

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C
import gate as G

SHARDS = ["aishell3", "vctk", "commons"]
MAX_PER_SPEAKER = 5
REQUIRED_EVIDENCE = ("license_url", "captured_at", "quote", "local_copy")


def dedupe(seq):
    """Order-preserving de-duplication (the shard tags carried 真人声 twice)."""
    out, seen = [], set()
    for x in seq or []:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def wave_recheck(path, sf_dur):
    """Independent decode with the stdlib `wave` module -> (ok, problems).

    soundfile is the primary decoder; a second, dependency-free decoder has to agree on
    the container facts (mono / 16-bit / uncompressed / 48 kHz) and on the duration, so
    no gate number in this file rests on one library's parsing alone.
    """
    probs = []
    try:
        w = wave.open(path, "rb")
        ch, sw, sr, n = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
        comp, compname = w.getcomptype(), w.getcompname()
        w.close()
    except Exception as ex:
        return False, ["wave_decode_fail:%s" % type(ex).__name__]
    if (ch, sw, sr) != (1, 2, 48000) or comp != "NONE":
        probs.append("wave_format:%dch/%dbit/%dHz/%s/%s" % (ch, sw * 8, sr, comp, compname))
    if abs(n / float(sr) - sf_dur) >= 0.02:
        probs.append("wave_duration_delta:%.3f" % (n / float(sr) - sf_dur))
    return (len(probs) == 0), probs


def audit_entry(e):
    """-> (ok, problems, disk_features)."""
    probs = []
    f = e.get("local_file") or ""
    if not os.path.exists(f):
        return False, ["file_missing"], None
    try:
        info = sf.info(f)
        x, sr = sf.read(f, dtype="float32")
    except Exception as ex:
        return False, ["decode_fail:%s" % type(ex).__name__], None
    if x.ndim > 1:
        x = x.mean(axis=1)
    if not (info.format == "WAV" and info.subtype == "PCM_16" and info.channels == 1 and sr == 48000):
        probs.append("format_mismatch:%s/%s/%sch/%dHz" % (info.format, info.subtype, info.channels, sr))
    okw, pw = wave_recheck(f, e["duration_s"])
    probs += pw
    ft, ok, why = G.evaluate(np.asarray(x, dtype=np.float64), sr)
    if not ok:
        probs += ["gate:" + r for r in why]
    if abs(ft["dur"] - e["duration_s"]) >= 0.05:
        probs.append("duration_delta:%.3f" % (ft["dur"] - e["duration_s"]))
    if not (G.THRESH["dur_min"] <= e["duration_s"] <= G.THRESH["dur_max"]):
        probs.append("manifest_duration_out_of_band")
    if not e.get("license"):
        probs.append("license_field_empty")
    if not e.get("source_url") or not e.get("page_url"):
        probs.append("provenance_url_missing")
    if not e.get("attribution"):
        probs.append("attribution_empty")
    ev = e.get("license_evidence") or []
    if not ev:
        probs.append("license_evidence_missing")
    else:
        for k in REQUIRED_EVIDENCE:
            if not any(x.get(k) for x in ev):
                probs.append("license_evidence_no_%s" % k)
        for x in ev:
            q = str(x.get("quote") or "")
            if x.get("license_url") and not x.get("sha256") and "FETCH" not in q:
                probs.append("license_evidence_unhashed")
                break
        if not any(x.get("http_status") == 200 for x in ev):
            probs.append("license_page_not_reachable")
    okl, whyl = C.license_verdict(e.get("license_name_raw") or e.get("license"),
                                  (ev[0].get("license_url") if ev else None),
                                  json.dumps(ev, ensure_ascii=False)[:1500])
    if not okl:
        probs.append("license_gate:" + whyl)
    if not e.get("speech_gate"):
        probs.append("speech_gate_missing")
    return (len(probs) == 0), probs, ft


def main():
    merged = []
    for s in SHARDS:
        p = os.path.join(C.SELF_DIR, "manifest_%s.json" % s)
        if os.path.exists(p):
            merged += C.read_json(p, [])
    seen_slug, per_spk, verified, bad = set(), collections.Counter(), [], []
    n_wave_ok = 0
    for e in merged:
        f = e.get("local_file") or ""
        slug = os.path.basename(f)
        spk = (e.get("source_site") or "") + ":" + (e.get("speaker_id") or "unknown")
        ok, probs, ft = audit_entry(e)
        if not ok:
            bad.append({"slug": slug, "problems": probs})
            continue
        if slug in seen_slug:
            bad.append({"slug": slug, "problems": ["duplicate_slug"]})
            continue
        if per_spk[spk] >= MAX_PER_SPEAKER:
            bad.append({"slug": slug, "problems": ["speaker_cap_exceeded(%s)" % spk]})
            continue
        seen_slug.add(slug)
        per_spk[spk] += 1
        # the published gate numbers ARE the re-decoded ones; shard values only serve
        # as a drift reference.
        shard = e.get("speech_gate") or {}
        drift = max([abs(float(ft[k]) - float(shard[k])) for k in ft
                     if isinstance(shard.get(k), (int, float))] or [0.0])
        e["speech_gate"] = {k: (round(v, 5) if isinstance(v, float) else v) for k, v in ft.items()}
        e["duration_s"] = round(ft["dur"], 3)
        e["peak"] = round(ft["peak"], 5)
        e["rms"] = round(ft["rms"], 5)
        e["tags"] = dedupe(e.get("tags"))
        wok, _ = wave_recheck(f, ft["dur"])
        n_wave_ok += 1 if wok else 0
        e["verify"] = {"redecoded": True, "format": "48k/mono/PCM_16",
                       "gate_pass_on_disk": True,
                       "dur_delta": round(ft["dur"] - shard.get("dur", ft["dur"]), 4),
                       "independent_wave_decode": wok,
                       "gate_delta_vs_shard": round(drift, 5),
                       "license_evidence_ok": True,
                       "license_verdict": e.get("license_verdict", {}).get("reason", "?"),
                       "verified_at": C.time.strftime("%Y-%m-%dT%H:%M:%S")}
        verified.append(e)
    C.write_json(os.path.join(C.SELF_DIR, "real_speech_manifest.json"), verified)

    bysrc = collections.Counter(e["source_site"] for e in verified)
    byl = collections.Counter((e.get("language") or "?") for e in verified)
    byg = collections.Counter((e.get("gender") or "unknown") for e in verified)
    spk = collections.Counter((e["source_site"], e.get("speaker_id")) for e in verified)
    durs = [e["duration_s"] for e in verified]
    lic = collections.Counter(e["license"] for e in verified)
    reason_hist = collections.Counter()
    for b in bad:
        for p in b["problems"]:
            reason_hist[p.split("(")[0].split(":")[0] if not p.startswith("gate:") else p] += 1
    summary = {
        "generated_at": C.time.strftime("%Y-%m-%dT%H:%M:%S"),
        "merged_from_shards": {s: len(C.read_json(os.path.join(C.SELF_DIR, "manifest_%s.json" % s), []))
                               for s in SHARDS},
        "candidates_merged": len(merged),
        "verified_total": len(verified),
        "rejected_in_verify": len(bad),
        "by_source": dict(bysrc), "by_language": dict(byl), "by_gender": dict(byg),
        "by_license": dict(lic),
        "distinct_speakers": len(spk),
        "speakers_per_source": dict(collections.Counter(k[0] for k in spk)),
        "max_per_speaker": max(list(spk.values()) or [0]),
        "independent_wave_decode_ok": n_wave_ok,
        "gate_numbers_recomputed_from_disk": True,
        "duration_s": {"min": round(min(durs), 3) if durs else 0,
                       "mean": round(float(np.mean(durs)), 3) if durs else 0,
                       "max": round(max(durs), 3) if durs else 0,
                       "band": [G.THRESH["dur_min"], G.THRESH["dur_max"]]},
        "verify_reject_reasons": dict(reason_hist),
        "bad_sample": bad[:25],
        "gate_thresholds": G.THRESH,
    }
    C.write_json(os.path.join(C.SELF_DIR, "verify_real_speech.json"), summary)
    print(json.dumps(summary, ensure_ascii=False)[:2200])


if __name__ == "__main__":
    main()
