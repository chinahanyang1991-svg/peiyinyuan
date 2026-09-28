"""
[智能体2 回归] round-1b PY-115 / PY-117 / PY-118 / PY-120 / PY-121 修复验证
运行: python qa/harness/agent2/verify_py115_silence_guards.py
日志: qa/round-1/logs/agent2_verify_py115_silence.log

设计说明:
  真实 VoxCPM 冷启动 220.6s + 合成 202s/句(CPU 硬件限制, PY-116)无法作为每次
  回归的成本, 本脚本用"替身引擎"驱动同一条生产代码路径
  (ConversionWorkflow._stage_synthesize -> _acquire_engine -> model_preloader ->
   engine_registry.get_loaded), 只把权重加载/音频波形换成可控替身:
    - 加载耗时/失败可注入 -> 验证 loading_model 心跳与失败上抛
    - 音频可注入(正弦波/全零/抛异常) -> 验证静音兜底已彻底移除
  真实音频端到端仍以 qa/harness/run_p7_synth.py 为准(智能体1/3 拥有)。
"""
import asyncio
import math
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # qa/harness
import harness_lib as H  # noqa: E402

import numpy as np  # noqa: E402
from modules.tts_engine.engines.base_tts import BaseTTSEngine  # noqa: E402
from modules.tts_engine.engine_registry import engine_registry, EngineLoadError  # noqa: E402
from modules.tts_engine.model_preloader import model_preloader  # noqa: E402
from modules.workflow.conversion_workflow import ConversionWorkflow  # noqa: E402
from modules.workflow.progress_tracker import ProgressTracker  # noqa: E402
from backend.services.task_service import task_service  # noqa: E402

SR = 48000


class FakeEngine(BaseTTSEngine):
    """可注入加载耗时/加载异常/音频形态的替身引擎。"""

    engine_name = "fake"
    engine_display = "替身引擎"
    sample_rate = SR

    def __init__(self, load_s=0.0, load_fail=None, mode="voice"):
        super().__init__()
        self._load_s = load_s
        self._load_fail = load_fail
        self._mode = mode
        self.n_synth = 0

    def load_model(self, model_path=None):
        if self._load_s:
            time.sleep(self._load_s)
        if self._load_fail:
            raise self._load_fail
        self._model = object()
        self._loaded = True

    def synthesize(self, text="", voice_id=None, speed=1.0, pitch=0.0, emotion="neutral"):
        self.n_synth += 1
        n = max(int(len(text) * 0.08 * SR), SR)
        if self._mode == "zeros":
            return np.zeros(n, dtype=np.float32)
        if self._mode == "boom":
            raise ValueError("模拟引擎内部崩溃")
        t = np.arange(n, dtype=np.float32) / SR
        return (0.5 * np.sin(2 * math.pi * 220 * t)).astype(np.float32)


def register(name, engine):
    engine_registry._loaded.pop(name, None)
    engine_registry._engines[name] = engine
    engine_registry._engine_factories[name] = lambda: engine
    return engine


def make_ctx(task_id, n_sentences=3):
    sents = [{"chapter_id": "c1", "chapter_title": "第一章 替身",
              "text": f"这是第 {i} 句用于验证的中文文本。", "char_count": 16,
              "tone": "neutral", "voice_id": None} for i in range(n_sentences)]
    return {"task_id": task_id, "sentences": sents, "chapters": [],
            "processed_chars": 0, "total_chars": 48, "settings": {}}


def with_tracker(cw, task_id):
    tr = ProgressTracker(task_id)
    cw._active_trackers[task_id] = tr
    return tr


def main():
    log = H.LOGS / "agent2_verify_py115_silence.log"
    with H.Tee(log):
        su = H.Suite("Agent2-verify-PY115-117-118", "agent2/verify_py115_silence_guards.py")
        r = su.new("PY-115: 冷启动后台预热 + loading_model 心跳事件")
        r.evidence_add(log)
        task = task_service.create_task(book_id=None, output_format="wav")
        tid = task["id"]
        H.kv("用于回归的临时任务行", tid)

        cw = ConversionWorkflow()
        eng = register("fake_slow", FakeEngine(load_s=6.0))
        model_preloader.reset()
        tr = with_tracker(cw, tid)
        broadcast_msgs = []
        tr.subscribe(lambda d: broadcast_msgs.append(d.get("message", "")))
        ctx = make_ctx(tid)
        t0 = time.time()
        out = asyncio.run(cw._stage_synthesize(ctx, {"engine": "fake_slow"}))
        waited = round(time.time() - t0, 1)
        stages = tr._visited_stages()
        beats = [h for h in tr.stage_history if h["stage"] == "loading_model"]
        H.kv("引擎加载耗时(替身注入 6.0s)", f"后台加载 {model_preloader.snapshot()['load_seconds']}s")
        H.kv("synthesize 等待时长", f"{waited}s")
        H.kv("阶段序列", stages)
        H.kv("loading_model 心跳次数", len(beats))
        H.kv("最后一条状态消息", tr.message)
        H.kv("产出段数", len(out["audio_arrays"]))
        r.check("合成产出真实波形(非静音)", len(out["audio_arrays"]) == 3
                and all(s["peak"] > 0.1 for s in out["audio_arrays"]),
                f"peaks={[round(s['peak'], 3) for s in out['audio_arrays']]}")
        r.check("加载期发出 loading_model 事件(>=1 次心跳)", len(beats) >= 1,
                f"beats={len(beats)}, stages={stages}")
        r.check("事件携带面向用户的'模型加载中'文案",
                any("模型加载中" in m for m in broadcast_msgs),
                f"广播消息数={len(broadcast_msgs)}, 加载期文案="
                f"{[m for m in broadcast_msgs if '模型' in m][:3]}")
        r.check("预热状态机终态为 ready", model_preloader.state == "ready",
                str(model_preloader.snapshot()))
        snap = model_preloader.snapshot()
        r.check("加载耗时被记录(PY-116 环境约束的度量口)",
                isinstance(snap.get("load_seconds"), float) and snap["load_seconds"] >= 5.5,
                f"load_seconds={snap.get('load_seconds')}")

        # ---------------- PY-118 ----------------
        r = su.new("PY-118: 模型加载失败必须上抛, 任务不得继续")
        r.evidence_add(H.PROJECT_ROOT / "modules/tts_engine/engine_registry.py")
        register("fake_broken", FakeEngine(load_fail=RuntimeError("权重文件缺失(模拟)")))
        model_preloader.reset()
        cw2 = ConversionWorkflow()
        tr2 = with_tracker(cw2, tid)
        ctx2 = make_ctx(tid, n_sentences=2)
        t0 = time.time()
        try:
            asyncio.run(cw2._stage_synthesize(ctx2, {"engine": "fake_broken"}))
            r.fail("加载失败仍被吞掉", "未抛异常 -> 任务会以静音/半死引擎继续")
        except EngineLoadError as e:
            H.kv("上抛异常", f"{type(e).__name__}: {e}")
            H.kv("耗时", f"{round(time.time() - t0, 1)}s")
            r.check("get_loaded 失败上抛 EngineLoadError", True, str(e)[:120])
            r.check("异常原因含底层真实错误", "权重文件缺失" in str(e), str(e)[:160])
        except Exception as e:
            r.fail("异常类型不符", f"{type(e).__name__}: {e}")
        # 直接单元层面确认 strict 语义
        register("fake_broken2", FakeEngine(load_fail=RuntimeError("boom")))
        model_preloader.reset()
        try:
            engine_registry.get_loaded("fake_broken2")
            r.fail("get_loaded(strict) 未抛错", "")
        except EngineLoadError as e:
            r.check("engine_registry.get_loaded 默认 strict 上抛", True, str(e)[:120])

        # ---------------- PY-117 ----------------
        r = su.new("PY-117: 合成异常/合成出静音一律失败, 不再以静音补齐")
        seg_src = Path(H.PROJECT_ROOT / "modules/workflow/conversion_workflow.py") \
            .read_text(encoding="utf-8") \
            .split("async def _stage_synthesize", 1)[1] \
            .split("def _stage_postprocess", 1)[0]
        H.kv("_stage_synthesize 内静音兜底字面量出现次数", seg_src.count("np.zeros"))
        r.check("源码中不存在静音兜底数组(与 run_p7_synth 用例 C 同判据)",
                "np.zeros" not in seg_src, f"count={seg_src.count('np.zeros')}")

        for mode, desc in (("boom", "引擎抛错"), ("zeros", "引擎返回全零")):
            name = f"fake_{mode}"
            register(name, FakeEngine(mode=mode))
            engine_registry._loaded[name] = True  # 跳过加载, 只验合成期守卫
            model_preloader.reset()
            model_preloader.start(name)
            model_preloader.wait_terminal(timeout=5)
            cw3 = ConversionWorkflow()
            with_tracker(cw3, tid)
            try:
                asyncio.run(cw3._stage_synthesize(make_ctx(tid), {"engine": name}))
                r.fail(f"{desc} 竟被当作成功", "任务会继续走向 package -> 静音成品仍可能 completed")
            except RuntimeError as e:
                H.kv(f"{desc} 的处理", str(e)[:160])
                r.check(f"{desc} -> 抛错中止任务", True, str(e)[:120])
            engine_registry._loaded.pop(name, None)

        # mock_tts 静音替身必须被拒绝
        cw4 = ConversionWorkflow()
        with_tracker(cw4, tid)
        try:
            asyncio.run(cw4._stage_synthesize(make_ctx(tid), {"mock_tts": True}))
            r.fail("mock_tts 静音替身仍可用", "会产出纯静音成品却标 completed")
        except RuntimeError as e:
            r.check("mock_tts 被显式拒绝", "PY-117" in str(e), str(e)[:140])

        # ---------------- PY-120 / PY-121 ----------------
        r = su.new("PY-120/PY-121: 9 阶段留痕 + total_chars 落库")
        r.evidence_add(H.PROJECT_ROOT / "backend/services/task_service.py")
        cw5 = ConversionWorkflow()
        tr5 = with_tracker(cw5, tid)
        ctx5 = make_ctx(tid)

        async def drive_all_stages():
            pipeline = cw5.create_pipeline(tid, None, None, "wav", {"engine": "x"})
            seen = []

            async def spy(stage, progress, message, error=None):
                seen.append(stage)
                await cw5._emit(tid, ctx5, stage, progress, message=message or "")
            for st in pipeline._handlers:
                await spy(st.value, 0.5, f"开始阶段: {st.value}")
            return seen

        seen = asyncio.run(drive_all_stages())
        row = task_service.get_task(tid)
        H.kv("pipeline 注册阶段数", len(seen))
        H.kv("DB 阶段日志", [x["stage"] for x in (row.get("logs") or [])])
        H.kv("DB total_chars / processed_chars", f"{row['total_chars']} / {row['processed_chars']}")
        H.kv("tracker stages_visited", tr5._visited_stages())
        r.check("9 个阶段全部可追溯(DB logs)",
                len([x["stage"] for x in (row.get("logs") or [])]) >= 9,
                str([x["stage"] for x in (row.get("logs") or [])]))
        r.check("total_chars 落库(此前恒为 0 -> 分母 0)", row["total_chars"] == 48,
                f"total_chars={row['total_chars']}")
        prog = cw5.get_progress(tid)
        r.check("进度响应携带 message/stages_visited",
                "stages_visited" in prog and "message" in prog, str(sorted(prog))[:200])

        # 清理临时任务行
        task_service.delete_task(tid)
        su.finish(exit_when_fail=False)


if __name__ == "__main__":
    main()
