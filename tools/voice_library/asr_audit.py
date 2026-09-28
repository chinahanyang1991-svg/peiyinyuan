"""音色库 ASR 终判：试听产物必须被识别器读出**成句的语音**。

统计门（谐波/调制/平坦度）只能证"像人声"，证不了"在说话"——持续乐音类音效同样能过。
这支脚本补上那一环：走真实 HTTP 取每类音色的试听产物，喂 faster-whisper，
按"读出字数 + 语言置信"判 SPEECH / NON_SPEECH。

误判方向已知且单向：识别器读不出词 ≠ 音频里没话（CPU small 档对强风格化音色会漏），
所以 NON_SPEECH 只标"待人工复听"，不据此删数据。

用法:
  python -X utf8 tools/voice_library/asr_audit.py --per-class 6
产物: agent-ops/voice-audit/asr-<ts>.json（逐条：音色 id/类/文件名/时长/识别文本/判定）
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
OUT_ROOT = REPO.parent / "agent-ops" / "voice-audit"
BASE = os.getenv("PYY_BACKEND", "http://127.0.0.1:8000")
CLASSES = ("human", "anime", "electronic", "animal", "mechanical")
# 中文一句最少约 5 字；低于此按"没读出话"算
MIN_CHARS = 5


def api(path: str):
    with urllib.request.urlopen(BASE + path, timeout=120) as r:
        return json.load(r)


def preview(vid: str, dst: Path):
    t0 = time.time()
    try:
        with urllib.request.urlopen(BASE + f"/api/voices/{vid}/preview", timeout=300) as r:
            dst.write_bytes(r.read())
    except urllib.error.HTTPError as e:
        return {"http": e.code, "detail": e.read()[:180].decode("utf-8", "replace")}
    return {"http": 200, "bytes": dst.stat().st_size, "s": round(time.time() - t0, 2)}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--per-class", type=int, default=6)
    ap.add_argument("--seed", type=int, default=20260922)
    ap.add_argument("--model", default="small")
    args = ap.parse_args()

    os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
    from faster_whisper import WhisperModel
    import soundfile as sf

    OUT_ROOT.mkdir(parents=True, exist_ok=True)
    tmp = OUT_ROOT / "asr_clips"
    tmp.mkdir(exist_ok=True)
    rnd = random.Random(args.seed)

    t0 = time.time()
    model = WhisperModel(args.model, device="cpu", compute_type="int8")
    print(f"[asr] model={args.model} ready {time.time()-t0:.1f}s", flush=True)

    records = []
    for cat in CLASSES:
        pool = [v["id"] for v in api(f"/api/voices?category={cat}&limit=500")["voices"]]
        pick = rnd.sample(pool, min(args.per_class, len(pool)))
        for vid in pick:
            f = tmp / f"{cat}_{vid[:8]}.wav"
            pr = preview(vid, f)
            if pr.get("http") != 200:
                records.append({"id": vid, "category": cat, "preview": pr,
                                "verdict": "PREVIEW_FAIL"})
                print(cat, vid[:8], pr, flush=True)
                continue
            data, sr = sf.read(str(f), always_2d=False)
            dur = round(len(data) / sr, 2)
            segs, info = model.transcribe(str(f), beam_size=3, vad_filter=False)
            text = "".join(s.text.strip() for s in segs)
            n = len("".join(text.split()))
            records.append({"id": vid, "category": cat, "file": f.name,
                            "dur_s": dur, "bytes": pr["bytes"],
                            "fetch_s": pr["s"], "lang": info.language,
                            "lang_prob": round(float(info.language_probability), 3),
                            "chars": n, "text": text,
                            "verdict": "SPEECH" if n >= MIN_CHARS else "NON_SPEECH"})
            print(f"{cat:11s} {vid[:8]} {dur:5.2f}s {n:3d}字 "
                  f"{records[-1]['verdict']:10s} {text[:34]}", flush=True)

    by = defaultdict(lambda: {"n": 0, "speech": 0})
    for r in records:
        by[r["category"]]["n"] += 1
        by[r["category"]]["speech"] += r.get("verdict") == "SPEECH"
    summary = {k: dict(v, rate=round(v["speech"] / v["n"], 4)) for k, v in by.items()}
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = OUT_ROOT / f"asr-{ts}.json"
    out.write_text(json.dumps({"summary": summary, "records": records},
                              ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({"total_speech": sum(v["speech"] for v in by.values()),
                      "total": len(records), "by_class": summary,
                      "report": str(out)}, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
