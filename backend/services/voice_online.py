"""#89 VOICE-ONLINE —— 在线音色库的搜索 / 详情 / 真下载（含 DB 注册）。

复现原文（见 agent-ops/VOICE-API-REPORT.md）：
    `GET /api/voices/online/search?query=voice` 在 10:07 实测 **curl 25s 完全无响应
    （code=000）**，用户侧就是"在线音色库功能无法获取音色文件"。

根因两条，都有实测证据，不猜：
  1. **事件循环被同步 socket 拖死**：旧路由是 `async def` + 直接调用
     `voice_service.search_online()`，里面是 `httpx.Client` 同步请求
     （30s 超时 ×3 次退避重试）。HF 端点在本机不可达（curl: "Failed to connect to
     huggingface.co port 443 after 21303 ms", code=000），于是这 93 秒里**整个
     FastAPI 事件循环**都停摆 —— 同期 `/api/voices/statistics` 和一个已知可用的
     `/api/voices/{id}/preview` 一起返回 000。修法：路由一律 `asyncio.to_thread`
     下放 + `asyncio.wait_for` 硬预算，爬虫侧超时降到连接 5s/读 10s。
  2. **失败被假数据盖掉**：旧 `hf_crawler._fallback_search` 在 API 不通时返回 10 条
     手写 "well-known"（downloads=999999），前端"搜到了"，但 `download_voice()`
     随后 `snapshot_download()` 还是连不上 HF —— 表现成"拿不到音色文件"。
     现在：失败只返回空列表 + `reason`；下载走**实测可用**的镜像 resolve/raw 通道，
     拿到真音频文件、解码归一化后才登记 1 行。

可用源是实测出来的，不是猜的（本机 2026-09-22 11:03 一组 curl）：
    https://huggingface.co/api/...  code=000 t=21.3s  -> 不可用（如实上报）
    https://hf-mirror.com/api/...   code=200 t=0.96s  -> 可用，真条目
    https://api.github.com/search/repositories  code=200 t=2.0s -> 可用，真条目
    https://hf-mirror.com/hexgrad/Kokoro-82M/resolve/main/samples/af_heart_0.wav
        code=200 t=1.4s size=237644 -> RIFF WAVE PCM16 mono 24000Hz 4.95s

对生产库的写入（阈值受 #89 工单约束）：**只在下载成功时新增 1 行**
`voice_profiles`（外加对该新行自己的 1 次 UPDATE 补路径），失败不落库、
不改不删任何既有行。下载产物落 `data/voices/online/`。

PY-364（同源缺口·在线下载这半边）：UPDATE 现同时写克隆档引擎的
`reference_audio_path`，且登记前后各过一道与
`tools/resource_scout/import_to_voices.py::intake_gate` **同一个函数**的入库门；
门拒 → 结构化失败回前端，不留半截行、不造"挑了等于没挑"的假音色。

U-7 收口（#89 之后仍开的两处，本地实测口径）：
  * **M-1 线程按请求累积**：`search_all()` 曾经每次自己 `ThreadPoolExecutor(2)` 再
    `shutdown(wait=False, cancel_futures=True)` —— 并发 10 次搜索瞬时就是 20 枚线程，
    而被"放弃"的那几枚还带着 socket 继续跑。现在是**进程级共享池**（`search_pool()`，
    并行度 `VOICE_ONLINE_POOL_SIZE`，默认 6），作业开工前自查剩余预算，到点让位。
  * **M-2 放弃之后没有账**：超时/放弃一律记账（`source_timeouts` /
    `cancelled_while_queued` / `abandoned_still_running` / `skipped_no_budget`），
    路由层超总预算时落一条终态记录（内存 + 日志，**不写生产库**），
    `GET /api/voices/online/sources` 直接能看到超时率和池内存活线程数。
  * **`..` 段门**：`_SAFE_REPO`/`_SAFE_FILE` 的字符类允许 `.`，`samples/../../x` 逐字
    符合法却能过正则并被拼进 resolve/raw URL —— 由 `has_upward_segment()` 按段拒。
"""

from __future__ import annotations

import logging
import os
import re
import sys
import threading
import time
import uuid
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor, wait
from pathlib import Path, PurePosixPath
from typing import Optional

logger = logging.getLogger(__name__)

# 单个源的搜索预算（爬虫内部）+ 路由层总预算。总预算 > 单源预算：两源并发。
SOURCE_DEADLINE_S = float(os.getenv("VOICE_ONLINE_SOURCE_DEADLINE_S", "12"))
ROUTE_DEADLINE_S = float(os.getenv("VOICE_ONLINE_ROUTE_DEADLINE_S", "20"))

# 试听样本的体积/时长约束：太长对"试听"没意义，还会把音色库撑爆。
MAX_SAMPLE_SECONDS = float(os.getenv("VOICE_ONLINE_MAX_SAMPLE_SECONDS", "20"))
MIN_SAMPLE_SECONDS = float(os.getenv("VOICE_ONLINE_MIN_SAMPLE_SECONDS", "0.4"))

_SAFE_REPO = re.compile(r"^[A-Za-z0-9_.\-]{1,80}(/[A-Za-z0-9_.\-]{1,120})?$")
_SAFE_FILE = re.compile(r"^[A-Za-z0-9_.\-/ ]{1,200}$")


def has_upward_segment(value: str) -> bool:
    """含 `..` 路径段就拒。

    两道正则的字符类都允许 `.`，所以 `a/../../b`、`samples/../../etc/passwd`
    这种**逐字符合法**的串能同时过 `_SAFE_REPO` 和 `_SAFE_FILE`，然后被拼进
    resolve/raw URL 和本地产物名。正则管不了"段"，只能按段查。
    """
    return any(part == ".." for part in PurePosixPath(str(value or "")).parts)


# reason -> HTTP 状态（路由层用；结构化 detail 里带原始 error 文本）
ERROR_STATUS = {
    "no_http_client": 503,
    "no_results": 200,
    "bad_repo": 400,
    "repo_not_found": 404,
    "no_audio_in_repo": 422,
    "unreachable": 502,
    "timeout": 504,
    "deadline_exceeded": 504,
    "too_large": 413,
    "forbidden": 403,
    "rate_limited": 429,
    "http_error": 502,
    "bad_payload": 502,
    "network_error": 502,
    "tls_error": 502,
    "redirect_loop": 502,
    "decode_failed": 422,
    "quality_gate_failed": 422,
    "too_short": 422,
    "too_long": 422,
    "write_failed": 500,
    "engine_unregistered": 400,
    "engine_unavailable": 400,
    # PY-364 同源护栏：克隆档引擎缺"行上参考音频"一律拒入池（见 _register_voice）
    "intake_gate_failed": 422,
}


def status_for(reason: str, default: int = 502) -> int:
    return int(ERROR_STATUS.get(reason or "", default))


def online_dir() -> Path:
    """下载产物目录（`VOICE_ONLINE_DIR` 可覆盖，单测指向 tmp_path）。"""
    override = os.getenv("VOICE_ONLINE_DIR")
    if override:
        return Path(override)
    from backend.config import Config
    return Path(Config.VOICE_DIR) / "online"


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", (text or "").strip().replace(" ", "_"))[:80]


def _fail(reason: str, message: str, **extra) -> dict:
    return {"ok": False, "reason": reason, "message": message,
            "status": status_for(reason), **extra}


# ------------------------------------------------------------------ 进程级搜索池
# M-1：旧实现每次搜索自己 `ThreadPoolExecutor(max_workers=2)` 再 `shutdown(wait=False)`
# —— 并发 10 次就是 20 枚线程，而"放弃等待"时那几枚还带着连接继续跑。池按**进程**建
# 一次、并行度封顶就够：每源的超时预算本来就是按请求传进 httpx 的
# （`online_net.request_timeout(left)`），不需要靠"每请求一个池"来隔离。
POOL_THREAD_PREFIX = "voice-online"


def _env_f(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "") or default)
    except (TypeError, ValueError):
        return default


SEARCH_POOL_SIZE = max(2, int(_env_f("VOICE_ONLINE_POOL_SIZE", 6)))

_pool_lock = threading.Lock()
_pool: Optional[ThreadPoolExecutor] = None


def search_pool() -> ThreadPoolExecutor:
    """懒建 + 全进程共用（懒建是为了 import 期不起线程，单测收集阶段不该有池）。"""
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = ThreadPoolExecutor(max_workers=SEARCH_POOL_SIZE,
                                       thread_name_prefix=POOL_THREAD_PREFIX)
        return _pool


def shutdown_search_pool(drain: bool = False) -> None:
    """关停/测试用：把池线程放回去，好断言"线程不随请求累积"。"""
    global _pool
    with _pool_lock:
        if _pool is not None:
            _pool.shutdown(wait=drain, cancel_futures=not drain)
            _pool = None


def pool_threads_alive() -> int:
    return sum(1 for t in threading.enumerate()
               if t.name.startswith(POOL_THREAD_PREFIX) and t.is_alive())


# ------------------------------------------------------------------ 搜索统计（M-2 的账）
_stats_lock = threading.Lock()
_STATS = {
    "searches": 0,                  # 进过 search_all 的次数
    "source_jobs": 0,               # 提交过的单源作业数（超时率的分母）
    "source_timeouts": 0,           # 到点没取到结果的作业数
    "cancelled_while_queued": 0,    # 还在队列里就被真取消掉的作业数
    "abandoned_still_running": 0,   # 放弃时**仍在跑**的作业数（原来完全没记）
    "skipped_no_budget": 0,         # 开工前自查预算已耗尽、直接让位的作业数
    "route_timeouts": 0,            # 路由层外层预算到点的次数
}
_recent: deque = deque(maxlen=50)   # 最近的终态记录（route_timeout 等）


def search_stats() -> dict:
    """超时率与"放弃后仍在跑的线程数"必须可断言，不能只写在注释里。"""
    with _stats_lock:
        snap = dict(_STATS)
        recent = list(_recent)
    jobs = snap["source_jobs"]
    snap["source_timeout_rate"] = round(snap["source_timeouts"] / jobs, 4) if jobs else 0.0
    snap["pool_max_workers"] = SEARCH_POOL_SIZE
    snap["pool_threads_alive"] = pool_threads_alive()
    snap["recent"] = recent
    return snap


def _bump(key: str, n: int = 1) -> None:
    """计数器累加（池线程里也会调，必须带锁）。未知键直接炸：写错键名的"记上了"是最坏的假绿。"""
    with _stats_lock:
        if key not in _STATS:
            raise KeyError(f"未知统计键: {key}")
        _STATS[key] += n


def reset_search_stats() -> None:
    with _stats_lock:
        for key in _STATS:
            _STATS[key] = 0
        _recent.clear()


def record_route_timeout(query: str = "", source: str = "all", limit: int = 0,
                         budget_s: float = 0.0) -> dict:
    """路由层外层预算到点：落一条**终态**记录（内存 + 日志，不写生产库）。

    `asyncio.to_thread` 起的线程取消不掉，能做的只有把共享池里还没开工的作业撤掉，
    并记下"还剩几枚在跑"——不记录就等于谁也不知道有没有泄漏。
    """
    rec = {"kind": "route_timeout", "query": query, "source": source, "limit": limit,
           "budget_s": round(float(budget_s), 2),
           "at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())}
    with _stats_lock:
        _STATS["route_timeouts"] += 1
        _recent.appendleft({k: rec[k] for k in ("kind", "query", "source", "budget_s", "at")})
    # 快照放在累加**之后**：否则这条记录看不到自己那一笔，route_timeouts 永远少 1。
    rec["stats"] = {k: v for k, v in search_stats().items() if k != "recent"}
    logger.warning("在线音色搜索超路由预算 %ss：%s", f"{float(budget_s):.1f}", rec["query"])
    return rec


# ------------------------------------------------- 单源作业（进共享池的就是它）
def _guarded_search(crawler, name: str, query: str, language, limit: int,
                    job_deadline: float) -> dict:
    """单源作业：开工前先自查预算，再把**剩余**预算当 deadline 交给爬虫。

    共享池只有 `SEARCH_POOL_SIZE` 枚线程，超载时作业会在队列里等。排队排到点才轮到
    的作业如果照原样发起请求，就是在调用方早已放弃之后再去烧 12s —— 所以这里一发
    都不发地让位（`skipped_no_budget`）。外层 `cancel()` 撤得掉的撤掉，撤不掉
    （已经开始跑）的由这道自查兜住。
    """
    left = job_deadline - time.monotonic()
    if left <= 0:
        _bump("skipped_no_budget")
        return {"source": name, "ok": False, "results": [],
                "reason": "budget_exhausted",
                "error": "排队到开工前总预算已耗尽，本作业主动让位（未发起请求）",
                "http_code": 0, "elapsed_ms": 0.0, "attempts": []}
    return crawler.search_with_status(query=query, language=language, limit=limit,
                                      deadline_s=min(SOURCE_DEADLINE_S, left))


# ------------------------------------------------------------------ 搜索
def search_all(query: str = "", language: str = None, source: str = "all",
               limit: int = 20, budget_s: float = None) -> dict:
    """两源并发搜索的**同步**实现（路由用 to_thread 包一层）。

    返回:
      {ok, results, total, sources: [{source, ok, endpoint, http_code, elapsed_ms,
        reason, error, count, endpoint_status}], deadline_s, skipped, pool}
    `ok` 只表示"至少一个源真的活着"；死源在 `sources` 里带真实 reason/error，
    前端据此显示"哪个源、什么原因"。

    线程口径（M-1）：作业提交到**进程级**共享池，并行度封顶 `SEARCH_POOL_SIZE`，
    所以 N 枚并发请求的线程数不随 N 增长。外层放弃等待时对未完成的 future 一律
    `cancel()`，并分开记"还在队列里就被撤掉"和"放弃时仍在跑"——后者不记就没人知道
    有没有泄漏。
    """
    from backend.services.voice_service import voice_service

    wanted = (source or "all").strip().lower()
    if wanted in ("hf", "huggingface", "hugging_face"):
        wanted = "huggingface"
    do_hf = wanted in ("huggingface", "all", "")
    do_gh = wanted in ("github", "all", "")

    t_submit = time.monotonic()
    budget = float(budget_s) if budget_s else ROUTE_DEADLINE_S
    # 每个源自己的墙钟上限：爬虫内部已有 deadline，这里只是**兜底**——绝不让
    # "某个源卡在没被取消的线程里"把整个搜索/路由拖住。总预算小的话以总预算为准，
    # 留 1s 给结果装配，否则路由层先超时、这边还在等价作业。
    grace_s = max(1.0, min(SOURCE_DEADLINE_S + 6.0, budget - 1.0))
    job_deadline = t_submit + grace_s

    pool = search_pool()
    futures: "dict[Future, str]" = {}
    sources = []
    results = []
    skipped = 0
    _bump("searches")
    if do_hf:
        futures[pool.submit(_guarded_search, voice_service.hf_crawler, "huggingface",
                            query, language, limit, job_deadline)] = "huggingface"
    if do_gh:
        futures[pool.submit(_guarded_search, voice_service.github_crawler, "github",
                            query, language, limit, job_deadline)] = "github"
    _bump("source_jobs", len(futures))

    done, pending = wait(futures.keys(), timeout=grace_s)
    for fut in pending:
        # 只有"还在队列里"的作业撤得掉；已经在跑的撤不掉，只能记下来。
        if fut.cancel():
            _bump("cancelled_while_queued")
        else:
            _bump("abandoned_still_running")
    for fut, name in futures.items():
        if fut not in done:
            _bump("source_timeouts")
            sources.append({
                "source": name, "ok": False, "results": [],
                "reason": "source_timeout",
                "error": f"该源超过 {grace_s:.0f}s 仍未返回，已放弃等待（不阻塞其它源）",
                "http_code": 0, "count": 0,
                "elapsed_ms": round((time.monotonic() - t_submit) * 1000.0, 1),
                "attempts": [],
            })
            continue
        try:
            status = fut.result()
        except Exception as e:            # noqa: BLE001 - 单源炸不能带走另一个源
            status = {"source": name, "ok": False, "results": [],
                      "reason": "worker_crashed",
                      "error": f"{type(e).__name__}: {e}", "http_code": 0,
                      "elapsed_ms": 0.0, "attempts": []}
        status = dict(status or {})
        items = status.pop("results", []) or []
        if not status.get("ok"):
            # 死源带回来的条目不进结果：#89 的第一层病根就是"失败还端出假数据"。
            items = []
        status["count"] = len(items)
        results.extend(items)
        if status.get("reason") == "budget_exhausted":
            skipped += 1
        sources.append(status)
    # 这里**不**关池：池是进程级的，随每次请求关建就是 M-1 的成因。

    ok = any(s.get("ok") for s in sources)
    note = ""
    if not ok and sources:
        note = "；".join(f"{s['source']}: {s.get('error') or s.get('reason') or '不可用'}"
                         for s in sources)
    return {
        "ok": ok,
        "query": query,
        "language": language,
        "requested_source": source,
        "results": results,
        "total": len(results),
        "sources": sources,
        "fabricated": False,          # 明确声明：本响应里没有编造的条目
        "deadline_s": round(grace_s, 2),
        "skipped": skipped,
        "pool": {"shared": True, "max_workers": SEARCH_POOL_SIZE,
                 "threads_alive": pool_threads_alive()},
        "message": "" if ok else (note or "没有可用在线源"),
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime()),
    }


def detail(model_name: str, source: str = "huggingface") -> dict:
    """条目详情（HF 走镜像 API；GitHub 返回仓库信息 + 可下载音频清单）。"""
    from backend.services.voice_service import voice_service

    repo = (model_name or "").strip()
    if not repo or not _SAFE_REPO.match(repo):
        return _fail("bad_repo", f"模型/仓库名不合法: {repo!r}", model_name=repo)
    if has_upward_segment(repo):
        return _fail("bad_repo", f"模型/仓库名含上层路径段: {repo!r}", model_name=repo)

    if (source or "").strip().lower() == "github":
        crawler = voice_service.github_crawler
        branch, rec = crawler.resolve_repo(repo, deadline_s=SOURCE_DEADLINE_S)
        if not rec.get("ok"):
            return _fail(rec.get("reason") or "unreachable",
                         f"GitHub 仓库详情获取失败: {rec.get('error') or rec.get('reason')}",
                         model_name=repo, endpoint=crawler.API_URL,
                         http_code=rec.get("http_code", 0), attempts=rec.get("attempts", []))
        candidates, listing = crawler.audio_candidates(repo, deadline_s=SOURCE_DEADLINE_S)
        return {"ok": bool(rec.get("ok")), "source": "github", "model_name": repo,
                "branch": branch, "audio_files": candidates, "listing": listing,
                "url": f"https://github.com/{repo}"}

    crawler = voice_service.hf_crawler
    d = crawler.get_model_details(repo, deadline_s=SOURCE_DEADLINE_S)
    if not d.get("available"):
        reason = d.get("reason") or "unreachable"
        return _fail(reason, f"HuggingFace 模型详情获取失败: {d.get('error') or reason}",
                     model_name=repo, endpoint_status=crawler.endpoint_status())
    candidates, listing = crawler.audio_candidates(repo, deadline_s=SOURCE_DEADLINE_S)
    d["audio_files"] = candidates
    d["listing"] = listing
    d["ok"] = True
    d["source"] = "huggingface"
    d["model_name"] = repo
    return d


# ------------------------------------------------------------------ 下载
# PY-364 同源护栏（本文件这半边）：判据**不另立第二套** —— 直接 import
# tools/resource_scout/import_to_voices.py 里已提交的 intake_gate /
# REFERENCE_ENGINES / MIN_REF_BYTES，两扇门共用同一函数，口径永不漂移。
_intake_guard_mod = None


def _intake_guard():
    global _intake_guard_mod
    if _intake_guard_mod is None:
        root = Path(__file__).resolve().parents[2]
        tools_dir = str(root / "tools" / "resource_scout")
        if tools_dir not in sys.path:
            sys.path.insert(0, tools_dir)
        import import_to_voices as mod      # 顶层只有 argparse/rs_db，无副作用
        _intake_guard_mod = mod
    return _intake_guard_mod


def _register_voice(*, name: str, engine: str, wav_path: Path, download_url: str,
                    origin: dict, quality: dict, duration_s: float) -> dict:
    """下载成功后登记 1 行（唯一的写库点：1 次 INSERT + 对该新行的 1 次 UPDATE）。

    PY-364 定罪：这里曾经**从不写 reference_audio_path**（只有 sample_audio_path），
    而 route 的 engine 默认值是 voxcpm（零样本克隆档）——下载 Kokoro 仓样本留默认，
    就造出 c7b3ebd6 那种"可挑可绑、挑了拿到引擎默认声"的假音色。现在双门：
      * 门一（入库前）：手里这份 wav 必须在盘上、体积合理，且来源仓库归属不与
        engine 冲突，否则**建行之前**就拒（天然不留半截行）；
      * 门二（写库后）：克隆档引擎（REFERENCE_ENGINES）必须回读确认
        reference_audio_path **真落在行上**且指向在盘文件；不过就删行+清文件，
        返回结构化失败（绝不"照样入池但出默认声"）。
    非克隆档（kokoro/edgetts 等预设说话人）不要求参考音频，照常登记。
    """
    from modules.voice_manager.voice_store import voice_store

    try:
        guard = _intake_guard()
    except Exception as e:            # noqa: BLE001 - 护栏自证不可用宁可拒
        return _fail("intake_gate_failed",
                     f"PY-364 登记门原语无法装载（{type(e).__name__}: {e}），"
                     f"护栏不可用时一律拒绝入库", engine=engine)

    eng = str(engine or "").strip().lower()
    origin = origin or {}
    gate_meta = {"source_url": download_url, "page_url": download_url,
                 "origin": {"repo": origin.get("repo")}}

    def _gate(ref_path: str):
        return guard.intake_gate(eng, ref_path, gate_meta, download_url)

    wav = Path(wav_path)
    reason = _gate(str(wav) if wav.is_file() else "")
    if reason:
        return _fail("intake_gate_failed",
                     f"下载样本未通过登记门，拒绝入音色池 (PY-364): {reason}",
                     engine=eng, repo=origin.get("repo"), wav=str(wav))

    # 注意：**不能**给 create() 传 is_downloaded —— 它内部已经写死
    # `VoiceProfile(..., is_downloaded=1, **kwargs)`，再传一次就是
    # "got multiple values for keyword argument 'is_downloaded'" TypeError。
    # 旧的 `download_voice()` 正是踩了这颗雷（并且 except 块里引用了未绑定的
    # `voice.id` → NameError），所以 `POST /voices/online/download` 在任何网络
    # 条件下都必然 500 —— 这是"在线音色库拿不到音色文件"的第一层根因。
    row = voice_store.create(
        name=name,
        source="downloaded",
        engine=engine,
        description=origin.get("description") or f"来自 {origin.get('source')}: {origin.get('repo')}",
        download_url=download_url,
        category=origin.get("category") or "human",
        language=origin.get("language") or "zh",
        meta={"origin": origin, "quality": quality, "duration_s": duration_s,
              "registered_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())},
    )
    final = wav_path.with_name(f"{row.id}.wav")
    os.replace(str(wav_path), str(final))
    base_meta = dict(row.meta) if isinstance(row.meta, dict) else {}
    base_meta["origin"] = {**(origin or {}), "local_path": str(final)}
    update_fields = dict(sample_audio_path=str(final), meta=base_meta)
    if eng in guard.REFERENCE_ENGINES:
        # 克隆档引擎：这份下载 wav 既是试听也是克隆参考音频。写了才可能在
        # voxcpm_adapter._restore_voices 的 `and v.reference_audio_path` 里活下来。
        update_fields["reference_audio_path"] = str(final)
    voice_store.update(row.id, **update_fields)

    # 门二：以**库里回读**为准复核（防"代码写了但库里没落上"这类静默漂移）。
    check = voice_store.get(row.id)
    reason = _gate(str(getattr(check, "reference_audio_path", "") or "")) \
        if check is not None else "登记行回读失败（行已不在库里）"
    if reason:
        try:
            voice_store.delete(row.id)
        except Exception as e:        # noqa: BLE001 - 删不掉也要如实报
            logger.warning("[online] PY-364 门二回滚删行失败 %s: %s", row.id, e)
        try:
            if Path(final).is_file():
                Path(final).unlink()
        except OSError as e:
            logger.warning("[online] PY-364 门二清理文件失败 %s: %s", final, e)
        return _fail("intake_gate_failed",
                     f"写库后复核未通过，已回滚、拒绝入音色池 (PY-364): {reason}",
                     engine=eng, repo=origin.get("repo"), voice_id=row.id)

    return {"ok": True, "voice_id": row.id, "local_path": str(final)}


def download_and_register(model_name: str, engine: str = "voxcpm",
                          source: str = "huggingface", file_path: str = None,
                          name: str = None, max_bytes: int = None,
                          keep_raw: bool = False) -> dict:
    """把在线条目**真的**下成可试听的人声样本并登记 1 行。

    链路：列文件 -> 流式下载(体积/时间双上限) -> magic 校验 -> 解码归一化成
    48k 单声道 PCM16 -> 时长/质量双门 -> 落 `data/voices/online/` -> 登记。
    任一步失败：清理本次产生的临时文件，**不写库**。

    Returns 成功:
      {ok, voice_id, path, bytes, magic, format, sample_rate, channels,
       duration_s, source, repo, file_path, origin_url, endpoint, elapsed_ms, quality}
    失败: {ok: False, reason, message, status, ...真实错误}
    """
    from modules.tts_engine.engine_registry import engine_registry
    from modules.voice_manager.audio_quality import assess_file
    from modules.voice_manager.voice_crawler import online_net

    repo = (model_name or "").strip()
    src = (source or "huggingface").strip().lower()
    if src in ("hf", "hugging_face"):
        src = "huggingface"
    if not repo or not _SAFE_REPO.match(repo):
        return _fail("bad_repo", f"模型/仓库名不合法: {repo!r}", model_name=repo, source=src)
    if has_upward_segment(repo):
        return _fail("bad_repo", f"模型/仓库名含上层路径段: {repo!r}",
                     model_name=repo, source=src)
    if file_path and not _SAFE_FILE.match(file_path):
        return _fail("bad_repo", f"文件名不合法: {file_path!r}", model_name=repo)
    if file_path and has_upward_segment(file_path):
        return _fail("bad_repo", f"文件名含上层路径段: {file_path!r}", model_name=repo)
    if not online_net.HAS_HTTPX:
        return _fail("no_http_client", "httpx 未安装，在线音色下载不可用", model_name=repo)

    known = set(engine_registry.known_engine_names())
    eng = (engine or "").strip()
    if eng not in known:
        return _fail("engine_unregistered",
                     f"引擎 '{eng}' 不在注册表 {sorted(known)} 中，登记了也合成不出来",
                     model_name=repo, known=sorted(known))
    _inst = engine_registry.get(eng)
    _reason = _inst.unavailable_reason() if _inst is not None else "适配器实例化失败"
    if _reason:
        return _fail("engine_unavailable",
                     f"引擎 '{eng}' 在本机不可用（{_reason}），登记了也合成不出来",
                     model_name=repo, engine=eng, unavailable_reason=_reason)

    t0 = time.monotonic()
    dest_dir = online_dir()
    dest_dir.mkdir(parents=True, exist_ok=True)
    scratch = dest_dir / f".{uuid.uuid4().hex}"
    raw_path = Path(str(scratch) + ".part")
    wav_path = Path(str(scratch) + ".wav")
    registered: Optional[Path] = None        # 登记成功的最终文件（清理逻辑必须避开）

    from backend.services.voice_service import voice_service
    if src == "github":
        crawler = voice_service.github_crawler
        origin_url = f"https://raw.githubusercontent.com/{repo}/<branch>/{file_path or '<auto>'}"
        page_url = f"https://github.com/{repo}"
    else:
        crawler = voice_service.hf_crawler
        origin_url = f"https://huggingface.co/{repo}/resolve/main/{file_path or '<auto>'}"
        page_url = f"https://huggingface.co/{repo}"

    try:
        rec = crawler.download_sample(repo, raw_path, file_path=file_path,
                                      max_bytes=max_bytes,
                                      deadline_s=float(os.getenv(
                                          "VOICE_ONLINE_DOWNLOAD_DEADLINE_S", "60")))
        if not rec.get("ok"):
            reason = rec.get("reason") or "unreachable"
            if rec.get("http_code") == 404:
                reason = "repo_not_found"
            return _fail(reason,
                         f"下载失败({src}): {rec.get('error') or rec.get('reason') or '未知网络错误'}",
                         model_name=repo, source=src, attempts=rec.get("attempts", []),
                         http_code=rec.get("http_code", 0), endpoint=rec.get("url", ""),
                         candidates=rec.get("candidates", []))

        probe = online_net.probe_audio(raw_path)
        if probe["bytes"] < 1024 or not probe["magic_hint"]:
            return _fail("decode_failed",
                         f"下载内容不像音频文件: magic={probe['magic']} bytes={probe['bytes']}",
                         model_name=repo, source=src, probe=probe)

        try:
            norm = online_net.normalize_to_wav(raw_path, wav_path,
                                              max_seconds=MAX_SAMPLE_SECONDS)
        except Exception as e:            # noqa: BLE001 - 解码失败要如实出口
            return _fail("decode_failed",
                         f"音频解码/归一化失败: {type(e).__name__}: {e}",
                         model_name=repo, source=src, probe=probe)

        duration_s = float(norm["duration_s"])
        if duration_s < MIN_SAMPLE_SECONDS:
            return _fail("too_short",
                         f"样本只有 {duration_s}s，短于 {MIN_SAMPLE_SECONDS}s，不足以试听",
                         model_name=repo, source=src, duration_s=duration_s)

        report, err = assess_file(str(wav_path))
        if report is None:
            return _fail("decode_failed", f"归一化后的 wav 无法质量判定: {err}",
                         model_name=repo, source=src, duration_s=duration_s)
        if not report.get("ok"):
            return _fail("quality_gate_failed",
                         f"下载的样本未通过质量双门: {report.get('reason')}",
                         model_name=repo, source=src, quality=report,
                         duration_s=duration_s)

        final_probe = online_net.probe_audio(wav_path)
        entry_name = (name or "").strip() or f"{repo.split('/')[-1]}"[:60]
        reg = _register_voice(
            name=entry_name, engine=eng, wav_path=wav_path, download_url=page_url,
            origin={
                "source": src, "repo": repo, "file_path": rec.get("file_path"),
                "candidates": rec.get("candidates", []), "origin_url": origin_url,
                "endpoint": rec.get("url", ""), "http_code": rec.get("http_code", 0),
                "raw_bytes": probe["bytes"], "raw_magic": probe["magic"],
                "raw_magic_hint": probe["magic_hint"], "raw_format": probe["format"],
                "raw_sample_rate": probe["sample_rate"], "raw_channels": probe["channels"],
                "raw_duration_s": probe["duration_s"],
                "language": (rec.get("language") or ""),
                "description": (rec.get("description") or ""),
            },
            quality=report, duration_s=final_probe["duration_s"] or duration_s)
        # PY-364 护栏拒绝：原样把结构化失败送回（route 会转成带 reason/message 的
        # HTTPException，前端可见），绝不能吞掉错误继续当作成功。
        if not reg.get("ok"):
            return reg
        # 登记成功：这个路径从此是**库里的资产**，finally 的清理绝不能碰它
        registered = Path(reg["local_path"])
        wav_path = registered

        return {
            "ok": True,
            "status": "downloaded",
            "voice_id": reg["voice_id"],
            "path": str(wav_path),
            "bytes": wav_path.stat().st_size,
            "magic": final_probe["magic"],
            "magic_hint": final_probe["magic_hint"],
            "format": final_probe["format"],
            "sample_rate": final_probe["sample_rate"],
            "channels": final_probe["channels"],
            "duration_s": final_probe["duration_s"] or duration_s,
            "source": src,
            "repo": repo,
            "file_path": rec.get("file_path"),
            "endpoint_url": rec.get("url", ""),
            "page_url": page_url,
            "download_ms": rec.get("elapsed_ms", 0.0),
            "total_ms": round((time.monotonic() - t0) * 1000.0, 1),
            "quality": report,
            "engine": eng,
            "raw": {"path": str(raw_path) if raw_path.exists() else None,
                    "bytes": probe["bytes"], "magic": probe["magic"],
                    "sample_rate": probe["sample_rate"],
                    "channels": probe["channels"],
                    "duration_s": probe["duration_s"],
                    "format": probe["format"]} if keep_raw else None,
        }
    finally:
        protected = set()
        if registered is not None:
            protected.add(str(registered))
        if keep_raw:
            protected.add(str(raw_path))
        for leftover in (raw_path, wav_path):
            if str(leftover) in protected:
                continue
            try:
                if Path(leftover).exists():
                    Path(leftover).unlink()
            except OSError as e:
                logger.warning(f"[online] 临时文件清理失败 {leftover}: {e}")


def sources_status() -> dict:
    """两源当前实测状态 + 端点冷却表（诊断入口，前端可用来显示"哪个源死了"）。"""
    from backend.services.voice_service import voice_service
    return {
        "huggingface": {
            "endpoints": voice_service.hf_crawler.endpoint_status(),
            "last": _slim(voice_service.hf_crawler.last_status),
        },
        "github": {
            "endpoints": voice_service.github_crawler.pool.status(),
            "last": _slim(voice_service.github_crawler.last_status),
        },
        "budget_s": {"source": SOURCE_DEADLINE_S, "route": ROUTE_DEADLINE_S},
        # M-2 的账：超时率 / 放弃时仍在跑的作业数 / 池线程数，诊断入口直接可见
        "search_stats": search_stats(),
    }


def _slim(status: dict) -> dict:
    if not isinstance(status, dict):
        return {}
    return {k: status.get(k) for k in ("ok", "source", "endpoint", "http_code",
                                       "elapsed_ms", "reason", "error", "count")}
