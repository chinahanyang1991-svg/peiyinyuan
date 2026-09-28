# -*- coding: utf-8 -*-
"""PY-365: 产物目录（data/output）的孤儿盘点、显式清理与启动对账门。

为什么这样做（一句话）：删任务绝不顺手删产物文件（用户红线：数据不丢，成品可能
比任务行本身更值钱），唯一安全的推进是让孤儿**看得见**（甲：只读盘点 + 带三重门
的显式 prune）并且让行↔文件的不一致在**启动时说出来**（丙：对账只报告、不删除）。

口径约定（不造第二套真值）：
  * "产物丢失" = `output_path` 非空且 `os.path.getsize` 失败 —— 与
    `backend/models/task.py:44-49` 的 `output_size_bytes is None` 同源；
  * "孤儿" = 文件在 `Config.OUTPUT_DIR` 之下、且**没有任何任务行**（含
    cast_analysis 等不产音频的行）的 `output_path` 解析后指向它；隐藏文件
    （`.gitkeep` 等以 `.` 开头）不参与判定；
  * prune 的三重门：① 必须显式 `confirm=true`；② 每条路径解析后必须落在
    output 目录**之内**（防 `../` 目录穿越与任意绝对路径）；③ 必须逐项出现在
    当场重算的孤儿清单里。任何一条不合 → 整单拒绝、零删除。删单在动手**之前**
    落盘到 `Config.DATA_DIR/output_prune_manifests/`（红线②：删了什么都要有账）。
"""
from __future__ import annotations

import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Iterable, Optional

from backend.config import Config
from backend.models.base import get_session
from backend.models.task import ConversionTask

logger = logging.getLogger(__name__)

# conversion_workflow 的产物命名口径（`conversion_workflow.py:1949`）：
#   {safe_title}_{task_id}.{fmt}，task_id 是标准 uuid4 —— 据此从文件名还原书名。
_UUID_TAIL_RE = re.compile(
    r"^(?P<title>.+)_[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$", re.IGNORECASE)


def _norm(path: str) -> str:
    """判等口径：绝对 + realpath + normcase（Windows 大小写/8.3 短名全部归一）。"""
    return os.path.normcase(os.path.realpath(os.path.abspath(str(path))))


def _is_under(root_norm: str, target_norm: str) -> bool:
    """target 是否落在 root 目录之内（真子目录，不含 root 本身）。"""
    return target_norm.startswith(root_norm + os.sep)


def _all_task_rows() -> list:
    session = get_session()
    try:
        return session.query(
            ConversionTask.id,
            ConversionTask.output_path,
            ConversionTask.status,
        ).all()
    finally:
        session.close()


def book_title_from_name(filename: str) -> Optional[str]:
    """从 `<书名>_<uuid>.<ext>` 形态的文件名还原书名；不匹配返回 None。"""
    stem = os.path.splitext(os.path.basename(filename or ""))[0]
    m = _UUID_TAIL_RE.match(stem)
    return m.group("title") if m else None


def build_inventory(output_dir: Optional[str] = None) -> dict:
    """只读盘点：孤儿文件清单 + 计数 + 字节总数 + "行在文件不在"清单。零副作用。"""
    out_root = os.path.abspath(str(output_dir or Config.OUTPUT_DIR))
    rows = _all_task_rows()

    referenced: dict = {}          # norm路径 -> [引用它的任务id, ...]
    missing_outputs = []
    for r in rows:
        if not r.output_path:
            continue               # cast_analysis 等不产音频的行：无路径 ≠ 丢产物
        referenced.setdefault(_norm(r.output_path), []).append(r.id)
        if not os.path.exists(r.output_path):
            missing_outputs.append({
                "task_id": r.id, "output_path": str(r.output_path),
                "status": r.status,
            })

    orphans = []
    total_bytes = 0
    referenced_count = 0
    if os.path.isdir(out_root):
        for name in sorted(os.listdir(out_root)):
            path = os.path.join(out_root, name)
            if not os.path.isfile(path) or name.startswith("."):
                continue
            n = _norm(path)
            if n in referenced:
                referenced_count += 1
                continue
            try:
                size = os.path.getsize(path)
                mtime = os.path.getmtime(path)
            except OSError:
                continue
            orphans.append({
                "name": name,
                "path": path,
                "bytes": size,
                "mtime": datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat(),
                "book_title": book_title_from_name(name),
            })
            total_bytes += size

    return {
        "output_dir": out_root,
        "orphans": orphans,
        "orphan_count": len(orphans),
        "orphan_total_bytes": total_bytes,
        "referenced_count": referenced_count,
        "missing_output_rows": missing_outputs,
        "missing_output_count": len(missing_outputs),
    }


def output_state(task: dict) -> dict:
    """给单条任务字典打"产物是否丢失"标记 —— 与 output_size_bytes 同源判定。

    models 侧的 to_dict 不归本次改动（文件白名单），端点在 `backend/api/tasks.py`
    以 `/{task_id}/output-state` 单独出口，避免动既有详情契约。
    """
    path = (task or {}).get("output_path")
    size = (task or {}).get("output_size_bytes")
    return {
        "output_missing": bool(path) and size is None,
        "output_size_bytes": size,
        "output_path": path,
    }


def reconcile_output_dir(output_dir: Optional[str] = None) -> dict:
    """丙·启动对账门：只盘点、只打一行汇总日志，**绝不删任何东西**。

    设计约束：删数据不是对账，报告才是。真正的删除只允许走 prune 的三重门。
    """
    inv = build_inventory(output_dir)
    summary = {
        "output_dir": inv["output_dir"],
        "orphan_count": inv["orphan_count"],
        "orphan_total_bytes": inv["orphan_total_bytes"],
        "referenced_count": inv["referenced_count"],
        "missing_output_count": inv["missing_output_count"],
    }
    logger.info(
        f"[配音员][PY-365] 启动产物对账: 孤儿 {summary['orphan_count']} 枚 / "
        f"{summary['orphan_total_bytes']} 字节，行在文件不在 {summary['missing_output_count']} 项"
        f"（只报告，不删除）")
    return summary


def prune_orphans(paths: Iterable[str], confirm: bool,
                  output_dir: Optional[str] = None,
                  manifest_dir: Optional[str] = None) -> dict:
    """甲·显式清理：三重门全过才删，且删前把删单落盘。任何不合 → ValueError 零删除。"""
    if not confirm:
        raise ValueError("prune 需要显式确认：confirm 必须为 true（默认不删任何东西）")
    items = [str(p) for p in (paths or [])]
    if not items:
        raise ValueError("paths 不能为空：清理必须逐条点名，不接受笼统的\"全删\"式请求")

    out_root = os.path.abspath(str(output_dir or Config.OUTPUT_DIR))
    out_norm = _norm(out_root)
    allowed = {o["path"]: o for o in build_inventory(out_root)["orphans"]}
    allowed_norm = {_norm(k): v for k, v in allowed.items()}

    planned = []
    for raw in items:
        n = _norm(raw)
        if not _is_under(out_norm, n):
            raise ValueError(f"路径越界：{raw!r} 解析后不在 output 目录 {out_root} 之内，整单拒绝")
        if n not in allowed_norm:
            raise ValueError(f"{raw!r} 不在当前孤儿清单里（仍被任务引用/不存在/盘点后新增），整单拒绝")
        entry = allowed_norm[n]
        planned.append({"path": entry["path"], "bytes": entry["bytes"],
                        "name": entry["name"]})

    # —— 红线②：删之前先把删单落盘（逐条路径+字节），事后能对账"到底删了什么" ——
    mdir = manifest_dir or os.path.join(str(Config.DATA_DIR), "output_prune_manifests")
    os.makedirs(mdir, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    manifest_path = os.path.join(mdir, f"prune-{ts}-{uuid.uuid4().hex[:8]}.json")
    manifest = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "output_dir": out_root,
        "total_bytes": sum(e["bytes"] for e in planned),
        "entries": planned,
    }
    with open(manifest_path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2)
        fh.flush()
        os.fsync(fh.fileno())

    deleted = []
    for e in planned:
        try:
            os.remove(e["path"])
        except OSError as exc:
            logger.warning(f"[配音员][PY-365] prune 删除失败(已记删单): {e['path']} -> {exc}")
            continue
        deleted.append(e)
    logger.info(
        f"[配音员][PY-365] prune 完成: 删除 {len(deleted)} 枚孤儿 / "
        f"{sum(e['bytes'] for e in deleted)} 字节，删单: {manifest_path}")
    return {
        "deleted": deleted,
        "freed_bytes": sum(e["bytes"] for e in deleted),
        "manifest_path": manifest_path,
    }
