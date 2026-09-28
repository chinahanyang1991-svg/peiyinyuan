"""
音色管理 API 路由

路由顺序即 FastAPI 匹配顺序：所有**静态子路径**（/search、/statistics …）
必须声明在动态段 /{voice_id} 之前，否则会被 /{voice_id} 抢先匹配 (WF-05)。
"""

import os
from pathlib import Path
from typing import Optional
from fastapi import APIRouter, UploadFile, File, HTTPException, Query, Form, Body
from fastapi.responses import FileResponse
from backend.services.voice_service import voice_service

router = APIRouter()


def _voice_store():
    """音色库直连（WF-12）。

    `backend/services/voice_service.py` 不在本次可改文件清单内（并行任务占用），
    其 `list_voices()` 也没有 offset/COUNT 能力，故列表分页在此直连
    `modules.voice_manager/voice_store`：service 的实现本来就是
    `[v.to_dict() for v in voice_store.list(...)]`，序列化口径完全一致。
    """
    from modules.voice_manager.voice_store import voice_store
    return voice_store


# US-B3: **只有**服务端真读 prompt_text 的引擎才谈得上 prompt-free。
# 判据抄自 `modules/tts_engine/engines/gptsovits_adapter.py:159-177`
# （`_ref_for_voice`: ref = reference_audio_path or model_path；
#   prompt_text = meta["transcript"]）与 `:278-284`（空/纯空白 → prompt-free 通报）。
# voxcpm/openvoice 走参考音频 in-context、edgetts/kokoro/sample 是预设说话人，
# 都不读 prompt_text —— 给它们打 prompt 标签就是撒谎，故发 None。
PROMPT_TEXT_ENGINES: frozenset = frozenset({"gptsovits"})

PROMPT_MODE_PROMPT = "prompt"                 # 有参考音频 + 有转写（正常档）
PROMPT_MODE_PROMPT_FREE = "prompt_free"       # 有参考音频、**无转写** = 会退化
PROMPT_MODE_NO_REFERENCE = "no_reference"     # 无参考音频（该引擎合成期直接报错）


def _prompt_mode_of(payload: dict, profile=None) -> Optional[str]:
    """给一条音色载荷打 prompt_mode 标 (US-B3 列表侧)。

    `profile` 是 voice_store 的 ORM 行（列表路径传）—— 因为列表载荷
    `to_dict()` **不含** reference_audio_path / model_path（只有详情的
    `to_dict_full()` 含），不传行对象就会把每条有参考音频的克隆音色误判成
    `no_reference`。

    纯字段判定，**不做任何文件系统 stat / 模型加载**：列表一页最多 500 行，
    每行 os.path.isfile 会把一次翻页变成 500 次磁盘探测。参考音频"路径写了但
    文件已不在"这种情况由合成期的 `_ref_for_voice` 报错兜底，列表不越权判。
    """
    if (payload.get("engine") or "") not in PROMPT_TEXT_ENGINES:
        return None
    meta = payload.get("meta") or {}
    src = profile if profile is not None else payload
    has_ref = bool(getattr(src, "reference_audio_path", None)
                   or payload.get("reference_audio_path")
                   or getattr(src, "model_path", None)
                   or payload.get("model_path")
                   or meta.get("reference_audio_path")
                   or meta.get("model_path"))
    transcript = (meta.get("transcript") or "").strip()
    if not has_ref:
        return PROMPT_MODE_NO_REFERENCE if not transcript else PROMPT_MODE_PROMPT
    return PROMPT_MODE_PROMPT if transcript else PROMPT_MODE_PROMPT_FREE


def _annotate_prompt_mode(payload, profile=None):
    """把 `prompt_mode` 就地补进一条音色载荷并返回它 (US-B3)。

    列表 (`to_dict()`) 与详情 (`to_dict_full()`) 两条序列化口径共用这一个函数，
    避免"列表打了标、详情没有"的第二个口径。非 dict（理论不到）原样返回。
    """
    if isinstance(payload, dict):
        payload["prompt_mode"] = _prompt_mode_of(payload, profile)
    return payload


@router.get("/")
async def list_voices(
    language: str = Query(None, description="语言筛选"),
    gender: str = Query(None, description="性别筛选"),
    source: str = Query(None, description="来源筛选"),
    engine: str = Query(None, description="引擎筛选"),
    style_tag: str = Query(None, description="风格标签筛选"),
    category: str = Query(None, description="类别筛选: human/animal/electronic/anime/mechanical"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0, description="分页偏移 (WF-12)"),
):
    """音色列表（多条件筛选 + 分页）。

    WF-12: 旧实现只有 limit 且 `total = len(voices)`（本页条数），
    库里 517 条时默认视图返回 100 条却报 `total: 100`——既取不全也翻不了页。
    现在 `total` 是同一筛选条件下的 SQL `COUNT(*)`，并新增 offset 翻页；
    `status/voices/total` 三个既有键的语义与类型不变（total 变准而非改名）。

    US-B3（列表侧打标，补齐泳道 B 留在 §跨泳道 的半截）：每条音色新增
    `prompt_mode` —— 选声阶段就能看出"这个克隆音色没有参考转写，合成时会
    prompt-free 退化"，而不是等任务日志里那条 warn。取值
    `prompt` / `prompt_free` / `no_reference` / `null`（该引擎不吃 prompt_text）。
    详情侧 `GET /api/voices/{id}` 走 `to_dict_full()`，同键由 `_annotate_prompt_mode`
    统一补齐。

    PERF-IDX: `store.list()`/`store.count()` 是同步 SQLAlchemy 查询（created_at
    排序 + COUNT(*)），旧写法在 `async def` 里直接跑，把事件循环按在数据库上；
    两条 SELECT 收进一个同步壳、整体 `asyncio.to_thread` 下放，序列化口径不变。
    """
    import asyncio

    store = _voice_store()
    filters = dict(language=language, gender=gender, source=source,
                   engine=engine, style_tag=style_tag, category=category)

    def _query_page():
        return (store.list(limit=limit, offset=offset, **filters),
                store.count(**filters))

    voices, total = await asyncio.to_thread(_query_page)
    return {
        "status": "ok",
        "voices": [_annotate_prompt_mode(v.to_dict(), v) for v in voices],
        "total": total,
        "limit": limit,
        "offset": offset,
        "returned": len(voices),
    }


@router.get("/search")
async def search_voices(keyword: str = Query(...)):
    """搜索音色"""
    voices = voice_service.search_voices(keyword)
    return {"status": "ok", "voices": voices, "total": len(voices)}


@router.get("/statistics")
async def voice_statistics():
    """音色库统计信息 (WF-05: 必须声明在 /{voice_id} 之前，否则恒 404)"""
    stats = voice_service.get_statistics()
    return {"status": "ok", "statistics": stats}


@router.get("/{voice_id}")
async def get_voice(voice_id: str):
    """获取音色详情"""
    voice = voice_service.get_voice(voice_id)
    if not voice:
        raise HTTPException(404, "音色不存在")
    return {"status": "ok", "voice": _annotate_prompt_mode(voice)}


@router.get("/{voice_id}/preview")
async def preview_voice(
    voice_id: str,
    wait: Optional[float] = Query(None, ge=0, le=600,
                                  description="无样本时最多等待现合成多少秒；"
                                              "0 = 只查文件/缓存，不等"),
    regenerate: int = Query(0, ge=0, le=1, description="1 = 忽略缓存重跑生成"),
    quality: str = Query("fast", pattern="^(fast|high)$",
                         description="fast(默认)=edge-tts 速度档，秒级；"
                                     "high=该音色绑定的引擎（voxcpm/GPT-SoVITS 等重引擎，"
                                     "受墙钟预算约束，只建议离线用）"),
):
    """试听音色 —— 返回音频文件（#87 不再恒 404；#95 默认档压到秒级）。

    四条出口（`backend/services/voice_preview.py::resolve_preview`）：

      1. `voice_sample`    音色行自带样本/模型音频（过 P0-4 双门）→ 直发；
      2. `generated_cache` 此前为这条音色现合成过、落在
                           `data/voices/previews_generated/` 的产物 → 直发（≤1s）；
      3. `generated_now`   都没有 → **默认用 edge-tts 速度档**现合成一小段
                           （文本 ≤14 字），过双门 + 试听口径后原子落盘再发；
                           同音色并发请求合并到一次生成（单飞），另有全局并发闸；
      4. `?quality=high`   显式高档出口才走该音色自己绑定的引擎（重引擎只进离线
                           整书合成是本项目既有裁定），同样受预算约束。

    旧行为的问题（复现：anime `99f782df…` / builtin `4b60f1e7…` 恒 404；
    第一代修复又把它变成 44 分钟的僵尸现合成，见 agent-ops/PREVIEW-FAST-REPORT.md）：
    只有第 1 条出口时 404 无任何信息；有第 3 条但没有预算/并发闸时承诺不可兑现。
    现在所有失败都是**结构化 detail**（`reason` + `code` + `next`，前端
    `detailText()` 直接拼人话）：
      * 404 音色不存在
      * 429 同档并发生成已达上限（`engine_busy`）
      * 429 上一条任务已被预算判死、但它的合成线程还没确认退出（PY-322
             `engine_busy_cancelling`：该 `voice_id::quality` 处于 poisoned 态，
             本次**不排队**、直接拒，避免第二个已放弃的请求继续烧重引擎串行锁）
      * 502 合成抛错 / 产物未过质量门（不返回静音顶替，PY-304 口径）
      * 503 引擎未注册/依赖未装/模型加载失败，以及"后台还在生成"（带 `Retry-After`）
      * 504 任务超过墙钟预算（`timeout`，冷却期内后续请求拿同一个码，不再无界重跑）

    US-C4 契约保持：响应头 `X-Last-Successful-Seed` 回传该音色**最近一次成功合成
    用的 seed**（行上的 `seed` 列为准；生成型产物行上无值时退而用旁挂元数据里
    本次生效的 seed）。两处都没有就**不发**该头 —— 不发比发假值诚实。
    """
    import asyncio

    from backend.services import voice_preview
    from backend.services.voice_service import get_voice_seed

    wait_s = voice_preview.DEFAULT_WAIT_S if wait is None else wait
    # resolve_preview 会阻塞（等合成线程）—— 放线程池，绝不占事件循环
    outcome = await asyncio.to_thread(
        voice_preview.resolve_preview, voice_id, wait_s, bool(regenerate), quality)

    if not outcome.get("ok"):
        detail = {
            "reason": outcome.get("message") or "该音色暂不可试听",
            "code": outcome.get("code"),
            "voice_id": voice_id,
            "quality": outcome.get("quality", quality),
        }
        for k in ("engine", "next", "budget_s", "elapsed_s", "seq",
                  "quality_report", "text"):
            if outcome.get(k) is not None:
                detail[k] = outcome[k]
        headers = {}
        if outcome.get("retry_after"):
            headers["Retry-After"] = str(int(outcome["retry_after"]))
        raise HTTPException(outcome.get("status", 500), detail, headers=headers or None)

    audio_path = outcome["path"]
    if not os.path.exists(audio_path):
        # 走到这里只可能是"文件刚被外部删掉"（缓存目录是数据，不是代码）
        raise HTTPException(410, {
            "reason": f"试听音频文件已不存在（{Path(audio_path).name}），请带 "
                      f"?regenerate=1 重新生成",
            "code": "file_gone", "voice_id": voice_id})

    seed = get_voice_seed(voice_id)
    if seed is None:
        seed = (outcome.get("meta") or {}).get("seed")
    meta = outcome.get("meta") or {}
    headers = {"X-Preview-Origin": outcome["origin"],
              "X-Preview-Quality": outcome.get("quality", quality),
              "X-Preview-Engine": str(outcome.get("engine") or meta.get("engine") or ""),
              "Cache-Control": "no-cache"}
    # 代理声如实标注：默认档对非 edge 音色是"最接近的 Edge 官方音色"，
    # 听的是"这个性别/年龄档位像不像"，不是终稿。
    binding = meta.get("voice_binding")
    if binding:
        headers["X-Preview-Voice-Binding"] = str(binding)
    if meta.get("edge_voice"):
        headers["X-Preview-Voice"] = str(meta["edge_voice"])
    if seed is not None:
        headers["X-Last-Successful-Seed"] = str(seed)
    return FileResponse(audio_path, media_type="audio/wav", headers=headers)


@router.post("/clone")
async def clone_voice(
    audio: UploadFile = File(...),
    name: str = Form(...),
    transcript: str = Form(None),
    language: str = Form("zh"),
    gender: str = Form(None),
    engine: str = Form("voxcpm"),
):
    """上传音频克隆音色。

    `engine` 选择该参考音频将来由哪个引擎发声（#76：新增本地即时克隆档
    "openvoice"；默认仍是 "voxcpm"，GSV/edgetts 既有行为不变）。
    未注册或不支持克隆的引擎名 → ValueError → 400，绝不建出合成期才炸的坏音色。
    """
    if not audio.filename:
        raise HTTPException(400, "请上传参考音频文件")

    try:
        result = voice_service.clone_voice(
            audio_file=audio,
            name=name,
            transcript=transcript,
            language=language,
            gender=gender,
            engine=engine,
        )
        # PY-111: 统一音色主键字段名 (与列表接口的 id/voice_id 双输出保持一致)
        if isinstance(result, dict):
            for alias_key, primary in (("voice_id", "id"), ("id", "voice_id")):
                if not result.get(alias_key) and result.get(primary):
                    result[alias_key] = result[primary]
        return {"status": "ok", "voice": result}
    except ValueError as e:
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        raise HTTPException(500, str(e))


@router.post("/analyze")
async def analyze_audio(
    audio: UploadFile = File(...),
    name: str = Form(None),
):
    """上传音频文件并提取音色特征"""
    if not audio.filename:
        raise HTTPException(400, "请上传音频文件")

    try:
        result = voice_service.analyze_audio(
            audio_file=audio,
            name=name or Path(audio.filename).stem,
        )
        return {"status": "ok", "analysis": result}
    except (ValueError, FileNotFoundError) as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"音频分析失败: {e}")


@router.post("/analyze-and-save")
async def analyze_and_save_voice(
    audio: UploadFile = File(...),
    name: str = Form(...),
    language: str = Form("zh"),
    engine: str = Form(None),
):
    """上传音频分析并保存为音色。

    US-C2: `engine` 必须是**注册表里真有的引擎名**（不传=用配置的默认引擎）。
    这里以前没有 engine 形参、服务层硬写 `engine="cloned"` —— 而 "cloned" 是
    `source` 的取值、注册表无此工厂，于是这批行一到按引擎取用就 EngineLoadError。
    非法引擎名 → ValueError → 400，不建坏行。
    """
    if not audio.filename:
        raise HTTPException(400, "请上传音频文件")

    try:
        result = voice_service.analyze_and_save_voice(
            audio_file=audio,
            name=name,
            language=language,
            engine=engine,
        )
        return {"status": "ok", "voice": result}
    except (ValueError, FileNotFoundError) as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"音频分析保存失败: {e}")


@router.post("/design")
async def design_voice(
    description: str = Body(..., description="语音描述，如：30岁温柔女声，四川话，语速稍慢"),
    name: str = Body(None, description="音色名称（可选，自动生成）"),
    category: str = Body(None, description="五类归属: human/animal/electronic/anime/mechanical（可选，默认 human）"),
):
    """语音设计：文本描述生成音色"""
    try:
        result = voice_service.design_voice(description, name, category=category)
        return {"status": "ok", "voice": result}
    except RuntimeError as e:
        raise HTTPException(500, str(e))


@router.post("/design/preview")
async def preview_design(description: str = Form(...)):
    """预览语音设计特征（不生成音色）"""
    features = voice_service.preview_design(description)
    return {"status": "ok", "features": features}


@router.get("/online/sources")
async def online_sources_status():
    """在线源实时状态（哪个源活着、哪个端点在冷却、上次真实错误）。

    纯内存读 + 只读探测：不联网、不写库。前端可用它在搜索前就说明现状。
    """
    from backend.services import voice_online
    return {"status": "ok", "sources": voice_online.sources_status()}


@router.get("/online/search")
async def search_online_voices(
    query: str = Query("", description="搜索关键词"),
    language: str = Query(None, description="语言筛选"),
    source: str = Query("all", description="来源: huggingface / github / all"),
    limit: int = Query(20, ge=1, le=50, description="每源返回条数上限"),
):
    """搜索在线音色库（HuggingFace 镜像 + GitHub），**如实**报告每个源的状态。

    #89 的两条硬要求都在这实现：
      * 不阻塞事件循环：爬虫是同步 httpx，一律 `asyncio.to_thread` 下放，外层再
        `asyncio.wait_for` 给总预算（默认 20s，`VOICE_ONLINE_ROUTE_DEADLINE_S`）。
        旧实现直接在 `async def` 里同步请求 → HF 端点不可达时整个服务停摆 93s，
        用户侧就是"完全无响应"。
      * 不编数据：所有源都不可达 -> 502 + 结构化 detail（含真实错误文本与端点
        冷却状态）；只要有一个源活着就 200，死源在 `sources[]` 里如实标出来。

    外层为什么仍走 `asyncio.to_thread`、而不是把整枚搜索丢进在线搜索的共享池：
    `search_all` 会占着一枚线程等池里的单源作业，而共享池的并行度是封顶的
    （`SEARCH_POOL_SIZE`）——外层也用它，并发一上来就是自己饿死自己。
    共享池只装**单源爬虫作业**，外层这枚线程由 asyncio 的默认线程池承担。

    超时不再是空口 504：先落一条终态记录（撤掉队列里还没开工的作业、记下"放弃时
    仍在跑"的作业数），再把记录和实时统计一起回给调用方。
    """
    import asyncio

    from backend.services import voice_online

    try:
        payload = await asyncio.wait_for(
            asyncio.to_thread(voice_online.search_all, query, language, source, limit,
                              voice_online.ROUTE_DEADLINE_S),
            timeout=voice_online.ROUTE_DEADLINE_S,
        )
    except asyncio.TimeoutError:
        record = voice_online.record_route_timeout(
            query=query, source=source, limit=limit,
            budget_s=voice_online.ROUTE_DEADLINE_S)
        raise HTTPException(504, {
            "reason": "deadline_exceeded",
            "message": f"在线音色搜索超过 {voice_online.ROUTE_DEADLINE_S:.0f}s 总预算，已中止",
            "abandoned": record["stats"],
            "next": "网络可达性或源端问题；可查看 /api/voices/online/sources 的实时状态",
        })

    if not payload.get("ok"):
        raise HTTPException(502, {
            "reason": "all_sources_unavailable",
            "message": payload.get("message") or "所有在线源都不可用",
            "sources": payload.get("sources", []),
            "next": "HuggingFace 官方端点在本机不可达时会自动改用镜像；镜像也不通就是网络出口问题",
        })
    return {"status": "ok", **payload}


@router.get("/online/{model_name:path}/detail")
async def get_online_voice_detail(
    model_name: str,
    source: str = Query("huggingface"),
):
    """在线条目详情 + **可下载音频文件清单**（下载链路能不能走通，这里就能看出来）。"""
    import asyncio

    from backend.services import voice_online

    detail = await asyncio.to_thread(voice_online.detail, model_name, source)
    if detail.get("ok") is False:
        raise HTTPException(detail.get("status") or 502, {
            "reason": detail.get("reason") or "unreachable",
            "message": detail.get("message") or "在线源详情获取失败",
            "model_name": model_name,
            "source": source,
            "http_code": detail.get("http_code", 0),
            "attempts": detail.get("attempts", []),
        })
    return {"status": "ok", "detail": detail}


@router.post("/online/download")
async def download_voice(
    model_name: str = Body(..., description="模型/仓库名称，如 hexgrad/Kokoro-82M"),
    engine: str = Body("voxcpm", description="登记到哪个引擎（必须是注册表里的键）"),
    source: str = Body("huggingface", description="huggingface / github"),
    file_path: str = Body(None, description="仓库内音频文件路径；不填自动挑最小样本"),
    name: str = Body(None, description="音色显示名（可选）"),
):
    """下载在线音色：**必须真的拿到音频文件**才登记入库。

    与旧行为的差别（这就是"拿不到音色文件"的修复点）：
      * 旧实现先建一行 `is_downloaded=0` 的记录，再用 `snapshot_download()` 直连
        官方 HF（本机不可达）—— 结果是留下一条永远下不完的坏行 + `status=failed`。
      * 现在：列仓库文件 → 走实测可用端点流式下载（体积/时间双上限）→ magic 校验
        → 解码归一化成 48k 单声道 PCM16 wav → 时长 + P0-4 质量双门 → **成功才
        登记 1 行**；任何一步失败都不写库，并把真实原因结构化返回。
    """
    import asyncio

    from backend.services import voice_online

    result = await asyncio.to_thread(
        voice_online.download_and_register, model_name, engine, source, file_path, name)
    if not result.get("ok"):
        raise HTTPException(result.get("status") or 502, {
            "reason": result.get("reason") or "download_failed",
            "message": result.get("message") or "在线音色下载失败",
            "model_name": model_name,
            "source": source,
            "candidates": result.get("candidates", []),
            "http_code": result.get("http_code", 0),
            "attempts": result.get("attempts", []),
            "next": "换 source / 指定 file_path（见 /api/voices/online/{name}/detail 的 audio_files）",
        })
    return {"status": "ok", "download": result}


@router.delete("/{voice_id}")
async def delete_voice(voice_id: str):
    """删除音色"""
    if voice_service.delete_voice(voice_id):
        return {"status": "ok"}
    raise HTTPException(404, "音色不存在")