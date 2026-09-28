"""
[红线探针] 对 Round-1 真实成品音频做听感红线量化: 削波 / 长静音 / 逐段响度跳变
运行: python qa/harness/probe_audio_redline.py
对象: qa/round-1/artifacts/p7_http_output.wav (P7 HTTP 端到端真实产物)
      data/output/harness_p9_book_harness-p9-wav.wav (P9 package 产物)
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness_lib as H  # noqa: E402

import numpy as np  # noqa: E402
import soundfile as sf  # noqa: E402

TARGETS = [
    H.ARTIFACTS / "p7_http_output.wav",
    H.PROJECT_ROOT / "data" / "output" / "harness_p9_book_harness-p9-wav.wav",
]


def clip_ratio(data):
    """|x| >= 0.99 的采样占比 (16bit 满刻度附近) + 连续削波帧数。"""
    hot = np.abs(data) >= 0.99
    n = int(hot.sum())
    # 连续 >=3 个样本的削波串
    runs = longest = 0
    for v in hot:
        runs = runs + 1 if v else 0
        longest = max(longest, runs)
    return n, len(data), longest


def main():
    log = H.LOGS / "probe_audio_redline.log"
    with H.Tee(log):
        su = H.Suite("Redline-audio-probe", "probe_audio_redline.py")
        for t in TARGETS:
            r = su.new(f"成品体检 {t.name}")
            r.evidence_add(t)
            if not Path(t).exists():
                r.block("产物不存在", str(t))
                continue
            data, sr = sf.read(str(t), dtype="float32")
            if data.ndim > 1:
                data = data.mean(axis=1)
            st = H.audio_stats(t)
            for k in ("size", "sr", "duration_s", "peak", "rms_db", "active_ratio",
                      "longest_silence_s"):
                H.kv(f"  {k}", st.get(k))
            n, total, longest = clip_ratio(data)
            H.kv("满刻度样本(>=0.99)", f"{n}/{total} ({n/total:.4%})")
            H.kv("最长连续满刻度样本数", f"{longest} ({longest/sr*1000:.2f}ms)")
            r.check("无成片削波 (满刻度占比 <0.1%)", n / max(total, 1) < 0.001,
                    f"{n/total:.4%} 满刻度, 最长串 {longest/sr*1000:.2f}ms")
            r.check("峰值余量 >=1dBFS (成品不应顶格)", st.get("peak", 1.0) <= 0.891,
                    f"peak={st.get('peak')} ({20*np.log10(max(st.get('peak',1e-9),1e-9)):.2f} dBFS)"
                    f" <- package 把整体推到 -0.09dBFS, 与逐段 -3dB 归一叠加")
            # 句级响度台阶: 直接对各真实合成段施加产品的逐段峰值归一后比 RMS
            import json as _json
            import glob as _glob
            seg_dir = H.ARTIFACTS / "p7_segments"
            levels = []
            for f in sorted(_glob.glob(str(seg_dir / "seg*.npy"))):
                meta = _json.loads(Path(f.replace(".npy", ".json")
                                         ).read_text(encoding="utf-8"))
                a = np.fromfile(f, dtype=np.float32)
                pk = float(np.max(np.abs(a)))
                if pk <= 1e-6:
                    continue
                b = a * (0.708 / pk)  # 产品的 normalize_volume 目标
                levels.append((Path(f).stem,
                               float(20 * np.log10(float(np.sqrt(np.mean(b ** 2))) + 1e-12))))
            if len(levels) > 1:
                db = np.array([x[1] for x in levels])
                H.kv("逐段归一后句级响度(dBFS)",
                     ", ".join(f"{n}={v:.2f}" for n, v in levels))
                H.kv("句级响度跨度", f"{db.max()-db.min():.2f}dB")
                r.check("句间响度台阶 <=6dB", (db.max() - db.min()) <= 6.0,
                        f"跨度 {db.max()-db.min():.2f}dB -> 逐段按峰值归一不等于响度归一")
            else:
                r.block("真实合成段不足, 句级响度不可判", f"levels={levels}")
            # 诊断: 成品波形内 100ms 人声核窗 (floor=0.05) 的分布, 只记录不断言
            win = int(0.1 * sr)
            rms = [float(np.sqrt(np.mean(data[i:i + win] ** 2)))
                   for i in range(0, len(data) - win, win)]
            voiced = [r_ for r_ in rms if r_ > 0.05]
            if voiced:
                vdb = 20 * np.log10(np.array(voiced) + 1e-9)
                H.kv("[诊断] 人声核窗数/总窗数", f"{len(voiced)}/{len(rms)}")
                H.kv("[诊断] 人声核窗 RMS 分布",
                     f"min={vdb.min():.2f} p50={np.percentile(vdb,50):.2f} "
                     f"max={vdb.max():.2f} dBFS")
            sil = float((np.abs(data) < 1e-3).mean())
            H.kv("静音占比(<1e-3)", f"{sil:.2%}")
        su.finish(exit_when_fail=False)


if __name__ == "__main__":
    main()
