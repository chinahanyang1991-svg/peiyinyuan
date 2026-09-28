"""隔离区收口：出清单 -> 复核零引用 -> （--apply 时）删除并记录释放字节。

判据：文件名与 voice_profiles 的三个路径列逐基名比对。隔离目录里的音频是从
原位置 move 出来的，move 不改文件名，所以按 basename 比对才有效（按完整路径
比对会恒为 0，那是假绿）。
"""
import os
import sys
import json
import time
import shutil
import sqlite3
import hashlib

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
QDIR = os.path.join(ROOT, "data", "quarantine")
LANES = os.path.join(os.path.dirname(ROOT), "agent-ops")
DB = os.path.join(ROOT, "data", "peiyinyuan.db")
PATH_COLS = ("model_path", "sample_audio_path", "reference_audio_path")


def db_basenames():
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    out = {}
    for col in PATH_COLS:
        for (v,) in c.execute(
                f"SELECT {col} FROM voice_profiles WHERE {col} IS NOT NULL"):
            if v:
                out.setdefault(os.path.basename(str(v)).lower(), 1)
    rows = c.execute("SELECT count(*) FROM voice_profiles").fetchone()[0]
    c.close()
    return out, rows


def collect(db_base):
    bundles, total_bytes, refs = [], 0, []
    for bundle in sorted(os.listdir(QDIR)):
        bdir = os.path.join(QDIR, bundle)
        if not os.path.isdir(bdir):
            continue
        files, nbytes = [], 0
        for dirpath, _, names in os.walk(bdir):
            for f in names:
                full = os.path.join(dirpath, f)
                try:
                    st = os.stat(full)
                except OSError:
                    continue
                nbytes += st.st_size
                with open(full, "rb") as fh:
                    digest = hashlib.sha256(fh.read()).hexdigest()[:16]
                files.append({"path": os.path.relpath(full, ROOT).replace("\\", "/"),
                              "bytes": st.st_size, "sha256_16": digest})
                if f.lower() in db_base:
                    refs.append(os.path.relpath(full, ROOT))
        ids = []
        m = os.path.join(LANES, f"swap-{bundle.split('voice-purge-')[-1]}",
                         "purge_ids.json")
        if os.path.isfile(m):
            ids = json.load(open(m, encoding="utf-8"))
        still = []
        if ids:
            c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
            q = ",".join("?" * len(ids))
            still = [r[0] for r in c.execute(
                f"SELECT id FROM voice_profiles WHERE id IN ({q})", ids)]
            c.close()
        bundles.append({"bundle": bundle, "files": len(files),
                        "mb": round(nbytes / 1048576, 1), "bytes": nbytes,
                        "purge_ids": len(ids), "purge_ids_still_in_db": len(still),
                        "file_list": files})
        total_bytes += nbytes
    return bundles, total_bytes, refs


def main():
    apply = "--apply" in sys.argv
    db_base, rows = db_basenames()
    bundles, total_bytes, refs = collect(db_base)
    manifest = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "voice_profiles_rows": rows,
        "db_path_basenames": len(db_base),
        "quarantine_referenced_now": len(refs),
        "referenced_examples": refs[:10],
        "total_bytes": total_bytes,
        "total_mb": round(total_bytes / 1048576, 1),
        "mode": "delete" if apply else "dry-run",
        "deleted": [],
        "bundles": bundles,
    }
    out = os.path.join(ROOT, "qa",
                       f"quarantine_close_{time.strftime('%Y%m%d_%H%M%S')}.json")
    if refs:
        manifest["decision"] = "KEEP: 仍有音色行引用隔离音频"
        json.dump(manifest, open(out, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)
        print(json.dumps({k: manifest[k] for k in
                          ("decision", "quarantine_referenced_now", "total_mb")},
                         ensure_ascii=False))
        print("manifest:", out)
        return 1
    if apply:
        for bundle in sorted(os.listdir(QDIR)):
            bdir = os.path.join(QDIR, bundle)
            if not os.path.isdir(bdir):
                continue
            shutil.rmtree(bdir)
            manifest["deleted"].append(bundle)
        manifest["decision"] = "DELETED: 零引用确认后释放"
    else:
        manifest["decision"] = "DRY-RUN: 零引用，待 --apply"
    json.dump(manifest, open(out, "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print(json.dumps({"decision": manifest["decision"], "total_mb": manifest["total_mb"],
                      "bundles": [(b["bundle"], b["files"], b["mb"],
                                   b["purge_ids_still_in_db"]) for b in bundles]},
                     ensure_ascii=False, indent=1))
    print("manifest:", out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
