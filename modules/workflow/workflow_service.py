"""工作流模板管理服务"""

import uuid
from datetime import datetime
from typing import Optional
from backend.models.base import get_session
from backend.models.workflow import WorkflowTemplate


class WorkflowTemplateService:
    """工作流模板 CRUD"""

    def create(self, name: str, description: str = "",
               stages: list = None, engine: str = "",
               output_format: str = "m4b", settings: dict = None) -> dict:
        """`engine` 留空 = 模板不声明引擎，运行时由档位定（见 backend/api/workflows.py）。"""
        session = get_session()
        try:
            template = WorkflowTemplate(
                id=str(uuid.uuid4()),
                name=name,
                description=description,
                stages=stages or self._default_stages(),
                engine=engine,
                output_format=output_format,
                settings=settings or {},
            )
            session.add(template)
            session.commit()
            session.refresh(template)
            return template.to_dict()
        finally:
            session.close()

    def get(self, template_id: str) -> Optional[dict]:
        session = get_session()
        try:
            t = session.query(WorkflowTemplate).filter_by(id=template_id).first()
            return t.to_dict() if t else None
        finally:
            session.close()

    def list_all(self, limit: int = 50) -> list:
        session = get_session()
        try:
            templates = (
                session.query(WorkflowTemplate)
                .order_by(WorkflowTemplate.updated_at.desc())
                .limit(limit).all()
            )
            return [t.to_dict() for t in templates]
        finally:
            session.close()

    def update(self, template_id: str, **kwargs) -> bool:
        session = get_session()
        try:
            t = session.query(WorkflowTemplate).filter_by(id=template_id).first()
            if not t:
                return False
            for key, val in kwargs.items():
                if hasattr(t, key) and key != "id":
                    setattr(t, key, val)
            t.updated_at = datetime.utcnow()
            session.commit()
            return True
        finally:
            session.close()

    def find_by_name(self, name: str) -> Optional[dict]:
        session = get_session()
        try:
            t = session.query(WorkflowTemplate).filter_by(name=name).first()
            return t.to_dict() if t else None
        finally:
            session.close()

    def delete(self, template_id: str) -> bool:
        session = get_session()
        try:
            t = session.query(WorkflowTemplate).filter_by(id=template_id).first()
            if not t:
                return False
            session.delete(t)
            session.commit()
            return True
        finally:
            session.close()

    @staticmethod
    def _default_stages() -> list:
        return [
            {"name": "import", "label": "导入", "config": {}},
            {"name": "parse", "label": "解析", "config": {}},
            {"name": "clean", "label": "清洗", "config": {}},
            {"name": "split", "label": "分句", "config": {}},
            {"name": "analyze", "label": "分析", "config": {}},
            {"name": "assign", "label": "分配", "config": {}},
            {"name": "synthesize", "label": "合成", "config": {}},
            {"name": "postprocess", "label": "后处理", "config": {}},
            {"name": "package", "label": "封装", "config": {}},
        ]


workflow_template_service = WorkflowTemplateService()


# ---------------------------------------------------------------- 官方模板
# U-5：此前 /api/workflows/templates 恒返空集（0 行），UI 有入口、数据面永远
# 什么都没有。这里给三条**真能被 settings 解释**的官方模板——字段只用
# conversion_workflow 实际读取的键（engine / max_sentences），不造无人消费的键。
OFFICIAL_TEMPLATES: list = [
    {
        "name": "官方 · 整本有声书（m4b / voxcpm）",
        "description": "单人朗读整本成书，m4b 带章节标记，引擎取本地 voxcpm（音质最优，CPU 机较慢）。",
        "engine": "voxcpm",
        "output_format": "m4b",
        "settings": {"engine": "voxcpm"},
    },
    {
        "name": "官方 · 快速抽检（wav / edgetts / 前 20 句）",
        "description": "只为听效果：在线 edgetts + 前 20 句截断，秒级出 wav，不占本地模型。",
        "engine": "edgetts",
        "output_format": "wav",
        "settings": {"engine": "edgetts", "max_sentences": 20},
    },
    {
        "name": "官方 · 多人角色剧（m4b / 需先确认 cast）",
        "description": "走配音项目的角色分配（PY-113/114 + 批次C cast 归一）；"
                       "须先在「配音项目」里做过角色分析并确认，否则全部落旁白音色。",
        "engine": "voxcpm",
        "output_format": "m4b",
        "settings": {"engine": "voxcpm"},
    },
]


def seed_official_templates() -> list:
    """幂等播种官方模板，返回本次新建的名字列表。

    幂等口径 = 「同名即跳过」：用户可以改它（改动不会被覆盖），删了会在下次
    启动重新出现——与内置音色 `_init_builtin_voices` 的既有约定一致。
    任何异常（表还没建、库被锁）都不拖垮 init_db：播种失败只是少三条模板。
    """
    created = []
    for tpl in OFFICIAL_TEMPLATES:
        try:
            if workflow_template_service.find_by_name(tpl["name"]):
                continue
            workflow_template_service.create(**tpl)
            created.append(tpl["name"])
        except Exception:  # noqa: BLE001 - 启动期播种不得阻断服务起来
            break
    return created
