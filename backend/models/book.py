"""
电子书与章节数据模型
"""

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import Column, String, Integer, DateTime, ForeignKey, JSON, Text, Index
from sqlalchemy.orm import relationship
from backend.models.base import Base


# 列表响应里正文预览的截断长度（与 Chapter.to_dict 的 200 字口径一致）
PREVIEW_CHARS = 200


class Book(Base):
    """电子书模型"""
    __tablename__ = "books"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    title = Column(String, nullable=False, comment="书名")
    author = Column(String, comment="作者")
    format = Column(String, comment="文件格式: epub/mobi/pdf/txt/azw3/chm")
    file_path = Column(String, comment="原始文件路径")
    cover_path = Column(String, comment="封面图片路径")
    total_chars = Column(Integer, default=0, comment="总字符数")
    language = Column(String, default="zh", comment="主要语言")
    extra_data = Column(JSON, default=dict, comment="扩展元数据")
    created_at = Column(DateTime, default=datetime.utcnow, comment="创建时间")
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, comment="更新时间")

    # 关联章节（级联删除）
    chapters = relationship("Chapter", back_populates="book", cascade="all, delete-orphan")

    def to_dict(self):
        """转换为字典"""
        return {
            "id": self.id,
            "title": self.title,
            "author": self.author,
            "format": self.format,
            "file_path": self.file_path,
            "cover_path": self.cover_path,
            "total_chars": self.total_chars,
            "language": self.language,
            "chapter_count": len(self.chapters) if self.chapters else 0,
            "extra_data": self.extra_data,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

    def to_list_dict(self, chapter_count=None):
        """列表专用序列化 (WF-09 书籍列表 N+1)。

        输出**键集与 to_dict() 逐项一致**（前端按现有字段消费，字段名不删不改），
        唯一区别是 `chapter_count` 允许由调用方用一次 `GROUP BY` 的结果直接给出，
        从而避开 `len(self.chapters)` 的逐书懒加载（50 本 = 50 条 SELECT，
        且每章整段 content 都会被读进内存，实测 3.5-4.6 s）。
        未传 chapter_count 时才回落到关系加载，保证单独调用也能拿到正确值。
        """
        if chapter_count is None:
            chapter_count = len(self.chapters) if self.chapters else 0
        return {
            "id": self.id,
            "title": self.title,
            "author": self.author,
            "format": self.format,
            "file_path": self.file_path,
            "cover_path": self.cover_path,
            "total_chars": self.total_chars,
            "language": self.language,
            "chapter_count": int(chapter_count),
            "extra_data": self.extra_data,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

    def __repr__(self):
        return f"<Book(id={self.id}, title={self.title})>"


class Chapter(Base):
    """章节模型"""
    __tablename__ = "chapters"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    book_id = Column(String, ForeignKey("books.id"), nullable=False, comment="所属书籍ID")
    title = Column(String, comment="章节标题")
    index = Column(Integer, comment="章节序号")
    content = Column(Text, comment="章节文本内容")
    char_count = Column(Integer, default=0, comment="字符数")
    extra_data = Column(JSON, default=dict, comment="扩展元数据")

    # PERF-IDX: `WHERE book_id=? ORDER BY index` 是章节列表的固定形态，没索引时
    # SQLite 计划是 SCAN chapters + TEMP B-TREE（最大那本 1626 章实测 251 ms）。
    # 用列对象而非字符串建复合索引；`index` 同时是 Python 属性名和 SQL 保留字，
    # SQLAlchemy 渲染 DDL 时会自动带引号，手写裸 index 会语法错。
    __table_args__ = (
        Index("ix_chapters_book_id_index", book_id, index),
    )

    # 关联书籍
    book = relationship("Book", back_populates="chapters")

    def to_dict(self):
        """转换为字典"""
        return {
            "id": self.id,
            "book_id": self.book_id,
            "title": self.title,
            "index": self.index,
            "char_count": self.char_count,
            "content": self.content,
            "content_preview": self.content[:200] + "..." if self.content and len(self.content) > 200 else self.content,
            "extra_data": self.extra_data,
        }

    def to_list_dict(self, content_preview=None):
        """章节列表序列化 (WF-09)：键集与 to_dict() 一致，但**不再下发整章正文**。

        `content` 键保留（前端 EditPage/DubbingPage 的 Chapter 类型里它是 optional，
        完整正文走 GET /api/books/chapters/{id}/content 懒加载），值给 None；
        `content_preview` 优先用调用方传入的 SQL `substr(content,1,200)` 结果，
        避免把整本书读进内存。详情接口仍用 to_dict()，契约不变。
        """
        if content_preview is None:
            content_preview = preview_text(self.content, self.char_count)
        return {
            "id": self.id,
            "book_id": self.book_id,
            "title": self.title,
            "index": self.index,
            "char_count": self.char_count,
            "content": None,
            "content_preview": content_preview,
            "extra_data": self.extra_data,
        }

    def __repr__(self):
        return f"<Chapter(id={self.id}, title={self.title}, chars={self.char_count})>"


def preview_text(content, char_count=None) -> Optional[str]:
    """整章正文 -> 200 字预览（超长补 "..."，与 Chapter.to_dict 既有口径一致）。"""
    if not content:
        return content
    if char_count is None:
        char_count = len(content)
    head = content[:PREVIEW_CHARS]
    return head + "..." if char_count > PREVIEW_CHARS else head


def chapter_preview_dict(chapter_id, book_id, title, index, char_count,
                         extra_data, content_head) -> dict:
    """用「只取正文前 200 字」的 SQL 投影直接拼章节列表行 (WF-09)。

    与 Chapter.to_list_dict() 输出同一套键；差别是这条路径根本不把整段
    content 读进 Python，`char_count` 只用于判断是否补省略号。
    """
    preview = None
    if content_head is not None:
        preview = content_head + "..." if (char_count or 0) > PREVIEW_CHARS else content_head
    return {
        "id": chapter_id,
        "book_id": book_id,
        "title": title,
        "index": index,
        "char_count": char_count,
        "content": None,
        "content_preview": preview,
        "extra_data": extra_data,
    }