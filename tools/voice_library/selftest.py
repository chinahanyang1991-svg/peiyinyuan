"""离线自检 (VA-3) —— 不联网, 用 numpy 合成样本走完整 ingest 质检/入库/幂等路径。

断言:
  1) 3 个合成 wav -> 2 入库 + 1 拒收 (静音坏样本);
  2) 拒因分类正确 (silent);
  3) 主样本落盘为 48kHz 单声道 PCM16, 试听副本 <= 5s;
  4) 幂等: 同一 manifest 重跑 0 新增 / 全 skip; 同批内重复条目也只入一次;
  5) dry-run 不写盘、不写库。

自检库文件写在系统临时目录, 绝不碰 data/peiyinyuan.db。
运行: cd 项目根 && PYTHONIOENCODING=utf-8 python tools/voice_library/selftest.py
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

import numpy as np
import soundfile as sf

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
import ingest as IN  # noqa: E402

FADE = 0.02


def _tone(sr: int, seconds: float, f0: float, peak: float, voice: str = "sine") -> np.ndarray:
    """合成一个有谐波/包络的"类音色"信号, 避免纯音被当成脉冲。"""
    t = np.arange(int(sr * seconds), dtype=np.float32) / sr
    harm = {"sine": [(1.0, 1.0), (2.0, 0.35), (3.5, 0.12)],
            "buzz": [(1.0, 1.0), (2.0, 0.6), (3.0, 0.45), (4.0, 0.3),
                     (5.0, 0.22), (7.0, 0.12)]}[voice]
    x = np.zeros_like(t)
    for mult, amp in harm:
        x += amp * np.sin(2 * np.pi * f0 * mult * t).astype(np.float32)
    x += 0.02 * (np.random.default_rng(7).random(t.size).astype(np.float32) - 0.5)
    nf = max(int(sr * FADE), 1)
    env = np.ones(t.size, dtype=np.float32)
    env[:nf] = np.linspace(0, 1, nf, dtype=np.float32)
    env[-nf:] *= np.linspace(1, 0, nf, dtype=np.float32)
    x *= env
    m = float(np.max(np.abs(x))) or 1.0
    return (x * (peak / m)).astype(np.float32)


def synth_cases(wav_dir: Path) -> list:
    """3 个假 wav: 2 个好样本(不同采样率/声道) + 1 个静音坏样本。"""
    wav_dir.mkdir(parents=True, exist_ok=True)
    good_a = wav_dir / "good_a_22050_stereo.wav"
    good_b = wav_dir / "good_b_8000_long.wav"
    bad = wav_dir / "bad_silent.wav"
    sf.write(str(good_a), np.stack([_tone(22050, 2.0, 220.0, 0.62, "buzz"),
                                    _tone(22050, 2.0, 218.0, 0.60, "buzz")], axis=1),
             22050, subtype="PCM_16")
    sf.write(str(good_b), _tone(8000, 12.0, 90.0, 0.31, "sine"), 8000, subtype="PCM_16")
    rng = np.random.default_rng(3)
    sf.write(str(bad), (rng.random(int(48000 * 1.5)) - 0.5).astype(np.float32) * 1e-4,
             48000, subtype="PCM_16")
    return [
        {"category": "electronic", "name": "自检-合成蜂鸣A", "local_file": str(good_a),
         "page_url": "https://selftest.local/electronic/good-a",
         "source_url": "https://selftest.local/electronic/good-a.wav",
         "license": "CC0", "attribution": "selftest synthetic", "duration_s": 2.0},
        {"category": "mechanical", "name": "自检-合成低频B", "local_file": str(good_b),
         "page_url": "https://selftest.local/mechanical/good-b",
         "source_url": "https://selftest.local/mechanical/good-b.wav",
         "license": "CC BY 4.0", "attribution": "selftest synthetic", "duration_s": 12.0},
        {"category": "animal", "name": "自检-静音坏样本", "local_file": str(bad),
         "page_url": "https://selftest.local/animal/silent",
         "source_url": "https://selftest.local/animal/silent.wav",
         "license": "Public domain", "attribution": "selftest synthetic",
         "duration_s": 1.5},
    ]


def _count_db(db_path: str):
    s = IN.open_session(db_path)
    from backend.models.voice import VoiceProfile
    try:
        rows = s.query(VoiceProfile).filter_by(source="sample").all()
        return {r.id: r for r in rows}
    finally:
        s.close()


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="voice_library_selftest_"))
    fails = []

    def check(label, cond, detail=""):
        print("[%s] %s %s" % ("PASS" if cond else "FAIL", label, detail))
        if not cond:
            fails.append(label)

    try:
        items = synth_cases(tmp / "wav")
        lib = tmp / "library"
        db = tmp / "selftest.db"

        print("=== 1. dry-run: 只质检, 不写盘不写库 ===")
        st = IN.ingest_manifest(items, apply=False, library_root=lib, db_path=str(db))
        check("dry-run 不建 library 目录", not lib.exists() or not any(lib.rglob("*.wav")))
        check("dry-run 不建 db 文件", not db.exists())
        check("dry-run accepted=2", st["accepted"] == 2, str(st["by_category"]))
        check("dry-run rejected=1", st["rejected"] == 1)
        check("拒因=silent", list(st["reject_reasons"]) == ["silent"],
              json.dumps(st["reject_reasons"], ensure_ascii=False))

        print("=== 2. apply: 真实入库 ===")
        st = IN.ingest_manifest(items, apply=True, library_root=lib, db_path=str(db))
        check("accepted=2", st["accepted"] == 2, str(st["by_category"]))
        check("rejected=1", st["rejected"] == 1)
        check("拒收条目名正确",
              st["rejected_items"][0]["name"] == "自检-静音坏样本",
              str(st["rejected_items"]))
        rows = _count_db(str(db))
        check("DB source=sample 行数=2", len(rows) == 2, "n=%d" % len(rows))
        check("DB 类别分布", sorted(r.category for r in rows.values()) ==
              ["electronic", "mechanical"])
        check("入库统一 language=None / source=sample / engine=sample",
              all(r.language is None and r.source == "sample" and r.engine == "sample"
                  for r in rows.values()))
        check("入库 meta.verified 与 ingested_by",
              all(r.meta["verified"] is True and r.meta["ingested_by"] == "voice_library_v1"
                  for r in rows.values()))
        check("model_path == reference_audio_path",
              all(r.model_path == r.reference_audio_path for r in rows.values()))
        check("style_tags 含分类中文标签/许可标签/音效库",
              all(t in r.style_tags for r in rows.values()
                  for t in (IN.CATEGORIES[r.category], "音效库")),
              str([r.style_tags for r in rows.values()]))

        print("=== 3. 落盘音频规格 ===")
        ok_master = ok_prev = True
        for r in rows.values():
            xm, srm = sf.read(r.model_path, dtype="float32", always_2d=True)
            xp, srp = sf.read(r.sample_audio_path, dtype="float32", always_2d=True)
            if not (srm == 48000 and xm.shape[1] == 1 and r.model_path.endswith(".wav")
                    and Path(r.model_path).parent.name == r.category):
                ok_master = False
            if not (srp == 48000 and xp.shape[1] == 1 and xp.shape[0] / 48000 <= 5.001):
                ok_prev = False
            info = sf.info(r.model_path)
            if info.subtype != "PCM_16":
                ok_master = False
        check("主样本 48kHz/单声道/PCM16/<library>/<category>/<uuid>.wav", ok_master)
        check("试听副本存在且 <=5s 单声道", ok_prev)
        check("主样本目录按类分目录",
              all(Path(r.model_path).parent.name in IN.CATEGORIES for r in rows.values()))
        check("试听副本目录 previews",
              all(Path(r.sample_audio_path).parent.name == "previews"
                  for r in rows.values()))

        print("=== 4. 幂等 ===")
        again = IN.ingest_manifest(items, apply=True, library_root=lib, db_path=str(db))
        check("重跑 0 新增", again["accepted"] == 0, str(again))
        check("重跑全 skip", again["skipped"] == 2 and len(_count_db(str(db))) == 2,
              "skipped=%d rows=%d" % (again["skipped"], len(_count_db(str(db)))))
        dup_batch = IN.ingest_manifest(items[:1] + items[:1], apply=True,
                                       library_root=lib, db_path=str(db))
        check("同批内重复也只 skip, DB 行数不变",
              dup_batch["accepted"] == 0 and dup_batch["skipped"] == 2
              and len(_count_db(str(db))) == 2,
              "accepted=%d skipped=%d" % (dup_batch["accepted"], dup_batch["skipped"]))
        b2 = IN.ingest_manifest(items[:1] + [{**items[0], "page_url": "",
                                              "source_url": "https://selftest.local/x.wav"}],
                               apply=True, library_root=lib, db_path=str(db))
        check("新 page_url 条目正常新增", b2["accepted"] == 1 and
              len(_count_db(str(db))) == 3, "accepted=%d" % b2["accepted"])

        print("=== 5. 其它质检门 (超时长/超短/坏类别/坏文件) ===")
        longp = tmp / "wav" / "long.wav"
        sf.write(str(longp), _tone(48000, 31.0, 100.0, 0.5), 48000, subtype="PCM_16")
        shortp = tmp / "wav" / "short.wav"
        sf.write(str(shortp), _tone(48000, 0.1, 900.0, 0.5), 48000, subtype="PCM_16")
        brokenp = tmp / "wav" / "broken.wav"
        brokenp.write_bytes(b"this is definitely not a wav file" * 200)
        gate = [{"category": "anime", "name": "gate-long", "local_file": str(longp),
                 "page_url": "https://selftest.local/gate/long"},
                {"category": "anime", "name": "gate-short", "local_file": str(shortp),
                 "page_url": "https://selftest.local/gate/short"},
                {"category": "anime", "name": "gate-broken", "local_file": str(brokenp),
                 "page_url": "https://selftest.local/gate/broken"},
                {"category": "not_a_category", "name": "gate-bcat",
                 "local_file": str(shortp), "page_url": "https://selftest.local/gate/bcat"},
                {"category": "human", "name": "gate-nofile",
                 "page_url": "https://selftest.local/gate/nofile"}]
        st5 = IN.ingest_manifest(gate, apply=True, library_root=lib, db_path=str(db))
        check("超时长/超短/坏文件/坏类别/缺文件 全部拒收且 0 入库",
              st5["accepted"] == 0 and st5["rejected"] == 5,
              json.dumps(st5["reject_reasons"], ensure_ascii=False))
        check("拒因覆盖 too_long/too_short/undecodable/bad_category/no_local_file",
              {"too_long", "too_short", "undecodable", "bad_category",
               "no_local_file"} <= set(st5["reject_reasons"]),
              json.dumps(st5["reject_reasons"], ensure_ascii=False))
        check("坏样本一条也没进库", len(_count_db(str(db))) == 3)
        print("-" * 72)
        print("selftest 结论: %s (%d 项失败)" % ("全部通过" if not fails else "有失败", len(fails)))
        if fails:
            print("失败项: " + ", ".join(fails))
        return 1 if fails else 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
