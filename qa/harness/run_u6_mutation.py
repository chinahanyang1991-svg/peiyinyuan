"""U-6 变异检验：把每一道新守卫逐条拆掉，测试必须变红。

二进制读写（历史坑：Path.read_text/write_text 在 Windows 把 LF 改成 CRLF）。
每枚变异后立刻按 md5 复原并校验，且快照变异前测试文件的失败数基线。
"""
import hashlib
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(".")
SNAP = Path("qa/harness/tmp/u6mut_snapshot")
TESTS = ["tests/test_u6_no_fake_success.py"]

# (代号, 文件, 原文, 改成, 预期至少红几枚)
MUTANTS = [
    ("M1 503分支失效", "backend/api/llm.py",
     '    if result.source == "empty":',
     '    if False and result.source == "empty":', 3),
    ("M2 changed恒真", "backend/api/llm.py",
     "        changed=text != req.text,",
     "        changed=True,", 1),
    ("M3 health恒ok", "backend/api/llm.py",
     '            "status": "unavailable",',
     '            "status": "ok",', 1),
    ("M4 情感编造neutral", "modules/llm_client/llm_service.py",
     '            return LLMResult(None, "empty", reason or "LLM 无回复")',
     '            return LLMResult({"sentiment": "neutral", "intensity": 0.5}, "llm", reason)', 2),
    ("M5 回显冒充llm", "modules/llm_client/llm_service.py",
     '                return LLMResult(text, "passthrough", reason or "LLM 无回复，原文回透传")',
     '                return LLMResult(text, "llm", None)', 1),
    ("M6 客户端丢原因", "modules/llm_client/freellmapi_client.py",
     '            reason = f"连不上 Sidecar（{type(e).__name__}，url={self._chat_url}）: {e}"',
     '            reason = ""', 2),
    ("M7 moss原因恒空", "modules/tts_engine/engines/moss_tts_adapter.py",
     "        if missing:",
     "        if False and missing:", 1),
    ("M10 阻塞客户端回挂事件循环", "backend/api/llm.py",
     "def llm_polish(req: PolishRequest):",
     "async def llm_polish(req: PolishRequest):", 2),
    ("M9 错误原因不收成一行", "modules/llm_client/freellmapi_client.py",
     '    flat = re.sub(r"\\s+", " ", _TAG_RE.sub(" ", text or "")).strip()',
     "    flat = (text or '').strip()", 1),
    ("M8 外壳可被模型覆盖", "backend/api/llm.py",
     '''    return {
        **payload,
        # 契约键放最后：LLM 解析出的 dict 里若恰好带 status/source/reason，不得覆盖外壳。
        "status": "ok" if result.source == "llm" else "degraded",
        "source": result.source,
        "reason": result.reason,
    }''',
     '''    out = {
        "status": "ok" if result.source == "llm" else "degraded",
        "source": result.source,
        "reason": result.reason,
    }
    out.update(payload)
    return out''', 1),
]


def _read(p: Path) -> bytes:
    return p.read_bytes()


def _write(p: Path, data: bytes):
    p.write_bytes(data)


def _md5(p: Path) -> str:
    return hashlib.md5(_read(p)).hexdigest()


def _replace(raw: bytes, old: str, new: str) -> bytes:
    for nl, note in (("\n", "LF"), ("\r\n", "CRLF")):
        o = old.replace("\n", nl).encode("utf-8")
        n = new.replace("\n", nl).encode("utf-8")
        if o in raw:
            assert raw.count(o) == 1, f"anchor not unique ({note}): {old[:40]!r} x{raw.count(o)}"
            return raw.replace(o, n)
    raise SystemExit(f"anchor not found in file: {old[:60]!r}")


def _run_tests():
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", *TESTS, "-q", "--no-header", "-p", "no:cacheprovider"],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    out = proc.stdout + proc.stderr
    m = re.search(r"(\d+) failed", out)
    m = int(m.group(1)) if m else 0
    return proc.returncode, m, out


def main():
    files = sorted({ROOT / f for f, *_ in [(m[1],) for m in MUTANTS]})
    baseline = {p: _md5(p) for p in files}
    code, failed, out = _run_tests()
    print(f"[基线] exit={code} failed={failed}\n{out.strip().splitlines()[-1]}")
    assert failed == 0, "变异前先绿，否则整份变异检验无意义"

    results = []
    for name, f, old, new, _expect in MUTANTS:
        p = ROOT / f
        orig = _read(p)
        _write(p, _replace(orig, old, new))
        code, failed, out = _run_tests()
        red = code != 0 and failed > 0
        _write(p, orig)
        assert _md5(p) == baseline[p], f"复原漂移: {p}"
        last = out.strip().splitlines()[-1] if out.strip() else "(no output)"
        results.append((name, red, failed, last))
        print(f"{'RED  ' if red else 'GREEN'} {name}: failed={failed} | {last}")

    bad = [r for r in results if not r[1]]
    print(f"\n===== 变异检验: {len(results) - len(bad)}/{len(results)} 变红 =====")
    for name, _r, _f, last in bad:
        print(f"  STILL-GREEN {name}: {last}")
    # 终局：所有文件须回到基线指纹
    for p, h in baseline.items():
        assert _md5(p) == h, f"终局指纹不符 {p}"
    print("工作区指纹已复原 OK")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
