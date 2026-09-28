"""给音色行打**参考音频语种档**（PY-323 a′，声学复判，可逆、先演练、可续跑）。

为什么需要它（实测，勿再猜）：`agent-ops/py323/REPORT.md` 的网格结论是 openvoice 的字准
瓶颈**不在噪声系数**，而在**跨语种 SE × converter 交叉项**——普通话参考 tpsim
0.917~1.000，英文参考 0.500~0.917（生产 6 枚复判 5 枚不合格，均值 0.819）。
所以"英文参考的音色别说中文"是**分发问题**：`modules/voice_manager/language_tier.py`
在试听/P5 两个入口按档把 openvoice 行降级到别的引擎。而分发时在线跑尺子不可承受
（CPU 上 whisper 每枚 6~14s），故改成**离线一次性打标**，运行时只查一列。

口径（三条，都是刻意的）：
  * 判据 = `faster_whisper.WhisperModel.detect_language()` 对**参考音频本身**判种，
    置信 < `MIN_LANG_PROB` 一律落 `unknown`（宁可不降级，也不拿一次猜测改路由）；
  * `language` 列**不作数**：库里 animal/electronic 行也写着 'zh'，那是"待合成文本"
    的语种，与参考音频无关。本工具只在 `basis=acoustic` 时改标，声明档只作对照打印；
  * 已打过标（`audio_language_tier` 非空）的行默认跳过 —— `--only-untagged` 让 100 分钟
    的长跑可以中断续跑（999 枚 × ~6s + 装载 48s ≈ 100 分钟）。

已知单向误判（记录而非掩盖）：whisper 对**非语音音效**（动物叫/电子音）会幻觉出普通话
句子（实测 zh p=0.993，文本"我家貓今早踩了三下鬥鐘…"）。误判方向只会把 nonmandarin
误读成 mandarin，而闸门只作用于 `nonmandarin_ref` —— 假 mandarin 不会误降级，
只会漏降级（漏的那枚仍有试听/P5 之外的兜底：合成结果进质量门）。

用法：
    python -X utf8 tools/voice_library/tag_language_tier.py                 # 看（dry-run，仍要跑模型）
    python -X utf8 tools/voice_library/tag_language_tier.py --limit 12      # 抽样
    python -X utf8 tools/voice_library/tag_language_tier.py --apply         # 写库（先出快照+回滚演练）
产物：agent-ops/language-tier/tag-<ts>.json（逐条判定）+ pre_tag_<ts>.db（写库前快照）
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

DB_PATH = REPO / "peiyinyuan" / "data" / "peiyinyuan.db"
if not DB_PATH.exists():                                   # 兼容从仓库根内直接跑
    DB_PATH = REPO / "data" / "peiyinyuan.db"
OUT_ROOT = REPO / "agent-ops" / "language-tier"
COLUMN = "audio_language_tier"


def _connect(write: bool = False) -> sqlite3.Connection:
    uri = f"file:{DB_PATH.as_posix()}?mode={'rw' if write else 'ro'}"
    conn = sqlite3.connect(uri, uri=True)
    # 活库带 WAL 且后端进程在并发读写：等锁 15s，别一撞就抛 database is locked
    conn.execute("PRAGMA busy_timeout=15000")
    conn.row_factory = sqlite3.Row
    return conn


def _has_column(conn) -> bool:
    return bool(conn.execute(f'PRAGMA table_info(voice_profiles)')) and any(
        r["name"] == COLUMN for r in conn.execute("PRAGMA table_info(voice_profiles)"))


def _sql_str(v) -> str:
    if v is None:
        return "NULL"
    return "'" + str(v).replace("'", "''") + "'"


def _csv_lower(value: str) -> list:
    """逗号分隔 -> 去空白/转小写/去空项（`--engine` 与 `--ref-not-like` 共用口径）。"""
    return [x.strip().lower() for x in str(value or "").split(",") if x.strip()]


def pick_rows(conn, args) -> list:
    """候选 = 有参考音频文件路径的行（语种档描述的就是这段音频）。"""
    sql = ("SELECT id, name, engine, source, language, category, meta, "
           "       audio_language_tier, reference_audio_path "
           "FROM voice_profiles WHERE reference_audio_path IS NOT NULL "
           "  AND reference_audio_path <> ''")
    params = []
    if args.engine:
        names = [e.strip().lower() for e in args.engine.split(",") if e.strip()]
        sql += f" AND lower(engine) IN ({','.join('?' * len(names))})"
        params += names
    if args.ref_like:
        sql += " AND lower(reference_audio_path) LIKE ?"
        params.append(f"%{args.ref_like.lower()}%")
    for sub in _csv_lower(args.ref_not_like):
        sql += " AND lower(reference_audio_path) NOT LIKE ?"
        params.append(f"%{sub}%")
    if args.only_untagged:
        sql += f" AND {COLUMN} IS NULL"
    sql += " ORDER BY id"
    if args.limit:
        sql += " LIMIT ?"
        params.append(int(args.limit))
    return conn.execute(sql, params).fetchall()


def detect_one(model, path: str) -> dict:
    """一枚参考音频 -> {lang, prob, tier}。解码失败/文件缺失由调用方先挡住。

    契约（本机 faster-whisper 实测）：`detect_language` 返回**三元组**
    `(language, language_probability, all_language_probs)` —— 按两元解包会
    `ValueError: too many values to unpack`，首跑就是这么 4/4 全失败的。
    """
    from faster_whisper.audio import decode_audio
    from modules.voice_manager.language_tier import tier_from_detection

    t0 = time.time()
    audio = decode_audio(path, sampling_rate=16000)
    lang, prob, all_probs = model.detect_language(audio)
    runner_up = ""
    if all_probs:
        ranked = sorted(all_probs, key=lambda kv: -kv[1])
        runner_up = ranked[1][0] if len(ranked) > 1 else ""
    return {"lang": str(lang or ""), "prob": round(float(prob), 4),
            "runner_up": runner_up,
            "tier": tier_from_detection(lang, prob),
            "seconds": round(time.time() - t0, 2)}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="真写库（默认只出清单）")
    ap.add_argument("--only-untagged", action="store_true",
                    help="只处理语种档为空的行（长跑中断后续跑用）")
    ap.add_argument("--engine", default="", help="逗号分隔的引擎筛选，如 openvoice,voxcpm")
    ap.add_argument("--ref-like", default="",
                    help="参考音频路径子串筛选（如 common_voice_en_，用于定向抽验）。"
                         "注意：库内 999 条路径**全部以反斜杠存储**，写 'refill/animal' "
                         "这种带正斜杠的子串会 0 命中（实测空跑过一轮）；用不带分隔符的"
                         "词（如 refill）或在 shell 里给足反斜杠")
    ap.add_argument("--ref-not-like", default="",
                    help="逗号分隔的参考音频路径子串**排除**。用途：把"
                         "必为普通话的语料族（aishell / 本地 refill 变体）摘出长跑，"
                         "只对可能非普通话的族做全量声学复判")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--model", default="small", help="faster-whisper 尺寸（small=int8 实测够用）")
    args = ap.parse_args()

    os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
    OUT_ROOT.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")

    ro = _connect()
    if not _has_column(ro):
        print(f"[中止] voice_profiles 无 {COLUMN} 列：先启动一次后端"
              f"（init_db 的 F-R4-3 轻量列迁移会自动 ALTER TABLE 补列）再跑本工具。")
        return 2
    rows = pick_rows(ro, args)
    total_tiered = ro.execute(
        f"SELECT COUNT(*) FROM voice_profiles WHERE {COLUMN} IS NOT NULL").fetchone()[0]
    print(f"[候选] {len(rows)} 行（全库已打标 {total_tiered} 行）"
          f" 筛选 engine={args.engine or '(不限)'} ref_like={args.ref_like or '(不限)'}"
          f" only_untagged={args.only_untagged}")

    records, skipped = [], []
    for r in rows:
        p = Path(r["reference_audio_path"])
        if not p.is_absolute():
            p = REPO / r["reference_audio_path"]
        if not p.is_file():
            skipped.append({"id": r["id"], "name": r["name"], "why": "文件不存在",
                            "path": r["reference_audio_path"]})
            continue
        records.append((r, str(p)))

    if not records:
        print(f"[结论] 可判候选 0 行（跳过 {len(skipped)} 行：见证据 JSON）")
        ro.close()
        _write_evidence(ts, args, [], skipped, {}, {}, applied=False)
        return 0

    from faster_whisper import WhisperModel
    t_load = time.time()
    model = WhisperModel(args.model, device="cpu", compute_type="int8")
    load_s = round(time.time() - t_load, 1)
    print(f"[模型] faster-whisper '{args.model}' int8/cpu 就绪 {load_s}s "
          f"待判 {len(records)} 枚 ≈ {len(records)*6/60:.1f} 分钟", flush=True)

    plans = []
    for i, (r, abs_ref) in enumerate(records, 1):
        t0 = time.time()
        try:
            d = detect_one(model, abs_ref)
        except Exception as e:
            print(f"  [{i}/{len(records)}] 判种失败 {r['name']}: "
                  f"{type(e).__name__}: {e}", flush=True)
            skipped.append({"id": r["id"], "name": r["name"],
                            "why": f"{type(e).__name__}: {e}"})
            continue
        declared = r["audio_language_tier"]
        plans.append({
            "id": r["id"], "name": r["name"], "engine": r["engine"],
            "category": r["category"], "declared_language": r["language"],
            "old_tier": declared, "new_tier": d["tier"],
            "acoustic": {"lang": d["lang"], "prob": d["prob"],
                         "runner_up": d.get("runner_up")},
            "ref": Path(r["reference_audio_path"]).name,
            "seconds": round(time.time() - t0, 2),
            "old_meta": r["meta"],
        })
        flag = "" if d["tier"] == declared else "  <- 改标"
        print(f"  [{i}/{len(records)}] {r['name'][:22]:22s} {r['engine']:9s} "
              f"p={d['prob']:.3f} {d['lang']:4s} -> {d['tier']}{flag}", flush=True)

    dist_new = Counter(p["new_tier"] for p in plans)
    changed = [p for p in plans if p["new_tier"] != p["old_tier"]]
    print(f"\n[分布] 本次判定: {dict(dist_new)}  需要改标 {len(changed)} 行 "
          f"（平均 {sum(p['seconds'] for p in plans)/max(1,len(plans)):.1f}s/枚）")
    print(f"[跳过] {len(skipped)} 行")

    if not args.apply:
        print("[dry-run] 未改动数据库。加 --apply 才写入。")
        ro.close()
        _write_evidence(ts, args, plans, skipped, dict(dist_new), {}, applied=False)
        return 0

    if not changed:
        print("[结论] 无改标需求，不执行任何写操作。")
        ro.close()
        _write_evidence(ts, args, plans, skipped, dict(dist_new), {}, applied=False)
        return 0

    # ---- 快照（活库带 WAL，禁 copy2，必须 backup API） ----
    snapshot = OUT_ROOT / f"pre_tag_{ts}.db"
    src = _connect()
    dst = sqlite3.connect(str(snapshot))
    src.backup(dst)
    dst.close()
    src.close()
    print(f"[快照] {snapshot.relative_to(REPO)}")

    # ---- 回滚 SQL：逐行还原两列旧值，先在快照上实跑一遍才算有回滚 ----
    rollback = [
        'UPDATE voice_profiles SET {col} = {old}, meta = {om} WHERE id = "{rid}";'.format(
            col=COLUMN, old=_sql_str(p["old_tier"]), om=_sql_str(p["old_meta"]),
            rid=p["id"])
        for p in changed
    ]
    before_ids = {p["id"]: p["old_tier"] for p in changed}
    ev = sqlite3.connect(str(snapshot))
    ev.row_factory = sqlite3.Row
    for p in changed:
        ev.execute(f"UPDATE voice_profiles SET {COLUMN} = ?, meta = ? WHERE id = ?",
                   (p["new_tier"], _meta_with_tier(p, p["new_tier"]), p["id"]))
    ev.commit()
    got_after = {r["id"]: r[COLUMN] for r in ev.execute(
        f'SELECT id, {COLUMN} FROM voice_profiles WHERE id IN '
        f'({",".join("?" * len(changed))})', tuple(before_ids))}
    not_written = [p["id"] for p in changed if got_after.get(p["id"]) != p["new_tier"]]
    assert not not_written, f"快照上写入未生效: {not_written[:5]}"
    for sql in rollback:
        ev.execute(sql)
    ev.commit()
    got_back = {r["id"]: r[COLUMN] for r in ev.execute(
        f'SELECT id, {COLUMN} FROM voice_profiles WHERE id IN '
        f'({",".join("?" * len(changed))})', tuple(before_ids))}
    bad = [i for i, want in before_ids.items() if got_back.get(i) != want]
    ev.close()
    if bad:
        print(f"[中止] 回滚演练失败 {len(bad)} 行（{bad[:5]}），活库未动。")
        return 2
    print(f"[演练] 快照上 写入→回滚 {len(changed)} 行逐行还原通过；活库未动")

    live = _connect(write=True)
    for p in changed:
        live.execute(f"UPDATE voice_profiles SET {COLUMN} = ?, meta = ? WHERE id = ?",
                     (p["new_tier"], _meta_with_tier(p, p["new_tier"]), p["id"]))
    live.commit()
    after_dist = {r["t"]: r["c"] for r in live.execute(
        f"SELECT COALESCE({COLUMN},'(NULL)') t, COUNT(*) c "
        f"FROM voice_profiles GROUP BY t")}
    live.close()
    ro.close()

    out = _write_evidence(ts, args, plans, skipped, dict(dist_new), after_dist,
                          applied=True, snapshot=snapshot, rollback=rollback)
    print(f"[完成] 改标 {len(changed)} 行 -> 全库分布 {after_dist}")
    print(f"[回滚] 单行还原 SQL 已落 {out.name} 的 rollback_sql 字段（已在快照上实跑通过）")
    return 0


def _meta_with_tier(plan: dict, tier: str):
    """把声学结论并进 meta.language_tier；meta 解析不开就**原样保留**不改。"""
    raw = plan.get("old_meta")
    try:
        meta = json.loads(raw) if isinstance(raw, str) else (raw or {})
        if not isinstance(meta, dict):
            return raw
    except Exception:
        return raw
    meta["language_tier"] = {
        "tier": tier,
        "basis": "acoustic",
        "declared_language": plan.get("declared_language"),
        "acoustic": plan.get("acoustic"),
        "tagged_at": datetime.now().isoformat(timespec="seconds"),
    }
    return json.dumps(meta, ensure_ascii=False)


def _write_evidence(ts, args, plans, skipped, dist_new, after_dist, *, applied,
                    snapshot=None, rollback=None) -> Path:
    out = OUT_ROOT / f"tag-{ts}.json"
    body = {
        "ts": ts,
        "args": vars(args),
        "db": str(DB_PATH),
        "model_load_note": "detect_language 只吃参考音频；language 列不参与判据",
        "judged": [{k: v for k, v in p.items() if k != "old_meta"} for p in plans],
        "skipped": skipped,
        "tier_distribution_this_run": dist_new,
        "tier_distribution_after": after_dist,
        "applied": applied,
        "snapshot": str(snapshot.relative_to(REPO)) if snapshot else None,
        "rollback_sql": rollback or [],
    }
    out.write_text(json.dumps(body, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[证据] {out.relative_to(REPO)}")
    return out


if __name__ == "__main__":
    sys.exit(main())
