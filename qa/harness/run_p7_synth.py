"""
[P7] synthesize 阶段实测: 真实 VoxCPM 推理, 逐句检查非静音

运行（两条路径**必须分两次进程跑**）:
    python qa/harness/run_p7_synth.py --path a      # 默认。进程内真实引擎逐句合成
    python qa/harness/run_p7_synth.py --path b      # 自起后端走 HTTP 端到端
    PYY_TEST_PORT=8000 python qa/harness/run_p7_synth.py --path b
                                                   # B 复用已在 :8000 上的实例（不再要第 2 份 5GB）
    PYY_TEST_PORT=8000 PYY_P7_ENGINE=edgetts python qa/harness/run_p7_synth.py --path b
                                                   # B 换"快档"引擎（EDGE-PACE 计时用，不装权重）
    建议 PYY_P7_SENTENCES=4（默认 4）；只要素材时 1 也够，冷启动开销不变。

B 路径的产物名带引擎 + 句数预算（p7_synth_b_<engine>-n<N>.log /
result_P7-synthB-<engine>-n<N>.json / p7_timings_b_<engine>-n<N>.json /
p7_http_output_<engine>-n<N>.wav）——同一本书换引擎、或同引擎换 max_sentences，
都是**两次不同的测量**，两份证据不能互相覆盖（被覆盖过一次 C5 记录、一次
EDGE-PACE 整书记录，教训记在 qa/LEDGER.md）。**同配置复跑**再加 `-r2` / `-r3`
（首跑不带后缀，所以账本里已引用的名字不受影响）。A 路径
恒为进程内 voxcpm，只有 `-rN` 这一层。

跨引擎比计时前先读这句：`max_sentences` 限的是**句数**，而分句策略按引擎注入
（US-B2/M7 `_resolve_split_policy`），所以同一个 n 在不同引擎下对应的字数不同
（09-24 06:4x 实测：n=4 时 edgetts 处理 88 字、voxcpm 处理 22 字）。要么按
processed_chars 归一再比，要么就别把"每字符秒数"当常数引用。

真实路径 A: 生产 ConversionWorkflow._stage_synthesize + 真实引擎(进程内加载权重)
            -> 逐句 npy 落盘, 用峰值/RMS 证明不是静音, 也不是 _mock_synthesize
            -> p8 / p9 / C5 的素材（qa/round-1/artifacts/p7_segments）就是这一枚产的
真实路径 B: 起后端 -> POST /api/tasks/convert (max_sentences 限流)
            -> 轮询进度 -> 下载产物 -> 音频体检

两条路径各自收尾删掉自己造的夹具书（B 还删 task 行），并打印跑测前后的库计数自证
—— 历史形态是"A 导的书留给 B 删"，拆成两次进程后没人删，就会往用户的库里塞测试书。

为什么拆成 `--path a` / `--path b`（PY-337 波的结构性结论，09-23 06:2x 实测）：
一条跑测里先 A 后 B 时，A 的 `engine_registry.unload_all()` 之后**进程仍不还那
~15GB private commit**（torch 的 CPU 权重 + 缓存不会随 gc 回到 OS），紧着起的
B 后端自带预热，在 `free 2.42GB` 的机器上判"照装"、然后死在 `loading_model` 里
——那次 p7 就是死在这一步，红的是容量窗不是产品。所以两条路径各占一次进程：
A 结束后 OS 才真正回收，B 起跑时才有它那份 5GB。两条路径的日志/JSON/计时也各自
成套（p7_synth_a.log / result_P7-synthA.json / p7_timings_a.json 与 _b 同理），
不再互相覆盖 —— 覆盖过一次 C5 记录的教训记在 qa/LEDGER.md。

B 路径另配一道**容量预检**（`PYY_P7_MIN_FREE_GB`，默认 6.0GB）：不够就是 BLOCKED
并打印实测余量，不记产品 FAIL —— 拿不到容量窗是一次没跑成的测量，不是一次失败的产品。
"""
import argparse
import os
import sys
import time
import asyncio
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness_lib as H  # noqa: E402

import numpy as np  # noqa: E402

N_SENT = int(os.environ.get("PYY_P7_SENTENCES", "4"))
MIN_FREE_GB = float(os.environ.get("PYY_P7_MIN_FREE_GB", "6.0"))
# 只作用于 B 路径（HTTP 端到端）。默认 voxcpm 是红线④那一档；`edgetts` 用来量
# "快档"到底多快——它不装权重，所以不需要那 5GB 容量窗，16GB 的机器随时能跑。
ENGINE_B = os.environ.get("PYY_P7_ENGINE", "voxcpm")
RUN_TAG = ""                 # 同配置复跑的去覆盖后缀，由 main() 现算（首跑为空）
SEG_DIR = H.ARTIFACTS / "p7_segments"
# 九阶段权威名单（modules/workflow/pipeline.py:18-27 PipelineStage 的 value，
# loading_model 不在其中——它是 PY-115 在 synthesize 里发的子事件）
STAGES_CANONICAL = ("import", "parse", "clean", "split", "analyze",
                    "assign", "synthesize", "postprocess", "package")


def parse_path() -> str:
    ap = argparse.ArgumentParser(
        description="P7 合成实测：A=进程内真实引擎逐句；B=自起后端 HTTP 端到端。"
                    "两条路径不能同进程共存（见模块文档），一次只跑一条。")
    ap.add_argument("--path", choices=("a", "b"), default="a",
                    help="a（默认，产 p8/p9/C5 的素材）或 b（HTTP 端到端真机证据）")
    return ap.parse_args().path


def _import_fixture_book(r):
    """导入夹具书，返回 book_id（A 取章节用，B 整条流水线用）。"""
    code, body = H.http("POST", "/books/import",
                        files={"file": ("p7_sample.txt",
                                        (H.FIXTURES / "sample_small.txt").read_bytes(),
                                        "text/plain")}, timeout=300)
    if code != 200:
        r.block("书籍导入失败", str(body)[:200])
        return None
    return body["book"]["id"]


def _capacity_gate(r) -> bool:
    """装一份 voxcpm 前先看机器还给不给得出这 5GB。

    不够 ⇒ BLOCKED 并打印实测余量，**不记产品 FAIL**：拿不到容量窗是一次没做成的
    测量，不是一次失败的合成。这条门对 A/B 两路同用（A 是进程内装载，同样吃 5GB）。
    """
    try:
        import psutil
        free = psutil.virtual_memory().available / 2 ** 30
    except Exception as e:            # 取不到余量就当"够"，让真机去判
        H.kv("容量预检", f"WARN 取不到可用内存: {e}")
        return True
    H.kv("容量预检可用内存", f"{free:.2f}GB / 门限 {MIN_FREE_GB}GB")
    if free < MIN_FREE_GB:
        r.block("可用内存低于点火门限",
                f"free={free:.2f}GB < {MIN_FREE_GB}GB（voxcpm 单份约 5GB）。"
                f"先按 qa/LEDGER.md 的点火容量清单腾出容量窗再复跑；本条不作产品判定。")
        return False
    return True


# ============================ A: 进程内真实引擎 ============================
def run_a(su, timings):
    r = su.new("A. 引擎真实加载(非降级/非模拟)")
    r.evidence_add(timings["_log"])
    if not _capacity_gate(r):
        return
    from modules.tts_engine.engine_registry import engine_registry
    t0 = time.time()
    engine = engine_registry.get_loaded("voxcpm")
    timings["engine_load_s"] = round(time.time() - t0, 1)
    H.kv("engine", type(engine).__name__ if engine else None)
    H.kv("load_seconds", timings["engine_load_s"])
    if engine is None:
        r.fail("引擎为 None -> 合成全部走静音兜底", "见 PY- 引擎缺失条目")
        return
    H.kv("_model", type(engine._model).__name__ if engine._model else "None")
    H.kv("device", getattr(engine, "device", "?"))
    H.kv("adapter.sample_rate", engine.sample_rate)
    inner = getattr(engine._model, "tts_model", None)
    H.kv("model.sample_rate", getattr(inner, "sample_rate", "?"))
    r.check("模型对象非 None (未落入 _mock_synthesize)", engine._model is not None,
            f"_model={engine._model}")
    r.check("引擎加载耗时可接受(<120s)", timings["engine_load_s"] < 120,
            f"CPU 冷启动 {timings['engine_load_s']}s -> 首次转换前 UI 无反馈")

    # ---------- 生产 _stage_synthesize 逐句 ----------
    r = su.new("A2. 生产 _stage_synthesize 逐句真实推理 + 非静音校验")
    chs = []
    pre = _db_counts()
    be = H.start_backend(H.LOGS / "p7_backend_a.log",
                         env_extra={"PYY_PRELOAD_MODEL": "0"})
    try:
        bid = _import_fixture_book(r)
        if bid:
            _, cb = H.http("GET", f"/books/{bid}/chapters", timeout=120)
            chs = cb.get("chapters", [])
            # WF-09 起列表只下发预览，正文须走懒加载出口补（同 run_p4_split 口径）。
            # 不补则 all_s 为空 -> 生产 _stage_synthesize 按 PY-117 正确报错，
            # 红的是取数口径，不是合成链路。
            chs = H.fill_chapter_content(chs)
            H.kv("章节数/正文合计字符", f"{len(chs)} / {sum(len(c['content']) for c in chs)}")
            # 夹具书在这条路径里只是"取正文的载体"，取完就删：留着它就往用户的库里
            # 塞一本测试书（历史形态是等 B 路径收尾删，拆成两次跑测后没人删了）。
            H.http("DELETE", f"/books/{bid}")
    finally:
        # 释放后端进程占用的内存: 模型只能有一份(8.2GB 可用 / 单份约 5GB)
        H.stop_backend(be)
    post = _db_counts()
    if pre and post:
        H.kv("取数期库计数 前/后", f"books {pre['books']}->{post['books']} / "
             f"tasks {pre['tasks']}->{post['tasks']}")
        r.check("取数不留库内残留", post == pre, f"pre={pre} post={post}")
    try:
        if chs:
            from modules.text_processor.sentence_splitter import SentenceSplitter
            sp = SentenceSplitter()
            all_s = []
            for ch in chs:
                for s in sp.split(ch["content"] or "", "zh"):
                    if len(s.strip()) >= 6:
                        all_s.append({"chapter_id": ch["id"],
                                      "chapter_title": ch["title"],
                                      "text": s, "char_count": len(s),
                                      "tone": "neutral", "voice_id": None})
            picked = [s for s in all_s if "“" not in s["text"]][:N_SENT]
            if len(picked) < N_SENT:
                picked = all_s[:N_SENT]
            H.kv("候选句总数", len(all_s))
            for s in picked:
                H.kv("  送入合成", repr(s["text"]))
            ctx = {"task_id": "harness-p7a", "sentences": [dict(s) for s in picked],
                   "total_chars": sum(len(s["text"]) for s in picked)}
            from modules.workflow.conversion_workflow import ConversionWorkflow
            cw = ConversionWorkflow()
            t0 = time.time()
            out = asyncio.run(cw._stage_synthesize(ctx, {"engine": "voxcpm"}))
            synth_wall = time.time() - t0
            segs = out["audio_arrays"]
            timings["stage_synth_wall_s"] = round(synth_wall, 1)
            H.kv("生产 _stage_synthesize 返回段数", f"{len(segs)} / 输入 {len(picked)}")
            H.kv("总耗时", f"{synth_wall:.1f}s (含 {len(picked)} 句真实推理)")
            peaks = []
            silent = []
            for seg in segs:
                a = np.asarray(seg["audio"], dtype=np.float32)
                pk = float(np.max(np.abs(a))) if len(a) else 0.0
                rms = float(np.sqrt(np.mean(a ** 2))) if len(a) else 0.0
                nz = float((np.abs(a) > 1e-4).mean()) if len(a) else 0.0
                peaks.append(pk)
                H.kv(f"  句{seg['index']} ({seg['text'][:16]!r})",
                     f"len={len(a)} dur={seg['duration_ms']/1000:.2f}s "
                     f"peak={pk:.4f} rms={rms:.4f} active={nz:.2%}")
                (SEG_DIR / f"seg{seg['index']:02d}.npy").write_bytes(a.tobytes())
                with open(SEG_DIR / f"seg{seg['index']:02d}.json", "w", encoding="utf-8") as f:
                    json.dump({"text": seg["text"], "len": len(a), "sr": out["sample_rate"],
                               "peak": pk, "rms": rms}, f, ensure_ascii=False)
                if pk < 1e-3:
                    silent.append(seg["index"])
            r.check("每段音频峰值均显著非零 (真实人声)", not silent,
                    f"静音段: {silent}  peaks={peaks}")
            r.check("段数与句数一致", len(segs) == len(picked),
                    f"{len(segs)} vs {len(picked)}")
            # 与 _mock_synthesize 的输出特征对比: mock 是纯零且按 24000 采样
            mock = np.zeros(int(max(len(picked[0]["text"]) * 0.08, 0.3) * 24000),
                            dtype=np.float32)
            H.kv("对照组 _mock_synthesize 特征", f"len={len(mock)} peak=0.0")
            r.check("输出不等同 mock 静音", all(p > 0 for p in peaks),
                    f"peaks={peaks} (mock 恒为 0)")
            # 时长合理性: 中文约 4-6 字/秒
            for seg, pk in zip(segs, peaks):
                a = np.asarray(seg["audio"], dtype=np.float32)
                dur = len(a) / out["sample_rate"]
                cps = len(seg["text"]) / max(dur, 0.01)
                H.kv(f"  句{seg['index']} 语速", f"{cps:.1f} 字/秒 (dur={dur:.2f}s)")
            rt = synth_wall / max(sum(len(s["audio"]) / out["sample_rate"] for s in segs), 0.01)
            timings["rtf"] = round(rt, 2)
            H.kv("RTF(合成耗时/音频时长)", timings["rtf"])
            H.kv("单句平均耗时", f"{synth_wall/max(len(segs),1):.1f}s")
            r.check("CPU 实时率可接受(RTF<10)", rt < 10,
                    f"RTF={rt:.1f} -> 33 万字书籍按 52 句/千字估算需数十小时")
    finally:
        r.evidence_add(SEG_DIR)
        H.kv("逐句产物目录", str(SEG_DIR))
        # 释放本进程的模型（OS 真正回收要等本进程退出 —— 这就是 B 不能同树的原因）
        try:
            from modules.tts_engine.engine_registry import engine_registry
            engine_registry.unload_all()
            import gc
            gc.collect()
        except Exception as e:
            H.kv("unload_all", f"WARN {e}")


# ============================ B: HTTP 端到端 ============================
def _db_counts():
    """只读计数，用来自证"这枚跑测没往库里留东西"（#85 HARNESS-LEAK 同一条线）。"""
    try:
        return {"books": H.db_rows("SELECT COUNT(*) AS n FROM books")[0]["n"],
                "tasks": H.db_rows("SELECT COUNT(*) AS n FROM conversion_tasks")[0]["n"]}
    except Exception as e:
        H.kv("库计数", f"WARN 取不到: {e}")
        return None


def _engine_load_seconds(model_payload, engine):
    """取**被测引擎自己那一格**的加载秒数。

    顶层 `model.load_seconds` 镜像的是 primary/最后一次装载的那个槽位，不是本次被测的
    引擎 —— 09-24 06:0x 那跑就是拿它读出了「voxcpm 服务端自报加载 0.9s」（0.9 是
    edgetts 那格留下的）。槽位缺失（老服务端 / 该引擎还没进过 slots）才回落顶层，
    并在返回里标明用的是哪一层，避免把回落值当槽位实测值记账。
    """
    m = model_payload or {}
    slot = (m.get("slots") or {}).get(engine) or {}
    if slot.get("load_seconds") is not None:
        return slot["load_seconds"], f"slots.{engine}"
    return m.get("load_seconds"), "顶层(primary 镜像, 非本引擎槽位)"


def run_b(su, timings):
    r = su.new("B. HTTP /api/tasks/convert 端到端真实合成 (wav)")
    if H.port_open(H.TEST_PORT):
        # 复用已在跑的实例（`PYY_TEST_PORT=8000` 就指向用户那台）。模型是它自己那份，
        # 本进程不再要第 2 份 5GB —— 容量门拦的是"还要再起一份"，这里跳过它。
        _, m = H.model_status()
        load_s, src = _engine_load_seconds(m, ENGINE_B)
        H.kv("复用实例", f"{H.BASE} | model_status={m.get('state')} | "
             f"{ENGINE_B} 服务端自报加载 {load_s}s (取 {src})")
        timings["attached"] = H.BASE
    elif not _capacity_gate(r):
        return

    pre = _db_counts()
    # B 自己导书：A 不在这台进程里跑，也就没有它留下的 bid
    be = H.start_backend(H.LOGS / "p7_backend_b.log")
    task_id = bid = None
    try:
        bid = _import_fixture_book(r)
        if not bid:
            return
        settings = {"engine": ENGINE_B, "max_sentences": N_SENT, "speed": 1.0}
        timings["engine"] = ENGINE_B
        t0 = time.time()
        code, body = H.http("POST", "/tasks/convert",
                            json_body={"project_id": None, "book_id": bid,
                                       "output_format": "wav", "settings": settings},
                            timeout=60)
        H.kv("POST /tasks/convert", f"{code} {str(body)[:180]}")
        if code != 200:
            r.fail("转换请求被拒绝", str(body)[:200])
            return
        task_id = body["task"]["id"]
        seen_stages = []           # 我这边**采样到**的阶段（受 2s 轮询间隔限制）
        srv_stages = set()         # 服务端自报的累计清单（不受采样影响）
        # 逐句边界的来源：产品自己按句推进的 `processed_chars`。采样只在 (stage, chars)
        # 变化时记账，所以这份序列既是"每句何时落地"，又不会在长任务里撑爆。
        # 为什么必须有它：09-24 06:34 那跑 voxcpm n=4 的终态是 processed_chars=22/791
        # —— 只落地了 4 句里的 1 句，而当时那台实例 stdout 没落档（PY-346 同根），
        # 于是"22 字是 4 句还是 1 句"这件事**永远归不了因**。序列在 json 里就没有第二回。
        series = []                # [{"t_s","stage","chars"}]
        last = None
        out_file = None
        while time.time() - t0 < 2400:
            _, pb = H.http("GET", f"/tasks/{task_id}/progress", timeout=30)
            p = pb.get("progress", {}) if isinstance(pb, dict) else {}
            stage = p.get("current_stage")
            chars = p.get("processed_chars")
            if not series or (series[-1]["stage"], series[-1]["chars"]) != (stage, chars):
                series.append({"t_s": round(time.time() - t0, 1),
                               "stage": stage, "chars": chars})
            if stage and (not seen_stages or seen_stages[-1] != stage):
                seen_stages.append(stage)
                H.kv(f"进度 {round(time.time()-t0,1)}s",
                     f"stage={stage} progress={p.get('progress')} "
                     f"chars={chars}/{p.get('total_chars')} "
                     f"status={p.get('status')}")
            srv_stages.update(p.get("stages_visited") or [])
            if p.get("status") in ("completed", "failed", "cancelled"):
                last = p
                break
            time.sleep(2)
        timings["http_e2e_s"] = round(time.time() - t0, 1)
        timings["progress_series"] = series
        # 由序列导出的逐句边界：chars 每次跳变就是产品推进了一句（或几句）
        edges = []
        for a, b in zip(series, series[1:]):
            dc = (b["chars"] or 0) - (a["chars"] or 0)
            if dc > 0:
                edges.append({"chars": dc, "s": round(b["t_s"] - a["t_s"], 1)})
        timings["per_sentence_s"] = edges
        H.kv("看到的阶段序列", seen_stages)
        H.kv("逐句边界(字数,耗时s)", edges if edges else "未采到跳变（轮询比句子慢或只有一句）")
        if edges:
            H.kv("逐句统计", f"最少 {min(e['s'] for e in edges)}s / 最多 "
                 f"{max(e['s'] for e in edges)}s / 合计 {round(sum(e['s'] for e in edges),1)}s "
                 f"覆盖 {sum(e['chars'] for e in edges)} 字")

        H.kv("服务端累计阶段", sorted(srv_stages))
        H.kv("终态", last)
        # 冷启动秒数取服务端自报（独立跑测时没有 A 路径那份计时），且取**被测引擎那一格**
        _, mb = H.http("GET", "/system/model_status", timeout=20)
        load_s, src = _engine_load_seconds((mb or {}).get("model"), ENGINE_B)
        timings["engine_load_s"] = load_s
        H.kv("服务端自报模型加载秒数", f"{load_s} (取 {src})")
        r.check("端到端在 40 分钟内完成", timings["http_e2e_s"] < 2400,
                f"{timings['http_e2e_s']}s (含后端冷启动加载模型 {load_s}s)")
        r.check("任务最终 completed", (last or {}).get("status") == "completed",
                str(last)[:200])
        _, tk = H.http("GET", f"/tasks/{task_id}", timeout=30)
        task = tk.get("task", {}) if isinstance(tk, dict) else {}
        # 阶段覆盖判据取**落库的逐阶段留痕** `task.logs`（PY-120：每次阶段推进追加一条），
        # 不取轮询采样、也不取 `stages_visited`：
        # - 采样：P1~P6 在 1s 内连跑完，2s 轮询必漏（09-24 02:55 那跑就是这么造出假 FAIL 的）；
        # - `stages_visited`：它只在**内存 tracker 还活着**时才有（`get_progress` 一落到
        #   DB 回落分支就没有这个键），而 postprocess/package 常常整个塞在同一次轮询的
        #   两个 2s 之间 → 02:57 复跑实测 `缺席=['postprocess']`，那枚红是量具的。
        log_stages = [e.get("stage") for e in (task.get("logs") or [])
                      if isinstance(e, dict)]
        hit_log, seen_log = [], set()
        for s in log_stages:
            if s and s not in seen_log:
                seen_log.add(s)
                hit_log.append(s)
        stages_hit = seen_log | set(seen_stages) | srv_stages
        missing = [s for s in STAGES_CANONICAL if s not in stages_hit]
        H.kv("DB 逐阶段留痕", hit_log)
        r.check("9 个阶段都被推进过", not missing,
                f"缺席={missing} | logs 留痕={hit_log} | "
                f"tracker 累计={sorted(srv_stages)} | 采样序列={seen_stages}")
        out_file = task.get("output_path")
        H.kv("task.output_path", out_file)
        H.kv("task.error_message", task.get("error_message"))
        if out_file and Path(out_file).exists():
            st = H.audio_stats(out_file)
            for k in ("size", "sr", "duration_s", "peak", "rms_db",
                      "active_ratio", "is_silent"):
                H.kv(f"  产物.{k}", st.get(k))
            r.check("产物时长 > 0", st.get("duration_s", 0) > 0, st.get("duration_s"))
            r.check("产物峰值非零(非静音冒充成功)", st.get("peak", 0) > 0.01,
                    f"peak={st.get('peak')} rms={st.get('rms_db')}dB")
            copied = H.ARTIFACTS / f"p7_http_output_{ENGINE_B}-n{N_SENT}{RUN_TAG}.wav"
            try:
                import shutil
                shutil.copy2(out_file, copied)
                r.evidence_add(copied)
                H.kv("产物已归档", str(copied))
            except Exception as e:
                H.kv("归档失败", e)
        else:
            r.fail("未产出可检查的音频文件", f"output_path={out_file}")
            r.evidence_add(H.LOGS / "p7_backend_b.log")
        # 下载端点
        code, raw = H.http("GET", f"/tasks/{task_id}/download", timeout=60, raw=True)
        H.kv("GET /download", f"{code} bytes={len(raw) if isinstance(raw, bytes) else raw}")
        r.check("下载端点返回音频字节", code == 200 and isinstance(raw, bytes)
                and len(raw) > 1000, f"{code}")
    finally:
        # 清理自证：task 行先删（它挂着产物文件），再删书（级联章节）。历史形态只删书，
        # 于是跑测过的 conversion_tasks 一直留在库里 —— 那是 #85 HARNESS-LEAK 的同一笔账。
        if task_id:
            H.http("DELETE", f"/tasks/{task_id}")
        if bid:
            H.http("DELETE", f"/books/{bid}")
        post = _db_counts()
        if pre and post:
            H.kv("库计数 跑测前/后", f"books {pre['books']}->{post['books']} / "
                 f"tasks {pre['tasks']}->{post['tasks']}")
            r.check("跑测不留库内残留", post == pre, f"pre={pre} post={post}")
        H.stop_backend(be)


# ============================ C: 两条路径都要过的静态归因 ============================
def run_static_attribution(su):
    r = su.new("C. 兜底分支归因检查(静音兜底是否会被计入成功)")
    r.evidence_add(H.PROJECT_ROOT / "modules/workflow/conversion_workflow.py")
    src = (H.PROJECT_ROOT / "modules/workflow/conversion_workflow.py").read_text(
        encoding="utf-8")
    seg = src.split("async def _stage_synthesize", 1)[1].split("def _stage_postprocess", 1)[0]
    H.kv("静音兜底出现次数", seg.count("np.zeros"))
    pkg = src.split("def _stage_package", 1)[1].split("def _write_wav", 1)[0]
    H.kv("package 阶段是否校验峰值", "np.max" in pkg and "raise" in pkg)
    r.check("合成失败会中止任务而非产出静音",
            "np.zeros" not in seg,
            "except 分支用 np.zeros 静音替换失败句并继续; package 不做整体静音校验 "
            "-> 引擎全挂时任务仍标 completed 并产出静音文件")
    er = (H.PROJECT_ROOT / "modules/tts_engine/engine_registry.py").read_text(
        encoding="utf-8")
    r.check("get_loaded 模型加载失败会明确报错", "raise" in er.split("def get_loaded", 1)[1]
            .split("def get_all_voices", 1)[0],
            "except 分支只 logger.warning('可能降级到静音/模拟') 后 return engine")


def main():
    path = parse_path()
    # B 路径的产物名带上引擎 + 句数预算：换引擎或换 payload 再跑都不能覆盖前一份证据
    # （覆盖过一次 C5 记录、一次 EDGE-PACE 整书记录，教训记在 qa/LEDGER.md）。
    # 只带引擎不够 —— 同引擎不同 max_sentences 是两次不同的测量。A 恒为进程内 voxcpm，不改名。
    base_suffix = f"_{ENGINE_B}-n{N_SENT}" if path == "b" else ""
    base_stage = f"P7-synth{path.upper()}" + (f"-{ENGINE_B}-n{N_SENT}" if path == "b" else "")
    # `-n<N>` 挡得住"换 payload 覆盖"，挡不住"同 payload 复跑覆盖"：09-24 07:36 与 08:03
    # 两跑 voxcpm n=4 同名互覆，前一跑的逐句序列只剩当时的 stdout。首跑仍用文档里的名字，
    # 其后再复跑同配置依次 -r2 / -r3。
    global RUN_TAG

    tag = ""
    rep = 2
    while (H.ARTIFACTS / f"result_{base_stage}{tag}.json").exists():
        tag = f"-r{rep}"
        rep += 1
    RUN_TAG = tag
    suffix = base_suffix + tag
    stage_name = base_stage + tag
    SEG_DIR.mkdir(parents=True, exist_ok=True)
    log = H.LOGS / f"p7_synth_{path}{suffix}.log"
    timings = {"_log": str(log), "path": path}
    with H.Tee(log):
        su = H.Suite(stage_name, "run_p7_synth.py")
        H.kv("本次只跑路径", path.upper())
        H.kv("另一条路径须另起进程", "python qa/harness/run_p7_synth.py --path "
             + ("b" if path == "a" else "a"))
        (run_a if path == "a" else run_b)(su, timings)
        run_static_attribution(su)
        timings.pop("_log", None)
        (H.ARTIFACTS / f"p7_timings_{path}{suffix}.json").write_text(
            json.dumps(timings, ensure_ascii=False, indent=2), encoding="utf-8")
        su.finish(exit_when_fail=False)


if __name__ == "__main__":
    main()
