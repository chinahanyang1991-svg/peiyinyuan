# -*- coding: utf-8 -*-
"""Merge per-category shards -> refill_manifest.json and independently re-verify.

Verification per manifest entry: local_file exists, soundfile decodes,
format is WAV/mono/48k/PCM, full speech gate re-run from the ON-DISK file.
Any mismatch = entry removed from merged manifest + listed in verify_refill.json.
"""
import json, os, sys, collections
import soundfile as sf
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C, gate as G

shards = ["animal", "electronic", "mechanical", "anime", "human"]
merged, bad = [], []
for cat in shards:
    p = os.path.join(C.SELF_DIR, "manifest_%s.json" % cat)
    if os.path.exists(p):
        merged += json.load(open(p, encoding="utf-8"))

verified = []
for e in merged:
    f = e["local_file"]
    try:
        info = sf.info(f)
        x, sr = sf.read(f, dtype="float32")
        if x.ndim > 1: x = x.mean(axis=1)
        ft, ok, reasons = G.evaluate(x.astype("float64"), sr)
        fmt_ok = (info.format == "WAV" and info.subtype == "PCM_16"
                  and info.channels == 1 and sr == 48000)
        dur_ok = abs(ft["dur"] - e["duration_s"]) < 0.05
        if ok and fmt_ok and dur_ok:
            e["verify"] = {"redecoded": True, "format": "48k/mono/PCM_16",
                           "dur_delta": round(ft["dur"] - e["duration_s"], 4)}
            verified.append(e)
        else:
            bad.append({"slug": os.path.basename(f), "reasons": reasons +
                        ([] if fmt_ok else ["format_mismatch"]) +
                        ([] if dur_ok else ["duration_mismatch"])})
    except Exception as ex:
        bad.append({"slug": f, "reasons": ["read_fail:%s" % type(ex).__name__]})

out = os.path.join(C.SELF_DIR, "refill_manifest.json")
json.dump(verified, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
bycat = collections.Counter(e["category"] for e in verified)
ge3 = collections.Counter(e["category"] for e in verified if e["duration_s"] >= 3.0)
stats = {c: json.load(open(os.path.join(C.SELF_DIR, "stats_%s.json" % c), encoding="utf-8"))
         for c in shards if os.path.exists(os.path.join(C.SELF_DIR, "stats_%s.json" % c))}
summary = {"verified_total": len(verified), "rejected_in_verify": len(bad),
           "by_category": dict(bycat), "ge3s_by_category": dict(ge3),
           "bad": bad[:20], "gen_stats": {c: {k: s[k] for k in
                       ("attempted", "network_failed", "generated", "passed", "pass_rate", "wall_s")}
                       for c, s in stats.items()}}
json.dump(summary, open(os.path.join(C.SELF_DIR, "verify_refill.json"), "w", encoding="utf-8"),
          ensure_ascii=False, indent=1)
print(json.dumps(summary, ensure_ascii=False, indent=1)[:1500])
