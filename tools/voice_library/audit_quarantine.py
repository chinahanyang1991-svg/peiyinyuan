"""审计 data/quarantine 下的隔离音频是否仍被库引用。只读，不删。

隔离目录里的文件是从原位置 move 出来的，DB 路径列指向的是**原位置**，
所以按 basename 比对（文件名在 move 中保持不变），而不是按完整路径。
"""
import os
import sqlite3
import json
import glob

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
QDIR = os.path.join(ROOT, "data", "quarantine")
LANES = os.path.dirname(ROOT)

db_base = {}
c = sqlite3.connect(f"file:{os.path.join(ROOT,'data','peiyinyuan.db')}?mode=ro", uri=True)
rows = list(c.execute(
    "SELECT id, engine, source, name, sample_audio_path, reference_audio_path "
    "FROM voice_profiles"))
for rid, eng, src, name, p1, p2 in rows:
    for p in (p1, p2):
        if p:
            db_base.setdefault(os.path.basename(str(p)).lower(), []).append(rid)
n_rows = len(rows)
c.close()

summary = []
for bundle in sorted(os.listdir(QDIR)):
    bdir = os.path.join(QDIR, bundle)
    if not os.path.isdir(bdir):
        continue
    n = size = 0
    referenced = []
    for dirpath, _, files in os.walk(bdir):
        for f in files:
            if f.endswith(".json"):
                continue
            full = os.path.join(dirpath, f)
            n += 1
            try:
                size += os.path.getsize(full)
            except OSError:
                pass
            if f.lower() in db_base:
                referenced.append({"file": os.path.relpath(full, ROOT),
                                   "voice_ids": db_base[f.lower()][:3]})
    ids = []
    m = os.path.join(LANES, f"swap-{bundle.split('voice-purge-')[-1]}", "purge_ids.json")
    if os.path.isfile(m):
        ids = json.load(open(m, encoding="utf-8"))
    still = []
    if ids:
        cc = sqlite3.connect(f"file:{os.path.join(ROOT,'data','peiyinyuan.db')}?mode=ro", uri=True)
        q = ",".join("?" * len(ids))
        still = [r[0] for r in cc.execute(
            f"SELECT id FROM voice_profiles WHERE id IN ({q})", ids)]
        cc.close()
    summary.append({"bundle": bundle, "files": n, "mb": round(size / 1048576, 1),
                    "referenced_now": len(referenced), "ref_examples": referenced[:3],
                    "purge_ids": len(ids), "purge_ids_still_in_db": len(still),
                    "still_examples": still[:3]})

print(json.dumps({"voice_profiles_rows": n_rows, "bundles": summary},
                 ensure_ascii=False, indent=1))
