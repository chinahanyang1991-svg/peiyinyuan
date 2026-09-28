# -*- coding: utf-8 -*-
"""U-11 前端健壮性包守卫：断线要能自愈、兜底要真能兜住、失败要看得见。

为什么还是读源码的字面量守卫：本仓没有 JS 测试运行器（且不得为此新增依赖），
而这几条缺陷全是"改回去就复发、且在单测里跑不出来"的形态问题 —— 真机侧的
浏览器实点结论记录在 qa/LEDGER.md（未知路由出兜底页、切页后零周期请求）。

四段结构：
  ① 404 兜底 + 路由级 ErrorBoundary（U-11 第 3 项）
  ② ConvertPage 的传输生命周期（U-11 第 2 项：卸载清定时器 + onerror/onclose 走重连）
  ③ 裸 axios / 裸 catch / 只有 state 没有入口的筛选（含 U-11b：model_status 轮询主人唯一）
  ④ by_engine 统计（引擎筛选选项的数据源，真库口径）
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "frontend" / "src"
APP = SRC / "App.tsx"
SHELL = SRC / "components" / "AppShell.tsx"
BOUNDARY = SRC / "components" / "ErrorBoundary.tsx"
NOT_FOUND = SRC / "routes" / "NotFoundPage.tsx"
CONVERT = SRC / "routes" / "ConvertPage.tsx"
DUBBING = SRC / "routes" / "DubbingPage.tsx"
STUDIO = SRC / "routes" / "VoiceStudioPage.tsx"
PREVIEW_HOOK = SRC / "components" / "ModelStatusBar.tsx"
WIZARD = SRC / "components" / "cast" / "CastWizard.tsx"
API = SRC / "services" / "api.ts"
STORE = SRC / "store" / "appStore.ts"
ZH = SRC / "i18n" / "zh.ts"
EN = SRC / "i18n" / "en.ts"
VOICE_STORE = ROOT / "modules" / "voice_manager" / "voice_store.py"

NEW_KEYS = [
    ("app", ["notFoundTitle", "notFoundDesc", "backHome"]),
    ("convert", ["pollGone"]),
    ("wizard", ["pollGone"]),
    ("voices", ["downloadTarget"]),
]


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# ============ ① 未知路由要有兜底页，崩掉的页面要能靠换路由自愈 ============
def test_catch_all_route_is_inside_the_layout():
    src = _read(APP)
    assert re.search(r'<Route path="\*" element=\{<NotFoundPage />\} />', src), (
        "path=\"*\" 兜底不见了：未知地址会渲染成整页空白"
    )
    assert 'lazy(() => import(\'./routes/NotFoundPage\'))' in src or \
        'lazy(() => import("./routes/NotFoundPage"))' in src, "兜底页又回到打包主 chunk"


def test_catch_all_is_not_before_the_real_routes():
    """兜底必须在具名路由之后 —— 放在前面会把 /voices 一路吃掉。"""
    lines = [ln.strip() for ln in _read(APP).splitlines()]
    catch_all = next(i for i, ln in enumerate(lines) if 'path="*"' in ln)
    named = [i for i, ln in enumerate(lines) if ln.startswith("<Route path=\"/") and ln != "<Route path=\"*\""]
    assert len(named) >= 8, f"具名路由只剩 {len(named)} 条，顺序核对失去意义"
    assert catch_all == max(named) + 1 or catch_all > max(named), "兜底路由排到了具名路由前面"


def test_not_found_page_is_i18n_and_offers_a_way_home():
    src = _read(NOT_FOUND)
    assert 't("app.notFoundTitle")' in src and 't("app.backHome")' in src
    assert "useI18n" in src, "兜底页硬编码文案 = 红线②（双语一致）的回退"
    assert re.search(r"<Page[\s>]", src), "兜底页没套 Page：宽度/间距脱离设计系统"


def test_route_level_boundary_is_keyed_by_pathname():
    src = _read(SHELL)
    assert re.search(r"<ErrorBoundary key=\{location\.pathname\} inline>", src), (
        "路由级 ErrorBoundary 不见了，或丢了 key —— 丢了 key 就只能刷新整站才能恢复"
    )
    assert re.search(r"<ErrorBoundary key=\{location\.pathname\} inline>\s*<Outlet />\s*</ErrorBoundary>", src), (
        "边界没有包住 <Outlet />：页面级异常仍会冒到 app 级"
    )


def test_inline_boundary_does_not_repaint_the_shell():
    """外壳里再套一层外壳会渲染出两套导航/页脚；inline 分支必须只出卡片。"""
    src = _read(BOUNDARY)
    assert "inline?: boolean" in src
    body = src[src.index("function ErrorScreen"):]
    assert body.index("if (inline)") < body.index('className="shell"'), (
        "inline 分支排到外壳之后：内联场景仍会多渲染一层 shell"
    )
    assert "inline={this.props.inline}" in body, "render() 没把 inline 传给 ErrorScreen"


# ============ ② ConvertPage：主动关闭 vs 意外断线，定时器一律收口 ============
def test_transport_teardown_clears_every_timer():
    src = _read(CONVERT)
    body = src[src.index("const stopTransport = useCallback"):]
    body = body[: body.index("}, []);")]
    for needle in ("pollTimerRef.current", "reconnectTimerRef.current", "wsRef.current"):
        assert needle in body, f"stopTransport 漏了 {needle}：切页后它还在自己跑"
    assert "closingByUsRef.current = true" in body, (
        "关闭前没立「我方主动关」的旗：onclose 会跟自己的重连抢方向"
    )


def test_effects_hand_teardown_to_stop_transport():
    src = _read(CONVERT)
    assert src.count("return stopTransport;") == 2, (
        "两处 useEffect 又各自手写清理：reconnectTimer 那一枚漏在清理之外"
    )
    for stale in ("if (wsRef.current) wsRef.current.close();",
                  "clearInterval(pollTimerRef.current);\n        pollTimerRef.current = null;"):
        assert stale not in src, f"清理逻辑又分叉了：{stale!r}"


def test_unexpected_close_falls_back_to_polling_and_reconnects_with_a_cap():
    src = _read(CONVERT)
    onclose = src[src.index("ws.onclose = () => {"): src.index("ws.onclose = () => {") + 900]
    assert "if (closingByUsRef.current || wsRef.current !== ws) return;" in onclose, (
        "主动关闭/被替换的连接也会触发恢复：重连会跟用户自己的切页较劲"
    )
    assert "startPolling(id)" in onclose, "断线后不再兜轮询 = 进度条卡死"
    assert "reconnectTriesRef.current < RECONNECT_LIMIT" in onclose, "重连失去上限"
    assert "1000 * 2 ** reconnectTriesRef.current" in onclose, "重连不再是指数退避"
    assert re.search(r"^const RECONNECT_LIMIT = (\d+);", src, re.M), "重连上限常量不见了"


def test_polling_surfaces_consecutive_failures_instead_of_spinning():
    src = _read(CONVERT)
    body = src[src.index("const startPolling = useCallback"):]
    body = body[: body.index("[stopTransport, t]")]
    assert "pollFailRef.current += 1" in body
    assert "pollFailRef.current = 0" in body, "成功之后不清零：偶发一次失败也会攒到收口"
    assert "pollFailRef.current >= POLL_FAIL_LIMIT" in body
    assert 'setWsError(t("convert.pollGone"))' in body, "轮询失败到顶却什么都不说"
    assert "stopTransport()" in body.split("POLL_FAIL_LIMIT", 1)[1], "到顶后没有停下定时器"


def test_websocket_error_is_attributed_not_duplicated():
    """onerror 只留痕；恢复动作统一在 onclose（浏览器一定会再给一次 onclose）。"""
    src = _read(CONVERT)
    onerror = src[src.index("ws.onerror = (err) => {"):]
    onerror = onerror[: onerror.index("};")]
    assert "console.error" in onerror, "onerror 完全无痕"
    assert "startPolling" not in onerror, "onerror 也去兜底 = 与 onclose 双份恢复，互相抢方向"


def test_error_message_is_visible_in_the_ui():
    src = _read(CONVERT)
    assert re.search(r"wsError && taskId \? <Alert tone=\"warning\" title=\{wsError\}", src), (
        "wsError 只进 state 不进界面：用户看到的仍是一根安静的进度条"
    )


# ============ ③ 裸 axios / 裸 catch / 只有 state 没有入口的筛选 ============
def test_dubbing_page_uses_the_configured_client():
    src = _read(DUBBING)
    assert "axios" not in src, (
        "DubbingPage 又直接 import axios：绕过 baseURL/timeout 的那条请求会永不超时"
    )
    for call in ("dubbingApi.addRole(", "dubbingApi.deleteRole(",
                 "dubbingApi.assignVoice(", "dubbingApi.updateSettings("):
        assert call in src, f"{call} 不见了：角色管理四条写路径失去超时保护"


def test_settings_wrapper_is_not_double_wrapped():
    """dubbingApi.updateSettings 自己会包一层 {settings}；再包一次后端就收到嵌套壳。"""
    api_src = _read(API)
    assert "api.put(`/dubbing/projects/${id}/settings`, { settings })" in api_src
    page = _read(DUBBING)
    line = next(ln for ln in page.splitlines() if "dubbingApi.updateSettings(" in ln)
    assert "settings:" not in line, f"请求体被包了两层：{line.strip()}"


COMMENT_ONLY_CATCH = re.compile(r"catch(?: \(\w+\))? \{\s*(?:/\*[^*]*\*/\s*)*\}", re.S)
EMPTY_CATCH = re.compile(r"catch(?: \(\w+\))? \{\s*\}", re.S)


def test_no_comment_only_or_empty_catch_blocks():
    """每个 catch 至少要 toast/log 一次 —— "看着像在跑"是最贵的一类缺陷。"""
    offenders = []
    for p in sorted(SRC.rglob("*")):
        if p.suffix not in (".ts", ".tsx"):
            continue
        src = _read(p)
        for pat in (COMMENT_ONLY_CATCH, EMPTY_CATCH):
            for m in pat.finditer(src):
                offenders.append(f"{p.relative_to(SRC)}:{src[:m.start()].count(chr(10)) + 1}")
    assert not offenders, "出现只写注释/完全空白的 catch: " + ", ".join(offenders)


def test_wizard_polling_reports_a_stalled_backend():
    src = _read(WIZARD)
    body = src[src.index("let fails = 0;"):]
    body = body[: body.index("}, 1500);")]
    assert "fails = 0" in body.split("catch", 1)[0], "成功不清零，偶发失败会攒成假故障"
    assert "fails >= POLL_FAIL_LIMIT" in body
    assert 'setError(t("wizard.pollGone"))' in body, "轮询到顶后仍然静默"
    assert "window.clearInterval(timer)" in body.split("POLL_FAIL_LIMIT")[1], "到顶没停表"
    assert re.search(r"^const POLL_FAIL_LIMIT = (\d+);", src, re.M)


def test_model_status_probe_leaves_a_trace():
    src = _read(API)
    body = src[src.index("modelStatus: async"):]
    body = body[: body.index("},\n};")]
    assert "return null" in body, "探测失败仍要退化成空态，不能拖垮页面"
    assert "console.warn" in body, "状态条一直是空却零线索：至少要留一行控制台痕迹"


def test_preview_network_failure_is_readable():
    """断网试听必须有可见错误，而且不能是一句英文 "Failed to fetch"。"""
    src = _read(PREVIEW_HOOK)
    body = src[src.index("export function useVoicePreview"):]
    assert re.search(r'err instanceof TypeError \? t\("common\.networkError"\)', body), (
        "fetch 层的网络失败又变成裸英文 message"
    )
    assert "setError(" in body, "试听失败不再写 error state = 回到静默"
    page = _read(STUDIO)
    assert "preview.error" in page, "错误态没有出口：界面上读不到它"


def test_engine_filter_has_a_real_input_and_data_source():
    src = _read(STUDIO)
    assert re.search(r"setFilter\(\{ engine: e\.target\.value \}\)", src), (
        'filters.engine 又回到「只有 state、没有写入口」的死键'
    )
    assert "voiceApi" in src and ".statistics()" in src, "引擎选项不是从数据来的"
    assert "params.engine = filters.engine" in src
    assert "const engineOptions = useMemo" in src
    assert 'placeholder={t("voices.filterEngine")}' in src
    # 空结果必须归因到"筛选不匹配"，而不是"库里没东西"
    assert "filters.source || filters.engine ? (" in src, "空态判断漏了引擎这一维"


def test_download_targets_the_local_tier_engine_not_the_active_one():
    """指令#1 改了这条的**正确形**：下载回来的权重/参考音频只有**本地档引擎**会读，
    所以登记目标必须按档位取本地档，而不是"当前主引擎"（那枚自 09-24 起很可能是 edgetts）。

    U-11 原判据（跟着 `ttsEngine`）在新档位下恰好是要防的错：`POST /voices/online/download`
    接受任何已注册引擎名，跟主引擎走会把本地音色登记到在线引擎名下、永远取用不到。
    这里同时钉住三条：不许写死引擎名、取不到本地档时必须有拒绝分支、目标引擎要告诉用户。
    """
    src = _read(STUDIO)
    assert 'modeOfTier(ttsModes, "local")?.engine ?? ""' in src, "登记目标没按档位取本地档"
    assert "voiceApi.download(modelName, localEngine)" in src
    assert 'voiceApi.download(modelName, "voxcpm")' not in src, "引擎名又写回去了"
    assert "voiceApi.download(modelName, ttsEngine)" not in src, (
        "跟回主引擎：在线档下会把本地权重登记到用不到它的引擎名下")
    assert re.search(r"if \(!localEngine\) \{[^}]*downloadNoLocalEngine[^}]*\breturn;", src, re.S), (
        "取不到本地档引擎时必须有**真的**拒绝分支（只留一句文案、门被写成恒假也算过 = 假绿）")
    assert 't("voices.downloadTarget", { engine: localEngine })' in src, "目标引擎没告诉用户"
    store = _read(STORE)
    assert "export function modeOfTier" in store, "档位→引擎的取数口不见了（各页会各自猜引擎名）"
    assert "ttsModes" in store


def test_model_status_polling_has_a_single_owner():
    """U-11b 的结论钉在结构上：这条 4s 节拍**只能有一个主人**。

    2026-09-23 实测（`qa/LEDGER.md` §24）：单页连续 14 个间隔 = 3993–4016ms，恰为
    `POLL_MS`；页内既没有重复定时器、也没有第二个调用者 —— 上轮那句"约 13 枚/16s"
    是探针把跨多次整页重载的累计计数当成了 16s 窗口。这条守卫防的是它反过来发生：
    将来谁再挂一条 model_status 轮询、或把节拍调到秒级，都会当场变红。
    """
    hook = _read(PREVIEW_HOOK)
    texts = {p.relative_to(SRC).as_posix(): _read(p) for p in sorted(SRC.rglob("*.ts*"))}
    owners = sorted(f for f, txt in texts.items() if re.search(r"\bmodelStatus\s*\(", txt))
    assert owners == ["components/ModelStatusBar.tsx"], (
        f"model_status 的调用者不再是单独一枚：{owners}（请求速率按调用者数量成倍涨）"
    )
    assert len(re.findall(r"\bmodelStatus\s*\(", hook)) == 1
    for name, txt in texts.items():
        if name != "components/ModelStatusBar.tsx":
            assert "useModelSlot(" not in txt, f"{name} 又自立了一条模型状态轮询（主人必须唯一）"
    match = re.search(r"^const POLL_MS = (\d+);$", hook, re.M)
    assert match and int(match.group(1)) >= 3000, "model_status 节拍失去下限"
    assert hook.count("window.setInterval(() => void refresh(), POLL_MS)") == 1, "定时器不再是 POLL_MS 那一枚"
    assert _read(API).count("api.get('/system/model_status')") == 1, "端点出口必须只有一枚"


# ============ ④ by_engine：筛选选项的数据源（真库口径） ============
@pytest.fixture()
def voice_db(tmp_path, monkeypatch):
    """临时 sqlite + 临时数据目录；不加载引擎、不打网络、不碰生产库。"""
    import backend.models.base as base
    from backend.config import Config

    data_dir = tmp_path / "data"
    (data_dir / "books").mkdir(parents=True)
    monkeypatch.setattr(base, "get_db_path", lambda: str(tmp_path / "u11.db"))
    monkeypatch.setattr(base, "_engine", None, raising=False)
    monkeypatch.setattr(base, "_SessionLocal", None, raising=False)
    monkeypatch.setattr(Config, "DATA_DIR", data_dir, raising=False)
    monkeypatch.setattr(Config, "VOICE_DIR", data_dir / "voices", raising=False)
    monkeypatch.setattr(Config, "OUTPUT_DIR", data_dir / "output", raising=False)

    base.get_session().close()  # 建表 + 内置/Edge 音色种子
    yield base
    monkeypatch.setattr(base, "_engine", None, raising=False)
    monkeypatch.setattr(base, "_SessionLocal", None, raising=False)


def test_statistics_expose_engine_breakdown(voice_db):
    from backend.models.voice import VoiceProfile
    from modules.voice_manager.voice_store import voice_store

    session = voice_db.get_session()
    try:
        for i in range(3):
            session.add(VoiceProfile(name=f"u11-kokoro-{i}", engine="kokoro", source="sample"))
        for i in range(2):
            session.add(VoiceProfile(name=f"u11-edge-{i}", engine="edgetts", source="builtin"))
        session.commit()
    finally:
        session.close()

    stats = voice_store.get_statistics()
    for key in ("total", "by_source", "by_engine"):
        assert key in stats, f"statistics 少了 {key}：前端引擎筛选会失去选项源"
    assert stats["by_engine"]["kokoro"] >= 3
    assert stats["by_engine"]["edgetts"] >= 2
    assert sum(stats["by_engine"].values()) == stats["total"], (
        "by_engine 与 total 对不上：说明有行的 engine 为 NULL 或分组口径漂了"
    )
    assert sum(stats["by_source"].values()) == stats["total"]
    # 前端按下标取键名当筛选值，故键必须都是非空字符串
    assert all(isinstance(k, str) and k for k in stats["by_engine"]), "by_engine 出现空键"


def test_statistics_endpoint_shape_and_parity(voice_db):
    """/api/voices/statistics 必须把 by_engine 原样带到前端（多一层丢键就白干）。"""
    from fastapi.testclient import TestClient

    from backend.main import app
    from modules.voice_manager.voice_store import voice_store

    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/api/voices/statistics")
    assert resp.status_code == 200
    payload = resp.json()["statistics"]
    assert "by_engine" in payload, sorted(payload)
    assert payload == voice_store.get_statistics(), "端点与 store 的口径不再同源"


def test_i18n_new_keys_are_paired():
    zh, en = _read(ZH), _read(EN)
    for section, keys in NEW_KEYS:
        for key in keys:
            for name, src in (("zh", zh), ("en", en)):
                assert re.search(rf"^    {key}: ", src, re.M), f"{name} 缺 {section}.{key}"


def test_guard_targets_exist():
    """方向核对：守卫读的文件必须都还在原处（改名/搬目录会让守卫静默恒真）。"""
    for p in (APP, SHELL, BOUNDARY, NOT_FOUND, CONVERT, DUBBING, STUDIO,
              PREVIEW_HOOK, WIZARD, API, STORE, ZH, EN, VOICE_STORE):
        assert p.exists() and p.stat().st_size > 0, p
