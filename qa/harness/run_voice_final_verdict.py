"""U-12③：1039 条音色的「能说话人声」终判清单（AUDIT-2D §8 U-12 第三项）。

口径写死在这里，免得下一轮又拿"在盘"当"能说话"：

    机器四档（本次全量复采，判据 = 产品自己在用的双门）
        no_artifact          sample_audio_path 为空 —— 只有描述，没有任何声音产物
        file_missing         记了路径但盘上没有（册记与磁盘不一致）
        artifact_gate_failed 文件在盘，被双门拒（静音地板 / 退化音 / 空 / 非有限值）
        artifact_gate_ok     文件在盘且双门通过 —— 这仍是"有真实非退化声音"，
                             **不等于**"能说话的人声"，见下面第六档

    第六档（ASR 代理，`--asr N` 才跑）
        对分层抽到的枚跑 faster-whisper，记 `文本 / 语种 / CJK 字数`。
        「字准率」在这批数据上**不可计算**：入册时留的 `meta.transcript` 参照文本
        实测 0 枚非空（本脚本会把这个数现采进报告）。所以 ASR 档只能回答
        "像不像有人在说话"，不能回答"说得对不对"。人耳抽验仍是必需的另一半。

生产库全程 `mode=ro`；双门调的是 `modules/voice_manager/audio_quality.assess_file`
本体，不另写一套判据。

用法：
    PYTHONUTF8=1 python -X utf8 qa/harness/run_voice_final_verdict.py
    PYTHONUTF8=1 python -X utf8 qa/harness/run_voice_final_verdict.py --asr 60
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import random
import sqlite3
import statistics
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from modules.voice_manager.audio_quality import assess_file  # noqa: E402  产品同一把门

PROD_DB = REPO / "data" / "peiyinyuan.db"
OUT_DIR = REPO.parent / "agent-ops" / "audit" / "voice-verdict"
# 本机 HF 缓存里只有 `Systran/faster-whisper-small`（09-22 那批 ASR 用的就是它）。
# 写 `base` 会去联网下载并 ConnectTimeout —— 所以钉死 small，并离线优先。
ASR_MODEL = "small"
ASR_BUDGET_S = 900.0


def open_ro(path: Path) -> sqlite3.Connection:
    return sqlite3.connect("file:{}?mode=ro".format(str(path).replace("\\", "/")), uri=True)


def resolve(raw: str) -> Path:
    p = Path(raw)
    return p if p.is_absolute() else REPO / raw


def gate_all(db: sqlite3.Connection) -> list[dict]:
    """逐条复采双门，并带上册内登记的门检值（只作对照，不作判据）。"""
    rows = db.execute(
        "SELECT id,name,source,engine,category,language,audio_language_tier,"
        "sample_audio_path,meta FROM voice_profiles ORDER BY category,source,name,id"
    ).fetchall()
    out = []
    for (vid, name, source, engine, category, language, tier,
         path_raw, meta_raw) in rows:
        rec = {"id": vid, "name": name, "source": source, "engine": engine,
               "category": category, "language": language, "tier": tier,
               "path": path_raw, "ingest_gate": None, "peak_dbfs": None,
               "flatness": None, "dur_s": None, "sr": None}
        try:
            meta = json.loads(meta_raw) if meta_raw else {}
        except Exception:
            meta = {}
        sg = meta.get("speech_gate")
        if isinstance(sg, dict):
            rec["ingest_gate"] = {k: sg.get(k) for k in ("dur", "peak", "rms")}
        rec["has_transcript"] = bool(str(meta.get("transcript") or "").strip())

        if not path_raw:
            rec["verdict"] = "no_artifact"
            out.append(rec)
            continue
        p = resolve(path_raw)
        if not p.exists():
            rec["verdict"] = "file_missing"
            out.append(rec)
            continue
        report, err = assess_file(str(p))
        if report is None:
            rec["verdict"] = "artifact_unreadable"
            rec["reason"] = err
            out.append(rec)
            continue
        rec["peak_dbfs"] = round(report["peak_dbfs"], 2)
        rec["flatness"] = report["spectral_flatness"]
        rec["sr"] = report["sample_rate"]
        if report["sample_rate"]:
            rec["dur_s"] = round(report["samples"] / report["sample_rate"], 3)
        rec["verdict"] = "artifact_gate_ok" if report["ok"] else "artifact_gate_failed"
        if not report["ok"]:
            rec["reason"] = f"{report['code']}: {report['reason']}"
        out.append(rec)
    return out


def asr_sample(records: list[dict], n: int) -> list[dict]:
    """按 category 分层轮转抽样（每类等量优先），只抽有产物且过门的枚。"""
    pool = defaultdict(list)
    for r in records:
        if r["verdict"] == "artifact_gate_ok" and r["path"]:
            pool[r["category"] or "uncategorized"].append(r)
    rng = random.Random(20260923)
    for cat in pool:
        rng.shuffle(pool[cat])
    picked: list[dict] = []
    cats = sorted(pool)
    i = 0
    while len(picked) < n and any(pool[c] for c in cats):
        cat = cats[i % len(cats)]
        if pool[cat]:
            picked.append(pool[cat].pop())
        i += 1
    return picked


def run_asr(picked: list[dict]) -> dict:
    from faster_whisper import WhisperModel

    started = time.monotonic()
    model = WhisperModel(ASR_MODEL, device="cpu", compute_type="int8")
    for r in picked:
        if time.monotonic() - started > ASR_BUDGET_S:
            r["asr_text"] = None
            r["asr_skipped"] = "wall_budget"
            continue
        try:
            segs, info = model.transcribe(resolve(r["path"]).as_posix(), language=None,
                                           beam_size=1, vad_filter=False)
            text = "".join(s.text.strip() for s in segs).strip()
        except Exception as exc:
            r["asr_text"] = None
            r["asr_error"] = f"{type(exc).__name__}: {exc}"
            continue
        r["asr_text"] = text
        r["asr_lang"] = getattr(info, "language", None)
        r["asr_cjk"] = sum(1 for ch in text if "一" <= ch <= "鿿")
    return {"model": ASR_MODEL, "requested": len(picked),
            "done": sum(1 for r in picked if "asr_text" in r and "asr_skipped" not in r),
            "wall_s": round(time.monotonic() - started, 1)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--asr", type=int, default=0, metavar="N",
                    help="额外对 N 枚分层抽样跑 ASR 代理判定（默认 0 = 只跑机器四档）")
    args = ap.parse_args()

    if not PROD_DB.exists():
        print("找不到生产库:", PROD_DB)
        return 2
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")

    db = open_ro(PROD_DB)
    total = db.execute("SELECT count(*) FROM voice_profiles").fetchone()[0]
    records = gate_all(db)
    db.close()

    by_verdict = Counter(r["verdict"] for r in records)
    by_cat = Counter(r["category"] for r in records)
    ok_by_cat = Counter(r["category"] for r in records
                        if r["verdict"] == "artifact_gate_ok")
    durs = [r["dur_s"] for r in records if r["dur_s"]]
    summary = {
        "stamp": stamp,
        "voices_total": total,
        "rows_checked": len(records),
        "verdicts": dict(by_verdict),
        "gate_ok_by_category": {k: ok_by_cat.get(k, 0) for k in sorted(by_cat)},
        "category_totals": {k: by_cat[k] for k in sorted(by_cat)},
        "dur_s": {"min": min(durs), "median": round(statistics.median(durs), 3),
                  "max": max(durs)} if durs else None,
        "transcript_reference_nonempty": sum(1 for r in records if r["has_transcript"]),
        "cer_computable": False,
        "note": "artifact_gate_ok 只证明「有真实、非退化的声音」，不证明「说得清是人在说话」；"
                "后者需 ASR 代理 + 人耳抽验，且因 transcript 全空而无法计算字准率。",
    }
    csv_path = OUT_DIR / ("verdict_%s.csv" % stamp)
    json_path = OUT_DIR / ("summary_%s.json" % stamp)

    def dump() -> None:
        with csv_path.open("w", encoding="utf-8-sig", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["id", "name", "source", "engine", "category", "tier", "verdict",
                        "dur_s", "sr", "peak_dbfs", "flatness", "ingest_gate_dur",
                        "asr_lang", "asr_cjk", "asr_text", "reason", "path"])
            for r in records:
                ig = r["ingest_gate"] or {}
                w.writerow([r["id"], r["name"], r["source"], r["engine"], r["category"],
                            r["tier"], r["verdict"], r["dur_s"], r["sr"], r["peak_dbfs"],
                            r["flatness"], ig.get("dur"), r.get("asr_lang"),
                            r.get("asr_cjk"), r.get("asr_text"), r.get("reason", ""),
                            r["path"]])
        json_path.write_text(json.dumps(summary, ensure_ascii=False, indent=1),
                             encoding="utf-8")

    dump()          # 第一阶段先落盘：ASR 跑挂也不能把已经复采完的全量清单丢掉
    if args.asr:
        picked = asr_sample(records, args.asr)
        try:
            summary["asr"] = run_asr(picked)
        except Exception as exc:
            summary["asr"] = {"error": f"{type(exc).__name__}: {exc}",
                              "model": ASR_MODEL, "done": 0}
        else:
            summary["asr"]["with_cjk"] = sum(1 for r in picked if r.get("asr_cjk"))
            summary["asr"]["empty_text"] = sum(1 for r in picked
                                               if r.get("asr_text") == "")
            summary["asr"]["by_category"] = dict(Counter(r["category"] for r in picked))
        dump()      # 第二阶段带 ASR 列重写同一份清单

    print(json.dumps(summary, ensure_ascii=False, indent=1))
    print("清单:", csv_path)
    print("核对: 本次逐条复采 rows_checked=%d / 库内 voice_profiles=%d" % (len(records), total))
    return 0


if __name__ == "__main__":
    sys.exit(main())
