"""
[C3] 混音实测: modules/mixer/audio_mix.py + bgm_store.py
运行: set PYTHONIOENCODING=utf-8 && python qa/harness/run_c3_mix.py

真实路径: 直接调用生产 AudioMix（与批次 B 将挂到 P8 末尾的是同一对象），
         BGM/音效走 data/bgm 的真实占位素材（numpy 合成并落盘 wav，soundfile 读回）。
         人声输入用 numpy 合成的两段伪波形（本工程不依赖 TTS，混音器只认 ndarray）。
判定: exit 0 = ALL_GREEN；结果 JSON 落 qa/round-1/artifacts/result_C3-mix.json
"""
import ast
import json
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness_lib as H  # noqa: E402

SR = 48000
CEILING = float(10 ** (-1.0 / 20))          # -1 dBFS
RNG = np.random.default_rng(20260920)


def synth_voice(seconds: float, f0: float = 170.0, amp: float = 0.25,
                syllable_hz: float = 3.2) -> np.ndarray:
    """伪人声：基频 + 2/3 次谐波 + 音节包络 + 轻微噪声（模拟 P8 输出的单声道段）。"""
    n = int(round(seconds * SR))
    if n == 0:
        return np.zeros(0, dtype=np.float32)
    t = np.arange(n, dtype=np.float32) / SR
    body = (np.sin(2 * np.pi * f0 * t)
            + 0.5 * np.sin(2 * np.pi * 2 * f0 * t)
            + 0.22 * np.sin(2 * np.pi * 3 * f0 * t))
    env = (0.55 + 0.45 * np.sin(2 * np.pi * syllable_hz * t)) ** 2
    noise = RNG.standard_normal(n).astype(np.float32) * 0.004
    return (body * env * amp + noise).astype(np.float32)


def silence(seconds: float) -> np.ndarray:
    return np.zeros(int(round(seconds * SR)), dtype=np.float32)


def rms_db(x: np.ndarray) -> float:
    if x is None or len(x) == 0:
        return float("-inf")
    return float(20 * np.log10(float(np.sqrt(np.mean(x.astype(np.float64) ** 2))) + 1e-12))


def s(a):
    return int(round(a * SR))


def build_segments():
    """[对白A 4s][旁白静音 4s][对白B 4s][静音 3s] -> 15s 章节轨。"""
    return [
        {"audio": synth_voice(4.0, f0=210.0, amp=0.26), "kind": "dialogue",
         "role": "林晚晴", "role_id": "r_lin"},
        {"audio": silence(4.0), "kind": "narration", "role": "旁白", "role_id": "r_nar"},
        {"audio": synth_voice(4.0, f0=132.0, amp=0.24), "kind": "dialogue",
         "role": "周建国", "role_id": "r_zhou"},
        {"audio": silence(3.0), "kind": "narration", "role": "旁白", "role_id": "r_nar"},
    ]


MIX_SETTINGS = {
    "bgm": {"ref": "bgm_warm_pad", "gain_db": -14.0, "ducking": True, "duck_db": 8.0},
    "sfx": {"chapter_marks": True},
    "role_gains": {"r_lin": {"gain_db": 1.0}},
    "master": {"gain_db": 0.0, "target_lufs": -16.0},
}


def _parse(path: Path):
    text = path.read_text(encoding="utf-8")
    return text, ast.parse(text, filename=str(path))


def _walk(node, func, text, kinds, found):
    for child in ast.iter_child_nodes(node):
        child_func = (child.name if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                      else func)
        if isinstance(child, kinds):
            detail = ast.get_source_segment(text, child.iter if isinstance(child, ast.For)
                                            else child) or "?"
            found.append((child_func or "<module>", child.lineno, " ".join(detail.split())))
        _walk(child, child_func, text, kinds, found)


def audit_loops(path: Path):
    """列出音频代码里所有 for 循环: (所在函数, 行号, 迭代对象源码)。"""
    text, tree = _parse(path)
    found: list = []
    _walk(tree, None, text, ast.For, found)
    return found


def audit_whiles(path: Path):
    """列出 while 循环: (所在函数, 行号, 循环体源码)。"""
    text, tree = _parse(path)
    found: list = []
    _walk(tree, None, text, ast.While, found)
    return found


def main():
    log = H.LOGS / "c3_mix.log"
    with H.Tee(log):
        su = H.Suite("C3-mix", "run_c3_mix.py")

        from modules.mixer.audio_mix import AudioMix, CEILING_DB, DEFAULT_GAP_MS
        from modules.mixer.bgm_store import BgmStore
        from modules.tts_engine.audio_postprocess import AudioPostProcessor

        store = BgmStore()
        mixer = AudioMix(SR, post=AudioPostProcessor(SR), store=store)

        # ---------------- 1: 占位素材库 ----------------
        r = su.new("素材库: 3 段占位 BGM + 2 个占位音效落盘与 meta.json")
        r.evidence_add(log)
        r.evidence_add(store.meta_path)
        made = store.ensure_builtin()
        H.kv("ensure_builtin", json.dumps(made, ensure_ascii=False))
        bgms = store.list_bgm()
        sfxs = store.list_sfx()
        for row in bgms + sfxs:
            H.kv(f"  {row['id']}", f"{row['name']} dur={row['duration_s']}s "
                                  f"loud={row['loudness_db']}dB loop={row['loopable']}")
            r.evidence_add(store.bgm_dir / row["file"])
        r.check("BGM >= 3 段", len(bgms) >= 3, str(len(bgms)))
        r.check("音效 >= 2 个", len(sfxs) >= 2, str(len(sfxs)))
        need = {"id", "name", "moods", "duration_s", "loopable", "loudness_db", "file"}
        r.check("meta 字段齐备", all(need <= set(row) for row in bgms + sfxs),
                str(sorted(need)))
        r.check("占位标记", all(row.get("placeholder") for row in bgms + sfxs),
                str([row.get("name") for row in bgms]))
        r.check("入库统一 48k 立体声",
                all(row["sample_rate"] == SR and row["channels"] == 2 for row in bgms + sfxs),
                str({row["id"]: (row["sample_rate"], row["channels"]) for row in bgms[:2]}))
        r.check("情绪标签非空", all(row["moods"] for row in bgms), str([row["moods"] for row in bgms]))
        mood_hits = store.list_bgm(mood="悬疑")
        r.check("按情绪标签筛选", [x["id"] for x in mood_hits] == ["bgm_suspense_pad"],
                str([x["id"] for x in mood_hits]))

        # ---------------- 2: 直通路径逐样本一致 ----------------
        r = su.new("直通: 无 bgm/sfx 且增益全 1 -> 与现状逐样本一致")
        r.evidence_add(log)
        segs = build_segments()
        audios = [seg["audio"] for seg in segs]
        baseline = mixer.post.concat_audios(audios, gap_ms=DEFAULT_GAP_MS)
        out_passthrough = mixer.mix(segs, {})
        r.check("is_passthrough({}) 为真", AudioMix.is_passthrough({}), "OK")
        r.check("is_passthrough(仅 0dB 主增益) 为真",
                AudioMix.is_passthrough({"master": {"gain_db": 0.0}}), "OK")
        r.check("is_passthrough(带 bgm) 为假",
                not AudioMix.is_passthrough({"bgm": {"ref": "bgm_warm_pad"}}), "OK")
        r.check("is_passthrough(角色增益非 0) 为假",
                not AudioMix.is_passthrough({"role_gains": {"a": {"gain_db": 2}}}), "OK")
        r.check("mix(段序列) 与 P8 concat 逐样本一致",
                out_passthrough.dtype == baseline.dtype
                and len(out_passthrough) == len(baseline)
                and np.array_equal(out_passthrough, baseline),
                f"{len(out_passthrough)} vs {len(baseline)} samples, "
                f"equal={np.array_equal(out_passthrough, baseline)}")
        same = mixer.mix_track(baseline, {})
        r.check("mix_track 直通返回原数组(零拷贝)", same is baseline, f"{same is baseline}")
        partial = mixer.mix(segs, {"bgm": {}})          # 无 ref 的 bgm 空配置 -> 仍直通
        r.check("空 bgm 配置仍直通", np.array_equal(partial, baseline),
                f"{len(partial)} samples")

        # ---------------- 3: 完整链路基本量 ----------------
        r = su.new("完整混音: 无削波 / 时长守恒 / 峰值上限 -1dBFS")
        r.evidence_add(log)
        t0 = time.perf_counter()
        out = mixer.mix(segs, json.loads(json.dumps(MIX_SETTINGS)))
        elapsed = time.perf_counter() - t0
        track = mixer.assemble(segs, MIX_SETTINGS)
        stats = dict(mixer.last_stats)
        H.kv("耗时(15s 轨)", f"{elapsed:.3f}s")
        H.kv("last_stats", json.dumps(stats, ensure_ascii=False, default=str)[:700])
        peak = float(np.max(np.abs(out)))
        H.kv("输出峰值", f"{peak:.6f} ({20*np.log10(peak+1e-12):.2f} dBFS)")
        r.evidence_add(H.ARTIFACTS / "result_C3-mix.json")
        r.check("峰值 <= -1dBFS", peak <= CEILING + 1e-6,
                f"{20*np.log10(peak+1e-12):.3f} dBFS ceiling={CEILING_DB}dB")
        r.check("无满幅削波样本", int(np.sum(np.abs(out) >= 0.999)) == 0,
                str(int(np.sum(np.abs(out) >= 0.999))))
        r.check("时长守恒(与拼接轨道一致)", len(out) == len(track),
                f"{len(out)} vs {len(track)}")
        expected = sum(len(a) for a in audios) + DEFAULT_GAP_MS / 1000 * SR * (len(audios) - 1)
        drift = abs(len(out) - expected) / expected
        H.kv("段序列期望样本", int(expected))
        r.check("时长守恒 ±5% (相对段序列)", drift <= 0.05, f"drift={drift*100:.3f}%")
        r.check("输出 dtype/形状", out.dtype == np.float32 and out.ndim == 1,
                f"{out.dtype} {out.shape}")
        r.check("BGM 已铺底(非全零差)", stats.get("bgm", {}).get("loaded") is True,
                json.dumps(stats.get("bgm", {}), ensure_ascii=False, default=str)[:200])
        r.check("章节音效已落点", len(stats.get("sfx", {}).get("placed", [])) == 2
                and all(p.get("loaded") for p in stats["sfx"]["placed"]),
                json.dumps(stats.get("sfx"), ensure_ascii=False)[:220])

        # ---------------- 4: RMS 闪避深度 ----------------
        r = su.new("RMS 侧链闪避: 人声段 BGM 至少压低 6dB")
        r.evidence_add(log)
        conf_duck = {"bgm": {"ref": "bgm_warm_pad", "gain_db": -14.0, "ducking": True, "duck_db": 8.0},
                     "master": {"gain_db": 0.0, "target_lufs": None}}
        conf_noduck = {"bgm": {"ref": "bgm_warm_pad", "gain_db": -14.0, "ducking": False},
                       "master": {"gain_db": 0.0, "target_lufs": None}}
        vocal_track = mixer.assemble(segs, conf_duck)
        bed_only = mixer.mix_track(vocal_track, conf_noduck)      # 对照: 同素材同相位, BGM 满档
        peak_ref = float(np.max(np.abs(bed_only)))
        r.check("对照组未触发限幅(闪避对比保持线性)", peak_ref <= CEILING + 1e-6,
                f"{20*np.log10(peak_ref+1e-12):.2f} dBFS")
        ducked = mixer.mix_track(vocal_track, conf_duck)          # 实验: 开启闪避
        stats_duck = dict(mixer.last_stats)                      # mix_track 每次都覆盖 last_stats
        r.check("未闪避配置重复调用逐样本一致",
                np.array_equal(bed_only, mixer.mix_track(vocal_track, conf_noduck)), "OK")
        voice_window = slice(s(0.6), s(3.8))                      # 人声 A 段
        steady_window = slice(s(1.2), s(3.8))                     # 避开 100ms 攻击斜坡后的稳态
        quiet_window = slice(s(5.0), s(7.6))                      # 旁白静音段 = 纯 BGM 段
        # 同一段时间窗内, 减去人声后剩下的就是纯 BGM 成分; 满档 vs 闪避之差才是闪避深度
        residual_full = bed_only[steady_window] - vocal_track[steady_window]
        residual_duck = ducked[steady_window] - vocal_track[steady_window]
        depth = rms_db(residual_full) - rms_db(residual_duck)
        H.kv("人声稳态窗 BGM 满档 RMS", f"{rms_db(residual_full):.2f} dBFS")
        H.kv("人声稳态窗 BGM 闪避后 RMS", f"{rms_db(residual_duck):.2f} dBFS")
        H.kv("闪避深度", f"{depth:.2f} dB (配置 8dB)")
        r.check("闪避深度 >= 6dB", depth >= 6.0, f"{depth:.2f}dB (配置 8dB)")
        r.check("稳态深度接近配置值(±1.0dB)", abs(depth - 8.0) <= 1.0, f"{depth:.2f}dB")
        vad = stats_duck.get("bgm", {}).get("vad", {})
        H.kv("VAD", json.dumps(vad, ensure_ascii=False))
        r.check("VAD 判定到人声占比合理", 0.2 < vad.get("voice_ratio", 0) < 0.8,
                str(vad.get("voice_ratio")))
        r.check("闪避后曲线最低增益接近配置值",
                abs(vad.get("min_gain_db", 0.0) + 8.0) <= 1.0, str(vad.get("min_gain_db")))
        r.check("人声帧内典型衰减等于配置值(无抽气回归)",
                abs(vad.get("voice_median_gain_db", 0.0) + 8.0) <= 0.5,
                f"median={vad.get('voice_median_gain_db')} mean={vad.get('voice_mean_gain_db')}dB")
        # spec 9 口径: 成品里人声段 vs 纯 BGM 段的 RMS 差 >= 6dB
        spec_diff = rms_db(ducked[voice_window]) - rms_db(ducked[quiet_window])
        bed_quiet = rms_db(ducked[quiet_window] - vocal_track[quiet_window])
        H.kv("成品人声段 RMS", f"{rms_db(ducked[voice_window]):.2f} dBFS")
        H.kv("成品纯 BGM 段 RMS", f"{rms_db(ducked[quiet_window]):.2f} dBFS")
        H.kv("成品人声段 vs 纯BGM段 RMS 差", f"{spec_diff:.2f} dB")
        H.kv("BGM 成分: 纯 BGM 段 vs 人声段", f"{bed_quiet:.2f} -> {rms_db(residual_duck):.2f} dBFS")
        r.check("成品人声段比纯 BGM 段响 >=6dB (spec 9 口径)", spec_diff >= 6.0,
                f"{spec_diff:.2f}dB")
        r.check("人声段里的 BGM 比纯 BGM 段低 >=6dB", bed_quiet - rms_db(residual_duck) >= 6.0,
                f"{bed_quiet - rms_db(residual_duck):.2f}dB")
        r.check("人声段人声仍占主导(人声 > BGM 残差)",
                rms_db(vocal_track[voice_window]) - rms_db(residual_duck) >= 6.0,
                f"{rms_db(vocal_track[voice_window]) - rms_db(residual_duck):.2f}dB")

        # ---------------- 5: BGM 采样率统一 + 素材缺失显式告警 ----------------
        r = su.new("BGM 统一到轨道 sr; 素材缺失不静默")
        r.evidence_add(log)
        odd = H.TMP / "c3_bgm_22050.wav"
        import soundfile as sf
        src, _ = store.read("bgm_daylight_pad")
        sf.write(str(odd), (src[::2] * 0.2).astype(np.float32), 22050, subtype="FLOAT")
        conf_odd = {"bgm": {"ref": str(odd), "gain_db": -12.0, "ducking": False},
                    "master": {"gain_db": 0.0, "target_lufs": None}}
        mixed_odd = mixer.mix_track(mixer.assemble(segs, conf_odd), conf_odd)
        st_odd = dict(mixer.last_stats)
        H.kv("22050 素材混音 stats", json.dumps(st_odd.get("bgm", {}), ensure_ascii=False)[:260])
        r.check("外部低 sr 素材可用(经 AudioPostProcessor.resample)",
                mixed_odd.shape[0] == mixer.assemble(segs, conf_odd).shape[0]
                and st_odd.get("bgm", {}).get("sr") == SR,
                json.dumps(st_odd.get("bgm", {}), ensure_ascii=False)[:160])
        conf_missing = {"bgm": {"ref": "不存在的曲子"}, "master": {"target_lufs": None}}
        mixed_missing = mixer.mix_track(mixer.assemble(segs, conf_missing), conf_missing)
        st_missing = dict(mixer.last_stats)
        H.kv("缺失素材 warnings", json.dumps(st_missing.get("warnings", []), ensure_ascii=False))
        r.check("缺失素材显式告警且不退化直通", bool(st_missing.get("warnings"))
                and mixed_missing.shape == mixer.assemble(segs, conf_missing).shape,
                str(st_missing.get("warnings")))
        r.check("meta 里 loopable 素材被循环铺满",
                st_odd.get("bgm", {}).get("source_samples", 0) > 0,
                str(st_odd.get("bgm", {}).get("looped")))

        # ---------------- 6: LUFS 归一与软限幅 ----------------
        r = su.new("LUFS 归一 (-16) + tanh 软限幅 (峰值 <= -1dBFS)")
        r.evidence_add(log)
        # 6a 偏轻输入（限幅不介入）: 归一应真正施加增益并命中目标
        mid = [{"audio": synth_voice(4.0, f0=200.0, amp=0.10), "kind": "dialogue", "role": "a"},
               {"audio": synth_voice(4.0, f0=140.0, amp=0.09), "kind": "dialogue", "role": "b"}]
        conf_mid = {"bgm": {"ref": "bgm_suspense_pad", "gain_db": -22.0, "ducking": True},
                    "master": {"gain_db": 0.0, "target_lufs": -20.0}}
        out_mid = mixer.mix(mid, conf_mid)
        st_mid = dict(mixer.last_stats)
        H.kv("偏轻输入 lufs", json.dumps(st_mid.get("lufs", {}), ensure_ascii=False))
        H.kv("偏轻输入 peak", json.dumps(st_mid.get("peak", {}), ensure_ascii=False))
        r.check("归一前明显偏轻(before < 目标-5LU)",
                st_mid["lufs"]["before"] is not None and st_mid["lufs"]["before"] < -25.0,
                str(st_mid["lufs"]))
        r.check("归一施加了正增益", st_mid["lufs"]["applied_db"] > 5.0, str(st_mid["lufs"]))
        r.check("中等电平无需限幅(before<=ceiling)",
                st_mid["peak"]["before_db"] <= CEILING_DB + 1e-6, str(st_mid["peak"]))
        r.check("限幅不改变电平(after==before)",
                abs(st_mid["peak"]["after_db"] - st_mid["peak"]["before_db"]) < 0.02,
                str(st_mid["peak"]))
        r.check("归一后实测 LUFS 命中目标 ±2 LU",
                st_mid["lufs"]["after"] is not None and abs(st_mid["lufs"]["after"] + 20.0) <= 2.0,
                str(st_mid["lufs"]))
        r.check("无截断告警", st_mid["lufs"]["adjust_capped"] is False
                and not st_mid.get("warnings"), str(st_mid.get("warnings")))
        r.check("偏轻输入成品不越 -1dBFS",
                float(np.max(np.abs(out_mid))) <= CEILING + 1e-6,
                f"{20*np.log10(float(np.max(np.abs(out_mid)))+1e-12):.2f} dBFS")
        # 6b 过载: 软限幅兜底 + 截断显式告警
        hot = [{"audio": synth_voice(4.0, f0=200.0, amp=1.6), "kind": "dialogue", "role": "a"},
               {"audio": synth_voice(4.0, f0=140.0, amp=2.4), "kind": "dialogue", "role": "b"}]
        conf_hot = {"bgm": {"ref": "bgm_suspense_pad", "gain_db": -10.0, "ducking": True},
                    "sfx": {"chapter_marks": True},
                    "role_gains": {"a": {"gain_db": 3.0}},
                    "master": {"gain_db": 6.0, "target_lufs": -16.0}}
        out_hot = mixer.mix(hot, conf_hot)
        st_hot = dict(mixer.last_stats)
        peak_hot = float(np.max(np.abs(out_hot)))
        H.kv("过载输入峰值", f"{float(np.max(np.abs(np.concatenate([h['audio'] for h in hot])))):.3f}")
        H.kv("限幅后峰值", f"{peak_hot:.6f} ({20*np.log10(peak_hot+1e-12):.2f} dBFS)")
        H.kv("过载 lufs", json.dumps(st_hot.get("lufs", {}), ensure_ascii=False))
        H.kv("过载 peak", json.dumps(st_hot.get("peak", {}), ensure_ascii=False))
        H.kv("过载 warnings", json.dumps(st_hot.get("warnings", []), ensure_ascii=False))
        r.check("过载输入仍不越 -1dBFS", peak_hot <= CEILING + 1e-6,
                f"{20*np.log10(peak_hot+1e-12):.3f} dBFS")
        r.check("削波样本数为 0", st_hot["peak"]["clipped_samples"] == 0, str(st_hot["peak"]))
        r.check("归一被上限截断时记录 adjust_capped", st_hot["lufs"]["adjust_capped"] is True
                and abs(st_hot["lufs"]["applied_db"]) <= 18.0 + 1e-6, str(st_hot["lufs"]))
        r.check("截断时显式告警(不静默)",
                any("截断" in w for w in st_hot.get("warnings", [])), str(st_hot.get("warnings")))
        r.check("响度朝目标推进 >=6LU", st_hot["lufs"]["before"] - st_hot["lufs"]["after"] >= 6.0,
                f"{st_hot['lufs']['before']} -> {st_hot['lufs']['after']}")
        # 6d 归一后仍过载（主增益 +12dB, 关掉归一）: 软限幅必须介入并留下告警
        conf_over = {"bgm": {"ref": "bgm_suspense_pad", "gain_db": -12.0, "ducking": True},
                     "master": {"gain_db": 12.0, "target_lufs": None}}
        out_over = mixer.mix(hot, conf_over)
        st_over = dict(mixer.last_stats)
        peak_over = float(np.max(np.abs(out_over)))
        H.kv("过载档(归一关闭) peak", json.dumps(st_over.get("peak", {}), ensure_ascii=False))
        H.kv("过载档(归一关闭) warnings", json.dumps(st_over.get("warnings", []), ensure_ascii=False))
        r.check("过载档软限幅介入并告警",
                any("软限幅" in w for w in st_over.get("warnings", [])), str(st_over.get("warnings")))
        r.check("过载档峰值被压到上限以下", peak_over <= CEILING + 1e-6,
                f"{20*np.log10(peak_over+1e-12):.3f} dBFS (归一前 "
                f"{st_over['peak']['before_db']}dBFS)")
        r.check("过载档无满幅削波样本", st_over["peak"]["clipped_samples"] == 0
                and int(np.sum(np.abs(out_over) >= 0.999)) == 0, str(st_over["peak"]))
        r.check("LUFS 估计方法已标注", mixer.lufs_method() in
                ("shelf_plus_highpass", "highpass_only_approx"), mixer.lufs_method())
        # 6c 限幅曲线本身：拐点以下线性、单调、不越上限
        src = np.array([0.05, 0.1, -0.2, 0.4, -0.4], dtype=np.float32)
        lin = mixer.soft_limit(src)
        r.check("远低于上限时原样返回(不改节目电平)",
                lin.dtype == np.float32 and np.array_equal(lin, src), f"{lin.dtype} {lin}")
        grid = np.linspace(-1.8, 1.8, 4001, dtype=np.float32)
        curve = mixer.soft_limit(grid.copy())
        mag = np.abs(curve[2000:])
        r.check("软限幅单调(无交叉失真)", bool(np.all(np.diff(mag) >= -1e-6)),
                f"min diff {float(np.min(np.diff(mag))):.2e}")
        r.check("软限幅后峰值不越上限", float(np.max(np.abs(curve))) <= CEILING + 1e-6,
                f"{float(np.max(np.abs(curve))):.6f}")
        r.check("小信号保持线性关系", abs(float(curve[2100]) - 0.09) < 1e-4, str(float(curve[2100])))

        # ---------------- 7: 性能红线 3 分钟轨道 ----------------
        r = su.new("性能红线: 3 分钟轨道混音 < 5s")
        r.evidence_add(log)
        blocks = []
        for i in range(30):
            blocks.append({"audio": synth_voice(5.0, f0=150 + 3 * i, amp=0.22),
                           "kind": "dialogue" if i % 2 == 0 else "narration", "role": f"r{i%3}"})
            blocks.append({"audio": silence(1.0), "kind": "narration", "role": "旁白"})
        conf_perf = json.loads(json.dumps(MIX_SETTINGS))
        t0 = time.perf_counter()
        out3 = mixer.mix(blocks, conf_perf)
        dur = time.perf_counter() - t0
        H.kv("轨道时长", f"{len(out3)/SR:.1f}s")
        H.kv("混音耗时", f"{dur:.3f}s")
        H.kv("realtime factor", f"{(len(out3)/SR)/max(dur,1e-6):.1f}x")
        r.check("轨道 >= 3 分钟", len(out3) / SR >= 180.0, f"{len(out3)/SR:.1f}s")
        r.check("耗时 < 5s", dur < 5.0, f"{dur:.3f}s for {len(out3)/SR:.1f}s track")
        r.check("长轨同样不削波", float(np.max(np.abs(out3))) <= CEILING + 1e-6,
                f"{20*np.log10(float(np.max(np.abs(out3)))+1e-12):.2f} dBFS")

        # ---------------- 8: 向量化审计（禁止逐样本 Python 循环） ----------------
        r = su.new("代码审计: 混音器无逐样本 Python 循环")
        r.evidence_add(log)
        src_py = H.PROJECT_ROOT / "modules" / "mixer" / "audio_mix.py"
        loops = audit_loops(src_py)
        # 允许的循环: 段级(段列表)、帧级(range(frames))、块级(range(0,n,valid))、配置级(dict)
        allowed = {
            "assemble": ("段级: 段序列/拼接块列表", ("segments", "pieces")),
            "_duck_curve": ("帧级: 帧数与样本数无关", ("range(frames)",)),
            "_weighted_mean_square": ("块级: FFT 分块", ("range(0, n, valid)",)),
            "_biquad_impulse": ("冲激响应构造(定长 1024, 与音频长度无关)", ("range(n)",)),
            "_build_sfx": ("配置级: 音效条目", ("items",)),
            "is_passthrough": ("配置级: role_gains 值", ("values()",)),
            # C4-3 后 validate_settings 逐项校验 bgm/sfx/role_gains/gaps：
            # 循环次数由"用户配置的条目数/键名数"决定（个位数），与音频样本数无关，
            # 属于本审计允许的配置级循环，不是被禁止的逐样本循环。
            "validate_settings": ("配置级: 遍历用户设置项(sfx 条目/role_gains 项/gaps 键名)",
                                  ("items", "enumerate(", '"default"')),
        }
        for func, ln, it in loops:
            H.kv(f"  循环 {func}:{ln}", it)
        bad = []
        for func, ln, it in loops:
            why, keys = allowed.get(func, (None, ()))
            if why is None or not any(k in it for k in keys):
                bad.append((func, ln, it))
        r.evidence_add(src_py)
        r.check("每个 for 都落在段级/帧级/块级/配置级白名单内", not bad,
                f"越界循环 {bad}；共 {len(loops)} 处")
        src_text = src_py.read_text(encoding="utf-8")
        sample_level = [pat for pat in ("range(len(", "for i in range(n_samples",
                        "enumerate(track", "for s in range(", "out[i] =", "y[i] =")
                        if pat in src_text]
        r.check("源码中不出现逐样本索引写法", not sample_level, str(sample_level))
        whiles = audit_whiles(src_py)
        for func, ln, body in whiles:
            H.kv(f"  while {func}:{ln}", f"循环体: {body[:170]}")
        r.check("while 仅出现在 _tile 的乐句级推进",
                len(whiles) == 1 and whiles[0][0] == "_tile",
                str([(f, ln) for f, ln, _ in whiles]))
        r.check("_tile 每次推进一整段素材(非 +1)",
                any("pos += take" in body for _f, _ln, body in whiles),
                str([body[:80] for _f, _ln, body in whiles]))

        rc = su.finish(exit_when_fail=False)
        print("")
        print("ALL_GREEN" if rc == 0 else f"RED ({rc})")
        sys.exit(0 if rc == 0 else 1)


if __name__ == "__main__":
    main()
