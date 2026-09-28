"""Build the PeiYinYuan voice reference library from open-licensed corpora.

Pipeline
--------
1. probe each catalogued corpus through the reachable HuggingFace mirror
   (hf-mirror.com; huggingface.co itself does not resolve from this box)
2. pull short clips without downloading whole datasets:
      kind=parquet  one row group at a time over HTTP Range
      kind=files    one small audio object at a time
3. cut a 5-20 s speech window, resample to 48 kHz (clone reference) and
   22.05 kHz (preview), write 16 bit PCM WAV under data/resources/
4. record every clip with license + source_url in data/resource.db and in
   data/resources/manifest.jsonl.  `verified` stays 0 until verify_samples.py
   has reopened the bytes on disk.

Only permissively licensed corpora are catalogued (CC0 / CC-BY / Apache).
Non-commercial or gated corpora (RAVDESS, CREMA-D, ESD, MEAD, Emo-DB,
VoxCeleb) are intentionally excluded - see BLOCKERS.md for what that costs.

Idempotent: rerun any time, finished uids are skipped.

Examples
    python build_library.py --list-sources
    python build_library.py --probe                      # metadata only
    python build_library.py                              # full build
    python build_library.py --sources vctk_philgzl --limit 20
    python build_library.py --dry-run --budget-mb 300
"""

import argparse
import os
import re
import sys
import time
import traceback
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import rs_audio as A       # noqa: E402
import rs_db as D          # noqa: E402
import rs_net as N         # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
DEF_DB = ROOT / "data" / "resource.db"
DEF_OUT = ROOT / "data" / "resources"
LOG_DIR = Path(__file__).resolve().parent / "logs"

# --------------------------------------------------------------------- sources
SOURCES = [
    dict(
        key="vctk_philgzl", kind="parquet", repo="philgzl/vctk",
        license="CC-BY-4.0",
        license_url="https://creativecommons.org/licenses/by/4.0/",
        page="https://hf-mirror.com/datasets/philgzl/vctk",
        attribution="VCTK-Corpus, CSTR / University of Edinburgh (CC BY 4.0)",
        language="en", path_in=("data/", ".parquet"), path_skip=("mic2",),
        speaker_cols=("speaker_id", "speaker", "speaker_name", "path", "filename"),
        gender_cols=("gender", "sex"), age_cols=("age", "age_band"),
        emotion_cols=("emotion",), max_file_mb=90, max_rg_mb=24,
        per_source_voices=45, samples_per_voice=2, prefer_long=True,
        note="English multi-speaker, 48 kHz, per-microphone parquet chunks",
    ),
    dict(
        key="vctk_jspaulsen", kind="parquet", repo="jspaulsen/vctk",
        license="CC-BY-4.0",
        license_url="https://creativecommons.org/licenses/by/4.0/",
        page="https://hf-mirror.com/datasets/jspaulsen/vctk",
        attribution="VCTK-Corpus, CSTR / University of Edinburgh (CC BY 4.0)",
        language="en", tree_path="data",
        path_in=("data/", ".parquet"), path_skip=("mic2",),
        speaker_cols=("speaker_id", "speaker", "speaker_name", "path", "filename"),
        gender_cols=("gender", "sex"), age_cols=("age", "age_band"),
        emotion_cols=("emotion",), max_file_mb=700, max_rg_mb=80,
        per_source_voices=45, samples_per_voice=2, prefer_long=True,
        note="460-550 MB shards; only worth the Range traffic if it carries the "
             "per-speaker gender/age columns philgzl's audio+name layout lacks",
    ),
    dict(
        key="vctk_milana", kind="parquet", repo="Milana/vctk_dataset_no_unknown",
        license="CC-BY-4.0",
        license_url="https://creativecommons.org/licenses/by/4.0/",
        page="https://hf-mirror.com/datasets/Milana/vctk_dataset_no_unknown",
        attribution="VCTK-Corpus, CSTR / University of Edinburgh (CC BY 4.0)",
        language="en", tree_path="data",
        path_in=("data/", ".parquet"), path_skip=("mic2",),
        speaker_cols=("speaker_id", "speaker", "speaker_name", "path", "filename"),
        gender_cols=("gender", "sex"), age_cols=("age", "age_band"),
        emotion_cols=("emotion",), max_file_mb=700, max_rg_mb=80,
        per_source_voices=45, samples_per_voice=2, prefer_long=True,
        note="same corpus, filtered to speakers with known attributes; hub card "
             "declares no licence, so attribution rests on the upstream VCTK CC BY 4.0",
    ),
    dict(
        key="libritts_r", kind="parquet", repo="mythicinfinity/libritts_r",
        license="CC-BY-4.0",
        license_url="https://creativecommons.org/licenses/by/4.0/",
        page="https://hf-mirror.com/datasets/mythicinfinity/libritts_r",
        attribution="LibriTTS-R, derived from LibriVox public-domain readings (CC BY 4.0)",
        language="en", path_in=("data/", "clean", ".parquet"), path_skip=(),
        speaker_cols=("speaker_id", "speaker", "path", "filename", "id"),
        gender_cols=("gender",), age_cols=(), emotion_cols=(),
        max_file_mb=620, max_rg_mb=28,
        per_source_voices=40, samples_per_voice=2, prefer_long=True,
        note="audiobook narration style; speaker id parsed from the file name",
    ),
    dict(
        key="voices_libritts", kind="files", repo="sdialog/voices-libritts",
        license="Apache-2.0", license_url="https://www.apache.org/licenses/LICENSE-2.0",
        page="https://hf-mirror.com/datasets/sdialog/voices-libritts",
        attribution="sdialog/voices-libritts (Apache 2.0)",
        language="en", path_in=("audio/", ".wav"), path_skip=(),
        speaker_regex=r"/\d+_([^/]+)\.wav$",
        per_source_voices=30, samples_per_voice=2, max_obj_mb=8,
        note="one 1.4 MB wav per speaker chunk - the cheapest harvest available",
    ),
    dict(
        key="fleurs_zh", kind="parquet", repo="google/fleurs",
        license="CC-BY-4.0",
        license_url="https://creativecommons.org/licenses/by/4.0/",
        page="https://hf-mirror.com/datasets/google/fleurs",
        attribution="FLEURS, Google (CC BY 4.0)",
        language="zh", tree_path="parquet-data/cmn_hans_cn",
        path_in=("parquet-data/cmn_hans_cn", ".parquet"),
        path_skip=("train",),
        speaker_cols=("speaker_id", "path", "id"), gender_cols=("gender",),
        age_cols=(), emotion_cols=(), max_file_mb=700, max_rg_mb=24,
        per_source_voices=25, samples_per_voice=1,
        note="read Mandarin speech at 16 kHz; FLEURS carries no speaker key, so "
             "each accepted clip becomes its own voice entry",
    ),
    dict(
        key="fleurs_en", kind="parquet", repo="google/fleurs",
        license="CC-BY-4.0",
        license_url="https://creativecommons.org/licenses/by/4.0/",
        page="https://hf-mirror.com/datasets/google/fleurs",
        attribution="FLEURS, Google (CC BY 4.0)",
        language="en", tree_path="parquet-data/en_us",
        path_in=("parquet-data/en_us", ".parquet"), path_skip=("train",),
        speaker_cols=("speaker_id", "path", "id"), gender_cols=("gender",),
        age_cols=(), emotion_cols=(), max_file_mb=700, max_rg_mb=24,
        per_source_voices=25, samples_per_voice=1,
        note="read American English at 16 kHz",
    ),
    dict(
        key="cv17_zh", kind="parquet", repo="fsicoli/common_voice_17_0",
        license="CC0-1.0", license_url="https://creativecommons.org/publicdomain/zero/1.0/",
        page="https://hf-mirror.com/datasets/fsicoli/common_voice_17_0",
        attribution="Mozilla Common Voice 17.0 zh-CN (CC0)",
        language="zh", tree_path="audio/zh-CN/dev",
        path_in=("audio/zh-CN/dev", ".parquet"), path_skip=("invalid",),
        speaker_cols=("client_id", "speaker_id", "path"), gender_cols=("gender",),
        age_cols=("age",), emotion_cols=(), max_file_mb=220, max_rg_mb=24,
        per_source_voices=30, samples_per_voice=2,
        note="self-reported age+gender in the dev split; this repo uses "
             "train/dev/test directories, there is no 'validated' one",
    ),
    dict(
        key="cv17_en", kind="parquet", repo="fsicoli/common_voice_17_0",
        license="CC0-1.0", license_url="https://creativecommons.org/publicdomain/zero/1.0/",
        page="https://hf-mirror.com/datasets/fsicoli/common_voice_17_0",
        attribution="Mozilla Common Voice 17.0 en (CC0)",
        language="en", tree_path="audio/en/dev",
        path_in=("audio/en/dev", ".parquet"), path_skip=("invalid",),
        speaker_cols=("client_id", "speaker_id", "path"), gender_cols=("gender",),
        age_cols=("age",), emotion_cols=(), max_file_mb=220, max_rg_mb=24,
        per_source_voices=30, samples_per_voice=2,
        note="self-reported age bands give real 青年/中年/老年 coverage",
    ),
    dict(
        key="cv_fixie_en", kind="parquet", repo="fixie-ai/common_voice_17_0",
        license="CC0-1.0", license_url="https://creativecommons.org/publicdomain/zero/1.0/",
        page="https://hf-mirror.com/datasets/fixie-ai/common_voice_17_0",
        attribution="Mozilla Common Voice 17.0 (CC0), fixie-ai parquet export",
        language="en", tree_path="en",
        path_in=("en/", ".parquet"), path_skip=("invalid",),
        speaker_cols=("client_id", "speaker_id", "path"), gender_cols=("gender",),
        age_cols=("age",), emotion_cols=(), max_file_mb=430, max_rg_mb=60,
        per_source_voices=20, samples_per_voice=2,
        note="parquet export, one ~400 MB shard per split; only single row groups "
             "are pulled over HTTP Range, self-reported age+gender per utterance",
    ),
    dict(
        key="cv_fixie_zh", kind="parquet", repo="fixie-ai/common_voice_17_0",
        license="CC0-1.0", license_url="https://creativecommons.org/publicdomain/zero/1.0/",
        page="https://hf-mirror.com/datasets/fixie-ai/common_voice_17_0",
        attribution="Mozilla Common Voice 17.0 zh-CN (CC0), fixie-ai parquet export",
        language="zh", tree_path="zh-CN",
        path_in=("zh-cn/", ".parquet"), path_skip=("invalid",),
        speaker_cols=("client_id", "speaker_id", "path"), gender_cols=("gender",),
        age_cols=("age",), emotion_cols=(), max_file_mb=430, max_rg_mb=60,
        per_source_voices=20, samples_per_voice=2,
        note="Mandarin crowd speech at 16 kHz with self-reported age+gender - the "
             "best fit for a Chinese dubbing pipeline",
    ),
]
BY_KEY = {s["key"]: s for s in SOURCES}
SPK_NUM_RE = re.compile(r"(?:^|[^0-9])(\d{3,6})(?:[^0-9]|$)")


# ----------------------------------------------------------------------- utils
def tree_of(spec):
    """Repo listing for a source, scoped to `tree_path` when set.

    Common Voice and FLEURS ship every language in one repo; a recursive root
    listing there runs to tens of thousands of entries and stalls, while the
    handful of shards we actually want lives under one sub-directory.
    """
    tree = N.dataset_tree(spec["repo"], spec.get("tree_path", ""))
    if not tree:
        N.log("  ! %-14s 列目录为空 repo=%s path=%r" % (
            spec["key"], spec["repo"], spec.get("tree_path", "")))
        return []
    return tree


def slug(text):
    return A.safe_slug(text, 48)


def pick(row, names):
    for n in names or ():
        if n in row and row[n] not in (None, "", [], {}):
            return n, row[n]
    return None, None


def looks_like_path(value):
    s = str(value)
    return ("/" in s) or ("\\" in s) or bool(re.search(r"\.(wav|flac|mp3|ogg|opus)$", s, re.I))


def parse_speaker(value):
    s = str(value or "").replace("\\", "/").rsplit("/", 1)[-1]
    head = s.split("_")[0]
    if head.isdigit() and len(head) >= 2:
        return head
    m = SPK_NUM_RE.search(s)
    if m:
        return m.group(1)
    return slug(re.sub(r"\.(wav|flac|mp3|ogg|opus)$", "", s, flags=re.I))[:24] or "na"


def speaker_of(row, spec):
    col, val = pick(row, spec.get("speaker_cols", ()))
    if val is None:
        return "na", col
    return (parse_speaker(val) if looks_like_path(val) else slug(val)), col


class Ctx:
    def __init__(self, args):
        self.args = args
        self.db = None if args.dry_run else D.ResourceDB(args.db)
        self.out = Path(args.out)
        self.manifest = self.out / "manifest.jsonl"
        self.voices = {}
        self.n_voices = 0
        self.n_samples = 0
        self.skipped = 0
        self.errors = []
        self.trace = []
        if not args.dry_run:
            self.out.mkdir(parents=True, exist_ok=True)

    def full(self):
        return self.n_voices >= self.args.max_voices or (
            self.args.limit and self.n_samples >= self.args.limit)

    def room(self, need_mb=24):
        return N.BYTE_BUDGET - N.transferred() > need_mb * (1 << 20)

    def voice_for(self, spec, speaker_id, attrs):
        key = "%s:%s" % (spec["key"], speaker_id)
        if key in self.voices:
            return self.voices[key]
        rid = self.db.upsert_resource(spec["_resource"]) if self.db else None
        vid = None
        if self.db is not None:
            vid = self.db.upsert_voice(dict(
                voice_key=key, resource_id=rid, source_key=spec["key"],
                speaker_id=str(speaker_id), name_hint=attrs.get("name_hint"),
                gender=attrs.get("gender") or "unknown",
                age_band=attrs.get("age_band") or "unknown",
                language=attrs.get("language") or spec.get("language", "other"),
                style=attrs.get("style") or "unknown",
                emotion=attrs.get("emotion") or "unknown",
                attr_source=attrs.get("attr_source", ""),
                sample_count=0, verified_count=0))
            row = self.db.get_voice(key)
            if row is not None:
                self.n_voices += 1
                self.voices[key] = dict(voice_key=key, id=row["id"], resource_id=rid,
                                        samples=0, gender=row["gender"],
                                        age_band=row["age_band"], language=row["language"],
                                        style=row["style"], emotion=row["emotion"])
                return self.voices[key]
        self.n_voices += 1
        self.voices[key] = dict(voice_key=key, id=vid, resource_id=rid, samples=0,
                                **{k: attrs.get(k) for k in
                                   ("gender", "age_band", "language", "style", "emotion")})
        return self.voices[key]


def trace(ctx, key, target, status, note=""):
    if len(ctx.trace) < 20000:
        ctx.trace.append("%-16s %-7s %-58s %s" % (key, status, str(target)[:58], note))
    if ctx.db is not None and status in ("error", "skip"):
        ctx.db.log("build_library", status, ("%s:%s" % (key, target))[:120],
                   "info", 0, note[:400])


# --------------------------------------------------------------- clip ingest
def ingest_decoded(ctx, x, sr, spec, speaker_id, clip_name, attrs, src_fmt=""):
    """Window, slice, measure, write and register one decoded candidate."""
    args = ctx.args
    if ctx.full():
        return False
    a, b, info = A.speech_window(x, sr, args.min_dur, args.max_dur)
    if b <= a:
        ctx.skipped += 1
        trace(ctx, spec["key"], clip_name, "skip", info.get("reason", "no window"))
        return False
    seg = np.ascontiguousarray(x[a:b])
    m = A.metrics(seg, sr)
    if m["rms"] < A.RMS_FLOOR:
        ctx.skipped += 1
        trace(ctx, spec["key"], clip_name, "skip", "near-silent rms=%.5f" % m["rms"])
        return False
    if m["voiced_ratio"] < 0.15:
        ctx.skipped += 1
        trace(ctx, spec["key"], clip_name, "skip",
              "low voiced_ratio=%.3f" % m["voiced_ratio"])
        return False
    if m["clipping"] > max(40, int(seg.shape[0] * 0.0005)):
        ctx.skipped += 1
        trace(ctx, spec["key"], clip_name, "skip", "clipped=%d" % m["clipping"])
        return False

    final = dict(attrs)
    src_notes = []
    if final.get("gender") in (None, "unknown") and m["f0_median"]:
        g, _conf = A.estimate_gender_from_f0(m["f0_median"])
        if g != "unknown":
            final["gender"] = g
            src_notes.append("f0")
    if final.get("style") in (None, "", "unknown"):
        final["style"] = A.estimate_style(m)
        src_notes.append("prosody")
    final["gender"] = A.norm_gender(final.get("gender"))
    final["age_band"] = A.norm_age(final.get("age_band"))
    final["style"] = A.norm_style(final.get("style"))
    final["emotion"] = final.get("emotion") or final["style"]
    final["language"] = A.norm_language(final.get("language") or spec.get("language"))
    base_src = final.get("attr_source") or "source:none"
    final["attr_source"] = base_src + ("+" + "+".join(src_notes) if src_notes else "")

    if args.want_gender and final["gender"] not in args.want_gender:
        ctx.skipped += 1
        trace(ctx, spec["key"], clip_name, "skip",
              "gender=%s not in %s" % (final["gender"], "/".join(sorted(args.want_gender))))
        return False
    if args.want_age and final["age_band"] not in args.want_age:
        ctx.skipped += 1
        trace(ctx, spec["key"], clip_name, "skip",
              "age_band=%s not in %s" % (final["age_band"],
                                         "/".join(sorted(args.want_age))))
        return False

    uid = "%s:%s" % (spec["key"], clip_name)
    if ctx.db is not None and ctx.db.sample_exists(uid):
        return False
    voice = ctx.voice_for(spec, speaker_id, final)

    vdir = ctx.out / spec["key"] / slug(speaker_id)
    stem = slug(os.path.basename(clip_name).rsplit(".", 1)[0])[:56]
    rel_master = str((vdir / ("%s.wav" % stem)).relative_to(ROOT)).replace("\\", "/")
    rel_prev = str((vdir / ("%s_22k.wav" % stem)).relative_to(ROOT)).replace("\\", "/")
    size_bytes = 0
    if args.dry_run:
        N.log("  [dry-run] %-46s %5.1fs %s rms=%.4f" % (uid[:46], m["duration_s"],
                                                       final["gender"], m["rms"]))
    else:
        paths = A.make_master(seg, sr, str(vdir), stem)
        size_bytes = os.path.getsize(paths["master"]) + os.path.getsize(paths["preview"])
        rec = dict(
            uid=uid, voice_id=voice["id"], resource_id=voice["resource_id"],
            source_key=spec["key"], speaker_id=str(speaker_id),
            gender=final["gender"], age_band=final["age_band"],
            language=final["language"], style=final["style"], emotion=final["emotion"],
            path=rel_master, path_22k=rel_prev, sample_rate=A.MASTER_SR, channels=1,
            duration_s=m["duration_s"], rms=m["rms"], peak=m["peak"],
            noise_floor=m["noise_floor"], voiced_ratio=m["voiced_ratio"],
            f0_median=m["f0_median"], clipping=m["clipping"],
            src_sample_rate=int(sr), src_format=src_fmt or "",
            license=spec["license"],
            source_url=spec.get("current_url") or spec["page"], page_url=spec["page"],
            manifest_path=str(vdir.relative_to(ROOT)).replace("\\", "/"),
            verified=0, verify_note=None, created_at=D.now())
        ctx.db.insert_sample(rec)
        D.write_manifest_line(str(ctx.manifest), rec)
        ctx.db.conn.execute("UPDATE voices SET sample_count=sample_count+1 WHERE id=?",
                            (voice["id"],))
        ctx.db.conn.commit()
        voice["samples"] = voice.get("samples", 0) + 1
    N.log("  [+%3d] %-16s %-12s %-6s %-4s %-4s %5.1fs rms=%.4f f0=%5.1f %s" % (
        ctx.n_samples + 1, spec["key"], str(speaker_id)[:12], final["gender"][:6],
        final["age_band"][:4], final["language"], m["duration_s"], m["rms"],
        m["f0_median"], N._fmt(size_bytes) if size_bytes else ""))
    ctx.n_samples += 1
    return True


def attrs_from_row(spec, row):
    _gcol, gval = pick(row, spec.get("gender_cols", ()))
    _acol, aval = pick(row, spec.get("age_cols", ()))
    _ecol, eval_ = pick(row, spec.get("emotion_cols", ()))
    got = []
    gender = A.norm_gender(gval)
    age_band = A.norm_age(aval)
    style = A.norm_style(eval_)
    if gval is not None and gender != "unknown":
        got.append("gender")
    if aval is not None and age_band != "unknown":
        got.append("age")
    if eval_ is not None and style != "unknown":
        got.append("emotion")
    hint = row.get("text") or row.get("transcription") or row.get("raw_transcription")
    return dict(gender=gender, age_band=age_band, style=style,
                emotion=style, language=A.norm_language(row.get("language")
                                                        or spec.get("language")),
                attr_source="source:" + (",".join(got) if got else "none"),
                name_hint=(str(hint)[:40] if hint else None))


# -------------------------------------------------------------- parquet harves
def harvest_parquet(ctx, spec):
    repo = spec["repo"]
    tree = tree_of(spec)
    files = matching(tree, spec)
    files.sort(key=lambda t: t.get("size", 0))
    spec["_files_seen"] = len(files)
    N.log("- %-16s parquet matched=%d smallest=%s" % (
        spec["key"], len(files),
        N._fmt(files[0]["size"]) if files else "-"))
    if not files:
        return 0
    made = {}
    cap = spec.get("per_source_voices", 30)
    want = spec.get("samples_per_voice", ctx.args.samples_per_voice)
    for t in files:
        if len(made) >= cap or ctx.full() or not ctx.room(30):
            break
        if t.get("size", 0) / 1e6 > spec.get("max_file_mb", 200):
            continue
        url = N.resolve_url(repo, t["path"])
        spec["current_url"] = url
        pf = rr = None
        try:
            pf, rr = N.open_parquet(url)
        except Exception as exc:  # noqa: BLE001
            trace(ctx, spec["key"], t["path"], "error", "open %s" % exc)
            if rr:
                rr.close()
            continue
        try:
            names = pf.schema_arrow.names
            trace(ctx, spec["key"], t["path"], "seen", "cols=%s" % ",".join(names))
            acol = _audio_col(pf)
            if acol is None:
                trace(ctx, spec["key"], t["path"], "skip", "no audio column: %s" % names)
                break
            aux = [c for c in names if c != acol]
            meta = pf.metadata
            rg_from = max(0, int(ctx.args.rg_start))
            for rg in range(rg_from, min(meta.num_row_groups, rg_from + 8)):
                if len(made) >= cap or ctx.full():
                    break
                comp = N.row_group_bytes(meta.row_group(rg))
                if not ctx.room(comp / 1e6 + 1.0):
                    trace(ctx, spec["key"], t["path"], "skip",
                          "row_group %d = %.1f MB > 剩余预算 %.1f MB" % (
                              rg, comp / 1e6,
                              (N.BYTE_BUDGET - N.transferred()) / (1 << 20)))
                    break
                if comp / 1e6 > spec.get("max_rg_mb", 24):
                    trace(ctx, spec["key"], t["path"], "skip",
                          "row_group %d = %.1f MB > cap %s" % (
                              rg, comp / 1e6, spec.get("max_rg_mb")))
                    continue
                rows = pf.read_row_group(rg, columns=[acol] + aux).to_pylist()
                for row in rows:
                    # the audio bytes were paid for with the row group; per-row
                    # work is decode + slice + write, so no room() gate here
                    if len(made) >= cap or ctx.full():
                        break
                    spk, _c = speaker_of(row, spec)
                    if made.get(spk, 0) >= want:
                        continue
                    blob = row.get(acol)
                    data, hint = _audio_payload(blob, row)
                    if not data:
                        continue
                    try:
                        x, sr, fmt, _e = A.decode_bytes(data, hint or "")
                    except Exception as exc:  # noqa: BLE001
                        trace(ctx, spec["key"], str(row.get("path"))[:60], "skip",
                              "decode %s" % type(exc).__name__)
                        continue
                    base = os.path.basename(str(hint or row.get("path") or "clip"))
                    clip = "%s/%s" % (spk, base)[:76]
                    if ingest_decoded(ctx, x, sr, spec, spk, clip,
                                      attrs_from_row(spec, row), src_fmt=fmt):
                        made[spk] = made.get(spk, 0) + 1
                    if not spec.get("multi_speaker_file", True) and made:
                        break
        except N.BudgetExceeded:
            raise
        except Exception as exc:  # noqa: BLE001
            trace(ctx, spec["key"], t["path"], "error", "rowgroups %s" % exc)
            N.log("    ! %s %s" % (spec["key"], exc))
        finally:
            try:
                rr.close()
            except Exception:  # noqa: BLE001
                pass
    return len(made)


def _audio_col(pf):
    names = pf.schema_arrow.names
    for c in ("audio", "wav", "speech", "clip", "array"):
        if c in names:
            return c
    for c in names:
        try:
            if str(pf.schema_arrow.field(c).type).startswith("struct"):
                return c
        except Exception:  # noqa: BLE001
            continue
    return None


def _audio_payload(blob, row):
    if isinstance(blob, dict):
        return blob.get("bytes") or None, blob.get("path") or row.get("path") or ""
    if isinstance(blob, (bytes, bytearray)) and blob:
        return bytes(blob), row.get("path") or ""
    return None, None


def matching(tree, spec):
    ins = tuple(s.lower() for s in spec.get("path_in", ()))
    skp = tuple(s.lower() for s in spec.get("path_skip", ()))
    return [t for t in tree
            if t.get("type") != "directory"
            and all(s in t["path"].lower() for s in ins)
            and not any(s in t["path"].lower() for s in skp)]


# --------------------------------------------------------------- files harvest
def harvest_files(ctx, spec):
    repo = spec["repo"]
    tree = tree_of(spec)
    files = matching(tree, spec)
    spec["_files_seen"] = len(files)
    N.log("- %-16s audio objects=%d" % (spec["key"], len(files)))
    groups = {}
    rx = re.compile(spec["speaker_regex"]) if spec.get("speaker_regex") else None
    for t in files:
        p = t["path"].replace("\\", "/")
        spk = "na"
        if rx:
            m = rx.search(p)
            if m:
                spk = slug(m.group(1) if m.groups() else m.group(0))
        else:
            spk = parse_speaker(p)
        groups.setdefault(spk, []).append(t)
    cap = spec.get("per_source_voices", 30)
    want = spec.get("samples_per_voice", ctx.args.samples_per_voice)
    made = 0
    for spk, items in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        if made >= cap or ctx.full() or not ctx.room(16):
            break
        got = 0
        for t in items[:want * 3]:
            if got >= want or ctx.full() or not ctx.room(16):
                break
            if t.get("size", 0) / 1e6 > spec.get("max_obj_mb", 8):
                continue
            url = N.resolve_url(repo, t["path"])
            spec["current_url"] = url
            try:
                data, _ = N.fetch_bytes(url, max_bytes=int(spec.get("max_obj_mb", 8) * 1e6))
                x, sr, fmt, _e = A.decode_bytes(data, t["path"])
            except N.BudgetExceeded:
                raise
            except Exception as exc:  # noqa: BLE001
                trace(ctx, spec["key"], t["path"], "skip", "fetch/decode %s" % exc)
                continue
            attrs = dict(gender="unknown", age_band="unknown",
                         language=A.norm_language(spec.get("language")),
                         style="unknown", emotion="unknown",
                         attr_source="source:none", name_hint=spk[:40])
            clip = "%s/%s" % (spk, os.path.basename(t["path"]))[:76]
            if ingest_decoded(ctx, x, sr, spec, spk, clip, attrs, src_fmt=fmt):
                got += 1
        if got:
            made += 1
    return made


# ------------------------------------------------------------------------ probe
def probe_sources(ctx):
    N.log("=" * 78)
    N.log("SOURCE PROBE - metadata only, no audio transfer")
    for spec in SOURCES:
        if ctx.args.sources and spec["key"] not in ctx.args.sources:
            continue
        try:
            info = N.dataset_info(spec["repo"])
        except Exception as exc:  # noqa: BLE001
            N.log("%-16s %-38s UNREACHABLE %s" % (spec["key"], spec["repo"][:38], exc))
            continue
        if not info:
            N.log("%-16s %-38s gated/not-found" % (spec["key"], spec["repo"][:38]))
            continue
        card = info.get("cardData") or {}
        lic = card.get("license")
        if isinstance(lic, list):
            lic = "+".join(map(str, lic))
        if not lic:
            # the hub mirrors the YAML licence into tags even when cardData is empty
            tags = [t.get("label") if isinstance(t, dict) else str(t)
                    for t in (info.get("tags") or [])]
            lic = "+".join(t.split(":", 1)[1] for t in tags
                           if str(t).startswith("license:")) or None
            if lic:
                N.log("%-16s cardData 无 license，改用 hub tags: %s" % (spec["key"], lic))
        tree = tree_of(spec)
        files = matching(tree, spec)
        sizes = sorted(f.get("size", 0) for f in files)
        N.log("%-16s %-38s lic=%-11s gated=%-6s repo_files=%d matched=%d smallest=%s" % (
            spec["key"], spec["repo"][:38], str(lic)[:11], str(info.get("gated"))[:6],
            len(tree), len(files), N._fmt(sizes[0]) if sizes else "-"))
        if spec["kind"] == "parquet" and files:
            url = N.resolve_url(spec["repo"], files[0]["path"])
            try:
                pf, rr = N.open_parquet(url)
                rgs = [round(N.row_group_bytes(pf.metadata.row_group(i)) / 1e6, 1)
                       for i in range(min(4, pf.metadata.num_row_groups))]
                N.log("      columns: %s" % ", ".join(pf.schema_arrow.names))
                N.log("      row_groups=%d first_mb=%s" % (pf.metadata.num_row_groups, rgs))
                rr.close()
            except Exception as exc:  # noqa: BLE001
                N.log("      parquet open failed: %s" % exc)
    N.log("probe transferred: %s" % N._fmt(N.transferred()))


# ------------------------------------------------------------------------- main
def resource_rec(spec):
    return dict(source_key=spec["key"], provider="huggingface-mirror",
                dataset=spec["repo"], source_url=spec["page"], page_url=spec["page"],
                license=spec["license"], license_url=spec.get("license_url"),
                attribution=spec.get("attribution"), languages=spec.get("language"),
                harvest_kind=spec["kind"], status="pending", note=spec.get("note"),
                files_seen=0, bytes_downloaded=0, sample_count=0)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=str(DEF_DB))
    ap.add_argument("--out", default=str(DEF_OUT))
    ap.add_argument("--sources", type=lambda s: [x.strip() for x in s.split(",") if x.strip()],
                    default=[], help="comma separated source keys")
    ap.add_argument("--list-sources", action="store_true")
    ap.add_argument("--max-voices", type=int, default=150)
    ap.add_argument("--samples-per-voice", type=int, default=2)
    ap.add_argument("--per-source-voices", type=int, default=0,
                    help="覆盖源内音色上限（0=用源配置），一批多取可减少重复下载")
    ap.add_argument("--rg-start", type=int, default=0,
                    help="parquet 从第几个 row group 开始读（已入库的跳过，省流量）")
    ap.add_argument("--want-gender",
                    type=lambda s: [x.strip() for x in s.split(",") if x.strip()],
                    default=[], help="只收这些性别: male,female,neutral")
    ap.add_argument("--want-age",
                    type=lambda s: [x.strip() for x in s.split(",") if x.strip()],
                    default=[], help="只收这些年龄段: child,young,adult,senior")
    ap.add_argument("--min-dur", type=float, default=5.0)
    ap.add_argument("--max-dur", type=float, default=20.0)
    ap.add_argument("--budget-mb", type=int, default=2600)
    ap.add_argument("--limit", type=int, default=0, help="stop after N samples")
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--tree", default=None, metavar="REPO[:PATH]",
                    help="只读列出数据集目录一层（元数据，不下载音频）")
    ap.add_argument("--read", default=None, metavar="REPO:PATH",
                    help="只读抓取小文本文件，打印其中的许可条款行")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    if args.read:
        repo, _, sub = args.read.partition(":")
        url = N.resolve_url(repo, sub)
        data, _ = N.fetch_bytes(url, max_bytes=200000)
        text = data.decode("utf-8", "replace")
        hits = ("license", "licence", "cc0", "cc-by", "creativecommons", "apache",
                "public domain", "bsd", "mit", "attribut", "non-commercial", "nc ")
        N.log("read %s -> %s bytes, %d lines" % (url, N._fmt(len(data)),
                                                 text.count("\n")))
        for i, line in enumerate(text.splitlines(), 1):
            if any(h in line.lower() for h in hits):
                N.log("%5d| %s" % (i, line.strip()[:150]))
        return 0

    if args.tree:
        repo, _, sub = args.tree.partition(":")
        entries = N.list_tree("/api/datasets/%s/tree/main/%s" % (repo, sub),
                              None) or []
        N.log("tree %s/%s -> %d entries (non-recursive)" % (repo, sub, len(entries)))
        for t in sorted(entries, key=lambda e: (e["type"], e["path"])):
            N.log("  %-11s %-70s %s" % (t["type"], t["path"],
                                        N._fmt(t.get("size") or 0)))
        return 0

    if args.list_sources:
        for s in SOURCES:
            print("%-16s %-9s %-38s %-12s %s" % (s["key"], s["kind"], s["repo"],
                                                 s["license"], s["note"][:48]))
        return 0
    unknown = [k for k in args.sources if k not in BY_KEY]
    if unknown:
        print("未知来源: %s | 可用: %s" % (unknown, list(BY_KEY)))
        return 2
    if args.per_source_voices:
        for spec in SOURCES:
            if not args.sources or spec["key"] in args.sources:
                spec["per_source_voices"] = args.per_source_voices

    args.db = str(Path(args.db).resolve())
    args.out = str(Path(args.out).resolve())
    N.set_budget(args.budget_mb * (1 << 20))
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    ctx = Ctx(args)
    t0 = time.time()
    N.log("resource_scout | db=%s | out=%s | budget=%.2f GB | mode=%s" % (
        args.db, args.out, args.budget_mb / 1024.0,
        "probe" if args.probe else ("dry-run" if args.dry_run else "build")))
    if args.probe:
        probe_sources(ctx)
        return 0

    for spec in SOURCES:
        if args.sources and spec["key"] not in args.sources:
            continue
        if ctx.n_voices >= args.max_voices:
            N.log("max-voices reached (%d)" % args.max_voices)
            break
        if not ctx.room(16):
            N.log("byte budget exhausted")
            break
        spec["_resource"] = resource_rec(spec)
        N.log("=" * 78)
        n, status, err = 0, "ok", ""
        try:
            n = harvest_parquet(ctx, spec) if spec["kind"] == "parquet" else harvest_files(ctx, spec)
            status = "ok" if n else "empty"
        except N.BudgetExceeded as exc:
            status, err = "budget", str(exc)
            N.log("- %-16s BUDGET STOP %s" % (spec["key"], exc))
        except Exception as exc:  # noqa: BLE001
            status, err = "error", "%s: %s" % (type(exc).__name__, exc)
            ctx.errors.append("%s %s" % (spec["key"], err))
            N.log("- %-16s ERROR %s" % (spec["key"], err))
            traceback.print_exc(limit=2)
        N.log("- %-16s -> voices=%d samples=%d status=%s" % (
            spec["key"], n, ctx.n_samples, status))
        spec["_resource"].update(status=status, note=(spec.get("note") or "") +
                                 (" | " + err if err else ""),
                                 files_seen=spec.get("_files_seen", 0),
                                 bytes_downloaded=N.transferred())
        if ctx.db is not None:
            rid = ctx.db.upsert_resource(spec["_resource"])
            ctx.db.log("build_library", "source", spec["key"], status,
                       N.transferred(), "voices=%d %s" % (n, err[:180]))

    mins = (time.time() - t0) / 60.0
    N.log("=" * 78)
    N.log("done in %.1f min | voices=%d samples=%d skipped=%d errors=%d | transferred=%s" % (
        mins, ctx.n_voices, ctx.n_samples, ctx.skipped, len(ctx.errors),
        N._fmt(N.transferred())))
    lines = []
    if ctx.db is not None:
        ctx.db.recompute_voice_counts()
        for sql in ("SELECT COUNT(*) samples FROM voice_samples",
                    "SELECT COUNT(*) verified FROM voice_samples WHERE verified=1",
                    "SELECT COUNT(DISTINCT voice_id) voices FROM voice_samples",
                    "SELECT gender, COUNT(*) n FROM voice_samples GROUP BY gender",
                    "SELECT age_band, COUNT(*) n FROM voice_samples GROUP BY age_band",
                    "SELECT language, COUNT(*) n FROM voice_samples GROUP BY language",
                    "SELECT style, COUNT(*) n FROM voice_samples GROUP BY style",
                    "SELECT source_key, COUNT(*) n FROM voice_samples GROUP BY source_key"):
            lines.append("%-62s %s" % (sql[:62], ctx.db.rows(sql)))
        for spec in SOURCES:
            r = ctx.db.get_resource(spec["key"])
            if r:
                lines.append("resource %-16s status=%-7s samples=%-4s license=%s" % (
                    r["source_key"], r["status"], r["sample_count"], r["license"]))
        stamp = time.strftime("%Y%m%d_%H%M%S")
        (LOG_DIR / ("trace_%s.txt" % stamp)).write_text("\n".join(ctx.trace) + "\n",
                                                        encoding="utf-8")
        ctx.db.log("build_library", "run", ",".join(args.sources) or "ALL", "done",
                   N.transferred(), "voices=%d samples=%d skipped=%d" % (
                       ctx.n_voices, ctx.n_samples, ctx.skipped))
    with open(LOG_DIR / "build_report.txt", "a", encoding="utf-8") as fh:
        fh.write("=== run %s args=%s ===\n" % (D.now(), vars(args)))
        fh.write("voices=%d samples=%d skipped=%d errors=%d transferred=%s elapsed=%.1fmin\n"
                 % (ctx.n_voices, ctx.n_samples, ctx.skipped, len(ctx.errors),
                    N._fmt(N.transferred()), mins))
        fh.write("\n".join(lines) + "\n")
        for e in ctx.errors:
            fh.write("ERROR %s\n" % e)
        fh.write("\n")
    for l in lines:
        N.log(l)
    if ctx.db is not None:
        ctx.db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
