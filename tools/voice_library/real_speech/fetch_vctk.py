# -*- coding: utf-8 -*-
"""Source 2: CSTR VCTK Corpus 0.92 (English, 110 speakers, CC BY 4.0).

Official host: University of Edinburgh Datashare, item 10283/3443.  Licence evidence is
captured twice: the item page rights field and the license_text.txt shipped with the
item ("Creative Commons License: Attribution 4.0 International").

The 11.7 GB zip is never downloaded: httpzip reads only the central directory (cached to
_tmp/vctk_cd.json) and then individual members by HTTP Range.  VCTK 0.92 ships
44.1 kHz mono FLAC (silence-trimmed), so clip length is estimated from compressed size
with a median bytes/second ratio learned from the first decodes (recorded in
fetch_stats_vctk.json as an explicit limitation); the real 3.0-10.0 s band is enforced
later by the gate, never by this estimate.
"""
import os, re, sys, json, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C
import httpzip

SRC = "vctk"
ITEM = "https://datashare.ed.ac.uk/handle/10283/3443"
BIT = "https://datashare.ed.ac.uk/server/api/core/bitstreams/"
ZIP_URL = BIT + "535f4286-e54c-4038-838c-a02285e32cb2/content"
LIC_TXT_URL = BIT + "956a1688-0b59-428c-8a2f-10837433dde3/content"
CD_CACHE = os.path.join(C.TMP_DIR, "vctk_cd.json")
MAX_PER_SPEAKER = 5
PICK_PER_SPEAKER = int(os.environ.get("RS_VK_PICK", "8"))
TARGET_SPEAKERS = int(os.environ.get("RS_VK_SPEAKERS", "45"))
EST_SR = 44100
FALLBACK_RATIO = 48500.0        # flac bytes per second of 44.1k mono speech


def learn_ratio():
    """Median flac-bytes/second over already-decoded raw members."""
    d = os.path.join(C.RAW_DIR, SRC)
    vals = []
    if os.path.isdir(d):
        import soundfile as sf
        for n in os.listdir(d):
            if n.endswith(".flac"):
                try:
                    info = sf.info(os.path.join(d, n))
                    if info.duration > 0.4:
                        vals.append(os.path.getsize(os.path.join(d, n)) / info.duration)
                except Exception:
                    pass
    if len(vals) >= 12:
        vals.sort()
        return float(vals[len(vals) // 2]), len(vals)
    return FALLBACK_RATIO, len(vals)


def parse_speaker_info(txt):
    out = {}
    for ln in (txt or "").splitlines():
        f = ln.split()
        if len(f) >= 3 and re.match(r"^P\d{3}$", f[0], re.I):
            age = gender = None
            for tok in f[1:]:
                if age is None and tok.isdigit():
                    age = tok
                elif gender is None and tok.upper() in ("M", "F", "MALE", "FEMALE"):
                    gender = {"M": "male", "F": "female", "MALE": "male", "FEMALE": "female"}[tok.upper()]
            out[f[0].upper()] = {"age": age, "gender": gender, "extra": " ".join(f[1:])}
    return out


def main():
    http = C.Http(SRC, rate=2.0, conc=2)
    out_dir = os.path.join(C.RAW_DIR, SRC)
    os.makedirs(out_dir, exist_ok=True)
    log = {"source": SRC, "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "errors": [],
           "blocked": None, "audio_kind": "FLAC 44.1kHz mono (silence trimmed)"}

    evs = []
    ev_page = C.capture_license(ITEM, "dataset_official_item_page",
                                "Edinburgh Datashare item 10283/3443 rights field")
    if ev_page.get("local_copy") and os.path.exists(ev_page["local_copy"]):
        txt = open(ev_page["local_copy"], encoding="utf-8", errors="replace").read()
        m = re.search(r'(Creative Commons Attribution [0-9.]+ International[^"<]{0,80})', txt)
        ev_page["quote"] = ("item rights field: %s" % m.group(1).strip()) if m else ev_page["quote"]
    evs.append(ev_page)
    raw, err = http.get(LIC_TXT_URL, max_bytes=400000)
    if raw is None:
        evs.append({"license_url": LIC_TXT_URL, "kind": "shipped_license_text",
                    "http_status": None, "quote": "FETCH_ERR %s" % err, "local_copy": None})
    else:
        p = os.path.join(C.LIC_DIR, "vctk_license_text.txt")
        open(p, "wb").write(raw)
        evs.append({"license_url": LIC_TXT_URL, "kind": "shipped_license_text",
                    "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "local_copy": p,
                    "http_status": 200, "sha256": C.hashlib.sha256(raw).hexdigest(),
                    "quote": " ".join(raw.decode("utf-8", "replace").split())[:240]})
    C.write_json(os.path.join(C.SELF_DIR, "license_evidence_%s.json" % SRC), evs)

    try:
        zf, size = httpzip.open_remote_zip(http, ZIP_URL)
    except Exception as e:
        log["fatal"] = "zip_open:%s: %s" % (type(e).__name__, str(e)[:200])
        C.write_json(os.path.join(C.SELF_DIR, "fetch_stats_%s.json" % SRC), log)
        print(json.dumps(log, ensure_ascii=False))
        return
    log["zip_bytes"], log["url"] = size, ZIP_URL

    cd = C.read_json(CD_CACHE)
    if cd is None:
        cd = [{"n": i.filename, "s": i.file_size, "c": i.compress_type, "o": i.header_offset}
              for i in zf.infolist()]
        C.write_json(CD_CACHE, cd)
        log["central_directory"] = "fetched(%d entries)" % len(cd)
    else:
        log["central_directory"] = "cached(%d entries)" % len(cd)

    dem = {}
    for e in cd:
        if e["n"].lower().endswith("speaker-info.txt"):
            try:
                dem = parse_speaker_info(zf.read(e["n"]).decode("utf-8", "replace"))
                log["demographics_file"], log["demographics_n"] = e["n"], len(dem)
                open(os.path.join(C.TMP_DIR, "vctk_speaker_info.txt"), "w",
                     encoding="utf-8").write(zf.read(e["n"]).decode("utf-8", "replace"))
            except Exception as ex:
                log["errors"].append("speaker-info:%s" % str(ex)[:120])
            break

    ratio, nmeas = learn_ratio()
    log["flac_bytes_per_second_used"] = round(ratio, 1)
    log["ratio_measured_from"] = nmeas

    by_spk = {}
    pat = re.compile(r"wav48_silence_trimmed/([a-z]\d{3})/[a-z]\d{3}_(\d{3})_mic(\d)\.flac$", re.I)
    for e in cd:
        n = e["n"]
        if "wav48_silence_trimmed" not in n:
            continue
        m = pat.search(n)
        if not m or m.group(3) != "1":       # mic1 only: never take the same sentence twice
            continue
        dur = e["s"] / ratio
        if not (3.2 <= dur <= 10.2):
            continue
        by_spk.setdefault(m.group(1).upper(), []).append((n, e["s"], dur))
    log["speakers_with_candidates"] = len(by_spk)

    idx, got = [], 0
    for spk in sorted(by_spk):
        if got >= TARGET_SPEAKERS:
            break
        good = sorted(by_spk[spk], key=lambda t: -t[1])
        step = max(1, len(good) // PICK_PER_SPEAKER)
        picked = good[::step][:PICK_PER_SPEAKER]
        made = 0
        for n, fsz, dur in picked:
            utt = re.search(r"_([0-9]{3})_mic\d\.flac$", n).group(1)
            if made >= MAX_PER_SPEAKER + 1:
                break
            dst = os.path.join(out_dir, ("vctk-%s-%s.flac" % (spk.lower(), utt)))
            if not (os.path.exists(dst) and os.path.getsize(dst) == fsz):
                try:
                    data = zf.read(n)
                except Exception as ex:
                    log["errors"].append("member %s: %s" % (n, str(ex)[:110]))
                    continue
                if len(data) != fsz:
                    log["errors"].append("size_mismatch %s" % n)
                    continue
                open(dst, "wb").write(data)
            d = dem.get(spk, {})
            idx.append({"slug": os.path.basename(dst)[:-5], "raw": dst, "speaker": spk,
                        "source_url": ZIP_URL + "#/" + n, "page_url": ITEM,
                        "license": "CC BY 4.0",
                        "license_name_raw": "Creative Commons Attribution 4.0 International Public License",
                        "attribution": "The CSTR VCTK Corpus, CSTR, University of Edinburgh (CC BY 4.0)",
                        "est_dur": round(dur, 2), "bytes": fsz, "container": "flac",
                        "meta_gender": d.get("gender"), "meta_age": d.get("age"),
                        "meta_accent": d.get("extra"), "lang": "en-GB"})
            made += 1
        got += 1
        if got % 10 == 0:
            print("vctk speakers", got, "files", len(idx), "bytes", http.stats["bytes"], flush=True)
    log["candidates"] = len(idx)
    log["http"] = http.stats
    log["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    C.write_json(os.path.join(out_dir, "index.json"), idx)
    C.write_json(os.path.join(C.SELF_DIR, "fetch_stats_%s.json" % SRC), log)
    print(json.dumps({k: log.get(k) for k in ("zip_bytes", "demographics_n", "flac_bytes_per_second_used",
                                              "ratio_measured_from", "speakers_with_candidates",
                                              "candidates", "http", "blocked", "fatal")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
