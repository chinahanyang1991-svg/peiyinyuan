"""U-8 变异检验：流式导入 / 出事件循环这套守卫，逐枚注释掉必须变红。

与 `run_u7_mutation.py` 同一套规矩（那三条是被 U-7 首轮 10 枚 `NOT_APPLIED`
换来的，这里原样继承）：
  * 替换串必须**逐字命中一次**，命不中报 `NOT_APPLIED`，不算变异通过；
  * 模式里的 `\\n` 按**目标文件实际换行符**展开（`core.autocrlf=true` 的机器上
    worktree 检出 CRLF，写死 `\\n` 会 0 命中）；
  * 读写一律二进制；跑完核对基线 md5，任何文件字节不同 = 还原失败，非零退出。

跑法（干净 worktree 根，勿在产品树/未提交改动上跑）：
    PYTHONUTF8=1 python -X utf8 qa/harness/run_u8_mutation.py
    PYTHONUTF8=1 python -X utf8 qa/harness/run_u8_mutation.py --only M1,M9
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BOOKS = "backend/api/books.py"
SVC = "backend/services/book_service.py"
DUB = "backend/api/dubbing.py"
TESTS = ["tests/test_u8_event_loop_blocking.py", "tests/test_text_quality.py"]

# (编号, 说明, 文件, 原文, 变异后)
MUTATIONS = [
    ("M1", "导入回到事件循环里同步入库（M-3 主病灶复发）", BOOKS,
     '        book = await asyncio.to_thread(\n'
     '            book_service.import_book_from_path, spool, file.filename, None,\n'
     '            encoding if encoding != "auto" else None)\n',
     '        book = book_service.import_book_from_path(\n'
     '            spool, file.filename, None,\n'
     '            encoding if encoding != "auto" else None)\n'),
    ("M2", "分块 read 退回整份 read（≤500MB 一次进堆）", BOOKS,
     "                chunk = await file.read(book_service.UPLOAD_CHUNK_BYTES)\n",
     "                chunk = await file.read()\n"),
    ("M3", "体积上限门失效（超限也照收）", BOOKS,
     "                if written > max_bytes:\n",
     "                if False and written > max_bytes:\n"),
    ("M4", "磁盘余量门失效（写满才 ENOSPC）", BOOKS,
     "    if free < Config.MIN_FREE_DISK_MB * 1024 * 1024:\n",
     "    if False:\n"),
    ("M5", "finally 不再清中转件（半截上传留在盘上）", BOOKS,
     "        book_service.discard_spool(spool)\n",
     "        pass\n"),
    ("M6", "搬家退回拷贝语义（500MB 白写一遍盘）", SVC,
     "            os.replace(src, str(dst))\n",
     "            shutil.copy2(src, str(dst))\n"),
    ("M7", "跨卷兜底失效（EXDEV 直接抛给用户）", SVC,
     "            if e.errno != errno.EXDEV:\n"
     "                raise\n",
     "            if True:\n"
     "                raise\n"),
    ("M8", "批量导入回到事件循环里同步跑", BOOKS,
     "    results = await asyncio.to_thread(book_service.import_batch, files)\n",
     "    results = book_service.import_batch(files)\n"),
    ("M9", "试听回到 handler 内联（模型推理按住循环）", DUB,
     "        wav_path, seed_used = await asyncio.to_thread(\n"
     "            _render_preview, project_id, voice_id, settings)\n",
     "        wav_path, seed_used = _render_preview(project_id, voice_id, settings)\n"),
    ("M10", "成功试听的 seed 响应头丢失（US-C4 前端拿不到值）", DUB,
     "        if seed_used is not None:\n",
     "        if False:\n"),
    ("M11", "质量门失败不再翻 502（退化成裸异常->500）", DUB,
     '        raise HTTPException(502, f"试听音频未通过质量门: {qe.report[\'reason\']}")\n',
     "        raise\n"),
    ("M12", "seed 生产者再次断线（全链路没人落库）", DUB,
     "    record_last_successful_seed(voice_id, outcome.seed)\n",
     "    pass\n"),
    ("M13", "引擎装载失败不再翻 503（PY-118 复发）", DUB,
     '        raise HTTPException(503, f"TTS 引擎模型加载失败：{e}")\n',
     "        raise\n"),
    ("M14", "空引擎门失效（None 一路走到合成）", DUB,
     "    if engine is None:\n",
     "    if False:\n"),
    ("M15", "临时 wav 句柄不关（写盘失败被 WinError 32 顶掉真因）", DUB,
     "    tmp_path = tmp.name\n    tmp.close()",
     "    tmp_path = tmp.name\n    pass  # mutated:"),
]

WATCH = [BOOKS, SVC, DUB]


def md5(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()


def _eol(raw: bytes) -> bytes:
    return b"\r\n" if b"\r\n" in raw else b"\n"


def _expand(pattern: str, eol: bytes) -> bytes:
    """把模式里手写的 `\\n` 换成目标文件真正用的换行符。"""
    return pattern.replace("\n", "\x00").encode("utf-8").replace(b"\x00", eol)


def apply_mutation(path: Path, old: str, new: str) -> bool:
    raw = path.read_bytes()
    eol = _eol(raw)
    needle = _expand(old, eol)
    if raw.count(needle) != 1:
        return False
    path.write_bytes(raw.replace(needle, _expand(new, eol), 1))
    return True


def run_tests() -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, "-X", "utf8", "-m", "pytest", *TESTS, "-q", "--no-header",
         "-p", "no:cacheprovider"],
        cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=900)
    tail = [ln for ln in (proc.stdout or "").splitlines() if ln.strip()][-6:]
    return proc.returncode, " | ".join(tail)


def main(argv: list[str]) -> int:
    only = set()
    if "--only" in argv:
        only = {s.strip() for s in argv[argv.index("--only") + 1].split(",") if s.strip()}

    baseline = {rel: md5(ROOT / rel) for rel in WATCH}
    code, out = run_tests()
    print(f"[base] 未变异基线 rc={code} :: {out}")
    if code != 0:
        print("ABORT: 基线就是红的，变异检验无意义")
        return 2

    results, dirty = [], []
    for mid, desc, rel, old, new in MUTATIONS:
        if only and mid not in only:
            continue
        path = ROOT / rel
        raw = path.read_bytes()
        hits = raw.count(_expand(old, _eol(raw)))
        if hits != 1:
            results.append((mid, desc, "NOT_APPLIED", f"模式命中 {hits} 次"))
            continue
        apply_mutation(path, old, new)
        try:
            rc, summary = run_tests()
        finally:
            path.write_bytes(raw)
            if md5(path) != baseline[rel]:
                dirty.append(rel)
        verdict = "RED" if rc != 0 else "GREEN(守卫无效)"
        results.append((mid, desc, verdict, summary))
        print(f"[{mid}] {verdict} :: {desc}\n      {summary}")

    print("\n=== U-8 变异检验 ===")
    red = 0
    for mid, desc, verdict, summary in results:
        print(f"{mid:>4} {verdict:<14} {desc}")
        if verdict == "RED":
            red += 1
    print(f"合计 {len(results)} 枚，RED {red}，非 RED {len(results) - red}，还原失败 {len(dirty)}")
    still = [rel for rel in WATCH if md5(ROOT / rel) != baseline[rel]]
    if still:
        print("DIRTY（字节与基线不符）:", still)
        return 1
    if dirty:
        print("DIRTY（还原校验失败）:", dirty)
        return 1
    if red != len(results) or not results:
        print("FAIL: 有变异未被对应测试定罪（或无一枚施加）")
        return 1
    print("PASS: 每枚新守卫都有对应测试转红，工作树字节回到基线")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
