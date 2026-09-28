# -*- coding: utf-8 -*-
"""Calibrate speech-gate thresholds (cached mp3s so re-runs are offline).

Positives: one clip per DSP family per category (edge-tts + full pipeline).
Negatives: existing raw-SFX library files from animal/mechanical/electronic.
Writes calib_report.json. Re-run reads _tmp/calib_*.mp3 cache (no network).
"""
import asyncio, json, os, random, sys
import numpy as np
import soundfile as sf

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C, gate as G

CACHE = os.path.join(C.SELF_DIR, "_tmp")

async def main():
    os.makedirs(CACHE, exist_ok=True)
    import generate as GN
    pos, neg = [], []
    jobs = []
    for cat, fams in C.FAMILIES.items():
        for i, fam in enumerate(fams):
            jobs.append(C.build_jobs(cat, i * 7, 1)[0])
    sem = asyncio.Semaphore(6)
    for j in jobs:
        cached = os.path.join(CACHE, "calib_%s.mp3" % j["slug"])
        if os.path.exists(cached):
            mp3 = open(cached, "rb").read()
        else:
            mp3, err = await GN.synth_one(sem, j, "calib", attempts=2)
            if mp3 is None:
                print("NETFAIL", j["slug"], err); continue
            open(cached, "wb").write(mp3)
        out = os.path.join(CACHE, j["slug"] + ".wav")
        ft, ok, reasons, _, _ = GN.process_one(j, mp3, out)
        ft = dict(ft); ft.update(slug=j["slug"], ok=ok, reasons=reasons)
        pos.append(ft)
    random.seed(7)
    lib = os.path.join(C.ROOT, "data", "voices", "library")
    files = []
    for d in ["animal", "mechanical", "electronic"]:
        fs = [os.path.join(lib, d, x) for x in os.listdir(os.path.join(lib, d)) if x.endswith(".wav")]
        files += random.sample(fs, min(10, len(fs)))
    for p in files:
        try:
            x, sr = sf.read(p, dtype="float32")
            if x.ndim > 1: x = x.mean(axis=1)
            ft = G.features(x, sr); ft = dict(ft); ft["slug"] = p.replace(C.ROOT, "")
            _, okn, rs = G.evaluate(np.asarray(x, dtype=np.float64), sr)
            ft["gate_ok"] = okn; ft["reasons"] = rs
            neg.append(ft)
        except Exception as e:
            print("NEGFAIL", p, e)
    json.dump({"positives": pos, "negatives": neg},
              open(os.path.join(C.SELF_DIR, "calib_report.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    def summ(rows, key):
        if not rows: return "[]"
        v = np.array([r[key] for r in rows], dtype=float)
        return "[%s, %s, %s]" % (round(float(v.min()), 4), round(float(np.median(v)), 4), round(float(v.max()), 4))
    print("N pos=%d N neg=%d" % (len(pos), len(neg)))
    for key in ["dur", "flat", "zcr", "mod_prom", "mod_f", "sil_ratio"]:
        print("%-10s pos(min,med,max)=%-22s neg(min,med,max)=%s" % (key, summ(pos, key), summ(neg, key)))
    print("pos_fail:", [(p["slug"], p["reasons"]) for p in pos if not p["ok"]])
    print("neg_gate_pass:", [n["slug"] for n in neg if n.get("gate_ok")])

if __name__ == "__main__":
    asyncio.run(main())
