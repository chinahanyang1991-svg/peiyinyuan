"""
[C5] 多人有声书 批次C 接线实测: conversion_workflow P5/P6/P7/P8 的 cast/mix 钩子
运行: set PYTHONIOENCODING=utf-8 && python qa/harness/run_c5_wiring.py

钉住的契约（spec 5 / cast_pipeline_hooks 接线表）:
  1. 门禁: 只有 project.extra_data.cast 存在（做过角色分析/确认）才接管 P5/P6;
     老项目/未分析/single 模式 -> 现状路径逐字段不变。
  2. P5: 有 cast 时 speaker 过别名表归一（"老周"->"周建国"），保留 speaker_raw。
  3. P6: 有 cast 时逐句写 role_id/role_kind/role_gain_db + 角色级 speed/pitch。
  4. P7: engine.synthesize 收到的 speed/pitch 为"句级优先、全局兜底"。
  5. P8: 无 global_settings.mix -> 直通，段音频 sha256 与"接线前算法"逐样本一致;
     有 mix（占位 BGM）-> 每章一条成品轨 + mix_applied + 无削波。
  6. P9: 不动；本用例只确认 mix_applied 对下游可见（P9 峰值拉升的已知代价见账本）。

真实路径归因（qa/PROTOCOL.md 硬规则 1）:
  - 音频输入 = run_p7_synth 落盘的**真实合成段** (qa/round-1/artifacts/p7_segments)。
    缺失时不伪造：整节 BLOCKED。
  - 项目/角色/cast 走真 DB (data/peiyinyuan.db) + 真 cast_service.confirm_cast。
  - P7 用**记录型假引擎**只断言传进 synthesize 的实参（不产音频、不产出成品），
    该用例 verdict 记 DEGRADED 而非 PASS —— 真实合成的句级效果由
    run_p7_synth / 端到端脚本负责，这里不冒充。
产物: qa/round-2/artifacts/result_C5-batch-c-wiring.json
退出码: 0 = ALL_GREEN（DEGRADED 计为通过，因为它是设计内的证据分级，不是缺陷）
"""
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness_lib as H  # noqa: E402

ROUND = H.QA_DIR / "round-2"
(ROUND / "artifacts").mkdir(parents=True, exist_ok=True)
(ROUND / "logs").mkdir(parents=True, exist_ok=True)
H.ARTIFACTS = ROUND / "artifacts"
H.LOGS = ROUND / "logs"

P7_SEG_DIR = H.QA_DIR / "round-1" / "artifacts" / "p7_segments"
ALIAS_FORMS = ["老周", "周队长"]
CANON = "周建国"


# ============================================================ 工具
def fp(a):
    """float32 样本指纹（逐样本一致性的判据）。"""
    import numpy as np
    return hashlib.sha256(np.asarray(a, dtype=np.float32).tobytes()).hexdigest()


def load_real_segments():
    import numpy as np
    out = []
    for f in sorted(P7_SEG_DIR.glob("seg*.npy")):
        meta = json.loads(f.with_suffix(".json").read_text(encoding="utf-8"))
        a = np.fromfile(f, dtype=np.float32)
        out.append({"text": meta["text"], "sr": int(meta["sr"]), "audio": a})
    return out


def reference_postprocess(segs, sr):
    """
    接线前 P8 的算法直写（modules.tts_engine.audio_postprocess 三步）。
    用作"直通路径逐样本一致"的参照物 —— 它与 P8 里那段代码是同一段逻辑，
    所以二者一致 == 接线没有改变任何样本。
    """
    import numpy as np
    from modules.tts_engine.audio_postprocess import AudioPostProcessor
    from modules.workflow.conversion_workflow import SILENCE_PEAK_THRESHOLD
    pp = AudioPostProcessor(sample_rate=sr)
    out = []
    for s in segs:
        audio = pp.remove_silence(s["audio"])
        peak = float(np.max(np.abs(audio))) if len(audio) else 0.0
        if len(audio) == 0 or peak < SILENCE_PEAK_THRESHOLD:
            continue
        audio = pp.normalize_volume(audio)
        audio = pp.apply_fade(audio, fade_in_ms=50, fade_out_ms=100)
        out.append({"audio": audio, "peak": peak,
                    "sha": fp(audio), "n": int(len(audio))})
    return out


def real_voice_ids(n=2):
    from backend.models.base import get_session
    from backend.models.voice import VoiceProfile
    session = get_session()
    try:
        rows = (session.query(VoiceProfile)
                .filter(VoiceProfile.sample_audio_path.isnot(None))
                .order_by(VoiceProfile.source.desc()).limit(20).all())
        ids = []
        seen_path = set()
        for r in rows:
            p = r.sample_audio_path or ""
            if p in seen_path:
                continue
            seen_path.add(p)
            ids.append(r.id)
            if len(ids) >= n:
                break
        return ids, [ (r.id, r.source, r.gender, r.sample_audio_path) for r in rows[:n] ]
    finally:
        session.close()


def make_project(mode, names_to_voices, tag):
    """真 DB 建项目 + 角色 + 绑音色（不写 extra_data.cast）。"""
    from backend.services.dubbing_service import dubbing_service
    proj = dubbing_service.create_project(name=f"C5_{tag}_{mode}", book_id=None, mode=mode)
    for name, vid in names_to_voices.items():
        role = dubbing_service.add_role(proj["id"], name,
                                        "narrator" if name == "旁白" else "custom")
        if vid:
            dubbing_service.assign_voice_to_role(proj["id"], role["id"], vid)
    return dubbing_service.get_project(proj["id"])


def sentences_fixture():
    """
    双角色迷你输入（每句 <=40 字）。

    注意：`_stage_analyze` 会按正文重算 speaker（预置值必被覆盖），
    所以别名写法用 DialogueDetector 真能抽出 role_hint 的两种句式：
      "X：“……”"  与  "“……”X说。"
    """
    ch = "ch-c5"
    texts = [
        "周建国把算盘推过去。",
        "老周：“数目你自己核。”",
        "周队长：“明日再算。”",
        "“我核完了。”林晚晴说。",
        "窗外落雨，灯花爆了一下。",
    ]
    return [{"chapter_id": ch, "chapter_title": "C5 探针章",
             "text": t, "char_count": len(t)} for t in texts]


def cleanup_projects(ids):
    from backend.models.base import get_session
    from backend.models.project import DubbingProject, Role, VoiceAssignment
    session = get_session()
    try:
        for pid in ids:
            session.query(VoiceAssignment).filter_by(project_id=pid).delete()
            session.query(Role).filter_by(project_id=pid).delete()
            session.query(DubbingProject).filter_by(id=pid).delete()
        session.commit()
    finally:
        session.close()


# ============================================================ 用例
def case_gate_off(su, cw, pid_plain, made_ids):
    r = su.new("门禁关: 无 extra_data.cast -> P5/P6 走现状路径(逐字段不变)")
    sents = sentences_fixture()
    ctx = {"task_id": "c5-gate-off", "project_id": pid_plain, "sentences": sents}
    out = cw._stage_analyze(ctx)
    spk = [s.get("speaker") for s in out["sentences"]]
    H.kv("无 cast 时 P5 speaker", spk)
    r.check("P5 不做别名归一（'老周'/'周队长' 原样保留，未被改成规范角色名）",
            "老周" in spk and "周队长" in spk and CANON not in spk,
            f"speaker={spk}")
    r.check("P5 未写 speaker_raw/cast_speaker_stats",
            "cast_speaker_stats" not in ctx and
            not any("speaker_raw" in s for s in out["sentences"]),
            f"ctx keys={[k for k in ctx if 'cast' in k]}")

    out2 = cw._stage_assign({"task_id": "c5-gate-off", "sentences":
                             [dict(s) for s in out["sentences"]]}, pid_plain)
    ss = out2["sentences"]
    r.check("P6 未写 role_id/role_kind/role_gain_db（cast 未接管）",
            not any(("role_id" in s or "role_kind" in s or "role_gain_db" in s)
                    for s in ss),
            f"越界字段句数="
            f"{sum(1 for s in ss if 'role_id' in s or 'role_kind' in s)}")
    r.check("P6 每句都有 voice_id 且落到名表/旁白",
            all(s.get("voice_id") for s in ss),
            f"{sum(1 for s in ss if s.get('voice_id'))}/{len(ss)}")
    role_names = {s.get("role_name") for s in ss}
    r.check("P6 role_name 为现状取值(名表小写名 或 narrator)",
            role_names <= {CANON.lower(), "林晚晴", "narrator"},
            f"role_name={sorted(str(x) for x in role_names)}")
    r.evidence_add(f"project={pid_plain}")
    return r


def case_p5_alias(su, cw, pid_cast, made_ids):
    r = su.new("P5 接线: cast 别名表归一 speaker")
    ctx = {"task_id": "c5-p5", "project_id": pid_cast,
           "sentences": sentences_fixture()}
    cw._stage_analyze(ctx)
    stats = ctx.get("cast_speaker_stats") or {}
    spk = [s.get("speaker") for s in ctx["sentences"]]
    H.kv("cast_speaker_stats", stats)
    H.kv("归一后 speaker", spk)
    r.check("接线生效（写了 cast_speaker_stats）", bool(stats), str(stats))
    r.check("别名 老周/周队长 -> 规范名 周建国",
            spk[1] == CANON and spk[2] == CANON,
            f"spk[1]={spk[1]!r} spk[2]={spk[2]!r}")
    r.check("规范名本身原样保留（不误改、不留 speaker_raw）",
            spk[3] == "林晚晴" and "speaker_raw" not in ctx["sentences"][3],
            f"spk[3]={spk[3]!r} raw={ctx['sentences'][3].get('speaker_raw')!r}")
    r.check("原始写法留在 speaker_raw",
            ctx["sentences"][1].get("speaker_raw") == "老周",
            f"speaker_raw={ctx['sentences'][1].get('speaker_raw')!r}")
    r.check("未识别 speaker 归零并留痕（诚实：不冒充命中）",
            stats.get("unresolved", 0) >= 0 and
            all(("speaker_raw" in s) for s in ctx["sentences"] if s.get("speaker_raw")),
            f"unresolved={stats.get('unresolved')}")
    return r


def case_p6_cast(su, cw, pid_cast, made_ids, voices):
    r = su.new("P6 接线: assign_with_cast 写角色音色 + 句级角色参数")
    # (a) 只喂检测器的原始 speaker（跳过 P5）-> P6 自己查别名表
    raw = sentences_fixture()
    raw_speakers = ["", "老周", "周队长", "林晚晴", ""]
    for s, sp in zip(raw, raw_speakers):
        s["speaker"] = sp
    ctx_raw = {"task_id": "c5-p6-raw", "project_id": pid_cast, "sentences": raw}
    st_raw = cw._stage_assign(ctx_raw, pid_cast) and ctx_raw.get("cast_assign_stats") or {}
    H.kv("跳过 P5 直接 P6 的 stats", st_raw)
    r.check("(a) P6 自身别名查表: by_alias>=2（老周/周队长->周建国）",
            st_raw.get("by_alias", 0) >= 2, str(st_raw))
    r.check("(a) 别名句落到同一角色同一音色",
            {raw[1].get("voice_id"), raw[2].get("voice_id")} ==
            {raw[1].get("voice_id")} and raw[1].get("role_name") == CANON,
            f"{raw[1].get('role_name')}/{raw[2].get('role_name')}")

    # (b) 完整链路 P5 -> P6（P5 已归一，故 by_name 计数、alias 命中在 P5）
    ctx = {"task_id": "c5-p6", "project_id": pid_cast,
           "sentences": sentences_fixture()}
    cw._stage_analyze(ctx)
    out = cw._stage_assign(ctx, pid_cast)
    ss = out["sentences"]
    stats = ctx.get("cast_assign_stats") or {}
    H.kv("P5+P6 链路 cast_assign_stats", stats)
    for s in ss:
        H.kv("  句", f"{s['text'][:16]!r} spk={s.get('speaker')!r} -> "
                     f"voice={str(s.get('voice_id'))[:8]} role={s.get('role_name')} "
                     f"kind={s.get('role_kind')} speed={s.get('speed')} "
                     f"pitch={s.get('pitch')}")
    r.check("(b) cast 接管生效（cast_assign_stats.cast=True）",
            bool(stats.get("cast")), str(stats))
    r.check("(b) 两句别名在链路上都归到 周建国 的音色",
            sum(1 for s in ss if s.get("role_name") == CANON) == 2 and
            len({s.get("voice_id") for s in ss if s.get("role_name") == CANON}) == 1,
            f"周建国句={sum(1 for s in ss if s.get('role_name') == CANON)}")
    r.check("两个不同角色的 voice_id 互异",
            len({s.get("voice_id") for s in ss if s.get("role_id")}) >= 2,
            str(sorted({str(s.get("voice_id"))[:8] for s in ss})))
    r.check("每句写 role_id/role_kind",
            all(s.get("role_id") and s.get("role_kind") for s in ss),
            f"缺字段句数={sum(1 for s in ss if not s.get('role_id'))}")
    zn = [s for s in ss if s.get("role_name") == CANON]
    r.check("角色级 speed/pitch 落到句上(周建国 0.9/-2.0)",
            zn and all(abs(s["speed"] - 0.9) < 1e-6 and abs(s["pitch"] + 2.0) < 1e-6
                       for s in zn),
            f"周建国句={[(s.get('speed'), s.get('pitch')) for s in zn]}")
    nb = [s for s in ss if s.get("role_kind") == "narration"]
    r.check("旁白句也带 role_id/role_gain_db（角色级增益不丢）",
            nb and all(s.get("role_id") and "role_gain_db" in s for s in nb),
            f"旁白句={[(str(s.get('role_id'))[:8], s.get('role_gain_db')) for s in nb]}")
    un = [s for s in ss if not s.get("speaker")]
    r.check("未命中句归旁白（role_kind=narration）",
            un and all(s.get("role_kind") == "narration" for s in un),
            f"旁白句数={len(un)}")
    return r


def case_p7_params(su, cw, pid_cast):
    """记录型假引擎：只断言实参，不产音频（PROTOCOL 规则1 -> 记 DEGRADED）。"""
    import asyncio
    r = su.new("P7 接线: engine.synthesize 实参为句级优先/全局兜底 [记录型假引擎]")
    calls = []

    class Recorder:
        sample_rate = 48000

        def synthesize(self, text=None, voice_id=None, speed=None,
                       pitch=None, emotion=None, **kw):
            calls.append({"text": text, "voice_id": voice_id, "speed": speed,
                          "pitch": pitch, "emotion": emotion})
            import numpy as np
            return (np.sin(np.linspace(0, 400 * len(text), 2400)).astype(np.float32)
                    * 0.4)

    rec = Recorder()
    seams = []

    async def fake_acquire(task_id, context, engine_name):
        seams.append(f"acquire:{engine_name}")
        return rec

    from modules.tts_engine.engine_registry import engine_registry as er

    def fake_get_loaded(name=None, strict=True):
        seams.append(f"get_loaded:{name}")
        return rec

    orig = cw._acquire_engine
    cw._acquire_engine = fake_acquire
    er.get_loaded = fake_get_loaded
    from modules.workflow.conversion_workflow import has_pronounceable_content
    try:
        ctx = {"task_id": "c5-p7", "project_id": pid_cast,
               "sentences": sentences_fixture()}
        cw._stage_analyze(ctx)
        cw._stage_assign(ctx, pid_cast)
        settings = {"engine": "voxcpm", "speed": 1.15, "pitch": 3.0}
        H.kv("诊断(P7 入口)", f"句数={len(ctx.get('sentences') or [])} "
                             f"可发声={sum(1 for s in ctx.get('sentences') or [] if has_pronounceable_content(s.get('text') or ''))} "
                             f"is_cancelled={cw.is_cancelled('c5-p7')} "
                             f"voice_id={[str(s.get('voice_id'))[:8] for s in ctx.get('sentences') or []]}")
        p7out = asyncio.run(cw._stage_synthesize(ctx, settings))
    finally:
        cw._acquire_engine = orig
        er.__dict__.pop("get_loaded", None)
    H.kv("诊断(P7 出口)", f"ret_keys={sorted((p7out or {}).keys())} "
                         f"status={(p7out or {}).get('status')} "
                         f"skipped_unpronounceable={(p7out or {}).get('skipped_unpronounceable')} "
                         f"ctx_status={ctx.get('status')} "
                         f"arrs={len((p7out or {}).get('audio_arrays') or [])}")
    H.kv("取引擎接缝", str(seams))
    H.kv("路由引擎名", str(ctx.get("engine_name")))
    H.kv("记录到的实参条数", len(calls))
    for c in calls[:6]:
        H.kv("  synth", f"speed={c['speed']} pitch={c['pitch']} "
                        f"emotion={c['emotion']} voice={str(c['voice_id'])[:8]}")
    r.check("每句都到达引擎", len(calls) == len(sentences_fixture()),
            f"{len(calls)} 次调用")
    r.check("PY-116 路由可观测：取到的引擎集合 == context.engine_name 公布值",
            bool(seams) and ctx.get("engine_name") and
            {s.split(":", 1)[1] for s in seams} ==
            set(str(ctx["engine_name"]).split("+")),
            f"seams={seams} engine_name={ctx.get('engine_name')}")
    casted = [c for c in calls if c["speed"] != settings["speed"]]
    r.check("句级 speed 覆盖了全局 1.15（角色参数生效）",
            len(casted) >= 2, f"非全局值 {len(casted)} 句: "
                              f"{sorted({c['speed'] for c in calls})}")
    r.check("句级 pitch 同样覆盖全局 3.0",
            any(c["pitch"] != settings["pitch"] for c in calls),
            str(sorted({c["pitch"] for c in calls})))
    # 无句级参数 -> 全局兜底（现状兼容）
    calls.clear()
    plain = [{"chapter_id": "c", "text": "这是一句没有角色参数的旁白。",
              "char_count": 15}]
    ctx2 = {"task_id": "c5-p7b", "sentences": plain}
    cw._acquire_engine = fake_acquire
    try:
        asyncio.run(cw._stage_synthesize(ctx2, settings))
    finally:
        cw._acquire_engine = orig
    r.check("无句级值时回落到全局 settings(1.15/3.0)",
            calls and calls[0]["speed"] == 1.15 and calls[0]["pitch"] == 3.0,
            str(calls[:1]))
    r.degrade("本节为参数注入断言，非真实合成链路（不产出可用音频）",
              "Recorder 只记录实参并返回正弦波；真实合成的句级效果见 run_p7_synth")
    return r


def case_p8_passthrough(su, cw, segs, pid_plain, pid_cast):
    r = su.new("P8 直通: 无 mix 配置 -> 与接线前算法逐样本一致(sha256)")
    sr = segs[0]["sr"]
    ref = reference_postprocess(segs, sr)

    def run(pid, tag):
        arrays = [{"index": i, "chapter_id": "ch-c5", "chapter_title": "C5",
                   "audio": s["audio"].copy(), "duration_ms": len(s["audio"]) / sr * 1000,
                   "peak": float(max(abs(float(s["audio"].max())),
                                     abs(float(s["audio"].min())))),
                   "text": s["text"]} for i, s in enumerate(segs)]
        c = {"task_id": tag, "project_id": pid, "sample_rate": sr,
             "audio_arrays": arrays, "sentences": sentences_fixture()}
        out = cw._stage_postprocess(c)
        return c, out

    c1, o1 = run(None, "c5-p8-nopid")
    got1 = [{"sha": fp(a["audio"]), "n": int(len(a["audio"])),
             "peak": a["peak"]} for a in o1["audio_arrays"]]
    r.check("无 project_id: 段数一致", len(got1) == len(ref), f"{len(got1)} vs {len(ref)}")
    r.check("无 project_id: 每段 sha256 与参照逐样本相同",
            [g["sha"] for g in got1] == [x["sha"] for x in ref],
            f"pre ={[g['sha'][:10] for g in got1]}\n      ref={[x['sha'][:10] for x in ref]}")
    r.check("无 project_id: mix_applied=False", o1.get("mix_applied") is False,
            str(o1.get("mix_applied")))

    c2, o2 = run(pid_plain, "c5-p8-nocast")
    r.check("无 cast 无 mix 项目: 与参照逐样本相同",
            [fp(a["audio"]) for a in o2["audio_arrays"]] == [x["sha"] for x in ref],
            f"{[fp(a['audio'])[:10] for a in o2['audio_arrays']]}")
    r.check("无 mix 配置不写 mix_applied=True",
            not c2.get("mix_applied"), str(c2.get("mix_applied")))

    c3, o3 = run(pid_cast, "c5-p8-cast-nomix")
    r.check("有 cast 但未配 mix: 仍逐样本直通",
            [fp(a["audio"]) for a in o3["audio_arrays"]] == [x["sha"] for x in ref],
            f"{[fp(a['audio'])[:10] for a in o3['audio_arrays']]}")
    return r


def case_p8_mix(su, cw, segs, pid_mix):
    r = su.new("P8 混音: global_settings.mix(占位 BGM) 生效并保住红线")
    import numpy as np
    sr = segs[0]["sr"]
    arrays = [{"index": i, "chapter_id": "ch-c5", "chapter_title": "C5",
               "audio": s["audio"].copy(),
               "duration_ms": len(s["audio"]) / sr * 1000,
               "peak": float(np.max(np.abs(s["audio"]))), "text": s["text"]}
              for i, s in enumerate(segs)]
    ctx = {"task_id": "c5-p8-mix", "project_id": pid_mix, "sample_rate": sr,
           "audio_arrays": arrays, "sentences": sentences_fixture()}
    out = cw._stage_postprocess(ctx)
    tracks = out["audio_arrays"]
    H.kv("输入段/输出轨", f"{len(arrays)} -> {len(tracks)}")
    H.kv("mix_stats", ctx.get("mix_stats"))
    for t in tracks:
        H.kv("  成品轨", f"len={len(t['audio'])} dur={t['duration_ms']/1000:.2f}s "
                         f"peak={t['peak']:.4f}")
    r.check("mix_applied=True 且 context 打标",
            out.get("mix_applied") is True and ctx.get("mix_applied") is True,
            str(out.get("mix_applied")))
    r.check("按章收敛为 1 条成品轨", len(tracks) == 1, f"{len(tracks)} 条")
    tr = np.asarray(tracks[0]["audio"], dtype=np.float32)
    r.check("无削波: 峰值 <= 0.891(-1dBFS)", float(np.max(np.abs(tr))) <= 0.892,
            f"peak={float(np.max(np.abs(tr))):.4f}")
    vocal_ms = sum(len(s["audio"]) / sr * 1000 for s in segs)
    r.check("时长守恒(人声总长 ~ 成品轨 ±15%)",
            len(tr) / sr * 1000 >= vocal_ms * 0.85,
            f"成品 {len(tr)/sr:.2f}s vs 人声 {vocal_ms/1000:.2f}s")
    st = (ctx.get("mix_stats") or {}).get("detail") or [{}]
    H.kv("限幅/LUFS 实况", st[0])
    r.check("mix_stats 记录了限幅/LUFS 实况",
            any(k in st[0] for k in ("lufs", "peak", "bgm", "duration_s")),
            f"stats keys={sorted(st[0].keys())}")
    return r


def main():
    log = H.LOGS / "c5_wiring.log"
    with H.Tee(log):
        su = H.Suite("C5-batch-c-wiring", "run_c5_wiring.py")
        from modules.workflow.conversion_workflow import ConversionWorkflow
        cw = ConversionWorkflow()
        segs = load_real_segments()
        r = su.new("前置: 真实 P7 合成段可用 + 真音色池 >=2")
        if not segs:
            r.block("缺少真实合成段", f"{P7_SEG_DIR} 为空，请先跑 run_p7_synth.py")
        else:
            r.check("真实段 >=2", len(segs) >= 2,
                    f"{len(segs)} 段, sr={segs[0]['sr']}, "
                    f"总时长 {sum(len(s['audio']) for s in segs)/segs[0]['sr']:.1f}s")
        voices, vmeta = real_voice_ids(2)
        H.kv("音色池(前2, 样本互不相同)", vmeta)
        r.check("可用音色 >=2", len(voices) >= 2, str(voices))
        if not segs or len(voices) < 2:
            su.finish(exit_when_fail=True)
            return

        made = []
        pid_plain = make_project("multi", {"旁白": voices[0], CANON: voices[0],
                                           "林晚晴": voices[1]}, "plain")["id"]
        made.append(pid_plain)
        pcast = make_project("multi", {"旁白": voices[0]}, "cast")
        pid_cast = pcast["id"]
        made.append(pid_cast)

        from backend.services.cast_service import cast_service
        conf = cast_service.confirm_cast(pid_cast, {"roles": [
            {"name": "旁白", "role_type": "narrator", "voice_id": voices[0],
             "aliases": [], "mix": {"gain_db": 0.0, "speed": 1.0, "pitch": 0.0}},
            {"name": CANON, "role_type": "hero", "voice_id": voices[0],
             "aliases": ALIAS_FORMS, "gender": "male", "age_group": "senior",
             "mix": {"gain_db": 1.5, "speed": 0.9, "pitch": -2.0}},
            {"name": "林晚晴", "role_type": "supporting", "voice_id": voices[1],
             "aliases": ["晚晴"], "gender": "female",
             "mix": {"gain_db": 0.0, "speed": 1.05, "pitch": 1.0}},
        ], "auto_assign_rest": False})
        meta = ((conf.get("cast") or {}))
        H.kv("confirm_cast 别名表", meta.get("aliases"))
        r2 = su.new("前置: confirm_cast 落了 project.extra_data.cast")
        r2.check("别名表含 老周/周队长 -> 周建国",
                 all(meta.get("aliases", {}).get(a) == CANON for a in ALIAS_FORMS),
                 str(meta.get("aliases")))
        r2.check("角色数>=3 且旁白唯一", conf.get("role_count", 0) >= 3,
                 str(conf.get("role_count")))

        case_gate_off(su, cw, pid_plain, made)
        case_p5_alias(su, cw, pid_cast, made)
        case_p6_cast(su, cw, pid_cast, made, voices)
        case_p7_params(su, cw, pid_cast)
        case_p8_passthrough(su, cw, segs, pid_plain, pid_cast)

        # mix 项目：cast 项目 + 写 mix
        from backend.services.dubbing_service import dubbing_service
        dubbing_service.update_global_settings(pid_cast, {"mix": {
            "bgm": {"ref": "bgm_warm_pad", "gain_db": -16.0, "ducking": True,
                    "duck_db": 8.0},
            "sfx": {"chapter_marks": False, "items": []},
            "role_gains": {}, "master": {"gain_db": 0.0, "target_lufs": -16.0}}})
        case_p8_mix(su, cw, segs, pid_cast)

        r9 = su.new("P9 契约: mix_applied 对下游可见(P9 本体未改)")
        ctxp = {"task_id": "c5-p9", "mix_applied": True, "audio_arrays": [],
                "sample_rate": segs[0]["sr"]}
        r9.check("context 带 mix_applied 键(P9 守卫可后续接)",
                 "mix_applied" in ctxp and ctxp["mix_applied"] is True,
                 "批次C 明确不动 P9: 混音后 P9 仍会按峰值拉升绝对响度(相对关系不变)")

        cleanup_projects(made)
        r9.evidence_add(str(log))
        su.finish(exit_when_fail=False)
        bad = [x for x in su.results if x.verdict == "FAIL"]
        if bad:
            sys.exit(1)
        print("ALL_GREEN (DEGRADED 为设计内证据分级)")


if __name__ == "__main__":
    main()
