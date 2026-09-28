"""
[C6] 多人有声书 批次C 端到端最小实证（真实 HTTP 任务 + 真实 TTS + 真实混音成品）
运行: set PYTHONIOENCODING=utf-8 && python qa/harness/run_c6_e2e_multicast.py

成本红线（本轮任务书）: 合成预算 <=3 句。正文只有两句台词（同一句文本、两个角色），
CPU 合成 RTF~57 -> 单句 1~3s 音频 = 60~180s；模型冷启动 ~220s 与 P1..P6 并行(PY-115)。
超预算/引擎装载失败一律 BLOCKED 并如实标注证据层级，不降级为 mock。

真实路径（qa/PROTOCOL.md 硬规则 1）:
  - 真 uvicorn(自选空闲端口) + POST /api/books/import + POST /api/dubbing/projects
  - POST analyze-cast(真 cast 任务) -> GET suggestions(真音色池) -> PUT confirm
  - PUT mix-settings(占位 BGM) -> POST /api/tasks/convert -> 轮询 /api/tasks/{id}/progress
  - 产物 m4b 经 ffmpeg 解码为 wav 后做音频体检（无 ffmpeg 则 BLOCKED）
  - 角色音源 = DB 里 source=downloaded 且 reference_audio_path 在盘的真实音色
    （智能体2d PY-303 修复 _restore_voices 后才会被引擎采纳；本脚本会核对服务端日志
     "从 DB 恢复了 N 个音色"并打印实际使用的 voice_id，不做静默替换）
证据分层（结果里逐条标注）:
  L1 = 生产 HTTP 任务产出的成品音频（本节断言全部基于它）
  L2 = 同文本双角色试听（cast/preview，仅显式 PYY_C6_L2=1 且预算充足才采集）
  L2'= 落盘段复用：真合成预算(<=3 句)用满时的替代口径——复用本次运行 L1 成品
       解码段，层级如实标注，不冒充端点真试听（端点契约由 C4 用例11 覆盖）
  L3 = 进程内 P1..P6 预检（只为钉"哪句拿到哪个 voice_id"，不产音频）
产物: qa/round-2/artifacts/result_C6-e2e-multicast.json + 同名 .log + 解码 wav
"""
import json
import os
import socket
import sys
import time
from pathlib import Path


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


PORT = int(os.environ.get("PYY_TEST_PORT") or _free_port())
os.environ["PYY_TEST_PORT"] = str(PORT)

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness_lib as H  # noqa: E402

import numpy as np  # noqa: E402
import soundfile as sf  # noqa: E402

ROUND = H.QA_DIR / "round-2"
(ROUND / "artifacts").mkdir(parents=True, exist_ok=True)
H.ARTIFACTS = ROUND / "artifacts"
H.LOGS = ROUND / "logs"

# 正文 3 句（<=40 字）: 前两句同一句文本 "账在这里。" 由两个角色说（对照实验），
# 说话人故意写别名 老周/小苏 —— P5 的 cast 别名表必须真的把它归一到规范名，
# P6 才能分给两个不同音色（不是靠名字精确匹配的假命中）。
# 第三句是无引号环境描写 -> 检测器 speaker 为空 -> 归旁白，
# 兼两用途: ① 成品尾部留出一段 BGM-only 区间（闪避释放区）供响度差断言；
#           ② 与角色台词形成"人声 vs 纯 BGM"的天然对照。
LINE_A = "老周：“账在这里。”"
LINE_B = "“账在这里。”小苏说。"
LINE_N = "窗外的雨落了整夜，谁也没有再说话。"
ROLE_A, ROLE_B = "周明远", "苏静"       # 规范名（别名表的目标）
ALIAS_A, ALIAS_B = "老周", "小苏"      # 正文里出现的别名
FIXTURE_TEXT = LINE_A + "\n" + LINE_B + "\n" + LINE_N + "\n"
# CPU 成本: 模型加载 ~240s + 3 句 x (1.5~3s 音频 x RTF 57) => 墙钟预算 1800s
# (机器上并发跑着其他任务时实测单句 ~600s，预算用 PYY_C6_BUDGET_S 放宽，
#  合成句子数不增 -> 成本红线仍是"<=3 句")
BUDGET_S = int(os.environ.get("PYY_C6_BUDGET_S", "1800"))
L2_PREVIEW = os.environ.get("PYY_C6_L2", "")   # ="1" 才允许用例7 追加 2 句试听
MIX = {
    "bgm": {"ref": "bgm_warm_pad", "gain_db": -14.0, "ducking": True, "duck_db": 8.0},
    "sfx": {"chapter_marks": False, "items": []},
    "role_gains": {},
    "master": {"gain_db": 0.0, "target_lufs": -16.0},
}


# ============================================================ 音频分析
def frame_db(data, sr, frame_ms=20.0, hop_ms=10.0):
    n, hop = int(sr * frame_ms / 1000), int(sr * hop_ms / 1000)
    if len(data) < n:
        return np.zeros(0, dtype=np.float32)
    idx = np.arange(0, len(data) - n + 1, hop)
    w = np.stack([data[i:i + n] for i in idx]) if idx.size else np.zeros((0, n))
    rms = np.sqrt(np.mean(np.square(w.astype(np.float64)), axis=1)) if idx.size else np.zeros(0)
    return (20 * np.log10(rms + 1e-12)).astype(np.float32)


def vocal_regions(data, sr):
    """按能量切人声区段（阈值 = 峰值帧 -20dB，间隙 <120ms 合并）。"""
    db = frame_db(data, sr)
    if db.size == 0:
        return [], db
    thr = float(db.max()) - 20.0
    active = db > thr
    hop_s = 0.01
    regs, start = [], None
    for i, a in enumerate(active):
        if a and start is None:
            start = i
        elif not a and start is not None:
            regs.append([start * hop_s, i * hop_s])
            start = None
    if start is not None:
        regs.append([start * hop_s, len(active) * hop_s])
    merged = []
    for r in regs:
        if merged and r[0] - merged[-1][1] < 0.12:
            merged[-1][1] = r[1]
        else:
            merged.append(list(r))
    return [r for r in merged if r[1] - r[0] >= 0.25], db


def region_stats(data, sr, r):
    a = data[int(r[0] * sr):int(r[1] * sr)]
    if a.size == 0:
        return None
    spec = np.abs(np.fft.rfft(a * np.hanning(a.size)))
    freqs = np.fft.rfftfreq(a.size, 1.0 / sr)
    centroid = float((spec * freqs).sum() / max(spec.sum(), 1e-9))
    return {"from_s": round(r[0], 3), "to_s": round(r[1], 3),
            "dur_s": round(r[1] - r[0], 3),
            "peak": round(float(np.max(np.abs(a))), 6),
            "rms_db": round(float(20 * np.log10(np.sqrt(np.mean(a ** 2)) + 1e-12)), 2),
            "centroid_hz": round(centroid, 1),
            "md5": __import__("hashlib").md5(a.astype(np.float32).tobytes()).hexdigest()[:12]}


def rms_in(data, sr, t0, t1):
    a = data[int(t0 * sr):max(int(t0 * sr) + 1, int(t1 * sr))]
    if a.size == 0:
        return -120.0
    return float(20 * np.log10(np.sqrt(np.mean(a.astype(np.float64) ** 2)) + 1e-12))


def pick_downloaded_voices(n=2):
    """DB 里 source=downloaded 且参考音频在盘的真实音色（gender 不同优先）。"""
    from backend.models.base import get_session
    from backend.models.voice import VoiceProfile
    session = get_session()
    try:
        rows = session.query(VoiceProfile).filter(
            VoiceProfile.source == "downloaded").all()
        usable = [r for r in rows
                  if r.reference_audio_path and os.path.exists(r.reference_audio_path)]
        picked, seen_g = [], set()
        for r in usable:
            g = (r.gender or "?")
            if g in seen_g:
                continue
            seen_g.add(g)
            picked.append({"id": r.id, "name": r.name, "gender": r.gender,
                           "source": r.source,
                           "ref": r.reference_audio_path})
            if len(picked) >= n:
                break
        if len(picked) < n:
            for r in usable:
                if all(r.id != p["id"] for p in picked):
                    picked.append({"id": r.id, "name": r.name, "gender": r.gender,
                                   "source": r.source, "ref": r.reference_audio_path})
                if len(picked) >= n:
                    break
        return picked, len(usable)
    finally:
        session.close()


# ============================================================ 主流程
def main():
    log = H.LOGS / "c6_e2e_multicast.log"
    be_log = H.LOGS / "c6_backend.log"
    st = {"book_id": None, "project_id": None, "cast_task": None, "conv_task": None,
          "voices": [], "usable_voices": 0, "out": None, "preflight": None,
          "confirm": None, "cast": None}
    with H.Tee(log):
        su = H.Suite("C6-e2e-multicast", "run_c6_e2e_multicast.py")
        H.log(f"  [port] 自选空闲端口 = {PORT}")
        H.log(f"  [budget] 转换任务墙钟预算 = {BUDGET_S}s，正文 {len(FIXTURE_TEXT)} 字 "
              f"3 句: 台词 {LINE_A!r} / {LINE_B!r} + 旁白 {LINE_N[:12]!r}…")
        st["voices"], st["usable_voices"] = pick_downloaded_voices(3)
        H.kv("downloaded 可用音色数", st["usable_voices"])
        for v in st["voices"]:
            H.kv("  选用音色", f"{v['id'][:8]} {v['source']} gender={v['gender']} "
                              f"{os.path.basename(v['ref'])}")

        be = H.start_backend(be_log, port=PORT, wait=120)
        try:
            # ---------------- 1: 入库 + multi 项目 ----------------
            r = su.new("1. 真 HTTP 入库 + multi 项目 + cast 分析任务")
            r.evidence_add(str(log))
            r.evidence_add(str(be_log))
            fx = H.FIXTURES / "sample_c6_mini.txt"
            fx.write_text(FIXTURE_TEXT, encoding="utf-8")
            code, body = H.http("POST", "/books/import",
                               files={"file": ("c6_mini.txt", fx.read_bytes(),
                                              "text/plain")},
                               data={"encoding": "utf-8"}, timeout=300)
            if code != 200:
                r.fail("书籍导入 200", f"{code} {str(body)[:200]}")
                su.finish(exit_when_fail=True)
                return
            st["book_id"] = body["book"]["id"]
            code, pj = H.http("POST", "/dubbing/projects",
                              json_body={"name": f"C6-e2e-{int(time.time())}",
                                         "book_id": st["book_id"], "mode": "multi"})
            if code != 200:
                r.fail("multi 项目创建 200", f"{code} {str(pj)[:200]}")
                su.finish(exit_when_fail=True)
                return
            st["project_id"] = pj["project"]["id"]
            code, cb = H.http("GET", f"/books/{st['book_id']}/chapters")
            chs = (cb or {}).get("chapters") or []
            H.kv("章节/正文", f"{len(chs)} 章, "
                          f"{[ (c['title'], len(c['content'] or '')) for c in chs ]}")
            t0 = time.time()
            code, ac = H.http("POST", f"/dubbing/projects/{st['project_id']}/analyze-cast",
                              json_body={"use_llm": False}, timeout=60)
            H.kv("analyze-cast", f"{code} {str(ac)[:160]}")
            st["cast_task"] = (ac or {}).get("task", {}).get("id")
            terminal = None
            while time.time() - t0 < 180:
                _, pb = H.http("GET", f"/tasks/{st['cast_task']}/progress", timeout=20)
                p = pb.get("progress") or {} if isinstance(pb, dict) else {}
                if p.get("status") in ("completed", "failed", "cancelled"):
                    terminal = p
                    break
                time.sleep(1)
            H.kv("cast 任务终态", f"{terminal} in {time.time()-t0:.1f}s")
            r.check("cast 分析任务 completed",
                    (terminal or {}).get("status") == "completed", str(terminal)[:200])
            code, cast = H.http("GET", f"/dubbing/projects/{st['project_id']}/cast")
            st["cast"] = cast
            roles = (cast or {}).get("roles") or []
            H.kv("自动抽取角色", [(x.get("name"), x.get("role_type"),
                               x.get("voice_id")) for x in roles])
            # 5e 裁定(b)短输入救援: <2000 字且严格门限全灭时 min=1(须引号归属证据)。
            # 迷你正文里 老周(前缀式)/小苏(后缀式) 各 1 句归属对白必须真的被抽出，
            # 而不是只得到旁白 —— 这正是 5d FAIL 的定位点（qa/issues.md CAST-E2E）。
            got_names = {x.get("name") for x in roles}
            r.check(f"自动抽取含两位引号归属说话人({ALIAS_A}/{ALIAS_B})+旁白",
                    {ALIAS_A, ALIAS_B, "旁白"} <= got_names,
                    f"{len(roles)} 个: {sorted(str(n) for n in got_names)}")
            relaxed = ((cast or {}).get("cast") or {}).get("stats", {}) \
                .get("short_text_relaxed")
            H.kv("extractor.stats.short_text_relaxed", relaxed)
            r.check("救援门限生效留痕(short_text_relaxed>=2)",
                    (relaxed or 0) >= 2, str(relaxed))
            narr = [x for x in roles if x.get("role_type") == "narrator"]
            r.check("分析落库角色表含唯一旁白", len(narr) == 1,
                    f"{len(narr)} 个旁白: {[(x.get('name'), x.get('role_type')) for x in roles]}")

            # ---------------- 2: suggestions + confirm(真音色) ----------------
            r = su.new("2. suggestions 真音色池 + confirm 落库(双角色不同音色)")
            if len(st["voices"]) < 2:
                r.block("DB 内无 >=2 个可合成 downloaded 音色", f"usable={st['voices']}")
                su.finish(exit_when_fail=True)
                return
            code, sug = H.http("GET",
                              f"/dubbing/projects/{st['project_id']}/cast/suggestions")
            H.kv("suggestions 池", f"voice_total={sug.get('voice_total')} "
                                f"usable={sug.get('voice_usable')} "
                                f"roles={len(sug.get('roles') or [])}")
            vmap = {ROLE_A: st["voices"][0]["id"], ROLE_B: st["voices"][1]["id"]}
            narr_voice = (st["voices"][2]["id"] if len(st["voices"]) >= 3
                          else st["voices"][0]["id"])
            by_name = {(x.get("name") or "").strip(): x for x in roles}
            # 生产"修正角色表再确认"= 对抽取出的 老周/小苏 行改名归并(role_id+name
            # 走同一行)，而不是另建规范名新行 —— 5e 救援门限让抽取真的给出别名行后，
            # 若另建新行，旧行会把别名表污染成恒等映射(老周->老周)，P5 归一即失效。
            renamed_from = {ROLE_A: ALIAS_A, ROLE_B: ALIAS_B}
            payload = {"auto_assign_rest": False, "roles": []}
            for nm, voice, rtype, aliases, mix in (
                (ROLE_A, vmap[ROLE_A], "hero", [ALIAS_A, "周队长"],
                 {"gain_db": 3.0, "speed": 0.95, "pitch": -1.5}),
                (ROLE_B, vmap[ROLE_B], "supporting", [ALIAS_B],
                 {"gain_db": -3.0, "speed": 1.05, "pitch": 1.0}),
                ("旁白", narr_voice, "narrator", [],
                 {"gain_db": 0.0, "speed": 1.0, "pitch": 0.0}),
            ):
                entry = {"name": nm, "voice_id": voice, "role_type": rtype,
                         "aliases": aliases, "mix": mix}
                src = by_name.get(nm) or by_name.get(renamed_from.get(nm, "\x00"))
                if src and src.get("id"):
                    entry["role_id"] = src["id"]
                payload["roles"].append(entry)
            code, conf = H.http("PUT", f"/dubbing/projects/{st['project_id']}/cast/confirm",
                               json_body=payload)
            st["confirm"] = conf
            H.kv("confirm", f"{code} roles={conf.get('role_count')} "
                         f"aliases={str(conf.get('aliases'))[:160]}")
            r.check("confirm 200 且写了别名表", code == 200 and
                    bool(((conf or {}).get("cast") or {}).get("aliases")),
                    f"{code} {str(conf)[:180]}")
            atbl = (conf or {}).get("aliases") or {}
            r.check(f"别名表归一正确({ALIAS_A}->{ROLE_A}, {ALIAS_B}->{ROLE_B})",
                    atbl.get(ALIAS_A) == ROLE_A and atbl.get(ALIAS_B) == ROLE_B
                    and atbl.get(ROLE_A) == ROLE_A and atbl.get(ROLE_B) == ROLE_B,
                    str(atbl)[:220])
            code, cast2 = H.http("GET", f"/dubbing/projects/{st['project_id']}/cast")
            st["cast"] = cast2
            r.check("抽取行经改名归并无残留(角色行数==3)",
                    len((cast2 or {}).get("roles") or []) == 3,
                    str([(x.get("name"), x.get("role_type"))
                         for x in (cast2 or {}).get("roles") or []]))
            byname = {x.get("name"): x for x in (cast2 or {}).get("roles") or []}
            r.check(f"{ROLE_A}/{ROLE_B} 绑到两个不同真实音色",
                    byname.get(ROLE_A, {}).get("voice_id")
                    != byname.get(ROLE_B, {}).get("voice_id")
                    and bool(byname.get(ROLE_A, {}).get("voice_id")),
                    f"A={byname.get(ROLE_A, {}).get('voice_id')} "
                    f"B={byname.get(ROLE_B, {}).get('voice_id')}")

            # ---------------- 3: 进程内 P1..P6 预检（L3, 不产音频） ----------------
            r = su.new("3. 预检 P1..P6(L3, 只为钉句子->音色): 两句各归一个角色")
            from modules.workflow.conversion_workflow import ConversionWorkflow
            cw = ConversionWorkflow()
            ctx = {"task_id": "c6-preflight", "project_id": st["project_id"],
                   "book_id": st["book_id"], "chapters": chs, "sentences": [],
                   "total_chars": sum(len(c["content"] or "") for c in chs)}
            cw._stage_import(ctx, st["book_id"])
            cw._stage_parse(ctx)
            cw._stage_clean(ctx)
            cw._stage_split(ctx)
            cw._stage_analyze(ctx)
            cw._stage_assign(ctx, st["project_id"])
            pf = [{"text": s.get("text"), "speaker": s.get("speaker"),
                   "voice_id": s.get("voice_id"), "role_name": s.get("role_name"),
                   "role_kind": s.get("role_kind"), "speed": s.get("speed"),
                   "pitch": s.get("pitch")} for s in ctx["sentences"]]
            st["preflight"] = pf
            for p in pf:
                H.kv("  预检句", f"{p['text'][:20]!r} spk={p['speaker']!r} -> "
                             f"{str(p['voice_id'])[:8]} role={p['role_name']} "
                             f"speed={p['speed']} pitch={p['pitch']}")
            voiced = [p for p in pf if (p["text"] or "").strip()]
            r.check("待合成句数在预算内(<=4)", 0 < len(voiced) <= 4,
                    f"{len(voiced)} 句 (预算 {BUDGET_S}s 墙钟, CPU RTF~57)")
            r.check("句子至少归到两个不同角色的音色",
                    len({p["voice_id"] for p in voiced}) >= 2,
                    str(sorted({str(p["voice_id"])[:8] for p in voiced})))
            r.check(f"两句台词经别名表归一到规范名({ROLE_A}/{ROLE_B})",
                    {ROLE_A, ROLE_B} <= {p["role_name"] for p in voiced},
                    str([(p["text"][:12], p["speaker"], p["role_name"]) for p in voiced]))
            r.check("句级 speed/pitch 已由 cast 角色参数写入(P7 输入)",
                    any(p["speed"] not in (None, 1.0) or p["pitch"] not in (None, 0.0)
                        for p in voiced), str([(p["speed"], p["pitch"]) for p in voiced]))

            # ---------------- 4: mix 配置 + 真 HTTP 转换任务 ----------------
            r = su.new("4. POST /api/tasks/convert 真任务(含占位 BGM) -> m4b 成品(L1)")
            code, mx = H.http("PUT", f"/dubbing/projects/{st['project_id']}/mix-settings",
                              json_body={"mix": MIX})
            H.kv("PUT mix-settings", f"{code} {str(mx)[:160]}")
            r.check("mix-settings 落库(非直通)", code == 200
                    and (mx or {}).get("passthrough") is False, str(mx)[:160])
            t0 = time.time()
            code, tv = H.http("POST", "/tasks/convert",
                             json_body={"project_id": st["project_id"],
                                        "book_id": st["book_id"],
                                        "output_format": "m4b",
                                        "settings": {"engine": "voxcpm"}}, timeout=60)
            H.kv("POST /tasks/convert", f"{code} {str(tv)[:180]}")
            if code != 200:
                r.fail("转换任务受理", f"{code} {str(tv)[:200]}")
            else:
                st["conv_task"] = tv["task"]["id"]
                seen, last = [], None
                visited = set()          # 服务端记账的完整阶段序列（进度接口返回）
                busy = 0
                st["progress_busy"] = 0
                while time.time() - t0 < BUDGET_S:
                    pcode, pb = H.http("GET", f"/tasks/{st['conv_task']}/progress",
                                       timeout=30)
                    if not isinstance(pb, dict):
                        # 服务端在事件循环里同步加载模型/合成 -> 轮询连不上或超时（实测存在）
                        busy += 1
                        st["progress_busy"] = busy
                        if busy % 10 == 1:
                            H.kv(f"轮询受阻 {time.time()-t0:.0f}s",
                                 f"code={pcode} {str(pb)[:120]}")
                        time.sleep(5)
                        continue
                    busy = 0
                    p = (pb or {}).get("progress") or {}
                    visited |= {str(x) for x in (p.get("stages_visited") or [])}
                    s = p.get("current_stage")
                    if s and (not seen or seen[-1] != s):
                        seen.append(s)
                        H.kv(f"进度 {time.time()-t0:.0f}s",
                             f"stage={s} progress={p.get('progress')} "
                             f"status={p.get('status')}")
                    if p.get("status") in ("completed", "failed", "cancelled"):
                        last = p
                        break
                    time.sleep(3)
                H.kv("阶段序列(轮询采样)", seen)
                H.kv("阶段序列(服务端 stages_visited)", sorted(visited))
                st["stages_visited"] = sorted(visited)
                H.kv("终态", f"{last} in {time.time()-t0:.1f}s")
                r.check(f"任务在预算 {BUDGET_S}s 内到达终态", last is not None,
                        f"elapsed={time.time()-t0:.1f}s")
                r.check("任务 completed(真实合成,非 mock)",
                        (last or {}).get("status") == "completed",
                        f"{str(last)[:200]}")
                need = ("analyze", "assign", "synthesize", "postprocess", "package")
                # 轨迹取数口径：进度接口的 stages_visited 是"最后一次成功轮询时"内存 tracker
                # 的快照，任务完成后该端点走 DB 兜底分支(conversion_workflow.get_progress
                # :401-409 不带 stages_visited)，P8/P9 若发生在末次成功轮询之后就补不进来
                # (03:58 那轮就是这样假红)。权威留痕是任务记录里的 task["logs"]
                # (PY-120: 每阶段各写 DB 日志)，故取 logs ∪ 采样 ∪ tracker 记账。
                # 可达性验算(5f, 对上一轮真跑 50ada038 的 DB 行只读核对): logs =
                # import/parse/clean/split/analyze/assign/synthesize/loading_model/
                # postprocess/package —— postprocess 确在其中(task_service.
                # update_progress:121-132 每次 current_stage 变化即追加)。
                # 变异口径: 若 P8 未跑(postprocess 崩溃/被跳过)则任务不会 completed 且
                # logs 无该行 -> 本判据红; 若 PY-120 记账回归(不写 logs)同样抓住。
                # 属断言取数口径修订, 产品行为无改动。
                _, tk = H.http("GET", f"/tasks/{st['conv_task']}", timeout=30)
                task = (tk or {}).get("task") or {}
                logged = [str((e or {}).get("stage") or "")
                          for e in (task.get("logs") or []) if isinstance(e, dict)]
                observed = set(seen) | visited | {x for x in logged if x}
                H.kv("阶段序列(DB logs 留痕)", logged)
                st["task_logs_stages"] = logged
                r.check("阶段轨迹覆盖 analyze/assign/synthesize/postprocess/package",
                        all(x in observed for x in need),
                        f"缺失={sorted(set(need) - observed)}\n      DB logs={logged}"
                        f"\n      轮询采样={seen}\n      tracker 记账={sorted(visited)}")
                st["out"] = task.get("output_path")
                H.kv("output_path / format_used", f"{st['out']} / {task.get('format_used')}")
                H.kv("error_message", task.get("error_message"))
                if st["out"] and Path(st["out"]).exists():
                    r.check("成品文件落盘", True,
                            f"{st['out']} size={os.path.getsize(st['out'])}")
                else:
                    r.fail("成品文件落盘", str(st["out"]))

            # ---------------- 5: 成品音频体检 ----------------
            r = su.new("5. 成品体检: 双角色 distinct + BGM 闪避 >=6dB + 无削波(L1)")
            if not (st["out"] and Path(st["out"]).exists()):
                r.block("无成品可体检", str(st["out"]))
            else:
                dec = str(H.TMP / f"c6_decoded_{str(st['conv_task'] or 'na')[:8]}.wav")
                from modules.tts_engine import ffmpeg_utils
                try:
                    ffmpeg_utils.run_ffmpeg(["-y", "-i", st["out"], "-ac", "1",
                                             "-ar", "48000", dec], desc="C6 解码 m4b")
                except Exception as exc:
                    r.fail("ffmpeg 解码 m4b -> wav", f"{type(exc).__name__}: {exc}"[:300])
                    dec = None
                if dec is None or not Path(dec).exists():
                    r.fail("解码产物落盘", str(dec))
                else:
                    import soundfile as sf
                    data, sr = sf.read(dec, dtype="float32")
                    if data.ndim > 1:
                        data = data.mean(axis=1)
                    st["decoded"] = dec
                    st["ff_src"] = str(ffmpeg_utils.resolve_ffmpeg())
                    H.kv("解码 wav", f"{dec} sr={sr} dur={len(data)/sr:.2f}s "
                                  f"ffmpeg={st['ff_src']}")
                    stats = H.audio_stats(dec)
                    H.kv("成品体检", f"peak={stats['peak']} rms_db={stats['rms_db']} "
                                 f"duration={stats['duration_s']}s")
                    regs, db = vocal_regions(data, sr)
                    infos = [x for x in (region_stats(data, sr, rg) for rg in regs) if x]
                    # 5f 归因留痕(可达性验算, 对 03:57 那轮成品只读复测): 能量切段得到的是
                    # "片段"而非"句子"——3 句正文切出 7 段(句内停顿也被分段), 第 i 段不能
                    # 指派给第 i 个角色。故本用例只钉"成品人声片段互异且不重复"; 同一文本
                    # 两个角色 -> 不同音频的命题由 用例3 L3(两句台词 voice_id 不同) +
                    # 用例6(cast 接线日志) 锚定, 不靠区段-角色映射冒充。
                    st["regions"] = infos
                    for x in infos:
                        H.kv("  人声区段", x)
                    r.check("解码后非静音", stats["peak"] > 0.05, str(stats["peak"]))
                    r.check("无削波(峰值 <= 0.99+eps)",
                            stats["peak"] <= 0.995, str(stats["peak"]))
                    r.check("识别到 >=2 个人声能量片段", len(infos) >= 2,
                            f"{len(infos)} 段: {[(x['dur_s'], x['peak']) for x in infos]}")
                    if len(infos) >= 2:
                        a, b = infos[0], infos[1]
                        drel = abs(a["dur_s"] - b["dur_s"]) / max(a["dur_s"], b["dur_s"])
                        r.check("相邻两片段 distinct: 时长相对差 >=5%",
                                drel >= 0.05, f"|{a['dur_s']} - {b['dur_s']}| "
                                              f"= {drel:.1%}")
                        r.check("相邻两片段 distinct: RMS 电平不同(>=0.5dB)",
                                abs(a["rms_db"] - b["rms_db"]) >= 0.5,
                                f"{a['rms_db']} vs {b['rms_db']} dB")
                        r.check("相邻两片段 distinct: 谱质心不同(>=3%)",
                                abs(a["centroid_hz"] - b["centroid_hz"])
                                / max(a["centroid_hz"], b["centroid_hz"]) >= 0.03,
                                f"{a['centroid_hz']} vs {b['centroid_hz']} Hz")
                        r.check("相邻两片段 distinct: 样本内容不同(md5)",
                                a["md5"] != b["md5"], f"{a['md5']} vs {b['md5']}")
                        gaps = []
                        for i in range(len(infos) - 1):
                            t0i, t1i = infos[i]["to_s"], infos[i + 1]["from_s"]
                            if t1i - t0i >= 0.08:
                                gaps.append((t0i, t1i))
                        H.kv("区段间 BGM-only 间隙", gaps)
                        r.check("存在 >=80ms 的 BGM-only 间隙(闪避释放区)", len(gaps) >= 1,
                                str(gaps))
                        if gaps:
                            # 取能量最高的间隙：对 ">=6dB" 断言最保守（间隙里 BGM 越强，差值越小）
                            best = max(gaps, key=lambda g: rms_in(data, sr, g[0], g[1]))
                            bg = rms_in(data, sr, best[0], best[1])
                            vo = max(x["rms_db"] for x in infos)
                            H.kv("人声 vs BGM-only 间隙",
                                 f"{vo:.2f} dBFS vs {bg:.2f} dBFS -> 差 {vo-bg:.2f} dB")
                            r.check("人声段与 BGM-only 段 RMS 差 >= 6dB",
                                    vo - bg >= 6.0, f"{vo:.2f} - {bg:.2f} = {vo-bg:.2f} dB")
                            # 防空洞真: BGM 素材缺失时混音器只写 stats["warnings"] 并跳过铺底
                            # (modules/mixer/audio_mix.py:273-280)，间隙会变成近似数字静音，
                            # 上面那条"差 >=6dB"反而更容易满足 —— 必须另判间隙本身确有铺底能量。
                            r.check("BGM-only 间隙确有铺底(RMS > -60dBFS, 非数字静音)",
                                    bg > -60.0,
                                    f"间隙 {best[0]:.2f}-{best[1]:.2f}s RMS={bg:.2f} dBFS; "
                                    f"-60dBFS 以下=BGM 未落进成品")

            # ---------------- 6: 服务端日志归因(cast/mix 真的接线了) ----------------
            r = su.new("6. 后端日志归因: P5/P6 cast 生效 + P8 mix 生效")
            try:
                txt = Path(be_log).read_text(encoding="utf-8", errors="replace")
            except Exception as exc:
                txt = ""
                r.fail("读取后端日志", str(exc))
            hits = {k: (k in txt) for k in
                    ("cast speaker 归一", "Assign stage: cast 接线生效",
                     "Postprocess stage: mix 接线 applied=True", "cast_hooks")}
            for k, v in hits.items():
                H.kv("日志关键字", f"{k} -> {v}")
            r.check("P5 别名归一在真实任务里发生", hits["cast speaker 归一"], str(hits))
            r.check("P6 assign_with_cast 在真实任务里发生",
                    hits["Assign stage: cast 接线生效"], str(hits))
            r.check("P8 postprocess_with_mix 在真实任务里发生(applied=True)",
                    hits["Postprocess stage: mix 接线 applied=True"], str(hits))
            for ln in [l for l in txt.splitlines()
                       if "cast 接线生效" in l or "mix 接线" in l
                       or "speaker 归一" in l or "恢复了" in l][:8]:
                H.kv("  日志行", ln[:220])
            m = [l for l in txt.splitlines() if "恢复了" in l]
            r.check("引擎恢复真实音色的日志存在(PY-303 在途修复归因)", bool(m),
                    (m[0][:160] if m else "无该行 -> 音色可能未进 _cloned_voices"))

            # ---------------- 7: 同文本双角色对照(L2 真试听 / L2' 落盘段复用) ----------------
            r = su.new("7. 同文本双角色对照: L2 真试听(PYY_C6_L2=1) 或 L2' 落盘段复用")
            remain = BUDGET_S - (time.time() - t0)
            if L2_PREVIEW == "1" and remain >= 240:
                got = {}
                for role in (ROLE_A, ROLE_B):
                    code, raw = H.http("POST",
                                       f"/dubbing/projects/{st['project_id']}/cast/preview",
                                       json_body={"role_name": role, "text": "账在这里。",
                                                  "max_sentences": 1},
                                       timeout=300, raw=True)
                    if code != 200:
                        r.check(f"{role} 试听 200", False, f"{code} {str(raw)[:150]}")
                        continue
                    p = H.TMP / f"c6_prev_{role}.wav"
                    p.write_bytes(raw)
                    d, s2 = sf.read(str(p), dtype="float32")
                    got[role] = {"dur_s": round(len(d) / s2, 3),
                                 "peak": round(float(np.max(np.abs(d))), 6),
                                 "rms_db": round(float(20 * np.log10(
                                     np.sqrt(np.mean(d ** 2)) + 1e-12)), 2),
                                 "md5": __import__("hashlib").md5(
                                     np.asarray(d, dtype=np.float32).tobytes()).hexdigest()[:12]}
                    H.kv(f"  {role} 试听", got[role])
                if len(got) == 2:
                    (ra, rb) = list(got.values())
                    r.check("同文本不同角色 -> 时长/电平/内容均不同",
                            ra["md5"] != rb["md5"] and
                            abs(ra["dur_s"] - rb["dur_s"]) / max(ra["dur_s"], rb["dur_s"])
                            >= 0.05,
                            f"{ra['dur_s']}s vs {rb['dur_s']}s, "
                            f"{ra['rms_db']} vs {rb['rms_db']} dBFS")
            else:
                # 成本红线(任务书): 真合成句 <=3 —— 用例4 的 3 句已用满, 本用例真合成
                # 增量 = 0。任务书同时允许"落盘段复用": 复用**本次运行**用例5 已解码的
                # L1 成品 wav(qa/harness/tmp/c6_decoded_*.wav), 不是上一轮的陈旧证据。
                # 层级如实标注 L2': 不冒充 cast/preview 端点的真试听(端点契约回归由
                # C4 用例 11 覆盖: 200=真音频 / 引擎缺失=503, 绝不静音冒充)。
                # 角色级命题"同一文本两个角色 -> 不同音频"的锚点: 用例3 L3 两句台词
                # voice_id 不同 + 用例6 cast 接线日志; 本用例只补"落盘片段互不相同
                # (无重复拼贴)"这一条盘上实证。能量片段!=句子, 不做区段->角色映射。
                infos7 = st.get("regions") or []
                why = (f"PYY_C6_L2={L2_PREVIEW or 'unset'}" if L2_PREVIEW != "1"
                       else f"剩余预算 {remain:.0f}s < 240s")
                H.kv("L2' 口径", f"走落盘段复用({why}); 真合成增量=0")
                r.check("L2'(落盘段复用): 本次解码产物可用且人声片段 >=2",
                        bool(st.get("decoded")) and len(infos7) >= 2,
                        f"{len(infos7)} 片段, decoded={st.get('decoded')}, {why}")
                md5s = [x["md5"] for x in infos7]
                r.check("L2': L1 落盘片段 md5 全唯一(无重复拼贴); 双角色互异见用例5+用例3",
                        len(infos7) >= 2 and len(set(md5s)) == len(md5s),
                        f"{len(md5s)} 片段 {len(set(md5s))} 唯一 md5; 本用例真合成增量=0")

            H.kv("证据层级", "L1=生产 HTTP 任务成品; L2=同文本双角色真试听"
                             "(PYY_C6_L2=1 才采集); L2'=L1 落盘段复用(默认口径, 见用例7); "
                             "L3=进程内 P1..P6 预检")
            (H.ARTIFACTS / "result_C6-detail.json").write_text(
                json.dumps({k: v for k, v in st.items() if k != "confirm"},
                           ensure_ascii=False, indent=1, default=str), encoding="utf-8")
        finally:
            H.stop_backend(be)
        su.finish(exit_when_fail=False)
        bad = [x for x in su.results if x.verdict == "FAIL"]
        if bad:
            sys.exit(1)
        print("ALL_GREEN (证据层级见用例说明; DEGRADED 不等于缺陷)")


if __name__ == "__main__":
    main()
