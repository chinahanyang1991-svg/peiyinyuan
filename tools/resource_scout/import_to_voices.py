"""Register verified resource-library samples as engine VoiceProfile rows.

Reads   data/resource.db  (voice_samples WHERE verified=1)
Writes  data/peiyinyuan.db voice_profiles   (only with --apply)

Field mapping - VoiceProfile (backend/models/voice.py)  <-  voice_samples
    name                  <-  "开放音色-<性别>-<年龄段>-<风格>-<source>-<speaker>"
    source                <-  "downloaded"   (开放授权上游语料，非用户克隆)
                               NB: PY-303 已按方案 B 闭环 —— voxcpm_adapter._restore_voices()
                               现同时接受 source in ("cloned","downloaded")，默认值可直接合成，
                               无需再退回 --source cloned。
    engine                <-  --engine (default "voxcpm"; VoxCPM2 clones through
                               generate(reference_wav_path=...), see voice_cloner.py)
    language              <-  language            (zh / en)
    gender                <-  gender              (male / female / neutral / None)
    age_group             <-  age_band mapped to child|young|adult|senior (None if unknown)
    style_tags            <-  [style tag, provenance tag, license tag]
    description           <-  human readable provenance + measured quality
    model_path            <-  same 48 kHz master wav as reference_audio_path
                              (voice_cloner.clone() writes both fields the same way)
    reference_audio_path  <-  path        (48 kHz mono PCM16, 5-20 s, the clone prompt)
    sample_audio_path     <-  path_22k    (22.05 kHz preview, cheap for the UI)
    is_downloaded         <-  1
    download_url          <-  source_url  (upstream object this clip came from)
    meta                  <-  {resource_uid, source_key, speaker_id, license,
                               source_url, page_url, duration_s, rms, peak,
                               sample_rate, f0_median, attr_source, verified_at}

Safety: the production database is never written unless --apply is passed.
Use --dry-run to print, or --db <copy> to test on a throwaway database.
"""

import argparse
import json
import os
import sys
import uuid
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import rs_db as D  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
LOG_DIR = Path(__file__).resolve().parent / "logs"

GENDER_CN = {"male": "男", "female": "女", "neutral": "中性", "unknown": "未知", None: "未知"}
AGE_CN = {"child": "儿童", "young": "青年", "adult": "中年", "senior": "老年",
          "unknown": "成年", None: "成年"}
AGE_TO_GROUP = {"child": "child", "young": "young", "adult": "adult",
                "senior": "senior", "unknown": None, None: None}
STYLE_TAGS = {"平静": ["平静", "沉稳", "旁白"], "开心": ["活泼", "明亮", "愉快"],
              "悲伤": ["低沉", "伤感"], "激昂": ["激昂", "有力", "气势"],
              "严肃": ["严肃", "权威"], "温柔": ["温柔", "知性"],
              "叙述": ["叙述", "标准", "通用"], "unknown": ["标准", "通用"]}
LICENSE_TAG = {"CC-BY-4.0": "CCBY音色", "CC0-1.0": "公有领域", "CC-BY-3.0": "CCBY音色",
               "CDLA-permissive-2.0": "CDLA", "Apache-2.0": "Apache"}

WIRING_NOTE = """
[接线说明] PY-303 已闭环（2026-09-21，智能体2 方案 B）：
  modules/tts_engine/engines/voxcpm_adapter.py :: _restore_voices()
    if v.source in ("cloned", "downloaded") and v.reference_audio_path
  -> 本工具默认 source="downloaded" 入库的行现在会被装进 _cloned_voices，
     合成时 synthesize() 据 voice_id 取 reference_wav_path 传给
     model.generate(reference_wav_path=...)，真实下载样本可直接应用。
  历史备选（无需再用）：
    A. --apply --source cloned —— 当年为绕开该缺口而不改生产代码的做法，
       代价是下载音色混入"我的克隆"列表；现由 source="downloaded" 保持区分。
  实测证据：qa/harness/tmp/py303/（wiring 25/25 + 真合成 wav 指标）。
"""

# ---------------------------------------------------------------------------
# PY-364 入库护栏（主修，防复发）
#
# 定罪现场：/online/download 通道把 hexgrad/Kokoro-82M 的样本以
# engine='voxcpm' 登记（id=c7b3ebd6-…，reference_audio_path=NULL），
# voxcpm 是零样本克隆引擎、只照参考音频说话（_restore_voices 的
# `and v.reference_audio_path` 把它挡在装载外）——可挑可绑，挑了却拿到
# voxcpm 默认声。本工具是同一条登记路径上的另一扇门（resource_scout 批量
# 入库），必须在**登记前**验两件事：
#   ① 要写成需要参考音频的引擎（克隆档）→ 本地参考 wav 必须真落在盘上且路径已写入；
#   ② 来源仓库若指向**别的引擎的模型仓**（Kokoro 仓样本登记成 voxcpm）→ 拒。
# 不满足即拒登记并打印明说为什么拒（不静默跳过、不退化成"照样入池"）。
# ---------------------------------------------------------------------------

# 需要真实参考音频才能发声的引擎（克隆/零样本档）；
# edgetts/kokoro/mosstts… 是预设说话人或另有语义，不在此列。
# 判据口径抄自 backend/api/voices.py:30-36 与 voxcpm_adapter._restore_voices。
REFERENCE_ENGINES = frozenset({"voxcpm", "openvoice", "gptsovits"})
MIN_REF_BYTES = 1024  # 低于 1KB 视为占位/空文件（对齐 cast_service 试听双门口径）

# owner/name(小写) -> 该模型仓库真正对应的引擎。克隆档引擎（voxcpm/openvoice/
# gptsovits）可吃任意语料仓的 wav；但**别的引擎的模型仓**（如 Kokoro-82M 之于
# kokoro）出来的样本冒充成需要克隆的引擎 = PY-364 的骗人形态，一律拒。
ENGINE_MODEL_REPOS = {
    "hexgrad/kokoro-82m": "kokoro",
    "indexteam/index-tts": "indextts",
    "index-tts/index-tts": "indextts",
    "openmoss/moss-tts": "mosstts",
    "rvc-boss/gpt-sovits": "gptsovits",
    "openbmb/voxcpm": "voxcpm",
    "myshell-ai/openvoice": "openvoice",
    "myshell-ai/openvoice-v2": "openvoice",
}

_REPO_HOSTS = ("huggingface.co", "hf-mirror.com", "github.com")


def repo_slug(*urls):
    """从一组 URL / repo 字符串里抽出最后一个 'owner/name'（小写），抽不到返回 None。"""
    import re
    slug = None
    for u in urls:
        s = str(u or "").strip().replace("\\", "/")
        if not s:
            continue
        m = re.search(r"(?:%s)/([^/?#\\]+)/([^/?#\\]+)" % "|".join(_REPO_HOSTS), s)
        if m:
            slug = "%s/%s" % (m.group(1), m.group(2))
            continue
        m = re.match(r"^([\w.-]+/[\w.-]+)", s)  # 裸 'owner/name'
        if m:
            slug = m.group(1)
    return slug.lower() if slug else None


def intake_gate(engine, reference_audio_path, meta=None, download_url=None):
    """PY-364 登记门。返回 None=放行；返回字符串=拒绝理由（调用方必须打印）。

    只拦"会被当成克隆档引擎使用却拿不出真参考音频"的行；
    正常在盘 wav + 无冲突仓库的行一律放行（防过度拦截）。
    """
    eng = str(engine or "").strip().lower()
    meta = meta or {}
    origin = meta.get("origin") or {}
    claimed_repo = repo_slug(meta.get("source_url"), meta.get("page_url"),
                             origin.get("repo"), download_url or meta.get("source_url"))
    owner = ENGINE_MODEL_REPOS.get(claimed_repo or "")
    if owner and eng and owner != eng:
        return ("来源仓库 %s 是引擎 %s 的模型仓，不能登记成 engine=%s 的音色"
                "（该样本只有 %s 引擎才发得出声）" % (claimed_repo, owner, eng, owner))
    if eng in REFERENCE_ENGINES:
        p = str(reference_audio_path or "").strip()
        if not p:
            return ("engine=%s 是零样本/克隆档，必须有本地参考音频，"
                    "但 reference_audio_path 为空——挑了也只会拿到引擎默认声 (PY-364)" % eng)
        if not os.path.isfile(p):
            return ("engine=%s 的本地参考音频不在盘上: %s (PY-364)" % (eng, p))
        if os.path.getsize(p) < MIN_REF_BYTES:
            return ("本地参考音频不足 %dB（疑似占位/空文件）: %s (PY-364)"
                    % (MIN_REF_BYTES, p))
    return None


def build_profiles(db, args):
    """Turn verified voice_samples rows into VoiceProfile-shaped dicts.

    Returns (picked, rejected); rejected = [(profile, 拒绝理由)] (PY-364 护栏)。
    """
    where, j = [], []
    if args.gender:
        where.append("s.gender=?")
        j.append(args.gender)
    if args.language:
        where.append("s.language=?")
        j.append(args.language)
    if args.style and args.style != "all":
        where.append("s.style=?")
        j.append(args.style)
    sql = """SELECT s.*, v.voice_key AS voice_key, v.attr_source AS attr_source,
                    r.attribution AS attribution,
                    r.license AS resource_license, r.source_url AS resource_source_url
             FROM voice_samples s
             JOIN voices v ON v.id=s.voice_id
             LEFT JOIN resources r ON r.id=s.resource_id
             WHERE s.verified=1"""
    if where:
        sql += " AND " + " AND ".join(where)
    sql += " ORDER BY s.source_key, s.speaker_id, s.duration_s DESC"
    rows = db.rows(sql, j)
    picked, rejected, seen = [], [], {}
    for r in rows:
        key = r["voice_key"]
        have = seen.get(key)
        if have is not None:
            if args.multiple_samples and len(have["_samples"]) < args.samples_per_voice:
                have["_samples"].append(r["path"])
            continue
        if args.one_per_voice is False and have is not None:
            continue
        master = r["path"] if os.path.isabs(r["path"]) else str(ROOT / r["path"])
        preview = r["path_22k"] if (r["path_22k"] and os.path.isabs(r["path_22k"])) \
            else (str(ROOT / r["path_22k"]) if r["path_22k"] else master)
        age_band = r["age_band"] or "unknown"
        style = r["style"] or "unknown"
        src = r["source_key"]
        spk = str(r["speaker_id"] or "")[:18]
        name = "开放音色-%s-%s-%s-%s-%s" % (
            GENDER_CN.get(r["gender"], "未知"), AGE_CN.get(age_band, "成年"),
            style, src.split("_")[0][:10], spk or "na")
        tags = list(STYLE_TAGS.get(style, ["标准"]))
        tags.append(("中文" if r["language"] == "zh" else "英文"))
        tags.append(LICENSE_TAG.get(r["license"], "开放授权"))
        prof = dict(
            id=str(uuid.uuid5(args.namespace, r["uid"])),
            name=name[:70],
            source=args.source,
            engine=args.engine,
            language=r["language"] or "zh",
            gender=r["gender"] if r["gender"] in ("male", "female", "neutral") else None,
            age_group=AGE_TO_GROUP.get(age_band),
            style_tags=tags,
            model_path=master,
            sample_audio_path=preview,
            reference_audio_path=master,
            description="开源语料参考音色 (%s / %s)。授权: %s；来源: %s；"
                        "参考音频 %.1fs @%dHz，RMS %.4f，峰值 %.3f。导入时间 %s" % (
                            src, spk or "na", r["license"], r["source_url"],
                            r["duration_s"] or 0.0, r["sample_rate"] or 48000,
                            r["rms"] or 0.0, r["peak"] or 0.0,
                            datetime.now().strftime("%Y-%m-%d %H:%M")),
            is_downloaded=1,
            download_url=r["source_url"],
            meta={
                "resource_uid": r["uid"], "source_key": src, "speaker_id": spk,
                "license": r["license"], "source_url": r["source_url"],
                "page_url": r["page_url"], "attribution": r["attribution"],
                "duration_s": r["duration_s"], "rms": r["rms"], "peak": r["peak"],
                "sample_rate": r["sample_rate"], "f0_median": r["f0_median"],
                "age_band": age_band, "style": style, "emotion": r["emotion"],
                "attr_source": r["attr_source"], "verified_at": r["verified_at"],
                "imported_by": "tools/resource_scout/import_to_voices.py",
            },
            _samples=[master], _ruid=r["uid"],
        )
        seen[key] = prof
        # PY-364 入库护栏：拒了就说清为什么拒，绝不静默、绝不退化成"照样入池"。
        reason = intake_gate(args.engine, prof["reference_audio_path"],
                             prof["meta"], prof.get("download_url"))
        if reason:
            rejected.append((prof, reason))
            print("[import] 拒绝登记 (PY-364 护栏) %s id=%s：%s" % (
                prof["name"], prof["id"], reason))
            continue
        picked.append(prof)
        if args.limit and len(picked) >= args.limit:
            break
    return picked, rejected


def apply_profiles(profiles, target_db, dry_run):
    """Insert/update voice_profiles rows using the production ORM."""
    if dry_run:
        return 0, 0
    sys.path.insert(0, str(ROOT))
    import backend.models.base as base
    base.get_db_path = lambda: str(target_db)          # bind ORM to chosen db
    from backend.models.voice import VoiceProfile
    session = base.get_session()
    created = updated = 0
    try:
        for p in profiles:
            existing = session.query(VoiceProfile).filter_by(id=p["id"]).first()
            payload = {k: v for k, v in p.items()
                       if k in VoiceProfile.__table__.columns.keys() and not k.startswith("_")}
            payload["meta"] = dict(p["meta"])
            payload["created_at"] = existing.created_at if existing else datetime.utcnow()
            payload["updated_at"] = datetime.utcnow()
            if existing:
                for k, v in payload.items():
                    setattr(existing, k, v)
                updated += 1
            else:
                session.add(VoiceProfile(**payload))
                created += 1
        session.commit()
    finally:
        session.close()
    return created, updated


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--resource-db", default=str(ROOT / "data" / "resource.db"))
    ap.add_argument("--db", default=str(ROOT / "data" / "peiyinyuan.db"),
                    help="target voice_profiles database")
    ap.add_argument("--dry-run", action="store_true", help="只打印，不写库")
    ap.add_argument("--apply", action="store_true", help="真正写入 --db 指定的库")
    ap.add_argument("--engine", default="voxcpm")
    ap.add_argument("--source", default="downloaded",
                    help="VoiceProfile.source: downloaded (默认) 或 cloned")
    ap.add_argument("--gender", default=None)
    ap.add_argument("--language", default=None)
    ap.add_argument("--style", default="all")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--one-per-voice", dest="one_per_voice", action="store_true", default=True)
    ap.add_argument("--multiple-samples", dest="multiple_samples", action="store_true")
    ap.add_argument("--samples-per-voice", type=int, default=3)
    ap.add_argument("--namespace", type=lambda s: uuid.UUID(s),
                    default=uuid.UUID("6f6f6f6f-0000-4000-8000-0e57c0000001"),
                    help="stable uuid5 namespace so reruns are idempotent")
    ap.add_argument("--report", default=str(LOG_DIR / "import_report.json"))
    args = ap.parse_args(argv)

    if not os.path.exists(args.resource_db):
        print("[import] 资源库不存在: %s (先运行 build_library.py)" % args.resource_db)
        return 2
    db = D.ResourceDB(args.resource_db)
    stats = db.one("SELECT COUNT(*) n, SUM(verified) v FROM voice_samples")
    print("[import] 资源库: 样本 %s 条 / verified %s 条 | db=%s" % (
        stats["n"], stats["v"] or 0, args.resource_db))
    profiles, rejected = build_profiles(db, args)
    print("[import] 可注册音色 %d 个 -> VoiceProfile (engine=%s source=%s)%s" % (
        len(profiles), args.engine, args.source,
        "；护栏拒登记 %d 个 (PY-364)" % len(rejected) if rejected else ""))
    print("-" * 78)
    for i, p in enumerate(profiles, 1):
        print("%3d. %-40s id=%s" % (i, p["name"][:40], p["id"]))
        print("     lang=%-3s gender=%-8s age=%-8s tags=%s" % (
            p["language"], p["gender"], p["age_group"], "/".join(p["style_tags"])))
        print("     ref =%s" % p["reference_audio_path"])
        print("     aud =%s" % p["sample_audio_path"])
        print("     lic =%s | url=%s" % (p["meta"]["license"], str(p["meta"]["source_url"])[:70]))
    print("-" * 78)
    if args.apply and args.dry_run:
        print("[import] --dry-run 与 --apply 同时给出，按 dry-run 处理")
    if not args.apply:
        print("[import] dry-run 模式：未写入任何数据库。加 --apply 才会写 %s" % args.db)
        created = updated = 0
    else:
        created, updated = apply_profiles(profiles, args.db, False)
        print("[import] 写入 %s : 新建 %d, 更新 %d" % (args.db, created, updated))

    print(WIRING_NOTE)

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    Path(args.report).write_text(json.dumps(dict(
        at=D.now(), resource_db=args.resource_db, target_db=args.db,
        applied=bool(args.apply and not args.dry_run), created=created, updated=updated,
        rejected=[{"name": p["name"], "id": p["id"], "reason": why}
                  for p, why in rejected],
        profiles=[{k: v for k, v in p.items() if not k.startswith("_")} for p in profiles],
    ), ensure_ascii=False, indent=1), encoding="utf-8")
    db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
