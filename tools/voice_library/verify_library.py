"""全库可用性复检 (VA-3) —— voice_profiles 里所有 source='sample' 行的落地后体检。

对每一条样本音色逐条核查（任何一项不过 = 不可用）:
    1. 主样本文件存在         (model_path / reference_audio_path)
    2. 试听文件存在           (sample_audio_path)
    3. 两个文件都可解码
    4. 主样本规格: 48kHz / 单声道 / PCM16
    5. 时长门:   0.3s <= dur <= 30s
    6. 峰值门:   0.02 <= peak <= 1.0

不合格的行 -> 删除该 voice_profiles 行 + 删除它的两个音频文件 (--apply 时才真删)。

WF-19 新增 `--demote`（降级模式，**不删任何东西**）:
    对每条样本行落盘后的 master + preview 重新量 clip_ratio / peak，
    用 ingest.clip_gate_reason() 这一个判定门复裁决：
      * 超阈 -> meta["verified"]=False + meta["verify_note"]=原因（计数 demoted）；
        已带本次降级标记的行不再重复写（计数 still_failing），复检因此幂等；
      * 之前被本工具降级、如今复测通过 -> 恢复 verified=True（计数 restored）；
      * 绝不删 DB 行、绝不删音频文件（那是 --apply 的语义）。
    背景：ingest 过去把 meta["verified"] 恒置 True，报告点名的
    3e31b26f(clip_ratio=0.016875) / 7c04a138(peak=1.0449) 都带着"已验收"标记。

设计口径:
    * 只碰 source='sample' 行, 绝不触碰 builtin/cloned/designed/downloaded 音色;
    * 只删这两个文件自身, 不碰 incoming/ 上游原始下载 (那是许可复核区, 另说);
    * 与 ingest.py 同一套解码/阈值常量, 复检标准 = 入库标准, 不放宽不收紧;
    * 默认 dry-run 只报告; --apply 才写库删文件 (与 ingest 的 --apply 语义一致);
    * 削顶判定单点在 ingest.clip_gate_reason() —— 本工具不改写它的阈值。

CLI:
    python tools/voice_library/verify_library.py                # dry-run, 只体检不删
    python tools/voice_library/verify_library.py --apply        # 真复检 + 删不合格行/文件
    python tools/voice_library/verify_library.py --apply --report v.json
    python tools/voice_library/verify_library.py --demote       # WF-19: 只降级 verified 标记
    python tools/voice_library/verify_library.py --demote --report v.json
    python tools/voice_library/verify_library.py --db data/peiyinyuan.db --library-root data/voices/library
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import soundfile as sf

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
import ingest as IN   # 复用解码 / 阈值常量 / open_session / _VoiceProfile

TARGET_SR = IN.TARGET_SR            # 48000
MIN_DUR = IN.MIN_DURATION_S         # 0.3
MAX_DUR = IN.MAX_DURATION_S         # 30.0
MIN_PEAK = IN.MIN_PEAK              # 0.02
MAX_PEAK = 1.0                      # 主样本已限幅 -> 不应 > 1.0


def _abs(p):
    if not p:
        return ""
    return p if os.path.isabs(p) else str(IN.ROOT / p)


# WF-19: 降级动作的归因标记（幂等判定用 —— 只认本工具写过的行）
DEMOTE_BY = "verify_library.py --demote"


def measure_file(path):
    """量一个**落盘后**文件的削顶指标 -> {peak, clip_ratio, duration_s, sample_rate}。

    clip 的定义与 ingest.qc_file 完全一致（|x| >= ingest.MAX_PEAK_ACCEPT 的采样占比），
    这样"入库时量的"与"复检时量的"是同一个量，不存在两套口径。
    读不出来/空流 -> None（存在性由 check_row 负责，不在此重复报）。
    """
    if not path or not os.path.isfile(path):
        return None
    try:
        x, sr = IN.decode_audio(path)
    except Exception:
        return None
    import numpy as np
    x = np.asarray(x)
    if x.ndim > 1:
        x = x.mean(axis=1)
    if x.size == 0 or not sr:
        return None
    peak = float(np.max(np.abs(x)))
    clip = float(np.mean(np.abs(x) >= IN.MAX_PEAK_ACCEPT))
    return {"peak": peak, "clip_ratio": clip,
            "duration_s": round(x.size / float(sr), 3), "sample_rate": int(sr)}


def clip_check(model_path, preview_path):
    """WF-19 复检门 -> (ok, [原因...], {master/preview: 指标})。

    master 与 preview 各自判一次，任一超阈即未验收（试听是用户实际听到的东西，
    它超标一样不能算"已验收"）。
    """
    reasons, metrics = [], {}
    for tag, path in (("master", _abs(model_path)), ("preview", _abs(preview_path))):
        m = measure_file(path)
        if m is None:
            continue
        metrics[tag] = m
        why = IN.clip_gate_reason(m)
        if why:
            reasons.append("%s:%s" % (tag, why))
    return (len(reasons) == 0), reasons, metrics


def check_row(model_path, preview_path):
    """-> (ok, [失败原因码...]). 逐门检查, 一次跑完全部以便归类统计。"""
    reasons = []
    master = _abs(model_path)
    preview = _abs(preview_path)

    # 1/2 存在性
    if not master or not os.path.isfile(master):
        reasons.append("master_missing")
    if not preview or not os.path.isfile(preview):
        reasons.append("preview_missing")
    if reasons:
        return False, reasons   # 文件都不在, 无从谈解码

    # 3 可解码 + 4 规格 (soundfile 直读元信息, 拿不到即 undecodable)
    for tag, path in (("master", master), ("preview", preview)):
        try:
            info = sf.info(path)
        except Exception:
            reasons.append("%s_undecodable" % tag)
            continue
        if tag == "master":
            if int(getattr(info, "samplerate", 0) or 0) != TARGET_SR:
                reasons.append("master_sr!=%d" % TARGET_SR)
            if int(getattr(info, "channels", 0) or 0) != 1:
                reasons.append("master_not_mono")
            if (getattr(info, "subtype", "") or "") != "PCM_16":
                reasons.append("master_not_pcm16:%s" % (getattr(info, "subtype", "?")))

    if any(r.endswith("_undecodable") for r in reasons):
        return False, reasons   # 解不开就别再算时长/峰值

    # 5/6 时长与峰值 (在主样本上判, 与入库同一标准)
    try:
        x, sr = IN.decode_audio(master)
    except Exception:
        reasons.append("master_decode_fail")
        return False, reasons
    dur = (x.size / float(sr)) if sr else 0.0
    import numpy as np
    peak = float(np.max(np.abs(x))) if x.size else 0.0
    if dur < MIN_DUR:
        reasons.append("too_short:%.3fs" % dur)
    elif dur > MAX_DUR:
        reasons.append("too_long:%.2fs" % dur)
    if not (peak == peak) or peak < MIN_PEAK:      # NaN-safe
        reasons.append("silent:peak=%.4f" % peak)
    elif peak > MAX_PEAK + 1e-6:
        reasons.append("peak_over_unity:%.4f" % peak)

    return (len(reasons) == 0), reasons


def verify_db(db_path, library_root, apply, report_path="", demote=False):
    """全库复检。

    apply  : 删除不可用行 + 其文件（原语义，不变）
    demote : WF-19 —— 只按削顶门改写 meta.verified，**不删行、不删文件**
    """
    session = IN.open_session(db_path)
    VP = IN._VoiceProfile()
    rows = session.query(VP).filter_by(source="sample").all()
    summary = {"checked": len(rows), "ok": 0, "deleted": 0,
               "files_removed": 0, "reasons": {}, "by_category_kept": {},
               "by_category_deleted": {}, "dry_run": not apply, "rows": [],
               # ---- WF-19 计数 ----
               "demote_mode": bool(demote), "demoted": 0, "still_failing": 0,
               "restored": 0, "clip_ok": 0, "clip_failing": 0,
               "clip_unmeasured": 0, "clip_reasons": {}}
    deleted_objs = []
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for r in rows:
        cat = (r.category or "?")
        ok, reasons = check_row(r.model_path or r.reference_audio_path,
                                r.sample_audio_path)
        # WF-19: 削顶门独立于"可用性门"——超标素材是"能播但没验收"，
        # 不是"坏文件"，所以绝不并入 reasons（否则 --apply 会把它删掉）。
        clip_ok, clip_reasons, clip_metrics = clip_check(
            r.model_path or r.reference_audio_path, r.sample_audio_path)
        rec = {"id": r.id, "name": (r.name or "")[:40], "category": cat,
               "ok": ok, "reasons": reasons,
               "clip_ok": clip_ok, "clip_reasons": clip_reasons,
               "clip_metrics": clip_metrics}
        summary["rows"].append(rec)
        if ok:
            summary["ok"] += 1
            summary["by_category_kept"][cat] = summary["by_category_kept"].get(cat, 0) + 1
            mark = "OK  "
        else:
            summary["deleted"] += 1
            summary["by_category_deleted"][cat] = summary["by_category_deleted"].get(cat, 0) + 1
            for code in reasons:
                key = code.split(":")[0]
                summary["reasons"][key] = summary["reasons"].get(key, 0) + 1
            mark = "DEL " if apply else "BAD "
            files = [_abs(r.model_path), _abs(r.reference_audio_path),
                     _abs(r.sample_audio_path)]
            if apply:
                for fp in files:
                    if fp and os.path.isfile(fp):
                        try:
                            os.remove(fp)
                            summary["files_removed"] += 1
                        except OSError as e:
                            summary.setdefault("file_errors", []).append(
                                "%s: %s" % (fp, str(e)[:60]))
                deleted_objs.append(r)

        # ---------------- WF-19: verified 裁决 ----------------
        if clip_ok:
            summary["clip_ok"] += 1
        elif not clip_metrics:
            summary["clip_unmeasured"] += 1        # 文件缺失/解不开：交回可用性门
        if not clip_ok:
            summary["clip_failing"] += 1
            for code in clip_reasons:
                key = code.split(":")[0].split("_")[0]
                summary["clip_reasons"][key] = summary["clip_reasons"].get(key, 0) + 1
        if demote:
            meta = dict(r.meta or {})
            was = meta.get("verified")
            mine = str(meta.get("demoted_by") or "") == DEMOTE_BY
            if not clip_ok:
                if mine and was is False:
                    # 已被本次复检降级过 -> 幂等：不重复写，只统计
                    summary["still_failing"] += 1
                else:
                    meta["verified"] = False
                    meta["verify_note"] = "; ".join(clip_reasons)[:200]
                    meta["verified_before_demote"] = was
                    meta["demoted_by"] = DEMOTE_BY
                    meta["demoted_at"] = now
                    meta["clip_measured"] = {
                        k: {"peak": round(v["peak"], 6),
                            "clip_ratio": round(v["clip_ratio"], 6)}
                        for k, v in clip_metrics.items()}
                    r.meta = meta          # 整体赋值，确保 JSON 列被标脏
                    summary["demoted"] += 1
                    mark = "DEMD" if not mine else "DEMB"
            elif was is False and mine:
                meta["verified"] = True
                meta["verify_note"] = "clip_gate_passed@recheck %s" % now
                meta.pop("demoted_by", None)
                meta.pop("demoted_at", None)
                r.meta = meta
                summary["restored"] += 1
                mark = "REST"
        print("[%s] %-30s %-11s %s" % (mark, rec["name"], cat,
                                       ("; ".join(reasons or clip_reasons))[:120]))
    if apply or demote:
        for r in deleted_objs:
            session.delete(r)
        try:
            session.commit()
        except Exception:
            session.rollback()
            raise
    else:
        session.rollback()
    session.close()
    summary["finished_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print("-" * 90)
    print("[verify] 复检 %d 行: 合格 %d / 不合格 %d%s" % (
        summary["checked"], summary["ok"], summary["deleted"],
        "" if apply else "  (dry-run, 未删)"))
    print("[verify] 删除原因分布: %s" % json.dumps(summary["reasons"], ensure_ascii=False))
    print("[verify] WF-19 削顶门: 通过 %d / 超阈 %d / 无从测量 %d %s" % (
        summary["clip_ok"], summary["clip_failing"],
        summary["clip_unmeasured"],
        json.dumps(summary["clip_reasons"], ensure_ascii=False)))
    if demote:
        print("[verify] --demote: 本次降级 %d 行 / 此前已降 %d 行 / 恢复 %d 行"
              "（不删行不删文件）" % (summary["demoted"], summary["still_failing"],
                                     summary["restored"]))
    if apply:
        print("[verify] 已删文件 %d 个" % summary["files_removed"])
        print("[verify] 保留 by category: %s" % json.dumps(summary["by_category_kept"], ensure_ascii=False))
        print("[verify] 删除 by category: %s" % json.dumps(summary["by_category_deleted"], ensure_ascii=False))
    else:
        print("[verify] 未 --apply: 没有删除任何行/文件%s"
              % ("（--demote 只改 meta.verified）" if demote else ""))
    if report_path:
        Path(report_path).parent.mkdir(parents=True, exist_ok=True)
        Path(report_path).write_text(json.dumps(summary, ensure_ascii=False, indent=1),
                                     encoding="utf-8")
        print("[verify] 明细 -> %s" % report_path)
    return summary


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=str(IN.DEFAULT_DB))
    ap.add_argument("--library-root", default=str(IN.DEFAULT_LIBRARY))
    ap.add_argument("--apply", action="store_true",
                    help="删除不合格行与其两个文件; 不给则只 dry-run 报告")
    ap.add_argument("--demote", action="store_true",
                    help="WF-19: 按削顶门把超阈行的 meta.verified 置 False"
                         "（不删行、不删文件；与 --apply 互斥，给了 --demote 就不删）")
    ap.add_argument("--report", default="", help="把逐条结果+汇总写成 json")
    args = ap.parse_args(argv)
    if args.demote and args.apply:
        print("[verify] --demote 与 --apply 互斥（降级不得删文件）—— 本次按 --demote 处理")
        args.apply = False
    print("[verify] db=%s library=%s apply=%s demote=%s" % (
        args.db, args.library_root, args.apply, args.demote))
    print("-" * 90)
    verify_db(args.db, Path(args.library_root), args.apply, args.report,
              demote=args.demote)
    return 0


if __name__ == "__main__":
    sys.exit(main())
