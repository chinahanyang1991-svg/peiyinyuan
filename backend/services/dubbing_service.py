"""
配音编排服务
处理配音项目、角色管理、音色分配
"""

import logging
import math
import uuid
from typing import Optional

from fastapi import HTTPException

from backend.models.base import get_session
from backend.models.project import DubbingProject, Role, VoiceAssignment
from backend.models.book import Book, Chapter
from backend.models.task import ConversionTask

logger = logging.getLogger(__name__)

# WF-11: 值域口径与合成侧唯一真源保持一致（modules/voice_manager/conditioning.py），
# 避免"服务端存 999、引擎侧自己夹成 2.0"两套语义。
from modules.voice_manager.conditioning import (
    MAX_PITCH, MAX_SPEED, MIN_PITCH, MIN_SPEED,
)
# 指令#1: 档位（在线/本地）的值域同样只认那份真源，白名单不在这里另抄一遍。
from modules.tts_engine.tier import TIERS as TIER_VALUES

# ---------- 项目全局设置白名单与值域 (WF-11) ----------
# kind: number=数值(越界夹紧) / text=短字符串 / dict=结构由专属校验器负责
# 键集来源: 前端 DubbingPage 存 {speed,pitch,emotion,tier}；P6/P7 读 speed/pitch/
# emotion/tone/engine/tier；cast/mix 链路读 mix（由 modules/mixer 的 validate_settings
# 负责内部结构）。
GLOBAL_SETTING_SPECS: dict = {
    "speed": {"kind": "number", "min": MIN_SPEED, "max": MAX_SPEED},
    "pitch": {"kind": "number", "min": MIN_PITCH, "max": MAX_PITCH},
    "emotion": {"kind": "text", "max_len": 32},
    "tone": {"kind": "text", "max_len": 32},
    "engine": {"kind": "text", "max_len": 32},
    # 指令#1: 配音档位（在线/本地）跟着项目存，切档不必改环境变量。值域由
    # modules/tts_engine/tier.py 那份真值给，脏值在入库前就 422 —— 让它落库等于
    # 把"这单跑不起来"推迟到 P4/P7 才爆（WF-11 的原意：脏值一条都不落库）。
    "tier": {"kind": "text", "max_len": 16, "one_of": TIER_VALUES},
    "language": {"kind": "text", "max_len": 16},
    "mix": {"kind": "dict"},
}


class SettingsValidationError(HTTPException, ValueError):
    """设置键不在白名单 / 类型不对 / 结构非法 (WF-11)。

    同时继承 HTTPException(422) 与 ValueError：
      * FastAPI 见到 HTTPException 直接渲染成 422 JSON —— 因为
        `backend/api/dubbing.py` 不在本次可改文件清单内（并行任务占用），
        路由层没有 try/except 可用，只抛 ValueError 会冒成 500，比原 200 更难查；
      * `except ValueError` 的服务层调用方（脚本/其它 service）仍能按老习惯兜住。
    """

    def __init__(self, detail: str, field: Optional[str] = None):
        message = detail if field is None else f"{field}: {detail}"
        HTTPException.__init__(self, status_code=422, detail=message)
        self.detail = message


def _reject(message: str, field: Optional[str] = None) -> None:
    """以 422 拒绝非法设置 (WF-11)：脏值一条都不落库。"""
    raise SettingsValidationError(message, field=field)


def _validate_mix(value: dict) -> dict:
    """mix 子结构交给 modules/mixer 自己的校验器，避免两套规则 (WF-11)。"""
    try:
        from modules.mixer import AudioMix
    except Exception:                                   # pragma: no cover - 兜底
        return value
    problems = AudioMix.validate_settings(value)
    if problems:
        _reject("; ".join(problems), field="mix")
    return value


def sanitize_global_settings(settings) -> tuple:
    """校验 + 夹紧项目全局设置 (WF-11)。

    返回 `(clean, adjustments)`：
      clean       —— 可以安全入库的字典（未知键一个都不留）
      adjustments —— 被夹紧的项 [{"key","from","to","min","max"}]，供响应回显实际生效值

    策略（与测试锁定一致）：
      * 数值**越界** -> 夹紧并回写实际值（不报错），因为引擎侧本来也会夹，
        存下夹紧后的值才是"所见即所合成"；
      * **未知键 / 类型非法 / NaN / 超长文本 / 空请求** -> 422 拒绝，
        不做静默丢弃（静默丢弃正是"设置没生效"这类工单的来源）。
    """
    if settings is None or not isinstance(settings, dict):
        _reject("settings 必须是对象（键值对）")
    if not settings:
        _reject("settings 不能为空对象；如需恢复默认请显式提交默认值")

    clean: dict = {}
    adjustments: list = []

    unknown = [k for k in settings if k not in GLOBAL_SETTING_SPECS]
    if unknown:
        _reject(
            f"未知设置项: {', '.join(sorted(map(str, unknown)))}；"
            f"允许: {', '.join(sorted(GLOBAL_SETTING_SPECS))}")

    for key, spec in GLOBAL_SETTING_SPECS.items():
        if key not in settings:
            continue
        value = settings[key]
        kind = spec["kind"]

        if kind == "number":
            if isinstance(value, bool):
                # 必须在任何数值转换之前拦：float(True) == 1.0 会静默通过
                _reject("需要数字，不接受布尔值", field=key)
            if not isinstance(value, (int, float)):
                try:
                    value = float(value)          # 允许 "1.2" 这类数字串
                except (TypeError, ValueError):
                    _reject(f"需要数字，收到 {type(value).__name__}", field=key)
            number = float(value)
            if not math.isfinite(number):
                _reject("需要有限数字（NaN/inf 非法）", field=key)
            lo, hi = spec["min"], spec["max"]
            clamped = float(min(max(number, lo), hi))
            if clamped != number:
                adjustments.append({"key": key, "from": number, "to": clamped,
                                    "min": lo, "max": hi})
            clean[key] = clamped

        elif kind == "text":
            if not isinstance(value, str):
                _reject(f"需要字符串，收到 {type(value).__name__}", field=key)
            text = value.strip()
            if not text:
                _reject("不能为空", field=key)
            if len(text) > spec["max_len"]:
                _reject(f"过长（最多 {spec['max_len']} 字符）", field=key)
            one_of = spec.get("one_of")
            if one_of and text.lower() not in one_of:
                _reject(f"只能是 {', '.join(one_of)}，收到 {text!r}", field=key)
            clean[key] = text

        else:  # dict
            if not isinstance(value, dict):
                _reject("需要对象（键值对）", field=key)
            clean[key] = _validate_mix(value)

    return clean, adjustments


class DubbingService:
    """配音编排服务"""

    def create_project(self, name: str, book_id: str = None,
                       mode: str = "single") -> dict:
        """创建配音项目"""
        session = get_session()
        try:
            project = DubbingProject(
                id=str(uuid.uuid4()),
                name=name,
                book_id=book_id,
                mode=mode,
                global_settings={
                    "speed": 1.0,
                    "pitch": 0.0,
                    "emotion": "neutral",
                },
            )
            session.add(project)
            session.commit()
            session.refresh(project)

            # 自动创建"旁白"角色
            narrator = Role(
                id=str(uuid.uuid4()),
                project_id=project.id,
                name="旁白",
                role_type="narrator",
            )
            session.add(narrator)
            session.commit()

            return project.to_dict()
        finally:
            session.close()

    def list_projects(self) -> list:
        """列出所有项目"""
        session = get_session()
        try:
            projects = (
                session.query(DubbingProject)
                .order_by(DubbingProject.created_at.desc())
                .all()
            )
            return [p.to_dict() for p in projects]
        finally:
            session.close()

    def get_project(self, project_id: str) -> Optional[dict]:
        """获取项目详情"""
        session = get_session()
        try:
            project = session.query(DubbingProject).filter_by(id=project_id).first()
            if not project:
                return None

            data = project.to_dict()
            data["roles"] = [r.to_dict() for r in project.roles]
            data["assignments"] = [a.to_dict() for a in project.assignments]
            return data
        finally:
            session.close()

    def add_role(self, project_id: str, name: str,
                 role_type: str = "custom") -> dict:
        """添加角色

        PY-112: 项目内角色名唯一约束 —— 同名归并。
        自动创建的"旁白"与手工创建的同名角色不再是两条记录（旧行为导致
        一个同名角色永远没有音色）。归并时 role_type 取更具体的一个
        （narrator > custom，且手工指定优先于默认 custom）。
        """
        session = get_session()
        try:
            norm = (name or "").strip().lower()
            existing = None
            if norm:
                for r in (session.query(Role).filter_by(project_id=project_id).all()):
                    if (r.name or "").strip().lower() == norm:
                        existing = r
                        break
            if existing is not None:
                # 同名归并: 保留已有记录(及其已绑定的 voice_id)，仅补充更具体的 role_type
                if role_type and role_type != "custom" and \
                        (existing.role_type or "custom") == "custom":
                    existing.role_type = role_type
                    session.commit()
                    session.refresh(existing)
                return existing.to_dict()

            role = Role(
                id=str(uuid.uuid4()),
                project_id=project_id,
                name=name,
                role_type=role_type,
            )
            session.add(role)
            session.commit()
            session.refresh(role)
            return role.to_dict()
        finally:
            session.close()

    def assign_voice_to_role(self, project_id: str, role_id: str,
                             voice_id: str) -> bool:
        """为角色分配音色"""
        session = get_session()
        try:
            role = session.query(Role).filter_by(id=role_id, project_id=project_id).first()
            if not role:
                return False
            role.voice_id = voice_id
            session.commit()
            return True
        finally:
            session.close()

    def update_voice_assignments(self, project_id: str,
                                  assignments: list) -> None:
        """
        更新音色分配
        assignments: [
            {"chapter_id": str, "voice_id": str, "role_id": str,
             "start_char": int, "end_char": int, "settings": dict}, ...
        ]
        """
        session = get_session()
        try:
            # 清除旧分配
            session.query(VoiceAssignment).filter_by(project_id=project_id).delete()

            for a in assignments:
                va = VoiceAssignment(
                    id=str(uuid.uuid4()),
                    project_id=project_id,
                    chapter_id=a.get("chapter_id"),
                    voice_id=a.get("voice_id"),
                    role_id=a.get("role_id"),
                    start_char=a.get("start_char", 0),
                    end_char=a.get("end_char", 0),
                    settings=a.get("settings", {}),
                )
                session.add(va)

            session.commit()
        finally:
            session.close()

    def update_global_settings(self, project_id: str,
                                settings: dict) -> bool:
        """更新全局配音设置 (WF-11: 白名单 + 值域夹紧)。

        旧行为 `{**current, **settings}` 无任何约束：`speed=999` 原样入库，
        合成侧只会静默失真或报错。现在：
          * 未知键 / 类型非法 -> 422（SettingsValidationError），不写库；
          * 数值越界 -> 夹紧后**回写实际值**（GET 项目即读到夹后的值）；
          * 返回契约仍是 bool（api/dubbing.py 依赖 `False -> 404`）。
        """
        return self.update_global_settings_detailed(project_id, settings)["ok"]

    def update_global_settings_detailed(self, project_id: str,
                                         settings: dict) -> dict:
        """同 update_global_settings，并回显入库后的实际设置与被夹紧的项。

        {"ok": bool, "settings": <合并后的全量设置>, "adjustments": [...]}
        """
        clean, adjustments = sanitize_global_settings(settings)
        session = get_session()
        try:
            project = session.query(DubbingProject).filter_by(id=project_id).first()
            if not project:
                return {"ok": False, "settings": {}, "adjustments": adjustments}
            current = project.global_settings or {}
            merged = {**current, **clean}
            project.global_settings = merged
            session.commit()
            if adjustments:
                logger.info(
                    f"[WF-11] 项目 {project_id} 设置越界已夹紧并回写: "
                    + ", ".join(f"{a['key']}={a['from']}->{a['to']}"
                                f"(允许 {a['min']}~{a['max']})" for a in adjustments))
            return {"ok": True, "settings": merged, "adjustments": adjustments}
        finally:
            session.close()

    def delete_project(self, project_id: str) -> bool:
        """删除项目。

        `roles` / `voice_assignments` 由 ORM 级联删除，但 `conversion_tasks` 不在
        关系里——快检 harness「建项目→跑 analyze-cast→删项目」每轮漏下 3 行指向已删
        项目的任务（生产库实测 21 行孤儿）。这里显式同事务删掉，防止任务表无限增长。
        """
        session = get_session()
        try:
            project = session.query(DubbingProject).filter_by(id=project_id).first()
            if project:
                n_tasks = session.query(ConversionTask).filter_by(
                    project_id=project_id).delete(synchronize_session=False)
                session.delete(project)
                session.commit()
                if n_tasks:
                    logger.info(f"[WF-孤儿任务] 项目 {project_id} 删除时连带清掉 "
                                f"{n_tasks} 条转换任务")
                return True
            return False
        finally:
            session.close()


# 全局单例
dubbing_service = DubbingService()