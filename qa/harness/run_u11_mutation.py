"""U-11 变异检验：前端健壮性这一批守卫，逐枚注释掉必须变红。

与 `run_u8_mutation.py` / `run_u7_mutation.py` 同一套规矩（那三条是被 U-7 首轮
10 枚 `NOT_APPLIED` 换来的，这里原样继承）：
  * 替换串必须**逐字命中一次**，命不中报 `NOT_APPLIED`，不算变异通过；
  * 模式里的 `\\n` 按**目标文件实际换行符**展开（`core.autocrlf=true` 的机器上
    worktree 检出 CRLF，写死 `\\n` 会 0 命中）；
  * 读写一律二进制；跑完核对基线 md5，任何文件字节不同 = 还原失败，非零退出。

U-11 的特殊处：被钉的是**源码形态**（.tsx/.ts 字面量）而不是一段能跑的代码，
所以变异不需要能编译 —— 但要能定罪，因此每枚都对着 `tests/test_u11_frontend_
robustness.py` 里那条具体断言去写（编号注释里写清了是哪条）。④ 段（`by_engine`
统计）是真跑的 Python，变异会走 DB/TestClient 两条测试。

跑法（干净 worktree 根，勿在产品树/未提交改动上跑）：
    PYTHONUTF8=1 python -X utf8 qa/harness/run_u11_mutation.py --check   # 只核对命中数，不写盘
    PYTHONUTF8=1 python -X utf8 qa/harness/run_u11_mutation.py
    PYTHONUTF8=1 python -X utf8 qa/harness/run_u11_mutation.py --only M1,M9
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
APP = "frontend/src/App.tsx"
SHELL = "frontend/src/components/AppShell.tsx"
BOUND = "frontend/src/components/ErrorBoundary.tsx"
CONV = "frontend/src/routes/ConvertPage.tsx"
DUB = "frontend/src/routes/DubbingPage.tsx"
STUDIO = "frontend/src/routes/VoiceStudioPage.tsx"
PREVIEW = "frontend/src/components/ModelStatusBar.tsx"
WIZARD = "frontend/src/components/cast/CastWizard.tsx"
API = "frontend/src/services/api.ts"
VSTORE = "modules/voice_manager/voice_store.py"
TESTS = ["tests/test_u11_frontend_robustness.py", "tests/test_u3_voice_grid_windowing.py"]

# (编号, 说明, 文件, 原文, 变异后)
MUTATIONS = [
    # ---- ① 兜底路由 / 路由级 ErrorBoundary ----
    ("M1", "摘掉 path=\"*\" 兜底：未知地址回到整页空白（test_catch_all_route_is_inside_the_layout）",
     APP,
     '            {/* 兜底必须在布局路由之内：未知地址也要有壳、有导航，不能是整页空白 */}\n'
     '            <Route path="*" element={<NotFoundPage />} />\n',
     ''),
    ("M2", "兜底页 lazy 指向别的页面（同上测试的第二条断言）", APP,
     "const NotFoundPage = lazy(() => import('./routes/NotFoundPage'));",
     "const NotFoundPage = lazy(() => import('./routes/HomePage'));"),
    ("M3", "路由级边界丢掉 key：崩了只能刷新整站（test_route_level_boundary_is_keyed_by_pathname）",
     SHELL,
     '          <ErrorBoundary key={location.pathname} inline>\n            <Outlet />',
     '          <ErrorBoundary inline>\n            <Outlet />'),
    ("M4", "路由级边界丢掉 inline：外壳里再套一层外壳（同上测试）", SHELL,
     '          <ErrorBoundary key={location.pathname} inline>\n            <Outlet />',
     '          <ErrorBoundary key={location.pathname}>\n            <Outlet />'),
    ("M5", "render() 不把 inline 传给 ErrorScreen（test_inline_boundary_does_not_repaint_the_shell）",
     BOUND,
     "inline={this.props.inline} />",
     "inline={false} />"),
    # ---- ② ConvertPage 传输生命周期 ----
    ("M6", "stopTransport 不清重连定时器：切页后它自己还会再连（test_transport_teardown_clears_every_timer）",
     CONV,
     "    if (reconnectTimerRef.current) {\n"
     "      clearTimeout(reconnectTimerRef.current);\n"
     "      reconnectTimerRef.current = null;\n"
     "    }\n",
     "    // mutated: 留着定时器不查\n"),
    ("M7", "关闭前不立「我方主动关」旗（同 M6 测试的 closingByUs 断言）", CONV,
     "  const stopTransport = useCallback(() => {\n    closingByUsRef.current = true;\n",
     "  const stopTransport = useCallback(() => {\n"),
    ("M8", "第二处 useEffect 回到手写清理（test_effects_hand_teardown_to_stop_transport）", CONV,
     "    return stopTransport;\n  }, [taskId, loadCurrentTask, connectWebSocket, stopTransport]);",
     "    return () => {\n      if (wsRef.current) wsRef.current.close();\n    };\n"
     "  }, [taskId, loadCurrentTask, connectWebSocket, stopTransport]);"),
    ("M9", "意外断线不再兜轮询：进度条卡死（test_unexpected_close_falls_back_to_polling_and_reconnects_with_a_cap）",
     CONV,
     '          setWsError(t("convert.wsFallback"));\n          startPolling(id);\n',
     '          setWsError(t("convert.wsFallback"));\n'),
    ("M10", "重连失去上限（同上测试）", CONV,
     "          if (reconnectTriesRef.current < RECONNECT_LIMIT) {",
     "          if (true) {"),
    ("M11", "重连退回固定间隔（同上测试的指数退避断言）", CONV,
     "            const delay = 1000 * 2 ** reconnectTriesRef.current;",
     "            const delay = 1000;"),
    ("M12", "onerror 也去兜底：与 onclose 双份恢复（test_websocket_error_is_attributed_not_duplicated）",
     CONV,
     '          console.error("WebSocket错误:", err);\n',
     '          console.error("WebSocket错误:", err);\n          startPolling(id);\n'),
    ("M13", "轮询到顶后停下但不说（test_polling_surfaces_consecutive_failures_instead_of_spinning）",
     CONV,
     '            setWsError(t("convert.pollGone"));\n            stopTransport();\n',
     '            // mutated: 到此为止不再声张\n'),
    ("M14", "wsError 只进 state 不进界面（test_error_message_is_visible_in_the_ui）", CONV,
     '        {wsError && taskId ? <Alert tone="warning" title={wsError} icon={TriangleAlert} /> : null}',
     '        {false && taskId ? <Alert tone="warning" title={wsError} icon={TriangleAlert} /> : null}'),
    ("M15", "退回只写注释的 catch（test_no_comment_only_or_empty_catch_blocks）", CONV,
     '    } catch (err) {\n      console.error("加载任务列表失败:", err);\n    }\n',
     '    } catch (err) {\n      /* 拉不到列表不影响主流程 */\n    }\n'),
    # ---- ③ 裸 axios / 写入口 / 可见错误 ----
    ("M16", "加角色回到裸 axios（test_dubbing_page_uses_the_configured_client）", DUB,
     "      const res = await dubbingApi.addRole(selectedProjectId, {",
     "      const res = await axios.post(`/api/dubbing/projects/${selectedProjectId}/roles`, {"),
    ("M17", "settings 请求体被包两层（test_settings_wrapper_is_not_double_wrapped）", DUB,
     "      await dubbingApi.updateSettings(selectedProjectId, globalSettings);",
     "      await dubbingApi.updateSettings(selectedProjectId, { settings: globalSettings });"),
    ("M18", "引擎筛选失去写入口：filters.engine 回到死键（test_engine_filter_has_a_real_input_and_data_source）",
     STUDIO,
     "            onChange={(e) => setFilter({ engine: e.target.value })}",
     "            onChange={(e) => void e.target.value}"),
    ("M19", "空态判断漏掉引擎这一维（同上测试）", STUDIO,
     "filters.source || filters.engine ? (",
     "filters.source ? ("),
    ("M20", "下载引擎又写死 voxcpm（test_download_targets_the_active_engine_not_a_hardcoded_name）",
     STUDIO,
     '      await voiceApi.download(modelName, ttsEngine);',
     '      await voiceApi.download(modelName, "voxcpm");'),
    ("M21", "断网试听回到裸英文 message（test_preview_network_failure_is_readable）", PREVIEW,
     '        setError(err instanceof TypeError ? t("common.networkError") : err instanceof Error ? err.message : String(err));',
     "        setError(err instanceof Error ? err.message : String(err));"),
    ("M22", "模型状态探测失败重新零线索（test_model_status_probe_leaves_a_trace）", API,
     '      console.warn(\'读取模型状态失败，状态条按"未知"渲染:\', err);',
     "      // mutated: 静默退化"),
    ("M23", "向导轮询到顶后仍然静默（test_wizard_polling_reports_a_stalled_backend）", WIZARD,
     '          setError(t("wizard.pollGone"));',
     "          // mutated: 什么都不说"),
    # ---- ④ by_engine 统计（真跑的 Python） ----
    ("M24", "statistics 不再给 by_engine：引擎筛选失去数据源（test_statistics_expose_engine_breakdown + 端点同口径）",
     VSTORE,
     '            return {"total": total, "by_source": by_source, "by_engine": by_engine}',
     '            return {"total": total, "by_source": by_source}'),
    # ---- ⑤ U-11b：model_status 轮询的主人必须唯一、节拍有下限 ----
    ("M25", "model_status 节拍掉到秒级（test_model_status_polling_has_a_single_owner 的 POLL_MS 下限断言）",
     PREVIEW,
     "const POLL_MS = 4000;",
     "const POLL_MS = 500;"),
    ("M26", "第二枚组件自立模型状态轮询：请求速率按调用者翻倍（同上测试的 owners 断言）", SHELL,
     "  const probeBackend = useCallback(() => {\n    systemApi\n",
     "  const probeBackend = useCallback(() => {\n    void systemApi.modelStatus();\n    systemApi\n"),
    ("M27", "定时器不再用 POLL_MS 那一枚（同上测试的 setInterval 计数断言）", PREVIEW,
     "    const timer = window.setInterval(() => void refresh(), POLL_MS);",
     "    const timer = window.setInterval(() => void refresh(), 4000);"),
]

WATCH = [APP, SHELL, BOUND, CONV, DUB, STUDIO, PREVIEW, WIZARD, API, VSTORE]


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


def check_patterns() -> int:
    """只核对命中数：不写盘、不跑测试，用于先排掉 `NOT_APPLIED`。"""
    bad = 0
    for mid, desc, rel, old, _new in MUTATIONS:
        raw = (ROOT / rel).read_bytes()
        hits = raw.count(_expand(old, _eol(raw)))
        if hits != 1:
            bad += 1
            print(f"{mid:>4} 命中 {hits} 次  {rel}  {desc}")
    print(f"模式核对：{len(MUTATIONS)} 枚，命中数不为 1 的 {bad} 枚")
    return 1 if bad else 0


def main(argv: list[str]) -> int:
    if "--check" in argv:
        return check_patterns()
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

    print("\n=== U-11 变异检验 ===")
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
