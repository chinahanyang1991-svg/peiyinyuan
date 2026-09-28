"""
续测员1g Round-4 复跑包装器 (只调度既有 harness, 不改生产码)

与 round-2/rerun4.py 同构, 差别:
  - 产物前缀 result_R4G_ (落 qa/round-2/artifacts/, 不覆盖 3c 的 result_R4_* / R2 / R3 基线)
  - 只处理本次新写出的 result_*.json, 他人正在产出的文件绝不改名或覆盖
  - 除 checks 计数外, 额外打印 meta(环境/归因层) —— F-R4-5 要求跨轮次能比冷启动秒数

运行:
  set PYTHONIOENCODING=utf-8
  PYY_TEST_PORT=8151 python qa/harness/rerun1g.py run_p4_split.py
"""
import json
import sys
import importlib.util
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness_lib as H  # noqa: E402

R4 = H.QA_DIR / "round-2"
(R4 / "artifacts").mkdir(parents=True, exist_ok=True)
(R4 / "logs").mkdir(parents=True, exist_ok=True)
H.ARTIFACTS = R4 / "artifacts"
H.LOGS = R4 / "logs"
PREFIX = "result_R4G_"
KEEP_PREFIXES = ("result_R2_", "result_R3_", "result_R4_", "result_R4G_")


def summarize(tag):
    f = H.ARTIFACTS / f"result_{tag}.json"
    if not f.exists():
        print(f"[R4G] no result json for {tag}")
        return None
    j = json.loads(f.read_text(encoding="utf-8"))
    if not isinstance(j, dict) or "results" not in j:
        # 他人探针/工具写的非 Suite 结构产物 (checks/kv/passed 那一族) -> 原样留下, 不改名
        print(f"[R4G][{tag}] 跳过非 Suite 产物 {f.name} (顶层键={list(j)[:5]})")
        return None
    checks = [(r["name"], c["desc"], c["ok"], c["detail"])
              for r in j["results"] for c in r["checks"]]
    ok = [c for c in checks if c[2]]
    bad = [c for c in checks if not c[2]]
    print(f"[R4G][{tag}] checks PASS {len(ok)}/{len(checks)}  verdicts="
          + ",".join(f"{r['name']}:{r['verdict']}" for r in j["results"]))
    for name, desc, _o, detail in bad:
        print(f"[R4G][{tag}] NOT-PASS 用例={name[:24]} 断言={desc} detail={detail[:300]}")
    for k, v in (j.get("meta") or {}).items():
        print(f"[R4G][{tag}] meta.{k} = {str(v)[:400]}")
    dst = H.ARTIFACTS / f"{PREFIX}{tag}.json"
    if dst != f:
        f.replace(dst)
        print(f"[R4G] artifact -> {dst.name}")
    return len(ok), len(checks)


def run(script_name):
    path = H.HARNESS_DIR / script_name
    print(f"\n[R4G] === run {script_name} (port {H.TEST_PORT}, "
          f"backend_reuse={H.port_open()}) ===", flush=True)
    before = {p.name for p in H.ARTIFACTS.glob("result_*.json")}
    spec = importlib.util.spec_from_file_location(path.stem, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    try:
        mod.main()
    except SystemExit as e:
        print(f"[R4G] {script_name} sys.exit rc={e.code}")
    outs = []
    for p in sorted(H.ARTIFACTS.glob("result_*.json")):
        if p.name in before or p.name.startswith(KEEP_PREFIXES):
            continue
        r = summarize(p.name[len("result_"):-len(".json")])
        if r:
            outs.append(r)
    return outs


if __name__ == "__main__":
    print(f"[R4G] python={sys.executable} port={H.TEST_PORT}")
    rc = {}
    for name in sys.argv[1:]:
        rc[name] = run(name)
    print("\n[R4G] summary:")
    for k, v in rc.items():
        print(f"  {k}: {v}")
