"""
[P8] postprocess 阶段实测: 音量归一 / 去静音 / 淡入淡出 对真实产物的可测影响
运行: python qa/harness/run_p8_postprocess.py
前置: 先跑 run_p7_synth.py 生成 qa/round-1/artifacts/p7_segments/*.npy (真实人声)
"""
import sys
import json
import glob
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness_lib as H  # noqa: E402

import numpy as np  # noqa: E402

SEG_DIR = H.ARTIFACTS / "p7_segments"


def load_segments():
    out = []
    for f in sorted(glob.glob(str(SEG_DIR / "seg*.npy"))):
        meta = json.loads(Path(f.replace(".npy", ".json")).read_text(encoding="utf-8"))
        a = np.fromfile(f, dtype=np.float32)
        out.append({"index": len(out), "audio": a, "text": meta["text"],
                    "sr": meta["sr"], "duration_ms": len(a) / meta["sr"] * 1000.0})
    return out


def main():
    log = H.LOGS / "p8_postprocess.log"
    with H.Tee(log):
        su = H.Suite("P8-postprocess", "run_p8_postprocess.py")
        segs = load_segments()
        r = su.new("前置: 真实合成音频可用")
        r.evidence_add(SEG_DIR)
        if not segs:
            r.block("缺少 p7 真实音频段", f"{SEG_DIR} 为空, 请先运行 run_p7_synth.py")
            su.finish(exit_when_fail=False)
            return
        H.kv("段数", len(segs))
        H.kv("采样率", segs[0]["sr"])
        for s in segs:
            r.check(f"段{s['index']} 输入非静音", float(np.max(np.abs(s["audio"]))) > 1e-3,
                    f"peak={float(np.max(np.abs(s['audio']))):.4f} len={len(s['audio'])}")

        from modules.tts_engine.audio_postprocess import AudioPostProcessor
        sr = segs[0]["sr"]
        pp = AudioPostProcessor(sample_rate=sr)

        # ---------- 1: 去静音 ----------
        r = su.new("remove_silence: 首尾静音裁剪可测")
        for s in segs:
            a = s["audio"]
            b = pp.remove_silence(a)
            H.kv(f"  段{s['index']}", f"{len(a)/sr:.2f}s -> {len(b)/sr:.2f}s "
                                      f"(-{(1-len(b)/max(len(a),1))*100:.1f}%)")
            r.check(f"段{s['index']} 裁剪后不短于 50%",
                    len(b) >= len(a) * 0.5 and len(b) > 0,
                    f"{len(a)} -> {len(b)}")
        trimmed = [len(pp.remove_silence(s["audio"])) / len(s["audio"]) for s in segs]
        H.kv("裁剪比例", [round(x, 3) for x in trimmed])
        r.check("至少一段被实际裁剪", any(x < 0.999 for x in trimmed), str(trimmed))

        # ---------- 2: 音量归一 ----------
        r = su.new("normalize_volume: 峰值归一到 -3dBFS")
        for s in segs:
            a = s["audio"]
            b = pp.normalize_volume(a.copy())
            pk_in, pk_out = float(np.max(np.abs(a))), float(np.max(np.abs(b)))
            H.kv(f"  段{s['index']}", f"peak {pk_in:.4f} -> {pk_out:.4f} "
                                      f"({20*np.log10(max(pk_out,1e-9)):.2f} dBFS)")
            r.check(f"段{s['index']} 归一到 ~0.708(-3dB)", abs(pk_out - 0.708) < 0.02,
                    f"peak={pk_out:.4f}")
        # 每段独立归一 -> 段间响度跳变
        peaks_raw = [float(np.max(np.abs(s["audio"]))) for s in segs]
        gains = [0.708 / p if p > 1e-6 else 1.0 for p in peaks_raw]
        H.kv("各段施加的增益", [round(g, 2) for g in gains])
        spread = max(gains) / max(min(gains), 1e-9)
        r.check("段间增益差异温和(<3x)", spread < 3.0,
                f"增益跨度 {spread:.1f}x -> 逐段峰值归一使轻声句被放大(底噪抬升)、"
                f"响亮句被压低, 句间响度不连续")

        # ---------- 3: 淡入淡出 ----------
        r = su.new("apply_fade: 首末样本被渐弱")
        a = segs[0]["audio"].copy()
        b = pp.apply_fade(a, fade_in_ms=50, fade_out_ms=100)
        fi = int(0.050 * sr)
        fo = int(0.100 * sr)
        H.kv("淡入区首个样本", float(b[0]))
        H.kv("淡入区末样本", float(b[fi - 1]))
        H.kv("淡出区末样本", float(b[-1]))
        H.kv("淡出区起点", float(b[-fo]))
        r.check("淡入生效(首样本趋于0)", abs(float(b[0])) < abs(float(a[0])) + 1e-9,
                f"{float(a[0]):.6f} -> {float(b[0]):.6f}")
        r.check("淡出生效(末样本趋于0)", abs(float(b[-1])) < 0.05, f"{float(b[-1]):.6f}")
        r.check("长度不变", len(a) == len(b), f"{len(a)} vs {len(b)}")
        # 极短音频保护
        tiny = np.ones(int(0.03 * sr), dtype=np.float32) * 0.5
        tb = pp.apply_fade(tiny, fade_in_ms=50, fade_out_ms=100)
        H.kv("30ms 短音频 fade 后中段值", float(tb[len(tb)//2]))
        r.check("淡入淡出不吞掉极短音频中段", float(tb[len(tb)//2]) > 0.2,
                f"max_fade=len//2 保护生效, 中段={float(tb[len(tb)//2]):.3f}")

        # ---------- 4: 拼接 ----------
        r = su.new("concat_audios: gap 与总长度")
        arrs = [s["audio"] for s in segs]
        cat = pp.concat_audios(arrs, gap_ms=300)
        raw_sum = sum(len(a) for a in arrs)
        expect = raw_sum + 300 / 1000 * sr * (len(arrs) - 1)
        H.kv("段数和", raw_sum)
        H.kv("拼接后长度", f"{len(cat)} (期望 {int(expect)})")
        r.check("拼接长度=各段+gap", abs(len(cat) - expect) <= sr * 0.01,
                f"{len(cat)} vs {expect}")
        gap_samples = int(300 / 1000 * sr)
        # 真实 gap 窗口: 每段结束后的 gap_samples 个样本(最后一段后无 gap)
        cur = len(arrs[0])
        gap_peaks = [float(np.max(np.abs(cat[cur:cur + gap_samples]))) if gap_samples else 0.0]
        for a in arrs[1:]:
            cur += gap_samples + len(a)
            gap_peaks.append(float(np.max(np.abs(cat[cur:cur + gap_samples])))
                             if len(cat[cur:cur + gap_samples]) else 0.0)
        gap_peaks = gap_peaks[:-1]  # 末段之后的尾部不计
        H.kv("段间 gap 区段峰值(应为0)", [round(p, 6) for p in gap_peaks])
        r.check("段间 300ms 间隔存在(近静音)",
                all(p < 1e-3 for p in gap_peaks),
                f"gap_peaks={gap_peaks}")
        # 首段为空时的前导 gap
        lead = pp.concat_audios([np.array([], dtype=np.float32)] + arrs, gap_ms=300)
        r.check("首段为空不产生前导静音偏移",
                len(lead) == raw_sum + int(300 / 1000 * sr) * (len(arrs) - 1),
                f"len={len(lead)} 期望={raw_sum + int(300/1000*sr)*(len(arrs)-1)} "
                f"-> concat 用 i>0 判断加 gap, 空首段仍计入前导 gap")

        # ---------- 5: 生产 _stage_postprocess 端到端 ----------
        r = su.new("生产 _stage_postprocess 作用于真实 audio_arrays")
        from modules.workflow.conversion_workflow import ConversionWorkflow
        ctx = {"task_id": "harness-p8", "sample_rate": sr,
               "audio_arrays": [dict(s) for s in segs]}
        out = ConversionWorkflow()._stage_postprocess(ctx)
        procs = out["audio_arrays"]
        H.kv("输入段/输出段", f"{len(segs)} -> {len(procs)}")
        for s, o in zip(segs, procs):
            H.kv(f"  段{s['index']}", f"{len(s['audio'])} -> {len(o['audio'])} samples, "
                                      f"peak {float(np.max(np.abs(s['audio']))):.4f} -> "
                                      f"{float(np.max(np.abs(o['audio']))):.4f}, "
                                      f"duration_ms {s['duration_ms']:.0f} -> {o['duration_ms']:.0f}")
        r.check("全部段保留", len(procs) == len(segs), f"{len(procs)}")
        r.check("输出段峰值均落在 -3dBFS 附近",
                all(abs(float(np.max(np.abs(o["audio"]))) - 0.708) < 0.02 for o in procs),
                str([round(float(np.max(np.abs(o["audio"]))), 4) for o in procs]))
        r.check("duration_ms 已按裁剪后长度更新",
                all(abs(o["duration_ms"] - len(o["audio"]) / sr * 1000) < 1 for o in procs), "")

        # ---------- 6: 静音段的处理 ----------
        r = su.new("静音兜底段进入 postprocess 后是否被剔除")
        zeros = np.zeros(int(0.5 * sr), dtype=np.float32)
        z = pp.remove_silence(zeros.copy())
        H.kv("remove_silence(纯零)", f"{len(zeros)} -> {len(z)} samples")
        nz = pp.normalize_volume(zeros.copy())
        H.kv("normalize_volume(纯零) peak", float(np.max(np.abs(nz))))
        # PY-123 修复后改为真实行为断言(原为写死 FAIL 的占位登记):
        # 混入一个纯零段 -> 生产 _stage_postprocess 必须剔除并计数
        from modules.workflow.conversion_workflow import ConversionWorkflow as _CW
        mixed = [dict(s) for s in segs] + [
            {"index": 99, "audio": zeros.copy(), "text": "静音兜底段",
             "chapter_title": "第99章", "duration_ms": 500.0}]
        try:
            out2 = _CW()._stage_postprocess({"task_id": "harness-p8-silence",
                                             "sample_rate": sr, "audio_arrays": mixed})
            kept2 = out2["audio_arrays"]
            peaks2 = [round(float(np.max(np.abs(o["audio"]))), 4) for o in kept2]
            H.kv("混入静音段后 输入/保留", f"{len(mixed)} -> {len(kept2)}")
            H.kv("保留段峰值", str(peaks2))
            H.kv("剔除计数 dropped_silent_segments", out2.get("dropped_silent_segments"))
            r.check("纯零兜底段被 postprocess 剔除, 真实段全保留",
                    len(kept2) == len(segs) and all(p > 1e-3 for p in peaks2),
                    f"kept={len(kept2)}/{len(segs)} peaks={peaks2}")
        except Exception as e:
            r.fail("纯零兜底段被 postprocess 剔除", f"意外抛错 {type(e).__name__}: {e}")
        allz = [{"index": 0, "audio": zeros.copy(), "text": "全静音输入",
                 "chapter_title": "第0章", "duration_ms": 500.0}]
        try:
            _CW()._stage_postprocess({"task_id": "harness-p8-allzero",
                                      "sample_rate": sr, "audio_arrays": allz})
            r.fail("全静音输入应任务失败", "未抛错 -> 仍会产出静音成品")
        except Exception as e:
            H.kv("全静音输入的处理", f"{type(e).__name__}: {str(e)[:140]}")
            r.check("全静音输入 -> 抛错中止(不产出静音成品)", True,
                    f"{type(e).__name__} (PY-123/PY-117)")
        su.finish(exit_when_fail=False)


if __name__ == "__main__":
    main()
