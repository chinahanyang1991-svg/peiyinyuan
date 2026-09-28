"""Shared network helpers for the resource scout tooling.

Written for a constrained network:
  * endpoints tried in order (hf-mirror.com first, huggingface.co fallback)
  * every request carries a hard (connect, read) timeout and is retried
  * downloads resume via HTTP Range and honour a global byte budget
  * parquet row groups and tar members are read lazily through Range requests,
    so extracting one 10 second clip costs one row group, not one 500 MB shard

This module is standalone: it never imports production packages.
"""

import io
import os
import sys
import time

import requests

HF_ENDPOINTS = ["https://hf-mirror.com", "https://huggingface.co"]
USER_AGENT = "PeiYinYuan-resource-scout/1.0 (local TTS pipeline)"
DEFAULT_TIMEOUT = (10, 90)            # (connect, read) seconds
BYTE_BUDGET = int(2.6 * (1 << 30))    # 3 GB cap, kept slightly below
POLITE_SLEEP = 0.15                   # seconds between requests to one host

AUDIO_EXT = (".wav", ".flac", ".mp3", ".ogg", ".opus", ".m4a", ".w64", ".aiff")

_transferred = 0
_liveness_cache = {}
_session = None


class BudgetExceeded(RuntimeError):
    pass


def session():
    global _session
    if _session is None:
        _session = requests.Session()
        _session.headers.update({"User-Agent": USER_AGENT})
    return _session


def set_budget(byte_budget):
    global BYTE_BUDGET
    BYTE_BUDGET = int(byte_budget)


def transferred():
    return _transferred


def add_transferred(n):
    global _transferred
    _transferred += n
    if _transferred > BYTE_BUDGET:
        raise BudgetExceeded("download budget %.2f GiB exceeded"
                             % (BYTE_BUDGET / float(1 << 30)))


def log(msg):
    """ASCII/Chinese only, flushed - this terminal is GBK."""
    sys.stdout.write("%s\n" % msg)
    sys.stdout.flush()


def _fmt(n):
    for unit, div in (("G", 1 << 30), ("M", 1 << 20), ("K", 1 << 10)):
        if n >= div:
            return "%.1f%s" % (n / float(div), unit)
    return "%dB" % n


# --------------------------------------------------------------------------- api
def api_get(path, params=None, timeout=25, retries=2):
    """GET a JSON API resource, trying every endpoint. None means 404/401."""
    last = None
    for attempt in range(retries + 1):
        for base in HF_ENDPOINTS:
            try:
                r = session().get(base + path, params=params, timeout=timeout)
                if r.status_code == 200:
                    try:
                        return r.json()
                    except Exception:  # noqa: BLE001
                        return None
                last = "HTTP %s" % r.status_code
                if r.status_code in (401, 403, 404):
                    return None
            except Exception as exc:  # noqa: BLE001
                last = "%s" % type(exc).__name__
        time.sleep(min(1.5 ** attempt, 6))
    raise RuntimeError("api_get failed %s (%s)" % (path, last))


def _next_link(headers):
    """Cursor to the next page of a hub listing, or None.

    The hub truncates every listing at 1000 entries and only advertises the
    continuation in the RFC 5988 `Link` header, so a single GET silently hides
    most of a large repository.
    """
    import re
    for part in (headers.get("Link") or "").split(","):
        m = re.search(r'<([^>]+)>;\s*rel="\s*next\s*"', part)
        if m:
            return m.group(1)
    return None


def list_tree(path_or_url, params=None, timeout=30, retries=2, max_pages=30):
    """GET a hub listing and follow every cursor page. Returns a list of entries."""
    urls = [path_or_url] if path_or_url.startswith("http") \
        else [base + path_or_url for base in HF_ENDPOINTS]
    last = None
    out = []
    for page in range(max_pages):
        got = False
        for url in list(urls):
            for attempt in range(retries + 1):
                try:
                    r = session().get(url, params=params if page == 0 else None,
                                      timeout=timeout)
                    if r.status_code == 200:
                        try:
                            payload = r.json()
                        except Exception:  # noqa: BLE001
                            payload = None
                        if not isinstance(payload, list):
                            # A bad tree path answers with {"error": "..."}; extending
                            # that dict would hand callers fake one-key "entries".
                            last = "hub said %s" % (str(payload)[:160])
                            return out or None
                        out.extend(payload)
                        nxt = _next_link(r.headers)
                        if not nxt:
                            return out
                        urls = [nxt]              # stay on the endpoint that works
                        got = True
                        break
                    last = "HTTP %s" % r.status_code
                    if r.status_code in (401, 403, 404):
                        return out or None
                except Exception as exc:  # noqa: BLE001
                    last = "%s" % type(exc).__name__
                time.sleep(min(1.5 ** attempt, 6))
        if not got:
            break
    if not out:
        raise RuntimeError("list_tree failed %s (%s)" % (path_or_url, last))
    return out


def dataset_info(repo):
    return api_get("/api/datasets/" + repo, {"full": "true"})


def dataset_tree(repo, path="", recursive=True):
    """File listing of a dataset repo (or one sub-directory), fully paginated."""
    return list_tree("/api/datasets/%s/tree/main/%s" % (repo, path),
                     {"recursive": "true"} if recursive else None)


def working_endpoint(repo):
    key = "ep:%s" % repo
    if key in _liveness_cache:
        return _liveness_cache[key]
    ep = None
    for base in HF_ENDPOINTS:
        try:
            r = session().get("%s/api/datasets/%s" % (base, repo), timeout=20)
            if r.status_code == 200:
                ep = base
                break
        except Exception:  # noqa: BLE001
            continue
    _liveness_cache[key] = ep
    return ep


def _repo_href(repo, repo_type):
    """Prefix a repo id the way the hub expects.

    Datasets live under /datasets/<repo>, models under /models/<repo>; using the
    wrong (or missing) prefix answers 404, so every harvested object here is a
    dataset and the default reflects that.
    """
    repo = str(repo).strip().lstrip("/")
    if repo.startswith(("datasets/", "models/")):   # already prefixed by caller
        return repo
    kind = (repo_type or "dataset").lower()
    return ("datasets/" if kind.startswith("data") else "models/") + repo


def resolve_url(repo, path, revision="main", repo_type="dataset"):
    base = working_endpoint(repo) or HF_ENDPOINTS[0]
    return "%s/%s/resolve/%s/%s" % (base, _repo_href(repo, repo_type), revision, path)


def fetch_bytes(url, max_bytes=None, timeout=DEFAULT_TIMEOUT, retries=3):
    """Small object into memory (single clip). Returns (bytes, ok)."""
    last = None
    for attempt in range(retries):
        try:
            r = session().get(url, timeout=timeout, stream=True)
            if r.status_code == 429:
                wait = int(r.headers.get("Retry-After") or 5)
                time.sleep(min(wait, 20))
                last = "429"
                continue
            if r.status_code >= 400:
                r.close()
                raise RuntimeError("HTTP %s" % r.status_code)
            clen = int(r.headers.get("Content-Length") or 0)
            if max_bytes and clen and clen > max_bytes:
                r.close()
                raise RuntimeError("object too large %s > cap %s"
                                   % (_fmt(clen), _fmt(max_bytes)))
            buf = io.BytesIO()
            total = 0
            for chunk in r.iter_content(chunk_size=1 << 16):
                if not chunk:
                    continue
                buf.write(chunk)
                total += len(chunk)
                add_transferred(len(chunk))
                if max_bytes and total > max_bytes:
                    r.close()
                    raise RuntimeError("stream exceeded cap %s" % _fmt(max_bytes))
                if clen and total >= clen:
                    break
            r.close()
            time.sleep(POLITE_SLEEP)
            return buf.getvalue(), clen
        except BudgetExceeded:
            raise
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(min(1.6 ** attempt, 8))
    raise RuntimeError("fetch failed %s (%s)" % (url, last))


def download(url, dest, resume=True, max_bytes=None, retries=3):
    """GET url -> file with resume support. Returns bytes on disk."""
    d = os.path.dirname(os.path.abspath(dest))
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = dest + ".part"
    have = os.path.getsize(tmp) if (resume and os.path.exists(tmp)) else 0
    last = None
    for attempt in range(retries):
        try:
            headers = {"Range": "bytes=%d-" % have} if have else {}
            with session().get(url, headers=headers, timeout=DEFAULT_TIMEOUT,
                               stream=True) as r:
                if r.status_code == 416 and have:
                    if os.path.exists(tmp):
                        os.replace(tmp, dest)
                    return have
                if r.status_code >= 400:
                    raise RuntimeError("HTTP %s" % r.status_code)
                mode = "ab" if (have and r.status_code == 206) else "wb"
                if mode == "wb":
                    have = 0
                clen = int(r.headers.get("Content-Length") or 0)
                total = have + clen if mode == "ab" else clen
                if max_bytes and total and total > max_bytes:
                    raise RuntimeError("object too large %s > cap %s"
                                       % (_fmt(total), _fmt(max_bytes)))
                written = have
                with open(tmp, mode) as fh:
                    for chunk in r.iter_content(chunk_size=1 << 16):
                        if not chunk:
                            continue
                        fh.write(chunk)
                        written += len(chunk)
                        add_transferred(len(chunk))
                        if total and written >= total:
                            break
            if os.path.exists(tmp):
                os.replace(tmp, dest)
            time.sleep(POLITE_SLEEP)
            return written
        except BudgetExceeded:
            raise
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(min(1.6 ** attempt, 8))
    raise RuntimeError("download failed %s (%s)" % (url, last))


# ------------------------------------------------------------------- range file
class RangeReader(io.RawIOBase):
    """Seekable read-only view of an HTTP object, backed by Range requests.

    pyarrow accepts this, so one row group can be pulled from a large shard
    without transferring the file.
    """

    def __init__(self, url, cache_block=1 << 20):
        self.url = url
        self.cache_block = int(cache_block)
        self._size = None
        self._pos = 0
        self._buf = b""
        self._buf_start = 0

    def length(self):
        if self._size is None:
            r = session().head(self.url, timeout=DEFAULT_TIMEOUT,
                               allow_redirects=True)
            if r.status_code >= 400:
                raise RuntimeError("HEAD %s -> HTTP %s" % (self.url, r.status_code))
            self._size = int(r.headers.get("Content-Length") or 0)
        return self._size

    def seekable(self):
        return True

    def readable(self):
        return True

    def tell(self):
        return self._pos

    def seek(self, offset, whence=0):
        if whence == 1:
            offset += self._pos
        elif whence == 2:
            offset += self.length()
        self._pos = max(0, int(offset))
        return self._pos

    def _fetch(self, start, end):
        r = session().get(self.url, headers={"Range": "bytes=%d-%d" % (start, end)},
                          timeout=DEFAULT_TIMEOUT, stream=True)
        if r.status_code not in (200, 206):
            r.close()
            raise RuntimeError("range %s -> HTTP %s" % (self.url, r.status_code))
        data = r.content
        r.close()
        add_transferred(len(data))
        if r.status_code == 200 and start:
            data = data[start:end + 1]
        return data

    def read(self, size=-1):
        total = self.length()
        if size is None or size < 0:
            size = total - self._pos
        if size <= 0 or self._pos >= total:
            return b""
        chunks = []
        got = 0
        while got < size and self._pos < total:
            want = min(size - got, self.cache_block, total - self._pos)
            start = self._pos
            if self._buf and self._buf_start <= start < self._buf_start + len(self._buf):
                off = start - self._buf_start
                data = self._buf[off:off + want]
                if not data:                           # window exhausted - refetch
                    self._buf, self._buf_start = b"", 0
                    continue
            else:
                self._buf = self._fetch(start, min(total - 1, start + want - 1))
                self._buf_start = start
                data = self._buf[:want]
            if not data:
                break                                  # true EOF / dead server
            chunks.append(data)
            got += len(data)
            self._pos += len(data)
        return b"".join(chunks)

    def readinto(self, b):
        data = self.read(len(b))
        b[:len(data)] = data
        return len(data)


def row_group_bytes(rg) -> int:
    """Compressed on-disk size of one parquet row group.

    pyarrow >= 15 dropped `total_compressed_size` from RowGroupMetaData (it only
    lives on the column chunks), so summing is the portable way to budget a
    Range read.
    """
    try:
        return sum(int(rg.column(i).total_compressed_size or 0)
                   for i in range(rg.num_columns)) or int(rg.total_byte_size)
    except Exception:  # noqa: BLE001
        return int(rg.total_byte_size)


def open_parquet(url, cache_block=4 << 20):
    """Return (pyarrow ParquetFile, RangeReader) - caller must close the reader."""
    import pyarrow.parquet as pq
    rr = RangeReader(url, cache_block=cache_block)
    return pq.ParquetFile(rr), rr


def read_parquet_row_group(url, row_group=0, columns=None, cache_block=4 << 20):
    import pyarrow.parquet as pq
    rr = RangeReader(url, cache_block=cache_block)
    try:
        pf = pq.ParquetFile(rr)
        names = pf.schema_arrow.names
        want = [c for c in (columns or names) if c in names]
        rg = min(int(row_group), pf.metadata.num_row_groups - 1)
        return pf.read_row_group(rg, columns=want), names
    finally:
        rr.close()


def tar_list(url, limit=5000, stop_after_bytes=None):
    """Index an uncompressed remote tar via Range requests.

    Yields (name, data_offset, size). Only 512-byte headers are fetched per
    entry, but each step needs one round trip, so stop_after_bytes caps how far
    into the archive we are willing to walk.
    """
    rr = RangeReader(url, cache_block=4 << 20)
    total = rr.length()
    off = walked = 0
    n = 0
    try:
        while off + 512 <= total and n < limit:
            rr.seek(off)
            hdr = rr.read(512)
            if len(hdr) < 512 or not hdr[:100].strip(b"\0"):
                break
            name = hdr[0:100].split(b"\0")[0].decode("utf-8", "replace")
            try:
                size = int((hdr[124:136].split(b"\0")[0] or b"0").decode("ascii").strip(), 8)
            except Exception:  # noqa: BLE001
                break
            typ = hdr[156:157]
            pad = ((size + 511) // 512) * 512
            if typ in (b"0", b"\0") and name.lower().endswith(AUDIO_EXT):
                yield name, off + 512, size
                n += 1
            off += 512 + pad
            walked += 512
            if stop_after_bytes and off > stop_after_bytes:
                break
    finally:
        rr.close()
