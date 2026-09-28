# -*- coding: utf-8 -*-
"""Real-speech lane: shared helpers (paths, license gate, HTTP politeness, audio pipeline).

Scope: only *commercially usable* open corpora (Apache-2.0 / CC0 / PD / CC BY 3.0-4.0 /
MIT / BSD).  Every candidate must carry machine-checkable license evidence captured at
fetch time (raw text + url + timestamp + sha256 of the raw text).

This module is private to real_speech/.  It only READS sibling lanes (refill/gate.py
logic is copied, never imported-and-modified) and never writes outside:
  tools/voice_library/real_speech/   (code + manifests + reports)
  data/voices/library/real_speech/   (48k/mono/PCM_16 wav output)
"""
import os, re, json, time, hashlib, threading, io
import numpy as np
import soundfile as sf

SELF_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(SELF_DIR, "..", "..", ".."))
OUT_DIR = os.path.join(ROOT, "data", "voices", "library", "real_speech")
TMP_DIR = os.path.join(SELF_DIR, "_tmp")
RAW_DIR = os.path.join(TMP_DIR, "raw")
LIC_DIR = os.path.join(TMP_DIR, "license")
TARGET_SR = 48000
UA = "peiyinyuan-voice-library/1.0 (open-corpus harvest; licensing check only; contact: local)"

for d in (OUT_DIR, TMP_DIR, RAW_DIR, LIC_DIR):
    os.makedirs(d, exist_ok=True)

# --------------------------------------------------------------- license gate
REJECT_PAT = re.compile(
    r"(by[- ]?nc|nc[- ]?nd|\bnc\b|non[- ]?commercial|noncommercial|"
    r"by[- ]?sa|share[- ]?alike|\bsa\b|gfdl|attribution[- ]?[- ]?share|"
    r"research[- ]?only|for[- ]?research|academic[- ]?use|educational[- ]?use only|"
    r"all rights reserved|no license|unknown license|needs? permission|separate permission|"
    r"not[- ]?for[- ]?(commercial|distribution))", re.I)
ALLOW_PAT = re.compile(
    r"^(cc0|cc[- ]?0|zero|public domain|pd[- ]?[a-z0-9]*|"
    r"cc[- ]?by(?:[- ]?(?:intl|4\.0|3\.0))?(?:\.[0-9])?|"
    r"creative commons attribution (?:international )?(?:version )?(?:4\.0|3\.0)|"
    r"attribution 4\.0|attribution 3\.0|attribution-4\.0|attribution-3\.0|"
    r"apache[- ]?(?:license[, ]+)?(?:version +)?2(?:\.0)?|"
    r"mit|bsd(?:[- ]?(?:2|3|clause|license))?|"
    r"uasl|universal source|unlicense|isc|"
    r"cc[- ]?by[- ]?4\.0|cc[- ]?by[- ]?3\.0)", re.I)


def license_verdict(name, url, brief=""):
    """Return (ok: bool, reason: str).  Hard gate, per project ruling 2026-09-22:
    NC / SA / GFDL / research-only / missing licence / unknown -> reject."""
    name = (name or "").strip()
    if not name:
        return False, "license_missing"
    if not url:
        return False, "license_url_missing"
    blob = "%s %s" % (name, brief or "")
    m = REJECT_PAT.search(blob)
    if m:
        return False, "rejected_clause:%s" % m.group(0).strip().lower()
    if not ALLOW_PAT.match(name):
        # brief text may still carry the canonical name (e.g. Commons "CC BY 4.0")
        if not ALLOW_PAT.search(blob):
            return False, "license_not_in_allowlist:%s" % name[:60]
    return True, "ok"


def capture_license(url, kind, note=""):
    """GET a licence page/asset, store raw bytes locally, return evidence dict.
    Evidence must be re-checkable offline: file + sha256 + captured_at + key sentence."""
    import requests
    fn = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16] + ".txt"
    path = os.path.join(LIC_DIR, fn)
    ev = {"license_url": url, "kind": kind, "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
          "local_copy": path, "http_status": None, "sha256": None, "quote": note,
          "note": note}
    if os.path.exists(path):
        raw = open(path, "rb").read()
        ev.update(http_status=200, sha256=hashlib.sha256(raw).hexdigest(), cached=True)
        return ev
    try:
        r = requests.get(url, timeout=45, headers={"User-Agent": UA})
        ev["http_status"] = r.status_code
        if r.status_code != 200:
            ev["sha256"] = None
            ev["quote"] = "FETCH_FAIL http=%s" % r.status_code
            return ev
        txt = r.text
        open(path, "w", encoding="utf-8").write(txt)
        ev["sha256"] = hashlib.sha256(txt.encode("utf-8")).hexdigest()
        if not note:
            ev["quote"] = first_license_sentence(txt)
    except Exception as e:
        ev["quote"] = "FETCH_ERR %s: %s" % (type(e).__name__, str(e)[:120])
    return ev


def first_license_sentence(txt, keys=("license", "creative commons", "apache", "cc0",
                                      "public domain", "attribution", "commercial")):
    plain = re.sub(r"<[^>]+>", " ", txt or "")
    plain = re.sub(r"\s+", " ", plain)
    best = ""
    for sent in re.split(r"(?<=[.;])\s+", plain):
        s = sent.strip()
        if 20 <= len(s) <= 420 and any(k in s.lower() for k in keys):
            if len(s) > len(best):
                best = s
            if len(best) > 90:
                break
    return best[:400]


# ------------------------------------------------------- polite HTTP + throttle
class Bucket:
    """Per-source token throttle: <=rate req/s and <=CONC in flight."""
    def __init__(self, rate=2.0, conc=2):
        self.interval = 1.0 / float(rate)
        self.cv = threading.Condition()
        self.n = 0
        self.cap = conc
        self.last = 0.0

    def __enter__(self):
        with self.cv:
            while self.n >= self.cap:
                self.cv.wait(0.2)
            self.n += 1
        while True:
            with self.cv:
                wait = self.interval - (time.time() - self.last)
                if wait <= 0:
                    self.last = time.time()
                    break
            time.sleep(max(0.0, wait))
        return self

    def __exit__(self, *a):
        with self.cv:
            self.n -= 1
            self.cv.notify_all()


class Http:
    """requests session with throttle, retry+backoff and a hard stop on 403/429.

    Per the lane rules we NEVER rotate UA / IP / proxy to evade a block: on 403/429 we
    record the raw response and stop the source."""

    def __init__(self, name, rate=1.5, conc=2):
        import requests
        self.name = name
        self.b = Bucket(rate, conc)
        self.s = requests.Session()
        self.s.headers["User-Agent"] = UA
        self.stats = {"requests": 0, "bytes": 0, "retries": 0, "http_4xx": {}, "blocked": None}
        self.lock = threading.Lock()

    def get(self, url, max_bytes=None, timeout=60, headers=None, tries=3):
        import requests
        last = None
        for i in range(tries):
            with self.b:
                try:
                    r = self.s.get(url, timeout=timeout, stream=True, headers=headers or {})
                except Exception as e:
                    last = "%s: %s" % (type(e).__name__, str(e)[:160])
                    self._bump("exc")
                    time.sleep(1.5 * (i + 1) ** 2)
                    continue
                with r:
                    with self.lock:
                        self.stats["requests"] += 1
                    if r.status_code in (403, 429):
                        body = r.text[:300]
                        self._bump(str(r.status_code))
                        self.stats["blocked"] = {"url": url, "status": r.status_code, "body": body}
                        return None, "HTTP_%s %s" % (r.status_code, body.replace("\n", " ")[:200])
                    if r.status_code >= 400:
                        self._bump(str(r.status_code))
                        last = "HTTP_%s %s" % (r.status_code, r.text[:160].replace("\n", " "))
                        if r.status_code < 500:
                            return None, last
                        time.sleep(1.5 * (i + 1) ** 2)
                        continue
                    data = r.content if max_bytes is None else r.raw.read(max_bytes, decode_content=True)
                    with self.lock:
                        self.stats["bytes"] += len(data or b"")
                    return data, None
        return None, last or "unreachable"

    def get_page(self, url, timeout=60):
        """GET returning (body, next_url, err); follows Link rel="next" pagination."""
        with self.b:
            try:
                r = self.s.get(url, timeout=timeout, headers={"Accept": "application/json"})
            except Exception as e:
                return None, None, "%s: %s" % (type(e).__name__, str(e)[:160])
            with self.lock:
                self.stats["requests"] += 1
                self.stats["bytes"] += len(r.content)
            if r.status_code in (403, 429):
                self._bump(str(r.status_code))
                self.stats["blocked"] = {"url": url, "status": r.status_code, "body": r.text[:300]}
                return None, None, "HTTP_%s %s" % (r.status_code, r.text[:200].replace("\n", " "))
            if r.status_code >= 400:
                self._bump(str(r.status_code))
                return None, None, "HTTP_%s" % r.status_code
            nxt = (r.links or {}).get("next", {}).get("url")
            return r.content, nxt, None

    def get_range(self, url, start, end, timeout=120):
        """Strict Range GET: must come back 206 with Content-Range starting at `start`.

        Returns (bytes, None) or (None, error).  Never falls back to a full-object GET,
        so a server that ignores Range cannot make us download a multi-GB archive.
        """
        import requests
        for i in range(3):
            with self.b:
                try:
                    r = self.s.get(url, timeout=timeout, stream=True,
                                   headers={"Range": "bytes=%d-%d" % (start, end)})
                except Exception as e:
                    time.sleep(1.5 * (i + 1) ** 2)
                    continue
                with r:
                    with self.lock:
                        self.stats["requests"] += 1
                    if r.status_code in (403, 429):
                        self._bump(str(r.status_code))
                        self.stats["blocked"] = {"url": url, "status": r.status_code,
                                                 "body": r.text[:300]}
                        return None, "HTTP_%s blocked" % r.status_code
                    if r.status_code == 206:
                        cr = r.headers.get("Content-Range", "")
                        m = re.match(r"bytes\s+(\d+)-", cr)
                        if not m or int(m.group(1)) != start:
                            return None, "bad content-range:%s" % cr[:60]
                        data = r.content
                        with self.lock:
                            self.stats["bytes"] += len(data)
                        return data, None
                    if r.status_code == 200:
                        return None, "server_ignores_range(http200)"
                    self._bump(str(r.status_code))
                    time.sleep(1.5 * (i + 1) ** 2)
        return None, "range_unreachable"

    def head_range(self, url, n=64, timeout=30):
        """Return (status, content_range, length) - used to prove Range/random access."""
        import requests
        try:
            r = requests.get(url, timeout=timeout, headers=dict(self.s.headers, Range="bytes=0-%d" % (n - 1)), stream=True)
            out = (r.status_code, r.headers.get("Content-Range"), r.headers.get("Content-Length"))
            r.close()
            return out
        except Exception as e:
            return ("ERR", "%s: %s" % (type(e).__name__, str(e)[:120]), None)

    def _bump(self, k):
        with self.lock:
            self.stats["http_4xx"][k] = self.stats["http_4xx"].get(k, 0) + 1


# ------------------------------------------------------------------ audio ops
def load_any(path_or_bytes):
    if isinstance(path_or_bytes, (bytes, bytearray)):
        x, sr = sf.read(io.BytesIO(bytes(path_or_bytes)), always_2d=False, dtype="float32")
    else:
        x, sr = sf.read(path_or_bytes, always_2d=False, dtype="float32")
    if x.ndim > 1:
        x = x.mean(axis=1)
    return np.asarray(x, dtype=np.float32), sr


def to_target_sr(x, sr):
    if sr == TARGET_SR:
        return x.astype(np.float32), sr
    import librosa
    return librosa.resample(x.astype(np.float32), orig_sr=sr, target_sr=TARGET_SR,
                            res_type="soxr_hq").astype(np.float32), TARGET_SR


def trim_edges(x, sr, pad_ms=120, rel=0.02):
    peak = float(np.abs(x).max()) or 1.0
    idx = np.where(np.abs(x) > rel * peak)[0]
    if len(idx) == 0:
        return x
    p = int(sr * pad_ms / 1000.0)
    a, b = max(0, idx[0] - p), min(len(x), idx[-1] + 1 + p)
    return x[a:b]


def gain_policy(peak):
    """refill-compatible peak policy: <0.5 -> 0.70 ; >0.99 -> 0.90 (headroom); else 1.0."""
    if peak <= 0.0:
        return None
    if peak < 0.5:
        return 0.70 / peak
    if peak > 0.99:
        return 0.90 / peak
    return 1.0


def save_pcm16(x, sr, path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    xi = np.clip(np.round(np.asarray(x, dtype=np.float64) * 32767.0), -32768, 32767).astype("<i2")
    sf.write(path, xi, sr, subtype="PCM_16", format="WAV")
    return path


def write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)


def read_json(path, default=None):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def est_wav_dur(nbytes, sr=44100, ch=1, width=2):
    return max(0.0, (nbytes - 44) / float(sr * ch * width))
