"""
[P7c-edge] PY-116 合成引擎档位升级实测:
  A. 全 edge 句 -> 生产 _stage_synthesize 并发快速路径 (voxcpm 必须 0 加载)
  B. edge+voxcpm 混合 -> 句级路由 + 统一采样率 + 顺序路径
  C. 逐段红线体检 (峰值/非静音/时长-字数比)

运行: python qa/harness/run_p7c_edge.py   (cwd=peiyinyuan, PYTHONIOENCODING=utf-8)
证据: qa/round-1/artifacts/p7c_segments/*.npy+json, result_P7c-edge.json
"""
import sys
import os
import time
import asyncio
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness_lib as H  # noqa: E402

import numpy as np  # noqa: E402

SEG_DIR = H.ARTIFACTS / "p7c_segments"

EDGE_SET = ["Edge-晓晓(女·温暖)", "Edge-云健(男·磁性)", "Edge-云夏(童声)",
            "Edge-晓伊(女·活泼)", "Edge-云希(男·青年)"]

TEXTS = [
    "他慢慢推开门，屋里静得能听见自己的心跳。",
    "这个结果谁都没有想到，包括老陈自己。",
    "她笑了笑，把伞递过去，转身跑进了雨里。",
    "第二天一早，码头传来汽笛，船要开了。",
    "小满把糖纸一张张展平，夹进日记本。",
    "“你当真要走？”“当真。”",
    "风声掠过麦田，像整片海在呼吸。",
    "他把信折成两截，一截烧了，一截收进怀里。",
    "谁也没说话，茶凉了三次。",
    "山那边还是山，可他想再看看。",
    "“记住，别回头。”母亲只说了这一句。",
    "三年后他回来了，小镇却认不出他。",
]


def _edge_uuids():
    from backend.models.voice import VoiceProfile
    from backend.models.base import get_session
    s = get_session()
    out = {}
    for n in EDGE_SET:
        prof = s.query(VoiceProfile).filter_by(name=n, engine="edgetts").first()
        if prof is None:
            raise AssertionError(f"edge 音色未入库: {n}")
        out[n] = prof.id
    # 一个真实 downloaded(voxcpm) 音色供混合路由 B 使用
    dl = s.query(VoiceProfile).filter_by(engine="voxcpm", source="downloaded") \
          .filter(VoiceProfile.reference_audio_path.isnot(None)).first()
    s.close()
    return out, (dl.id if dl else None)


def _stats(a, sr):
    pk = float(np.max(np.abs(a))) if len(a) else 0.0
    rms = float(np.sqrt(np.mean(a ** 2))) if len(a) else 0.0
    dur = len(a) / sr
    return pk, rms, dur


def main():
    SEG_DIR.mkdir(parents=True, exist_ok=True)
    with H.Tee(H.LOGS / "p7c_edge.log"):
        su = H.Suite("P7c-edge", "run_p7c_edge.py")
        from modules.tts_engine.engine_registry import engine_registry
        from modules.workflow.conversion_workflow import ConversionWorkflow
        uuids, dl_voice = _edge_uuids()

        # ---------------- A: 全 edge 并发快速路径 ----------------
        r = su.new("A. 全edge句->生产_stage_synthesize并发路径(voxcpm零加载)")
        names = list(uuids.keys())
        sents = [{"text": TEXTS[i], "voice_id": uuids[names[i % len(names)]],
                  "tone": "neutral", "chapter_id": "c1", "chapter_title": "第一章"}
                 for i in range(12)]
        ctx = {"task_id": "harness-p7c-a", "sentences": [dict(s) for s in sents],
               "total_chars": sum(len(s["text"]) for s in sents)}
        t0 = time.time()
        outA = asyncio.run(ConversionWorkflow()._stage_synthesize(ctx, {"engine": "edgetts"}))
        wallA = time.time() - t0
        srA = outA["sample_rate"]
        segsA = outA["audio_arrays"]
        audioA = sum(len(s["audio"]) for s in segsA) / srA
        r.check("12 句全部产出", len(segsA) == 12, f"{len(segsA)}")
        r.check("voxcpm 未被拉取加载(纯edge免冷启动)",
                not engine_registry.is_loaded("voxcpm") and not engine_registry.is_instantiated("voxcpm"),
                f"loaded={engine_registry.is_loaded('voxcpm')}")
        bad = [s["index"] for s in segsA
               if _stats(np.asarray(s["audio"], np.float32), srA)[0] < 0.02]
        r.check("逐段峰值显著非静音", not bad, f"bad={bad}")
        rtfA = wallA / max(audioA, 0.01)
        H.kv("A 墙钟", f"{wallA:.1f}s / 音频 {audioA:.1f}s, RTF={rtfA:.2f}, 并发x{os.getenv('PYY_EDGE_CONCURRENCY','6')}")
        r.check("edge RTF < 1.0 (对照: voxcpm 基线 57~172)", rtfA < 1.0, f"RTF={rtfA:.2f}")
        r.evidence_add(H.LOGS / "p7c_edge.log")

        # ---------------- B: 混合路由 + 统一采样率 ----------------
        r = su.new("B. edge+voxcpm混合->句级路由/统一sr/顺序路径")
        if dl_voice is None:
            r.block("DB 无 downloaded(voxcpm) 音色，混合路由无法真实验证")
        else:
            mix = [
                {"text": "他慢慢推开门，屋里很静。", "voice_id": uuids["Edge-晓晓(女·温暖)"]},
                {"text": "老陈愣了一下，没有回头。", "voice_id": dl_voice},
                {"text": "风声掠过麦田，像海在呼吸。", "voice_id": uuids["Edge-云健(男·磁性)"]},
            ]
            for m in mix:
                m.update({"tone": "neutral", "chapter_id": "c1", "chapter_title": "第一章"})
            ctxB = {"task_id": "harness-p7c-b", "sentences": [dict(m) for m in mix],
                    "total_chars": sum(len(m["text"]) for m in mix)}
            t0 = time.time()
            outB = asyncio.run(ConversionWorkflow()._stage_synthesize(
                ctxB, {"engine": "voxcpm"}))  # 全局默认仍 voxcpm，靠句级路由分流
            wallB = time.time() - t0
            segsB = outB["audio_arrays"]
            srB = outB["sample_rate"]
            H.kv("B 引擎标记", ctxB.get("engine_name"), )
            H.kv("B 统一sr", srB)
            H.kv("B 墙钟", f"{wallB:.1f}s (含 voxcpm 冷启动 {ctxB.get('model_load_seconds')}s)")
            r.check("混合任务 engine_name 记录两引擎",
                    ctxB.get("engine_name") == "edgetts+voxcpm",
                    str(ctxB.get("engine_name")))
            r.check("统一采样率=48000(edge 段已重采样)", srB == 48000, str(srB))
            lens = []
            for s in segsB:
                a = np.asarray(s["audio"], np.float32)
                pk, rms, dur = _stats(a, srB)
                cps = len(s["text"]) / max(dur, 0.01)
                lens.append((s["index"], len(a), round(pk, 3), round(dur, 2), round(cps, 1)))
                (SEG_DIR / f"mix{ s['index']:02d}.npy").write_bytes(a.tobytes())
                with open(SEG_DIR / f"mix{s['index']:02d}.json", "w", encoding="utf-8") as f:
                    json.dump({"text": s["text"], "len": len(a), "sr": srB, "peak": pk},
                              f, ensure_ascii=False)
            H.kv("B 逐段 (idx,len,peak,dur,字/秒)", str(lens))
            r.check("3 段全部产出且顺序保持", [x[0] for x in lens] == [0, 1, 2], str(lens))
            r.check("各段非静音且语速在 1.5~9 字/秒",
                    all(p > 0.02 and 1.5 <= cps <= 9 for _, _, p, _, cps in lens), str(lens))
            # edge 段(0,2)时长与纯 edge 段1 相比不短于 0.5x
            dur_edge = lens[0][3]
            r.check("edge 段在 48k 下时长合理", 0.5 <= dur_edge <= 20, f"{dur_edge}s")

        # ---------------- C: 音色区分度 (不同 edge 音色音频互不相同) ----------------
        r = su.new("C. 多edge音色互异性")
        eng = engine_registry.get("edgetts")
        wave = {"same-text": {}}
        probe_text = "这是一段完全相同的测试文本。"
        for n in EDGE_SET:
            a = eng.synthesize(text=probe_text, voice_id=uuids[n])
            wave[n] = a
        ref = wave[EDGE_SET[0]]
        corrs = []
        for n in EDGE_SET[1:]:
            b = wave[n]
            m = min(len(ref), len(b))
            c = float(np.corrcoef(ref[:m], b[:m])[0, 1])
            corrs.append((n, round(c, 3)))
        H.kv("与基准音色相关系数", str(corrs))
        r.check("同文本不同音色互异(corr<0.6)", all(c < 0.6 for _, c in corrs), str(corrs))

        try:
            engine_registry.unload_all()
        except Exception:
            pass
        su.finish(exit_when_fail=False)


if __name__ == "__main__":
    main()
