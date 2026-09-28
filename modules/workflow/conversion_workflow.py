"""
Conversion workflow - ties the pipeline stages to real module operations.
Converts an ebook into an audiobook through 9 pipeline stages.

修复说明 (P0 #1, P0 #3, P1 #8, P1 #14):
- TTS 合成通过 asyncio.to_thread 在独立线程中执行，避免阻塞事件循环
- 音频输出通过 soundfile/M4BBuilder 正确编码，生成可播放的 WAV/MP3/FLAC/M4B
- 路径改为 Config.OUTPUT_DIR 统一管理，消除 dirname 硬编码
- duration_ms 从引擎 sample_rate 动态计算，不再写死 48000 Hz
"""

import asyncio
import logging
import math
import os
import time
import uuid
from pathlib import Path
from typing import Optional

from .pipeline import Pipeline, PipelineStage
from .progress_tracker import ProgressTracker

logger = logging.getLogger(__name__)

# PY-117/PY-123: 峰值低于此门限的段判为静音段（引擎兜底/失败的典型特征）
SILENCE_PEAK_THRESHOLD = 1e-3
# PY-128: 成品峰值上限 -1 dBFS（0.891），有损编码(mp3/m4b)需要余量，禁止二次顶格归一
TARGET_PEAK = 0.891
# PY-124: _stage_package 只认这些格式，未知格式直接报错，不再静默改写为 wav
SUPPORTED_OUTPUT_FORMATS = ("wav", "flac", "mp3", "m4b")
# PY-126: m4b 章节之间的静音，与 concat_audios 的段间隔保持一致
M4B_CHAPTER_GAP_MS = 300


# ============================================================ WF-08: 可发音判定
def has_pronounceable_content(text) -> bool:
    """片段里是否还有"能被念出来"的字符（字母/数字/CJK，即去掉标点与空白后非空）。

    背景 (WF-08): 逐字加句号的病态文本（`…的街道上。，。路。灯…`）在分句后会切出
    形如 `，。` 的零可发音片段。Edge-TTS 对它返回 NoAudioReceived，而 PY-117 的
    "真实合成故障必须中止整本书"语义把它也当成了引擎故障 —— 40 句里 1 句废掉整次
    转换。正确切分点是在合成**之前**丢弃这类片段（计入 dropped_empty_segments），
    PY-117 的中止语义只对"有文本却合成失败"保留。
    """
    if not text:
        return False
    for ch in str(text):
        if ch.isspace():
            continue
        if ch.isalnum():          # unicode 感知：CJK / 拉丁字母 / 数字都算
            return True
        # 连字符/破折号等项目符号单独出现时不可发音，继续看下一个字符
    return False


# ============================================================ US-B5: 音色分组调度
def group_gptsovits_execution_order(seq):
    """把 gptsovits 引擎负责的句子在 chapter 内按 voice_id 稳定分组后重排出执行顺序。

    服务端 ref 特征缓存是**单槽**（TTS.py:1144-1150，纯路径等值判命中），A/B/A/B
    交替会让每次换角重付 CN-HuBERT+mel+BERT 的 ~84s 冷成本（SPIKE-REPORT:182）。
    本函数让同一 (chapter_id, voice_id) 的 gsv 句子背靠背执行，把 ref 切换次数降到最低。

    seq: 文档顺序的 list，每项含 'index'(全局落账下标)/'chapter_id'/'voice_id'/'gsv'。
    返回: 输入的一个**排列**（每个 index 恰好出现一次、index 集合不变，不修改任何元素）。
    由于 _record_segment 按 index 落账、成品下游按 index 保序，重排执行不改成品顺序。
    """
    used = [False] * len(seq)
    out = []
    for pos, item in enumerate(seq):
        if used[pos]:
            continue
        used[pos] = True
        out.append(item)
        if item.get("gsv") and item.get("voice_id"):
            key = (item.get("chapter_id"), item.get("voice_id"))
            for q in range(pos + 1, len(seq)):
                it2 = seq[q]
                if (not used[q]) and it2.get("gsv") and it2.get("voice_id") \
                        and (it2.get("chapter_id"), it2.get("voice_id")) == key:
                    used[q] = True
                    out.append(it2)
    return out


def count_gsv_reference_switches(order):
    """按执行顺序统计 gsv 音色 (chapter_id, voice_id) 的装载次数（含首个）——越小越好。
    用于证明分组调度确实下降了换组次数（US-B5 验收）。"""
    switches = 0
    prev = object()
    for item in order:
        if item.get("gsv"):
            key = (item.get("chapter_id"), item.get("voice_id"))
            if key != prev:
                switches += 1
                prev = key
    return switches


def stricter_split_policy(policies):
    """把各引擎的 SplitPolicy 合并成"最严"一份（US-B2/M7：_stage_split 取实际引擎集合最严）。
    无有效 policy 时返回 DEFAULT_SPLIT_POLICY（与现状逐字等价，单引擎项目零变化）。"""
    from modules.text_processor.sentence_splitter import (
        DEFAULT_SPLIT_POLICY, SplitPolicy)
    pols = [p for p in policies if p is not None]
    if not pols:
        return DEFAULT_SPLIT_POLICY
    ubs = [p.upper_bound for p in pols if p.upper_bound and p.upper_bound > 0]
    return SplitPolicy(
        max_len=min(p.max_len for p in pols),
        min_len=max(p.min_len for p in pols),
        max_en_len=min(p.max_en_len for p in pols),
        tail_merge=any(p.tail_merge for p in pols),
        upper_bound=min(ubs) if ubs else 0,
    )


# ============================================================ 批次C: cast 接线门禁
def _cast_meta_present(cast: Optional[dict]) -> bool:
    """
    项目是否真的做过角色分析/确认（`project.extra_data.cast` 由
    cast_service 的 analyze / confirm 写入，含 analyzed_at / confirmed / aliases）。

    为什么必须有这道门禁：`dubbing_service.get_project()` 一向返回 roles，
    老项目（智能体1/2 的 single 模式回归用例、手工建角色 + assign-voice 的项目）
    只要绑过音色就会让 `cast_is_active()` 为真。若据此接管 P5/P6，
    未识别 speaker 会被清空、区间覆盖语义会漂移 —— 那不是"升级"而是回归。
    因此门禁 = "有 cast 元数据" AND "有可用角色音色"，缺一即逐字保持现状。
    """
    if not isinstance(cast, dict):
        return False
    meta = cast.get("cast")
    if not isinstance(meta, dict) or not meta:
        return False
    if not (meta.get("analyzed_at") or meta.get("confirmed")
            or meta.get("aliases") or meta.get("role_names")):
        return False
    try:
        from modules.workflow.cast_pipeline_hooks import cast_is_active
        return bool(cast_is_active(cast))
    except Exception as exc:                              # pragma: no cover
        logger.warning(f"[cast] cast_is_active 导入失败，按无 cast 处理: {exc}")
        return False


def _load_wired_cast(project_id: Optional[str],
                     project: Optional[dict] = None) -> Optional[dict]:
    """
    取"可接线"的 cast 视图：只认 `project.extra_data.cast`（分析/确认产物）。

    与 cast_pipeline_hooks.cast_for_assign 的分工：后者负责把 Role.extra_data
    补全（Role.to_dict 不带 extra_data），前者负责形状；本函数负责**门禁**，
    门禁不过一律返回 None，调用方据此退回现状实现。
    任何异常（DB 不可用 / 项目不存在）都只降级不抛错 —— 多人模式没配好
    绝不能让转换任务失败（spec 8 红线 / hooks 安全红线 1）。
    """
    if not project_id:
        return None
    try:
        if project is None:
            # P5 手上只有 project_id（_stage_analyze 不读项目），补一次读取；
            # cast_for_assign 对 project=None 会直接返回 None，所以这一步不能省。
            try:
                from backend.services.dubbing_service import dubbing_service
                project = dubbing_service.get_project(project_id)
            except Exception as exc:
                logger.warning(f"[Conversion:{project_id}] 读取项目失败，"
                               f"cast 接线跳过: {exc}")
                return None
        if not project:
            return None
        from modules.workflow.cast_pipeline_hooks import cast_for_assign
        cast = cast_for_assign(project, project_id)
    except Exception as exc:                              # pragma: no cover
        logger.warning(f"[cast] cast_for_assign({project_id}) 失败，按无 cast 处理: {exc}")
        return None
    return cast if _cast_meta_present(cast) else None


def _overlay_segment_assignments(sentences: list, seg_assignments: list) -> int:
    """
    PY-114: VoiceAssignment（章节 + 字符区间）覆盖，优先级高于名表/cast 名表。

    批次C 把它从 `_stage_assign` 的循环体里提出来，只因为 multi 路径现在有两条
    （cast 名表 / 老名表），区间覆盖这一层语义必须对二者一致。
    逐章累计字符游标的算法与原内联版本逐字等价：
      - 游标按 `char_count`（缺失则 len(text)）累加，跨章各自归零；
      - `start==0 and end==0` 视为"整章覆盖"；
      - 命中即 break（一句只吃第一条区间）。
    Returns: 命中区间覆盖的句数。
    """
    chapter_cursor: dict = {}
    hits = 0
    for sent in sentences:
        if not isinstance(sent, dict):
            continue
        cid = sent.get("chapter_id")
        offset = chapter_cursor.get(cid, 0)
        chapter_cursor[cid] = offset + int(sent.get("char_count")
                                           or len(sent.get("text", "") or ""))
        for a in seg_assignments:
            if a["chapter_id"] and a["chapter_id"] != cid:
                continue
            if a["start"] <= offset < a["end"] or (a["start"] == 0 and a["end"] == 0):
                sent["voice_id"] = a["voice_id"]
                if a["role_name"]:
                    sent["role_name"] = a["role_name"]
                for k in ("speed", "pitch", "emotion"):
                    if k in a["settings"]:
                        sent[k] = a["settings"][k]
                hits += 1
                break
    return hits


def _project_mix_settings(project_id: Optional[str],
                          task_id: Optional[str] = None) -> dict:
    """
    批次C (P8) 用：取项目 `global_settings["mix"]`（POST /cast/mix-settings 写入）。

    返回 {} 表示"未配置混音"—— 调用方据此**完全跳过** mix 子步骤，
    P8 输出与接线前逐样本一致（single 模式与老项目的回归保护）。
    读不到项目/DB 异常也只降级为直通并告警，不让转换任务因混音配置不可用而失败。
    """
    if not project_id:
        return {}
    try:
        from backend.services.dubbing_service import dubbing_service
        project = dubbing_service.get_project(project_id)
    except Exception as exc:                              # pragma: no cover - 防御
        logger.warning(f"[Conversion:{task_id}] 读取项目 {project_id} 混音配置失败，"
                       f"P8 直通: {exc}")
        return {}
    gs = (project or {}).get("global_settings") or {}
    if not isinstance(gs, dict):
        return {}
    mix = gs.get("mix")
    return dict(mix) if isinstance(mix, dict) and mix else {}


def task_service_stale_threshold() -> float:
    """F-R4-2: 心跳阈值秒数（口径唯一来源 = backend.services.task_service）。"""
    try:
        from backend.services.task_service import progress_stale_seconds
        return progress_stale_seconds()
    except Exception:
        return 1800.0


class ConversionWorkflow:
    """End-to-end ebook to audiobook conversion workflow."""

    def __init__(self):
        self._active_trackers: dict[str, ProgressTracker] = {}
        self._active_pipelines: dict[str, Pipeline] = {}
        # WF-07: 取消意图独立成册。旧实现只有 pipeline 在册时 cancel 才生效，
        # 于是"POST /convert 刚返回（pipeline 尚未注册）就取消"直接丢失意图。
        self._cancelled_tasks: set[str] = set()

    # ---- WF-07: 取消意图（供 P7 句循环内逐句检查） ----
    def request_cancel(self, task_id: str) -> None:
        """记录取消意图，并在 pipeline 已注册时同步置位其 Event。幂等。"""
        if not task_id:
            return
        self._cancelled_tasks.add(task_id)
        pipeline = self._active_pipelines.get(task_id)
        if pipeline is not None and not pipeline.is_cancelled():
            pipeline.cancel()

    def is_cancelled(self, task_id: str) -> bool:
        """该任务是否被要求取消（取消意图集合 或 pipeline Event 任一为真）。"""
        if task_id in self._cancelled_tasks:
            return True
        pipeline = self._active_pipelines.get(task_id)
        return bool(pipeline is not None and pipeline.is_cancelled())

    def clear_cancelled(self, task_id: str) -> None:
        """任务收尾后清掉取消意图，避免同名 task_id 复用时被误判。"""
        self._cancelled_tasks.discard(task_id)

    # ---- PY-311: 本进程活跃任务集合（启动对账的白名单） ----
    def active_task_ids(self) -> set:
        """本进程当前真正在跑的任务 id（tracker 或 pipeline 任一在册）。

        后端刚起时必为空集 —— 于是"表里所有非终态行"都是上一个进程遗留的僵尸。
        """
        return set(self._active_trackers.keys()) | set(self._active_pipelines.keys())

    def is_active(self, task_id: str) -> bool:
        """某任务是否由本进程在跑（F-R4-2: 在跑的行永不判 stale）。"""
        return (task_id in self._active_trackers) or (task_id in self._active_pipelines)

    # ---- 进度上报（PY-120/PY-121 统一入口） ----
    async def _emit(self, task_id: str, context: dict, stage: str,
                    progress: float, message: str = "", error: str = "",
                    status: str = "") -> None:
        """一次上报同时驱动 WS(tracker) 与 DB：带 total_chars，阶段可追溯。

        PY-121: 旧代码调用 task_service.update_progress 从不传 total_chars，
        导致任务完成瞬间 DB 回落为 processed=60/total=0（分母 0，前端百分比不可算）。
        PY-120: stage 逐阶段写入 tracker.stage_history 与 DB 的 logs 列，
        前端一次轮询即可画出 9 段进度（瞬时阶段不再被 2s 轮询漏掉）。
        """
        from backend.services.task_service import task_service

        processed = int(context.get("processed_chars", 0) or 0)
        total_chars = int(context.get("total_chars", 0) or 0)
        if stage:
            # WF-07: 收尾留痕需要"最后停在哪个阶段"，tracker 被 pop 后只能从这里取
            context["current_stage"] = stage

        tracker = self._active_trackers.get(task_id)
        if tracker:
            # 首次上报把 queued 推进为 running（WS/前端据此显示"转换中"）
            st = status or ("running" if tracker.status == "queued" else "")
            try:
                await tracker.update(progress, stage, processed, total_chars,
                                     status=st, error=error, message=message)
            except Exception as e:
                logger.warning(f"[Conversion:{task_id}] tracker update failed: {e}")
        try:
            task_service.update_progress(task_id, progress, stage, processed,
                                         total_chars=total_chars, message=message,
                                         error=error)
        except Exception as e:
            logger.warning(f"[Conversion:{task_id}] DB update failed: {e}")

    def create_pipeline(self, task_id: str, project_id: str,
                        book_id: str, output_format: str = "m4b",
                        settings: dict = None) -> Pipeline:
        """Create a conversion pipeline for a task."""
        settings = settings or {}

        pipeline = Pipeline()

        # Register stage handlers (lambda 包裹的同步/异步 handler 由 Pipeline.run 统一调度)
        pipeline.register_handler(PipelineStage.IMPORT,
            lambda ctx: self._stage_import(ctx, book_id))
        pipeline.register_handler(PipelineStage.PARSE,
            self._stage_parse)
        pipeline.register_handler(PipelineStage.CLEAN,
            self._stage_clean)
        pipeline.register_handler(PipelineStage.SPLIT,
            self._stage_split)
        pipeline.register_handler(PipelineStage.ANALYZE,
            self._stage_analyze)
        pipeline.register_handler(PipelineStage.ASSIGN,
            lambda ctx: self._stage_assign(ctx, project_id))
        pipeline.register_handler(PipelineStage.SYNTHESIZE,
            lambda ctx: self._stage_synthesize(ctx, settings))
        pipeline.register_handler(PipelineStage.POSTPROCESS,
            self._stage_postprocess)
        pipeline.register_handler(PipelineStage.PACKAGE,
            lambda ctx: self._stage_package(ctx, output_format))

        return pipeline

    async def run_conversion(self, task_id: str, project_id: str,
                             book_id: str, output_format: str = "m4b",
                             settings: dict = None) -> dict:
        """Run a full conversion workflow."""
        from backend.services.task_service import task_service

        # Create progress tracker
        tracker = ProgressTracker(task_id)
        self._active_trackers[task_id] = tracker

        # Build initial context
        context = {
            "task_id": task_id,
            "project_id": project_id,
            "book_id": book_id,
            "output_format": output_format,
            "settings": settings or {},
            "status": "running",
            "processed_chars": 0,
            "total_chars": 0,
            "chapters": [],
            "sentences": [],
            "assignments": [],
            "audio_segments": [],
            "audio_arrays": [],   # np.ndarray 形式，避免 bytes↔ndarray 来回转换
            "output_path": "",
        }

        try:
            # Load book for total chars
            from backend.services.book_service import book_service
            book = book_service.get_book(book_id)
            if not book:
                raise ValueError(
                    f"书籍 '{book_id}' 不存在。请确保书籍已正确导入后再开始转换。"
                )
            context["total_chars"] = book.get("total_chars", 0)

            chapters = book_service.get_chapters(book_id)
            context["chapters"] = chapters

            if not chapters:
                logger.warning(
                    f"[Conversion:{task_id}] Book has no chapters - "
                    f"this may cause the conversion to produce no audio."
                )

            logger.info(
                f"[Conversion:{task_id}] Starting: book='{book.get('title')}', "
                f"chapters={len(chapters)}, total_chars={context['total_chars']}, "
                f"format={output_format}"
            )

            # PY-115: 任务一开始就把 220s 量级的模型冷启动丢到后台线程预热，
            # 与 import~assign(1~6 阶段)并行；synthesize 阶段只需等待终态并每 5s
            # 发一次 "模型加载中" 心跳，首句不再串行付冷启动。
            # PY-306: 原来的 `if not settings.get("mock_tts")` 是死分支——mock_tts
            # 在 P7 必 RuntimeError(PY-117)，守卫只会让"注定失败的任务"跳过预热，
            # 对可达路径毫无意义 -> 无条件预热。
            # 指令#1: "无条件"保留，但**预热的是这单真会用到的引擎**。原来这里写死
            # voxcpm，在线档的任务因此白起一条 220s 的本地装载（还要占串行加载锁）。
            try:
                from modules.tts_engine.model_preloader import model_preloader
                from modules.tts_engine import tier as tts_tier
                task_tier = tts_tier.resolve_tier((settings or {}).get("tier"))
                warm_engine = ((settings or {}).get("engine")
                               or tts_tier.default_engine(task_tier))
                if tts_tier.is_online_engine(warm_engine):
                    logger.info(
                        f"[Conversion:{task_id}] 档位={task_tier} 引擎={warm_engine}"
                        f" - 在线档无权重可预热，跳过后台装载")
                else:
                    model_preloader.start(warm_engine)
            except Exception as e:
                logger.warning(f"[Conversion:{task_id}] 预热启动失败(任务内会重试): {e}")

            await self._emit(task_id, context, "import", 0.0,
                             message="书籍已导入，开始解析")

        except Exception as e:
            logger.exception(f"[Conversion:{task_id}] Failed to load book: {e}")
            context["status"] = "failed"
            context["error_message"] = str(e)
            try:
                await tracker.update(0, "import", 0, context.get("total_chars", 0),
                                     status="failed", error=str(e))
                task_service.fail_task(task_id, str(e))
            except Exception as e2:
                logger.warning(f"[Conversion:{task_id}] Failed to report error: {e2}")
            self._active_trackers.pop(task_id, None)
            self.clear_cancelled(task_id)
            return context

        # Create and run pipeline
        pipeline = self.create_pipeline(
            task_id, project_id, book_id, output_format, settings
        )

        # WF-07: 取消可能早于 pipeline 注册（POST /convert 刚返回就 POST /cancel）。
        # 这里在真正开跑前再查一次取消意图，直接落 cancelled 终态，不再白烧引擎。
        if self.is_cancelled(task_id):
            logger.info(f"[Conversion:{task_id}] 开跑前已收到取消请求，直接落 cancelled")
            try:
                await tracker.update(0, "import", 0, context.get("total_chars", 0),
                                     status="cancelled")
            except Exception as e:                          # pragma: no cover
                logger.warning(f"[Conversion:{task_id}] tracker 取消上报失败: {e}")
            self._active_trackers.pop(task_id, None)
            self.clear_cancelled(task_id)
            try:
                task_service.finalize_cancelled(task_id, "开跑前已收到取消请求")
            except Exception as e:                          # pragma: no cover
                logger.warning(f"[Conversion:{task_id}] 取消终态落库失败: {e}")
            return {
                "status": "cancelled",
                "output_path": "",
                "error_message": "",
                "format_used": "",
                "requested_format": output_format,
                "task_id": task_id,
                "project_id": project_id,
                "book_id": book_id,
            }

        # Wire progress callback to update both tracker and task service
        # PY-120: pipeline 对 9 个阶段各发"开始/完成"两次通知，统一走 _emit，
        # 保证 current_stage 覆盖全部 PipelineStage 且逐段留痕（DB logs）。
        async def on_progress(stage, progress, message, error=None):
            if message:
                context.setdefault("_stage_messages", {})[stage] = message
            await self._emit(task_id, context, stage, progress,
                             message=message or "", error=error or "")

        pipeline.on_progress(on_progress)

        # Note: We do NOT call scheduler.submit() here — it would:
        # 1) Overwrite the on_progress callback (set above) with a sync lambda that never awaits tracker.update()
        # 2) Put the task on a queue whose worker loop is never started (scheduler.start() is never called)
        # 3) The task would silently sit in the queue forever
        # Instead we run the pipeline directly via asyncio.create_task(run_task()) in the HTTP handler,
        # which gives us proper non-blocking execution and real-time progress updates.

        # Run pipeline directly (单进程内同步/异步混合执行)
        self._active_pipelines[task_id] = pipeline
        try:
            result = await pipeline.run(context)
        finally:
            self._active_pipelines.pop(task_id, None)

        # 先检查状态再清理（修复: context.clear() 会清空 status 字段，
        # 导致 result 永远为 unknown）
        status = result.get("status", "unknown")
        output_path = result.get("output_path", "")
        error_message = result.get("error_message", "")
        # PY-124: 打包阶段回传的真实格式必须在 context.clear() 之前取走
        format_used = result.get("format_used", "") or ""
        requested_format = result.get("requested_format", "") or output_format
        # WF-07: 同上，取消留痕要用的阶段名也必须在 clear 之前取
        current_stage = result.get("current_stage", "") or ""

        # Cleanup - 释放内存中的大型 numpy 数组
        if "audio_arrays" in context:
            context["audio_arrays"].clear()
        if "sentences" in context:
            context["sentences"].clear()
        if "chapters" in context:
            context["chapters"].clear()
        context.clear()

        self._active_trackers.pop(task_id, None)
        self._active_pipelines.pop(task_id, None)

        # 强制垃圾回收，释放 numpy/torch 内存
        import gc
        gc.collect()

        # WF-07: 收尾状态优先级 —— 取消意图一旦置位，取消就压过 failed。
        # Pipeline.run 的 except 分支会把"取消在途时手上最后一次的失败"（例如实测
        # 里第 10/40 句 NoAudioReceived）标成 status=failed，旧收尾据此调 fail_task，
        # 于是用户看到的终态从 cancelled 被改写成 failed。这里在写库前统一口径，
        # 使返回值（HTTP/日志）与 DB 行始终一致。
        cancel_requested = self.is_cancelled(task_id)
        if status == "failed" and cancel_requested:
            logger.info(f"[Conversion:{task_id}] 取消在途时收到阶段失败"
                        f"(stage={current_stage or '-'}, err={error_message[:120]})，"
                        f"按 cancelled 收尾 (WF-07)")
            status = "cancelled"

        if status == "completed":
            task_service.complete_task(task_id, output_path,
                                       format_used=format_used,
                                       requested_format=requested_format)
            if format_used and requested_format and format_used != requested_format:
                logger.warning(f"[Conversion:{task_id}] 实际产物格式 {format_used} "
                               f"与请求 {requested_format} 不一致，已回写任务表 (PY-124)")
        elif status == "cancelled":
            # WF-07: 取消终态由收尾器自己落稳（幂等），不再只依赖路由那一次写。
            reason = f"任务已在 {current_stage or '执行中'} 阶段被取消"
            if error_message:
                reason += f"（取消时手上的失败不再改写终态: {error_message[:120]}）"
            try:
                task_service.finalize_cancelled(task_id, reason)
            except Exception as e:                          # pragma: no cover
                logger.warning(f"[Conversion:{task_id}] 取消终态落库失败: {e}")
        elif status == "failed":
            # fail_task 内部保证：已是 cancelled 的行不再被覆写为 failed (WF-07)
            task_service.fail_task(task_id, error_message or "Unknown error")
        self.clear_cancelled(task_id)

        logger.info(
            f"[Conversion:{task_id}] finished with status={status}, "
            f"output={output_path or 'N/A'}, format_used={format_used or 'N/A'}"
        )
        # 返回新 dict（result 是 context 的引用，clear() 后已为空）
        return {
            "status": status,
            "output_path": output_path,
            "error_message": error_message,
            "format_used": format_used,
            "requested_format": requested_format,
            "task_id": task_id,
            "project_id": project_id,
            "book_id": book_id,
        }

    def get_progress(self, task_id: str) -> Optional[dict]:
        """Get current progress for a task.

        F-R4-2: 内存 tracker 有值 = 本进程正在跑 ⇒ stale=False；
        回落到 DB 读的行必须带心跳信息（stale/age_seconds/stale_after_seconds），
        否则僵尸行会被永久读成 "progress=0.44 status=running"（verify-round4 §9 F-R4-2）。
        """
        tracker = self._active_trackers.get(task_id)
        if tracker:
            payload = dict(tracker.to_dict())
            payload["stale"] = False
            payload["stale_after_seconds"] = task_service_stale_threshold()
            return payload
        # Fallback to database
        from backend.services.task_service import task_service
        try:
            task = task_service.get_task(task_id)
        except Exception as e:
            logger.warning(f"[Conversion:{task_id}] get_task failed: {e}")
            return None
        if task:
            payload = {
                "task_id": task_id,
                "progress": task.get("progress", 0),
                "current_stage": task.get("current_stage", ""),
                "processed_chars": task.get("processed_chars", 0),
                "total_chars": task.get("total_chars", 0),
                "status": task.get("status", ""),
                "error_message": task.get("error_message", ""),
                "updated_at": task.get("updated_at"),
            }
            # 本进程在跑的行永不判 stale（P7 长句合成可能数十分钟才回写一次 DB）
            if self.is_active(task_id):
                payload.update({"stale": False,
                                "stale_after_seconds": task_service_stale_threshold()})
            else:
                payload.update(task_service.progress_staleness(task))
            return payload
        return None

    def cancel(self, task_id: str) -> bool:
        """Cancel a running conversion using the active pipeline.

        WF-07: 无论 pipeline 是否已注册，先把取消意图记进集合 —— P7 的句循环
        每句检查该集合并提前收束；返回值保持旧语义（是否命中在册 pipeline）。
        """
        self.request_cancel(task_id)
        pipeline = self._active_pipelines.get(task_id)
        if pipeline:
            pipeline.cancel()
            return True
        return False

    def _append_task_log(self, task_id: str, stage: str, message: str = "",
                         extra: Optional[dict] = None, error: str = "") -> None:
        """WF-13: 把运行期事实写进 ConversionTask.logs（GET /tasks/{id} 可见）。

        只加日志不改状态；DB 不可用时降级为服务端 logger，绝不让转换任务因
        "可观测性写入失败"而失败。
        """
        if not task_id:
            return
        try:
            from backend.services.task_service import task_service
            if error:
                task_service.append_log(task_id, stage, message=message,
                                        extra=extra, error=error)
            else:
                task_service.append_log(task_id, stage, message=message,
                                        extra=extra)
        except Exception as e:                              # pragma: no cover
            logger.warning(f"[Conversion:{task_id}] append_log({stage}) 失败: {e}")

    def pause(self, task_id: str) -> bool:
        """Pause a running conversion using the active pipeline."""
        pipeline = self._active_pipelines.get(task_id)
        if pipeline:
            pipeline.pause()
            return True
        return False

    def resume(self, task_id: str) -> bool:
        """Resume a paused conversion using the active pipeline."""
        pipeline = self._active_pipelines.get(task_id)
        if pipeline:
            pipeline.resume()
            return True
        return False

    # ---- Stage Handlers ----

    def _stage_import(self, context: dict, book_id: str) -> dict:
        """Stage 1: Import - verify book exists and is ready."""
        from backend.services.book_service import book_service
        book = book_service.get_book(book_id)
        if not book:
            raise ValueError(f"Book {book_id} not found")

        context["book_title"] = book.get("title", "") or "未命名书籍"
        context["book_author"] = book.get("author", "") or ""
        context["book_language"] = book.get("language", "zh") or "zh"

        logger.info(f"[Conversion:{context.get('task_id')}] Import stage: "
                    f"book '{context['book_title']}' ready, "
                    f"{len(context.get('chapters', []))} chapters")
        return {"status": "running"}

    def _stage_parse(self, context: dict) -> dict:
        """
        Stage 2: Parse - 章节校验 + 缓存重建 (PY-104 裁定留痕, 智能体2 2026-09-20)

        裁定(二选一之"保留导入期解析为真实路径"):
          /books/import 已在入库时完成 ParserFactory.parse + 分章 + 清洗,
          chapters 表即真实解析产物。P2 不重复全量解析——大书(数十万字起)二次
          解析会成倍拉长任务总耗时, 且前端进度语义失真(P2 被当成瞬时阶段)。
        本阶段职责因此定义为:
          1) 校验: 章节内容非空、char_count 与实际一致; 若上下文只带截断
             preview 或计数漂移, 按章节 id 从 book_service 回填/修正(轻量修复缓存);
          2) 重建: 仅当可用章节数为 0 (chapters 缓存缺失/被清空), 才按
             book.file_path 用 ParserFactory 从导入期原文件重建内存章节缓存
             (自愈兜底, 不回写 DB, 单一事实源仍在导入路径)。
        与前端进度语义一致: happy path 为 O(章节数) 的毫秒级校验。
        """
        from backend.services.book_service import book_service

        chapters = context.get("chapters", []) or []
        input_chapters = len(chapters)
        repaired = 0
        usable = 0
        bad = 0
        for ch in chapters:
            content = ch.get("content")
            if not isinstance(content, str) or not content.strip():
                full = None
                cid = ch.get("id")
                if cid:
                    try:
                        full = book_service.get_chapter_content(cid)
                    except Exception:
                        full = None
                if full:
                    ch["content"] = full
                    ch["char_count"] = len(full)
                    repaired += 1
                    usable += 1
                    continue
                bad += 1
                continue
            cc = ch.get("char_count")
            if isinstance(cc, int) and cc != len(content):
                ch["char_count"] = len(content)
                repaired += 1
            usable += 1

        if usable == 0:
            # 兜底: 从导入期保存的原始文件重建章节缓存
            book_id = context.get("book_id")
            book = book_service.get_book(book_id) if book_id else None
            file_path = (book or {}).get("file_path") or ""
            if file_path and Path(file_path).exists():
                from modules.book_parser.parser_factory import ParserFactory
                logger.warning(
                    f"[Conversion:{context.get('task_id')}] Parse stage: 章节缓存不可用, "
                    f"按 {Path(file_path).name} 重建 (PY-104 兜底路径)")
                book_data = ParserFactory.parse(file_path)
                chapters = [
                    {"id": None, "book_id": book_id, "title": ch.get("title", ""),
                     "index": i, "content": ch["content"],
                     "char_count": ch["char_count"], "content_preview": "",
                     "extra_data": {}}
                    for i, ch in enumerate(book_data.chapters)
                ]
                context["chapters"] = chapters
                usable = len(chapters)

        # U-4/M-5: 零产出守卫 —— 对齐 PY-117 语义。空章节书(或重建后仍 0 章)过去
        # 会绿着穿过 P2-P6, 直到 P7 合成 0 句才失败, 前端看到的是"跑到一半没动静"。
        if usable == 0:
            raise RuntimeError(
                f"[P2 Parse] 可用章节 0 章 (输入 {input_chapters} 章, 坏章 {bad} 章, "
                f"PY-104 文件兜底亦未取到内容) —— 期望最小值 1 章, 无正文不得继续")

        if bad:
            # 丢弃无内容且无法回填的坏章, 不让空块流入后续阶段
            context["chapters"] = [c for c in context.get("chapters", [])
                                   if (c.get("content") or "").strip()]

        logger.info(
            f"[Conversion:{context.get('task_id')}] Parse stage: "
            f"校验通过 {usable} 章"
            + (f", 修复缓存 {repaired} 章" if repaired else "")
            + (f", 剔除坏章 {bad} 章" if bad else ""))
        return {"chapters": context.get("chapters", chapters), "status": "running"}

    def _stage_clean(self, context: dict) -> dict:
        """Stage 3: Clean - clean chapter text content."""
        from modules.book_parser.text_cleaner import TextCleaner

        cleaner = TextCleaner()
        cleaned_count = 0
        total_chars = 0
        for ch in context.get("chapters", []):
            if ch.get("content"):
                original = ch["content"]
                cleaned = cleaner.clean(original)
                ch["content"] = cleaned
                ch["char_count"] = len(cleaned)
                cleaned_count += 1
                total_chars += ch["char_count"]

        if total_chars == 0:
            raise RuntimeError(
                f"[P3 Clean] 清洗后正文 0 字符 (章节 {len(context.get('chapters', []))} 章, "
                f"清洗 {cleaned_count} 章) —— 期望最小值 1 字符, 空文本不往下派")

        context["total_chars"] = total_chars
        context["processed_chars"] = 0

        logger.info(f"[Conversion:{context.get('task_id')}] Clean stage: "
                    f"{cleaned_count} chapters, {total_chars} chars")
        return {"total_chars": total_chars, "processed_chars": 0}

    def _resolve_split_policy(self, settings: dict):
        """US-B2/M7: 由 split 阶段可见的引擎集合推导最严 SplitPolicy。

        split(P4) 在 assign(P6) 之前，句级引擎路由尚未定，这里能拿到的引擎集合 =
        项目主引擎 settings['engine']（+ 可选 settings['engines'] 列表）。
        读各适配器暴露的 split_policy 类属性取最严一份；取不到即 DEFAULT，
        保证单引擎/默认项目分句结果与改造前逐字一致。任何异常都降级到 DEFAULT。
        """
        from modules.text_processor.sentence_splitter import DEFAULT_SPLIT_POLICY
        names: list = []
        try:
            primary = (settings or {}).get("engine")
            if primary and primary not in names:
                names.append(primary)
            extra = (settings or {}).get("engines")
            if isinstance(extra, (list, tuple)):
                for e in extra:
                    if e and e not in names:
                        names.append(e)
            if not names:
                # 指令#1：P4 分句必须与 P7 实际路由的引擎同源。settings 里没有 engine
                # 时（前端两条发起路径都是空的），真值 = 当前档位的默认引擎；
                # 该引擎若没声明 split_policy，`stricter_split_policy([None])` 仍回 DEFAULT，
                # 所以这一改动只对"声明过更严策略的引擎"生效，不会凭空改写既有分句结果。
                from modules.tts_engine import tier as tts_tier
                names.append(tts_tier.default_engine(
                    tts_tier.resolve_tier((settings or {}).get("tier"))))
            from modules.tts_engine.engine_registry import engine_registry
            policies = []
            for n in names:
                inst = None
                try:
                    inst = engine_registry.get(n)
                except Exception:
                    inst = None
                policies.append(getattr(inst, "split_policy", None)
                                if inst is not None else None)
            return stricter_split_policy(policies)
        except Exception as e:                                # pragma: no cover
            logger.warning(f"[Conversion] _resolve_split_policy 降级 DEFAULT: {e}")
            return DEFAULT_SPLIT_POLICY

    def _stage_split(self, context: dict) -> dict:
        """Stage 4: Split - split chapter text into sentences."""
        from modules.text_processor.sentence_splitter import SentenceSplitter

        task_id = context.get("task_id")
        settings = context.get("settings", {})
        max_sentences = settings.get("max_sentences", 0)

        # US-B2/M7: 分句长度策略由实际引擎集合注入（取最严一份）；默认=现状，零变化
        policy = self._resolve_split_policy(settings)
        context["split_policy"] = {
            "max_len": policy.max_len, "min_len": policy.min_len,
            "max_en_len": policy.max_en_len, "tail_merge": policy.tail_merge,
            "upper_bound": policy.upper_bound,
        }
        splitter = SentenceSplitter(policy=policy)
        all_sentences = []
        dropped_empty = 0                 # WF-08: 无可发音内容的片段计数
        raw_segment_count = 0
        language = context.get("book_language", "zh")
        for ch in context.get("chapters", []):
            # 优先使用完整 content；部分旧实现/外部接口可能只返回 content_preview
            # 如果 content 是空或与 content_preview 一致（被截断），跳过
            content = ch.get("content") or ch.get("content_preview") or ""
            if not isinstance(content, str):
                content = str(content) if content is not None else ""
            if content:
                # 检测 content 是否是截断的预览（结尾是 "..." 而 content_preview 一致）
                preview = ch.get("content_preview", "")
                if preview and content == preview and content.endswith("..."):
                    # content 是截断预览，尝试从 chapter_id 加载完整内容
                    try:
                        from backend.services.book_service import book_service
                        cid = ch.get("id")
                        if cid:
                            full = book_service.get_chapter_content(cid)
                            if full:
                                content = full
                    except Exception:
                        pass
                sentences = splitter.split(content, language)
                for sent in sentences:
                    if max_sentences > 0 and len(all_sentences) >= max_sentences:
                        break
                    raw_segment_count += 1
                    # WF-08: 纯标点/纯空白片段（'，。'、'——'）没有任何可发音字符。
                    # 它们进合成必然触发引擎 NoAudioReceived，而 PY-117 要求"真实
                    # 合成故障必须中止整本书"——于是标点把整本书带崩。标点本身不是
                    # 故障，故在 split 阶段丢弃并计数（PY-117 语义原样保留）。
                    if not has_pronounceable_content(sent):
                        dropped_empty += 1
                        continue
                    all_sentences.append({
                        "chapter_id": ch.get("id"),
                        "chapter_title": ch.get("title", ""),
                        "text": sent,
                        "char_count": len(sent),
                    })
                if max_sentences > 0 and len(all_sentences) >= max_sentences:
                    break

        if not all_sentences:
            raise RuntimeError(
                f"[P4 Split] 分句产出 0 句 (chapters {len(context.get('chapters', []))} 章, "
                f"切出 {raw_segment_count} 段, 丢弃纯标点/空白 {dropped_empty} 段) —— "
                f"期望最小值 1 句, 无可发音文本即失败而非静默出空轨")

        context["sentences"] = all_sentences
        split_stats = {
            "chapters": len(context.get("chapters", [])),
            "raw_segments": raw_segment_count,
            "sentences": len(all_sentences),
            "dropped_empty_segments": dropped_empty,
            "max_sentences": max_sentences,
        }
        context["split_stats"] = split_stats
        logger.info(f"[Conversion:{task_id}] Split stage: {len(all_sentences)} sentences "
                    f"(from {len(context.get('chapters', []))} chapters, "
                    f"dropped {dropped_empty} punctuation-only segments: WF-08)")
        if dropped_empty:
            self._append_task_log(
                task_id, "split",
                message=f"分句完成: {len(all_sentences)} 句，丢弃无发音内容片段 "
                        f"{dropped_empty} 个（纯标点/空白，WF-08）",
                extra={"split_stats": split_stats})
        return {"sentences": all_sentences,
                "dropped_empty_segments": dropped_empty,
                "split_stats": split_stats}

    def _stage_analyze(self, context: dict) -> dict:
        """Stage 5: Analyze - detect dialogue, tone, and speaker roles.

        批次C 接线 (P5): DialogueDetector 的 role_hint 只做局部线索，
        项目若做过角色分析/确认（project.extra_data.cast 存在），
        在统计之前用 cast 别名表把 speaker 归一到规范角色名，P6 才有稳定 key。
        无 cast 数据（老项目 / single 模式 / 未分析）-> 本阶段行为逐字不变。
        """
        from modules.text_processor.dialogue_detector import DialogueDetector
        from modules.text_processor.tone_detector import ToneDetector

        # PY-338: 注入 cast 的判名门，恢复引号后「动作主语」这一档归因
        from modules.cast_analyzer.cast_extractor import looks_like_name
        dialogue_detector = DialogueDetector(name_gate=looks_like_name)
        tone_detector = ToneDetector()

        sentences = context.get("sentences", [])
        if not sentences:
            raise RuntimeError(
                f"[P5 Analyze] 输入 0 句 (total_chars="
                f"{context.get('total_chars')}) —— 期望最小值 1 句, 空清单不产出空标注")
        for sent in sentences:
            text = sent.get("text", "")
            if text:
                dialogue_info = dialogue_detector.detect_dialogues(text)
                tone_info = tone_detector.detect(text)
                has_dialogue = bool(dialogue_info) and any(
                    isinstance(d, dict) and d.get("type") == "dialogue"
                    for d in dialogue_info
                )
                speaker = ""
                if has_dialogue:
                    for d in dialogue_info:
                        if isinstance(d, dict) and d.get("type") == "dialogue" and d.get("role_hint"):
                            speaker = d["role_hint"]
                            break
                sent["is_dialogue"] = has_dialogue
                sent["speaker"] = speaker
                if isinstance(tone_info, dict):
                    sent["tone"] = tone_info.get("primary", "neutral")
                    sent["emotion"] = tone_info.get("confidence", 0.5)
                else:
                    sent["tone"] = "neutral"
                    sent["emotion"] = 0.5

        # ---- 批次C (P5): cast 别名表归一 speaker（无 cast 数据时逐字跳过） ----
        cast = _load_wired_cast(context.get("project_id"))
        if cast:
            from modules.workflow.cast_pipeline_hooks import normalize_speakers
            stats = normalize_speakers(sentences, cast)
            context["cast_speaker_stats"] = stats
            logger.info(f"[Conversion:{context.get('task_id')}] Analyze stage: "
                        f"cast speaker 归一 {stats}")

        dialogue_count = sum(1 for s in sentences if s.get("is_dialogue"))
        logger.info(f"[Conversion:{context.get('task_id')}] Analyze stage: "
                    f"{len(sentences)} sentences, {dialogue_count} dialogues")
        return {"sentences": sentences}

    def _stage_assign(self, context: dict, project_id: str) -> dict:
        """Stage 6: Assign - assign voices to sentences based on project config.

        修复 (P1 #多语言旁白): name_voice_map 的回退逻辑支持 narrator/旁白/默认 narrator role
        PY-114 修复: 读取项目 DB 的 voice_assignments 分段分配（章节+字符区间），
        区间覆盖优先于 speaker 名表映射；分配携带的 settings(speed/pitch/emotion)
        以句级字段输出，供 P7 合成消费。
        PY-113 修复: speaker 归一化(去空白/引号残留/小写)后再查名表，查不到归旁白。
        批次C 接线 (P6): multi 且项目带 project.extra_data.cast（做过角色分析/确认）
        时，名表这一层改走 cast_pipeline_hooks.assign_with_cast —— 先过别名表归一
        （"老周"->"周建国"），再把角色级 speed/pitch/gain_db 写成句级字段供 P7/P8 消费。
        无 cast 数据（老项目 / 未分析 / single 模式）-> 上面 PY-113/PY-114 的行为
        逐字不变；区间覆盖 (PY-114) 对两条 multi 路径完全一致。
        """
        from backend.services.dubbing_service import dubbing_service

        # U-4/M-5: 零产出守卫。放在项目查询之前，无项目/有项目两条返回路径共用同一道门。
        # 注意 "有句但无一分配音色" **不**算零产出 —— P7 的 default_needed 分支会合法
        # 落回项目主引擎默认音色，拦它会打断一条能出真音频的出厂路径。
        if not context.get("sentences"):
            raise RuntimeError(
                f"[P6 Assign] 输入 0 句 (project={project_id}) —— 期望最小值 1 句, "
                f"空清单不得产出空分配")

        project = dubbing_service.get_project(project_id) if project_id else None
        if not project:
            # 没有配音项目时，全部句子使用默认音色
            sentences = context.get("sentences", [])
            for s in sentences:
                s["voice_id"] = None
            logger.info(f"[Conversion:{context.get('task_id')}] Assign stage: "
                        f"no project ({project_id}), all sentences use default voice")
            return {"sentences": sentences}

        roles = project.get("roles", [])
        mode = project.get("mode", "single")

        # Build voice assignment map
        voice_map = {}  # role_id -> voice_id
        for role in roles:
            if role.get("voice_id"):
                voice_map[role["id"]] = role["voice_id"]

        # Name-based map (role name -> voice_id)，做小写去空白归一化
        name_voice_map = {}
        narrator_voice_id = None
        for role in roles:
            vid = role.get("voice_id")
            if not vid:
                continue
            name = (role.get("name", "") or "").strip().lower()
            if name:
                name_voice_map[name] = vid
            # 检测 narrator 角色（支持中文"旁白"和英文"narrator"，以及 role_type 字段）
            role_type = (role.get("role_type", "") or "").lower()
            if role_type == "narrator" or name in ("旁白", "narrator"):
                narrator_voice_id = vid

        # 若没有显式 narrator，取第一个可用音色作为回退
        default_voice = next(iter(voice_map.values()), None) if voice_map else None
        fallback_narrator = narrator_voice_id or default_voice

        # ---- PY-114: 分段分配 (voice_assignments 表) ----
        # [{chapter_id, voice_id, role_id, start_char, end_char, settings}]
        # 区间覆盖优先于名表映射
        seg_assignments = []
        for a in project.get("assignments", []) or []:
            a_voice = a.get("voice_id") or voice_map.get(a.get("role_id"))
            if not a_voice:
                continue
            role_name = None
            for role in roles:
                if role.get("id") == a.get("role_id"):
                    role_name = (role.get("name") or "").strip().lower()
                    break
            seg_assignments.append({
                "chapter_id": a.get("chapter_id"),
                "voice_id": a_voice,
                "role_name": role_name,
                "start": int(a.get("start_char", 0) or 0),
                "end": int(a.get("end_char", 0) or 0),
                "settings": a.get("settings", {}) or {},
            })

        # Assign voices to sentences
        sentences = context.get("sentences", [])
        seg_hits = 0
        name_hits = 0
        cast_stats: dict = {}
        if mode == "single":
            for sent in sentences:
                sent["voice_id"] = default_voice
        else:
            # ---- 批次C (P6): 有确认过的 cast -> 别名归一表 + 句级角色参数 ----
            cast = _load_wired_cast(project_id, project)
            if cast:
                from modules.workflow.cast_pipeline_hooks import assign_with_cast
                cast_stats = assign_with_cast(context, cast).get("cast_stats", {}) or {}
                sentences = context.get("sentences", sentences)
                context["cast_assign_stats"] = cast_stats
                name_hits = (int(cast_stats.get("by_name", 0))
                             + int(cast_stats.get("by_alias", 0)))
                logger.info(f"[Conversion:{context.get('task_id')}] Assign stage: "
                            f"cast 接线生效 {cast_stats}")
            else:
                for sent in sentences:
                    # 1) 名表映射 (PY-113: speaker 归一化)
                    speaker = (sent.get("speaker", "") or "").strip().strip('"\'“”『「」』')
                    speaker = speaker.lower()
                    if speaker and speaker in name_voice_map:
                        sent["voice_id"] = name_voice_map[speaker]
                        sent["role_name"] = speaker
                        name_hits += 1
                    else:
                        sent["voice_id"] = fallback_narrator
                        sent["role_name"] = "narrator"

            # 2) 区间覆盖（优先级高于名表；PY-114 语义保持不变）
            seg_hits = _overlay_segment_assignments(sentences, seg_assignments)

        context["voice_map"] = voice_map

        assigned = sum(1 for s in sentences if s.get("voice_id"))
        logger.info(f"[Conversion:{context.get('task_id')}] Assign stage: "
                    f"{assigned}/{len(sentences)} sentences assigned voices (mode={mode}, "
                    f"speaker名表命中={name_hits}, 分段区间命中={seg_hits})")
        return {"sentences": sentences}

    async def _acquire_engine(self, task_id: str, context: dict, engine_name: str):
        """PY-115: 取"已加载"引擎 —— 冷启动不再阻塞首句，且全程上报"模型加载中"。

        旧行为: `engine_registry.get_loaded()` 在 synthesize 循环前同步执行,
        CPU 冷启动实测 220.6s (qa/round-1/artifacts/p7_timings.json)，期间进度
        停在 import 0% 约 4 分钟，用户视角等同卡死。
        新行为: 加载交给 model_preloader 后台线程（后端启动/任务创建即预热,
        与 1~6 阶段并行 = 首句合成先队列化），本协程每 2s 查一次终态,
        每 5s 通过 tracker + DB 发一次 `current_stage="loading_model"` 心跳事件。
        加载失败按 PY-118 上抛 EngineLoadError，原因透到 task.error_message。
        """
        import time

        from modules.tts_engine.model_preloader import model_preloader
        from modules.tts_engine.engine_registry import engine_registry, EngineLoadError

        pre = model_preloader
        pre.start(engine_name)  # 幂等: ready/loading 中不会重复加载 4.5GB 权重

        t0 = time.time()
        last_beat = -1
        # US-C1: 等的是**本引擎那个槽**的终态 Event。旧代码等的是共享 Event ——
        # 别的引擎加载完也会把它置位，于是这里看到 state=="ready" 却是"假 ready"
        # （没人给本引擎加载过），一路放到函数末尾的 get_loaded，在 async 协程里
        # 同步冷启动 220~460s，整个事件循环停摆（PY-115 症状升级为全局故障）。
        # 等待本身也必须离开事件循环（Event.wait 直接压在协程里 = 每 2s 卡死一次
        # WS 心跳/进度推送），所以走 to_thread。
        while not await asyncio.to_thread(pre.wait_terminal, 2.0, engine_name):
            elapsed = time.time() - t0
            if int(elapsed // 5) != last_beat:  # 5s 一次心跳, 避免刷屏与狂写 DB
                last_beat = int(elapsed // 5)
                msg = pre.snapshot(engine_name).get("message") or "模型加载中"
                await self._emit(task_id, context, "loading_model", 6.0 / 9.0,
                                 message=msg)
                logger.info(f"[Conversion:{task_id}] {msg}")

        snap = pre.snapshot(engine_name)
        if snap.get("engine") != engine_name:
            # 引擎名断言：槽位归属与需求不符即判失败，**绝不放行**
            # （放行 = 下面那行在协程里同步加载，见上面的事故链）。
            raise EngineLoadError(
                engine_name,
                RuntimeError(
                    f"预热槽引擎名不符：需要 '{engine_name}'，快照是 "
                    f"'{snap.get('engine')}' (state={snap.get('state')})"
                    " —— 拒绝跨引擎假 ready (US-C1)"))
        if snap.get("state") != "ready":
            raise EngineLoadError(
                engine_name,
                RuntimeError(snap.get("error") or "模型加载失败（原因未知）"))

        waited = round(time.time() - t0, 1)
        context["model_load_seconds"] = snap.get("load_seconds")
        logger.info(f"[Conversion:{task_id}] 引擎 '{engine_name}' 就绪 "
                    f"(本次等待 {waited}s, 后台加载 {snap.get('load_seconds')}s)")
        await self._emit(task_id, context, "loading_model", 6.0 / 9.0,
                         message=f"模型已就绪（加载 {snap.get('load_seconds')}s）")
        return engine_registry.get_loaded(engine_name, strict=True)

    def _engine_refuses_voice(self, engine_name: str, voice_id: str) -> bool:
        """改派后的引擎明说"这枚 voice_id 我发不了声"才为真 (PY-360)。

        判据来自引擎自己的 `resolves_voice`（默认实现答"认"，只有只认自己库的适配器
        会准确拒绝）。没有这个口的一律按"认"处理：宁可让引擎自己报错，也不越权替它
        换掉用户选的嗓子。
        """
        from modules.tts_engine.engine_registry import engine_registry
        inst = engine_registry.get(engine_name)
        probe = getattr(inst, "resolves_voice", None)
        if not callable(probe):
            return False
        try:
            return not bool(probe(voice_id))
        except Exception as e:                        # 问不出来 != 它不认
            logger.warning(f"[P7] 询问引擎 '{engine_name}' 是否认音色 {voice_id} 时异常: {e}")
            return False

    # 粤语档的识别串：`zh-hk`/`zh-yue` 是 Azure/builtin 里的 locale 写法，
    # `粤`/`粵`（简繁两形，库里真行"圓龍(粵語男声)"用的是繁体）与 `cant` 覆盖名字列
    # —— 后者与 `language_tier.tier_from_language` 同一条判据，不另立一套。
    _CANTONESE_TOKENS = ("zh-hk", "zh-yue", "cant", "粤", "粵")

    @classmethod
    def _row_is_cantonese(cls, row) -> bool:
        """这枚音色行说的是粤语吗 (PY-361)。

        只看名字不够：生产库 3 枚粤语行里有 2 枚的名字就是 Azure 短名
        `Edge-zh-HK-HiuMaanNeural`，语种只写在 `meta.edge_voice` 的 locale 前缀里。
        所以取 meta 短名 + language + 名字三处并集，命中任一即算粤语档。
        """
        meta = getattr(row, "meta", None)
        meta = meta if isinstance(meta, dict) else {}
        probe = " ".join([str(meta.get("edge_voice") or ""),
                          str(getattr(row, "language", None) or ""),
                          str(getattr(row, "name", None) or "")]).lower()
        return any(tok in probe for tok in cls._CANTONESE_TOKENS)

    @staticmethod
    def _gender_from_name(name) -> str:
        """从音色名里读性别 (PY-361)。

        `voice_profiles.gender` 在生产库 1007 枚 voxcpm 行里有 539 枚是空的，其中 73 枚
        名字明写"男声/女声"—— 空值不等于没证据。两个字都出现（或都不出现）判为不知道：
        宁可不给性别偏好，也不拿一次猜测改选。
        """
        text = str(name or "")
        female, male = "女" in text, "男" in text
        if female and not male:
            return "female"
        if male and not female:
            return "male"
        return ""

    def _pick_substitute_voices(self, pending: list) -> list:
        """给"被改派到别的引擎、但那引擎认不出这枚 uuid"的音色行挑代打嗓子 (PY-360)。

        认不出的 uuid 会被目标适配器直接抛错（`edgetts_adapter._resolve_voice` 的
        PY-117 纪律："不许静默退化成默认音色"），所以换引擎的一方必须连 voice_id 一起
        换掉，否则整本书会在第一句就死（2026-09-26 整书实测第 1/5070 句）。

        挑选口径：只在目标引擎自己的音色行里选，按 vid 排序 —— 同一本书两次跑要得到
        同一批嗓子；同性别内轮转（多角色不能全塌成同一把嗓子）；每一枚候选都要再过一次
        引擎自己的判据（库里的行也可能映射缺失），全都不认或库为空时 `to=None`：那是
        引擎的默认音色，由调用方写进任务日志。

        PY-361 补的两条：候选先按"非粤语优先"排序（`voice_store.list` 是
        created_at desc，2026-09-22 导入的 zh-HK 目录行恰好在整池最前，于是普通话男旁白
        被抓成粤语女声念完整本书）；性别列空着时从名字读，不再当成"没有性别证据"。
        注意是**排序不是过滤**：整池只剩粤语行时照样能选出嗓子。
        """
        from modules.voice_manager.voice_store import voice_store
        report: list = []
        pools: dict = {}
        used: dict = {}
        for vid, prof, cond, engine in sorted(pending, key=lambda t: t[0]):
            pool = pools.get(engine)
            if pool is None:
                try:
                    pool = [v for v in voice_store.list_all(engine=engine)
                            if getattr(v, "id", None)]
                except Exception as e:            # 取不到库 != 可以把原 uuid 递过去
                    logger.warning(f"[P7] 取引擎 '{engine}' 的音色库失败: {e}")
                    pool = []
                pools[engine] = pool
            gender = (str(cond.get("gender") or "").strip().lower()
                      or str(getattr(prof, "gender", None) or "").strip().lower()
                      or self._gender_from_name(getattr(prof, "name", None)))
            ranked = sorted(pool, key=lambda v: (
                self._row_is_cantonese(v),
                str(getattr(v, "id", None) or "")))
            gendered = [v for v in ranked
                        if gender
                        and str(getattr(v, "gender", None) or "").strip().lower() == gender]
            bucket = gendered or ranked
            key = (engine, gender if gendered else "")
            used.setdefault(key, 0)
            sub = None
            for _ in range(len(bucket)):
                cand = bucket[used[key] % len(bucket)]
                used[key] += 1
                if not self._engine_refuses_voice(engine, getattr(cand, "id", None)):
                    sub = cand
                    break
            report.append({
                "from": vid,
                "from_name": getattr(prof, "name", None) or vid,
                "from_engine": getattr(prof, "engine", None),
                "to": getattr(sub, "id", None),
                "to_name": getattr(sub, "name", None),
                "engine": engine,
            })
        return report

    async def _stage_synthesize(self, context: dict, settings: dict) -> dict:
        """Stage 7: Synthesize - convert text to audio using TTS engine.

        修复 (P0 #1): 使用 asyncio.to_thread() 将同步 TTS 调用移至线程池执行，
                     避免阻塞事件循环，WebSocket 可继续推送进度。
        修复 (P1 #14): duration_ms 从 engine.sample_rate 动态计算。
        修复 (P0 #卡住): 每处理一段即报告进度到 tracker，前端看到实时进展。
        PY-115 修复: 模型冷启动走后台预热，等待期发 loading_model 心跳事件。
        PY-117 修复: 移除"零数组静音"兜底 —— 合成异常/合成出静音一律 raise，
                     任务标 failed。此前"时长正常但全静音"的成品仍标 completed，
                     违反 PROTOCOL 硬规则 1（兜底/模拟分支不得计入成功）。
        批次C 接线 (P7): speed/pitch 由"只读全局 settings"改为**句级优先、全局兜底**
                     （synth_params_for_sentence），PY-114 写进句子的区间参数与
                     cast 的角色参数才真正到达引擎；emotion 仍取 P5 句级 tone。
        """
        import numpy as np
        from modules.voice_manager.conditioning import \
            resolve_profile_conditioning
        from modules.workflow.cast_pipeline_hooks import synth_params_for_sentence

        task_id = context.get("task_id")
        sentences = context.get("sentences", [])
        total = len(sentences)
        audio_arrays = []   # [{"index":i, "chapter_id":..., "audio": np.ndarray, "duration_ms":...}]
        # 指令#1（双模式）：这一档默认哪个引擎由 `modules/tts_engine/tier.py` 定，
        # 不再是 `settings.get("engine", "voxcpm")` 那枚字面量 —— 旧写法让"切档"在主链上失效。
        from modules.tts_engine import tier as tts_tier
        tier = tts_tier.resolve_tier((settings or {}).get("tier"))
        engine_name = (settings or {}).get("engine") or tts_tier.default_engine(tier)
        tier_warning = tts_tier.check_engine_for_tier(tier, engine_name)
        if tier_warning:
            logger.warning(f"[P7] {tier_warning}")
            context["tier_warning"] = tier_warning
        if settings.get("mock_tts", False):
            # PY-117: 静音替身停用（它会让"引擎全挂"伪装成可交付成品）
            raise RuntimeError(
                f"settings.mock_tts（静音替身）已停用 (PY-117)：静音段不得作为合成产物"
                f"进入打包阶段。请确认引擎 '{engine_name}' 可用后重跑；"
                f"只想验证 1~6 阶段请直接调用对应 stage handler。")

        engine = None  # PY-116: 不再是单引擎，见下方句级路由
        from modules.tts_engine.engine_registry import engine_registry
        from modules.voice_manager.voice_store import voice_store
        from modules.voice_manager.language_tier import openvoice_tier_gate

        # ---------- PY-116: 句级引擎路由 ----------
        # 句子绑定音色的 profile.engine 决定用哪个引擎:
        #   Edge 在线音色(edgetts) -> 毫秒级"加载"+可并发, 整书提速 1~2 个数量级;
        #   克隆/downloaded 音色 -> 仍走其绑定的本地引擎(voxcpm 等)。
        # 单引擎项目走与旧实现等价的路径(同引擎同 sr, 无重采样)。
        voice_engine_map: dict = {}
        voice_substitute_map: dict = {}
        tier_voice_downgraded: list = []
        # PY-360: 最终路由到在线引擎、但 uuid 只存在于原引擎库里的音色行 (vid, prof, cond, engine)
        needs_voice_substitute: list = []
        for vid in sorted({s.get("voice_id") for s in sentences if s.get("voice_id")}):
            prof = voice_store.get(vid)
            if prof is None:
                continue
            # P0-1: 音色行 -> 引擎这条"读 profile 拼条件"也走单点函数
            # （modules/voice_manager/conditioning.py），与试听/P7 同一套优先级。
            # 音色行没写 engine 时落回 defaults，与旧写法（不入表 -> _engine_for
            # 用 engine_name）等价。
            cond = resolve_profile_conditioning(prof, defaults={"engine": engine_name})
            # PY-323(a′): 语种档闸门（与试听高档同一判据）—— 参考音频是外文的
            # openvoice 行跨语种克隆字准不合格（实测 0.500~0.917），改由兜底引擎发声。
            # 原因必须入日志：换引擎=换嗓子，静默换会让"这枚音色"悄悄变成另一个。
            ename, tier_reason = openvoice_tier_gate(cond.get("engine"), prof,
                                                     fallback=engine_name)
            if tier_reason:
                logger.warning(f"[P5] 音色 {vid} 语种档降级: {tier_reason}")
                cond["engine"] = ename
            # 指令#1（双模式）的最后一道闸：在线档里遇到"绑着本地引擎"的音色行。
            # 不装本地权重（那是一整本书都不该为一句台词吃 5GB），改用本档默认引擎发声 ——
            # 但**逐枚计数并写进任务日志**：换引擎=换嗓子，不声明就是欺骗用户。
            chosen = cond.get("engine")
            if (tier == tts_tier.TIER_ONLINE and chosen
                    and not tts_tier.is_online_engine(chosen)):
                tier_voice_downgraded.append((str(vid), chosen))
                cond["engine"] = engine_name
            # PY-360: 上面两道门（语种档 / 在线档）都可能把音色改到别的引擎发声，
            # 但句上绑的 voice_id 还是原引擎那行的 uuid —— 有的适配器（Edge）对自己
            # 库里没有的 uuid 会按 PY-117 直接抛错（拒绝静默退化成默认音色）。
            # 所以"换引擎"之前先问一句"新引擎认不认这枚嗓子"，不认就必须连嗓子一起换，
            # 缺一枚都会在整本书第一句就死。
            routed = cond.get("engine")
            if routed and routed != (getattr(prof, "engine", None) or "") \
                    and self._engine_refuses_voice(routed, str(vid)):
                needs_voice_substitute.append((str(vid), prof, cond, routed))
            if cond.get("engine"):
                voice_engine_map[vid] = cond["engine"]

        substitution_report = self._pick_substitute_voices(needs_voice_substitute)
        for _item in substitution_report:
            voice_substitute_map[_item["from"]] = _item["to"]
        sub_by_vid = {_item["from"]: _item for _item in substitution_report}

        def _sub_phrase(item: dict) -> str:
            to = item["to_name"] or "该引擎默认音色(在线库无可选音色)"
            return f"{item['from_name']}->{to}"

        if tier_voice_downgraded:
            downgraded_vids = {vid for vid, _ in tier_voice_downgraded}
            items = [sub_by_vid[vid] for vid in sorted(downgraded_vids)
                     if vid in sub_by_vid]
            downgrade_msg = (
                f"[P7] 在线档下 {len(tier_voice_downgraded)} 枚音色绑的是本地引擎"
                f"（{sorted({e for _, e in tier_voice_downgraded})}），已改由 "
                f"'{engine_name}' 发声 —— 音色身份未保留，需要原嗓子请切到本地档")
            if items:
                downgrade_msg += "；代打嗓子: " + ", ".join(map(_sub_phrase, items))
            logger.warning(downgrade_msg)
            # "写进任务日志"要落到 GET /tasks/{id} 看得见的地方，只进服务端日志=用户看不到
            self._append_task_log(task_id, "synthesize", message=downgrade_msg,
                                  extra={"tier": tier,
                                         "tier_voice_downgraded":
                                             len(tier_voice_downgraded),
                                         "tier_voice_substitutions": items})
        # 语种档那类降级不走在线档的门，也要单独交代（换了嗓子不声明就是骗用户）
        gated_only = [_item for _item in substitution_report
                      if _item["from"] not in {v for v, _ in tier_voice_downgraded}]
        if gated_only:
            gate_msg = (f"[P7] {len(gated_only)} 枚音色改由 "
                        f"{sorted({i['engine'] for i in gated_only})} 发声，该引擎认不出原 uuid"
                        f" —— 音色身份未保留，"
                        f"代打嗓子: " + ", ".join(map(_sub_phrase, gated_only)))
            logger.warning(gate_msg)
            self._append_task_log(task_id, "synthesize", message=gate_msg,
                                  extra={"tier_voice_substitutions": gated_only})
        context["tier"] = tier
        context["tier_voice_downgraded"] = len(tier_voice_downgraded)
        context["tier_voice_substituted"] = len(substitution_report)
        context["tier_voice_substitutions"] = substitution_report

        nonempty_vids = [s.get("voice_id") for s in sentences
                         if (s.get("text") or "").strip()]
        default_needed = any((not vid) or vid not in voice_engine_map
                             for vid in nonempty_vids)
        needed = []
        if default_needed or not nonempty_vids:
            needed.append(engine_name)
        for vid in nonempty_vids:
            ename = voice_engine_map.get(vid)
            if ename and ename not in needed:
                needed.append(ename)

        def _online(ename: str) -> bool:
            inst = engine_registry.get(ename)
            return bool(inst is not None and getattr(inst, "online", False))

        engines: dict = {}

        async def _acquire(ename: str):
            if ename in engines:
                return engines[ename]
            if _online(ename):
                # 在线引擎无权重可预热，不占用 model_preloader 单槽，
                # 避免被本地引擎 220s 冷启动队列卡住首句 (PY-115/PY-116)
                engines[ename] = engine_registry.get_loaded(ename, strict=True)
            else:
                engines[ename] = await self._acquire_engine(task_id, context, ename)
            return engines[ename]

        for ename in sorted(needed, key=lambda e: 0 if _online(e) else 1):
            await _acquire(ename)

        sr = max(int(e.sample_rate) for e in engines.values())
        context["engine_name"] = next(iter(engines)) if len(engines) == 1 \
            else "+".join(sorted(engines))
        context["sample_rate"] = sr

        def _engine_for(sent: dict):
            vid = sent.get("voice_id")
            ename = voice_engine_map.get(vid, engine_name) if vid else engine_name
            return engines.get(ename) or engines[engine_name]

        def _voice_for(sent: dict):
            """句上绑的音色行 -> 实际喂给路由后引擎的 voice_id (PY-360)。"""
            vid = sent.get("voice_id")
            if not vid:
                return None
            return voice_substitute_map.get(vid, vid)

        def _resample_to_unified(audio, src_sr: int):
            if src_sr == sr:
                return audio
            import soxr
            return np.asarray(soxr.resample(audio, src_sr, sr), dtype=np.float32)

        processed_chars_accum = 0
        loop = asyncio.get_event_loop()
        skipped_unpronounceable = 0

        def _cancelled() -> bool:
            """WF-07: 句级取消检查（命中时顺带置位 pipeline Event，让阶段边界也停）。"""
            if self.is_cancelled(task_id):
                self.request_cancel(task_id)
                return True
            return False

        # 进度更新频率：每 20 段报告一次（避免过于频繁的 WebSocket 推送/DB 写入）
        report_interval = max(1, min(50, total // 100)) if total > 0 else 1

        async def _synth_one(i: int, sent: dict):
            """单句合成 -> 校验后的 np 数组。失败带归因上抛 (PY-117 语义不变)。"""
            text = sent.get("text", "") or ""
            tone = sent.get("tone", "neutral")
            # 批次C (P7): 句级 speed/pitch 优先（P6 的 cast 角色参数 或
            # VoiceAssignment 区间参数都写在句上），全局 settings 兜底。
            sp = synth_params_for_sentence(sent, settings)
            eng = _engine_for(sent)
            try:
                audio_data = await loop.run_in_executor(
                    None,  # 默认 ThreadPoolExecutor
                    lambda: eng.synthesize(
                        text=text,
                        voice_id=_voice_for(sent),
                        speed=sp["speed"],
                        pitch=sp["pitch"],
                        emotion=tone,
                    )
                )
            except Exception as e:
                # PY-117: 失败句不再被静音替换，直接中止任务（原因进 task.error_message）
                msg = (f"第 {i + 1}/{total} 句合成失败"
                       f"(engine={getattr(eng, 'engine_name', '?')}, "
                       f"text={text[:24]!r}): {type(e).__name__}: {e}")
                logger.error(f"[Conversion:{task_id}] {msg}")
                raise RuntimeError(msg) from e

            # 保证是 float32 numpy 数组
            if not isinstance(audio_data, np.ndarray):
                audio_data = np.asarray(audio_data, dtype=np.float32)
            elif audio_data.dtype != np.float32:
                audio_data = audio_data.astype(np.float32, copy=False)

            if audio_data.size == 0:
                raise RuntimeError(
                    f"第 {i + 1}/{total} 句合成返回空音频 (text={text[:24]!r})"
                    f" —— 不以静音补齐 (PY-117)")
            audio_data = _resample_to_unified(audio_data, int(eng.sample_rate))

            seg_peak = float(np.max(np.abs(audio_data)))
            if seg_peak < SILENCE_PEAK_THRESHOLD:
                # 引擎"半死"时会返回全零而不抛错（PY-118 场景），必须判失败
                raise RuntimeError(
                    f"第 {i + 1}/{total} 句合成结果为静音 (peak={seg_peak:.6f}, "
                    f"text={text[:24]!r}) —— 引擎异常，中止任务 (PY-117)")
            return audio_data

        def _record_segment(i: int, sent: dict, audio_data) -> None:
            text = sent.get("text", "") or ""
            duration_ms = (len(audio_data) / sr) * 1000.0
            audio_arrays.append({
                "index": i,
                "chapter_id": sent.get("chapter_id"),
                "chapter_title": sent.get("chapter_title", ""),
                "audio": audio_data,
                "duration_ms": duration_ms,
                "peak": float(np.max(np.abs(audio_data))),
                "text": text[:100],
            })
            nonlocal processed_chars_accum
            processed_chars_accum += len(text)
            context["processed_chars"] = processed_chars_accum

        # US-B4: 给 gptsovits 适配器注入降级通报口 —— prompt-free/speed 夹取/
        # 分桶关闭等一切静默降级必须写进 task_log（PY-117：降级必须归因）。
        gsv_engine = engines.get("gptsovits")
        use_gsv_grouping = gsv_engine is not None
        if use_gsv_grouping and hasattr(gsv_engine, "set_log_sink"):
            def _gsv_log_sink(msg, _tid=task_id):
                self._append_task_log(_tid, "synthesize", message=f"[gptsovits] {msg}")
            gsv_engine.set_log_sink(_gsv_log_sink)

        if all(_online(getattr(e, "engine_name", "")) for e in engines.values()) \
                and not use_gsv_grouping:
            # ---------- PY-116 并发快速路径: 全在线引擎, IO 等待按块并行 ----------
            # 本地引擎(CPU/GPU 计算型)绝不进这条路径, 并发只会互相拖累。
            import os as _os
            concurrency = max(1, min(16, int(_os.getenv("PYY_EDGE_CONCURRENCY", "6"))))
            pending = []
            for i, s in enumerate(sentences):
                if has_pronounceable_content(s.get("text")):
                    pending.append((i, s))
                else:
                    skipped_unpronounceable += 1      # WF-08
            for c0 in range(0, len(pending), concurrency):
                if _cancelled():                      # WF-07: 块边界即止
                    break
                chunk = pending[c0:c0 + concurrency]
                results = await asyncio.gather(
                    *(_synth_one(i, s) for i, s in chunk), return_exceptions=True)
                for (i, s), r in zip(chunk, results):
                    if isinstance(r, BaseException):
                        raise r
                    _record_segment(i, s, r)
                done = min(c0 + concurrency, len(pending))
                await asyncio.sleep(0)
                if done % report_interval < concurrency or done == len(pending):
                    seg_ratio = done / len(pending) if pending else 1.0
                    overall_progress = 6.0 / 9.0 + seg_ratio * (1.0 / 9.0)
                    await self._emit(task_id, context, "synthesize", overall_progress,
                                     message=f"合成中 {done}/{len(pending)} 句(在线并发x{concurrency})")
        elif use_gsv_grouping:
            # ---------- US-B5: gptsovits 音色分组 + /set_refer_audio 预热 ----------
            # 服务端 ref 特征缓存单槽(TTS.py:1144-1150)，A/B/A/B 交替每换角重付
            # ~84s 冷成本。按 (chapter,voice) 稳定分组背靠背执行，组首句前预热一次。
            exec_items = []
            for i, s in enumerate(sentences):
                if not has_pronounceable_content(s.get("text", "") or ""):
                    skipped_unpronounceable += 1              # WF-08
                    continue
                exec_items.append({
                    "index": i,
                    "chapter_id": s.get("chapter_id"),
                    "voice_id": s.get("voice_id"),
                    "gsv": _engine_for(s) is gsv_engine,
                    "sent": s,
                })
            doc_order = list(exec_items)
            ordered = group_gptsovits_execution_order(exec_items)
            doc_switches = count_gsv_reference_switches(doc_order)
            grp_switches = count_gsv_reference_switches(ordered)
            n_items = len(ordered)
            warmed_key = object()
            done = 0
            for item in ordered:
                if _cancelled():                              # WF-07: 句边界即止
                    break
                i = item["index"]
                sent = item["sent"]
                # 组首句预热：gsv 且 (chapter_id, voice_id) 与上次不同 -> /set_refer_audio
                if item["gsv"]:
                    key = (item.get("chapter_id"), item.get("voice_id"))
                    if key != warmed_key:
                        warmed_key = key
                        try:
                            ref_path = gsv_engine._ref_for_voice(item["voice_id"])[0]
                            await loop.run_in_executor(
                                None, lambda p=ref_path: gsv_engine.warm_reference(p))
                        except Exception as e:
                            # 预热失败只 warn 不抛：/tts 会按 ref_audio_path 自会重算
                            logger.warning(
                                f"[Conversion:{task_id}] gsv 预热跳过 "
                                f"(voice={item.get('voice_id')!r}): "
                                f"{type(e).__name__}: {e}")
                audio_data = await _synth_one(i, sent)
                _record_segment(i, sent, audio_data)
                done += 1
                if done % 10 == 0:
                    await asyncio.sleep(0)
                if done % report_interval == 0 or done == n_items:
                    seg_ratio = done / n_items if n_items else 1.0
                    overall_progress = 6.0 / 9.0 + seg_ratio * (1.0 / 9.0)
                    await self._emit(task_id, context, "synthesize", overall_progress,
                                     message=f"合成中 {done}/{n_items} 句(gsv 音色分组)")
            # US-B5 验收：分组确实降低 ref 装载次数
            context["gsv_grouping"] = {"doc_reference_switches": doc_switches,
                                       "grouped_reference_switches": grp_switches}
            if grp_switches < doc_switches:
                self._append_task_log(
                    task_id, "synthesize",
                    message=f"gsv 音色分组调度: ref 装载次数 {doc_switches} -> "
                            f"{grp_switches}（降 {doc_switches - grp_switches}）",
                    extra=context["gsv_grouping"])
        else:
            # ---------- 顺序路径（含任一本地引擎, 无 gptsovits）: 与旧实现逐句等价 ----------
            for i, sent in enumerate(sentences):
                if _cancelled():                      # WF-07: 句边界即止
                    break
                text = sent.get("text", "") or ""
                if not has_pronounceable_content(text):
                    skipped_unpronounceable += 1      # WF-08
                    continue

                audio_data = await _synth_one(i, sent)
                _record_segment(i, sent, audio_data)

                # 让出事件循环（每 10 句一次，防止长时间阻塞 WebSocket / HTTP 响应）
                if i % 10 == 0:
                    await asyncio.sleep(0)

                # 定期报告进度
                if i % report_interval == 0 or i == total - 1:
                    # 合成阶段占总进度的 1/9（第 7 个阶段，索引 6）
                    seg_ratio = (i + 1) / total if total > 0 else 1.0
                    base = 6.0 / 9.0
                    stage_range = 1.0 / 9.0
                    overall_progress = base + seg_ratio * stage_range

                    await self._emit(task_id, context, "synthesize", overall_progress,
                                     message=f"合成中 {i + 1}/{total} 句")

        # US-B5: 分组只改**执行顺序**；_record_segment 按 index 落账，成品顺序由
        # index 决定（下游 P8 混音分桶/P9 拼接都按 index 保序）。交下游前按 index 归位，
        # 保证"乱序执行 == 顺序执行"的成品 index 序列逐字一致（非分组路径本就是 index 序，no-op）。
        if use_gsv_grouping and audio_arrays:
            audio_arrays.sort(key=lambda seg: seg["index"])

        context["skipped_unpronounceable"] = int(context.get("skipped_unpronounceable", 0)) \
            + skipped_unpronounceable

        if _cancelled():
            # WF-07: 取消优先级高于 PY-117 —— 已经停止合成的任务不能因为
            # "没产出音频"被判成 failed，必须落 cancelled 终态。
            logger.info(f"[Conversion:{task_id}] Synthesize 阶段收到取消，提前结束 "
                        f"(已合成 {len(audio_arrays)}/{total} 句)")
            await self._emit(task_id, context, "synthesize", 6.0 / 9.0,
                             message="已取消，停止合成")
            context["status"] = "cancelled"
            return {
                "status": "cancelled",
                "audio_arrays": audio_arrays,
                "sample_rate": sr,
                "skipped_unpronounceable": skipped_unpronounceable,
            }

        if not audio_arrays:
            raise RuntimeError(
                f"合成阶段未产出任何音频段（输入 {total} 句均无有效文本）"
                f" —— 任务失败而非输出静音成品 (PY-117)")

        context["audio_arrays"] = audio_arrays
        context["sample_rate"] = sr
        logger.info(f"[Conversion:{context.get('task_id')}] Synthesize stage: "
                    f"{len(audio_arrays)} segments, sample_rate={sr}, "
                    f"peak {min(s['peak'] for s in audio_arrays):.4f}"
                    f"~{max(s['peak'] for s in audio_arrays):.4f}")
        return {"audio_arrays": audio_arrays, "sample_rate": sr,
                "skipped_unpronounceable": skipped_unpronounceable}

    def _stage_postprocess(self, context: dict) -> dict:
        """Stage 8: Postprocess - normalize, remove silence, add effects.

        直接操作 context['audio_arrays'] 中的 np.ndarray，避免 bytes↔ndarray 来回转换。

        PY-123 修复：旧实现只丢 `len(audio) == 0` 的段，纯零数组经 remove_silence
        长度不变(24000→24000)、normalize 因 peak<1e-6 原样返回，于是"时长正常、
        内容静音"的兜底段被原样拼进成品（与 PY-117 合流）。现按峰值门限剔除并计数。
        批次C 接线 (P8): 项目配了 `global_settings["mix"]` 时，段级处理完成后追加混音
        子步骤（cast_pipeline_hooks.postprocess_with_mix：段间隙→拼章→BGM 铺底+闪避
        →SFX→角色/主增益→软限幅+LUFS），audio_arrays 变为"每章一条成品轨"。
        无 mix 配置 -> 完全不调用混音器，本阶段输出与接线前**逐样本一致**（直通）。
        """
        from modules.tts_engine.audio_postprocess import AudioPostProcessor
        import numpy as np

        task_id = context.get("task_id")
        sr = context.get("sample_rate", 24000)
        processor = AudioPostProcessor(sample_rate=sr)

        audio_arrays = context.get("audio_arrays", [])
        if not audio_arrays:
            raise RuntimeError(
                "[PY-117] Postprocess: audio_arrays 为空 —— 无任何合成产物，"
                "任务失败而非输出静音成品。请检查章节内容(chapters content / "
                "Chapter.to_dict 是否返回 content)。")

        processed = []
        dropped_silent = 0
        dropped_empty = 0
        dropped_failed = 0
        for seg in audio_arrays:
            audio = seg.get("audio")
            if audio is None or len(audio) == 0:
                dropped_empty += 1
                continue
            try:
                audio = processor.remove_silence(audio)
                peak = float(np.max(np.abs(audio))) if len(audio) else 0.0
                if len(audio) == 0 or peak < SILENCE_PEAK_THRESHOLD:
                    # 静音段（含引擎兜底零数组）：剔除，绝不带入成品
                    dropped_silent += 1
                    logger.warning(
                        f"[Conversion:{task_id}] Postprocess: 剔除静音段 "
                        f"index={seg.get('index')} peak={peak:.6f} "
                        f"text={str(seg.get('text', ''))[:24]!r} (PY-123)")
                    continue
                audio = processor.normalize_volume(audio)
                audio = processor.apply_fade(audio, fade_in_ms=50, fade_out_ms=100)
                seg["audio"] = audio
                seg["duration_ms"] = (len(audio) / sr) * 1000.0
                seg["peak"] = float(np.max(np.abs(audio)))
                processed.append(seg)
            except Exception as e:
                dropped_failed += 1
                logger.error(
                    f"[Conversion:{task_id}] "
                    f"Postprocess failed for segment {seg.get('index')}: {e}"
                )

        context["dropped_silent_segments"] = dropped_silent + dropped_empty + dropped_failed
        if not processed:
            raise RuntimeError(
                f"[PY-123] 后处理剔除静音/空/失败段 {dropped_silent}/"
                f"{dropped_silent + dropped_empty + dropped_failed} 后无任何可用音频 "
                f"(空段 {dropped_empty}, 处理失败 {dropped_failed}) —— 任务失败，"
                f"不产出静音成品")
        if dropped_silent:
            logger.warning(f"[Conversion:{task_id}] Postprocess: 剔除静音段 "
                           f"{dropped_silent} 个 (PY-123)")

        context["audio_arrays"] = processed
        logger.info(f"[Conversion:{context.get('task_id')}] Postprocess stage: "
                    f"{len(processed)} segments (剔除 静音={dropped_silent} "
                    f"空={dropped_empty} 失败={dropped_failed}), sr={sr}")
        # ---- 批次C (P8): 项目配了 global_settings.mix 才混音；无配置直通 ----
        # postprocess_with_mix 对"未配置/等价于直通"的 mix 原样返回入参段列表，
        # 上面 P8 自身的 去静音/归一/淡入淡出 结果逐样本不变（single 模式回归保护）。
        mix_applied = False
        mix_settings = _project_mix_settings(context.get("project_id"),
                                             context.get("task_id"))
        if mix_settings:
            from modules.workflow.cast_pipeline_hooks import postprocess_with_mix
            before = len(processed)
            mixed = postprocess_with_mix(context, mix_settings, processed)
            processed = mixed.get("audio_arrays") or processed
            mix_applied = bool(mixed.get("applied"))
            context["mix_applied"] = mix_applied
            mix_stats = context.get("mix_stats") or {}
            logger.info(f"[Conversion:{context.get('task_id')}] Postprocess stage: "
                        f"mix 接线 applied={mix_applied}, "
                        f"{before} 段 -> {len(processed)} 条成品轨, "
                        f"stats={mix_stats}")
            # WF-13: mix_applied/mix_stats 必须落到任务记录里（GET /tasks/{id} 可见），
            # 否则"有没有真的混音"只能靠翻服务端日志判断。False 分支同样留痕 ——
            # 排障时"配了 mix 但没生效"和"根本没配 mix"是两件不同的事。
            gain = mix_stats.get("bgm_gain_db")
            self._append_task_log(
                task_id, "postprocess",
                message=f"mix_applied={mix_applied}; "
                        f"{before} 段 -> {len(processed)} 条成品轨"
                        + (f"; bgm_gain_db={gain}" if gain is not None else ""),
                extra={"mix_applied": mix_applied, "mix_stats": mix_stats,
                       "mix_configured": True})
        else:
            self._append_task_log(
                task_id, "postprocess",
                message="mix_applied=False; 项目未配置 global_settings.mix，"
                        "本任务无 BGM/闪避（直通）",
                extra={"mix_applied": False, "mix_configured": False})
        if dropped_silent or dropped_empty or dropped_failed:
            self._append_task_log(
                task_id, "postprocess",
                message=f"段级剔除: 静音={dropped_silent} 空={dropped_empty} "
                        f"失败={dropped_failed} (PY-123)",
                extra={"dropped_silent_segments": dropped_silent,
                       "dropped_empty_segments": dropped_empty,
                       "dropped_failed_segments": dropped_failed})
        context["audio_arrays"] = processed
        return {"audio_arrays": processed,
                "dropped_silent_segments": dropped_silent,
                "dropped_empty_segments": dropped_empty,
                "dropped_failed_segments": dropped_failed,
                "mix_applied": mix_applied}

    def _stage_package(self, context: dict, output_format: str) -> dict:
        """Stage 9: Package - combine audio and save to a playable audio file.

        修复 (P0 #3):
            - 使用 soundfile 写入 WAV/FLAC（真正的音频文件，而不是裸二进制）
            - 使用 ffmpeg 编码 MP3/M4B
            - 集成 M4BBuilder 写入带章节标记的 M4B
            - 路径统一走 Config.OUTPUT_DIR，不再 dirname 硬编码

        Args:
            context: 工作上下文，包含 audio_arrays 列表
            output_format: 'wav' | 'flac' | 'mp3' | 'm4b'

        Returns:
            dict 包含 output_path / output_size / total_segments / duration_ms
        """
        from modules.tts_engine.audio_postprocess import AudioPostProcessor
        from modules.tts_engine.m4b_builder import M4BBuilder
        from modules.tts_engine import ffmpeg_utils
        from backend.config import Config
        import numpy as np

        fmt = (output_format or "m4b").strip().lower().lstrip(".")
        sr = int(context.get("sample_rate", 24000))
        audio_arrays = context.get("audio_arrays", [])

        if not audio_arrays:
            logger.error(f"[Conversion:{context.get('task_id')}] "
                         "Package: audio_arrays is empty. "
                         "Check chapters content (is Chapter.to_dict returning 'content'?).")
            raise RuntimeError(
                "没有可导出的音频内容。请确认："
                "1) 导入的书籍包含有效的章节文本，"
                "2) 章节内容没有被截断为预览（content_preview）。"
            )

        # ---------- 1. 拼接 ----------
        processor = AudioPostProcessor(sample_rate=sr)
        audio_arrays_only = [seg["audio"] for seg in audio_arrays if seg.get("audio") is not None]
        if not audio_arrays_only:
            logger.error(f"[Conversion:{context.get('task_id')}] "
                         "Package: no valid audio segments to export. "
                         "This usually means TTS engine failed to synthesize. "
                         "Check engine availability and chapters content.")
            raise RuntimeError(
                "没有可合成的音频内容。请确认："
                "1) 引擎已正确加载（后台显示 EngineRegistry 'voxcpm' 已启动），"
                "2) 导入的电子书包含有效文本内容。"
            )
        all_audio = processor.concat_audios(audio_arrays_only, gap_ms=300)
        all_audio = processor.apply_fade(all_audio, fade_in_ms=100, fade_out_ms=500)

        # ---------- 1b. 整体能量守门 (PY-117) ----------
        # 旧实现只看 "数组存在即通过"(if not audio_arrays_only: raise)，
        # 引擎全挂/静音兜底时能产出"时长正常但全静音"的文件并标 completed。
        if all_audio.size == 0:
            raise RuntimeError("打包阶段拿到空音频（拼接后 0 样本）—— 任务失败 (PY-117)")
        overall_peak = float(np.max(np.abs(all_audio)))
        active_ratio = float((np.abs(all_audio) > SILENCE_PEAK_THRESHOLD).mean())
        if overall_peak < SILENCE_PEAK_THRESHOLD or active_ratio < 0.01:
            raise RuntimeError(
                f"成品整体静音 (peak={overall_peak:.6f}, active_ratio={active_ratio:.4f}, "
                f"segments={len(audio_arrays)}) —— 引擎/音频链路异常，"
                f"拒绝以静音文件冒充成功 (PY-117)")
        context["overall_peak"] = overall_peak
        context["active_ratio"] = active_ratio
        logger.info(f"[Conversion:{context.get('task_id')}] Package 能量守门: "
                    f"peak={overall_peak:.4f}, active_ratio={active_ratio:.4f}")

        # ---------- 1c. 全局唯一一次归一化 (PY-128) ----------
        # 旧实现：这里 *0.99，随后 _write_wav/_write_flac/_write_mp3_via_ffmpeg/
        # _seg_to_wav_bytes 又各自再归一化一次 -> 成品峰值恒为 0.99 (-0.09 dBFS)，
        # 零余量，任何下游重编码/倍速都可能削波。
        # 现在只在这里归一化到 TARGET_PEAK(=-1 dBFS)，各 writer 只做定量(削波兜底)。
        pre_norm_peak = overall_peak
        norm_gain = TARGET_PEAK / pre_norm_peak if pre_norm_peak > 0 else 1.0
        if norm_gain != 1.0:
            all_audio = all_audio.astype(np.float32) * np.float32(norm_gain)
        post_norm_peak = float(np.max(np.abs(all_audio))) if all_audio.size else 0.0
        context["normalize_gain"] = norm_gain
        logger.info(f"[Conversion:{context.get('task_id')}] Package 归一化(仅此一次): "
                    f"peak {pre_norm_peak:.4f} -> {post_norm_peak:.4f} "
                    f"({20 * math.log10(max(post_norm_peak, 1e-9)):+.2f} dBFS, "
                    f"gain={norm_gain:.4f})")

        total_duration_ms = (len(all_audio) / sr) * 1000.0

        # ---------- 2. 输出路径 ----------
        output_dir = Path(Config.OUTPUT_DIR)
        output_dir.mkdir(parents=True, exist_ok=True)

        book_title = context.get("book_title") or "audiobook"
        safe_title = "".join(c for c in book_title
                             if c.isalnum() or c in (" ", "-", "_")).strip() or "audiobook"
        task_id = context.get("task_id") or uuid.uuid4().hex[:8]
        output_path = str(output_dir / f"{safe_title}_{task_id}.{fmt}")

        # ---------- 3. 按格式编码并写入 (PY-124/125/126/127/302) ----------
        # 旧实现: m4b 失败 -> 悄悄转 mp3 -> 再失败 -> 悄悄转 wav，返回值里没有任何
        # "实际格式"字段，任务表 output_format 仍是 m4b，前端按 m4b 播放直接失败。
        # 现在: 格式不合法/编码器缺失都在写盘前明确抛错；返回值带 format_used。
        requested_fmt = fmt
        format_note = ""
        chapters = []

        if requested_fmt not in SUPPORTED_OUTPUT_FORMATS:
            raise ValueError(
                f"[PY-124] 不支持的输出格式 {output_format!r} —— 可选 "
                f"{'/'.join(SUPPORTED_OUTPUT_FORMATS)}；已拒绝任务，"
                f"不再静默改写为 wav")

        # 需要 ffmpeg 的格式先统一探测（PY-127/PY-302）：
        # 缺失时给 FFmpegUnavailableError（明确 5xx 语义 + 修复建议），
        # 不再是 ffmpeg 分支里裸 FileNotFoundError(WinError 2)。
        if ffmpeg_utils.requires_ffmpeg(requested_fmt):
            if not ffmpeg_utils.is_available():
                raise ffmpeg_utils.FFmpegUnavailableError(requested_fmt)
            logger.info(f"[Package] ffmpeg 探测通过 ({ffmpeg_utils.probe()['source']}): "
                        f"{ffmpeg_utils.resolve_ffmpeg()}")

        if requested_fmt == "wav":
            _write_wav(all_audio, sr, output_path)
        elif requested_fmt == "flac":
            _write_flac(all_audio, sr, output_path)
        elif requested_fmt == "mp3":
            _write_mp3_via_ffmpeg(all_audio, sr, output_path)
        else:  # m4b
            segments_for_m4b = _m4b_segments(audio_arrays, sr, gain=norm_gain)
            metadata = {
                "title": context.get("book_title") or "Unknown Book",
                "author": context.get("book_author") or "Unknown Author",
                "album": context.get("book_title") or "Unknown Book",
                "genre": "Audiobook",
                "year": str(context.get("book_year") or "2024"),
                "comment": "Generated by PeiYinYuan",
            }
            builder = M4BBuilder()
            builder.build(segments_for_m4b, metadata, output_path, sample_rate=sr,
                          gap_ms=M4B_CHAPTER_GAP_MS)
            # PY-126: 章节时间轴来自 builder（与 concat 列表同源），不再全为 0
            chapters = builder.last_chapters
            context["m4b_segments"] = len(segments_for_m4b)
            logger.info(f"[Package] M4B 章节时间轴 {len(chapters)} 段: "
                        + ", ".join(f"[{i}]{c['title'][:12]}"
                                    f" {c['start_ms'] / 1000:.2f}-{c['end_ms'] / 1000:.2f}s"
                                    for i, c in enumerate(chapters[:5])))

        format_used = Path(output_path).suffix.lstrip(".").lower() or requested_fmt
        if format_used != requested_fmt:
            format_note = (f"实际写出 .{format_used}（请求 .{requested_fmt}）")
            logger.warning(f"[Package] {format_note} (PY-124)")

        file_size = os.path.getsize(output_path)
        logger.info(f"[Conversion:{context.get('task_id')}] Package stage: "
                    f"saved to {output_path} ({file_size} bytes, "
                    f"requested={requested_fmt}, format_used={format_used}, "
                    f"duration={total_duration_ms/1000:.1f}s)")
        context["format_used"] = format_used
        context["requested_format"] = requested_fmt
        result = {
            "output_path": output_path,
            "output_size": file_size,
            "total_segments": len(audio_arrays),
            "total_duration_ms": total_duration_ms,
            # PY-124: 实际格式与请求格式都回传，任何不一致都必须显式可见
            "requested_format": requested_fmt,
            "format_used": format_used,
            "format_note": format_note,
            "normalize_gain": norm_gain,
            "peak_after_normalize": post_norm_peak,
            "ffmpeg": ffmpeg_utils.probe(),
        }
        if chapters:
            result["chapters"] = chapters
            result["chapter_count"] = len(chapters)
            result["chapter_timeline_ms"] = [
                {"title": c["title"], "start_ms": round(c["start_ms"]),
                 "end_ms": round(c["end_ms"])} for c in chapters]
        return result


# ============================================================================
# 内部辅助：音频文件写入
# ============================================================================

def _quantize_int16(audio: "np.ndarray") -> "np.ndarray":
    """float32 [-1,1] -> int16 定量。

    PY-128: 归一化只在 `_stage_package` 里做一次（目标 TARGET_PEAK = -1 dBFS）。
    这里的 writer 们过去各自再 `peak/0.99` 一次，导致：
      1) 成品峰值恒为 0.99 (-0.09 dBFS)，零余量，任何重编码/倍速都可能削波；
      2) m4b 的每段 WAV 被独立顶格，段间相对响度（引擎本来的句子动态）被抹平。
    现在只在"真的超出安全峰值"时兜底压低，否则原样定量。
    """
    import numpy as np

    arr = np.asarray(audio, dtype=np.float32)
    if arr.size == 0:
        return arr.astype(np.int16)
    peak = float(np.max(np.abs(arr)))
    if peak > TARGET_PEAK:
        arr = arr * np.float32(TARGET_PEAK / peak)
    return (np.clip(arr, -1.0, 1.0) * 32767.0).astype(np.int16)


def _write_wav(audio: "np.ndarray", sample_rate: int, path: str) -> None:
    """写入 16-bit PCM WAV，优先使用 soundfile，回退使用 wave 模块。"""
    # PY-128: 不再二次归一化，只做定量
    int16 = _quantize_int16(audio)

    try:
        import soundfile
        soundfile.write(path, int16, sample_rate, subtype="PCM_16")
        return
    except ImportError:
        logger.info("[audio] soundfile 未安装，使用标准库 wave 写入 WAV")

    # 纯 Python 回退
    import wave
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(int16.tobytes())


def _write_flac(audio: "np.ndarray", sample_rate: int, path: str) -> None:
    """写入 FLAC（需要 soundfile + libsndfile）。"""
    try:
        import soundfile
    except ImportError as e:
        raise RuntimeError(
            "FLAC 输出需要 soundfile 库: pip install soundfile (或升级 libsndfile)"
        ) from e

    # PY-128: 归一化已在 _stage_package 完成一次，这里只定量
    int16 = _quantize_int16(audio)
    soundfile.write(path, int16, sample_rate, subtype="PCM_16", format="FLAC")


def _write_mp3_via_ffmpeg(audio: "np.ndarray", sample_rate: int, path: str) -> None:
    """通过 ffmpeg 将 PCM 编码为 MP3。

    PY-127: 旧实现不探测 ffmpeg，直接 `subprocess.run(["ffmpeg", ...])`，
    环境缺 ffmpeg 时抛裸 FileNotFoundError(WinError 2)，上层完全无法分辨原因。
    现在走 ffmpeg_utils.run_ffmpeg：缺失 -> FFmpegUnavailableError（含修复建议），
    失败 -> RuntimeError（含命令与 stderr 片段）。
    """
    import tempfile
    from pathlib import Path
    from modules.tts_engine import ffmpeg_utils

    # PY-128: 只定量，不再二次顶格归一
    int16 = _quantize_int16(audio)

    with tempfile.TemporaryDirectory(prefix="pyy_mp3_") as tmp:
        pcm_path = Path(tmp) / "input.pcm"
        pcm_path.write_bytes(int16.tobytes())

        args = [
            "-y",
            "-f", "s16le", "-ar", str(sample_rate), "-ac", "1",
            "-i", str(pcm_path),
            "-codec:a", "libmp3lame", "-b:a", "192k",
            path,
        ]
        ffmpeg_utils.run_ffmpeg(args, desc="MP3 编码", want_format="mp3")


def _seg_to_wav_bytes(audio: "np.ndarray", sample_rate: int) -> bytes:
    """将 numpy 音频段编码为 WAV bytes（供给 m4b_builder 做进一步的 ffmpeg 合并）。"""
    import io
    # PY-128: 段级不再各自顶格归一（否则 m4b 里每句都被拉到同一峰值，
    # 段间相对响度信息全丢）；统一沿用 _stage_package 的全局增益。
    int16 = _quantize_int16(audio)
    try:
        import soundfile
        buf = io.BytesIO()
        soundfile.write(buf, int16, sample_rate, subtype="PCM_16", format="WAV")
        return buf.getvalue()
    except ImportError:
        # 没有 soundfile 时，写入简易 WAV 头
        import struct
        data = int16.tobytes()

        byte_rate = sample_rate * 2
        block_align = 2
        data_size = len(data)
        riff_size = 36 + data_size

        header = b"".join([
            b"RIFF", struct.pack("<I", riff_size), b"WAVE", b"fmt ",
            struct.pack("<I", 16),       # PCM chunk size
            struct.pack("<H", 1),        # AudioFormat = PCM
            struct.pack("<H", 1),        # NumChannels = 1
            struct.pack("<I", sample_rate),
            struct.pack("<I", byte_rate),
            struct.pack("<H", block_align),
            struct.pack("<H", 16),       # BitsPerSample = 16
            b"data", struct.pack("<I", data_size),
        ])
        return header + data


def _m4b_segments(audio_arrays: list, sample_rate: int, gain: float = 1.0) -> list:
    """构造 M4BBuilder 需要的段字典契约 (PY-125/PY-126)。

    旧实现传给 builder 的是 `{"title","start_time","duration"}` 且没有任何音频数据，
    而 builder 读的是 `audio_data`(WAV bytes) / `chapter_title` / `duration_ms`
    —— 键名三重不匹配 => M4B 必然构建失败 => 被静默降级为 mp3/wav。

    这里同时解决章节粒度问题：生产的 audio_arrays 是"句子"级，逐句开章节会把
    一章切成几百个标记，所以只在每个章节的首句上打标题，其余留空 (PY-126)。

    Returns:
        [{"audio_data": bytes, "chapter_title": str, "duration_ms": float,
          "index": int, "chapter_start": bool}]
    """
    import numpy as np
    from modules.tts_engine.audio_postprocess import AudioPostProcessor

    sr = int(sample_rate)
    processor = AudioPostProcessor(sample_rate=sr)
    segs = [s for s in (audio_arrays or [])
            if s.get("audio") is not None and len(s.get("audio")) > 0]

    out = []
    prev_key = object()
    for i, seg in enumerate(segs):
        audio = np.asarray(seg["audio"], dtype=np.float32)
        if gain != 1.0:
            audio = audio * np.float32(gain)
        # 与 wav/mp3 分支一致：整体淡入只作用在成品首段，淡出只作用在末段
        if i == 0:
            audio = processor.apply_fade(audio, fade_in_ms=100, fade_out_ms=0)
        if i == len(segs) - 1:
            audio = processor.apply_fade(audio, fade_in_ms=0, fade_out_ms=500)

        title = str(seg.get("chapter_title") or seg.get("title") or "").strip()
        chapter_key = seg.get("chapter_id") or title or f"idx-{seg.get('index', i)}"
        is_chapter_start = chapter_key != prev_key
        prev_key = chapter_key

        out.append({
            "audio_data": _seg_to_wav_bytes(audio, sr),
            "chapter_title": title if is_chapter_start else "",
            "duration_ms": len(audio) / sr * 1000.0,
            "index": i,
            "chapter_start": is_chapter_start,
            "peak": float(np.max(np.abs(audio))) if audio.size else 0.0,
        })
    return out


# Global singleton
conversion_workflow = ConversionWorkflow()