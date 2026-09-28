"""U-8 真 HTTP 并发消融探针：红线①「不卡顿」的量级实测，不是"机制成立待测"。

为什么要它：单元测试能证明"调用点搬进了线程"，但证明不了"导入一本真书期间
别的请求真的不卡"。定罪口径照抄 PY-340 —— **单发计时无判别力**，必须在飞重活
的同时打轻量端点，看它被拖多少。

五段（E 是归因，不是门）：
  A 空载基线   —— 采 `/api/health` 的 p50/p99，作为后面各段的对照；
  B 反向对照   —— 探针自带的 `/api/_probe/block`（**故意**在 `async def` 里
                  `time.sleep`）在飞时，p99 必须**冲破** 200ms。冲不破说明
                  测量器没判别力，后面各段的绿一律不作数；
  E 归因段     —— 服务端里**另起一根纯 Python 计算线程**（不碰 IO、不上传）时
                  `/api/health` 被拖到多少。这条量的是"搬到线程"这件事的**天花
                  板**：线程解的是"事件循环被按住"，解不了 GIL。C 段的残差落在
                  E 段的量级上，就说明剩下的账在 GIL（要再降只能上进程池），
                  而不是"线程没搬干净"；
  C 真书导入   —— 4.9MB 真 MOBI（入库 1,591,388 字）在飞时采样：p99 ≤ 预算，
                  并且**回读 DB 确认那本书真进了库**（产物≠执行者）；
  D 超限上传   —— 客户端宣称 78MB + 服务端 `MAX_UPLOAD_SIZE_MB=6`：必须被拒收，
                  服务端 RSS **在飞峰值**增量 ≤ 24MB（改动前是整份进堆），
                  库里不许多书，断流后服务必须还健康。

判据落在 C/D 两段；B 是夹具自检，E 只做归因。

D 段为什么不用普通 HTTP 客户端：见 `post_partial` 的docstring——"写完才读"在
服务端提前拒收时会卡在 TCP 重传上（实测 694 秒），量的是网络栈放弃时间，不是产品。

用法：
    PYTHONUTF8=1 python -X utf8 qa/harness/run_u8_loop_probe.py
自serve子进程由主流程自己拉起（`--serve` 分支），无需手工启动 :8000。
"""

from __future__ import annotations

import os
import socket
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

CONTROL_MIN_MS = 200.0          # 反向对照必须冲破这条，否则测量器无判别力
# C 段（真书导入在飞）的预算。**不是"要求降低了"**，是先把天花板量出来再定档。
# 同一枚探针、同一本真书，分别跑在**修前的树**（`863b8af` 的干净 worktree）与修后
# 的工作树上（2026-09-23 16:41 / 16:44 本机实测，非推导、非拼接）：
#                         修前 863b8af              修后（本工作树）
#   A 空载基线         p50 15.0 / p99 25.9      p50 2.3  / p99 28.3
#   B 故意同步阻塞 3s   p50 3003.2               p50 3005.2   （夹具自检）
#   E 纯 Python 计算线程 p50 98.3 / p99 173.2   p50 108.6 / p99 310.6  ← 无 IO 无上传
#   E2 整篇大字符串正则  p50 16.8 / max 575.4   p50 22.9 / p99 588.8
#   C 真 MOBI 导入在飞   n=3，**p99 10160.1**    n=81，p50 111.4 / p99 207.6 / max 226.8
#   D 超限 78.6MB 上传    RSS ** +83.2MB **       RSS +3.2MB
# 修前 C 段 10.2 秒里只挤出 3 枚采样、单枚 10160ms = **整枚导入期间循环全程按住**
# （那台服务器在此期间对任何请求都不响应）；修后同书同机 p99 207.6ms，49 倍。
# 剩下的百毫秒残差不再是"循环被按住"而是 GIL：E 段一根**只做 Python 计算**的线程
# 在两棵树上都复现出同量级中位数（98.3 / 108.6ms），E2 另给尖峰形态——单条不放
# GIL 的大字符串 C 调用能把轻量请求顶到 575~589ms。所以这条钉的是"循环不被按住"，
# 300/400ms 是**单进程 + 线程这个架构的下限带**（E 段单根线程就能把 p99 打到
# 173~311ms，可见这条带并不宽松）。再往下只有两条真办法：把整篇解析切成小块
# （消掉单条大 C 调用）或把解析挪进进程池；那是独立工程，已另立后续项，
# 不在这里冒充已完成。
HEALTH_P99_BUDGET_MS = 300.0
HEALTH_MAX_BUDGET_MS = 400.0
RSS_GROWTH_BUDGET_MB = 24.0     # 修前同一枚 78.6MB 超限体实测 +83.2MB；24MB 是带宽不是目标
POLL_INTERVAL_S = 0.015
NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# 真书：data/books 里字节数适中、且字数在 MAX_BOOK_CHARS(200万) 内可入库的那本
BIG_BOOK = Path("data/books/25a42f6b-0744-4ef5-b191-a4641fdfd070.mobi")
# 2026-09-23 实测入库字数（`scan_chapters` 口径）。改书/改解析器时这条会红，
# 那是**故意的**：C 段的绿必须建立在"真书真进了库"上，不是 200 状态码上。
EXPECTED_IMPORT_CHARS = 1591388


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ---------------------------------------------------------------- serve 分支

def build_app(tmp_data: Path, block_s: float = 3.0):
    """起一个只挂 books 路由的最小服务器，但 DB/存储**全部**指向 tmp。

    `backend/models/base.get_db_path()` 写死 `data/peiyinyuan.db`（不读
    Config.DB_PATH），所以必须在引擎落地前把 `_engine/_SessionLocal` 换掉
    —— 否则探针往产品库插书。
    """
    from fastapi import FastAPI
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from backend.config import Config
    from backend.models import base as db_base

    tmp_data.mkdir(parents=True, exist_ok=True)
    Config.DATA_DIR = tmp_data
    db_path = (tmp_data / "probe.db").as_posix()
    engine = create_engine(f"sqlite:///{db_path}",
                           connect_args={"check_same_thread": False})
    db_base.Base.metadata.create_all(engine)
    db_base._engine = engine
    db_base._SessionLocal = sessionmaker(bind=engine)

    app = FastAPI()
    from backend.api import books
    app.include_router(books.router, prefix="/api/books")

    @app.get("/api/health")
    async def health():
        return {"status": "ok", "t": time.time()}

    @app.get("/api/_probe/block")
    async def probe_block(sec: float = block_s):
        # 反向对照专用：**故意**同步阻塞，用来证明采样器看得见卡顿。
        time.sleep(float(sec))
        return {"status": "blocked", "sec": sec}

    @app.get("/api/_probe/books")
    async def probe_books():
        from sqlalchemy import text
        with engine.connect() as conn:
            rows = conn.execute(text(
                "SELECT id, title, total_chars FROM books")).fetchall()
        return {"count": len(rows),
                "rows": [{"id": r[0], "title": r[1], "chars": r[2]} for r in rows]}

    @app.get("/api/_probe/cpu")
    async def probe_cpu(ms: float = 3000.0, big: float = 0.0):
        """归因段用：**另起一根线程只算 CPU**，立即返回。两种口径分开量。

        - `big=0`：小步纯 Python 计算（生成器求和）。这类活儿每 5ms 就会因
          GIL 切换间隔让出一次循环，代表"普通 Python 计算"的下限；
        - `big=秒数`：对**一整篇 160 万字正文**反复做正则/切分/拼接——这类调用
          一进 C 层就把 GIL 攥到底，切换间隔对它无效，正是 MOBI 解析里
          `scan_chapters` / 全文正则的形状。

        两条一起才说得清 C 段的残差到底是"普通 GIL 争用"还是"单条不释放 GIL 的
        大字符串调用"——修法完全不同（前者要进程池，后者要把解析切块）。
        """
        def burn():
            end = time.perf_counter() + ms / 1000.0
            while time.perf_counter() < end:
                sum(i * i for i in range(2_000_000))

        def burn_big():
            import re
            text = ("第一章 起风了\n\n" + "他说：“正文内容，够长才行。”\n" * 40) * 1200
            end = time.perf_counter() + big / 1000.0
            while time.perf_counter() < end:
                re.sub(r"\s+", " ", text)
                text.split("“")
                "".join(reversed(text[:200000]))

        if ms:
            threading.Thread(target=burn, daemon=True).start()
        if big:
            threading.Thread(target=burn_big, daemon=True).start()
        return {"status": "burning", "ms": ms, "big": big}

    return app


def serve(port: int, tmp_data: Path) -> int:
    import uvicorn
    uvicorn.run(build_app(tmp_data), host="127.0.0.1", port=port,
                log_level="warning")
    return 0


# ---------------------------------------------------------------- 客户端侧

def http(method: str, url: str, body: bytes | None = None,
         headers: dict | None = None, timeout: float = 300.0):
    """返回 (状态码, 响应体, 毫秒)；连接被对端提前掐断时状态码记 0。

    记 0 而不是抛异常：超限上传被服务端中途拒收时，h11 可能在客户端还在写
    的时候就关掉连接——那是**另一种合法拒收**，必须能读出来并换成
    "库里没多出这本书 + 服务还活着" 这类判据，而不是让探针崩在夹具里。
    """
    req = urllib.request.Request(url, data=body, method=method,
                                 headers=headers or {})
    t0 = time.perf_counter()
    try:
        with NO_PROXY.open(req, timeout=timeout) as resp:
            return resp.status, resp.read(), (time.perf_counter() - t0) * 1000
    except urllib.error.HTTPError as e:
        return e.code, e.read(), (time.perf_counter() - t0) * 1000
    except (urllib.error.URLError, ConnectionError, OSError) as e:
        return 0, str(e).encode("utf-8", "replace"), (time.perf_counter() - t0) * 1000


def multipart(filename: str, payload: bytes,
              chunk: int = 1024 * 1024) -> tuple[bytes, str]:
    """流式拼 multipart（客户端也一次一块，别把 64MB 再造进内存）。"""
    boundary = f"----u8probe{uuid.uuid4().hex}"
    head = (f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            f"Content-Type: application/octet-stream\r\n\r\n").encode()
    tail = f"\r\n--{boundary}--\r\n".encode()
    body = bytearray(head)
    for i in range(0, len(payload), chunk):
        body += payload[i:i + chunk]
    body += tail
    return bytes(body), f"multipart/form-data; boundary={boundary}"


def post_partial(port: int, path: str, body: bytes, ctype: str,
                 read_deadline_s: float = 30.0):
    """**边写边探响应**的 POST：另起线程推 body，主线程只等响应头。

    为什么不用 `http()`：超限那一段的载荷比上限大一个量级，服务端在门点后
    就不再读请求体了；urllib"写完才读"会一直卡在 `sendall` 上，直到 TCP 重传
    超时——实测 694 秒。真实客户端（浏览器）是"看到响应就停止上传"，所以这里
    也照那个语义来：响应一到就返回，写线程随后随连接一起作废。

    返回 (状态码, 响应体尾部文本, 从发出到拿到响应头的毫秒)；0 = 连接被提前掐断。
    """
    t0 = time.perf_counter()
    sock = socket.create_connection(("127.0.0.1", port), timeout=5)
    err: dict = {}

    def writer():
        try:
            head = (f"POST {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
                    f"Content-Type: {ctype}\r\nContent-Length: {len(body)}\r\n"
                    f"Connection: close\r\n\r\n").encode("ascii")
            sock.sendall(head + body[:1024 * 1024])
            for i in range(1024 * 1024, len(body), 1024 * 1024):
                sock.sendall(body[i:i + 1024 * 1024])
        except OSError as e:
            err["writer"] = repr(e)

    th = threading.Thread(target=writer, daemon=True)
    th.start()
    sock.settimeout(read_deadline_s)
    buf = b""
    try:
        while b"\r\n\r\n" not in buf:
            piece = sock.recv(65536)
            if not piece:
                break
            buf += piece
    except OSError as e:
        err["reader"] = repr(e)
    status, text = 0, ""
    if buf:
        header_blob, _, rest = buf.partition(b"\r\n\r\n")
        first = header_blob.split(b"\r\n", 1)[0].decode("latin-1")
        parts = first.split(" ")
        if len(parts) >= 2 and parts[1].isdigit():
            status = int(parts[1])
        declared = 0
        for ln in header_blob.split(b"\r\n")[1:]:
            k, _, v = ln.partition(b":")
            if k.strip().lower() == b"content-length":
                declared = int(v.strip() or 0)
        got = rest
        while len(got) < declared:
            try:
                piece = sock.recv(65536)
            except OSError:
                break
            if not piece:
                break
            got += piece
        text = got.decode("utf-8", "replace")
    sock.close()
    err.pop("writer", None)      # 写线程被连接关闭打断是**预期**，不是失败
    return status, text or str(err), (time.perf_counter() - t0) * 1000


class Sampler(threading.Thread):
    """每隔 POLL_INTERVAL_S 打一次轻量端点，记录往返毫秒。"""

    def __init__(self, url: str):
        super().__init__(daemon=True)
        self.url = url
        self.samples: list[float] = []
        self._halt = threading.Event()

    def run(self):
        while not self._halt.is_set():
            _, _, ms = http("GET", self.url, timeout=60)
            self.samples.append(ms)
            time.sleep(POLL_INTERVAL_S)

    def start_sampling(self):
        self.start()

    def stop(self) -> list[float]:
        self._halt.set()
        self.join(timeout=5)
        return self.samples


class RssWatcher(threading.Thread):
    """在飞采样服务端 RSS，取峰值。

    为什么不能"上传结束后再读一次"：改动前那个整份 `await file.read()` 的
    字节串在 400 抛出后就被释放了，事后读到的 RSS 会**假绿**（PY-340 同一条
    教训的反面形态：计时点选错，缺陷就看不见）。
    """

    def __init__(self, pid: int):
        super().__init__(daemon=True)
        self.pid = pid
        self.peak = 0.0
        self._halt = threading.Event()

    def run(self):
        while not self._halt.is_set():
            try:
                self.peak = max(self.peak, rss_mb(self.pid))
            except Exception:
                return
            time.sleep(0.02)

    def stop(self) -> float:
        self._halt.set()
        self.join(timeout=5)
        return self.peak


def pct(samples: list[float], q: float) -> float:
    if not samples:
        return float("nan")
    s = sorted(samples)
    idx = min(len(s) - 1, max(0, int(round(q * (len(s) - 1)))))
    return s[idx]


def describe(name: str, samples: list[float]) -> dict:
    if not samples:
        print(f"  [{name}] 无采样（测量无效）")
        return {"name": name, "n": 0}
    out = {"name": name, "n": len(samples),
           "p50": round(pct(samples, 0.50), 1),
           "p99": round(pct(samples, 0.99), 1),
           "max": round(max(samples), 1)}
    print(f"  [{name}] n={out['n']} p50={out['p50']}ms "
          f"p99={out['p99']}ms max={out['max']}ms")
    return out


def rss_mb(pid: int) -> float:
    import psutil
    return psutil.Process(pid).memory_info().rss / 1024 / 1024


def _book_rows(base: str) -> list[dict]:
    code, raw, _ = http("GET", base + "/api/_probe/books", timeout=10)
    if code != 200:
        raise RuntimeError(f"回读 books 失败：{code} {raw[:120]!r}")
    import json
    return json.loads(raw.decode("utf-8"))["rows"]


def main() -> int:
    if not BIG_BOOK.exists():
        print(f"FAIL: 真书不在盘上 {BIG_BOOK}")
        return 2
    book_bytes = BIG_BOOK.read_bytes()
    big_upload = book_bytes * (64 // (len(book_bytes) // 1024 // 1024) or 12)
    print(f"[probe] 真书 {BIG_BOOK.name} = {len(book_bytes)/1024/1024:.1f}MB；"
          f"超限上传体 = {len(big_upload)/1024/1024:.1f}MB")

    tmp = Path(tempfile.mkdtemp(prefix="u8_probe_"))
    port = free_port()
    # 子进程 stderr 必须落在文件里：首轮探针 90s 起不来，输出被 DEVNULL 吞了，
    # 只能靠手工复跑 --serve 才看到"参数校验写错、子进程秒退"这种夹具缺陷。
    child_log = open(tmp / "server_stderr.log", "wb")
    child = subprocess.Popen(
        [sys.executable, "-X", "utf8", str(Path(__file__)), "--serve",
         str(port), str(tmp / "data")],
        cwd=str(ROOT), stdout=child_log, stderr=subprocess.STDOUT,
        env={**os.environ, "PYTHONUTF8": "1", "MAX_UPLOAD_SIZE_MB": "6"})
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.time() + 90
        while time.time() < deadline:
            try:
                code, _, _ = http("GET", base + "/api/health", timeout=2)
                if code == 200:
                    break
            except OSError:
                time.sleep(0.3)
        else:
            child_log.flush()
            try:
                tail = (tmp / "server_stderr.log").read_bytes()[-1500:]
            except OSError:
                tail = b"<child log unavailable>"
            print("FAIL: 探针服务器 90s 内没起来 —— 子进程输出尾部：")
            print(tail.decode("utf-8", "replace"))
            return 2
        print(f"[probe] 服务器已起：pid={child.pid} port={port}")

        fails: list[str] = []

        # A 空载基线
        print("[A] 空载基线")
        s = Sampler(base + "/api/health")
        s.start_sampling()
        time.sleep(1.0)
        a = describe("A 空载", s.stop())

        # B 反向对照：故意同步阻塞 3s，采样必须看得见
        print("[B] 反向对照（探针自带阻塞端点，不是被测产品）")
        s = Sampler(base + "/api/health")
        s.start_sampling()
        _, _, block_ms = http("GET", base + "/api/_probe/block?sec=3", timeout=60)
        b = describe("B 对照", s.stop())
        if b["p99"] < CONTROL_MIN_MS:
            fails.append(f"B 段 p99={b['p99']}ms 未冲破 {CONTROL_MIN_MS}ms："
                         "采样器看不见 3 秒同步阻塞，C/D 段的绿不作数")

        # E 归因段（不是门）：另起一根纯 Python 计算线程，量 GIL 的天花板
        print("[E] 归因：服务端内一根纯计算线程（无 IO、无上传）")
        s = Sampler(base + "/api/health")
        s.start_sampling()
        http("GET", base + "/api/_probe/cpu?ms=4000", timeout=10)
        time.sleep(4.2)
        e = describe("E 纯计算线程", s.stop())
        gil_floor_ms = e.get("p50", float("nan"))
        print(f"  归因：GIL 争用下的空载 p50 = {gil_floor_ms}ms "
              f"（A 段同口径 {a.get('p50')}ms）")

        # E2 归因：一根只做"超大字符串整段正则"的线程
        print("[E2] 归因：另起一根线程只对 160 万字整篇跑正则/切分")
        s = Sampler(base + "/api/health")
        s.start_sampling()
        http("GET", base + "/api/_probe/cpu?big=4000", timeout=10)
        time.sleep(4.2)
        e2 = describe("E2 大字符串线程", s.stop())
        print(f"  对照：A 空载 p50={a.get('p50')} | E 小步计算 p50={e.get('p50')} "
              f"| E2 整篇正则 p50={e2.get('p50')}（p99={e2.get('p99')}/"
              f"max={e2.get('max')}）")

        # C 真书导入期间打 health
        print("[C] 真书导入在飞")
        body, ctype = multipart(BIG_BOOK.name, book_bytes)
        s = Sampler(base + "/api/health")
        s.start_sampling()
        t0 = time.perf_counter()
        code, raw, dt = http("POST", base + "/api/books/import", body,
                             {"Content-Type": ctype})
        c = describe("C 导入在飞", s.stop())
        printed = raw.decode("utf-8", "replace")[:160]
        print(f"  import -> {code} in {time.perf_counter() - t0:.1f}s :: {printed}")
        if code != 200:
            fails.append(f"C 段导入未成功（{code}），后判无意义")
        else:
            rows = _book_rows(base)
            print(f"  回读 DB -> {rows}")
            if not any(int(r["chars"]) == EXPECTED_IMPORT_CHARS for r in rows):
                fails.append(f"C 段回读不到 {EXPECTED_IMPORT_CHARS} 字那行："
                             "导入没真的入库（产物≠执行者）")
        if c.get("n", 0) < 20:
            fails.append(f"C 段只采到 {c.get('n')} 枚，采样太稀")
        if c.get("p99", 1e9) > HEALTH_P99_BUDGET_MS:
            fails.append(f"C 段 p99={c['p99']}ms > {HEALTH_P99_BUDGET_MS}ms")
        if c.get("max", 1e9) > HEALTH_MAX_BUDGET_MS:
            fails.append(f"C 段出现 {c['max']}ms 单枚尖峰 > {HEALTH_MAX_BUDGET_MS}ms")

        # D 超限上传：边写边探响应 + 在飞 RSS
        print("[D] 超限上传（服务端 MAX_UPLOAD_SIZE_MB 临时压到 6MB，"
              f"客户端宣称 {len(big_upload) / 1024 / 1024:.0f}MB）")
        body, ctype = multipart("oversize.txt", big_upload)
        rows_before = len(_book_rows(base))
        rss_before = rss_mb(child.pid)
        s = Sampler(base + "/api/health")
        w = RssWatcher(child.pid)
        s.start_sampling()
        w.start()
        code, text, dt = post_partial(port, "/api/books/import", body, ctype)
        d = describe("D 超限上传在飞", s.stop())
        peak = w.stop()
        rows_after = len(_book_rows(base))
        print(f"  upload -> {code} in {dt / 1000:.1f}s :: {text[:120]}")
        print(f"  服务端 RSS {rss_before:.1f}MB -> 峰值 {peak:.1f}MB "
              f"(增量 {peak - rss_before:.1f}MB / 预算 {RSS_GROWTH_BUDGET_MB}MB)")
        # 0 = 连接被服务端提前掐断：也算拒收，但要额外证"没入库 + 服务还活着"
        rejected = code == 400 or code == 0
        if not rejected:
            fails.append(f"D 段超限上传应被拒收（400），实得 {code}")
        if rows_after != rows_before:
            fails.append(f"D 段超限上传后库里多出 {rows_after - rows_before} 本书："
                         "拒收却没回滚")
        alive, _, alive_ms = http("GET", base + "/api/health", timeout=10)
        print(f"  断流后健康检查 -> {alive} in {alive_ms:.0f}ms")
        if alive != 200:
            fails.append("D 段拒收后服务不再健康（连接没被干净地丢弃）")
        if peak - rss_before > RSS_GROWTH_BUDGET_MB:
            fails.append(f"D 段服务端 RSS 在飞峰值涨了 {peak - rss_before:.1f}MB —— "
                         "仍在整份缓冲上传体")

        print("\n=== U-8 事件循环消融探针 ===")
        print(f"A 空载 p99={a.get('p99')}ms | B 对照 p99={b.get('p99')}ms "
              f"| E 小步计算 p99={e.get('p99')}ms | E2 整篇正则 p99={e2.get('p99')}ms "
              f"| C 导入 p99={c.get('p99')}ms/max={c.get('max')}ms "
              f"| D 超限 p99={d.get('p99')}ms")
        if fails:
            for f in fails:
                print("FAIL:", f)
            return 1
        print(f"PASS: 真书导入在飞时 /api/health p99 ≤ {HEALTH_P99_BUDGET_MS}ms "
              f"且无 >{HEALTH_MAX_BUDGET_MS}ms 尖峰；超限上传不再整份进内存")
        return 0
    finally:
        child.terminate()
        try:
            child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            child.kill()
        child_log.close()          # Windows 上句柄不关，下面的 rmtree 清不掉目录
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    argv = sys.argv[1:]
    if argv[:1] == ["--serve"]:
        # argv = ["--serve", PORT, TMPDATA]（`--serve` 本身已在 argv[0]）
        if len(argv) != 3 or not argv[1].isdigit():
            raise SystemExit("用法：--serve PORT TMPDATA")
        raise SystemExit(serve(int(argv[1]), Path(argv[2])))
    raise SystemExit(main())
