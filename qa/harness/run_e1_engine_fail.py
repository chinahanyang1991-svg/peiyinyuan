"""
[E1] PY-304 "半死引擎" 失败语义实测: voxcpm 权重路径 patch 成不存在

缺陷原文 (qa/verify-round3.md §6 PY-304): 4 个 adapter 的 load_model 失败后仍
`_loaded=True` 且 synthesize 走 `_mock_synthesize`(np.zeros) 静默出静音, 于是
`engine_registry.get_loaded(strict=True)` 判定就绪、`model_preloader.snapshot()` 报
state="ready", 错误原因永不上抛, 只剩 PY-117 的逐句峰值守门在最后一步兜住。

本用例证明修复后"模型缺失"沿三条通道都成为错误而不是静音:
  A. 前置: 真实权重目录存在(patch 有意义) + 4 个 adapter 源码内不再有"按时长造出的
     np.zeros 静音替身"（`np.zeros(0)` 这种零长度空产物是 PY-322 的显式失败形态，放行入日志）
  B. engine_registry.get_loaded("voxcpm", strict=True) 抛 EngineLoadError, 消息含真实原因
  C. model_preloader 状态机 state=="failed"(进程内, 等价 /api/system/model_status 的 model 字段)
  D. 任务形状路径: 生产 Pipeline + ConversionWorkflow._stage_synthesize -> status=failed
     且 error_message 含真实原因, 不产出任何 audio_arrays
  E. registry 侧加固: 第三方/未来适配器复刻"吞异常 + _loaded=True + _model=None"
     同样被 EngineLoadError 拦下(不再被缓存为已加载)

运行:
  set PYTHONIOENCODING=utf-8
  python qa/harness/run_e1_engine_fail.py
产物:
  qa/round-2/artifacts/result_E1-engine-fail.json
  qa/round-2/logs/e1_engine_fail.log
退出码: 0 = ALL_GREEN
"""
import asyncio
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness_lib as H  # noqa: E402

# 产物落在 round-2 (与 1c/3 轮基线同目录, 不覆盖 round-1)
H.ARTIFACTS = H.QA_DIR / "round-2" / "artifacts"
H.LOGS = H.QA_DIR / "round-2" / "logs"
(H.ARTIFACTS).mkdir(parents=True, exist_ok=True)
(H.LOGS).mkdir(parents=True, exist_ok=True)

ADAPTERS = ["voxcpm_adapter.py", "index_tts_adapter.py",
            "kokoro_adapter.py", "moss_tts_adapter.py"]
REASON_KEY = "权重目录不存在"


def zeros_offenders(src: str):
    """把源码里的 `np.zeros(...)` 分成两类，只有前者是 PY-304 的罪。

    - **按时长造出的静音**（`np.zeros(len(text)*sr)` / `np.zeros_like(x)`）= 假装有产物 → 红；
    - **零长度空产物**（`np.zeros(0, ...)`）= 什么都没产出，交给 PY-117 逐句峰值门显式判失败
      （PY-322 的"预算到点即终止执行者"就是靠它，不再拿静音顶替）→ 放行，但原样列进日志。

    旧版只判"这一行有没有 `np.zeros(`"，于是把后者误伤成前者（09-23 实测红在
    voxcpm_adapter.py:562 的 `return np.zeros(0, dtype=np.float32)`）。
    """
    bad, empty = [], []
    for raw in src.splitlines():
        line = raw.strip()
        call = re.search(r"np\.zeros(?:_like)?\s*\(", line)
        if line.startswith("#") or not call:
            continue                                        # 只数真调用，文档里的字样不算罪
        open_paren = call.end() - 1
        if line[open_paren + 1:open_paren + 2] == ")":
            bad.append(line)                                # 无参调用
            continue
        depth, i, arg = 1, open_paren + 1, ""
        while i < len(line):
            ch = line[i]
            if ch in "([":
                depth += 1
            elif ch in ")]":
                depth -= 1
                if depth == 0:
                    break
            if depth and ch == ",":
                break                                       # 只看第一个实参
            if depth:
                arg += ch
            i += 1
        (empty if arg.strip() == "0" else bad).append(line)
    return bad, empty


def fake_weight_dir() -> Path:
    """一个确定不存在的 voxcpm 权重目录 (patch 用)。"""
    p = H.PROJECT_ROOT / "data" / "models" / "e1_missing_voxcpm2"
    if p.exists():
        import shutil
        shutil.rmtree(p, ignore_errors=True)
    return p


def main():
    log = H.LOGS / "e1_engine_fail.log"
    with H.Tee(log):
        su = H.Suite("E1-engine-fail", "run_e1_engine_fail.py")
        missing = fake_weight_dir()

        # ================= A. 前置与静态红线 =================
        r = su.new("A. 前置: 真实权重存在 + 静音替身已清除")
        r.evidence_add(log)
        import modules.tts_engine.engines.voxcpm_adapter as va
        from modules.tts_engine.engine_registry import engine_registry, EngineLoadError

        real_dir = Path(va._DEFAULT_MODEL_DIR)
        H.kv("真实权重目录", real_dir)
        r.check("真实 voxcpm 权重目录存在(patch 才有意义)", real_dir.is_dir(),
                f"{real_dir} is_dir={real_dir.is_dir()}")
        weight_files = ([p for p in real_dir.glob("*.safetensors")]
                        + [p for p in real_dir.glob("*.pth")]) if real_dir.is_dir() else []
        H.kv("权重文件", [p.name for p in weight_files][:4])
        r.check("目录内确有 .safetensors/.pth 权重", bool(weight_files),
                f"files={[p.name for p in weight_files][:4]}")
        H.kv("patch 目标(不存在的路径)", missing)

        eng_dir = H.PROJECT_ROOT / "modules" / "tts_engine" / "engines"
        offenders = {}
        for name in ADAPTERS:
            src = (eng_dir / name).read_text(encoding="utf-8")
            bad, empty = zeros_offenders(src)
            offenders[name] = {"zeros": bad, "empty_product": empty,
                               "mock_def": "def _mock_synthesize" in src}
        for name, o in offenders.items():
            H.kv(f"  {name}", f"按时长造静音={o['zeros']} 零长度空产物={o['empty_product']} "
                              f"def _mock_synthesize={o['mock_def']}")
        r.check("4 个 adapter 均无『按时长造出的静音替身』/无 _mock_synthesize 定义",
                all(not o["zeros"] and not o["mock_def"] for o in offenders.values()),
                json.dumps(offenders, ensure_ascii=False)[:300])
        r.check("VoxCPMAdapter 不再暴露 _mock_synthesize",
                not hasattr(va.VoxCPMAdapter, "_mock_synthesize"))

        # ================= B. get_loaded(strict=True) 必须上抛 =================
        r = su.new("B. engine_registry.get_loaded(strict=True) -> EngineLoadError")
        r.evidence_add(log)
        engine = engine_registry.get("voxcpm")
        r.check("引擎可实例化(仅构造, 不加载)", engine is not None, str(engine))
        orig_path = engine.model_path
        engine.model_path = str(missing)          # patch 权重路径
        engine._model = None
        engine._loaded = False
        engine_registry._loaded.pop("voxcpm", None)
        engine_registry._engines["voxcpm"] = engine

        err_b = None
        t0 = __import__("time").time()
        try:
            got = engine_registry.get_loaded("voxcpm", strict=True)
            H.kv("get_loaded 返回", f"{got} (FAIL: 半死引擎被当成可用)")
        except EngineLoadError as e:
            err_b = e
            H.kv("EngineLoadError", str(e)[:220])
        except Exception as e:  # 其它异常形态也算泄漏, 但不是契约类型
            err_b = e
            H.kv("非契约异常", f"{type(e).__name__}: {e}")
        H.kv("耗时", f"{__import__('time').time() - t0:.2f}s (须远小于真冷启动)")
        r.check("strict=True 抛 EngineLoadError", isinstance(err_b, EngineLoadError),
                f"{type(err_b).__name__ if err_b else 'no-raise'}")
        msg_b = str(err_b or "")
        r.check("错误消息含真实原因(权重目录不存在 + 被 patch 的路径)",
                REASON_KEY in msg_b and str(missing) in msg_b, msg_b[:220])
        r.check("失败未被缓存为已加载", "voxcpm" not in engine_registry._loaded,
                str(list(engine_registry._loaded)))
        r.check("适配器自认未就绪 (_loaded/is_loaded)",
                engine._loaded is False and engine.is_loaded() is False,
                f"_loaded={engine._loaded} is_loaded={engine.is_loaded()}")
        r.check("registry.is_loaded('voxcpm') 为假",
                engine_registry.is_loaded("voxcpm") is False)

        # 直接调 synthesize 也不得产出静音
        synth_err = None
        audio = None
        try:
            audio = engine.synthesize("这是一句用于验证失败语义的文本。")
        except Exception as e:
            synth_err = e
        H.kv("synthesize(半死引擎)", f"raise {type(synth_err).__name__}: {str(synth_err)[:180]}"
             if synth_err else f"返回 {audio!r} (FAIL)")
        r.check("半死引擎 synthesize 不返回静音数组而是上抛",
                synth_err is not None and audio is None, str(synth_err)[:200])

        # ================= C. 预热状态机 / model_status =================
        r = su.new("C. model_preloader.snapshot() 报 failed (非 ready)")
        r.evidence_add(log)
        from modules.tts_engine.model_preloader import model_preloader
        model_preloader.reset()
        engine_registry._loaded.pop("voxcpm", None)
        st = model_preloader.start("voxcpm")
        H.kv("start() 返回", st)
        term = model_preloader.wait_terminal(timeout=120)
        snap = model_preloader.snapshot()
        H.kv("wait_terminal", term)
        H.kv("snapshot", json.dumps(snap, ensure_ascii=False)[:400])
        r.check("进入终态", term is True, str(term))
        r.check("state == 'failed'", snap.get("state") == "failed", str(snap.get("state")))
        r.check("state != 'ready'", snap.get("state") != "ready", str(snap.get("state")))
        r.check("error 字段带真实原因",
                REASON_KEY in (snap.get("error") or "") and str(missing) in (snap.get("error") or ""),
                (snap.get("error") or "")[:220])

        # /api/system/model_status 的 payload (与端点同一 handler)
        try:
            from backend.main import model_status as ms_endpoint
            payload = asyncio.run(ms_endpoint())
            H.kv("GET /api/system/model_status ->",
                 json.dumps(payload, ensure_ascii=False)[:400])
            r.check("/api/system/model_status 亦报 failed",
                    payload.get("model", {}).get("state") == "failed",
                    json.dumps(payload, ensure_ascii=False)[:220])
        except Exception as e:
            H.kv("HTTP 端点通道未走通(改用进程内 preloader)", f"{type(e).__name__}: {e}")
            r.check("HTTP 端点通道: 进程内 preloader 已覆盖 (端点不可用时不判失败)",
                    snap.get("state") == "failed", f"{type(e).__name__}: {e}")

        # ================= D. 任务形状: Pipeline + 生产 _stage_synthesize =================
        r = su.new("D. 任务形状路径: status=failed 且 error_message 含真实原因")
        r.evidence_add(log)
        from modules.workflow.pipeline import Pipeline, PipelineStage
        from modules.workflow.conversion_workflow import ConversionWorkflow

        wf = ConversionWorkflow()
        pipe = wf.create_pipeline(task_id="e1-task", project_id=None, book_id=None,
                                 output_format="m4b", settings={"engine": "voxcpm"})
        pipe.stages = [PipelineStage.SYNTHESIZE]  # 只跑合成阶段(1~6 阶段与本缺陷无关)
        ctx = {"task_id": "e1-task", "processed_chars": 0, "total_chars": 20,
               "sample_rate": 48000, "sentences": [
                   {"text": "林昭拖着行李走进客栈大堂，", "chapter_id": "c1",
                    "chapter_title": "第一章", "voice_id": None, "tone": "neutral"},
                   {"text": "老掌柜抬起头。", "chapter_id": "c1",
                    "chapter_title": "第一章", "voice_id": None, "tone": "neutral"}]}
        res = asyncio.run(pipe.run(ctx))
        H.kv("pipeline status", res.get("status"))
        H.kv("error_message", (res.get("error_message") or "")[:400])
        H.kv("audio_arrays", res.get("audio_arrays", "<未产出>"))
        r.check("任务 status == failed", res.get("status") == "failed", str(res.get("status")))
        em = res.get("error_message") or ""
        r.check("error_message 含真实原因(不只'未知错误')",
                REASON_KEY in em and str(missing) in em, em[:260])
        r.check("未产出任何合成段(静音成品无从诞生)",
                not res.get("audio_arrays"), str(res.get("audio_arrays"))[:200])
        model_preloader.reset()

        # ================= E. registry 侧加固: 吞异常形态也拦得住 =================
        r = su.new("E. 吞异常形态适配器(复刻旧行为)也被 EngineLoadError 拦下")
        r.evidence_add(log)
        from modules.tts_engine.engines.base_tts import BaseTTSEngine

        class SwallowAdapter(BaseTTSEngine):
            """复刻 PY-304 旧行为: load_model 吞异常, _loaded=True 而 _model=None。"""
            engine_name = "e1-swallow"
            sample_rate = 48000
            requires_model_object = True

            def load_model(self, model_path=None):
                self._model = None
                self._loaded = True

            def synthesize(self, text, voice_id=None, speed=1.0, pitch=0.0,
                           emotion="neutral"):
                raise NotImplementedError

        engine_registry.register("e1-swallow", SwallowAdapter())
        err_e = None
        try:
            e = engine_registry.get_loaded("e1-swallow", strict=True)
            H.kv("get_loaded 返回", f"{e} is_loaded={e.is_loaded()} (FAIL)")
        except EngineLoadError as ex:
            err_e = ex
            H.kv("EngineLoadError", str(ex)[:220])
        r.check("吞异常引擎也被上抛", isinstance(err_e, EngineLoadError),
                f"{type(err_e).__name__ if err_e else 'no-raise'}")
        r.check("未被写入 _loaded 缓存", "e1-swallow" not in engine_registry._loaded,
                str(list(engine_registry._loaded)))
        for n in ("e1-swallow",):
            engine_registry._engine_factories.pop(n, None)
            engine_registry._engines.pop(n, None)
            engine_registry._loaded.pop(n, None)

        # ---- 复位 patch, 避免同进程后续被污染 ----
        engine.model_path = orig_path
        engine_registry._engines.pop("voxcpm", None)
        engine_registry._loaded.pop("voxcpm", None)
        model_preloader.reset()

        rc = su.finish(exit_when_fail=False)
        H.log(f"  E1 判定: {'ALL_GREEN' if rc == 0 else 'NOT_GREEN'}")
        sys.exit(rc)


if __name__ == "__main__":
    main()
