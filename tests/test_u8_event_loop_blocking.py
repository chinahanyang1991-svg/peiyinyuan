"""U-8：重 IO/重计算不得压在事件循环上（红线①「不卡顿」的第二条病灶）。

定罪基准 = **改动前的真实代码**（`git show HEAD:backend/api/books.py`），不是
凭空造一个阻塞函数。三处病灶同族：

1. `POST /books/import`：`content = await file.read()`（≤500MB 整份进堆）
   + `await file.seek(0)` + **同步** `book_service.import_book(file)` —— 于是
   `copyfileobj` + `ParserFactory.parse` + `scan_chapters` + 逐章 INSERT 全在
   循环上跑完；
2. `POST /books/import/batch`：`book_service.import_batch(files)` 直接调用；
3. `POST /dubbing/projects/{id}/preview`：`get_loaded("voxcpm")` +
   `synthesize_with_gate` + `record_last_successful_seed` + `sf.write` 全在循环上。

判别力口径沿用 PY-340 的教训：**单发计时对这类缺陷不具判别力**（"这个请求
本来就慢"看不出它冻住了别人）。所以每处都用 ticker 量"该请求在飞时事件循环
的空隙"——阻塞版空隙 ≈ 阻塞时长，线程版 ≈ 毫秒级调度抖动。
"""
import asyncio
import errno
import inspect
import shutil
import sys
import time
import types
from pathlib import Path

import pytest
from fastapi import HTTPException

from backend.api import books, dubbing
from backend.config import Config
from backend.services.book_service import book_service

# 阻塞夹具时长。0.6s 与预算 0.15s 之间留 4 倍带：GIL/CPU 争用只会把线程版的
# 空隙推到毫秒级，推不进 150ms；而被同步阻塞的循环必然是 600ms 整。
BLOCK_S = 0.6
LOOP_GAP_BUDGET_S = 0.15


def _blocking(fn, name):
    """把 fn 换成"睡 BLOCK_S 秒再走原逻辑"的同步替身（复刻阻塞点，不换语义）。"""

    def inner(*a, **k):
        time.sleep(BLOCK_S)
        return fn(*a, **k)

    inner.__name__ = name
    return inner


async def _with_ticker(coro):
    """跑 coro，同时量事件循环空隙。返回 (结果, 空隙列表)。

    必须**先让 ticker 拿到一次调度权**再开测：`await <无 await 的协程>` 是在当前
    步里一路跑完的（不经过事件循环），若 ticker 还没定下 `last`，那 0.6s 的阻塞
    恰好发生在测量窗口之前 —— 夹具会"绿得看不到缺陷"。这条是本体测出来的
    （首版漏了预热，反向定罪测试 max 只有 16ms）。
    """
    gaps = []
    stop = asyncio.Event()

    async def ticker():
        last = time.perf_counter()
        while not stop.is_set():
            await asyncio.sleep(0.01)
            now = time.perf_counter()
            gaps.append(now - last)
            last = now

    task = asyncio.create_task(ticker())
    # 必须先把循环让出去：`create_task` 只是排期，而 `await coro` 若一路同步到底
    # （正是被测缺陷形态）就不会给 ticker 第一次调度的机会 —— 测量器会在阻塞
    # **之后**才开始计时，把红判成绿（首版实测：0.6s 同步块只量到 16ms）。
    await asyncio.sleep(0)
    try:
        out = await coro
        await asyncio.sleep(0.02)
    finally:
        stop.set()
        await task
    return out, gaps


def _assert_loop_alive(gaps, where):
    assert gaps, "ticker 没跑到，测量无效（夹具问题，不作产品绿）"
    worst = max(gaps)
    assert worst < LOOP_GAP_BUDGET_S, (
        f"{where}：在飞请求把事件循环按住 {worst * 1000:.0f}ms —— "
        f"handler 里又有同步阻塞（对照 PY-340 定罪现场 1748ms）"
    )


class FakeUpload:
    """够用的 UploadFile 替身：**记录每一次 read 的口径**。

    `sizes` 里出现 `None` 就是"整份读进内存"（改动前的 `await file.read()`），
    这正是本文件第 1 条断言的定罪依据。
    """

    def __init__(self, filename, payload=b"", chunk_size=4096, infinite=False):
        self.filename = filename
        self._payload = payload
        self._pos = 0
        self._chunk = chunk_size
        self._infinite = infinite
        self.sizes = []
        self.seek_calls = 0
        self.file = _SyncFile(payload)

    async def read(self, size=None):
        self.sizes.append(size)
        if self._infinite:
            return b"x" * (size or self._chunk)
        if size is None:
            rest = self._payload[self._pos:]
            self._pos = len(self._payload)
            return rest
        rest = self._payload[self._pos:self._pos + size]
        self._pos += len(rest)
        return rest

    async def seek(self, offset):
        self.seek_calls += 1
        self._pos = offset

    @property
    def pulled(self) -> int:
        return sum(s or 0 for s in self.sizes)


class _SyncFile:
    """薄壳路径要用的 `.file`（真 UploadFile 的 SpooledTemporaryFile 替身）。"""

    def __init__(self, payload=b""):
        from io import BytesIO
        self._buf = BytesIO(payload)

    def seek(self, *a):
        return self._buf.seek(*a)

    def read(self, n=-1):
        return self._buf.read(n)


@pytest.fixture
def spool_root(tmp_path, monkeypatch):
    """把 DATA_DIR 挪进 tmp_path：中转件/存储件都不许碰真库目录。"""
    root = tmp_path / "data"
    monkeypatch.setattr(Config, "DATA_DIR", root, raising=True)
    return root


# ---------------------------------------------------------------- 病灶 1：导入

async def test_import_spools_in_chunks_and_never_reads_whole_file(
        spool_root, monkeypatch):
    """`await file.read()`（无长度）= 500MB 上限整份进堆，必须绝迹。"""
    payload = "第一章 起风\n\n" + "正文内容。" * 500
    up = FakeUpload("chunked.txt", payload.encode("utf-8"))
    landed = {}

    def fake_import(spool_path, upload_name=None, storage_dir=None, encoding=None):
        landed["bytes"] = Path(spool_path).read_bytes()
        landed["name"] = upload_name
        return {"id": "b1"}

    monkeypatch.setattr(book_service, "import_book_from_path", fake_import)
    out = await books.import_book(file=up, encoding="auto")

    assert out == {"status": "ok", "book": {"id": "b1"}}
    assert None not in up.sizes, f"又出现无长度 read：{up.sizes[:3]}"
    assert set(up.sizes) == {book_service.UPLOAD_CHUNK_BYTES}, up.sizes[:5]
    assert landed["bytes"] == payload.encode("utf-8")   # 分块写完内容必须逐字节一致
    assert landed["name"] == "chunked.txt"


async def test_oversize_aborts_midstream_without_buffering(spool_root, monkeypatch):
    """超上限要**边写边断**，不是"先整份读进来再比大小"。

    载荷给**有限**的 3MB（不是无限流）：无限流下"门失效"的变异靠 pytest 超时才变红，
    那是假红 —— 有限载荷下门一失效就直接 200，红得干脆。
    """
    monkeypatch.setattr(Config, "MAX_UPLOAD_SIZE_MB", 1, raising=True)
    up = FakeUpload("huge.txt", b"x" * (3 * 1024 * 1024))
    called = []
    monkeypatch.setattr(book_service, "import_book_from_path",
                        lambda *a, **k: called.append(1))

    with pytest.raises(HTTPException) as ei:
        await books.import_book(file=up, encoding="auto")

    assert ei.value.status_code == 400
    assert f"{Config.MAX_UPLOAD_SIZE_MB}MB" in str(ei.value.detail)
    assert not called, "超限文件不该走到入库"
    # 断流后最多只多拷两块；改动前这里是"无限大"（整份进内存）。
    assert up.pulled <= 1024 * 1024 + 2 * book_service.UPLOAD_CHUNK_BYTES, up.pulled


async def test_disk_floor_gate_rejects_before_writing(spool_root, monkeypatch):
    """流式落盘把 OOM 换成了 ENOSPC，所以收前必须有一道余量门。"""
    monkeypatch.setattr(shutil, "disk_usage",
                        lambda p: type("U", (), {"free": 1024})())
    up = FakeUpload("x.txt", "第一章\n\n正文".encode("utf-8"))

    with pytest.raises(HTTPException) as ei:
        await books.import_book(file=up, encoding="auto")

    assert ei.value.status_code == 507
    assert "MIN_FREE_DISK_MB" in str(ei.value.detail)
    assert list(book_service.spool_dir().glob("*")) == [], "拒收前不该留下半截上传"


async def test_import_runs_off_the_event_loop(spool_root, monkeypatch):
    """并发消融：入库阻塞 0.6s 期间，循环空隙必须仍是毫秒级。"""
    up = FakeUpload("slow.txt", "第一章\n\n正文".encode("utf-8"))
    monkeypatch.setattr(book_service, "import_book_from_path",
                        _blocking(lambda *a, **k: {"id": "b1"}, "import"))
    out, gaps = await _with_ticker(books.import_book(file=up, encoding="auto"))
    assert out["book"] == {"id": "b1"}
    _assert_loop_alive(gaps, "POST /books/import")


async def test_spool_never_survives_the_request(spool_root, monkeypatch):
    """成功(搬家)/失败(抛错)/拒收(400) 三条路都不许留中转件。"""
    up = FakeUpload("a.txt", b"body")

    def boom(*a, **k):
        raise ValueError("解析失败")

    monkeypatch.setattr(book_service, "import_book_from_path", boom)
    with pytest.raises(HTTPException) as ei:
        await books.import_book(file=up, encoding="auto")
    assert ei.value.status_code == 400
    assert list(book_service.spool_dir().glob("spool_*")) == []

    monkeypatch.setattr(book_service, "import_book_from_path",
                        lambda *a, **k: {"id": "b1"})
    await books.import_book(file=FakeUpload("b.txt", b"body"), encoding="auto")
    assert list(book_service.spool_dir().glob("spool_*")) == []


# ---------------------------------------------------------------- `_relocate`

def test_relocate_renames_instead_of_recopying(tmp_path, monkeypatch):
    """同卷必须走 `os.replace`（0 字节拷贝）；拷一份 500MB 正是原来的阻塞源。"""

    def no_copy(*a, **k):
        raise AssertionError("又退回到拷贝语义了")

    monkeypatch.setattr(shutil, "copy2", no_copy)
    src = tmp_path / "spool.txt"
    src.write_bytes("正文" .encode("utf-8"))
    dst = tmp_path / "books" / "uuid.txt"
    dst.parent.mkdir()
    book_service._relocate(str(src), dst)
    assert dst.read_bytes() == "正文".encode("utf-8")
    assert not src.exists(), "搬家后中转件必须不在原地"


def test_relocate_falls_back_on_cross_device(tmp_path, monkeypatch):
    """EXDEV（跨卷）不是世界末日：退回 copy2 + 删源，且不静默丢上传。"""
    used = {"copy2": 0}
    real_copy2 = shutil.copy2

    def fake_replace(a, b):
        raise OSError(errno.EXDEV, "cross-device link")

    def spy_copy2(a, b):
        used["copy2"] += 1
        return real_copy2(a, b)

    monkeypatch.setattr("os.replace", fake_replace)
    monkeypatch.setattr(shutil, "copy2", spy_copy2)
    src = tmp_path / "spool.txt"
    src.write_bytes(b"payload")
    book_service._relocate(str(src), tmp_path / "dst.txt")
    assert used["copy2"] == 1
    assert (tmp_path / "dst.txt").read_bytes() == b"payload"
    assert not src.exists()


# ---------------------------------------------------------------- 病灶 2：批量

async def test_batch_import_runs_off_the_event_loop(monkeypatch):
    monkeypatch.setattr(book_service, "import_batch",
                        _blocking(lambda files, encoding=None: [{"success": True}],
                                  "import_batch"))
    out, gaps = await _with_ticker(
        books.import_books_batch(files=[FakeUpload("a.txt", b"x")]))
    assert out["results"] == [{"success": True}]
    _assert_loop_alive(gaps, "POST /books/import/batch")


# ---------------------------------------------------------------- 病灶 3：试听

async def test_preview_synthesis_runs_off_the_event_loop(monkeypatch, tmp_path):
    monkeypatch.setattr(dubbing.dubbing_service, "get_project", lambda pid: {
        "global_settings": {}, "assignments": [{"voice_id": "v1"}]})
    wav = tmp_path / "preview.wav"
    wav.write_bytes(b"RIFF")

    def blocking_render(project_id, voice_id, settings):
        time.sleep(BLOCK_S)
        return str(wav), 1234

    monkeypatch.setattr(dubbing, "_render_preview", blocking_render)
    out, gaps = await _with_ticker(dubbing.preview_dubbing("p1"))
    _assert_loop_alive(gaps, "POST /dubbing/.../preview")
    assert out.path == str(wav)
    assert out.headers["x-last-successful-seed"] == "1234"
    assert out.headers["content-disposition"] == "inline"


@pytest.mark.parametrize("status,text", [
    (503, "TTS 引擎模型加载失败：权重缺失"),
    (502, "试听音频未通过质量门: silent"),
])
async def test_preview_error_codes_still_pass_through(monkeypatch, status, text):
    """搬到线程里不许改掉 503/502/500 三档口径（PY-118/P0-4 的翻译层）。"""
    monkeypatch.setattr(dubbing.dubbing_service, "get_project", lambda pid: {
        "global_settings": {}, "assignments": [{"voice_id": "v1"}]})

    def raiser(project_id, voice_id, settings):
        raise HTTPException(status, text)

    monkeypatch.setattr(dubbing, "_render_preview", raiser)
    with pytest.raises(HTTPException) as ei:
        await dubbing.preview_dubbing("p1")
    assert ei.value.status_code == status
    assert text in str(ei.value.detail)


async def test_preview_unexpected_error_is_500_not_silence(monkeypatch):
    """PY-117：线程里的意外异常必须还是 500 可见，不许被 to_thread 吞成空响应。"""
    monkeypatch.setattr(dubbing.dubbing_service, "get_project", lambda pid: {
        "global_settings": {}, "assignments": [{"voice_id": "v1"}]})

    def boom(project_id, voice_id, settings):
        raise RuntimeError("CUDA out of memory")

    monkeypatch.setattr(dubbing, "_render_preview", boom)
    with pytest.raises(HTTPException) as ei:
        await dubbing.preview_dubbing("p1")
    assert ei.value.status_code == 500
    assert "CUDA out of memory" in str(ei.value.detail)


async def test_preview_seed_header_omitted_when_engine_has_no_seed(monkeypatch, tmp_path):
    """kokoro/edgetts 这类不支持 seed 的引擎：无值不发头（原行为）。"""
    wav = tmp_path / "p.wav"
    wav.write_bytes(b"RIFF")
    monkeypatch.setattr(dubbing.dubbing_service, "get_project", lambda pid: {
        "global_settings": {}, "assignments": [{"voice_id": "v1"}]})
    monkeypatch.setattr(dubbing, "_render_preview",
                        lambda *a, **k: (str(wav), None))
    out = await dubbing.preview_dubbing("p1")
    assert "x-last-successful-seed" not in out.headers


# ------------------------------------------------ `_render_preview` 翻译层本体

@pytest.fixture
def stub_env(monkeypatch, tmp_path):
    """把 `_render_preview` 摸到的四个外部依赖换成桩。

    `backend.services.voice_service` 单枚 import 15.5s（真爬虫在模块级就建对象），
    所以按本项目惯例整块换成 sys.modules 桩；引擎/质量门/音色行同理。
    """
    import numpy as np

    import modules.tts_engine.engine_registry as er
    import modules.voice_manager.audio_quality as aq
    import modules.voice_manager.voice_store as vs

    recorded = []
    fake_vs = types.SimpleNamespace(
        make_preview_seed=lambda engine: 7,
        record_last_successful_seed=lambda vid, seed: recorded.append((vid, seed)),
    )
    monkeypatch.setitem(sys.modules, "backend.services.voice_service", fake_vs)
    engine = types.SimpleNamespace(sample_rate=22050, supports_seed=True)
    holder = {"engine": engine, "gate": []}
    monkeypatch.setattr(er.engine_registry, "get_loaded",
                        lambda name, *a, **k: holder["engine"])
    monkeypatch.setattr(vs.voice_store, "get", lambda vid: {"id": vid})

    def fake_gate(eng, **kw):
        holder["gate"].append(kw)
        return types.SimpleNamespace(
            audio=np.zeros(2205, dtype=np.float32), seed=kw.get("seed"))

    monkeypatch.setattr(aq, "synthesize_with_gate", fake_gate)
    return holder, recorded


def test_render_preview_load_failure_is_503_not_500(monkeypatch, stub_env):
    """PY-118 的翻译层在搬进线程后仍要成立：装载失败 = 503 且带上原因。"""
    import modules.tts_engine.engine_registry as er

    def boom(name, *a, **k):
        raise er.EngineLoadError("voxcpm", FileNotFoundError("权重文件缺失"))

    holder, recorded = stub_env
    monkeypatch.setattr(er.engine_registry, "get_loaded", boom)
    with pytest.raises(HTTPException) as ei:
        dubbing._render_preview("p1", "v1", {})
    assert ei.value.status_code == 503
    assert "权重文件缺失" in str(ei.value.detail)
    assert recorded == []


def test_render_preview_absent_engine_is_503(monkeypatch, stub_env):
    import modules.tts_engine.engine_registry as er

    holder, recorded = stub_env
    holder["engine"] = None
    monkeypatch.setattr(er.engine_registry, "get_loaded", lambda name, *a, **k: None)
    with pytest.raises(HTTPException) as ei:
        dubbing._render_preview("p1", "v1", {})
    assert ei.value.status_code == 503
    assert recorded == []


def test_render_preview_write_failure_reports_the_real_error(monkeypatch, stub_env):
    """写盘失败要抛出**原始错误**并清掉半截 wav，不能被 Windows 的"文件占用"顶掉。

    形态不是构想的：U-8 变异跑 M14（摘掉空引擎门）时，真实失败面是
    `[WinError 32] 另一个程序正在使用此文件` —— `NamedTemporaryFile` 的句柄没关，
    兜底 `os.unlink` 自己先炸，把原始异常整个吞掉（磁盘满/写坏了都看不到真因）。
    """
    seen = {}

    def boom(path, data, samplerate, **kw):
        seen["path"] = path
        raise OSError(28, "No space left on device")

    monkeypatch.setattr("soundfile.write", boom)
    with pytest.raises(OSError) as ei:
        dubbing._render_preview("p1", "v1", {})
    assert ei.value.errno == 28, "真因被 unlink 的 WinError 32 顶掉了"
    assert seen["path"] and not Path(seen["path"]).exists(), "半截 wav 不许留在盘上"


def test_render_preview_quality_gate_failure_is_502_with_reason(monkeypatch, stub_env):
    """P0-4：两次都不过门 -> 502 且把 `report['reason']` 端出来，不许退化成 500。"""
    import modules.voice_manager.audio_quality as aq

    holder, recorded = stub_env

    def fail_gate(eng, **kw):
        raise aq.AudioQualityError("静音地板未过", "silent", {"reason": "rms -80 dBFS"}, 2)

    monkeypatch.setattr(aq, "synthesize_with_gate", fail_gate)
    with pytest.raises(HTTPException) as ei:
        dubbing._render_preview("p1", "v1", {})
    assert ei.value.status_code == 502
    assert "rms -80 dBFS" in str(ei.value.detail)
    assert recorded == [], "没过门的合成不得把 seed 记成'最近一次成功'"


def test_render_preview_writes_wav_and_seeds_the_profile(stub_env):
    """过门之后：seed 落到音色行（US-C4 的账），wav 真在盘上（PY-117 不许静默）。"""
    holder, recorded = stub_env
    path, seed = dubbing._render_preview("p1", "v1", {"speed": 1.2})
    try:
        assert Path(path).stat().st_size > 44, "只写了个 wav 头 = 空产物"
        assert seed == 7
        assert recorded == [("v1", 7)]
        assert holder["gate"][0]["speed"] == pytest.approx(1.2)
    finally:
        Path(path).unlink(missing_ok=True)


# ---------------------------------------------------------------- 源码门

def test_the_three_stall_sites_are_now_threaded():
    """结构门：三处调用点必须都经 `asyncio.to_thread`，且不再整份 read。

    行为测试能盖住当时的分支，盖不住"以后有人又加回一条同步调用"。
    比对前把空白折叠成单空格 —— 换行续行是常态，按字面匹配只会得到假红。
    """
    def nospace(src):
        # 比对前去掉所有空白：换行续行是常态，按字面匹配只会得到假红
        return "".join(src.split())

    def code_only(fn):
        """源码去掉 docstring：负向门若连"旧写法长什么样"的说明一起查，会假红。"""
        return inspect.getsource(fn).replace(fn.__doc__ or "", "")

    imp = code_only(books.import_book)
    assert "to_thread(book_service.import_book_from_path" in nospace(
        inspect.getsource(books.import_book)), imp
    assert "await file.read()" not in imp and "await file.seek" not in imp
    assert "to_thread(book_service.import_batch" in nospace(
        inspect.getsource(books.import_books_batch))
    prev = nospace(inspect.getsource(dubbing.preview_dubbing))
    assert "to_thread(_render_preview" in prev, prev
    # 重活必须在 _render_preview 里（线程边界之内），不许漂回 handler
    render = inspect.getsource(dubbing._render_preview)
    for call in ("get_loaded(", "synthesize_with_gate(", "sf.write(",
                 "record_last_successful_seed("):
        assert call in render, f"{call} 不在被搬进线程的那段里"
    handler = code_only(dubbing.preview_dubbing)
    for leaked in ("synthesize_with_gate(", "sf.write(", "get_loaded("):
        assert leaked not in handler, f"{leaked} 又漂回 handler 了"


async def test_the_ticker_measures_really_discriminate():
    """夹具自检（反向定罪）：同一个测量器喂"同步阻塞版 handler"必须判红。

    没有这条，`_assert_loop_alive` 可能是一把永远不响的尺子 —— 那前面四条
    "出循环"测试全是自证。做法是把改动前的调用形态原样复刻一遍（在循环里
    直接同步调 import），预期它被同一个预算判红。
    """
    def sync_import(*a, **k):
        time.sleep(BLOCK_S)
        return {"id": "b1"}

    async def pre_fix_handler():
        # 改动前的形态：没有 to_thread，handler 里一路同步跑完
        return {"status": "ok", "book": sync_import()}

    out, gaps = await _with_ticker(pre_fix_handler())
    assert out["book"]["id"] == "b1"
    assert max(gaps) >= BLOCK_S * 0.8, (
        f"同步阻塞版也没能撑开循环空隙（max={max(gaps) * 1000:.0f}ms）"
        " —— 测量器失效，前面所有'出循环'断言不可信")
    with pytest.raises(AssertionError):
        _assert_loop_alive(gaps, "改动前形态")
