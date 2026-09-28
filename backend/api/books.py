"""
电子书管理 API 路由
"""

import asyncio
import errno
import shutil

from fastapi import APIRouter, UploadFile, File, HTTPException, Query
from pydantic import BaseModel, Field
from typing import Optional, Union
from backend.services.book_service import book_service

router = APIRouter()


class ChapterUpdate(BaseModel):
    """单个章节更新请求"""
    title: Optional[str] = None
    content: Optional[str] = None
    index: Optional[int] = None


class ChapterMergeRequest(BaseModel):
    """章节合并请求"""
    chapter_ids: list[str]
    new_title: str


class ChapterSplitRequest(BaseModel):
    """章节拆分请求"""
    positions: list[int]


class ChapterData(BaseModel):
    """PUT /books/{id}/chapters 的单章条目 (WF-22)。

    只有 `id` 是必填（它决定改哪一行）；title/index/content 省略即**不改**，
    所以序列化时用 `exclude_unset=True`（见 update_chapters）。
    """
    id: str
    title: Optional[str] = None
    index: Optional[int] = None
    content: Optional[str] = None


class ChaptersUpdateRequest(BaseModel):
    """整章批量更新的请求体 (WF-22)。

    `chapters_data` 至少 1 条：把一本书的章节清空不是本端点的语义
    （服务层逐条按 id 更新，空列表是 no-op —— WF-16 §④-1 记录的"0 章书
    不可构造"正是此意），故显式 422 而不是静默 no-op。
    """
    chapters_data: list[ChapterData] = Field(
        ..., min_length=1,
        description="章节更新条目，至少 1 条；清空全部章节不被支持")


@router.put("/{book_id}/chapters")
async def update_chapters(
    book_id: str,
    payload: Union[ChaptersUpdateRequest, list[ChapterData]],
):
    """更新章节（重命名、调整顺序、批量改内容）—— Pydantic 请求体 (WF-22)。

    旧签名 `async def update_chapters(book_id: str, chapters_data: list)` 里
    的 `chapters_data` 既不是 Pydantic 模型也没标 `Body(...)`，FastAPI 只能把它
    当**查询参数**看待：于是前端 `api.updateChapters()` 发 JSON 数组 → 422
    `chapters_data Field required`、发 multipart → 500、发 `{"chapters_data":[]}`
    → 422（复测三种形态全部失败，只是"零脏写"）。现在两种请求形态都收：
      * 裸数组 `[{"id": "...", "title": "..."}]` —— 前端 `api.updateChapters` 的现状；
      * 对象 `{"chapters_data": [...]}` —— 与旧形参名一致的显式形态。
    其它不相关字段会被 pydantic 拒为 422（此前是 500 全家）。
    """
    if not book_service.book_exists(book_id):
        raise HTTPException(404, "书籍不存在")
    items = (payload.chapters_data if isinstance(payload, ChaptersUpdateRequest)
             else payload)
    if not items:
        # 裸数组形态绕过了 ChaptersUpdateRequest 的 min_length，这里补同一道门：
        # "清空全部章节"不是本端点的语义，静默 no-op 会让调用方以为删掉了。
        raise HTTPException(
            422,
            detail=[{
                "loc": ["body"],
                "msg": "chapters_data 至少 1 条；本端点不支持清空全部章节",
                "type": "value_error.too_short",
            }])
    # exclude_unset: 客户端没写的字段保持库内原值（服务层用 .get(key, 原值) 兜底）
    chapters_data = [c.model_dump(exclude_unset=True) for c in items]
    try:
        book_service.update_chapters(book_id, chapters_data)
        return {"status": "ok", "updated": len(chapters_data)}
    except Exception as e:
        raise HTTPException(500, str(e))


async def _spool_upload(file: UploadFile, spool: str) -> None:
    """把上传流分块写进中转件，边写边查体积与磁盘余量 (U-8)。

    旧写法是 `content = await file.read()`（最多 500MB 整份进内存）+ `seek(0)`
    + 在事件循环里同步 `copyfileobj` 落盘。分块之后峰值内存 = 一块，且写盘
    每块都让出循环。
    """
    from backend.config import Config
    max_bytes = Config.MAX_UPLOAD_SIZE_MB * 1024 * 1024
    free = shutil.disk_usage(str(book_service.spool_dir())).free
    if free < Config.MIN_FREE_DISK_MB * 1024 * 1024:
        raise HTTPException(
            507, f"磁盘剩余空间不足（{free // 1024 // 1024}MB < "
                 f"MIN_FREE_DISK_MB={Config.MIN_FREE_DISK_MB}MB），暂不接收上传")
    written = 0
    try:
        with open(spool, "wb") as out:
            while True:
                chunk = await file.read(book_service.UPLOAD_CHUNK_BYTES)
                if not chunk:
                    break
                written += len(chunk)
                # 超限即断流：不再把剩下的字节拷到盘上（旧实现是先整份读进内存
                # 再比大小，等于超限文件也照样吃了带宽和堆）。
                if written > max_bytes:
                    raise HTTPException(
                        400, f"文件过大，最大支持 {Config.MAX_UPLOAD_SIZE_MB}MB")
                out.write(chunk)
    except OSError as e:
        if e.errno == errno.ENOSPC:
            raise HTTPException(507, f"磁盘写入失败（空间不足）: {e}") from e
        raise


@router.post("/import")
async def import_book(
    file: UploadFile = File(...),
    encoding: str = Query("auto", description="文本编码（仅对 txt 格式有效，如 utf-8, gbk, big5）"),
):
    """导入电子书文件 (U-8: 流式中转 + 解析整体出事件循环)。

    旧实现在 `async def` 里干了三件阻塞事件循环的事：整份 `await file.read()`
    （≤500MB 进堆）、同步 `copyfileobj` 落盘、以及 `book_service.import_book`
    的解析 + `scan_chapters` + 逐章 INSERT。导入一本大书期间 `/api/health`
    这类轻量端点会一起卡住（红线①）。现在：上传分块落中转件，入库整体
    `asyncio.to_thread`，循环只在 `await` 处呼吸。
    """
    if not file.filename:
        raise HTTPException(400, "请选择文件")

    ext = f".{file.filename.rsplit('.', 1)[-1].lower()}" if '.' in file.filename else ''
    from modules.book_parser.parser_factory import ParserFactory
    supported = sorted(ParserFactory.FORMAT_MAP.keys())
    if ext not in ParserFactory.FORMAT_MAP:
        raise HTTPException(400, f"不支持的文件格式: {ext}（支持: {', '.join(supported)}）")

    spool = book_service.new_spool(ext)
    try:
        await _spool_upload(file, spool)
        book = await asyncio.to_thread(
            book_service.import_book_from_path, spool, file.filename, None,
            encoding if encoding != "auto" else None)
        return {"status": "ok", "book": book}
    except ValueError as e:
        # 用户可修正的错误（格式不受支持/超出字数上限/无可用章节/文件损坏）-> 400
        # WF-04: book_service 把「解析后无可用章节」「文件损坏或加密」也归到
        # BookImportError(ValueError 子类) 这条路径，且 detail 里回显的是用户上传
        # 文件名而非存储 UUID。
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        # 仅剩真·服务端错误（依赖缺失/未预期异常）-> 500
        raise HTTPException(500, str(e))
    finally:
        # 成功时 import_book_from_path 已把中转件搬走，这里是 no-op；
        # 失败（含 HTTPException）时保证不留半截上传。
        book_service.discard_spool(spool)


@router.post("/import/batch")
async def import_books_batch(files: list[UploadFile] = File(...)):
    """批量导入电子书 (U-8: 整批出事件循环)。

    逐份走 `book_service.import_book` 的薄壳（内部同样先落中转件再搬家），
    批处理本身就是同步语义，所以整体 `to_thread`——N 本书的解析期不再
    把循环按住 N 次。
    """
    results = await asyncio.to_thread(book_service.import_batch, files)
    return {"status": "ok", "results": results}


@router.get("/")
async def list_books(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0, description="分页偏移 (WF-23)"),
):
    """获取电子书列表 (WF-09: 2 条 SQL，不再逐书懒加载章节)。

    WF-23: 旧实现 `total = len(books)` 是**本页条数**（库里 104 本时
    limit=5 也报 total=5），且路由没有 offset 形参、翻不了页。现在
    `total` 是不带筛选条件的 SQL `COUNT(*)`、支持 offset，并回显
    `limit/offset/returned` —— 口径与已修好的 WF-12 音色列表
    (`backend/api/voices.py:29-59` + `voice_store.count()`) 完全一致。
    `books/total` 两个既有键的名与类型不变。

    PERF-IDX: 两条 SELECT 都是同步 SQLAlchemy，`async def` 里直接调就把事件循环
    按在数据库上（与 U-8 导入路径同一个病），一律 `asyncio.to_thread` 下放。
    """
    books = await asyncio.to_thread(book_service.list_books, limit, offset)
    total = await asyncio.to_thread(book_service.count_books)
    return {
        "status": "ok",
        "books": books,
        "total": total,
        "limit": limit,
        "offset": offset,
        "returned": len(books),
    }


@router.get("/{book_id}")
async def get_book(book_id: str):
    """获取书籍详情 (PERF-IDX: DB 读下线程池，响应结构不变)"""
    book = await asyncio.to_thread(book_service.get_book, book_id)
    if not book:
        raise HTTPException(404, "书籍不存在")
    return {"status": "ok", "book": book}


def _chapter_rows_for(book_id: str):
    """存在性校验 + 章节预览投影，一次线程调用读完 (PERF-IDX)。

    返回 `(书是否存在, 章节行)`：把两条 SELECT 收进同一个同步壳里，路由只
    `await` 一次，语义与旧的 `if not book_exists: 404` 逐字一致。
    """
    if not book_service.book_exists(book_id):
        return False, []
    return True, book_service.get_chapter_previews(book_id)


@router.get("/{book_id}/chapters")
async def get_chapters(book_id: str):
    """获取章节列表（只下发 content_preview）。

    WF-16: 旧实现对不存在的 book_id 返回 `200 {"chapters":[],"total":0}`，
    与「书存在但没章节」不可区分 -> 先做存在性校验，不存在直接 404。
    WF-09: 列表用 `get_chapter_previews`（SQL 侧 substr 前 200 字），不再把整本书
    正文塞进响应；键集不变，前端需要正文时走
    `GET /api/books/chapters/{chapter_id}/content` 懒加载（EditPage 已是此用法）。
    PERF-IDX: 这条是最大那本书（1626 章）的热点，SELECT 下线程池执行。
    """
    exists, chapters = await asyncio.to_thread(_chapter_rows_for, book_id)
    if not exists:
        raise HTTPException(404, "书籍不存在")
    return {"status": "ok", "chapters": chapters, "total": len(chapters)}


@router.get("/chapters/{chapter_id}/content")
async def get_chapter_content(chapter_id: str):
    """获取章节完整内容 (PERF-IDX: DB 读下线程池)"""
    content = await asyncio.to_thread(book_service.get_chapter_content, chapter_id)
    if content is None:
        raise HTTPException(404, "章节不存在")
    return {"status": "ok", "content": content}


@router.patch("/chapters/{chapter_id}")
async def update_chapter(chapter_id: str, update: ChapterUpdate):
    """更新单个章节（标题、内容、序号）(PERF-IDX: 写库下线程池，异常口径不变)"""
    try:
        ch = await asyncio.to_thread(
            book_service.update_chapter,
            chapter_id,
            title=update.title,
            content=update.content,
            index=update.index,
        )
        return {"status": "ok", "chapter": ch}
    except ValueError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        raise HTTPException(500, str(e))


@router.post("/{book_id}/chapters/merge")
async def merge_chapters(book_id: str, body: ChapterMergeRequest):
    """合并多个章节"""
    try:
        new_id = book_service.merge_chapters(book_id, body.chapter_ids, body.new_title)
        return {"status": "ok", "new_chapter_id": new_id}
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/chapters/{chapter_id}/split")
async def split_chapter(chapter_id: str, body: ChapterSplitRequest):
    """拆分章节"""
    try:
        new_ids = book_service.split_chapter(chapter_id, body.positions)
        return {"status": "ok", "new_chapter_ids": new_ids}
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.delete("/{book_id}")
async def delete_book(book_id: str):
    """删除电子书。

    WF-10: 关联配音项目同事务置 `status='book_missing'`，其 id 在响应的
    `projects_marked` 里回显（`status:"ok"` 契约不变，仅新增字段）。
    """
    result = book_service.delete_book(book_id)
    return {"status": "ok", **result}