"""
电子书处理服务
连接 API 层与解析模块
"""

import os
import re
import errno
import shutil
import uuid
import time
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from sqlalchemy import func
from sqlalchemy.exc import OperationalError

from backend.config import Config
from backend.models.base import get_session
from backend.models.book import Book, Chapter, chapter_preview_dict, PREVIEW_CHARS
from modules.book_parser.parser_factory import ParserFactory
from modules.book_parser.text_quality import scan_chapters, summarize

logger = logging.getLogger(__name__)

# SQLite 并发访问重试配置
_MAX_RETRIES = 3
_RETRY_DELAY = 0.1  # 秒

# 存储文件名是 UUID（导入时已改名），消息里出现即对用户无意义 -> 一律换成上传名 (WF-04)
_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")

# 解析层"用户可修正"错误特征串：命中即归 400，不命中才保留 500 (WF-04)。
# 依据: modules/book_parser/parser_factory.py 无可用章节文案、
#       parsers/epub_parser.py「无法解析 EPUB 文件（可能损坏或加密）」等。
USER_FIXABLE_MARKERS = (
    "未找到任何可读章节",
    "无法解析",
    "损坏",
    "加密",
    "bad zip",
    "not a zip",
    "文件为空",
    "不支持的文件格式",
    "文件格式",
    "无法读取",
    "不是有效",
    "No such file",
)


class BookImportError(ValueError):
    """用户可自行修正的导入错误 (WF-04)。

    是 ValueError 的子类，因此 `backend/api/books.py` 既有的
    `except ValueError -> HTTPException(400)` 分支天然接得住，
    同时携带 original_filename 以便消息里回显用户上传文件名而非存储 UUID。
    """

    def __init__(self, message: str, original_filename: Optional[str] = None):
        super().__init__(message)
        self.original_filename = original_filename


class BookParseUserError(BookImportError):
    """解析后无可用章节 / 文件损坏或加密 (WF-04)：400 而非 500。"""


def _display_filename(original_filename: Optional[str]) -> str:
    """把上传名收敛成安全的展示名（去掉客户端可能带来的目录前缀）。"""
    name = (original_filename or "").replace("\\", "/").rstrip("/").split("/")[-1].strip()
    return name or "上传文件"


def _scrub_storage_names(message: str, saved_path: Path, original_filename: Optional[str]) -> str:
    """把错误消息里的存储路径/UUID 文件名替换成用户上传文件名 (WF-04)。"""
    shown = _display_filename(original_filename)
    text = str(message)
    candidates = set()
    if saved_path is not None:
        candidates |= {str(saved_path), saved_path.name, saved_path.stem}
    for token in sorted(candidates, key=len, reverse=True):
        if token and token in text:
            text = text.replace(token, shown)
    # 兜底：解析器可能拼出别的 UUID 形态文件名（临时目录等）
    return _UUID_RE.sub(shown, text)


def _is_user_fixable(exc: BaseException) -> bool:
    """判定解析异常是否属于"用户改文件就能解决"的类别 (WF-04)。"""
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(marker.lower() in text for marker in USER_FIXABLE_MARKERS)


def _retry_on_lock(func):
    """装饰器：SQLite 并发锁定时自动重试"""
    def wrapper(*args, **kwargs):
        last_error = None
        for attempt in range(_MAX_RETRIES):
            try:
                return func(*args, **kwargs)
            except OperationalError as e:
                last_error = e
                if "database is locked" in str(e).lower() and attempt < _MAX_RETRIES - 1:
                    time.sleep(_RETRY_DELAY * (attempt + 1))
                    continue
                raise
        raise last_error
    return wrapper


class BookService:
    """电子书处理服务"""

    # 上传落盘目录：与 data/books 同卷，`os.replace` 才能真正是"移动"而非再拷一份。
    SPOOL_DIRNAME = ("tmp", "uploads")
    UPLOAD_CHUNK_BYTES = 1024 * 1024

    def spool_dir(self) -> Path:
        return Config.DATA_DIR.joinpath(*self.SPOOL_DIRNAME)

    def new_spool(self, ext: str = "") -> str:
        """开一枚上传中转文件（路由分块流式写入，写满/写失败由调用方 discard）。"""
        d = self.spool_dir()
        d.mkdir(parents=True, exist_ok=True)
        return str(d / f"spool_{uuid.uuid4().hex}{ext or ''}")

    def discard_spool(self, path) -> None:
        """删中转文件。删不掉不外抛——它只是临时件，且失败点常在主错误路径上，
        把主异常换成一个 CleanupError 反而是帮倒忙。"""
        try:
            if path and os.path.exists(path):
                os.remove(path)
        except OSError as e:
            logger.warning(f"[BookService] 上传中转件未清: {path} ({e})")

    def _relocate(self, src: str, dst: Path) -> None:
        """把中转件搬到最终存储名：**同卷用 os.replace（0 字节拷贝），
        跨卷才退回 copy2+remove**。

        统一走 `SPOOL_DIRNAME` 下的中转（`Config.DATA_DIR` 同卷），所以
        replace 是常态路径；原来 `shutil.copyfileobj` 把 500MB 上限的书再
        拷一遍存储名，正是事件循环上最长的一次同步阻塞。
        """
        dst = Path(dst)
        try:
            os.replace(src, str(dst))
        except OSError as e:
            if e.errno != errno.EXDEV:
                raise
            logger.warning(f"[BookService] 跨卷搬家: {src} -> {dst}，退回拷贝")
            shutil.copy2(src, dst)
            self.discard_spool(src)

    def import_book(self, file, storage_dir: str = None, encoding: str = None) -> dict:
        """导入电子书（旧签名，收 UploadFile）。

        U-8 后这只是 `import_book_from_path` 的一层薄壳：真正的入库流程以**磁盘路径**
        为入参，路由那条路先把上传流式落到中转文件再调它，于是 500MB 上传不再整份进
        内存、也不再在事件循环里 `copyfileobj`。走这层壳的调用方（批量导入、脚本）
        仍是同步语义，必须在**非事件循环线程**里调（路由用 `asyncio.to_thread`）。
        """
        upload_name = getattr(file, "filename", None) or ""
        ext = Path(upload_name).suffix.lower()
        spool = self.new_spool(ext)
        try:
            src = file.file
            try:
                src.seek(0)      # 薄壳的调用方（批量导入）可能已经把指针读到过末尾
            except (OSError, ValueError):
                pass
            with open(spool, "wb") as f:
                shutil.copyfileobj(src, f)
            return self.import_book_from_path(spool, upload_name,
                                             storage_dir=storage_dir, encoding=encoding)
        finally:
            self.discard_spool(spool)   # 已被 os.replace 搬走时这里就是 no-op

    def import_book_from_path(self, spool_path, upload_name: str = None,
                              storage_dir: str = None, encoding: str = None) -> dict:
        """从**已落盘**的上传件入库：1 搬到存储名 2 解析 3 字数/乱码校验 4 入库。

        `spool_path` 成功后不再存在（移动语义）。整段是同步阻塞代码——放线程池里调。

        Args:
            spool_path: 上传件在磁盘上的路径（路由分块写入的中转文件）
            upload_name: 用户上传名（仅用于回显/报错与存储后缀，存储名仍是 UUID）
            storage_dir: 文件存储目录
            encoding: 文本编码（仅对 txt 格式有效）
        """
        storage_dir = Path(storage_dir or Config.DATA_DIR / "books")
        storage_dir.mkdir(parents=True, exist_ok=True)

        # 1. 就位：移动而非再拷一份（拷一份 = 500MB 的量级白写一遍磁盘）
        upload_name = upload_name or Path(str(spool_path)).name
        spool = str(spool_path)
        ext = Path(upload_name).suffix.lower()
        book_id = str(uuid.uuid4())
        saved_path = storage_dir / f"{book_id}{ext}"
        self._relocate(spool, saved_path)

        # 1b. 大小/字数上限校验 (PY-103): 先查字节数, 避免超大文件白白解析
        max_bytes = Config.MAX_UPLOAD_SIZE_MB * 1024 * 1024
        file_size = saved_path.stat().st_size
        if file_size > max_bytes:
            os.remove(saved_path)
            raise BookImportError(
                _scrub_storage_names(
                    f"文件过大: {file_size / 1024 / 1024:.1f}MB 超过上限 "
                    f"{Config.MAX_UPLOAD_SIZE_MB}MB (MAX_UPLOAD_SIZE_MB)",
                    saved_path, upload_name),
                original_filename=upload_name)

        # 2. 解析
        # WF-04: 旧行为是「除 ValueError 外一律包成 RuntimeError -> 500」，于是
        # 「空文本无可用章节」「EPUB 损坏」这类用户改文件就能解决的问题也报
        # 服务器错误，且消息里带的是存储用的 UUID 文件名。现在按可修正性分流：
        #   可修正 -> BookParseUserError(ValueError 子类) -> 路由 400
        #   不可修正 -> RuntimeError -> 路由 500
        # 两条路径都把消息里的存储名换成用户上传文件名。
        try:
            book_data = ParserFactory.parse(str(saved_path), encoding=encoding)
        except ValueError as e:
            os.remove(saved_path)
            raise BookImportError(
                _scrub_storage_names(str(e), saved_path, upload_name),
                original_filename=upload_name) from None
        except Exception as e:
            os.remove(saved_path)
            message = _scrub_storage_names(str(e), saved_path, upload_name)
            if _is_user_fixable(e):
                raise BookParseUserError(
                    f"{message}\n  - 请检查文件内容后重新上传（{upload_name or '上传文件'}）",
                    original_filename=upload_name) from None
            logger.warning(f"[BookService] 解析失败(服务端): {type(e).__name__}: {e}")
            raise RuntimeError(
                f"电子书解析失败: {message}",
            ) from e

        # 2b. 字数上限校验 (PY-103): MAX_BOOK_CHARS 生效, 超限不入库
        if book_data.total_chars and book_data.total_chars > Config.MAX_BOOK_CHARS:
            os.remove(saved_path)
            raise BookImportError(
                _scrub_storage_names(
                    f"书籍内容 {book_data.total_chars} 字超过上限 {Config.MAX_BOOK_CHARS} 字"
                    f" (MAX_BOOK_CHARS)，请拆分后分批导入",
                    saved_path, upload_name),
                original_filename=upload_name)

        # 2c. 源文件自带乱码可见化 (PY-326)：只统计不改正文，计数随 extra_data 回读，
        #     并在日志里点名最脏的章 —— 免得原文件里的 U+FFFD 一路无声送到合成。
        quality_report = scan_chapters(book_data.chapters,
                                       title=book_data.title, author=book_data.author)
        book_data.metadata = {**(book_data.metadata or {}), "text_quality": quality_report}
        if quality_report["dirty"]:
            logger.warning(
                f"[BookService] {upload_name or book_data.title}: "
                f"源载荷自带乱码 -> {summarize(quality_report)}"
                "（解析器是否新增见 qa/harness/run_encoding_matrix.py 口径）")

        # 3. 保存到数据库
        session = get_session()
        try:
            book = Book(
                id=book_id,
                title=book_data.title or Path(upload_name or str(saved_path)).stem,
                author=book_data.author or "",
                format=book_data.format,
                file_path=str(saved_path),
                cover_path=book_data.cover_path or "",
                total_chars=book_data.total_chars,
                language=book_data.language,
                extra_data=book_data.metadata,
            )
            session.add(book)

            saved_chapter_count = 0
            saved_total_chars = 0
            for ch in book_data.chapters:
                chapter = Chapter(
                    id=str(uuid.uuid4()),
                    book_id=book_id,
                    title=ch["title"],
                    index=ch["index"],
                    content=ch["content"],
                    char_count=ch["char_count"],
                )
                session.add(chapter)
                saved_chapter_count += 1
                saved_total_chars += ch["char_count"]

            session.commit()
            logger.info(
                f"[BookService] 入库完成: {book_data.title} "
                f"({saved_chapter_count} 章, {saved_total_chars} 字)"
            )
            return book.to_dict()
        finally:
            session.close()

    def import_batch(self, files: list, encoding: str = None) -> list:
        """批量导入"""
        results = []
        for file in files:
            try:
                result = self.import_book(file, encoding=encoding)
                results.append({"success": True, "book": result})
            except Exception as e:
                results.append({"success": False, "filename": file.filename, "error": str(e)})
        return results

    @_retry_on_lock
    def get_book(self, book_id: str) -> Optional[dict]:
        """获取书籍详情"""
        session = get_session()
        try:
            book = session.query(Book).filter_by(id=book_id).first()
            return book.to_dict() if book else None
        finally:
            session.close()

    @_retry_on_lock
    def list_books(self, limit: int = 50, offset: int = 0) -> list:
        """列出书籍 (WF-09: 固定 2 条 SQL，不再有 N+1；WF-23: 支持 offset)。

        旧实现 `[b.to_dict() for b in books]` 里 `Book.to_dict` 读
        `len(self.chapters)`，每本触发一条 chapters SELECT 并把整本书正文读进内存
        ——50 本实测 3.5-4.6 s（≈0.068 s/行）。现在：
          1) 取书籍行（不带关系）
          2) 一次 `GROUP BY book_id` 拿到本章数
        响应字段名与 to_dict() 逐项一致，前端无需改动。
        """
        session = get_session()
        try:
            books = (
                session.query(Book)
                .order_by(Book.created_at.desc())
                .offset(max(0, int(offset)))
                .limit(limit)
                .all()
            )
            if not books:
                return []
            counts = dict(
                session.query(Chapter.book_id, func.count(Chapter.id))
                .filter(Chapter.book_id.in_([b.id for b in books]))
                .group_by(Chapter.book_id)
                .all()
            )
            return [b.to_list_dict(chapter_count=counts.get(b.id, 0)) for b in books]
        finally:
            session.close()

    @_retry_on_lock
    def count_books(self) -> int:
        """书籍总数 (WF-23)：SQL `COUNT(*)`，不是本页条数。

        口径照抄 WF-12 的 `voice_store.count()`（modules/voice_manager/
        voice_store.py:107-118），使 books 与 voices 两个列表端点的
        `total` 语义一致。
        """
        session = get_session()
        try:
            return int(session.query(func.count(Book.id)).scalar() or 0)
        finally:
            session.close()

    @_retry_on_lock
    def book_exists(self, book_id: str) -> bool:
        """书籍存在性校验 (WF-16)：只 SELECT id，不加载章节。"""
        session = get_session()
        try:
            return session.query(Book.id).filter_by(id=book_id).first() is not None
        finally:
            session.close()

    @_retry_on_lock
    def get_chapters(self, book_id: str) -> list:
        """获取书籍章节列表（含整段正文）。

        注意：`modules/workflow/conversion_workflow.py:287` 的 P4 阶段依赖这里的
        `content` 全量，因此这个方法保持原契约不变；只给前端/接口用的预览版
        序列化见 `get_chapter_previews()` (WF-09)。
        """
        session = get_session()
        try:
            chapters = (
                session.query(Chapter)
                .filter_by(book_id=book_id)
                .order_by(Chapter.index)
                .all()
            )
            return [c.to_dict() for c in chapters]
        finally:
            session.close()

    @_retry_on_lock
    def get_chapter_previews(self, book_id: str) -> list:
        """章节列表（预览版, WF-09）：整段正文不进 Python，SQL 里只 substr 前 200 字。

        返回行的键集与 `Chapter.to_dict()` 完全一致（`content` 为 None），
        正文本身请用 `GET /api/books/chapters/{chapter_id}/content` 懒加载
        ——前端 EditPage/DubbingPage 已是这个用法（只读 content_preview）。
        """
        session = get_session()
        try:
            rows = (
                session.query(
                    Chapter.id, Chapter.book_id, Chapter.title, Chapter.index,
                    Chapter.char_count, Chapter.extra_data,
                    func.substr(Chapter.content, 1, PREVIEW_CHARS),
                )
                .filter(Chapter.book_id == book_id)
                .order_by(Chapter.index)
                .all()
            )
            return [chapter_preview_dict(*row) for row in rows]
        finally:
            session.close()

    @_retry_on_lock
    def get_chapter_content(self, chapter_id: str) -> Optional[str]:
        """获取章节完整内容"""
        session = get_session()
        try:
            chapter = session.query(Chapter).filter_by(id=chapter_id).first()
            return chapter.content if chapter else None
        finally:
            session.close()

    def update_chapter(self, chapter_id: str, title: str = None, content: str = None, index: int = None):
        """更新单个章节 - 返回更新后的章节字典
        """
        session = get_session()
        try:
            chapter = session.query(Chapter).filter_by(id=chapter_id).first()
            if not chapter:
                raise ValueError("章节不存在")
            if title is not None:
                chapter.title = title
            if content is not None:
                chapter.content = content
            if index is not None:
                chapter.index = index
            chapter.char_count = len(chapter.content) if chapter.content else 0
            session.commit()
            return chapter.to_dict()
        finally:
            session.close()

    def update_chapters(self, book_id: str, chapters_data: list):
        """
        更新章节信息（合并、拆分、重命名、批量更新）
        chapters_data: [{"id": str, "title": str, "index": int, "content": str}, ...]
        """
        session = get_session()
        try:
            for ch_data in chapters_data:
                chapter = session.query(Chapter).filter_by(id=ch_data.get("id")).first()
                if chapter:
                    chapter.title = ch_data.get("title", chapter.title)
                    chapter.index = ch_data.get("index", chapter.index)
                    chapter.content = ch_data.get("content", chapter.content)
                    chapter.char_count = len(chapter.content) if chapter.content else 0
            session.commit()
        finally:
            session.close()

    @_retry_on_lock
    def delete_book(self, book_id: str) -> dict:
        """删除书籍及关联数据。

        WF-10: 旧实现只删文件 + `session.delete(book)`，`dubbing_projects.book_id`
        既无 relationship 也无 cascade（SQLite 默认不开 foreign_keys），于是删书后
        项目仍 200 可读、`status` 还是 `draft`，指向一本已不存在的书。
        现在同一事务内把引用该书的项目置 `status='book_missing'`（保留角色/分配等
        历史数据供回看），并在 `extra_data.book_missing` 留痕原状态与书名。

        返回 {"deleted": bool, "projects_marked": [project_id, ...]}。
        """
        from backend.models.project import DubbingProject, STATUS_BOOK_MISSING

        session = get_session()
        try:
            book = session.query(Book).filter_by(id=book_id).first()
            if not book:
                return {"deleted": False, "projects_marked": []}

            marked: list = []
            orphaned = (
                session.query(DubbingProject)
                .filter(DubbingProject.book_id == book_id)
                .all()
            )
            now = datetime.now(timezone.utc).isoformat()
            for project in orphaned:
                if project.status != STATUS_BOOK_MISSING:
                    extra = dict(project.extra_data or {})
                    extra["book_missing"] = {
                        "since": now,
                        "previous_status": project.status,
                        "book_id": book_id,
                        "book_title": book.title,
                    }
                    project.extra_data = extra
                    project.status = STATUS_BOOK_MISSING
                    marked.append(project.id)

            # 删除文件
            if book.file_path and os.path.exists(book.file_path):
                try:
                    os.remove(book.file_path)
                except Exception:
                    pass
            if book.cover_path and os.path.exists(book.cover_path):
                try:
                    os.remove(book.cover_path)
                except Exception:
                    pass
            session.delete(book)
            session.commit()
            if marked:
                logger.info(
                    f"[BookService] 删书 {book_id} -> {len(marked)} 个项目置 "
                    f"{STATUS_BOOK_MISSING}: {[m[:8] for m in marked]}")
            return {"deleted": True, "projects_marked": marked}
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def merge_chapters(self, book_id: str, chapter_ids: list, new_title: str) -> str:
        """合并多个章节"""
        session = get_session()
        try:
            chapters = (
                session.query(Chapter)
                .filter(Chapter.id.in_(chapter_ids))
                .order_by(Chapter.index)
                .all()
            )
            if not chapters:
                raise ValueError("章节不存在")

            merged_content = "\n\n".join(
                f"## {ch.title}\n\n{ch.content}" for ch in chapters
            )
            new_chapter = Chapter(
                id=str(uuid.uuid4()),
                book_id=book_id,
                title=new_title,
                index=chapters[0].index,
                content=merged_content,
                char_count=len(merged_content),
            )
            for ch in chapters:
                session.delete(ch)
            session.add(new_chapter)
            session.commit()
            return new_chapter.id
        finally:
            session.close()

    def split_chapter(self, chapter_id: str, split_positions: list) -> list:
        """
        拆分章节
        split_positions: 拆分的字符位置列表
        """
        session = get_session()
        try:
            chapter = session.query(Chapter).filter_by(id=chapter_id).first()
            if not chapter:
                raise ValueError("章节不存在")

            content = chapter.content
            new_ids = []
            last_pos = 0
            idx = chapter.index

            for pos in sorted(split_positions):
                if pos > len(content):
                    break
                part = content[last_pos:pos].strip()
                if part:
                    new_ch = Chapter(
                        id=str(uuid.uuid4()),
                        book_id=chapter.book_id,
                        title=f"{chapter.title}({idx + 1})",
                        index=idx,
                        content=part,
                        char_count=len(part),
                    )
                    session.add(new_ch)
                    new_ids.append(new_ch.id)
                    idx += 1
                last_pos = pos

            # 剩余部分
            remaining = content[last_pos:].strip()
            if remaining:
                new_ch = Chapter(
                    id=str(uuid.uuid4()),
                    book_id=chapter.book_id,
                    title=f"{chapter.title}({idx + 1})",
                    index=idx,
                    content=remaining,
                    char_count=len(remaining),
                )
                session.add(new_ch)
                new_ids.append(new_ch.id)

            session.delete(chapter)
            session.commit()
            return new_ids
        finally:
            session.close()


# 全局单例
book_service = BookService()