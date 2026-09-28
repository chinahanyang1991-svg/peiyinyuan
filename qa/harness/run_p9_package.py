"""
[P9] package 阶段实测: 真实音频 -> wav/flac/mp3/m4b 产物可播放性与章节标记
运行: python qa/harness/run_p9_package.py
前置: run_p7_synth.py 产出的真实音频段
"""
import sys
import json
import glob
import struct
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness_lib as H  # noqa: E402

import numpy as np  # noqa: E402

SEG_DIR = H.ARTIFACTS / "p7_segments"


def load_segments():
    out = []
    for i, f in enumerate(sorted(glob.glob(str(SEG_DIR / "seg*.npy")))):
        meta = json.loads(Path(f.replace(".npy", ".json")).read_text(encoding="utf-8"))
        a = np.fromfile(f, dtype=np.float32)
        out.append({"index": i, "audio": a, "text": meta["text"],
                    "chapter_title": f"第{i+1}章 测试", "chapter_id": f"c{i}",
                    "duration_ms": len(a) / meta["sr"] * 1000.0})
    return out


def mp4_atoms(path, depth=0, limit=400):
    """极简 MP4 box walker: 返回 atom 名列表 (用于判断 m4b/mp4 结构与章节痕迹)。
    2c: 原实现只下钻一层(moov 之后的 udta/meta/ilst 内不再递归)，
    找不到 chpl 导致假 FAIL；改为对容器 atom 递归下钻。"""
    names = []
    data = Path(path).read_bytes() if not isinstance(path, (bytes, bytearray)) else path
    CONTAINERS = ("moov", "trak", "mdia", "minf", "stbl", "udta", "meta", "ilst")
    i = 0
    while i + 8 <= len(data) and len(names) < limit:
        size = struct.unpack(">I", data[i:i + 4])[0]
        typ = data[i + 4:i + 8].decode("latin-1", "replace")
        if size < 8 or i + size > len(data):
            break
        names.append(typ)
        if typ in CONTAINERS and depth < 6:
            payload = data[i + 8:i + size]
            if typ == "meta":  # meta 是 fullbox，多 4 字节 version/flags
                payload = payload[4:]
            names.extend(mp4_atoms(payload, depth + 1, limit - len(names)))
        i += size
    return names


def mp4_atoms_retry(chunk):
    out = []
    i = 0
    while i + 8 <= len(chunk) and len(out) < 200:
        size = struct.unpack(">I", chunk[i:i + 4])[0]
        typ = chunk[i + 4:i + 8].decode("latin-1", "replace")
        if size < 8:
            break
        out.append(typ)
        i += size
    return out


def main():
    log = H.LOGS / "p9_package.log"
    with H.Tee(log):
        su = H.Suite("P9-package", "run_p9_package.py")
        segs = load_segments()
        r = su.new("前置: 真实音频段可用")
        r.evidence_add(SEG_DIR)
        if not segs:
            r.block("缺 p7 真实音频", "先运行 run_p7_synth.py")
            su.finish(exit_when_fail=False)
            return
        sr = segs[0]["audio"]  # placeholder to avoid lint
        sr = json.loads((SEG_DIR / "seg00.json").read_text(encoding="utf-8"))["sr"]
        H.kv("段数", len(segs))
        H.kv("sample_rate", sr)
        H.kv("ffmpeg in PATH", H.which("ffmpeg") or "NOT FOUND")

        from modules.workflow.conversion_workflow import ConversionWorkflow
        cw = ConversionWorkflow()
        produced = {}
        outs = {}

        for fmt in ("wav", "flac", "mp3", "m4b"):
            r = su.new(f"_stage_package 输出 {fmt}")
            r.evidence_add(log)
            import copy
            arrs = copy.deepcopy(segs)
            ctx = {"task_id": f"harness-p9-{fmt}", "sample_rate": sr,
                   "audio_arrays": arrs, "book_title": "harness_p9_book",
                   "book_author": "harness", "processed_chars": 100, "total_chars": 100}
            try:
                out = cw._stage_package(ctx, fmt)
                req = fmt
                got_fmt = out.get("output_path", "").rsplit(".", 1)[-1]
                H.kv("请求格式 -> 实际文件", f"{req} -> {out.get('output_path')}")
                H.kv("format_used/requested_format",
                     f"{out.get('format_used')}/{out.get('requested_format')}")
                H.kv("文件大小", out.get("output_size"))
                H.kv("总时长", f"{out.get('total_duration_ms', 0)/1000:.2f}s")
                st = H.audio_stats(out["output_path"])
                if st.get("error") and out["output_path"].endswith(".m4b"):
                    # 2c: soundfile/libsndfile 不解码 AAC/m4b —— 先用解析出的
                    # ffmpeg 转成 wav 再体检（此时 ffmpeg 可用是 PY-302 解除的前提）
                    from modules.tts_engine import ffmpeg_utils
                    dec = str(H.TMP / f"decoded_{Path(out['output_path']).stem}.wav")
                    ffmpeg_utils.run_ffmpeg(
                        ["-y", "-i", out["output_path"], "-c:a", "pcm_s16le", dec],
                        desc="m4b 解码体检")
                    st = H.audio_stats(dec)
                    st["decoded_via"] = ffmpeg_utils.resolve_ffmpeg()
                for k in ("sr", "duration_s", "peak", "rms_db", "active_ratio", "is_silent"):
                    H.kv(f"  体检.{k}", st.get(k))
                produced[fmt] = out["output_path"]
                outs[fmt] = out
                r.check(f"{fmt}: 产物存在", Path(out["output_path"]).exists(), out["output_path"])
                r.check(f"{fmt}: 请求格式未被静默替换", got_fmt == req,
                        f"请求 .{req} 实得 .{got_fmt}")
                r.check(f"{fmt}: 时长 > 0", st.get("duration_s", 0) > 0, st.get("duration_s"))
                r.check(f"{fmt}: 非静音", st.get("peak", 0) > 0.01, f"peak={st.get('peak')}")
                r.check(f"{fmt}: 采样率与引擎一致({sr})", st.get("sr") == sr,
                        f"{st.get('sr')} vs {sr}")
            except Exception as e:
                r.fail(f"{fmt} 导出抛错", f"{type(e).__name__}: {str(e)[:300]}")
                produced[fmt] = None

        # ---------- 章节标记 ----------
        r = su.new("M4B 章节标记可用性 (ffprobe/结构) 与降级链")
        r.evidence_add(H.PROJECT_ROOT / "modules/tts_engine/m4b_builder.py")
        m4b = produced.get("m4b")
        H.kv("m4b 请求的最终产物", m4b)
        if m4b and m4b.endswith(".wav"):
            r.check("m4b 请求得到真正的 m4b 文件", False,
                    "m4b 导出失败被静默降级为 wav (任务状态仍为 completed)")
        elif m4b:
            names = mp4_atoms(m4b)
            H.kv("MP4 atom 列表", names[:40])
            chap = [n for n in names if n in ("chpl", "chap", "©too", "udta", "ilst")]
            H.kv("章节相关 atom", chap)
            r.check("存在章节 atom (chpl/chap)", any(n in ("chpl", "chap") for n in names),
                    str(chap))
            # 2c: ffmpeg 解封装器权威清点（本机无 ffprobe，imageio-ffmpeg 只带 ffmpeg）
            import re
            from modules.tts_engine import ffmpeg_utils
            probe_res = ffmpeg_utils.run_ffmpeg(
                ["-i", m4b, "-f", "null", "-"], desc="章节清点", want_format="m4b")
            ch_lines = re.findall(
                r"Chapter #\d+:(\d+): start ([\d.]+), end ([\d.]+)",
                probe_res.stderr or "")
            # ffmpeg 对 -f null - 会把输入信息(含 Chapters)打印两遍，截到索引回落为止
            uniq = []
            for m in ch_lines:
                if int(m[0]) <= (int(uniq[-1][0]) if uniq else -1):
                    break
                uniq.append(m)
            ch_lines = uniq
            titles = re.findall(r"Chapter #\d+:\d+:.*?\n\s*Metadata:\s*\n\s*title\s*:\s*(.+)",
                                probe_res.stderr or "")
            H.kv("ffmpeg 清点章节数", len(ch_lines))
            for n, (idx, s, e) in enumerate(ch_lines[:10]):
                H.kv(f"  章节{n}", f"{float(s):.2f}-{float(e):.2f}s "
                                   f"{titles[n] if n < len(titles) else ''}")
            starts = [float(s) for _i, s, _e in ch_lines]
            ends = [float(e) for _i, _s, e in ch_lines]
            r.check("ffmpeg 清点章节数 >= 2", len(ch_lines) >= 2, str(ch_lines[:5]))
            r.check("章节时间轴单调且不全为 0",
                    bool(ch_lines) and starts[0] == 0.0
                    and all(b > a for a, b in zip(starts, starts[1:])),
                    f"starts={starts}")
            r.check("章节均非零宽", all(e > s for s, e in zip(starts, ends)),
                    f"ends={ends}")
        else:
            r.fail("m4b 无任何产物", "见上一用例")

        # ---------- M4BBuilder 契约一致性 ----------
        # 2c: 原用例是对"修复前源码"的静态断言（两侧都硬编码 False, 永远 FAIL）。
        # PY-125 修复后改为运行时契约验证：package 的段构造函数与 builder 必须同源。
        r = su.new("M4BBuilder 与 _stage_package 的段字典契约")
        builder_src = (H.PROJECT_ROOT / "modules/tts_engine/m4b_builder.py").read_text(
            encoding="utf-8")
        from modules.workflow.conversion_workflow import _m4b_segments
        from modules.tts_engine.m4b_builder import M4BBuilder
        mb = M4BBuilder()
        H.kv("builder 读取的键", [k for k in ("audio_data", "chapter_title", "duration_ms")
                                if k in builder_src])
        H.kv("M4BBuilder.available", mb.available)
        H.kv("ffmpeg 来源", mb.ffmpeg_path)
        import copy as _copy
        prod_segs = _m4b_segments(_copy.deepcopy(segs), sr, gain=1.0)
        keys_ok = prod_segs and all({"audio_data", "chapter_title", "duration_ms"}
                                    <= set(p.keys()) and p["audio_data"]
                                    for p in prod_segs)
        H.kv("_m4b_segments 产出键", sorted(prod_segs[0].keys()))
        r.check("package 产出的段字典满足 builder 契约", keys_ok,
                f"{len(prod_segs)} 段, keys={sorted(prod_segs[0].keys())}")
        fake = [{"title": "第一章", "start_time": 0, "duration": 3.2},
                {"title": "第二章", "start_time": 3.2, "duration": 2.1}]
        try:
            mb.build(fake, {"title": "t"}, str(H.TMP / "contract_test.m4b"), sample_rate=sr)
            r.check("缺 audio_data 的旧键段字典被显式拒绝", False, "意外成功, 会静默产出无章节文件")
        except Exception as e:
            r.check("缺 audio_data 的旧键段字典被显式拒绝(不再静默产出无章节文件)", True,
                    f"{type(e).__name__}: {str(e)[:160]}")
        if mb.available:
            try:
                p = mb.build(prod_segs, {"title": "harness", "author": "pyy"},
                             str(H.TMP / "real_build.m4b"), sample_rate=sr)
                st = H.audio_stats(p)
                H.kv("builder 直连产物", f"{p} {st}")
                r.check("builder 以 package 生产契约可产出 m4b",
                        Path(p).exists() and st.get("size", 0) > 0, str(st)[:200])
                r.check("builder.last_chapters 章节数与输入一致",
                        len(mb.last_chapters) == len(segs),
                        f"{len(mb.last_chapters)} vs {len(segs)}")
            except Exception as e:
                r.fail("builder 直连构建失败", f"{type(e).__name__}: {e}")
        else:
            r.block("ffmpeg 缺失, 无法验证 builder 正常路径", "见开头 ffmpeg 来源一行")

        # ---------- 章节起点是否被正确写入 ----------
        # 2c: 原用例是对修复前源码的静态断言（package 读 seg['start_time'] 恒 0）。
        # PY-126 修复后时间轴由 package/builder 同源累加，改为运行时验证。
        r = su.new("章节时间轴: package 回传 chapters 的真实递增与覆盖")
        H.kv("audio_arrays 段实际字段", sorted(segs[0].keys()))
        m4b_out = outs.get("m4b") or {}
        chaps = m4b_out.get("chapters") or []
        H.kv("package 回传 chapters 数", len(chaps))
        for c in chaps[:10]:
            H.kv("  ", f"{c['title'][:16]} {c['start_ms']:.0f}-{c['end_ms']:.0f}ms")
        expect_total_ms = segs_total_ms = sum(float(s["duration_ms"]) for s in segs) \
            + 300.0 * max(0, len(prod_segs) - 1)
        if chaps:
            starts = [c["start_ms"] for c in chaps]
            r.check("章节起点严格递增(不再恒 0)",
                    all(b > a for a, b in zip(starts, starts[1:])),
                    f"starts={starts}")
            r.check("末章 end ≈ 成品时长(±2%)",
                    abs(chaps[-1]["end_ms"] - expect_total_ms) <= 0.02 * expect_total_ms,
                    f"end={chaps[-1]['end_ms']:.0f} vs 预期 {expect_total_ms:.0f}")
            r.check("章节数 == 输入章节数", len(chaps) == len(segs),
                    f"{len(chaps)} vs {len(segs)}")
        else:
            r.fail("package 未回传 chapters", "m4b 用例失败或 ffmpeg 不可用")

        # ---------- 输出命名 ----------
        r = su.new("输出文件命名与下载可达性")
        for fmt, p in produced.items():
            if p:
                H.kv(fmt, f"{Path(p).name} ({Path(p).stat().st_size}B)")
        be = H.start_backend(H.LOGS / "p9_backend.log")
        try:
            code, body = H.http("GET", "/tasks/", timeout=60)
            tasks = body.get("tasks", []) if isinstance(body, dict) else []
            done = [t for t in tasks if t.get("status") == "completed"]
            H.kv("已完成任务数", len(done))
            for t in done[:3]:
                H.kv("  任务", f"{t['id'][:8]} fmt={t.get('output_format')} "
                               f"out={t.get('output_path')}")
            bad = [t for t in done if t.get("output_format") == "m4b"
                   and not (t.get("output_path") or "").endswith(".m4b")]
            H.kv("请求 m4b 但产物非 m4b 的已完成任务", len(bad))
            for t in bad[:5]:
                H.kv("  ", f"{t['id'][:8]} -> {t.get('output_path')}")
            r.check("已完成任务的产物格式与请求一致", not bad,
                    f"{len(bad)}/{len(done)} 个任务 m4b 请求实际得到其它格式 -> 降级未回写 "
                    f"output_format, 前端/播放器按 m4b 处理会失败")
        finally:
            H.stop_backend(be)
        su.finish(exit_when_fail=False)


if __name__ == "__main__":
    main()
