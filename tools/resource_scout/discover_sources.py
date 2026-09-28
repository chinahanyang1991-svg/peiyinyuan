"""Source discovery for the voice resource library (read-only metadata probe).

For each candidate HuggingFace dataset this prints the declared license, gating
flag and the real files in the repo with their sizes, so that build_library.py
only downloads permissively licensed, small, reachable audio.

huggingface.co is unreachable from this network, so requests go to the official
mirror hf-mirror.com first and fall back to huggingface.co.

Usage:
    python discover_sources.py                  # probe built-in candidate list
    python discover_sources.py <repo_id> ...    # probe specific repos
"""

import sys
from pathlib import Path

import requests

ENDPOINTS = ["https://hf-mirror.com", "https://huggingface.co"]
LOG_DIR = Path(__file__).resolve().parent / "logs"
AUDIO_EXT = (".wav", ".flac", ".mp3", ".ogg", ".opus", ".m4a", ".sph", ".w64")

CANDIDATES = [
    # English multi-speaker, gender+age metadata, CC-BY-4.0
    "CSTR-Edinburgh/vctk", "philgzl/vctk", "Milana/vctk_dataset_no_unknown",
    "amparacha/vctk_22kHz", "jspaulsen/vctk",
    # Common Voice (CC0) - carries client age/gender/accent metadata
    "fsicoli/common_voice_22_0", "fsicoli/common_voice_17_0",
    "espnet/common_voice_8_0_en", "espnet/common_voice_8_0_zh-CH",
    "mozilla-foundation/common_voice_17_0",
    # LibriTTS / LibriTTS-R (CC-BY-4.0, public domain reader material)
    "mythicinfinity/libritts_r", "mythicinfinity/libritts",
    "parler-tts/libritts_r_filtered", "sdialog/voices-libritts",
    # Chinese multi-speaker
    "alsace0053/aishell3", "kehanlu/aishell3", "songgao/opens1",
    # Emotional / expressive
    "kehanlu/ESD", "X-MOD/esd", "pariajm/sharif_emotional_speech_dataset",
    # Age / style coverage
    "LTU-TALP/cremad", "juletxara/emov-db", "SeymourNickerson/mixspeech",
]

LOG_FH = [None]


def pout(msg=""):
    """Print and mirror into logs/discovery_last.txt with an immediate flush."""
    print(msg, flush=True)
    fh = LOG_FH[0]
    if fh is not None:
        fh.write(msg + "\n")
        fh.flush()


def get(path, params=None, timeout=18):
    last = None
    for base in ENDPOINTS:
        try:
            r = requests.get(base + path, params=params, timeout=timeout,
                             headers={"User-Agent": "resource-scout/1.0"})
            if r.status_code == 200:
                return base, r
            last = "%s -> HTTP %s" % (base, r.status_code)
        except Exception as exc:  # noqa: BLE001
            last = "%s -> %s" % (base, type(exc).__name__)
    raise RuntimeError(last or "no endpoint")


def dataset_info(repo):
    try:
        base, r = get("/api/datasets/" + repo, {"full": "true"})
    except Exception as exc:  # noqa: BLE001
        return {"repo": repo, "error": str(exc)}
    d = r.json()
    card = d.get("cardData") or {}
    lic = card.get("license")
    if isinstance(lic, list):
        lic = "+".join(str(x) for x in lic)
    langs = [t.split(":", 1)[1] for t in d.get("tags", []) if t.startswith("language:")]
    return {
        "repo": repo, "endpoint": base, "license": lic or "-",
        "gated": d.get("gated", "?"), "downloads": d.get("downloads", 0),
        "langs": langs[:8],
    }


def dataset_tree(repo):
    try:
        _, r = get("/api/datasets/%s/tree/main" % repo, {"recursive": "true"})
    except Exception as exc:  # noqa: BLE001
        return None, str(exc)
    try:
        return r.json(), None
    except Exception as exc:  # noqa: BLE001
        return None, "bad json: %s" % exc


def _hm(n):
    if n >= 1 << 30:
        return "%.1fG" % (n / (1 << 30))
    if n >= 1 << 20:
        return "%.1fM" % (n / (1 << 20))
    return "%.0fK" % (n / (1 << 10))


def summarise(repo, limit_files=10):
    info = dataset_info(repo)
    if "error" in info:
        pout("%-46s ERR %s" % (repo, info["error"]))
        return
    pout("%-46s lic=%-20s gated=%-6s dl=%-8s langs=%s" % (
        repo, str(info["license"])[:20], info["gated"], info["downloads"],
        ",".join(info["langs"])[:40]))
    tree, err = dataset_tree(repo)
    if tree is None:
        pout("      tree ERR %s" % err)
        return
    audio = [f for f in tree if f.get("path", "").lower().endswith(AUDIO_EXT)]
    parquet = [f for f in tree if f.get("path", "").lower().endswith(".parquet")]
    pout("      files=%d audio=%d%s parquet=%d" % (
        len(tree), len(audio),
        (" max %s" % _hm(max((f["size"] for f in audio), default=0))) if audio else "",
        len(parquet)))
    for f in (audio or parquet or tree)[:limit_files]:
        pout("        %-74s %8s" % (f["path"][:74], _hm(f.get("size", 0))))


def main(argv):
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    repos = list(argv[1:]) or CANDIDATES
    LOG_FH[0] = open(LOG_DIR / "discovery_last.txt", "w", encoding="utf-8")
    try:
        pout("# discovery run, endpoints=%s, repos=%d" % (ENDPOINTS, len(repos)))
        for repo in repos:
            try:
                summarise(repo)
            except Exception as exc:  # noqa: BLE001
                pout("%-46s FATAL %s" % (repo, exc))
        pout("# done")
    finally:
        LOG_FH[0].close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
