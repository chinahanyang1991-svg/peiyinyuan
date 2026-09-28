# -*- coding: utf-8 -*-
"""Source 1: AISHELL-3 (Mandarin, 218 speakers, Apache-2.0).

Licensing evidence is captured from TWO independent official statements:
  * OpenSLR SLR93 record page ("License: Apache License v.2.0")
  * the AISHELL/AISHELL-3 dataset card README on the reachable mirror
Audio is taken per-file (one wav per request) from the public repo tree; no login, no
quota bypass, concurrency<=2, hard stop on 403/429.
"""
import os, re, sys, json, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C

REPO = "AISHELL/AISHELL-3"
MIRROR = "https://hf-mirror.com"
API = MIRROR + "/api/datasets/" + REPO
SLR_PAGE = "https://www.openslr.org/93/"
SPLIT_DIRS = ["test/wav", "train/wav"]
SRC = "aishell3"
MAX_PER_SPEAKER = 5
TARGET_SPEAKERS = int(os.environ.get("RS_A3_SPEAKERS", "120"))
TARGET_CLIPS = int(os.environ.get("RS_A3_CLIPS", "150"))
EST_SR = 44100


def list_children(http, path):
    """List one tree level, following the paginated Link rel=next cursor."""
    out, url = [], API + "/tree/main/" + path
    for _ in range(10):
        raw, nxt, err = http.get_page(url, timeout=60)
        if raw is None:
            return out, err
        try:
            out += json.loads(raw)
        except Exception as e:
            return out, "bad_json:%s" % e
        if not nxt:
            return out, None
        url = nxt
    return out, None


def main():
    http = C.Http(SRC, rate=2.0, conc=2)
    out_dir = os.path.join(C.RAW_DIR, SRC)
    os.makedirs(out_dir, exist_ok=True)
    log = {"source": SRC, "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
           "skips": {}, "errors": [], "blocked": None}

    # ---- license evidence ----------------------------------------------------
    lic_pages = []
    ev_slr = C.capture_license(SLR_PAGE, "dataset_official_record_page",
                               "OpenSLR SLR93 record: License: Apache License v.2.0")
    lic_pages.append(ev_slr)
    raw, err = http.get(MIRROR + "/datasets/" + REPO + "/resolve/main/README.md", max_bytes=200000)
    if raw:
        p = os.path.join(C.LIC_DIR, "aishell3_readme.md")
        open(p, "wb").write(raw)
        txt = raw.decode("utf-8", "replace")
        q = C.first_license_sentence(txt)
        lic_pages.append({"license_url": MIRROR + "/datasets/" + REPO + "/resolve/main/README.md",
                          "kind": "dataset_card", "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                          "local_copy": p, "http_status": 200, "sha256": C.hashlib.sha256(raw).hexdigest(),
                          "quote": q or "card front-matter license: apache-2.0", "note": "HF dataset card"})
    else:
        lic_pages.append({"license_url": MIRROR + "/datasets/" + REPO + "/resolve/main/README.md",
                          "kind": "dataset_card", "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                          "http_status": None, "quote": "FETCH_ERR %s" % err, "local_copy": None})
    C.write_json(os.path.join(C.SELF_DIR, "license_evidence_%s.json" % SRC), lic_pages)

    # speaker metadata (gender / age band) if the repo publishes it
    speaker_meta = {}
    for cand in ["speaker-info.txt", "sample-txt/speaker-info.txt", "metadata/speaker-info.txt"]:
        b, e2 = http.get(MIRROR + "/datasets/" + REPO + "/resolve/main/" + cand, max_bytes=200000)
        if b:
            p = os.path.join(C.TMP_DIR, "aishell3_speaker_info.txt")
            open(p, "wb").write(b)
            lines = b.decode("utf-8", "replace").splitlines()
            hdr = (lines[0] if lines else "")
            cols = [c.strip().lower() for c in hdr.lstrip("#").split()]
            for ln in lines[1:]:
                f = ln.split()
                if len(f) >= 2 and f[0].startswith("SSB"):
                    d = {}
                    for i, c in enumerate(cols[1:], start=1):
                        if i < len(f):
                            d[c] = f[i]
                    speaker_meta[f[0]] = d
            log["speaker_info_file"] = cand
            break

    # ---- enumerate speakers, then pick clips by estimated duration ------------
    cands, seen_spk = [], 0
    for split in SPLIT_DIRS:
        if seen_spk >= TARGET_SPEAKERS:
            break
        kids, lerr = list_children(http, split)
        if lerr:
            log["errors"].append("tree %s: %s" % (split, lerr))
        dirs = [k["path"] for k in kids if k.get("type") == "directory"]
        log.setdefault("dir_counts", {})[split] = len(dirs)
        for sp in dirs:
            if seen_spk >= TARGET_SPEAKERS:
                break
            sid = sp.rsplit("/", 1)[-1]
            files, ferr = list_children(http, sp)
            if ferr:
                log["errors"].append("tree %s: %s" % (sp, ferr))
                continue
            wavs = [f for f in files if f["path"].endswith(".wav")]
            good = []
            for w in wavs:
                dur = C.est_wav_dur(w.get("size") or 0, EST_SR)
                if 3.3 <= dur <= 9.6:
                    good.append((w["path"], w.get("size"), dur))
            if not good:
                log["skips"][sid] = "no_candidate_in_3.3-9.6s"
                continue
            seen_spk += 1
            step = max(1, len(good) // MAX_PER_SPEAKER)
            for p, sz, dur in good[::step][:MAX_PER_SPEAKER]:
                cands.append({"path": p, "speaker": sid, "size": sz, "est_dur": round(dur, 2),
                              "split": split})
        if len(cands) >= TARGET_CLIPS:
            break
    log["speakers_seen"] = seen_spk
    log["candidates"] = len(cands)

    # ---- download ------------------------------------------------------------
    idx = []
    for c in cands[:TARGET_CLIPS]:
        slug = "%s-%s-%s" % (SRC, c["speaker"].lower(), c["path"].rsplit("/", 1)[-1].replace(".wav", ""))
        dst = os.path.join(out_dir, slug + ".wav")
        url = MIRROR + "/datasets/" + REPO + "/resolve/main/" + c["path"]
        page = "https://hf-mirror.com/datasets/" + REPO + "/tree/main/" + os.path.dirname(c["path"])
        if not os.path.exists(dst) or os.path.getsize(dst) != c["size"]:
            data, err = http.get(url, max_bytes=8 * 1024 * 1024)
            if data is None:
                log["errors"].append("get %s: %s" % (url, err))
                if http.stats.get("blocked"):
                    log["blocked"] = http.stats["blocked"]
                    break
                continue
            open(dst, "wb").write(data)
        m = speaker_meta.get(c["speaker"], {})
        idx.append({"slug": slug, "raw": dst, "speaker": c["speaker"], "source_url": url,
                    "page_url": page, "license": "Apache-2.0",
                    "license_name_raw": "Apache License, Version 2.0 (OpenSLR SLR93: 'License: Apache License v.2.0')",
                    "attribution": "AISHELL-3, Beijing Shell Shell Technology Co., Ltd. (AISHELL foundation)",
                    "est_dur": c["est_dur"], "bytes": c["size"],
                    "meta_gender": m.get("gender"), "meta_age": m.get("age"),
                    "meta_accent": m.get("accent"), "lang": "zh-CN"})
        if len(idx) % 25 == 0:
            print("fetched", len(idx), "bytes", http.stats["bytes"], flush=True)
    log["http"] = http.stats
    log["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    C.write_json(os.path.join(out_dir, "index.json"), idx)
    C.write_json(os.path.join(C.SELF_DIR, "fetch_stats_%s.json" % SRC), log)
    print(json.dumps({k: log[k] for k in ("speakers_seen", "candidates", "http", "blocked")
                      if k in log}, ensure_ascii=False))


if __name__ == "__main__":
    main()
