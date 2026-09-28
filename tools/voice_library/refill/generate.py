# -*- coding: utf-8 -*-
"""Refill generator: edge-tts -> decode 48k -> DSP -> speech gate -> wav+manifest shard.

Usage:  python -X utf8 generate.py --category animal [--count 108] [--start N]
Writes (per category, resumable/append-safe):
  data/voices/library/refill/<cat>/<slug>.wav          (only gate-passed files)
  tools/voice_library/refill/manifest_<cat>.json       shard (list, manifest.json schema)
  tools/voice_library/refill/gates_<cat>.json          every measured candidate
  tools/voice_library/refill/stats_<cat>.json          attempt/network/gate tallies
  tools/voice_library/refill/REPORT-<cat>.md           per-category report
"""
import argparse, asyncio, json, os, sys, time, traceback
import numpy as np
import soundfile as sf
import edge_tts

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C
import gate as G

TMP = os.path.join(C.SELF_DIR, "_tmp")
os.makedirs(TMP, exist_ok=True)

def existing_shard(cat):
    p = os.path.join(C.SELF_DIR, "manifest_%s.json" % cat)
    if os.path.exists(p):
        with open(p, "r", encoding="utf-8") as f: return json.load(f)
    return []

async def synth_one(sem, job, tag, attempts=3):
    """edge-tts -> mp3 file; returns (bytes, error)."""
    os.makedirs(TMP, exist_ok=True)
    path = os.path.join(TMP, tag + ".mp3")
    last = None
    for k in range(attempts):
        try:
            async with sem:
                c = edge_tts.Communicate(job["text"], job["voice"],
                                        rate=job["rate"], pitch=job["pitch"])
                await c.save(path)
            with open(path, "rb") as f: b = f.read()
            if len(b) < 1000:
                last = "empty_body"; await asyncio.sleep(1.5); continue
            return b, None
        except Exception as e:
            last = "%s:%s" % (type(e).__name__, str(e)[:120])
            await asyncio.sleep(2.0 + 3.0 * k)
    return None, last

def process_one(job, mp3_bytes, out_path):
    """decode->48k->trim->dsp->normalize->gate. returns (feat,ok,reasons,src_sr,nbytes)."""
    import io
    x, sr = sf.read(io.BytesIO(mp3_bytes), dtype="float32")
    if x.ndim > 1: x = x.mean(axis=1)
    src_sr, nbytes = sr, len(mp3_bytes)
    x, sr = C.to_target_sr(x, sr)
    x = C.trim_edges(x, sr)
    x = C.apply_dsp(x, sr, job["dsp"])
    x = C.trim_edges(x, sr)
    x = C.normalize_peak(x, 0.80)
    ft, ok, reasons = G.evaluate(x, sr)
    if ok and not (0.02 <= ft["peak"]):  # defensive; normalize guarantees band
        ok, reasons = False, reasons + ["peak_too_low"]
    if ft["peak"] < 0.02 and not ok and "peak_too_low" in reasons:
        x = C.normalize_peak(x, 0.70)
        ft, ok, reasons = G.evaluate(x, sr)
    if ok:
        C.save_pcm(x, sr, out_path)
    return ft, ok, reasons, src_sr, nbytes

def manifest_entry(job, cat, out_path, ft, src_sr, nbytes):
    dsp_s = json.dumps(job["dsp"], ensure_ascii=False, sort_keys=True)
    return {
        "category": cat,
        "name": "%s语音-%s #%03d" % (C.CAT_CN[cat], job["fam_label"], job["idx"]),
        "source_url": "local://design/%s" % job["slug"],
        "page_url": "local://design/%s" % job["slug"],
        "license": "Internal-Generated",
        "attribution": ("edge-tts %s rate=%s pitch=%s | dsp=%s | text=%s"
                        % (job["voice"], job["rate"], job["pitch"], dsp_s, job["text"])),
        "local_file": out_path,
        "duration_s": round(ft["dur"], 3),
        "peak": round(ft["peak"], 4),
        "rms": round(ft["rms"], 5),
        "source_sample_rate": src_sr,
        "source_bytes": nbytes,
        "source_site": "local_engine",
        "upstream_title": "%s / %s %s %s" % (job["fam_label"], job["voice"], job["rate"], job["pitch"]),
        "tags": ["%s语音变体" % C.CAT_CN[cat], job["fam_key"], "speech-gate-pass"],
        "age_group": None,
        "speech_gate": {k: (round(v, 4) if isinstance(v, float) else v) for k, v in ft.items()},
    }

async def run(cat, count, start, conc):
    jobs = C.build_jobs(cat, start, count)
    shard = existing_shard(cat)
    done_slugs = {os.path.basename(e["local_file"]).rsplit(".", 1)[0] for e in shard}
    jobs = [j for j in jobs if j["slug"] not in done_slugs]
    sem = asyncio.Semaphore(conc)
    gates, stats = [], {"category": cat, "attempted": 0, "network_failed": 0,
                        "decoded_failed": 0, "generated": 0, "passed": 0,
                        "reject_reasons": {}, "network_errors": {}}
    t0 = time.time()
    new_entries = list(shard)

    async def worker(j):
        tag = "%s_%s" % (cat, j["slug"])
        out_path = os.path.join(C.AUDIO_ROOT, cat, j["slug"] + ".wav")
        mp3, err = await synth_one(sem, j, tag)
        stats["attempted"] += 1
        if mp3 is None:
            stats["network_failed"] += 1
            stats["network_errors"][err.split(":")[0]] = stats["network_errors"].get(err.split(":")[0], 0) + 1
            return
        try:
            ft, ok, reasons, src_sr, nbytes = process_one(j, mp3, out_path)
        except Exception as e:
            stats["decoded_failed"] += 1
            print("DECODEFAIL", j["slug"], type(e).__name__, str(e)[:100])
            return
        stats["generated"] += 1
        gates.append({"slug": j["slug"], "features": {k: (round(v, 4) if isinstance(v, float) else v) for k, v in ft.items()},
                      "pass": ok, "reasons": reasons})
        if ok:
            stats["passed"] += 1
            new_entries.append(manifest_entry(j, cat, out_path, ft, src_sr, nbytes))
        else:
            for r in reasons:
                stats["reject_reasons"][r] = stats["reject_reasons"].get(r, 0) + 1

    # chunked gather to keep memory/log readable
    CH = 40
    for i in range(0, len(jobs), CH):
        await asyncio.gather(*[worker(j) for j in jobs[i:i + CH]])
        done = stats["passed"]
        print("progress %d/%d pass=%d elapsed=%.0fs" % (min(i + CH, len(jobs)), len(jobs), done, time.time() - t0), flush=True)

    stats["pass_rate"] = round(stats["passed"] / stats["generated"], 4) if stats["generated"] else 0.0
    stats["total_passed_in_shard"] = len(new_entries)
    stats["wall_s"] = round(time.time() - t0, 1)
    def dump(name, obj):
        p = os.path.join(C.SELF_DIR, name)
        with open(p, "w", encoding="utf-8") as f: json.dump(obj, f, ensure_ascii=False, indent=1)
    dump("manifest_%s.json" % cat, new_entries)
    dump("gates_%s.json" % cat, gates)
    dump("stats_%s.json" % cat, stats)
    write_report(cat, new_entries, stats)
    print("SUMMARY", cat, json.dumps(stats, ensure_ascii=False)[:400])

def write_report(cat, entries, stats):
    import collections
    l3s = sum(1 for e in entries if e["duration_s"] >= 3.0)
    samp = entries[-3:]
    lines = ["# REPORT %s refill" % cat, "",
             "- 目标: >=100 合格", "- 实际生成(过门禁前): %d" % stats["generated"],
             "- 合格: %d (其中 >=3s: %d)" % (len(entries), l3s),
             "- 累计 shard 总数: %d, 可用率: %.1f%%" % (len(entries), 100 * stats["pass_rate"]),
             "- 网络失败: %d %s" % (stats["network_failed"], stats["network_errors"]),
             "- 解码失败: %d" % stats["decoded_failed"],
             "- 淘汰原因分布: %s" % stats["reject_reasons"], "", "## 抽样 (最近3条)", ""]
    for e in samp:
        lines.append("- %s | dur=%.2fs peak=%.3f rms=%.4f | flat=%.4f zcr=%.3f mod_prom=%.2f mod_f=%.2f" %
                     (e["local_file"], e["duration_s"], e["peak"], e["rms"],
                      e["speech_gate"]["flat"], e["speech_gate"]["zcr"],
                      e["speech_gate"]["mod_prom"], e["speech_gate"]["mod_f"]))
    fams = collections.Counter(e["tags"][1] for e in entries)
    lines += ["", "## 家族分布", json.dumps(dict(fams), ensure_ascii=False)]
    with open(os.path.join(C.SELF_DIR, "REPORT-%s.md" % cat), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--category", required=True, choices=list(C.FAMILIES))
    ap.add_argument("--count", type=int, default=108)
    ap.add_argument("--start", type=int, default=0)
    ap.add_argument("--conc", type=int, default=6)
    a = ap.parse_args()
    asyncio.run(run(a.category, a.count, a.start, a.conc))
