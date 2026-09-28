"""Format + reachability probe (read-only).

Answers three questions that decide how build_library.py works:
  1. Does the mirror honour HTTP Range requests? (needed for cheap slices)
  2. Which candidate datasets really expose per-speaker metadata we can key on
     (gender / age / emotion / language)?
  3. What is on disk inside them (individual audio files vs parquet vs tar).

Writes logs/probe_report.txt. Downloads metadata and at most a couple of small
row groups - no bulk transfer.
"""

import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
import rs_net as net  # noqa: E402

LOG = Path(__file__).resolve().parent / "logs" / "probe_report.txt"
_out = []
LOG.parent.mkdir(parents=True, exist_ok=True)
LOG.write_text("", encoding="utf-8")


def p(msg):
    msg = str(msg)
    _out.append(msg)
    with open(LOG, "a", encoding="utf-8") as fh:
        fh.write(msg + "\n")
    net.log(msg)


def flush():
    pass


def range_support():
    p("=" * 78)
    p("1. HTTP RANGE SUPPORT")
    url = net.resolve_url("philgzl/vctk", "data/test-mic1-000-000000.parquet")
    try:
        h = requests.head(url, timeout=20, allow_redirects=True,
                          headers={"User-Agent": net.USER_AGENT})
        p("   HEAD %s" % url)
        p("   status=%s accept-ranges=%s length=%s" % (
            h.status_code, h.headers.get("Accept-Ranges"), h.headers.get("Content-Length")))
        r = requests.get(url, headers={"Range": "bytes=0-1023",
                                       "User-Agent": net.USER_AGENT}, timeout=20)
        p("   GET range -> status=%s len=%s  => range_ok=%s" % (
            r.status_code, len(r.content), r.status_code == 206))
    except Exception as exc:  # noqa: BLE001
        p("   FAILED %r" % exc)


PARQUET_CANDS = [
    ("philgzl/vctk", ["data/test-mic1-000-000000.parquet", "data/train-mic1-000-000000.parquet"]),
    ("Milana/vctk_dataset_no_unknown", ["data/train-00000-of-00072.parquet"]),
    ("mythicinfinity/libritts_r", None),
    ("mteb/common_voice_21_0", None),
    ("parler-tts/libritts_r_filtered", None),
]

AUDIO_EXT = (".wav", ".flac", ".mp3", ".ogg", ".opus", ".m4a")


def first_parquet(repo):
    tree = net.dataset_tree(repo) or []
    return tree


def probe_parquet_repo(repo, paths=None):
    p("=" * 78)
    p("2. PARQUET SCHEMA PROBE: %s" % repo)
    try:
        info = net.dataset_info(repo)
    except Exception as exc:  # noqa: BLE001
        p("   info FAIL %r" % exc)
        return
    if not info:
        p("   not reachable / gated")
        return
    card = info.get("cardData") or {}
    lic = card.get("license")
    p("   license=%s gated=%s downloads=%s" % (lic, info.get("gated"), info.get("downloads")))
    tree = net.dataset_tree(repo) or []
    pq = [t for t in tree if t["path"].lower().endswith(".parquet")]
    au = [t for t in tree if t["path"].lower().endswith(AUDIO_EXT)]
    p("   files: parquet=%d audio=%d total=%d" % (len(pq), len(au), len(tree)))
    pq_sorted = sorted(pq, key=lambda t: t.get("size", 0))
    targets = [t for t in pq_sorted[:2]]
    for t in targets:
        url = net.resolve_url(repo, t["path"])
        try:
            table, names, meta = net.read_parquet(url, row_group=0)
            p("   -- %s (%.1f MB, row_groups=%d rows=%d)" % (
                t["path"], t.get("size", 0) / 1e6, meta.num_row_groups, table.num_rows))
            p("      columns=%s" % ", ".join(names))
            row = {c: table.column(c)[0].as_py() for c in names if c != "audio"}
            for k, v in list(row.items())[:14]:
                sv = str(v)
                p("         %-18s %s" % (k, sv[:90]))
            if "audio" in names:
                a = table.column("audio")[0].as_py()
                if isinstance(a, dict):
                    p("         audio: bytes=%s path=%s" % (
                        len(a.get("bytes") or b""), a.get("path")))
            p("      distinct in rowgroup: %s" % _distinct(table, names))
        except Exception as exc:  # noqa: BLE001
            p("   -- %s PARSE FAIL %r" % (t["path"], exc))
    if not pq:
        for t in au[:6]:
            p("   AUDIO %-60s %.2f MB" % (t["path"], t.get("size", 0) / 1e6))


def _distinct(table, names):
    out = []
    for c in ("speaker_id", "gender", "age", "accent", "emotion", "language", "split", "dur"):
        if c in names:
            try:
                vals = table.column(c).to_pylist()
                uniq = sorted({str(v) for v in vals})
                out.append("%s=%d%s" % (c, len(uniq), ("[" + ",".join(uniq[:4]) + "]") if len(uniq) <= 6 else ""))
            except Exception:  # noqa: BLE001
                pass
    return "; ".join(out) or "-"


SEARCHES = [
    ("aishell3", "chinese multi-speaker"),
    ("esd", "emotional speech"),
    ("opens1", "chinese tts"),
    ("thchs", "chinese female"),
    ("cantonese", "cantonese speech"),
    ("emotional", "emotion speech speaker"),
    ("elderly children speech", "age diversity speech"),
    ("primus wenetspeech", "chinese"),
    ("blizzard", "blizzard challenge speech"),
]


def hf_search():
    p("=" * 78)
    p("3. HF SEARCH (mirror) - permissive multi-speaker / emotion / chinese")
    for needles, why in SEARCHES:
        try:
            data = net.api_get("/api/datasets", {
                "search": needles, "sort": "downloads", "direction": -1,
                "limit": 14, "full": "true"})
        except Exception as exc:  # noqa: BLE001
            p("   [%s] FAIL %r" % (needles, exc))
            continue
        p("   --- search=%r for %s" % (needles, why))
        for d in (data or []):
            card = d.get("cardData") or {}
            lic = card.get("license")
            if isinstance(lic, list):
                lic = "+".join(map(str, lic))
            p("      %-46s lic=%-14s gated=%-6s dl=%s" % (
                d["id"][:46], str(lic)[:14], d.get("gated"), d.get("downloads", 0)))


def openslr():
    p("=" * 78)
    p("4. OPENSLR catalogue")
    try:
        r = requests.get("https://www.openslr.org/resources.php", timeout=25,
                         headers={"User-Agent": net.USER_AGENT})
        p("   resources.php -> HTTP %s (%d bytes)" % (r.status_code, len(r.content)))
        html = r.text
        import re
        items = re.findall(r'href="(/(\d+)/)"\s*>OpenSLR\s*:(.{0,120}?)</', html, re.I)
        p("   %d resources listed" % len(items))
        for href, num, title in items:
            title = title.strip()[:90]
            p("     SLR%-4s %s" % (num, title))
    except Exception as exc:  # noqa: BLE001
        p("   FAIL %r" % exc)


def main():
    range_support()
    for repo, paths in PARQUET_CANDS:
        try:
            probe_parquet_repo(repo, paths)
        except Exception as exc:  # noqa: BLE001
            p("%s FATAL %r" % (repo, exc))
    hf_search()
    openslr()
    flush()
    p("bytes transferred: %d" % net.transferred())
    flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
