"""U-7 变异检验：每枚新守卫都必须"注释掉就变红"，否则它只是文档。

做法：对**当前工作树**（应在一个干净的 worktree 里跑，别在产品树上做）逐条施加
一处最小破坏 → 跑 `tests/test_u7_online_threads.py` → 期望变红 → 立刻按字节还原。

三条自己的规矩：
  * 替换串必须**逐字命中一次**；命不中就报 `NOT_APPLIED`（模式失效的 RED 是假红，
    不算变异通过）；
  * 模式里的 `\n` 按**目标文件实际的换行符**展开：`core.autocrlf=true` 的机器上
    worktree 检出的是 CRLF，写死 `\n` 会让所有跨行模式 0 命中，造出 10 枚
    `NOT_APPLIED` 假红（实测首轮就这样废掉过一次）；
  * 读写一律二进制：中文源码用 `Path.write_text` 会把 LF 改成 CRLF，还原后树就脏了；
  * 跑完核对全树基线 md5：任何一枚文件字节不同 = 还原失败，脚本非零退出并打印差集。

用法（worktree 根）：
    PYTHONUTF8=1 python -X utf8 qa/harness/run_u7_mutation.py            # 全部
    PYTHONUTF8=1 python -X utf8 qa/harness/run_u7_mutation.py --only M3,M8
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
VO = "backend/services/voice_online.py"
API = "backend/api/voices.py"
TESTS = ["tests/test_u7_online_threads.py"]

# (编号, 说明, 文件, 原文, 变异后)
MUTATIONS = [
    ("M1", "线程回到按请求建池（M-1 复发）", VO,
     "    pool = search_pool()\n",
     "    from concurrent.futures import ThreadPoolExecutor as _TPE\n"
     "    pool = _TPE(max_workers=2)\n"),
    ("M2", "放弃等待时不再撤队列作业、也不再记账", VO,
     "    for fut in pending:\n"
     "        # 只有\"还在队列里\"的作业撤得掉；已经在跑的撤不掉，只能记下来。\n"
     "        if fut.cancel():\n"
     "            _bump(\"cancelled_while_queued\")\n"
     "        else:\n"
     "            _bump(\"abandoned_still_running\")\n",
     "    for fut in pending:\n        pass\n"),
    ("M3", "超时作业不入账（M-2 的账本被抹）", VO,
     "            _bump(\"source_timeouts\")\n",
     "            pass\n"),
    ("M4", "单源作业的预算自查失效", VO,
     "    left = job_deadline - time.monotonic()\n    if left <= 0:\n",
     "    left = job_deadline - time.monotonic()\n    if False:\n"),
    ("M5", "让位的作业不入账", VO,
     "        _bump(\"skipped_no_budget\")\n",
     "        pass\n"),
    ("M6", "剩余预算不再传进爬虫（httpx 收到固定 12s）", VO,
     "        deadline_s=min(SOURCE_DEADLINE_S, left))",
     "        deadline_s=SOURCE_DEADLINE_S)"),
    ("M7", "路由 504 回到\"空口一句话\"（终态记录被摘）", API,
     "            \"abandoned\": record[\"stats\"],\n",
     "            \"abandoned\": {},\n"),
    ("M8", "详情侧的 `..` 段门失效", VO,
     "    if has_upward_segment(repo):\n"
     "        return _fail(\"bad_repo\", f\"模型/仓库名含上层路径段: {repo!r}\", model_name=repo)\n",
     "    if False:\n"
     "        return _fail(\"bad_repo\", f\"模型/仓库名含上层路径段: {repo!r}\", model_name=repo)\n"),
    ("M9", "下载侧 file_path 的 `..` 段门失效", VO,
     "    if file_path and has_upward_segment(file_path):\n",
     "    if file_path and False:\n"),
    ("M10", "内层兜底不再跟总预算走", VO,
     "    grace_s = max(1.0, min(SOURCE_DEADLINE_S + 6.0, budget - 1.0))\n",
     "    grace_s = SOURCE_DEADLINE_S + 6.0\n"),
    ("M11", "死源带回的条目重新混进结果（#89 假数据形态）", VO,
     "            # 死源带回来的条目不进结果：#89 的第一层病根就是\"失败还端出假数据\"。\n"
     "            items = []\n",
     "            items = items\n"),
]

WATCH = [VO, API]


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
            restored = md5(path) == baseline[rel]
            if not restored:
                dirty.append(rel)
        verdict = "RED" if rc != 0 else "GREEN(守卫无效)"
        results.append((mid, desc, verdict, summary))
        print(f"[{mid}] {verdict} :: {desc}\n      {summary}")

    print("\n=== U-7 变异检验 ===")
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
