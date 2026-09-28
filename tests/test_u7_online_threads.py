"""U-7：在线音色库的线程与网络收口（M-1 线程累积 / M-2 放弃后无账 + `..` 段门）

#89 那次修的是"同步 socket 别把事件循环按死"，但它留下两处新的：

  M-1  `search_all()` 每次自己 `ThreadPoolExecutor(max_workers=2)` 再
       `shutdown(wait=False, cancel_futures=True)`。并发 10 次搜索 = 瞬时 20 枚线程，
       而被"放弃"的那几枚还带着 socket 在跑。
  M-2  超时/放弃**一点账都不留**：撤掉了几个作业、放弃时还有几个在跑、超时率多少，
       事后无从查证（路由层那枚 504 只是个静态字符串）。

现在的口径：进程级共享池（并行度封顶 `SEARCH_POOL_SIZE`）+ 作业开工前自查剩余预算 +
每个未完成的 future 一律 `cancel()` 并分开记账 + 路由超预算落一条终态记录。

另附 `..` 段门：两道校验正则的字符类都含 `.`，`samples/../../x` 逐字符合法。
"""

import asyncio
import inspect
import sys
import threading
import time
import types

import pytest
from fastapi import FastAPI, HTTPException

from backend.api import voices as voices_api
from backend.services import voice_online
from modules.voice_manager.voice_crawler import online_net


class _Crawler:
    """假源：记录每次调用的线程名与拿到的 deadline_s，行为由构造参数决定。"""

    def __init__(self, name, delay=0.0, results=None, boom=False, ok=True):
        self.name = name
        self.delay = delay
        self.results = results if results is not None else [{"id": f"{name}-1"}]
        self.boom = boom
        self.ok = ok
        self.calls = []                    # deadline_s 列表（顺序即调用顺序）
        self.thread_names = []
        self._lock = threading.Lock()

    # --- 搜索用
    def search_with_status(self, query="", language=None, limit=20, deadline_s=None, **kw):
        with self._lock:
            self.calls.append(deadline_s)
            self.thread_names.append(threading.current_thread().name)
        if self.boom:
            raise RuntimeError(f"{self.name} 炸了")
        if self.delay:
            time.sleep(self.delay)
        return {"source": self.name, "ok": self.ok, "results": list(self.results),
                "endpoint": f"https://{self.name}.invalid/api", "http_code": 200 if self.ok else 0,
                "elapsed_ms": round(self.delay * 1000.0, 1),
                "reason": "" if self.ok else "unreachable",
                "error": "" if self.ok else "连接被拒", "attempts": []}

    # --- 详情/下载用（只装到"过没过门"为止，不需要真网络）
    def endpoint_status(self):
        return []

    @property
    def pool(self):
        return types.SimpleNamespace(status=lambda: {})

    @property
    def last_status(self):
        return {"ok": True, "source": self.name}

    def get_model_details(self, repo, deadline_s=None):
        return {"available": False, "reason": "unreachable", "error": "夹具不出网"}

    def resolve_repo(self, repo, deadline_s=None):
        return "main", {"ok": True}

    def audio_candidates(self, repo, deadline_s=None):
        return [], {}


def _stub_service(monkeypatch, hf, gh):
    """把 `backend.services.voice_service` 整块换掉。

    真实模块 import 要 15s（引擎装载），而且 `search_all` 是在函数体里 import 的——
    替 sys.modules 是唯一既不打真网络、又能测到那条 import 的路子。
    """
    mod = types.SimpleNamespace(
        voice_service=types.SimpleNamespace(hf_crawler=hf, github_crawler=gh))
    monkeypatch.setitem(sys.modules, "backend.services.voice_service", mod)
    return mod


@pytest.fixture(autouse=True)
def _quiet_pool():
    """每枚测试前后把池和账归零：线程累积的判据必须从同一起点量。"""
    voice_online.reset_search_stats()
    yield
    voice_online.shutdown_search_pool(drain=True)
    voice_online.reset_search_stats()


# ---------------------------------------------------------------- M-1：线程不随请求累积
def test_pool_is_process_level_singleton(monkeypatch):
    assert voice_online.search_pool() is voice_online.search_pool()

    hf, gh = _Crawler("huggingface", delay=0.01), _Crawler("github", delay=0.01)
    _stub_service(monkeypatch, hf, gh)
    voice_online.search_all("女声")
    voice_online.search_all("男声")
    voice_online.search_all("电子音")
    names = set(hf.thread_names) | set(gh.thread_names)
    assert names, "三枚请求里爬虫一次都没被调用，夹具失效"
    # 同一枚池线程被复用，而不是每请求新建
    assert all(n.startswith(voice_online.POOL_THREAD_PREFIX) for n in names), names
    assert len(names) <= voice_online.SEARCH_POOL_SIZE, names


def test_search_all_no_longer_builds_a_pool_per_request():
    """源码门：M-1 的成因就是"按请求建池再弃"。全模块只允许**一处**建池语句。

    口径：按 `= ThreadPoolExecutor(` 数，不按裸类名数——模块注释里正好在**引用**
    旧写法（`ThreadPoolExecutor(2)`），数字符串会把说明也算进去。
    """
    src = inspect.getsource(voice_online)
    assert src.count("= ThreadPoolExecutor(") == 1, "除 search_pool() 外不得再建池"
    for fn in (voice_online.search_all, voice_online.detail,
               voice_online.download_and_register):
        assert "ThreadPoolExecutor(" not in inspect.getsource(fn), fn.__name__
    body = inspect.getsource(voice_online.search_all)
    assert "search_pool()" in body
    assert "shutdown(" not in body, "池是进程级的，请求里关池就是 M-1"


def test_ten_concurrent_searches_do_not_accumulate_threads(monkeypatch):
    """验收判据①：并发 10 次跨源搜索，池内活跃线程数**封顶在并行度**、且回到基线。

    旧实现这里会是 20 枚（每请求 2 枚），且线程名不带前缀 —— 所以下面三条都得成立：
    峰值封顶、干活的线程全属共享池、结束后回到基线。
    """
    hf, gh = _Crawler("huggingface", delay=0.12), _Crawler("github", delay=0.12)
    _stub_service(monkeypatch, hf, gh)
    base = voice_online.pool_threads_alive()

    peak = {"n": base}
    stop = threading.Event()

    def watch():
        while not stop.is_set():
            n = voice_online.pool_threads_alive()
            if n > peak["n"]:
                peak["n"] = n
            time.sleep(0.004)

    monitor = threading.Thread(target=watch, name="u7-watcher")
    monitor.start()
    callers = [threading.Thread(target=voice_online.search_all, args=("女声",))
               for _ in range(10)]
    for t in callers:
        t.start()
    for t in callers:
        t.join(timeout=30)
    stop.set()
    monitor.join(timeout=5)

    assert not any(t.is_alive() for t in callers), "有请求没在 30s 内收尾"
    assert peak["n"] <= voice_online.SEARCH_POOL_SIZE, (
        f"池内活跃线程峰值 {peak['n']} 超过了并行度上限 {voice_online.SEARCH_POOL_SIZE}"
        "——线程又按请求累积了（M-1）")
    seen = set(hf.thread_names) | set(gh.thread_names)
    assert len(seen) == peak["n"] or len(seen) <= voice_online.SEARCH_POOL_SIZE, seen
    assert all(n.startswith(voice_online.POOL_THREAD_PREFIX) for n in seen), \
        f"有作业没跑在共享池上：{sorted(seen)[:3]}"
    assert voice_online.pool_threads_alive() <= voice_online.SEARCH_POOL_SIZE
    stats = voice_online.search_stats()
    assert stats["searches"] == 10 and stats["source_jobs"] == 20, stats
    assert stats["source_timeouts"] == 0 and stats["source_timeout_rate"] == 0.0, stats


# ---------------------------------------------------------------- M-2：放弃之后要有账
def test_abandoned_running_job_is_counted_not_forgotten(monkeypatch):
    """慢源：外层到点放弃，作业**已经在跑** → 撤不掉，必须记成 abandoned_still_running。"""
    hf = _Crawler("huggingface", delay=2.5)
    gh = _Crawler("github", delay=0.0)
    _stub_service(monkeypatch, hf, gh)
    payload = voice_online.search_all("女声", budget_s=2.0)

    by_name = {s["source"]: s for s in payload["sources"]}
    assert by_name["huggingface"]["reason"] == "source_timeout"
    assert by_name["github"]["ok"] is True                      # 不被慢源拖成整单失败
    st = voice_online.search_stats()
    assert st["source_jobs"] == 2 and st["source_timeouts"] == 1, st
    assert st["abandoned_still_running"] == 1, st
    assert st["cancelled_while_queued"] == 0, st
    assert st["source_timeout_rate"] == 0.5, st
    assert payload["deadline_s"] <= 2.0 and payload["skipped"] == 0, payload


def test_queued_job_is_cancelled_before_it_ever_requests(monkeypatch):
    """并行度 1 时第二枚作业还在队列里 → 外层放弃时**真取消**，一发请求都不发。

    旧实现每请求 2 枚工作线程，两源都能开工，这枚判据直接红。
    """
    voice_online.shutdown_search_pool(drain=True)
    monkeypatch.setattr(voice_online, "SEARCH_POOL_SIZE", 1)
    hf = _Crawler("huggingface", delay=2.5)
    gh = _Crawler("github", delay=0.0)
    _stub_service(monkeypatch, hf, gh)
    started = time.monotonic()
    payload = voice_online.search_all("女声", budget_s=2.0)
    took = time.monotonic() - started

    assert took < 2.0, f"外层放弃没生效，白等 {took:.1f}s"
    assert gh.calls == [], "被放弃的队列作业还是去发请求了"
    st = voice_online.search_stats()
    assert st["cancelled_while_queued"] == 1, st
    assert st["abandoned_still_running"] == 1, st
    assert st["source_timeouts"] == 2, st
    assert {s["reason"] for s in payload["sources"]} == {"source_timeout"}


def test_guarded_search_stand_down_without_issuing_a_request():
    """预算自查（队列排到点才轮到的作业）：让位 + 记 `skipped_no_budget`，不发请求。"""
    gh = _Crawler("github", delay=0.0)
    res = voice_online._guarded_search(gh, "github", "女声", None, 20,
                                       time.monotonic() - 0.1)
    assert res["ok"] is False and res["reason"] == "budget_exhausted", res
    assert res["results"] == []
    assert gh.calls == []
    assert voice_online.search_stats()["skipped_no_budget"] == 1


def test_guarded_search_passes_remaining_budget_into_the_client():
    """剩余预算要当 deadline 交给爬虫（爬虫再传进 httpx），并封顶在单源预算。"""
    near = _Crawler("github")
    voice_online._guarded_search(near, "github", "q", None, 20, time.monotonic() + 4.0)
    assert len(near.calls) == 1 and near.calls[0] is not None
    assert near.calls[0] <= 4.0, near.calls

    far = _Crawler("github")
    voice_online._guarded_search(far, "github", "q", None, 20,
                                 time.monotonic() + 600.0)
    assert far.calls[-1] == pytest.approx(voice_online.SOURCE_DEADLINE_S)


def test_http_deadline_is_wired_into_the_clients():
    """源码门（U-7 的网络那一半）：请求超时必须是**按剩余预算**算出来的，
    不是建池时一份固定配置。`request_timeout()` 里 connect 还钉死着，不能漂。
    """
    src = inspect.getsource(online_net)
    assert "timeout=request_timeout(left)" in src, "GET 侧没按剩余预算收紧"
    assert "timeout=request_timeout(max(1.0," in src, "下载侧没按剩余预算收紧"
    rs = inspect.getsource(online_net.request_timeout)
    assert "connect=CONNECT_TIMEOUT_S" in rs, "剩余预算不得放大连接阶段超时"


# ---------------------------------------------------------------- 路由层：终态记录
def test_route_timeout_leaves_a_terminal_record(monkeypatch):
    """504 不再是空口一句话：落一条终态记录，并把实时统计回给调用方。"""
    def _slow(*a, **kw):
        time.sleep(1.0)
        return {"ok": True, "results": [], "total": 0, "sources": []}

    voice_online.reset_search_stats()
    monkeypatch.setattr(voice_online, "ROUTE_DEADLINE_S", 0.2)
    monkeypatch.setattr(voice_online, "search_all", _slow)

    with pytest.raises(HTTPException) as ei:
        asyncio.run(voices_api.search_online_voices(query="温柔女声", language=None,
                                                   source="all", limit=5))
    assert ei.value.status_code == 504
    detail = ei.value.detail
    assert detail["reason"] == "deadline_exceeded"
    assert detail["abandoned"]["route_timeouts"] == 1, detail["abandoned"]
    recent = voice_online.search_stats()["recent"]
    assert recent and recent[0]["kind"] == "route_timeout"
    assert recent[0]["query"] == "温柔女声"


def test_search_reports_budget_and_pool_in_its_payload(monkeypatch):
    """`sources[]` 之外还要能看出这单用了多少预算、跑在哪个池上（docstring 的承诺）。"""
    hf, gh = _Crawler("huggingface"), _Crawler("github", ok=False)
    _stub_service(monkeypatch, hf, gh)
    payload = voice_online.search_all("女声")
    assert payload["deadline_s"] > 0
    assert "skipped" in payload
    assert payload["pool"] == {"shared": True,
                               "max_workers": voice_online.SEARCH_POOL_SIZE,
                               "threads_alive": payload["pool"]["threads_alive"]}
    assert payload["ok"] is True and payload["fabricated"] is False
    by_name = {s["source"]: s for s in payload["sources"]}
    assert by_name["github"]["ok"] is False and by_name["github"]["reason"] == "unreachable"
    assert by_name["github"]["error"]                        # 死源要带真实原因，不许静默


def test_all_sources_dead_is_not_ok_and_names_the_reason(monkeypatch):
    hf = _Crawler("huggingface", ok=False)
    gh = _Crawler("github", ok=False)
    _stub_service(monkeypatch, hf, gh)
    payload = voice_online.search_all("女声")
    assert payload["ok"] is False and payload["results"] == []
    assert "huggingface" in payload["message"] and "github" in payload["message"]


def test_worker_crash_does_not_take_the_other_source_down(monkeypatch):
    hf = _Crawler("huggingface", boom=True)
    gh = _Crawler("github")
    _stub_service(monkeypatch, hf, gh)
    payload = voice_online.search_all("女声")
    by_name = {s["source"]: s for s in payload["sources"]}
    assert by_name["huggingface"]["reason"] == "worker_crashed"
    assert by_name["huggingface"]["error"] and "RuntimeError" in by_name["huggingface"]["error"]
    assert payload["ok"] is True and payload["total"] == 1


def test_sources_status_exposes_the_accounting(monkeypatch):
    """`GET /online/sources` 是诊断入口：超时率与池内存活线程数得在这里能读到。"""
    hf, gh = _Crawler("huggingface"), _Crawler("github")
    _stub_service(monkeypatch, hf, gh)
    status = voice_online.sources_status()
    assert "search_stats" in status
    for key in ("source_timeout_rate", "pool_max_workers", "pool_threads_alive",
                "abandoned_still_running", "cancelled_while_queued"):
        assert key in status["search_stats"], key


# ---------------------------------------------------------------- `..` 段门
@pytest.mark.parametrize("value", [
    "..", "a/..", "../x", "samples/../../etc/passwd", "a/../../b", "x/y/../../z"])
def test_upward_segments_are_rejected(value):
    assert voice_online.has_upward_segment(value) is True, value


@pytest.mark.parametrize("value", [
    "hexgrad/Kokoro-82M", "samples/af_heart_0.wav", "a/b.wav", "Kokoro.82M", "", None,
    "my samples/af heart.wav"])
def test_legit_names_still_pass_the_guard(value):
    assert voice_online.has_upward_segment(value) is False


def test_regexes_do_really_let_upward_segments_through():
    """定罪依据：这两道正则**确实**放过 `..`（否则那道新门就是无病呻吟）。"""
    assert voice_online._SAFE_REPO.match("..")
    assert voice_online._SAFE_REPO.match("a/..")
    assert voice_online._SAFE_FILE.match("samples/../../etc/passwd")


@pytest.mark.parametrize("repo", ["..", "a/.."])
def test_detail_rejects_upward_repo(monkeypatch, repo):
    hf, gh = _Crawler("huggingface"), _Crawler("github")
    _stub_service(monkeypatch, hf, gh)
    res = voice_online.detail(repo)
    assert res["ok"] is False and res["reason"] == "bad_repo", res
    assert res["status"] == 400
    assert hf.calls == [] and gh.get_model_details is not None


def test_detail_does_not_false_positive_on_legit_repo(monkeypatch):
    """门不能误伤：合法名要走到爬虫那一步（爬虫夹具返回不可达，而不是 bad_repo）。"""
    hf, gh = _Crawler("huggingface"), _Crawler("github")
    _stub_service(monkeypatch, hf, gh)
    res = voice_online.detail("hexgrad/Kokoro-82M")
    assert res["reason"] == "unreachable", res


@pytest.mark.parametrize("file_path", ["samples/../../etc/passwd", "audio/../x.wav",
                                       "..", "../x.wav"])
def test_download_rejects_upward_file_path(monkeypatch, file_path):
    hf, gh = _Crawler("huggingface"), _Crawler("github")
    _stub_service(monkeypatch, hf, gh)
    res = voice_online.download_and_register("hexgrad/Kokoro-82M", file_path=file_path)
    assert res["ok"] is False and res["reason"] == "bad_repo", res
    assert not hf.calls and not gh.calls      # 校验在任何网络动作之前


def test_download_rejects_upward_repo(monkeypatch):
    hf, gh = _Crawler("huggingface"), _Crawler("github")
    _stub_service(monkeypatch, hf, gh)
    res = voice_online.download_and_register("a/..", file_path="samples/a.wav")
    assert res["ok"] is False and res["reason"] == "bad_repo", res


def test_every_validator_site_calls_the_guard():
    """源码门：门只写在函数里不算数——三处校验点都得真的调它。"""
    assert inspect.getsource(voice_online.detail).count("has_upward_segment(repo)") == 1
    dl = inspect.getsource(voice_online.download_and_register)
    assert dl.count("has_upward_segment(repo)") == 1
    assert dl.count("has_upward_segment(file_path)") == 1


# ---------------------------------------------------------------- HTTP 层外壳
# 这三条是被 HTTP 探针**反向定罪**补进来的：探针首轮把 `search_stats` 当顶层键、
# 把 400 的 reason 当顶层键去读，结果两枚 FAIL 是夹具错、产品对。外壳形状没人钉，
# 探针就会一直在这两处假红——把它钉成契约，谁改谁变红。
async def _asgi_client():
    import httpx

    app = FastAPI()
    app.include_router(voices_api.router, prefix="/api/voices")
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                             base_url="http://test")


async def test_sources_route_nests_the_accounting_under_sources(monkeypatch):
    """`GET /online/sources` 的账在 `sources.search_stats`，探针读的就是这层。"""
    hf, gh = _Crawler("huggingface"), _Crawler("github")
    _stub_service(monkeypatch, hf, gh)
    async with await _asgi_client() as client:
        resp = await client.get("/api/voices/online/sources")
    assert resp.status_code == 200, resp.status_code
    body = resp.json()
    assert body["status"] == "ok"
    assert "search_stats" in body["sources"], sorted(body["sources"])
    assert "source_timeout_rate" in body["sources"]["search_stats"]


async def test_download_route_wraps_the_reject_reason_in_detail(monkeypatch):
    """400 的 reason 在 `detail.reason` 里（HTTPException 的外壳），不在顶层。"""
    hf, gh = _Crawler("huggingface"), _Crawler("github")
    _stub_service(monkeypatch, hf, gh)
    async with await _asgi_client() as client:
        resp = await client.post("/api/voices/online/download", json={
            "model_name": "hexgrad/Kokoro-82M",
            "file_path": "samples/../../Windows/win.ini",
        })
    assert resp.status_code == 400, resp.status_code
    detail = resp.json()["detail"]
    assert detail["reason"] == "bad_repo", detail
    assert not hf.calls and not gh.calls


async def test_search_route_shell_is_status_wrapped_or_structured_error(monkeypatch):
    """200 时 `status/fabricated/results` 在顶层；失败时 reason 在 `detail.reason`。"""
    hf, gh = _Crawler("huggingface"), _Crawler("github")
    _stub_service(monkeypatch, hf, gh)
    async with await _asgi_client() as client:
        resp = await client.get("/api/voices/online/search",
                                params={"query": "voice", "limit": 5})
    body = resp.json()
    if resp.status_code == 200:
        assert body["status"] == "ok" and body["fabricated"] is False, sorted(body)
        assert isinstance(body["results"], list) and body["results"]
    else:
        assert resp.status_code in (502, 504), resp.status_code
        assert body["detail"]["reason"], body

