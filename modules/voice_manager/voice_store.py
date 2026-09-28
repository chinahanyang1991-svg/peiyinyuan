"""
音色持久化存储
基于 SQLite 的音色 CRUD + 本地文件管理
"""

import os
import uuid
import shutil
from pathlib import Path
from typing import Optional, List

from sqlalchemy import func

from backend.models.base import get_session
from backend.models.voice import VoiceProfile


class VoiceStore:
    """音色数据持久化管理"""

    def __init__(self, voice_dir: str = None):
        from backend.config import Config
        self.voice_dir = Path(voice_dir or Config.VOICE_DIR)
        self.voice_dir.mkdir(parents=True, exist_ok=True)

    def create(self, name: str, source: str, engine: str,
               language: str = "zh", gender: str = None,
               age_group: str = None, style_tags: list = None,
               description: str = "", model_path: str = "",
               sample_audio_path: str = "", **kwargs) -> VoiceProfile:
        """创建音色记录"""
        session = get_session()
        try:
            voice = VoiceProfile(
                id=str(uuid.uuid4()),
                name=name,
                source=source,
                engine=engine,
                language=language,
                gender=gender,
                age_group=age_group,
                style_tags=style_tags or [],
                description=description,
                model_path=model_path,
                sample_audio_path=sample_audio_path,
                is_downloaded=1,
                **kwargs,
            )
            session.add(voice)
            session.commit()
            session.refresh(voice)
            return voice
        finally:
            session.close()

    def get(self, voice_id: str) -> Optional[VoiceProfile]:
        """获取音色"""
        session = get_session()
        try:
            return session.query(VoiceProfile).filter_by(id=voice_id).first()
        finally:
            session.close()

    def _filtered(self, query, language: str = None, gender: str = None,
                  source: str = None, engine: str = None,
                  style_tag: str = None, category: str = None):
        """把筛选谓词集中到一处，`list()` 与 `count()` 共用（WF-12）。"""
        if language:
            query = query.filter_by(language=language)
        if gender:
            query = query.filter_by(gender=gender)
        if source:
            query = query.filter_by(source=source)
        if engine:
            query = query.filter_by(engine=engine)
        if category:
            # VA-2: 五类音色库筛选（NULL 行已由启动回填归入 human）
            query = query.filter_by(category=category)
        if style_tag:
            # JSON 数组模糊匹配
            query = query.filter(VoiceProfile.style_tags.contains(style_tag))
        return query

    def list(self, language: str = None, gender: str = None,
             source: str = None, engine: str = None,
             style_tag: str = None, category: str = None,
             limit: int = 100, offset: int = 0) -> List[VoiceProfile]:
        """列出音色（支持多条件筛选 + 分页）

        WF-12: 新增 `offset`（默认 0，老调用方行为不变）。排序加了 `id` 兜底键——
        仅按 created_at 排时，同一批入库（时间戳相同）的行翻页会重复/漏读。
        """
        session = get_session()
        try:
            query = self._filtered(
                session.query(VoiceProfile),
                language=language, gender=gender, source=source, engine=engine,
                style_tag=style_tag, category=category,
            )
            return (query.order_by(VoiceProfile.created_at.desc(), VoiceProfile.id.desc())
                        .offset(max(0, int(offset)))
                        .limit(limit)
                        .all())
        finally:
            session.close()

    def list_all(self, page_size: int = 500, **filters) -> List[VoiceProfile]:
        """按分页取**全部**满足筛选条件的音色。

        引擎侧 `_restore_voices` 以前传 `limit=1000` 一次性取表：库一旦超过 1000 行，
        第 1001 行之后的音色在 UI 里看得见、在引擎里不存在（参考音频被无视，
        合成退化成默认音色）。这里逐页走完，短页即结束。
        """
        collected: List[VoiceProfile] = []
        offset = 0
        while True:
            page = self.list(limit=page_size, offset=offset, **filters)
            collected.extend(page)
            if len(page) < page_size:
                return collected
            offset += page_size

    def count(self, language: str = None, gender: str = None,
              source: str = None, engine: str = None,
              style_tag: str = None, category: str = None) -> int:
        """满足筛选条件的音色总数 (WF-12: SQL COUNT(*)，不是本页条数)。"""
        session = get_session()
        try:
            query = self._filtered(
                session.query(func.count(VoiceProfile.id)),
                language=language, gender=gender, source=source, engine=engine,
                style_tag=style_tag, category=category,
            )
            return int(query.scalar() or 0)
        finally:
            session.close()

    def search(self, keyword: str, limit: int = 50) -> List[VoiceProfile]:
        """搜索音色（按名称和描述）"""
        session = get_session()
        try:
            return (
                session.query(VoiceProfile)
                .filter(
                    (VoiceProfile.name.contains(keyword)) |
                    (VoiceProfile.description.contains(keyword)) |
                    (VoiceProfile.style_tags.contains(keyword))
                )
                .limit(limit)
                .all()
            )
        finally:
            session.close()

    def update(self, voice_id: str, **kwargs) -> bool:
        """更新音色属性"""
        session = get_session()
        try:
            voice = session.query(VoiceProfile).filter_by(id=voice_id).first()
            if not voice:
                return False
            for key, value in kwargs.items():
                if hasattr(voice, key):
                    setattr(voice, key, value)
            session.commit()
            return True
        finally:
            session.close()

    def delete(self, voice_id: str) -> bool:
        """删除音色"""
        session = get_session()
        try:
            voice = session.query(VoiceProfile).filter_by(id=voice_id).first()
            if not voice:
                return False

            # 删除本地文件
            if voice.model_path and os.path.exists(voice.model_path):
                try:
                    if os.path.isdir(voice.model_path):
                        shutil.rmtree(voice.model_path)
                    else:
                        os.remove(voice.model_path)
                except Exception:
                    pass

            session.delete(voice)
            session.commit()
            return True
        finally:
            session.close()

    def get_statistics(self) -> dict:
        """获取音色库统计信息"""
        session = get_session()
        try:
            total = session.query(VoiceProfile).count()
            func = __import__('sqlalchemy').func
            by_source = {}
            for row in session.query(
                VoiceProfile.source,
                func.count(VoiceProfile.id)
            ).group_by(VoiceProfile.source).all():
                by_source[row[0]] = row[1]
            # by_engine 是给前端"引擎"筛选框当选项源的：选项必须来自库里真有的引擎，
            # 否则选了个零行的引擎只会得到空列表，用户无从判断是筛选错了还是库是空的。
            by_engine = {}
            for row in session.query(
                VoiceProfile.engine,
                func.count(VoiceProfile.id)
            ).group_by(VoiceProfile.engine).all():
                by_engine[row[0]] = row[1]
            return {"total": total, "by_source": by_source, "by_engine": by_engine}
        finally:
            session.close()


# 全局单例
voice_store = VoiceStore()