"""
cast 后台任务编排服务（多人有声书 批次 B）

设计依据: docs/superpowers/specs/2026-09-20-multi-cast-audiobook-design.md 2/4/5/6

职责（spec 5 数据流第一段）:
    导入书籍 -> [cast 任务] 逐章抽取(modules/cast_analyzer) -> LLM 画像(fallback 兜底)
             -> 写 Role 表(属性进 extra_data, 旁白唯一) + project.extra_data.cast(别名表)

关键裁定:
- 走真 task 系统: 任务行 = ConversionTask(settings.task_type='cast_analysis')，
  状态机/暂停/取消复用 backend/services/task_service.py 的建法。
- WS 进度沿用 ProgressTracker: 把 tracker 注册进 conversion_workflow._active_trackers，
  于是既有 GET/WS /api/tasks/{id}/progress 一条代码都不用改就能读到人/写到人。
- 流式逐章: 一次只在内存里持有一章文本（红线: 不卡顿），清洗也在章级完成。
- LLM 不可达 -> CastProfiler 整表走规则兜底，任务不失败（spec 4.1/11）。

本文件不 import modules/workflow/conversion_workflow 的私有实现细节，
只在运行时按属性存取 tracker 注册表（缺失时降级为 DB 轮询进度）。
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
import uuid
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from backend.models.base import get_session
from backend.models.book import Chapter
from backend.models.project import DubbingProject, Role
from backend.models.voice import VoiceProfile
from backend.services.task_service import task_service

logger = logging.getLogger(__name__)

TASK_TYPE = "cast_analysis"
CAST_SOURCE = "cast_analysis"          # Role.extra_data.source：标识自动生成的角色
NARRATOR_NAMES = ("旁白", "narrator")
SUGGEST_TOP_N = 5
AUTO_ASSIGN_MIN_SCORE = 40
# CAST-FIX: 角色表落库规模的**护栏**（不是降噪手段）。降噪全部由抽取端逐条判据负责
# （姓名形态正向判定 / 非回溯证据门限 / 语料常见词说话位占比 / 戏份显著度下限），
# 真凶案例（155 万字《谍影重重》）抽取端收敛到 62 条，本护栏不生效（truncated=0）。
# 它只在极端文本下防止 Role 表无限膨胀（每多一条 Role 就多一次音色绑定与一屏前端噪声）。
# 环境变量 PYY_CAST_MAX_ROLES 可覆盖；<=0 表示不限制。
CAST_MAX_ROLES_ENV = "PYY_CAST_MAX_ROLES"
DEFAULT_MAX_PERSISTED_ROLES = 100

# 角色规模的**调参旋钮**（真正的降噪在这里，而不是 top-N 截断）：
# min_line_share = 一个说话人要占全书台词行的多大比例才够"戏份显著"。
# 默认 0.002（千分之二），与 CastExtractor 自身的默认值一致：
# 《谍影重重》25,012 台词行 → 下限 51 行，抽出的具名说话人从 327 候选收敛到 62。
# 想要更少的角色就调大（如 0.005），想全召回就调小或设 0 关闭该门限。
CAST_MIN_LINE_SHARE_ENV = "PYY_CAST_MIN_LINE_SHARE"
DEFAULT_MIN_LINE_SHARE = 0.002


def _env_int(name: str, default: int) -> int:
    try:
        return int(str(os.environ.get(name) or "").strip() or default)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(str(os.environ.get(name) or "").strip() or default)
    except ValueError:
        return default


MAX_PERSISTED_ROLES = _env_int(CAST_MAX_ROLES_ENV, DEFAULT_MAX_PERSISTED_ROLES)
# model_path 可能指向模型目录/权重，只有音频后缀才算真人声样本
AUDIO_SUFFIXES = (".wav", ".mp3", ".flac", ".m4a", ".ogg", ".aiff", ".aac")
# 小于 1KB 的"样本"（空文件/占位）不算真实人声
MIN_SAMPLE_BYTES = 1024


# ============================================================ 纯函数小工具
def norm_name(name: str) -> str:
    """角色名归一（speaker -> 名表 key）：去空白/引号残留/大小写。与 P6 PY-113 同形。"""
    s = str(name or "").strip()
    for ch in "\"'“”『「』』 \t\u3000":
        s = s.replace(ch, "")
    return s.lower()


def _candidate_name(cand) -> str:
    """角色候选取名：兼容 CastCandidate 对象与 dict 两种形状。"""
    if isinstance(cand, dict):
        return str(cand.get("name") or "")
    return str(getattr(cand, "name", "") or "")


def build_alias_table(candidates: List[Dict]) -> Dict[str, str]:
    """规范名 + 别名 -> 规范名的扁平表（spec 3: P5/P6 speaker 归一化用）。"""
    table: Dict[str, str] = {}
    for cand in candidates or []:
        canonical = str(cand.get("name") or "").strip()
        if not canonical:
            continue
        table[norm_name(canonical)] = canonical
        for alias in cand.get("aliases") or []:
            alias = str(alias or "").strip()
            if alias and norm_name(alias) not in table:
                table[norm_name(alias)] = canonical
    return table


def is_narrator_role(role: Dict) -> bool:
    """唯一旁白的判定：role_type=narrator 或名字是 旁白/narrator。"""
    if not isinstance(role, dict):
        return False
    if (role.get("role_type") or "").lower() == "narrator":
        return True
    return norm_name(role.get("name", "")) in NARRATOR_NAMES


def merge_narrator(roles: List[Dict]) -> List[Dict]:
    """
    保证"旁白唯一"（spec 4.1 输出含旁白固定项 + 角色表落库约束）。

    多条 narrator 时保留一条（优先已绑音色的那条），其余降级为 supporting；
    不重排调用方给的角色顺序（前端列表顺序 = DB 顺序）。纯函数，便于单测。
    """
    items = [dict(r) for r in (roles or [])]
    narrator_idx = [i for i, r in enumerate(items) if is_narrator_role(r)]
    if not narrator_idx:
        return items
    keeper = next((i for i in narrator_idx if items[i].get("voice_id")), narrator_idx[0])
    for i in narrator_idx:
        if i == keeper:
            items[i]["role_type"] = "narrator"
            continue
        items[i]["role_type"] = "supporting"
        items[i]["narrator_merged_from"] = "narrator"
    return items


def profile_llm_status(pstats: Dict) -> Tuple[str, Optional[str]]:
    """profiler.stats() -> (画像来源标签, 兜底原因)。

    落库的 meta 里必须放**标量**：这里曾直接塞整个 profiler.stats()，前端
    `stats.llm === "llm"` 永假，真用 LLM 跑出来的画像也报成"规则画像"（M-4 家族）。
    """
    if pstats.get("llm_ready"):
        return "llm", None
    return "rules", pstats.get("last_error") or "LLM Sidecar 未就绪"


def role_lines_count(role: Dict) -> int:
    """角色字典的戏份行数（Role.extra_data.cast.lines_count）。"""
    try:
        return int(((role or {}).get("cast") or {}).get("lines_count") or 0)
    except (TypeError, ValueError):
        return 0


def role_direct_hints(role: Dict) -> int:
    """非回溯归属证据数（引号直连提示 + 最近提及）；旧数据无此字段时记 0。"""
    try:
        return int(((role or {}).get("cast") or {}).get("direct_hints") or 0)
    except (TypeError, ValueError):
        return 0


def limit_role_dicts(roles: List[Dict], cap: Optional[int] = None) -> Tuple[List[Dict], int]:
    """
    CAST-FIX: 落库前按戏份截断角色表，返回 (保留列表, 被裁条数)。

    口径：
    - 排序键 = 戏份降序 -> 归属证据数降序 -> 名字，与前端展示顺序一致；
    - 旁白强制保留（有声书没有旁白直接不可用），不占排序位；
    - cap<=0 或行数 <= cap 时原样返回，dropped=0（默认不改变任何既有行为）；
    - 纯函数，不碰 DB，便于单测与 harness 复算。
    """
    items = [r for r in (roles or []) if isinstance(r, dict)]
    cap = MAX_PERSISTED_ROLES if cap is None else int(cap)

    def rank(role: Dict) -> Tuple[int, int, str]:
        return (-role_lines_count(role), -role_direct_hints(role),
                str(role.get("name") or ""))

    if cap <= 0 or len(items) <= cap:
        return items, 0

    ranked = sorted(items, key=rank)
    narrator = next((r for r in ranked if is_narrator_role(r)), None)
    kept: List[Dict] = [narrator] if narrator is not None else []
    picked = {id(narrator)} if narrator is not None else set()
    for role in ranked:
        if len(kept) >= cap:
            break
        if id(role) in picked:
            continue
        kept.append(role)
        picked.add(id(role))
    kept.sort(key=rank)
    return kept, len(items) - len(kept)


class CastService:
    """cast 分析任务编排 + 角色表/建议/确认/混音设置读写。"""

    def __init__(self):
        self._cancelled: set = set()
        self._lock = threading.Lock()

    # ---------------------------------------------------------- 任务生命周期
    def start_analysis(self, project_id: str, book_id: Optional[str] = None,
                       use_llm: Optional[bool] = None,
                       language: Optional[str] = None) -> Dict:
        """
        建 cast 任务行（真 task 系统）并返回 task 字典。
        真正的执行由 API 层 asyncio.create_task(run_analysis(...)) 驱动，
        与 /api/tasks/convert 的建法保持一致。
        """
        project = self._load_project(project_id)
        if not project:
            raise ValueError(f"项目不存在: {project_id}")
        book_id = book_id or project.get("book_id")
        if not book_id:
            raise ValueError("项目未关联书籍，无法分析角色")

        settings = {
            "task_type": TASK_TYPE,
            "project_id": project_id,
            "use_llm": (not _env_false("PYY_CAST_USE_LLM")) if use_llm is None else bool(use_llm),
            "language": language or project.get("language") or "zh",
        }
        task = task_service.create_task(project_id=project_id, book_id=book_id,
                                        output_format="cast", settings=settings)
        # 立即置 running，避免前端在首个进度事件前看到 queued
        task_service.update_progress(task["id"], 0.0, "cast_prepare", 0)
        with self._lock:
            self._cancelled.discard(task["id"])
        logger.info(f"[Cast] 任务创建 task={task['id']} project={project_id} book={book_id}")
        return {"task": task, "project": project}

    async def run_analysis(self, task_id: str, project_id: str,
                           book_id: str, use_llm: bool = True) -> Dict:
        """
        后台协程：逐章抽取 -> 画像 -> 落库，全程推进度。
        重活（正则/统计/CPU）放 asyncio.to_thread，事件循环不被占住（红线: 不卡顿）。
        """
        tracker = _register_tracker(task_id)
        loop = asyncio.get_running_loop()

        async def areport(frac: float, stage: str, chars: int, total: int,
                          status: str = "", error: str = "") -> None:
            """
            事件循环内的进度推进（tracker + DB 双写，WS 与 HTTP 轮询都吃这个）。

            PY-331: 顺序必须是**先落 DB、再发 tracker**。tracker 在时
            /tasks/{id}/progress 读内存、而任务列表/详情读 DB 行，旧顺序等于先宣布
            "已完成"再写库，中间隔着一次 to_thread + sqlite 提交；C4 的
            「DB 状态同步为 completed」实测抓到轮询到 completed 后立刻读库仍是
            running（三次复跑出现一次）。落库失败也必须继续发 tracker，否则前端
            进度会永久卡住（红线 1 的可见性）。
            """
            frac = max(0.0, min(float(frac), 1.0))
            try:
                await asyncio.to_thread(_persist_progress, task_id, frac, stage, chars,
                                        total, status)
            except Exception as exc:                # pragma: no cover - 防御
                logger.warning(f"[Cast:{task_id}] 进度落库失败: {exc}")
            if tracker is not None:
                try:
                    await tracker.update(frac, stage, chars, total, status=status, error=error)
                except Exception as exc:            # pragma: no cover - 防御
                    logger.warning(f"[Cast:{task_id}] tracker.update 失败: {exc}")

        last_persist = {"frac": -1.0}

        def report(frac: float, stage: str, chars: int, total: int) -> None:
            """
            工作线程 -> 进度：tracker 更新用 run_coroutine_threadsafe 转发回事件循环，
            DB 落库在本线程直写（按 >=1% 增量节流，长书下不产生每章一次写盘）。
            PY-331: 与 areport 同一顺序——先写库再转 tracker。
            """
            frac = max(0.0, min(float(frac), 1.0))
            if frac - last_persist["frac"] >= 0.01:
                last_persist["frac"] = frac
                try:
                    _persist_progress(task_id, frac, stage, chars, total)
                except Exception as exc:            # pragma: no cover - 防御
                    logger.warning(f"[Cast:{task_id}] 进度落库失败: {exc}")
            if tracker is not None and not loop.is_closed():
                try:
                    asyncio.run_coroutine_threadsafe(
                        tracker.update(frac, stage, chars, total), loop)
                except Exception as exc:            # pragma: no cover - 防御
                    logger.debug(f"[Cast:{task_id}] 进度转发失败: {exc}")

        def cancel_requested() -> bool:
            with self._lock:
                if task_id in self._cancelled:
                    return True
            try:
                snap = task_service.get_task(task_id)
            except Exception:
                return False
            return bool(snap) and snap.get("status") == "cancelled"

        t0 = time.perf_counter()
        await areport(0.0, "cast_extract", 0, 0, status="running")
        try:
            result = await asyncio.to_thread(
                self._analyze_sync, project_id, book_id, use_llm, report, cancel_requested)
        except _Cancelled as exc:
            elapsed = time.perf_counter() - t0
            await areport(1.0, "cast_cancelled", 0, 0, status="cancelled", error=str(exc))
            _safe_fail_task(task_id, str(exc))
            _unregister_tracker(task_id)
            return {"status": "cancelled", "error": str(exc), "elapsed_s": round(elapsed, 3)}
        except Exception as exc:
            elapsed = time.perf_counter() - t0
            logger.exception(f"[Cast:{task_id}] 分析失败: {exc}")
            await areport(1.0, "cast_failed", 0, 0, status="failed", error=str(exc))
            _safe_fail_task(task_id, f"{type(exc).__name__}: {exc}")
            _unregister_tracker(task_id)
            return {"status": "failed", "error": str(exc), "elapsed_s": round(elapsed, 3)}

        elapsed = time.perf_counter() - t0
        result["elapsed_s"] = round(elapsed, 3)
        await areport(1.0, "cast_done", result.get("chars", 0),
                      result.get("chars", 0), status="completed")
        try:
            await asyncio.to_thread(task_service.complete_task, task_id, "")
        except Exception as exc:                    # pragma: no cover - 防御
            logger.warning(f"[Cast:{task_id}] complete_task 失败: {exc}")
        _unregister_tracker(task_id)
        logger.info(f"[Cast:{task_id}] 完成 roles={result.get('role_count')} "
                    f"chars={result.get('chars')} elapsed={elapsed:.2f}s "
                    f"llm={result.get('llm')}")
        return {"status": "completed", **result}

    def cancel(self, task_id: str) -> bool:
        with self._lock:
            self._cancelled.add(task_id)
        return True

    # ---------------------------------------------------------- 分析主体（同步/线程内）
    def _analyze_sync(self, project_id: str, book_id: str, use_llm: bool,
                      report, cancel_requested) -> Dict:
        """
        逐章抽取 -> 画像 -> 写 Role 表 + project.extra_data.cast。
        report(frac, stage, chars, total) 为线程安全投递（由协程侧包装）。
        """
        from modules.book_parser.text_cleaner import TextCleaner
        from modules.cast_analyzer import (
            CastExtractor, CastProfiler, profiles_to_role_extra_data, auto_assign)

        chapters = self._iter_chapter_texts(book_id)
        total_chars = sum(len(t) for _i, t in chapters)
        if not chapters:
            raise ValueError(f"书籍 {book_id} 没有可分析的正文章节")

        cleaner = TextCleaner()
        extractor = CastExtractor(
            min_line_share=_env_float(CAST_MIN_LINE_SHARE_ENV, DEFAULT_MIN_LINE_SHARE))
        seen = 0
        for idx, raw in chapters:
            if cancel_requested():
                raise _Cancelled("cast 任务已取消")
            text = clean_chapter_text(cleaner, raw)
            extractor.process_chapter(text, idx)
            seen += len(text)
            del text
            frac = 0.05 + 0.6 * (seen / total_chars if total_chars else 1.0)
            _emit(report, frac, "cast_extract", seen, total_chars)
        del chapters

        candidates = extractor.finalize()
        _emit(report, 0.68, "cast_profile", seen, total_chars)
        profiler = CastProfiler(llm_service=_make_llm(use_llm))
        candidates = profiler.profile(candidates)
        _emit(report, 0.78, "cast_profile", seen, total_chars)

        role_dicts = profiles_to_role_extra_data(candidates)
        # CAST-FIX: 先按戏份截断再落库/推荐，保证 Role 表规模可控且
        # len(db_roles) == len(role_dicts)（C4 断言），被裁的角色不会拿到音色。
        role_dicts, roles_truncated = limit_role_dicts(role_dicts, MAX_PERSISTED_ROLES)
        kept_names = {norm_name(r.get("name", "")) for r in role_dicts}
        kept_candidates = [c for c in candidates
                           if norm_name(_candidate_name(c)) in kept_names]
        suggestions_by_name = auto_assign(kept_candidates, self._voice_dicts(),
                                          min_score=AUTO_ASSIGN_MIN_SCORE,
                                          taken=self._manual_voice_ids(project_id))
        _emit(report, 0.86, "cast_persist", seen, total_chars)
        stats = extractor.stats()
        pstats = profiler.stats()
        llm_label, llm_reason = profile_llm_status(pstats)
        if roles_truncated:
            logger.info("[Cast] 角色表按戏份截断: 超出 %d 条未落库（上限 %d）",
                        roles_truncated, MAX_PERSISTED_ROLES)
        saved = self._persist_cast(project_id, role_dicts, suggestions_by_name,
                                   {**stats, "llm": llm_label, "llm_reason": llm_reason,
                                    "llm_stats": pstats, "chars": total_chars,
                                    "roles_truncated": roles_truncated,
                                    "max_persisted_roles": MAX_PERSISTED_ROLES})
        _emit(report, 0.97, "cast_persist", seen, total_chars)
        return {
            "roles": saved["roles"],
            "role_count": saved["role_count"],
            "role_dict_count": len(role_dicts),
            "narrator": saved["narrator"],
            "chars": total_chars,
            "stats": saved["cast_meta"],
            "extractor": stats,
            "profiler": pstats,
            "llm": llm_label,
        }

    # ---------------------------------------------------------- 数据读写
    def _iter_chapter_texts(self, book_id: str) -> List[Tuple[int, str]]:
        """[(index, content)]，按章号升序；直接读 DB 以避开 to_dict 的 preview 截断语义。"""
        session = get_session()
        try:
            rows = (session.query(Chapter)
                    .filter_by(book_id=book_id)
                    .order_by(Chapter.index)
                    .all())
            return [(int(r.index or i), r.content or "") for i, r in enumerate(rows)]
        finally:
            session.close()

    @staticmethod
    def _voice_dicts(language: Optional[str] = None,
                     require_sample: bool = True) -> List[Dict]:
        """
        VoiceProfile -> voice_matcher 可吃的字典列表。

        C4-2（红线 8「人声可用」，本轮最小方案 = 只改推荐池，不动引擎装载链路）:
        默认只返回"当前就能出声"的音色——盘上有真实可回放人声样本（见
        voice_is_usable）。builtin 5 条没有任何音频文件，VoxCPM 拿到它的 voice_id
        会退化成同一个零样本默认声（角色之间无区分度）；designed/cloned 无试听样本，
        /api/voices/{id}/preview 直接 404，用户在 App 内无法复核。这三类都不得
        进入推荐与自动绑定（手工在音色选择器里选仍然允许）。

        PY-330（红线 8「人声可应用」的同一条口子，另一类漏网）: 回放档
        engine='sample' 的音色**能**试听出 own 嗓子（所以 usable=True，C4 的试听
        口径不改动），但 SamplePlaybackEngine 不吃台词——该角色每一句都回放同一条
        样本录音。实测《谍影重重》重跑后「卡克特斯/法官」被分到
        「人声-儿童声 Child-implant-users-imitation-of-happy--and-」，即台词全变成
        同一句英文。故推荐池再加一道 `usable 且非回放档`（见 voice_is_replay_only）。
        手工仍可选（固定台词/音效用途是它的正业）。
        require_sample=False 时返回全量池并逐条标注 usable/replay_only，仅供
        诊断/前端置灰。本函数只做只读判定，绝不触发模型装载。
        """
        session = get_session()
        try:
            q = session.query(VoiceProfile)
            if language:
                q = q.filter((VoiceProfile.language == language) | (VoiceProfile.language.is_(None)))
            # VA-2: 台词匹配池只收人声类别（动物/电子/机械音效行不进推荐）；
            # category 为 NULL 的历史行按回填语义视为 human，保持兼容。
            q = q.filter((VoiceProfile.category == "human") | (VoiceProfile.category.is_(None)))
            rows = q.all()
            out = []
            for v in rows:
                d = v.to_dict()
                d["sample_audio_path"] = v.sample_audio_path
                d["reference_audio_path"] = v.reference_audio_path
                d["model_path"] = v.model_path
                d["description"] = v.description
                d["usable"] = voice_is_usable(d)
                d["replay_only"] = voice_is_replay_only(d)
                out.append(d)
            if require_sample:
                kept = [d for d in out if d["usable"] and not d["replay_only"]]
                if len(kept) != len(out):
                    logger.info("[Cast] 音色池按可用性过滤: %d -> %d（剔除无样本 %d / 回放档 %d）",
                                len(out), len(kept),
                                sum(1 for d in out if not d["usable"]),
                                sum(1 for d in out if d["usable"] and d["replay_only"]))
                return kept
            return out
        finally:
            session.close()

    @staticmethod
    def _manual_voice_ids(project_id: str) -> set:
        """用户在本项目手工绑过的音色 id。重跑分析时先扣掉，别把它们分给别的角色。"""
        session = get_session()
        try:
            return {r.voice_id for r in session.query(Role).filter(
                Role.project_id == project_id).all()
                if r.voice_id and (r.extra_data or {}).get("voice_source") == "manual"}
        finally:
            session.close()

    def _load_project(self, project_id: str) -> Optional[Dict]:
        session = get_session()
        try:
            project = session.query(DubbingProject).filter_by(id=project_id).first()
            if not project:
                return None
            book = project.book_id
            language = None
            if book:
                from backend.models.book import Book
                row = session.query(Book).filter_by(id=book).first()
                language = row.language if row else None
            data = project.to_dict()
            data["extra_data"] = project.extra_data or {}
            data["roles"] = [r.to_dict_full() if hasattr(r, "to_dict_full")
                             else _role_dict(r) for r in (project.roles or [])]
            data["assignments"] = [a.to_dict() for a in (project.assignments or [])]
            data["language"] = language or "zh"
            return data
        finally:
            session.close()

    def _persist_cast(self, project_id: str, role_dicts: List[Dict],
                      suggested: Dict[str, Optional[str]], meta: Dict) -> Dict:
        """写 Role 表（属性进 extra_data，旁白唯一）+ project.extra_data.cast。"""
        session = get_session()
        try:
            project = session.query(DubbingProject).filter_by(id=project_id).first()
            if not project:
                raise ValueError(f"项目不存在: {project_id}")

            now = datetime.utcnow().isoformat()
            kept_ids: List[str] = []
            roles_out: List[Dict] = []
            existing = {norm_name(r.name): r for r in (project.roles or [])}

            for item in role_dicts:
                name = str(item.get("name") or "").strip()
                if not name:
                    continue
                key = norm_name(name)
                role = existing.get(key)
                prev_extra = dict(role.extra_data or {}) if role is not None else {}
                voice_id = suggested.get(name)
                # 用户手工绑过音色 -> 重跑不得改走推荐结果（推荐只是建议）
                if prev_extra.get("voice_source") == "manual" and (role is not None
                                                                   and role.voice_id):
                    voice_id = None
                payload = dict(item)
                payload["source"] = CAST_SOURCE
                payload["confirmed"] = False
                payload["analyzed_at"] = now
                # ---- C4-4: 与既有 extra_data 合并，保住用户在此之前的手工成果 ----
                if prev_extra.get("confirmed"):
                    payload["confirmed"] = True
                    if prev_extra.get("confirmed_at"):
                        payload["confirmed_at"] = prev_extra["confirmed_at"]
                if prev_extra.get("voice_source"):
                    payload["voice_source"] = prev_extra["voice_source"]
                prev_cast = dict(prev_extra.get("cast") or {})
                if prev_cast.get("profile_source") == "manual":
                    fresh_cast = dict(payload.get("cast") or {})
                    for k in ("gender", "age_group", "identity", "personality_tags"):
                        if prev_cast.get(k) is not None:
                            fresh_cast[k] = prev_cast[k]
                    fresh_cast["profile_source"] = "manual"
                    payload["cast"] = fresh_cast
                merged_aliases: List[str] = []
                for a in (list(prev_extra.get("aliases") or [])
                         + list(payload.get("aliases") or [])):
                    a = str(a or "").strip()
                    if a and a not in merged_aliases:
                        merged_aliases.append(a)
                payload["aliases"] = merged_aliases
                if prev_extra.get("mix"):
                    payload["mix"] = prev_extra["mix"]     # 角色局部混音是用户配置，非分析产物
                if role is None:
                    role = Role(id=str(uuid.uuid4()), project_id=project_id, name=name)
                    session.add(role)
                role.name = name
                role.role_type = _role_type_for(payload)
                if voice_id:
                    role.voice_id = voice_id
                    payload["voice_source"] = "suggested"
                elif role.voice_id:
                    payload["voice_source"] = "manual" if (
                        prev_extra.get("voice_source") == "manual") else "kept"
                else:
                    payload["voice_source"] = "none"
                prev_extra.pop("stale", None)
                role.extra_data = payload
                session.flush()
                kept_ids.append(role.id)
                roles_out.append(_role_dict(role))

            # 上一次自动分析产生、本次不再出现的角色 -> 删除（用户手工角色不动）
            removed = 0
            for role in list(project.roles or []):
                if role.id in kept_ids:
                    continue
                extra = role.extra_data or {}
                if extra.get("source") == CAST_SOURCE and not extra.get("confirmed"):
                    session.delete(role)
                    removed += 1
            session.flush()

            cast_meta = {
                "status": "analyzed",
                "analyzed_at": now,
                "aliases": build_alias_table(role_dicts),
                "role_names": [r["name"] for r in roles_out],
                "narrator": next((r["name"] for r in roles_out if is_narrator_role(r)), None),
                "stats": {k: v for k, v in meta.items() if v is not None},
                "confirmed": False,
                "removed_stale": removed,
                "updated_at": now,
            }
            extra_data = dict(project.extra_data or {})
            extra_data["cast"] = cast_meta
            project.extra_data = extra_data
            settings = dict(project.global_settings or {})
            project.global_settings = settings
            session.commit()

            return {"roles": roles_out, "role_count": len(roles_out),
                    "role_dict_count": len(role_dicts),
                    "narrator": cast_meta["narrator"],
                    "cast_meta": cast_meta}
        finally:
            session.close()

    # ---------------------------------------------------------- 对外查询
    def get_cast(self, project_id: str) -> Optional[Dict]:
        """角色表（含属性、aliases、样句、计数）+ 分析元信息。"""
        project = self._load_project(project_id)
        if not project:
            return None
        cast_meta = (project.get("extra_data") or {}).get("cast") or {}
        roles = [r for r in project.get("roles", []) if not (r.get("extra_data") or {}).get("stale")]
        roles = merge_narrator(roles)
        return {
            "project_id": project_id,
            "mode": project.get("mode"),
            "book_id": project.get("book_id"),
            "analyzed": bool(cast_meta.get("analyzed_at")),
            "confirmed": bool(cast_meta.get("confirmed")),
            "narrator": cast_meta.get("narrator") or _pick_narrator_name(roles),
            "cast": cast_meta,
            "roles": roles,
            "role_count": len(roles),
            "character_count": sum(1 for r in roles if not is_narrator_role(r)),
            "gaps": (project.get("global_settings") or {}).get("mix", {}).get("gaps", {}),
            "mix": (project.get("global_settings") or {}).get("mix") or {},
        }

    def get_suggestions(self, project_id: str, top_n: int = SUGGEST_TOP_N,
                        language: Optional[str] = None,
                        include_unusable: bool = False) -> Dict:
        """
        每角色 Top-N 推荐音色（含分数拆解），按当前 Role 属性重算，非缓存。

        C4-2: 默认只在"可应用"音色里排序（样本/参考在盘或语音设计音色）；
        include_unusable=true 时按全量池排序，但每条推荐都带 usable 标记，
        由前端置灰而非当作可选推荐。

        PY-330: 排序池同样剔回放档（engine='sample'）。两个计数分开报：
        voice_usable = 试听能把这枚音色自己的嗓子放出来（含回放档），
        voice_bindable = 其中能按台词合成、可进推荐/自动绑定的那部分。
        """
        from modules.cast_analyzer import suggest
        project = self._load_project(project_id)
        if not project:
            raise LookupError(f"项目不存在: {project_id}")
        candidates = self._cast_candidates(project)
        all_voices = self._voice_dicts(language, require_sample=False)
        usable_ids = {v.get("id") for v in all_voices if v.get("usable")}
        bindable_ids = {v.get("id") for v in all_voices
                        if v.get("usable") and not v.get("replay_only")}
        replay_ids = {v.get("id") for v in all_voices if v.get("replay_only")}
        voices = all_voices if include_unusable else [v for v in all_voices
                                                      if v.get("id") in bindable_ids]
        ranked = suggest(candidates, voices, top_n=top_n)
        out = []
        for cand in candidates:
            name = cand["name"]
            items = [dict(i, usable=(i.get("voice_id") in usable_ids),
                          replay_only=(i.get("voice_id") in replay_ids))
                     for i in ranked.get(name, [])[:top_n]]
            out.append({
                "role_name": name,
                "role_id": cand.get("role_id"),
                "is_narrator": bool(cand.get("is_narrator")),
                "attrs": cand.get("profile", {}),
                "suggestions": items,
            })
        return {"project_id": project_id, "top_n": top_n,
                "voice_total": len(all_voices), "voice_usable": len(usable_ids),
                "voice_bindable": len(bindable_ids),
                "pool_mode": "all" if include_unusable else "usable_only",
                "voice_total_pool_used": len(voices),
                "roles": out}

    def _cast_candidates(self, project: Dict) -> List[Dict]:
        """Role 行 -> voice_matcher 可吃的候选视图（画像在 extra_data.cast 下）。"""
        out: List[Dict] = []
        for role in merge_narrator(project.get("roles", []) or []):
            extra = role.get("extra_data") or {}
            cast = extra.get("cast") or {}
            profile = {
                "gender": cast.get("gender", "unknown"),
                "age_group": cast.get("age_group", "unknown"),
                "identity": cast.get("identity", "unknown"),
                "personality_tags": cast.get("personality_tags") or [],
                "language": project.get("language") or "zh",
            }
            out.append({
                "name": role.get("name"),
                "role_id": role.get("id"),
                "role_type": role.get("role_type"),
                "aliases": extra.get("aliases") or [],
                "profile": profile,
                "is_narrator": is_narrator_role(role),
                "lines_count": cast.get("lines_count", 0),
            })
        return out

    # ---------------------------------------------------------- 确认落库
    def confirm_cast(self, project_id: str, payload: Dict) -> Dict:
        """
        PUT /cast/confirm：写 Role（voice_id / 属性 / 别名 / 局部混音）
        + 别名表进 project.extra_data.cast（P5/P6 归一化读取处）。
        """
        payload = payload or {}
        wanted = payload.get("roles") or payload.get("assignments") or []
        auto_assign_rest = bool(payload.get("auto_assign_rest", True))
        project = self._load_project(project_id)
        if not project:
            raise LookupError(f"项目不存在: {project_id}")

        by_id = {r.get("id"): r for r in project.get("roles", []) if r.get("id")}
        by_name = {norm_name(r.get("name", "")): r for r in project.get("roles", [])}
        updates: List[Dict] = []
        for item in wanted:
            role = None
            if item.get("role_id"):
                role = by_id.get(item["role_id"])
            if role is None and item.get("name"):
                role = by_name.get(norm_name(item["name"]))
            if role is None:
                role = {"id": None, "name": item.get("name"),
                        "role_type": item.get("role_type") or "supporting",
                        "extra_data": {}}
            updates.append((role, item))

        if not updates:
            # 空 body = 一键确认现状（把推荐音色固化）
            updates = [(r, {}) for r in project.get("roles", [])]

        session = get_session()
        try:
            proj = session.query(DubbingProject).filter_by(id=project_id).first()
            if not proj:
                raise LookupError(f"项目不存在: {project_id}")
            live = {r.id: r for r in (proj.roles or [])}
            now = datetime.utcnow().isoformat()
            confirmed_rows: List[Dict] = []
            # PY-332: 这里原来是 `suggest(..., top_n=1)` —— 逐角色独立取 Top1，
            # 打分是纯函数，同画像角色算出完全相同的排序，于是"一键确认"把
            # PY-329 的塌缩在原样重演（分析路径已去重，确认路径没有）。
            # 现与 _analyze_sync 同用一个 auto_assign：跨角色去重 + min_score 门槛，
            # 且只给"用户这次没定音色"的角色分配，已定的那枚进 taken 不外借。
            name_by_id = {r.get("id"): r.get("name") for r in project.get("roles", [])}
            decided: Dict[str, str] = {}
            for role, item in updates:
                row = live.get(role.get("id"))
                vid = item.get("voice_id") or (row.voice_id if row else None)
                nm = norm_name(str(item.get("name") or role.get("name")
                                 or name_by_id.get(role.get("id") or "") or ""))
                if vid and nm:
                    decided[nm] = str(vid)

            suggested: Dict[str, Optional[str]] = {}
            if auto_assign_rest:
                from modules.cast_analyzer import auto_assign as _auto_assign
                pending = [c for c in self._cast_candidates(project)
                           if norm_name(str(c.get("name") or "")) not in decided]
                suggested = _auto_assign(pending, self._voice_dicts(),
                                         min_score=AUTO_ASSIGN_MIN_SCORE,
                                         taken=set(decided.values())
                                         | self._manual_voice_ids(project_id))

            for role, item in updates:
                row = live.get(role.get("id"))
                if row is None:
                    if not role.get("name"):
                        continue
                    row = Role(id=str(uuid.uuid4()), project_id=project_id,
                               name=str(role["name"]).strip())
                    session.add(row)
                    live[row.id] = row
                    session.flush()

                extra = dict(row.extra_data or {})
                cast = dict(extra.get("cast") or {})
                incoming_extra = item.get("extra_data") or {}
                incoming_cast = dict(incoming_extra.get("cast") or item.get("cast") or {})
                cast.update({k: v for k, v in incoming_cast.items() if v is not None})
                for k in ("gender", "age_group", "identity", "personality_tags"):
                    if item.get(k) is not None:
                        cast[k] = item[k]
                if "aliases" in item:
                    extra["aliases"] = [str(a).strip() for a in (item.get("aliases") or [])
                                        if str(a).strip()]
                extra["cast"] = cast
                extra["mix"] = item.get("mix") or extra.get("mix") or {"gain_db": 0.0,
                                                                       "speed": 1.0, "pitch": 0.0}
                extra["source"] = extra.get("source") or CAST_SOURCE
                extra["confirmed"] = True
                extra["confirmed_at"] = now

                voice_id = item.get("voice_id")
                if voice_id is not None:
                    extra["voice_source"] = "manual"    # 显式提交=手工选定，重跑不覆盖
                if voice_id is None and row.voice_id:
                    voice_id = row.voice_id
                if voice_id is None and suggested:
                    # PY-332: 分数不合格/池被占满时 auto_assign 给 None，
                    # 角色留在未绑定态由 UI 提示手选 —— 不硬塞不合格音色（PY-329 同口径）。
                    voice_id = suggested.get(row.name)
                    if voice_id:
                        extra["voice_source"] = "suggested"
                row.name = str(item.get("name") or row.name).strip()
                if item.get("role_type"):
                    row.role_type = item["role_type"]
                row.voice_id = voice_id or None
                row.extra_data = extra
                session.flush()
                confirmed_rows.append(_role_dict(row))

            role_dicts = []
            for row in live.values():
                extra = row.extra_data or {}
                role_dicts.append({
                    "name": row.name,
                    "aliases": extra.get("aliases") or [],
                    "role_type": row.role_type,
                })
            alias_table = build_alias_table(role_dicts)
            if proj.roles:
                for row in proj.roles:
                    extra = row.extra_data or {}
                    for alias in (extra.get("aliases") or []):
                        k = norm_name(alias)
                        if k and k not in alias_table:
                            alias_table[k] = row.name

            extra_data = dict(proj.extra_data or {})
            cast_meta = dict(extra_data.get("cast") or {})
            narrator_name = next((r["name"] for r in confirmed_rows if is_narrator_role(r)),
                                 cast_meta.get("narrator"))
            cast_meta.update({
                "status": "confirmed",
                "confirmed": True,
                "confirmed_at": now,
                "aliases": alias_table,
                "narrator": narrator_name,
                "role_names": [r["name"] for r in confirmed_rows],
                "speaker_map": {r["name"]: {"role_id": r["id"], "voice_id": r.get("voice_id")}
                                for r in confirmed_rows},
            })
            extra_data["cast"] = cast_meta
            proj.extra_data = extra_data
            if (proj.status or "draft") == "draft" and confirmed_rows:
                proj.status = "ready"
            session.commit()

            return {"project_id": project_id, "confirmed": True,
                    "roles": confirmed_rows, "role_count": len(confirmed_rows),
                    "aliases": alias_table, "narrator": narrator_name,
                    "cast": cast_meta}
        finally:
            session.close()

    # ---------------------------------------------------------- 混音设置
    def get_mix_settings(self, project_id: str) -> Optional[Dict]:
        project = self._load_project(project_id)
        if not project:
            return None
        settings = project.get("global_settings") or {}
        from modules.mixer import AudioMix
        return {
            "project_id": project_id,
            "mix": settings.get("mix") or {},
            "chapter_effective": AudioMix.settings_from_project(settings, 0),
            "passthrough": AudioMix.is_passthrough(settings.get("mix")),
        }

    def update_mix_settings(self, project_id: str, mix: Dict) -> Dict:
        from modules.mixer import AudioMix
        problems = AudioMix.validate_settings(mix if mix is not None else {})
        if problems:
            raise ValueError("; ".join(problems))
        session = get_session()
        try:
            proj = session.query(DubbingProject).filter_by(id=project_id).first()
            if not proj:
                raise LookupError(f"项目不存在: {project_id}")
            settings = dict(proj.global_settings or {})
            if mix is None:
                settings.pop("mix", None)
            else:
                settings["mix"] = mix
            proj.global_settings = settings
            session.commit()
            return {"project_id": project_id, "mix": mix or {},
                    "passthrough": AudioMix.is_passthrough(mix)}
        finally:
            session.close()

    # ---------------------------------------------------------- 角色属性编辑（可选）
    def update_role(self, project_id: str, role_id: str, patch: Dict) -> Optional[Dict]:
        """手工修正角色属性/别名/局部混音（spec 11: unknown 由 UI 标"待完善"）。"""
        session = get_session()
        try:
            row = session.query(Role).filter_by(id=role_id, project_id=project_id).first()
            if not row:
                return None
            extra = dict(row.extra_data or {})
            cast = dict(extra.get("cast") or {})
            for k in ("gender", "age_group", "identity"):
                if patch.get(k) is not None:
                    cast[k] = patch[k]
                    cast["profile_source"] = "manual"
            if patch.get("personality_tags") is not None:
                cast["personality_tags"] = list(patch["personality_tags"])
                cast["profile_source"] = "manual"
            if patch.get("aliases") is not None:
                extra["aliases"] = [str(a).strip() for a in patch["aliases"] if str(a).strip()]
            if patch.get("mix") is not None:
                extra["mix"] = patch["mix"]
            if patch.get("voice_id") is not None:
                row.voice_id = patch["voice_id"] or None
                extra["voice_source"] = "manual"
            if patch.get("name"):
                row.name = str(patch["name"]).strip()
            if patch.get("role_type"):
                row.role_type = patch["role_type"]
            extra["cast"] = cast
            row.extra_data = extra
            session.commit()
            return _role_dict(row)
        finally:
            session.close()

    def delete_role(self, project_id: str, role_id: str) -> bool:
        """删除项目角色（既有 DubbingPage 已在调 DELETE /roles/{id}，后端补齐）。"""
        session = get_session()
        try:
            row = session.query(Role).filter_by(id=role_id, project_id=project_id).first()
            if not row:
                return False
            if is_narrator_role({"role_type": row.role_type, "name": row.name}):
                raise ValueError("旁白角色不可删除（多人模式要求唯一旁白）")
            session.delete(row)
            session.commit()
            return True
        finally:
            session.close()


# ============================================================ 模块级辅助
class _Cancelled(RuntimeError):
    pass


def voice_is_usable(voice: Dict) -> bool:
    """
    音色能否"在 App 内真的出这个声"（C4-2，红线 8）——唯一判据：
    盘上有真实可回放人声样本（sample_audio_path，或本身就是音频文件的
    model_path），且文件大小 > 1KB。

    为什么只看样本（本轮最小方案：只改推荐池，不动引擎装载链路）：
      * builtin  5 条：没有任何音频文件，VoxCPM 只能退化成同一个零样本默认声，
                     多角色之间毫无区分度 —— 必须剔除；
      * designed 1 条：无样本，靠引擎装载后用 description 合成，冷启动时请求内
                     联加载模型（实测 203s），且 /api/voices/{id}/preview 404
                     —— 不进自动池；
      * cloned   3 条：只有 reference_audio_path，试听端点 get_preview_audio()
                     只认样本 → 同样 404，用户无法在 App 内复核 —— 也不进自动池。
    这三类音色在音色选择器里仍然可见、仍可手工绑定，只是不再被"自动推荐/自动
    分配"当成首选结果。待样本链路补齐（builtin 配样本、preview 覆盖
    reference/description 两支）后，把判据换成"可回放音频 ∪ 引擎已装载"即可放开。
    """
    if not isinstance(voice, dict):
        return False
    for key in ("sample_audio_path", "model_path"):
        path = str(voice.get(key) or "").strip()
        if not path:
            continue
        if os.path.splitext(path)[1].lower() not in AUDIO_SUFFIXES:
            continue                                    # model_path 常是模型目录
        try:
            if os.path.isfile(path) and os.path.getsize(path) > MIN_SAMPLE_BYTES:
                return True
        except OSError:                                 # pragma: no cover - 环境相关
            continue
    return False


def voice_is_replay_only(voice: Dict) -> bool:
    """
    这枚音色是不是"只会回放自己那条样本"（PY-330，红线 8「人声可应用」）。

    `SamplePlaybackEngine.synthesize(text, ...)` 收下 text 后**根本不看它**
    （modules/tts_engine/engines/sample_playback.py:62-104，直接读
    model_path/sample_audio_path 的 master 原样返回），所以绑了它的角色，
    全书每一句都是同一条录音。这与"能不能出声"无关（它能，且是自己的嗓子），
    因此判据独立于 voice_is_usable，只用于"能不能按台词说这句话"。

    与 C4-2 那三类不同：sample 行不是数据缺陷而是引擎语义，音色选择器里仍然
    可选（固定台词、音效段、旁白开场这类用途是它的正业），只是不该被
    自动推荐/自动分配挑中。
    """
    if not isinstance(voice, dict):
        return False
    return str(voice.get("engine") or "").strip().lower() == "sample"


def clean_chapter_text(cleaner, text: str) -> str:
    """章级清洗（P3 同函数）：cast 输入即清洗后文本，避免新增解析路径。"""
    try:
        return cleaner.clean(text) or ""
    except Exception as exc:                          # pragma: no cover - 防御
        logger.warning("[Cast] 章节清洗失败，按原文分析: %s", exc)
        return text or ""


def _role_type_for(payload: Dict) -> str:
    """Role.role_type：旁白固定 narrator，其余用抽取阶段的 hero/supporting。"""
    name = str(payload.get("name") or "").strip()
    if norm_name(name) in NARRATOR_NAMES:
        return "narrator"
    declared = str(payload.get("role_type") or "").strip()
    if declared in ("narrator", "hero", "supporting"):
        return declared
    return declared or "custom"


def _pick_narrator_name(roles: List[Dict]) -> Optional[str]:
    for r in roles or []:
        if is_narrator_role(r):
            return r.get("name")
    return None


def _role_dict(role: Role, session=None) -> Dict:
    """Role -> dict（含 extra_data）。既有 Role.to_dict() 不带 extra_data，这里补全。"""
    return {
        "id": role.id,
        "project_id": role.project_id,
        "name": role.name,
        "role_type": role.role_type,
        "voice_id": role.voice_id,
        "extra_data": role.extra_data or {},
        "cast": (role.extra_data or {}).get("cast") or {},
        "aliases": (role.extra_data or {}).get("aliases") or [],
        "mix": (role.extra_data or {}).get("mix") or {"gain_db": 0.0, "speed": 1.0, "pitch": 0.0},
        "confirmed": bool((role.extra_data or {}).get("confirmed")),
    }


def _persist_progress(task_id: str, frac: float, stage: str, chars: int,
                      total: int = 0, status: str = "") -> None:
    """把进度写进 ConversionTask 行（DB 是 get_progress 的兜底数据源）。

    C4-1: total_chars 必须一并落库。旧实现只传 4 个位置参数，tracker 一旦被 pop
    （任务结束），GET /api/tasks/{id}/progress 回落 DB 就出现
    {processed_chars: 819, total_chars: 0} —— 与 PY-121 同形缺陷。

    PY-331: 终态那一跳要把 status 一起交下去。`update_progress` 旧行为是
    "progress>=1.0 就猜 completed"，而失败/取消的终态进度同样是 1.0，于是
    cast_failed 先被写成 completed 再由 fail_task 改回 failed；取消的任务更糟——
    `CastService.cancel()` 只置内存标志，DB 行从没被写成 cancelled，
    于是 areport(status="cancelled") 落库成 completed、fail_task 又改成 failed，
    用户在任务列表里看到的是"我把分析停了 → 任务失败"。
    """
    task_service.update_progress(task_id, frac, stage, chars,
                                 total_chars=int(total or 0) or None,
                                 status=status or "")


def _safe_fail_task(task_id: str, message: str) -> None:
    try:
        task_service.fail_task(task_id, message)
    except Exception as exc:                          # pragma: no cover - 防御
        logger.warning(f"[Cast:{task_id}] fail_task 写入失败: {exc}")


def _make_llm(use_llm: bool):
    """
    use_llm=False -> 传一个 is_ready()==False 的桩，让 CastProfiler 整表走规则兜底
    （spec 4.1 允许的降级路径，profile.stats()['llm_ready'] 会留痕，不冒充 LLM 结果）。
    """
    if use_llm:
        return None
    return _DisabledLLM()


class _DisabledLLM:
    """显式禁用 LLM 的适配器（不是 mock 业务数据，只把可用性置 False）。"""

    def is_ready(self) -> bool:
        return False

    class client:                                    # noqa: N801 - 保持属性访问形状
        @staticmethod
        def chat(*a, **kw):
            raise RuntimeError("cast LLM 已被 PYY_CAST_USE_LLM=0 禁用")


def _emit(report, frac: float, stage: str, chars: int, total: int) -> None:
    """从工作线程投递进度（fire-and-forget，线程安全）。"""
    try:
        report(frac, stage, chars, total)
    except Exception as exc:                          # pragma: no cover - 防御
        logger.warning("[Cast] 进度投递失败: %s", exc)


def _register_tracker(task_id: str):
    """
    把 ProgressTracker 挂到 conversion_workflow 的运行中表，
    这样既有 GET/WS /api/tasks/{id}/progress 直接能读到 cast 任务进度。
    返回 None 时（导入异常）进度只落 DB，前端仍可用 HTTP 轮询取到。
    """
    try:
        from modules.workflow.conversion_workflow import conversion_workflow
        from modules.workflow.progress_tracker import ProgressTracker
    except Exception as exc:                          # pragma: no cover - 防御
        logger.warning(f"[Cast:{task_id}] ProgressTracker 不可用，仅落 DB 进度: {exc}")
        return None
    tracker = ProgressTracker(task_id)
    try:
        conversion_workflow._active_trackers[task_id] = tracker
    except Exception as exc:                          # pragma: no cover - 防御
        logger.warning(f"[Cast:{task_id}] tracker 注册失败: {exc}")
        return None
    return tracker


def _unregister_tracker(task_id: str) -> None:
    try:
        from modules.workflow.conversion_workflow import conversion_workflow
        conversion_workflow._active_trackers.pop(task_id, None)
    except Exception:
        pass


def _env_false(name: str) -> bool:
    import os
    return str(os.getenv(name, "")).strip().lower() in ("0", "false", "no", "off")


# 全局单例
cast_service = CastService()
