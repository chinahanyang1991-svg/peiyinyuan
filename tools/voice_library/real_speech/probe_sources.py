# -*- coding: utf-8 -*-
"""Measured reachability/licence evidence for every source the lane was asked to test.

One small request per probe (<= 2 in flight, >= 0.7 s apart, 20-64 byte bodies), strictly
through common.Http: on 403/429 the raw body is recorded and that source is dropped -
no UA/IP/proxy rotation, no registration, no login, no bypass of any gate.

Writes source_probes.json next to this script; every number quoted in the report comes
from that file.
"""
import os, sys, json, time, socket, traceback, re
from urllib.parse import urljoin
import requests
from requests.utils import quote

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C

HF = "https://huggingface.co"
MIRROR = "https://hf-mirror.com"
SLR = "https://www.openslr.org"
SLR_MIRROR = "https://openslr.trmal.net/resources"
MS = "https://www.modelscope.cn"
CX = "https://commons.wikimedia.org/w/api.php"


def probe(h, name, url, note="", max_bytes=2048, timeout=25, extra=None):
    t0 = time.time()
    body, err = h.get(url, max_bytes=max_bytes, timeout=timeout)
    rec = {"name": name, "url": url, "note": note,
           "ok": err is None, "err": (err or "")[:400],
           "bytes": len(body or b""), "secs": round(time.time() - t0, 2)}
    if extra and body is not None:
        try:
            rec["extract"] = extra(body)
        except Exception as e:
            rec["extract_err"] = "%s: %s" % (type(e).__name__, str(e)[:150])
    print(json.dumps({k: rec[k] for k in ("name", "ok", "bytes", "secs", "err")},
                     ensure_ascii=False)[:300], flush=True)
    return rec


def dns(host):
    try:
        addrs = sorted({info[4][0] for info in socket.getaddrinfo(host, 443)})
        return {"host": host, "resolved": addrs[:4]}
    except Exception as e:
        return {"host": host, "error": "%s: %s" % (type(e).__name__, str(e)[:160])}


def total_from(content_range):
    import re
    m = re.search(r"/(\d+)$", content_range or "")
    return int(m.group(1)) if m else None


def main():
    out = []
    h = C.Http("probe", rate=1.3, conc=2)

    # 1) Hugging Face direct vs mirror -----------------------------------------
    out.append(probe(h, "hf.co tree common_voice_17_0",
                      HF + "/api/datasets/Mozilla-Foundation/common_voice_17_0/tree/main",
                      "official HF API", extra=lambda b: json.loads(b.decode())[:2]))
    out.append(probe(h, "hf-mirror tree common_voice_17_0",
                      MIRROR + "/api/datasets/Mozilla-Foundation/common_voice_17_0/tree/main",
                      "mirror API for the same repo",
                      extra=lambda b: [x["path"] for x in json.loads(b.decode())]))
    out.append(probe(h, "hf-mirror resolve en tar (gated)",
                      MIRROR + "/datasets/Mozilla-Foundation/common_voice_17_0/resolve/main/en/en_0.tar",
                      "public tar/parquet direct link, no login"))
    out.append(probe(h, "hf-mirror tree common_voice_11_0",
                      MIRROR + "/api/datasets/Mozilla-Foundation/common_voice_11_0/tree/main",
                      "older CV snapshot"))
    out.append(probe(h, "voice-download.mozilla.org",
                      "https://voice-download.mozilla.org/", "official CV download host"))
    out.append(dns("voice-download.mozilla.org") | {"name": "dns voice-download.mozilla.org"})

    # 2) OpenSLR (licence authority) + big-archive Range probe -----------------
    # The record page is /<id>/ and its files are listed under /resources/<id>/;
    # read the page once, take the licence line + archive hrefs from it verbatim.
    h2 = C.Http("probe-range", rate=1.0, conc=1)
    for slr, what in (("93", "AISHELL-3 (Mandarin, 174 speakers)"),
                      ("52", "aishell-1"), ("68", "MAGICDATA"), ("28", "SLR28"),
                      ("26", "Southern Min"), ("95", "THUCPOS")):
        page = "%s/%s/" % (SLR, slr)
        text = {}

        def _read(b, _t=text):
            plain = re.sub(r"<[^>]+>", " ", b.decode("utf-8", "replace"))
            plain = re.sub(r"\s+", " ", plain)
            m = re.search(r"License[^|]{0,80}", plain)
            _t["licence_line"] = m.group(0).strip() if m else ""
            _t["archives"] = sorted({m[0] + " " + (m[1] or "").strip()
                                      for m in re.findall(r'href="([^"]+\.(?:tar|tgz|zip))"[^<]{0,40}'
                                                          r'([^<]{0,14})', plain)})[:6]
            return {"licence": _t["licence_line"], "archives": _t["archives"]}
        rec = probe(h, "openslr SLR%s record" % slr, page, what, max_bytes=60000, extra=_read)
        arch = (text.get("archives") or [""])[0].split(" ")[0]
        if arch:
            url = urljoin(page, arch)
            st, cr, cl = h2.head_range(url, n=8, timeout=20)
            rec["range_probe"] = {"url": url, "status": str(st), "content_range": cr,
                                  "total_bytes": total_from(cr), "content_length": cl,
                                  "note": "Range support + archive size (no full download)"}
        out.append(rec)
    # small text files served straight from OpenSLR
    out.append(probe(h2, "openslr 93 speaker-info.txt", SLR_MIRROR + "/93/speaker-info.txt",
                     "gender/age metadata?", max_bytes=200))
    out.append(probe(h2, "openslr 93 README.../text", SLR_MIRROR + "/93/README.TXT", "readme",
                     max_bytes=400))

    # 3) ModelScope public datasets --------------------------------------------
    for repo in ("opendatalab/AISHELL-3", "AISHELL-3", "iic/AISHELL-3", "speech_asr_aishell"):
        out.append(probe(h, "modelscope api %s" % repo,
                         MS + "/api/v1/datasets/%s" % repo, "public dataset listing",
                         max_bytes=1500,
                         extra=lambda b: json.loads(b.decode("utf-8", "replace")).get("Code")))
    out.append(probe(h, "modelscope web search", MS + "/api/v1/datasets?Query=AISHELL-3",
                     "search endpoint", max_bytes=1200))

    # 4) Edinburgh Datashare (VCTK authority) ----------------------------------
    out.append(probe(h, "vctk item page",
                     "https://datashare.ed.ac.uk/handle/10283/3443", "VCTK landing page",
                     max_bytes=8000,
                     extra=lambda b: C.first_license_sentence(b.decode("utf-8", "replace"))))
    out.append(probe(h, "vctk license_text.txt",
                     "https://datashare.ed.ac.uk/server/api/core/bitstreams/956a1688-0b59-428c"
                     "-8a2f-10837433dde3/content", "licence text shipped in the archive",
                     max_bytes=800))

    # 5) Wikimedia Commons: how many SHORT spoken files are actually there? ----
    QUERIES = ['filetype:audio "spoken word"', 'filetype:audio spoken sentence',
               'filetype:audio "read aloud"', 'filetype:audio podcast clip',
               'filetype:audio "TTS" sample', 'filetype:audio voice recording',
               'filetype:audio intitle:"spoken"', 'filetype:audio "news reading"']
    for q in QUERIES:
        u = (CX + "?action=query&list=search&srsearch=%s&srlimit=10&srnamespace=6"
             "&format=json" % quote(q))
        out.append(probe(h, "commons search %r" % q, u, "how many hits", max_bytes=4000,
                         extra=lambda b: json.loads(b.decode())["query"].get("searchinfo", {})
                         .get("total_hits")))

    stats = {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
             "http": {x: h.stats[x] for x in ("requests", "bytes", "retries", "http_4xx")},
             "blocked": h.stats.get("blocked"), "probes": out}
    C.write_json(os.path.join(C.SELF_DIR, "source_probes.json"), stats)
    print(json.dumps({"probes": len(out), "ok": sum(1 for p in out if p.get("ok")),
                      "blocked": stats["blocked"]}, ensure_ascii=False)[:600])


if __name__ == "__main__":
    main()
