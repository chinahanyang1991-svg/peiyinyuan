"""Verify every sample in data/resource.db by actually opening it.

`voice_samples.verified` is only ever written here, so a row can only be
trusted if the bytes on disk load, are long enough, are loud enough and are not
clipped.  A file that fails is flipped back to verified=0 with the reason in
verify_note, so nothing silently enters the library.

Usage:
    python verify_samples.py                     # verify everything
    python verify_samples.py --only-unverified
    python verify_samples.py --db <path> --root <data dir>
    python verify_samples.py --purge-broken      # also delete failed files
"""

import argparse
import json
import math
import os
import sys
import time
from collections import Counter
from pathlib import Path

import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parent))
import rs_audio as A       # noqa: E402
import rs_db as D          # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
LOG_DIR = Path(__file__).resolve().parent / "logs"


def resolve(root, rel):
    if not rel:
        return None
    p = Path(rel)
    if not p.is_absolute():
        p = Path(root) / p
    return p


def check_file(path, min_dur, max_dur, rms_floor, expect_sr=None):
    """Returns (ok, note, measured dict)."""
    if not path or not os.path.exists(path):
        return False, "missing file", {}
    try:
        size = os.path.getsize(path)
        if size < 1024:
            return False, "file too small (%dB)" % size, {}
        x, sr = sf.read(str(path), dtype="float32", always_2d=False)
        if getattr(x, "ndim", 1) > 1:
            x = x.mean(axis=1)
    except Exception as exc:  # noqa: BLE001
        return False, "soundfile load failed: %s" % type(exc).__name__, {}
    dur = float(x.shape[0]) / float(sr) if sr else 0.0
    rms = float((x.astype("float64") ** 2).mean() ** 0.5) if x.size else 0.0
    peak = float(abs(x).max()) if x.size else 0.0
    clip = int((abs(x) >= 0.999).sum())
    m = {"sample_rate": int(sr), "duration_s": round(dur, 3), "rms": round(rms, 6),
         "peak": round(peak, 6), "clipping": clip, "frames": int(x.shape[0])}
    if not math.isfinite(rms) or not math.isfinite(peak):
        return False, "rms/peak not finite", m
    if expect_sr and int(sr) != int(expect_sr):
        return False, "sample rate %s != %s" % (sr, expect_sr), m
    if dur < min_dur:
        return False, "duration %.2fs < %.2fs" % (dur, min_dur), m
    if dur > max_dur + 0.6:
        return False, "duration %.2fs > %.2fs" % (dur, max_dur), m
    if rms <= rms_floor:
        return False, "near-silent rms=%.5f <= %.5f" % (rms, rms_floor), m
    if peak < 0.02:
        return False, "peak %.4f too low" % peak, m
    if clip > max(50, int(x.shape[0] * 0.0005)):
        return False, "clipped samples=%d" % clip, m
    return True, "ok %.2fs rms=%.4f peak=%.3f sr=%d" % (dur, rms, peak, sr), m


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=str(ROOT / "data" / "resource.db"))
    ap.add_argument("--root", default=str(ROOT),
                    help="base directory for the relative paths stored in the db")
    ap.add_argument("--min-dur", type=float, default=4.5)
    ap.add_argument("--max-dur", type=float, default=21.0)
    ap.add_argument("--rms-floor", type=float, default=A.RMS_FLOOR)
    ap.add_argument("--only-unverified", action="store_true")
    ap.add_argument("--purge-broken", action="store_true")
    ap.add_argument("--report-json", default=str(LOG_DIR / "verify_summary.json"))
    args = ap.parse_args(argv)

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    if not os.path.exists(args.db):
        print("[verify] 数据库不存在: %s" % args.db)
        return 2
    db = D.ResourceDB(args.db)
    sql = "SELECT * FROM voice_samples"
    if args.only_unverified:
        sql += " WHERE verified<>1"
    sql += " ORDER BY source_key, speaker_id, id"
    rows = db.rows(sql)
    print("[verify] 待校验样本 %d 条 | rms阈值 %.5f | 时长 %.1f-%.1fs" % (
        len(rows), args.rms_floor, args.min_dur, args.max_dur))

    t0 = time.time()
    passed = failed = 0
    fail_reasons = Counter()
    lines = []
    for r in rows:
        p_master = resolve(args.root, r["path"])
        ok, note, m = check_file(p_master, args.min_dur, args.max_dur, args.rms_floor,
                                 expect_sr=None)
        p_prev = resolve(args.root, r["path_22k"])
        if ok:
            ok2, note2, _m2 = check_file(p_prev, args.min_dur, args.max_dur,
                                         args.rms_floor, expect_sr=None)
            if not ok2:
                ok, note = False, "preview 22k: " + note2
        dur = m.get("duration_s", r.get("duration_s") or 0)
        if ok:
            passed += 1
        else:
            failed += 1
            key = note.split(" ")[0] if note else "unknown"
            fail_reasons[key] += 1
            if args.purge_broken:
                for pp in (p_master, p_prev):
                    try:
                        if pp and os.path.exists(pp):
                            os.remove(pp)
                    except OSError:
                        pass
        db.set_verified(r["id"], 1 if ok else 0, (note or "")[:200])
        if m:
            db.conn.execute("UPDATE voice_samples SET duration_s=?, rms=?, peak=?, "
                            "clipping=?, sample_rate=? WHERE id=?",
                            (m.get("duration_s"), m.get("rms"), m.get("peak"),
                             m.get("clipping"), m.get("sample_rate"), r["id"]))
            db.conn.commit()
        line = "%-6s %-46s %-60s %s" % ("PASS" if ok else "FAIL",
                                        str(r["uid"])[:46], str(r["path"]).split("resources")[-1][:60],
                                        note)
        lines.append(line)
        print("  " + line)
    db.recompute_voice_counts()
    db.log("verify_samples", "verify", "all", "done", 0,
           "pass=%d fail=%d" % (passed, failed))

    total = db.one("SELECT COUNT(*) n FROM voice_samples")["n"]
    ver = db.one("SELECT COUNT(*) n FROM voice_samples WHERE verified=1")["n"]
    nvoices = db.one("SELECT COUNT(DISTINCT voice_id) n FROM voice_samples")["n"]
    nvervoices = db.one("SELECT COUNT(DISTINCT voice_id) n FROM voice_samples WHERE verified=1")["n"]
    print("=" * 78)
    print("[verify] 通过 %d / 失败 %d | 库内样本 %d, verified=1 %d (%.1f%%)" % (
        passed, failed, total, ver, (100.0 * ver / total) if total else 0.0))
    print("[verify] 音色 %d 个, 其中拥有 verified 样本的音色 %d 个" % (nvoices, nvervoices))
    print("[verify] 耗时 %.1fs" % (time.time() - t0))

    dist = {}
    for col in ("gender", "age_band", "language", "style", "source_key", "license"):
        dist[col] = {row[col]: row["n"] for row in db.rows(
            "SELECT %s AS %s, COUNT(*) n FROM voice_samples WHERE verified=1 GROUP BY %s"
            % (col, col, col))}
        print("  %-10s %s" % (col, dist[col]))
    with open(LOG_DIR / "verify_report.txt", "a", encoding="utf-8") as fh:
        fh.write("=== %s db=%s ===\n" % (D.now(), args.db))
        fh.write("\n".join(lines) + "\n")
        fh.write("passed=%d failed=%d total=%d verified=%d\n" % (passed, failed, total, ver))
    Path(args.report_json).write_text(json.dumps(dict(
        passed=passed, failed=failed, total=total, verified=ver,
        voices=nvoices, verified_voices=nvervoices, reasons=dict(fail_reasons),
        distributions=dist), ensure_ascii=False, indent=1), encoding="utf-8")
    db.close()
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
