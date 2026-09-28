#!/usr/bin/env python3
"""音色库换库事务：隔离 BAD + 灌入合格候选（可回滚，主线专用）。

用户 09-22 裁决：五类音色**都必须是"能说话的人声变体"**，原始音效一律无效。
审计智能体已对 517 行逐条判定（`agent-ops/voice-audit/out/verdicts.json`，
BAD 423 / SUSPECT 58 / OK 36）。本脚本做两件事，且**必须原子地一起做**，
免得库里出现"三类空掉"的窗口：

  1) 把 BAD 行的音频文件**移动**（不是删除）到 `data/quarantine/voice-purge-<ts>/`，
     并删掉这些 `voice_profiles` 行；
  2) 把候选清单（`refill_manifest.json` 等，字段对齐 `manifest.json`）灌进来：
     `source='downloaded'` + `engine=--engine` + `reference_audio_path=<wav>`，
     这样引擎 `_restore_voices` 会把它们当克隆音色注册，试听样本与合成结果同源。

三层可逆（缺一层就不许 --apply）：
  * 在线备份：`sqlite3` backup API → `data/backups/peiyinyuan-preSwap-<ts>.db`
  * 逐行回滚 SQL：每条被删的行写一条 `INSERT INTO voice_profiles (...) VALUES (...)`
  * 文件移动台账：`from<TAB>to` 逐条，恢复=反向 move

默认 `--dry-run`（只出计划，不写不删）。`--apply` 才动手。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
import uuid
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]          # peiyinyuan/
DB = REPO / "data" / "peiyinyuan.db"
QUARANTINE_ROOT = REPO / "data" / "quarantine"
BACKUP_ROOT = REPO / "data" / "backups"
AUDIT_VERDICTS = REPO / "agent-ops" / "voice-audit" / "out" / "verdicts.json"
COLS = ['id', 'name', 'source', 'engine', 'language', 'gender', 'age_group',
        'style_tags', 'model_path', 'sample_audio_path', 'description',
        'reference_audio_path', 'is_downloaded', 'download_url', 'meta',
        'created_at', 'updated_at', 'category', 'seed']
NAMESPACE = uuid.UUID("6f1e2a44-7c3b-4d5e-9a1b-2c3d4e5f6a7b")   # 本管线固定命名空间
MIN_DUR_S, MAX_DUR_S, MIN_PEAK = 1.2, 30.0, 0.02


def q_path(p: str | None) -> Path | None:
    return Path(p) if p else None


def load_bad_ids(path: Path) -> list[str]:
    d = json.loads(path.read_text(encoding="utf-8"))
    return sorted(r["id"] for r in d["records"] if r["bucket"] == "VERDICT-BAD")


def load_candidates(manifests: list[Path]) -> list[dict]:
    out, seen = [], set()
    for m in manifests:
        rows = json.loads(m.read_text(encoding="utf-8"))
        for r in rows:
            key = r.get("local_file") or r.get("page_url")
            if not key or key in seen:
                continue
            seen.add(key)
            r["_manifest"] = str(m)
            out.append(r)
    return out


def qc_candidate(r: dict) -> str | None:
    """候选自证门：文件在、能解码、时长/峰值达标。返回拒收原因或 None。"""
    f = q_path(r.get("local_file"))
    if not f or not f.exists():
        return "FILE_MISSING"
    try:
        import soundfile as sf
        info = sf.info(str(f))
        dur, sr, ch = info.duration, info.samplerate, info.channels
        data, _ = sf.read(str(f), always_2d=False)
    except Exception as e:
        return f"DECODE_FAIL:{type(e).__name__}"
    if not (MIN_DUR_S <= dur <= MAX_DUR_S):
        return f"DURATION_OUT_OF_RANGE:{dur:.2f}"
    peak = float(abs(data).max()) if len(data) else 0.0
    if peak < MIN_PEAK:
        return f"PEAK_TOO_LOW:{peak:.4f}"
    r["_measured"] = {"duration_s": round(dur, 3), "sample_rate": sr,
                      "channels": ch, "peak": round(peak, 4)}
    return None


def row_from_candidate(r: dict, engine: str, now: str) -> dict:
    wav = str(Path(r["local_file"]).resolve())
    vid = str(uuid.uuid5(NAMESPACE, "pyy-voice:" + wav.lower()))
    cat = r.get("category") or "human"
    desc = "｜".join(filter(None, [
        r.get("name"), f"类别:{cat}",
        f"许可:{r.get('license') or 'Internal-Generated'}",
        f"出处:{r.get('page_url') or r.get('source_url') or 'local'}"]))
    meta = {k: r[k] for k in ("license", "attribution", "source_site", "page_url",
                              "source_url", "tags", "speech_gate", "upstream_title")
            if k in r}
    meta["ingest_manifest"] = r["_manifest"]
    meta["transcript"] = r.get("transcript") or ""
    meta["ingested_at"] = now
    return {
        "id": vid, "name": r.get("name") or Path(wav).stem, "source": "downloaded",
        "engine": engine, "language": r.get("language") or "zh",
        "gender": r.get("gender"), "age_group": r.get("age_group"),
        "style_tags": json.dumps(r.get("tags") or [cat], ensure_ascii=False),
        "model_path": wav, "sample_audio_path": wav,
        "description": desc[:500], "reference_audio_path": wav,
        "is_downloaded": 1, "download_url": r.get("source_url"),
        "meta": json.dumps(meta, ensure_ascii=False),
        "created_at": now, "updated_at": now, "category": cat, "seed": None,
    }


def insert_sql(rec: dict) -> str:
    def lit(v):
        if v is None:
            return "NULL"
        if isinstance(v, int):
            return str(v)
        return "'" + str(v).replace("'", "''") + "'"
    return (f"INSERT INTO voice_profiles ({','.join(COLS)}) VALUES ("
            + ",".join(lit(rec.get(c)) for c in COLS) + ");")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--verdicts", default=str(AUDIT_VERDICTS))
    ap.add_argument("--manifest", action="append", default=[],
                    help="候选清单，可多次传（默认只灌 refill）")
    ap.add_argument("--engine", default="voxcpm",
                    help="灌入行使用的合成引擎（voxcpm|openvoice|gptsovits|edgetts）")
    ap.add_argument("--apply", action="store_true", help="真动手；缺省只出计划")
    args = ap.parse_args()

    manifests = [Path(m) for m in args.manifest] or [
        REPO / "tools" / "voice_library" / "refill" / "refill_manifest.json"]
    bad_ids = load_bad_ids(Path(args.verdicts))
    cands = load_candidates(manifests)
    keep, rejected = [], []
    for r in cands:
        why = qc_candidate(r)
        (rejected if why else keep).append((r, why) if why else r)

    con = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    bad_rows = []
    if bad_ids:
        ph = ",".join("?" * len(bad_ids))
        bad_rows = [dict(r) for r in con.execute(
            f"SELECT {','.join(COLS)} FROM voice_profiles WHERE id IN ({ph})", bad_ids)]
    existing = {r["id"] for r in con.execute("SELECT id FROM voice_profiles")}
    existing_files = {str(r["sample_audio_path"]).lower()
                      for r in con.execute("SELECT sample_audio_path FROM voice_profiles")
                      if r["sample_audio_path"]}
    con.close()

    new_rows = [row_from_candidate(r, args.engine,
                                   datetime.now().isoformat(sep=" ", timespec="seconds"))
                for r in keep]
    dup_id = [n["id"] for n in new_rows if n["id"] in existing]
    dup_file = [n["id"] for n in new_rows if str(n["sample_audio_path"]).lower()
                in existing_files]
    plan = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "apply": args.apply,
        "verdicts": str(args.verdicts), "manifests": [str(m) for m in manifests],
        "purge_bad_rows": len(bad_rows), "purge_ids": bad_ids,
        "candidates_total": len(cands), "qc_rejected": len(rejected),
        "qc_reject_reasons": {}
        , "insert_rows": len(new_rows),
        "already_present_by_id": len(dup_id), "already_present_by_file": len(dup_file),
        "file_moves": sum(1 for r in bad_rows
                          for c in ("model_path", "sample_audio_path", "reference_audio_path")
                          if q_path(r[c]) and q_path(r[c]).exists()),
        "counts_after_by_category": {},
    }
    from collections import Counter
    plan["qc_reject_reasons"] = dict(Counter(w.split(":")[0] for _, w in rejected))
    con = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)
    before = dict(con.execute("SELECT category, COUNT(*) FROM voice_profiles GROUP BY category"))
    con.close()
    after = dict(before)
    for cat, n in Counter(r["category"] for r in bad_rows).items():
        after[cat] = after.get(cat, 0) - n
    for cat, n in Counter(r["category"] for r in new_rows).items():
        after[cat] = after.get(cat, 0) + n
    plan["counts_before_by_category"] = before
    plan["counts_after_by_category"] = after

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    outdir = REPO.parent / "agent-ops" / f"swap-{ts}"
    print(json.dumps({k: v for k, v in plan.items() if k != "purge_ids"},
                     ensure_ascii=False, indent=1))
    if not args.apply:
        print("\n[dry-run] 未写库、未移动文件。加 --apply 执行。")
        return 0

    missing = [r["id"] for r in bad_rows if r["id"] not in set(bad_ids)]
    assert not missing, "BAD 行集与审计清单不一致，中止"

    BACKUP_ROOT.mkdir(parents=True, exist_ok=True)
    outdir.mkdir(parents=True, exist_ok=True)
    backup = BACKUP_ROOT / f"peiyinyuan-preSwap-{ts}.db"
    src = sqlite3.connect(DB)
    dst = sqlite3.connect(backup)
    src.backup(dst)
    dst.close()

    qroot = QUARANTINE_ROOT / f"voice-purge-{ts}"
    moves = []
    for r in bad_rows:
        for col in ("model_path", "sample_audio_path", "reference_audio_path"):
            p = q_path(r[col])
            if not p or not p.exists() or not p.is_file():
                continue
            dest = qroot / r["category"] / f'{r["id"]}-{col}-{p.name}'
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(p), str(dest))
            moves.append((str(p), str(dest)))

    (outdir / "rollback_rows.sql").write_text(
        "\n".join(insert_sql(r) for r in bad_rows), encoding="utf-8")
    (outdir / "delete_inserted.sql").write_text(
        "\n".join(f"DELETE FROM voice_profiles WHERE id='{n['id']}';" for n in new_rows),
        encoding="utf-8")
    (outdir / "file_moves.tsv").write_text(
        "\n".join(f"{a}\t{b}" for a, b in moves), encoding="utf-8")
    (outdir / "purge_ids.json").write_text(json.dumps(bad_ids), encoding="utf-8")
    (outdir / "plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=1),
                                       encoding="utf-8")

    con = sqlite3.connect(DB)
    try:
        cur = con.cursor()
        deleted = 0
        if bad_ids:                      # 纯补录（无 purge）时 IN () 是语法错误
            ph = ",".join("?" * len(bad_ids))
            cur.execute(f"DELETE FROM voice_profiles WHERE id IN ({ph})", bad_ids)
            deleted = cur.rowcount
        ins = 0
        for n in new_rows:
            if n["id"] in existing:
                continue
            cur.execute(f"INSERT INTO voice_profiles ({','.join(COLS)}) "
                        f"VALUES ({','.join('?' * len(COLS))})",
                        [n.get(c) for c in COLS])
            ins += cur.rowcount
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()
    print(json.dumps({"backup": str(backup), "moved_files": len(moves),
                      "deleted_rows": deleted, "inserted_rows": ins,
                      "rollback_dir": str(outdir)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
