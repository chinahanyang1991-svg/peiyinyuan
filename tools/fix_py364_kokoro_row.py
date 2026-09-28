"""PY-364 数据修正：把 Kokoro 错行从"voxcpm 可选本地音色池"里摘出来。

现场（2026-09-27 只读现采 data/peiyinyuan.db）：
    id=c7b3ebd6-2b20-4ab8-9555-bb3971ecdec8  "开放音色-Kokoro女声(af_heart)"
    source='downloaded'  engine='voxcpm'  reference_audio_path=NULL
    meta.origin={source: huggingface, repo: hexgrad/Kokoro-82M, file_path: samples/af_heart_0.wav}
    其样本 wav（data/voices/online/<id>.wav）也已不在盘上。
    981 枚 downloaded/voxcpm 里唯一没有参考路径的一枚（其余 980 枚全部在盘、可解码）。
voxcpm 是零样本克隆引擎、只照参考音频说话（voxcpm_adapter::_restore_voices 的
`and v.reference_audio_path` 把它挡在装载外）——可挑可绑，挑了拿到的是 voxcpm 默认声。

【摘的机制 = 改标注重定位，不 DELETE】把 engine 从 'voxcpm' 改成 'kokoro'
（= 该样本的真实归属）并在 meta 里落一枚 py364 印记：
  * 句级引擎路由读音色行声明的 engine（modules/voice_manager/conditioning.py 文件头），
    改标后这枚不再进任何 voxcpm 候选（/api/voices?engine=voxcpm 即被滤掉）；
    即便手工选中，kokoro 未装依赖时 engine_registry.get_loaded 会抛
    EngineLoadError → 任务显式 failed，**不再静默塌成默认声**（fail loud > fail wrong）。
  * 不动 source/reference_audio_path/名称：行仍可见、可回滚、无损；
    不补 reference_audio_path 是因为那个 wav 已不在盘上，补了就是接着骗。
  * 为什么不真接 Kokoro：要装 `kokoro, misaki, phonemizer` 三个依赖包，
    现采 /api/engines 里 kokoro available:false —— 为 1 枚音色不划算；摘掉则不再骗人。

用法（默认 --dry-run，绝不悄悄写库）：
    python tools/fix_py364_kokoro_row.py --db <path> [--apply|--rollback] [--backup <json>]
写前把该行完整原文（raw sqlite 值，含逐字 meta 文本）备份到
agent-ops/fixwave-20260927/C_py364/py364_row_backup.json（--backup 可改）。
幂等：已是目标态/已是原文态时第二跑什么都不改并明说。
"""
import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

TARGET_ID = "c7b3ebd6-2b20-4ab8-9555-bb3971ecdec8"
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BACKUP = str(ROOT / "agent-ops" / "fixwave-20260927" / "C_py364" / "py364_row_backup.json")
MARK_KEY = "py364"


def read_row(conn, voice_id):
    conn.row_factory = sqlite3.Row
    r = conn.execute("SELECT * FROM voice_profiles WHERE id=?", (voice_id,)).fetchone()
    return dict(r) if r is not None else None


def row_is_target(row):
    """目标态 = engine 已改标 kokoro 且 meta 带 py364 印记。"""
    if str(row.get("engine") or "").lower() != "kokoro":
        return False
    try:
        meta = json.loads(row.get("meta") or "{}")
    except (TypeError, ValueError):
        return False
    return isinstance(meta, dict) and MARK_KEY in meta


def _backup_path_for(args):
    return args.backup


def write_backup(path, db_path, voice_id, row):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"db": str(db_path), "table": "voice_profiles",
                   "voice_id": voice_id,
                   "captured_at": datetime.now(timezone.utc).isoformat(),
                   "columns": list(row.keys()), "row": row},
                  f, ensure_ascii=False, indent=1)


def do_apply(conn, args, voice_id, quiet=False):
    row = read_row(conn, voice_id)
    if row is None:
        print("[fix364] 行不存在 id=%s —— 未做任何修改" % voice_id)
        return 2
    if row_is_target(row):
        print("[fix364] 已经是目标态（engine=kokoro + meta.%s 印记齐全），未做任何修改。" % MARK_KEY)
        return 0
    plan = {"engine": [row.get("engine"), "kokoro"],
            "meta": "+%s 印记" % MARK_KEY,
            "updated_at": [row.get("updated_at"), "<now>"]}
    if not args.apply or args.dry_run:
        print("[fix364] dry-run：当前行原文如下（未写库）")
        print(json.dumps(row, ensure_ascii=False, indent=1, default=str))
        print("[fix364] 将要执行的动作: %s" % json.dumps(plan, ensure_ascii=False))
        print("[fix364] 加 --apply 才会写 %s" % args.db)
        return 0
    bak = _backup_path_for(args)
    if os.path.exists(bak):
        old = json.loads(open(bak, encoding="utf-8").read())
        if old.get("row") != row:
            print("[fix364] 备份 %s 已存在且与当前行不一致——拒绝覆盖（换 --backup 或先处置）" % bak)
            return 3
    else:
        write_backup(bak, args.db, voice_id, row)
        print("[fix364] 原行已备份 -> %s" % bak)
    try:
        meta = json.loads(row.get("meta") or "{}")
    except (TypeError, ValueError):
        meta = {}
    meta[MARK_KEY] = {
        "fixed_by": "tools/fix_py364_kokoro_row.py",
        "action": "unbound_from_voxcpm",
        "reason": "样本来自 hexgrad/Kokoro-82M，engine 曾被错标成 voxcpm 且"
                  "reference_audio_path=NULL——可挑可绑却拿到 voxcpm 默认声 (PY-364)。"
                  "改标 kokoro 后不再冒充 voxcpm 可选音色。",
        "at": datetime.now(timezone.utc).isoformat(),
        "backup": bak,
    }
    conn.execute("UPDATE voice_profiles SET engine='kokoro', meta=?, updated_at=? WHERE id=?",
                 (json.dumps(meta, ensure_ascii=False),
                  datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S.%f"), voice_id))
    conn.commit()
    if not quiet:
        print("[fix364] 已改标：engine voxcpm->kokoro + meta.%s 印记 (id=%s)" % (MARK_KEY, voice_id))
    return 0


def do_rollback(conn, args, voice_id, quiet=False):
    bak = _backup_path_for(args)
    if not os.path.exists(bak):
        print("[fix364] 无备份文件 %s —— 拒绝回滚（不猜原文）" % bak)
        return 2
    saved = json.loads(open(bak, encoding="utf-8").read())
    if saved.get("voice_id") != voice_id:
        print("[fix364] 备份里的 voice_id=%s 与目标 %s 不符 —— 拒绝" % (
            saved.get("voice_id"), voice_id))
        return 2
    orig = saved["row"]
    cur = read_row(conn, voice_id)
    if cur is None:
        print("[fix364] 行不存在 id=%s —— 回滚无从谈起" % voice_id)
        return 2
    if cur == orig:
        print("[fix364] 已经是原文状态，未做任何修改。")
        return 0
    cols = [c for c in orig if c != "id"]
    conn.execute("UPDATE voice_profiles SET %s WHERE id=?" %
                 ", ".join("%s=?" % c for c in cols),
                 [orig[c] for c in cols] + [voice_id])
    conn.commit()
    after = read_row(conn, voice_id)
    if after != orig:
        print("[fix364] 回滚后校验不一致！请人工核对 %s" % bak)
        return 4
    if not quiet:
        print("[fix364] 已逐字段还原为原文 (id=%s)" % voice_id)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="PY-364 Kokoro 错行摘出/回滚（默认 dry-run）")
    ap.add_argument("--db", default=str(ROOT / "data" / "peiyinyuan.db"))
    ap.add_argument("--voice-id", default=TARGET_ID)
    ap.add_argument("--dry-run", action="store_true", help="默认行为，显式给出只是为了可读")
    ap.add_argument("--apply", action="store_true", help="真正写库（先备份原行）")
    ap.add_argument("--rollback", action="store_true", help="从备份 JSON 逐字段还原")
    ap.add_argument("--backup", default=DEFAULT_BACKUP)
    args = ap.parse_args(argv)

    if args.apply and args.dry_run:
        print("[fix364] --dry-run 与 --apply 同时给出，按 dry-run 处理")
        args.apply = False
    if not os.path.exists(args.db):
        print("[fix364] 库不存在: %s" % args.db)
        return 2
    conn = sqlite3.connect(args.db)
    try:
        if args.rollback:
            return do_rollback(conn, args, args.voice_id)
        return do_apply(conn, args, args.voice_id)
    except sqlite3.OperationalError as e:
        print("[fix364] 数据库操作失败（服务占用/锁？）: %s" % e)
        return 5
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
