"""
配音员 Round-1 pipeline 测试 harness 公共库 (agent1-pipeline-tester)

原则:
  - 只读生产代码, 本文件不 import 任何会修改业务状态的逻辑
  - 所有结论必须落到 日志文件 + 磁盘产物
  - GBK 终端: 全 ASCII/中文输出, 严禁 emoji
"""

import io
import json
import os
import re
import sys
import time
import shutil
import signal
import socket
import subprocess
import threading
from pathlib import Path

# 强制 utf-8 输出, 避免 GBK 终端崩溃
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
if hasattr(sys.stdout, "buffer"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

HARNESS_DIR = Path(__file__).resolve().parent
QA_DIR = HARNESS_DIR.parent
PROJECT_ROOT = QA_DIR.parent
FIXTURES = HARNESS_DIR / "fixtures"
TMP = HARNESS_DIR / "tmp"
LOGS = QA_DIR / "round-1" / "logs"
ARTIFACTS = QA_DIR / "round-1" / "artifacts"

for _d in (FIXTURES, TMP, LOGS, ARTIFACTS):
    _d.mkdir(parents=True, exist_ok=True)

# 让 harness 能直接 import 生产包 (backend.* / modules.*), 与 uvicorn 启动时的 sys.path 一致
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# 独立测试端口, 避免与用户正在使用的 8000 冲突
TEST_PORT = int(os.environ.get("PYY_TEST_PORT", "8137"))
BASE = f"http://127.0.0.1:{TEST_PORT}"
API = BASE + "/api"

# 生产代码里的真实输出目录 (不得修改, 只读取)
OUTPUT_DIR = PROJECT_ROOT / "data" / "output"
BOOKS_DIR = PROJECT_ROOT / "data" / "books"
DB_PATH = PROJECT_ROOT / "data" / "peiyinyuan.db"


# ---------------------------------------------------------------- 日志
class Tee:
    """把 stdout 同时写到文件, 保证每个结论有日志证据。"""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.f = open(self.path, "w", encoding="utf-8", errors="replace")
        self._stdout = sys.stdout

    def __enter__(self):
        sys.stdout = _Writer(self._stdout, self.f)
        sys.stderr = _Writer(sys.__stderr__, self.f)
        return self

    def __exit__(self, *a):
        sys.stdout = self._stdout
        sys.stderr = sys.__stderr__
        self.f.close()


class _Writer:
    def __init__(self, orig, fh):
        self.orig = orig
        self.fh = fh

    def write(self, s):
        try:
            self.orig.write(s)
        except Exception:
            pass
        self.fh.write(s)
        self.fh.flush()

    def flush(self):
        try:
            self.orig.flush()
        except Exception:
            pass
        self.fh.flush()

    def isatty(self):
        return False


def log(msg=""):
    print(msg, flush=True)


def kv(k, v):
    log(f"    {k}: {v}")


# ---------------------------------------------------------------- 结果记录
class Result:
    """单个阶段/用例的判定结果。"""

    # 严重程度: 后写的高于先写的低 -> 取最大
    _RANK = {"PASS": 0, "BLOCKED": 1, "DEGRADED": 2, "FAIL": 3}

    def __init__(self, stage, name):
        self.stage = stage
        self.name = name
        self.verdict = "PASS"       # PASS / FAIL / BLOCKED / DEGRADED
        self.checks = []            # (desc, ok, detail)
        self.evidence = []          # 日志/产物路径
        self.notes = []

    def _downgrade(self, verdict):
        if self._RANK[verdict] > self._RANK[self.verdict]:
            self.verdict = verdict

    def check(self, desc, ok, detail=""):
        self.checks.append((desc, bool(ok), detail))
        mark = "OK  " if ok else "FAIL"
        log(f"      [{mark}] {desc}" + (f"  -> {detail}" if detail else ""))
        if not ok:
            self._downgrade("FAIL")
        return bool(ok)

    def fail(self, desc, detail=""):
        self.check(desc, False, detail)
        self._downgrade("FAIL")
        return False

    def block(self, desc, detail=""):
        self.checks.append((desc, False, detail))
        log(f"      [BLOCK] {desc}  -> {detail}")
        self._downgrade("BLOCKED")
        return False

    def degrade(self, desc, detail=""):
        """走了兜底/模拟/静音分支: 绝不记为 PASS。"""
        self.checks.append((desc, False, detail))
        log(f"      [DEGRADE] {desc}  -> {detail}")
        self._downgrade("DEGRADED")
        return False

    def evidence_add(self, p):
        self.evidence.append(str(p))

    def to_dict(self):
        return {
            "stage": self.stage, "name": self.name, "verdict": self.verdict,
            "checks": [{"desc": d, "ok": o, "detail": t} for d, o, t in self.checks],
            "evidence": self.evidence, "notes": self.notes,
        }


class Suite:
    def __init__(self, stage, script):
        self.stage = stage
        self.script = script
        self.results = []
        self.t0 = time.time()
        # meta: 运行环境/归因层字段 (F-R4-5: 当次冷启动秒数等), 与 checks 分离,
        # 便于跨轮次比较 "环境预算" 与 "逻辑断言" 两件事。
        self.meta = {}

    def new(self, name):
        r = Result(self.stage, name)
        self.results.append(r)
        log(f"  - 用例: {name}")
        return r

    def set_meta(self, key, value):
        self.meta[key] = value
        return value

    def finish(self, exit_when_fail=True):
        dur = time.time() - self.t0
        log("")
        log(f"  == [{self.stage}] 用时 {dur:.1f}s ==")
        for r in self.results:
            log(f"     {r.verdict:8s} {r.name}")
        if self.meta:
            log("  -- meta (环境/归因层) --")
            for k, v in self.meta.items():
                log(f"     {k}: {v}")
        payload = {
            "stage": self.stage, "script": self.script,
            "duration_s": round(dur, 1),
            "results": [r.to_dict() for r in self.results],
        }
        if self.meta:
            payload["meta"] = self.meta
        out = ARTIFACTS / f"result_{self.stage}.json"
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        log(f"  结果 JSON: {out}")
        bad = [r for r in self.results if r.verdict != "PASS"]
        if exit_when_fail and bad:
            sys.exit(1)
        return 0 if not bad else 1


# ---------------------------------------------------------------- HTTP
def http(method, path, json_body=None, files=None, data=None, timeout=60, raw=False):
    import urllib.request
    import urllib.error
    url = path if path.startswith("http") else API + path
    body = None
    headers = {}
    if json_body is not None:
        body = json.dumps(json_body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    elif files is not None or data is not None:
        boundary = "----pyyharness7f3a"
        buf = io.BytesIO()
        for k, v in (data or {}).items():
            buf.write(f"--{boundary}\r\n".encode())
            buf.write(f'Content-Disposition: form-data; name="{k}"\r\n\r\n'.encode())
            buf.write(str(v).encode("utf-8") + b"\r\n")
        for k, v in (files or {}).items():
            fname, content, ctype = v
            buf.write(f"--{boundary}\r\n".encode())
            buf.write(
                f'Content-Disposition: form-data; name="{k}"; filename="{fname}"\r\n'.encode("utf-8"))
            buf.write(f"Content-Type: {ctype}\r\n\r\n".encode())
            buf.write(content + b"\r\n")
        buf.write(f"--{boundary}--\r\n".encode())
        body = buf.getvalue()
        headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"

    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = resp.read()
            if raw:
                return resp.status, payload
            try:
                return resp.status, json.loads(payload.decode("utf-8"))
            except Exception:
                return resp.status, payload.decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        payload = e.read()
        if raw:
            return e.code, payload
        try:
            return e.code, json.loads(payload.decode("utf-8"))
        except Exception:
            return e.code, payload.decode("utf-8", "replace")
    except Exception as e:
        return 0, f"{type(e).__name__}: {e}"


def http_with_headers(method, path, json_body=None, timeout=60):
    """`http()` 的取头版：返回 (status, 小写响应头 dict, 原始字节)。

    存在理由：`http()` 丢掉响应头，而试听契约把"听到的嗓子是谁的"只写在
    `X-Preview-Voice-Binding` / `X-Preview-Origin` 里（backend/api/voices.py:226-238）。
    只要判据涉及"这条音色自己能不能出声"，就必须走这个入口，不能用字节数代替。
    """
    import urllib.request
    import urllib.error
    url = path if path.startswith("http") else API + path
    body = None
    headers = {}
    if json_body is not None:
        body = json.dumps(json_body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in (e.headers or {}).items()}, e.read()
    except Exception as e:
        return 0, {}, f"{type(e).__name__}: {e}".encode("utf-8")


# ---------------------------------------------------------------- 模型就绪等待 (F-R4-5)
# 冷启动量级 (本轮实测): CPU 加载 voxcpm 权重 220~488s。预算默认取 540s。
MODEL_READY_BUDGET_S = float(os.environ.get("PYY_MODEL_READY_TIMEOUT", "540"))
CLONE_TIMEOUT_S = float(os.environ.get("PYY_CLONE_TIMEOUT", "540"))


def model_status(timeout=20):
    """GET /api/system/model_status -> (code, 服务端状态机快照 dict)。"""
    code, body = http("GET", "/system/model_status", timeout=timeout)
    snap = body.get("model", {}) if isinstance(body, dict) else {}
    return code, (snap if isinstance(snap, dict) else {})


def wait_model_ready(total_timeout=None, poll=5.0, idle_grace=30.0, probe=None,
                     sleeper=None, clock=None):
    """显式等待 TTS 模型进入终态, 把"clone/合成是否命中已加载模型"从逻辑断言里剥离。

    只观测、不判定产品对错 —— 返回结构化观测值, 由调用方分层归因:
      ENV 层  : state 非 ready -> 记 BLOCKED/DEGRADED, 不得记产品 FAIL (F-R4-5)
      逻辑层  : 断言照常执行 (必要时改用库内既有音色兜底), 不得跳过

    `probe` / `sleeper` / `clock` 是为**变异检验**开的注入口: 变异脚本用脚本化的状态序列
    (loading -> ready / 恒 idle / 预算内不终态 / 端点不可达) 与虚拟时钟喂进来,
    在不启动后端的前提下验证观测值与早退逻辑, 以及调用方对每种观测值的归因。

    返回 dict:
      state        : **服务端状态机原值** (ready/failed/idle/loading); 取不到时为 unreachable
                     —— 预算耗尽不会伪造 state, 由 stop_reason='budget' 表达"没等到"
      stop_reason  : terminal(见终态) / idle_skipped(预热被跳过, 提前退) / budget(预算耗尽)
      load_seconds : 服务端自报的模型加载秒数 (= 当次冷启动秒数, 记进 artifact)
      waited_s     : 本次等待实际耗时
      skipped_reason: 预热被开关/内存门限跳过时的原因
      samples      : 轮询采样 [{t, state, seconds}] (最多留 6 条)
    """
    status = probe or model_status
    nap = sleeper or time.sleep
    now = clock or time.time
    budget = MODEL_READY_BUDGET_S if total_timeout is None else float(total_timeout)
    t0 = now()
    samples = []
    snap = {}
    last_code = 0
    stop_reason = "terminal"
    while True:
        waited = now() - t0
        last_code, snap = status()
        st = snap.get("state") or "unreachable"
        if last_code == 0:
            st = "unreachable"
        sec = snap.get("load_seconds")
        if sec is None:
            sec = snap.get("elapsed_seconds")
        samples.append({"t": round(waited, 1), "state": st, "seconds": sec})
        if st in ("ready", "failed"):
            stop_reason = "terminal"
            break
        if st == "idle" and waited > idle_grace:
            # 预热被跳过 (PYY_PRELOAD_MODEL=0 或内存门限): 继续等不会有变化,
            # 提前退出, 让调用方把"clone 必须同步付冷启动"这件事按 ENV 层处理。
            stop_reason = "idle_skipped"
            break
        if waited > budget:
            stop_reason = "budget"
            break
        nap(poll)
    waited = now() - t0
    out = {
        "state": snap.get("state") if last_code else "unreachable",
        "stop_reason": stop_reason,
        "load_seconds": snap.get("load_seconds"),
        "elapsed_seconds": snap.get("elapsed_seconds"),
        "engine": snap.get("engine"),
        "error": snap.get("error"),
        "skipped_reason": snap.get("skipped_reason"),
        "waited_s": round(waited, 1),
        "budget_s": budget,
        "endpoint_code": last_code,
        "samples": samples[-6:],
        "n_samples": len(samples),
    }
    kv("model_status 等待", f"state={out['state']} 服务端加载秒数={out['load_seconds']} "
                          f"本次等待 {out['waited_s']}s / 预算 {budget:.0f}s "
                          f"(轮询 {out['n_samples']} 次)")
    if out["skipped_reason"]:
        kv("  预热跳过原因", out["skipped_reason"])
    if out["error"]:
        kv("  加载错误", str(out["error"])[:160])
    return out


def is_env_transport_failure(code):
    """clone/synth 这类"必须等模型"的请求, 客户端超时(code=0)与服务端 5xx 属环境传导。

    F-R4-5 归因规则: 这类失败 **不得** 记成产品逻辑 FAIL; 但逻辑断言仍要照常执行。
    4xx 与 2xx 都不是 ENV 传输失败 (4xx = 真实产品契约, 照常判定)。
    """
    return code == 0 or code >= 500


# ---------------------------------------------------------------- 音频分析
def audio_stats(path):
    """返回时长/峰值/RMS/静音占比; 不依赖 ffmpeg (soundfile)。"""
    import numpy as np
    import soundfile as sf
    info = {"path": str(path), "exists": os.path.exists(path)}
    if not info["exists"]:
        return info
    info["size"] = os.path.getsize(path)
    try:
        data, sr = sf.read(str(path), dtype="float32")
    except Exception as e:
        info["error"] = f"soundfile 无法读取: {e}"
        return info
    if data.ndim > 1:
        data = data.mean(axis=1)
    info["sr"] = sr
    info["channels"] = 1
    info["samples"] = int(len(data))
    info["duration_s"] = round(len(data) / float(sr), 3) if sr else 0.0
    info["peak"] = round(float(np.max(np.abs(data))), 6) if len(data) else 0.0
    info["rms_db"] = round(float(20 * np.log10(np.sqrt(np.mean(data ** 2)) + 1e-12)), 2)
    thr = 1e-3
    info["active_ratio"] = round(float((np.abs(data) > thr).mean()), 4)
    # 最长连续静音
    mask = np.abs(data) > thr
    longest = cur = 0
    for v in mask:
        cur = 0 if not v else cur + 1
        longest = max(longest, cur)
    silence_longest = len(data) - longest
    info["longest_silence_s"] = round(silence_longest / float(sr), 3) if sr else 0.0
    info["is_silent"] = info["peak"] < 1e-4
    return info


def wav_segment_stats(path, boundaries=None):
    """按拼接边界列表检查每段是否非静音 (用于逐句验证)。"""
    import numpy as np
    import soundfile as sf
    data, sr = sf.read(str(path), dtype="float32")
    if data.ndim > 1:
        data = data.mean(axis=1)
    out = []
    if boundaries:
        prev = 0
        for b in list(boundaries) + [len(data)]:
            seg = data[prev:b]
            if len(seg):
                out.append({
                    "from": prev, "to": b,
                    "dur_s": round(len(seg) / sr, 3),
                    "peak": round(float(np.max(np.abs(seg))), 6),
                })
            prev = b
    return out


# ---------------------------------------------------------------- 后端进程控制
def port_open(port=TEST_PORT, host="127.0.0.1"):
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


def start_backend(log_path, port=TEST_PORT, wait=90, env_extra=None):
    """以真实生产入口 (uvicorn backend.main:app) 启动后端。

    env_extra: 只给"不需要模型"的用例（例如仅调导入/查询端点）传
    `{"PYY_PRELOAD_MODEL": "0"}` —— 走的是产品自带的显式配置口，
    省掉一份 ~5GB 的后台预装载，避免与 harness 进程内那份模型争内存。
    """
    if port_open(port):
        log(f"  [backend] 端口 {port} 已有服务, 复用")
        return None
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYY_LOG_LEVEL"] = "DEBUG"
    env["PEIYINYUAN_PORT"] = str(port)
    env["PYY_LOG_FILE"] = str(log_path)
    if env_extra:
        env.update({k: str(v) for k, v in env_extra.items()})
    cmd = [sys.executable, "-m", "uvicorn", "backend.main:app",
           "--host", "127.0.0.1", "--port", str(port), "--no-access-log"]
    log(f"  [backend] spawn: {' '.join(cmd)}")
    p = subprocess.Popen(cmd, cwd=str(PROJECT_ROOT), env=env,
                         stdout=open(log_path, "w", encoding="utf-8"),
                         stderr=subprocess.STDOUT)
    import urllib.request
    t0 = time.time()
    while time.time() - t0 < wait:
        if p.poll() is not None:
            log(f"  [backend] 进程提前退出 rc={p.returncode}, 见 {log_path}")
            return p
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=2) as r:
                if r.status == 200:
                    log(f"  [backend] ready in {time.time()-t0:.1f}s")
                    return p
        except Exception:
            time.sleep(0.5)
    log(f"  [backend] 启动超时 {wait}s")
    return p


def stop_backend(p):
    if p is None:
        return
    try:
        p.terminate()
        p.wait(timeout=15)
    except Exception:
        try:
            p.kill()
        except Exception:
            pass


# ---------------------------------------------------------------- DB (只读)
def db_rows(sql, args=()):
    import sqlite3
    uri = f"file:{DB_PATH.as_posix()}?mode=ro&immutable=0"
    con = sqlite3.connect(uri, uri=True, timeout=10)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute(sql, args).fetchall()]
    finally:
        con.close()


def fill_chapter_content(chs, timeout=120):
    """WF-09 起 `GET /books/{id}/chapters` 只下发 content_preview，`content` 恒为 null。

    需要整章正文的 harness 必须走懒加载出口逐章补（与前端 EditPage 同口径）；
    直读 ch["content"] 会拿到 None → 空串，于是所有"按正文断言"的门在零输入上恒真。
    """
    for ch in chs:
        if not ch.get("content"):
            _, cc = http("GET", f"/books/chapters/{ch['id']}/content", timeout=timeout)
            ch["content"] = (cc or {}).get("content") or ""
    return chs


# ---------------------------------------------------------------- 文本守恒度量 (F-R4-6)
# 汉字多重集用 CJK 统一表意文字 + 扩展 A + 兼容表意文字三段覆盖。
HAN_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")


def char_conservation_report(text_in, sentences_out):
    """把"分句是否丢字"拆成 **三栏独立度量** (F-R4-6)。

    旧口径 `total_out >= total_in*0.98` 把 排版空白 与 正文 混在同一个分子/分母里:
    分子 raw_out 含空白、分母 nonws_in 剔空白, 于是"丢正文"与"排版空白被合并"两件事
    互相抵消 —— 寻秦记真书上界内全书按 raw_out/raw_in 计得 97.91%(3c BIGBOOK 探针口径),
    按 run_p4 旧式 raw_out/nonws_in 计则恒 >=1.0, 白送约 2% 正文的静默丢失余量。
    本函数按语义分栏输出, 调用方只对"非空白守恒"和"汉字多重集差"下断言,
    空白差单独 kv 输出, 不参与判定。

    返回:
      raw_in/raw_out                       : 含空白的原始长度
      nonws_in/nonws_out/nonws_ratio       : 非空白字符数与守恒比
      ws_in/ws_out/ws_delta                : 空白字符数与差值 (排版空白, 单列不判)
      han_in/han_out                       : 汉字总数 (多重集)
      han_lost/han_gained                  : 多重集差 (in-out / out-in)
      han_lost_sample                      : 丢失汉字样本 (前 20 个)
    """
    from collections import Counter
    out_joined = "".join(sentences_out)
    nonws_in = re.sub(r"\s", "", text_in)
    nonws_out = re.sub(r"\s", "", out_joined)
    ws_in = len(text_in) - len(nonws_in)
    ws_out = len(out_joined) - len(nonws_out)
    han_in = Counter(HAN_RE.findall(text_in))
    han_out = Counter(HAN_RE.findall(out_joined))
    lost = {c: n - han_out.get(c, 0) for c, n in han_in.items() if n > han_out.get(c, 0)}
    gained = {c: n - han_in.get(c, 0) for c, n in han_out.items() if n > han_in.get(c, 0)}
    return {
        "raw_in": len(text_in),
        "raw_out": len(out_joined),
        "raw_ratio": round(len(out_joined) / max(len(text_in), 1), 6),
        "nonws_in": len(nonws_in),
        "nonws_out": len(nonws_out),
        "nonws_ratio": round(len(nonws_out) / max(len(nonws_in), 1), 6),
        "ws_in": ws_in,
        "ws_out": ws_out,
        "ws_delta": ws_in - ws_out,
        "han_in": sum(han_in.values()),
        "han_out": sum(han_out.values()),
        "han_lost": sum(lost.values()),
        "han_gained": sum(gained.values()),
        "han_lost_sample": "".join(sorted(lost)[:20]),
    }


def old_conservation_gate(rep, threshold=0.98):
    """复算 F-R4-6 修订**之前** run_p4_split.py:124 的单栏门限, 用于前后对照。

    旧式: total_in = 剔空白的输入字符数(nonws_in), total_out = 分句输出的原始字符数
    (raw_out, 含空白), 断言 raw_out >= nonws_in * 0.98。
    返回 loss_headroom_chars = raw_out - nonws_in*0.98:
    **这么多真实字符凭空消失, 旧断言依然报绿** —— 这就是它必须换掉的理由。
    """
    denom = max(rep["nonws_in"], 1)
    ratio = rep["raw_out"] / denom
    headroom = rep["raw_out"] - threshold * denom
    return {
        "formula": f"raw_out >= nonws_in * {threshold}",
        "ratio": round(ratio, 6),
        "passes": ratio >= threshold,
        "loss_headroom_chars": int(headroom),
        "loss_headroom_pct_of_body": round(headroom / denom, 6),
    }


# ---------------------------------------------------------------- 杂项
def which(name):
    return shutil.which(name)


def rel(p):
    try:
        return str(Path(p).relative_to(PROJECT_ROOT))
    except Exception:
        return str(p)


def ascii_name(s):
    import re
    s = re.sub(r"[^\w\-]", "_", s, flags=re.UNICODE)
    return re.sub(r"_+", "_", s)[:60]
