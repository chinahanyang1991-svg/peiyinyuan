# -*- coding: utf-8 -*-
"""Source 6: Wikimedia Commons - real human speech (read speech / narration).

Per file we read the Commons API extmetadata (LicenseShortName / UsageTerms / Artist) and
run the strict licence gate: CC0, PD and CC BY 3.0/4.0 are accepted; CC BY-SA, GFDL, any
NC/ND clause, missing or unparseable licence -> rejected and recorded.  Duration band is
taken from the API metadata ('length') before downloading, 12 MB hard cap, <=2 concurrent.
Speaker id does not exist on Commons, so the credited author is used as speaker proxy
(this is recorded in the manifest as a limitation).
"""
import os, re, sys, json, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C

API = "https://commons.wikimedia.org/w/api.php"
SRC = "commons"
MAX_FILES = int(os.environ.get("RS_CM_MAX", "80"))
MAX_BYTES = 12 * 1024 * 1024
QUERIES = [
    'filetype:audio intitle:"spoken"',
    'filetype:audio "read by" sentence',
    'filetype:audio "recitation"',
    'filetype:audio "narration" voice',
    'filetype:audio "speech" "press conference"',
    'filetype:audio "Wikinews" article read',
    'filetype:audio "language" "example sentence" recording',
    'filetype:audio "sermon" OR "lecture" excerpt',
]
LIC_URL = {
    "public domain": "https://commons.wikimedia.org/wiki/Commons:Public_domain",
    "pd": "https://commons.wikimedia.org/wiki/Commons:Public_domain",
    "cc0": "https://creativecommons.org/publicdomain/zero/legalcode",
    "cc by 4.0": "https://creativecommons.org/licenses/by/4.0/legalcode",
    "cc by 3.0": "https://creativecommons.org/licenses/by/3.0/legalcode",
    "cc by-sa 4.0": "https://creativecommons.org/licenses/by-sa/4.0/legalcode",
    "cc by-sa 3.0": "https://creativecommons.org/licenses/by-sa/3.0/legalcode",
    "gfdl": "https://www.gnu.org/licenses/fdl-1.3.html",
}
BAD_MIME_HINT = re.compile(r"\b(drone|ambien|bird|wind|music|bell|siren|engine|traffic)\b", re.I)


def api(http, params):
    import urllib.parse
    p = dict(params)
    p.setdefault("format", "json")
    raw, err = http.get(API + "?" + urllib.parse.urlencode(p), max_bytes=4 * 1024 * 1024, timeout=60)
    if raw is None:
        return None, err
    try:
        return json.loads(raw), None
    except Exception as e:
        return None, "bad_json:%s" % e


def meta_len(ii):
    for m in (ii.get("metadata") or []):
        if str(m.get("name", "")).lower() in ("length", "duration"):
            try:
                return float(m.get("value"))
            except Exception:
                pass
    return None


def main():
    http = C.Http(SRC, rate=1.5, conc=2)
    out_dir = os.path.join(C.RAW_DIR, SRC)
    os.makedirs(out_dir, exist_ok=True)
    log = {"source": SRC, "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
           "licence_rejects": {}, "skips": {}, "errors": [], "blocked": None,
           "queries": {}}
    titles = []
    for q in QUERIES:
        d, err = api(http, {"action": "query", "list": "search", "srsearch": q,
                            "srnamespace": "6", "srlimit": "60"})
        if d is None:
            log["errors"].append("search %r: %s" % (q, err))
            if http.stats.get("blocked"):
                log["blocked"] = http.stats["blocked"]
            break
        hits = (d.get("query") or {}).get("search") or []
        log["queries"][q] = {"totalhits": ((d.get("query") or {}).get("searchinfo") or {}).get("totalhits"),
                             "returned": len(hits)}
        titles += [h["title"] for h in hits]
        if len(titles) > 900:
            break
    titles = sorted(set(titles))
    log["titles"] = len(titles)

    # batched imageinfo (50 titles per request)
    pool = []
    for i in range(0, len(titles), 50):
        chunk = titles[i:i + 50]
        d, err = api(http, {"action": "query", "titles": "|".join(chunk), "prop": "imageinfo",
                            "iiprop": "timestamp|url|size|mime|metadata|extmetadata|mediatype",
                            "iiextmetadatafilter": "LicenseShortName|License|Artist|UsageTerms|"
                                                   "AttributionRequired|CopyrightStatus|ImageDescription"})
        if d is None:
            log["errors"].append("imageinfo batch: %s" % err)
            if http.stats.get("blocked"):
                log["blocked"] = http.stats["blocked"]
                break
            continue
        for p in (d.get("query") or {}).get("pages", {}).values():
            ii = (p.get("imageinfo") or [None])[0]
            if not ii or ii.get("mediatype") != "AUDIO":
                continue
            em = ii.get("extmetadata") or {}
            short = (em.get("LicenseShortName") or {}).get("value") or ""
            usage = (em.get("UsageTerms") or {}).get("value") or ""
            artist = re.sub(r"<[^>]+>", "", (em.get("Artist") or {}).get("value") or "").strip()
            dur = meta_len(ii)
            pool.append({"title": p.get("title"), "url": (ii.get("url") or "").split("?")[0],
                         "size": ii.get("size"), "mime": ii.get("mime"), "dur": dur,
                         "lic": short, "usage": usage, "artist": artist,
                         "desc": re.sub(r"<[^>]+>", "", ((em.get("ImageDescription") or {}).get("value") or ""))[:240]})
    log["audio_files_seen"] = len(pool)

    idx, dl = [], 0
    for f in pool:
        if dl >= MAX_FILES:
            break
        if not f["url"] or (f["size"] or 0) > MAX_BYTES or (f["size"] or 0) < 4000:
            log["skips"]["size"] = log["skips"].get("size", 0) + 1
            continue
        if f["dur"] is None or not (3.2 <= f["dur"] <= 10.0):
            log["skips"]["duration_band"] = log["skips"].get("duration_band", 0) + 1
            continue
        if BAD_MIME_HINT.search((f["title"] or "") + " " + (f["desc"] or "")):
            log["skips"]["non_speech_hint"] = log["skips"].get("non_speech_hint", 0) + 1
            continue
        ok, why = C.license_verdict(f["lic"], LIC_URL.get(f["lic"].strip().lower(), ""), f["usage"])
        if not ok:
            log["licence_rejects"]["%s|%s" % (f["lic"][:34] or "<none>", why)] = \
                log["licence_rejects"].get("%s|%s" % (f["lic"][:34] or "<none>", why), 0) + 1
            continue
        slug = "commons-" + C.hashlib.sha1(f["url"].encode()).hexdigest()[:14]
        ext = os.path.splitext(f["url"])[1][:6] or ".ogg"
        dst = os.path.join(out_dir, slug + ext)
        data, err = http.get(f["url"], max_bytes=MAX_BYTES, timeout=90)
        if data is None:
            log["errors"].append("get %s: %s" % (f["url"], err))
            if http.stats.get("blocked"):
                log["blocked"] = http.stats["blocked"]
                break
            continue
        if not os.path.exists(dst):
            open(dst, "wb").write(data)
        page = "https://commons.wikimedia.org/wiki/" + f["title"].replace(" ", "%20")
        ev_path = os.path.join(C.LIC_DIR, slug + ".json")
        ev_obj = {"title": f["title"], "api_url": API, "LicenseShortName": f["lic"],
                  "UsageTerms": f["usage"], "Artist": f["artist"], "description": f["desc"],
                  "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "file_page": page}
        C.write_json(ev_path, ev_obj)
        idx.append({"slug": slug, "raw": dst, "speaker": (f["artist"] or "unknown")[:40],
                    "source_url": f["url"], "page_url": page,
                    "license": f["lic"] or "PD",
                    "license_name_raw": f["lic"],
                    "attribution": "%s / Wikimedia Commons (%s)" % (f["artist"] or "unknown", f["lic"]),
                    "est_dur": f["dur"], "bytes": len(data), "container": ext.lstrip("."),
                    "lang": "unknown", "upstream_title": f["title"],
                    "license_evidence": [{"license_url": LIC_URL.get(f["lic"].strip().lower(), page),
                                          "kind": "commons_extmetadata+file_page",
                                          "captured_at": ev_obj["captured_at"],
                                          "local_copy": ev_path, "http_status": 200,
                                          "sha256": C.hashlib.sha256(
                                              json.dumps(ev_obj, ensure_ascii=False).encode()).hexdigest(),
                                          "quote": "LicenseShortName=%s ; UsageTerms=%s ; artist=%s"
                                                   % (f["lic"], f["usage"], f["artist"])}]})
        dl += 1
    log["candidates"] = len(idx)
    log["http"] = http.stats
    log["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    C.write_json(os.path.join(out_dir, "index.json"), idx)
    C.write_json(os.path.join(C.SELF_DIR, "fetch_stats_%s.json" % SRC), log)
    print(json.dumps({k: log.get(k) for k in ("titles", "audio_files_seen", "candidates",
                                              "licence_rejects", "skips", "http", "blocked")},
                     ensure_ascii=False)[:1600])


if __name__ == "__main__":
    main()
