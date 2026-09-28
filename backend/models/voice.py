"""
音色数据模型
"""

import uuid
from datetime import datetime
from sqlalchemy import Column, String, Integer, DateTime, JSON, Text
from backend.models.base import Base


class VoiceProfile(Base):
    """音色配置模型"""
    __tablename__ = "voice_profiles"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String, nullable=False, comment="音色名称")
    # PERF-IDX: source/engine/category 是音色列表的筛选谓词、created_at 是排序键，
    # 四个都没索引时 `ORDER BY created_at DESC LIMIT 50` 走 SCAN + TEMP B-TREE。
    source = Column(String, default="builtin", index=True,
                    comment="来源: builtin/downloaded/cloned/designed/sample")
    category = Column(String, default="human", index=True,
                      comment="音色类别: human/animal/electronic/anime/mechanical")
    engine = Column(String, default="voxcpm", index=True,
                    comment="引擎: voxcpm/kokoro/indextts/mosstts/edgetts")
    language = Column(String, default="zh", comment="主要语言")
    gender = Column(String, comment="性别: male/female/neutral")
    age_group = Column(String, comment="年龄段: child/young/adult/senior")
    style_tags = Column(JSON, default=list, comment="风格标签: ['温柔', '磁性']")
    model_path = Column(String, comment="模型文件路径")
    sample_audio_path = Column(String, comment="试听音频路径")
    description = Column(Text, comment="语音设计描述")
    reference_audio_path = Column(String, comment="克隆参考音频路径")
    is_downloaded = Column(Integer, default=0, comment="是否已下载")
    download_url = Column(String, comment="下载地址")
    meta = Column(JSON, default=dict, comment="扩展元数据")
    # US-C4: 最近一次**成功产出试听/成品**的采样种子（可复现重试的锚点）。
    # 可空 = "这条音色还没有 seed 化产物"；老库由 init_db 的轻量列迁移（F-R4-3）
    # 自动 ALTER TABLE 补上，不需要手工建表脚本。
    seed = Column(Integer, comment="最近一次成功合成的 seed (US-C4)")
    # PY-323(a′): **参考音频**的语种档（不是待合成文本的语种）。
    #   mandarin_ref    = 参考音频是普通话 → openvoice 克隆可用（实测 tpsim 0.917-1.000）
    #   nonmandarin_ref = 参考音频非普通话 → openvoice 说中文不合格（实测 0.500-0.917），
    #                     分发时按档降级（`modules/voice_manager/language_tier.py`）
    #   unknown         = 量不出/无音频/置信不足
    #   NULL            = 从没打过标（与 unknown 区别：未知≠量过说不知道）
    # 老库由 F-R4-3 轻量列迁移自动 ALTER TABLE 补上。
    audio_language_tier = Column(String, comment="参考音频语种档 (PY-323 a′)")
    created_at = Column(DateTime, default=datetime.utcnow, index=True,
                        comment="创建时间")
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def to_dict(self):
        """转换为字典

        PY-111: 统一音色主键字段名 —— 列表/详情接口同时输出 id 与 voice_id(别名)，
        与 /voices/clone 响应的 voice_id 对齐, 调用方只需一套取值逻辑。
        """
        return {
            "id": self.id,
            "voice_id": self.id,
            "name": self.name,
            "source": self.source,
            "category": self.category or "human",
            "engine": self.engine,
            "language": self.language,
            "gender": self.gender,
            "age_group": self.age_group,
            "style_tags": self.style_tags or [],
            "description": self.description,
            "is_downloaded": bool(self.is_downloaded),
            "has_sample": bool(self.sample_audio_path),
            "meta": self.meta,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }

    def to_dict_full(self):
        """完整字典（含路径信息）"""
        d = self.to_dict()
        d.update({
            "model_path": self.model_path,
            "sample_audio_path": self.sample_audio_path,
            "reference_audio_path": self.reference_audio_path,
            "download_url": self.download_url,
            # US-C4: 详情/试听链路回传"最近一次成功合成的 seed"（列表载荷保持不变）
            "seed": self.seed,
            # PY-323(a′): 语种档同样只进详情/试听载荷，列表不加键
            "audio_language_tier": self.audio_language_tier,
        })
        return d

    def __repr__(self):
        return f"<VoiceProfile(id={self.id}, name={self.name}, engine={self.engine})>"