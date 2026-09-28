"""
删除"指向不可用引擎"的音色行（可逆、先演练）

为什么要有这个工具：音色行是历史写下的，而引擎的可用性会变。kokoro 的 python
依赖与权重在本构建里都不在了（`pip show kokoro` 仍指向已删除的 vendor/kokoro，
`find_spec` 报 MISSING —— pip 元数据不等于能 import），indextts 同。挂在这些引擎
名下的音色行永远不会说话，用户点它必然失败（红线④「人声可应用」+ PY-117
「不得用静音冒充成功」）。

口径：
  * 候选 = **任意 source** 的音色行，只要其 engine 的 `unavailable_reason()` 非空
    （内置种子行与在线登记行同样不会说话）；安全性不靠 source，靠下面两条；
  * 逐行做**全表全文本列**引用普查，命中 >0 一律不删（宁可留坏行，也不制造悬空引用）；
  * 删除前 sqlite `backup()` API 出快照（活库带 WAL，copy2 会拿到撕裂页），
    回滚 INSERT **先在快照上实跑**并断言逐列还原成功，才对活库执行；
  * 行名下的音频文件不删，**移进证据目录**并记原路径，回滚时按原样放回；
  * 默认 dry-run，只出清单与证据；--apply 才动库。

用法：
    python -X utf8 tools/voice_library/purge_unavailable_voices.py            # 看
    python -X utf8 tools/voice_library/purge_unavailable_voices.py --apply    # 删
"""

import argparse
import datetime as dt
import hashlib
import json
import shutil
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

DB_PATH = REPO / "data" / "peiyinyuan.db"
EVIDENCE_DIR = REPO / "agent-ops" / "voice-purge"
PATH_COLUMNS = ("sample_audio_path", "reference_audio_path", "model_path")


def _connect(write: bool) -> sqlite3.Connection:
    uri = f"file:{DB_PATH.as_posix()}?mode={'rw' if write else 'ro'}"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _unavailable_engines() -> dict:
    """engine_name -> 人话原因（只查 import 可得性，不加载权重、不碰网络）。"""
    from modules.tts_engine.engine_registry import EngineRegistry

    reasons = {}
    for row in EngineRegistry().list_engines():
        if not row.get("available", True):
            reasons[row["name"]] = row.get("reason", "")
    return reasons


def _text_columns(conn, table: str) -> list:
    cols = []
    for r in conn.execute(f'PRAGMA table_info("{table}")'):
        t = (r["type"] or "").lower()
        if "char" in t or "text" in t or "clob" in t:
            cols.append(r["name"])
    return cols


def _reference_census(conn, table: str, voice_id: str) -> dict:
    """全表全文本列按 id 等值 + 子串比对。只保留命中 > 0 的表。"""
    hits = {}
    for col in _text_columns(conn, table):
        try:
            n = conn.execute(
                f'SELECT COUNT(*) c FROM "{table}" '
                f'WHERE "{col}" IS NOT NULL AND ("{col}" = ? OR "{col}" LIKE ?)',
                (voice_id, f'%{voice_id}%'),
            ).fetchone()["c"]
        except sqlite3.Error:
            continue
        if n:
            hits[table] = n
    return hits


def _dump_value(v):
    if isinstance(v, bytes):
        return {"__hex__": v.hex()}
    return v


def _load_value(raw):
    if isinstance(raw, dict) and "__hex__" in raw:
        return bytes.fromhex(raw["__hex__"])
    return raw


def _sql_literal(v) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, bytes):
        return f"X'{v.hex()}'"
    return "'" + str(v).replace("'", "''") + "'"


def _sidecars(row) -> list:
    """该行名下实际存在的音频/模型文件（绝对路径字符串）。"""
    out = []
    for col in PATH_COLUMNS:
        try:
            raw = row[col]
        except (IndexError, KeyError):
            continue
        if not raw or not isinstance(raw, str):
            continue
        p = Path(raw)
        if p.is_file():
            out.append(str(p))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真删（默认只出清单）")
    args = ap.parse_args()

    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    reasons = _unavailable_engines()
    print(f"[引擎实况] 不可用引擎: {json.dumps(reasons, ensure_ascii=False)}")
    if not reasons:
        print("[结论] 没有不可用引擎，无需清理。")
        return 0

    ro = _connect(write=False)
    tables = [r["name"] for r in ro.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")]
    placeholders = ",".join("?" * len(reasons))
    candidates = ro.execute(
        f"SELECT * FROM voice_profiles WHERE engine IN ({placeholders})",
        tuple(reasons)
    ).fetchall()
    print(f"[候选] {len(candidates)} 行")

    to_delete, kept = [], []
    for row in candidates:
        refs = {}
        for table in tables:
            if table == "voice_profiles":
                continue
            refs.update(_reference_census(ro, table, row["id"]))
        if refs:
            kept.append({"id": row["id"], "name": row["name"], "refs": refs})
            print(f"  [保留] {row['name']} ({row['id']}) 仍被引用: {refs}")
            continue
        cols = list(row.keys())
        to_delete.append({
            "id": row["id"],
            "name": row["name"],
            "engine": row["engine"],
            "source": row["source"],
            "reason": reasons.get(row["engine"], ""),
            "columns": cols,
            "values": {c: _dump_value(row[c]) for c in cols},
            "sidecar_files": _sidecars(row),
        })
        print(f"  [零引用] {row['name']} ({row['id']}) "
              f"engine={row['engine']} source={row['source']} "
              f"文件={len(to_delete[-1]['sidecar_files'])}")

    before_total = ro.execute("SELECT COUNT(*) c FROM voice_profiles").fetchone()["c"]
    ro.close()

    evidence = {
        "ts": ts,
        "unavailable_engines": reasons,
        "before_total_voices": before_total,
        "deleted": to_delete,
        "kept_referenced": kept,
        "rollback_sql": [
            "INSERT INTO voice_profiles ({}) VALUES ({});".format(
                ", ".join(f'"{c}"' for c in d["columns"]),
                ", ".join(_sql_literal(_load_value(d["values"][c]))
                          for c in d["columns"]),
            )
            for d in to_delete
        ],
    }

    if not args.apply:
        print("[dry-run] 未改动数据库。加 --apply 才执行。")
        _write_evidence(evidence, ts)
        return 0

    if not to_delete:
        print("[结论] 零引用候选为空，不执行任何写操作。")
        evidence["after_total_voices"] = before_total
        _write_evidence(evidence, ts)
        return 0

    # ---- 快照（backup API，非文件复制） ----
    snapshot = EVIDENCE_DIR / f"pre_purge_{ts}.db"
    src = _connect(write=False)
    dst = sqlite3.connect(str(snapshot))
    src.backup(dst)
    dst.close()
    src.close()
    digest = hashlib.sha256(snapshot.read_bytes()).hexdigest()[:16]
    print(f"[快照] {snapshot.relative_to(REPO)} sha256[:16]={digest}")

    # ---- 回滚演练：在快照上删→断言没了→按 dump 插回→断言逐列等值 ----
    ev = sqlite3.connect(str(snapshot))
    ev.row_factory = sqlite3.Row
    for d in to_delete:
        ev.execute("DELETE FROM voice_profiles WHERE id=?", (d["id"],))
        assert ev.execute("SELECT COUNT(*) FROM voice_profiles WHERE id=?",
                          (d["id"],)).fetchone()[0] == 0
    ev.commit()
    for d, sql in zip(to_delete, evidence["rollback_sql"]):
        ev.execute(sql)
    ev.commit()
    for d in to_delete:
        got = ev.execute("SELECT * FROM voice_profiles WHERE id=?",
                         (d["id"],)).fetchone()
        assert got is not None, f"回滚演练失败：{d['id']} 未还原"
        for c in d["columns"]:
            want = _load_value(d["values"][c])
            assert got[c] == want, f"{d['id']}.{c} 不等: {got[c]!r} != {want!r}"
    snap_after = ev.execute("SELECT COUNT(*) FROM voice_profiles").fetchone()[0]
    ev.close()
    assert snap_after == before_total, f"演练后快照行数漂了: {snap_after} != {before_total}"
    print(f"[演练] 快照上 删除→还原 {len(to_delete)} 行逐列等值通过，行数回到 {before_total}")

    # ---- 正式执行：先把旁路文件移进证据目录（不删），再删行 ----
    file_dir = EVIDENCE_DIR / f"files_{ts}"
    file_dir.mkdir(parents=True, exist_ok=True)
    moved = []
    for d in to_delete:
        for raw in d["sidecar_files"]:
            p = Path(raw)
            if not p.is_file():
                continue
            dest = file_dir / f"{d['id']}_{p.name}"
            shutil.move(str(p), str(dest))
            moved.append({"from": raw, "to": str(dest.relative_to(REPO))})
    evidence["moved_files"] = moved

    live = _connect(write=True)
    for d in to_delete:
        live.execute("DELETE FROM voice_profiles WHERE id=?", (d["id"],))
    live.commit()
    after_total = live.execute("SELECT COUNT(*) c FROM voice_profiles").fetchone()["c"]
    left = live.execute(
        f"SELECT COUNT(*) FROM voice_profiles WHERE engine IN ({placeholders})",
        tuple(reasons)).fetchone()[0]
    live.close()

    evidence["after_total_voices"] = after_total
    evidence["unavailable_rows_left"] = left
    evidence["snapshot"] = str(snapshot.relative_to(REPO))
    evidence["snapshot_sha256_16"] = digest
    evidence["rehearsal"] = "PASS(删除→还原→逐列等值→行数回到删前)"
    _write_evidence(evidence, ts)

    assert after_total == before_total - len(to_delete)
    assert left == 0, f"仍有 {left} 行指向不可用引擎"
    print(f"[完成] {before_total} -> {after_total}，删 {len(to_delete)} 行、"
          f"移走 {len(moved)} 个旁路文件；零引用判据 + 回滚演练均已实跑")
    return 0


def _write_evidence(evidence: dict, ts: str) -> Path:
    out = EVIDENCE_DIR / f"purge_{ts}.json"
    out.write_text(json.dumps(evidence, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    print(f"[证据] {out.relative_to(REPO)}")
    return out


if __name__ == "__main__":
    raise SystemExit(main())
