r"""启动器必须挑一台**真能配音**的解释器（PY-354，2026-09-24 18:32 试听事故）。

现象：用户双击 `start-all.cmd` 报
`TTS 引擎 'edgetts' 模型加载失败: RuntimeError: edge-tts 库未安装 … No module named 'edge_tts'`。

两层根因（判据层在 `tests/test_engine_availability.py::TestShippedTiersDeclareDeps`，
本文件管启动器这一层）：
  1. `start-all.cmd` 用的是 PATH 上的裸 `python` —— 这台机器上它解析到
     `…\Python314\python.exe`，而 `edge_tts` / `torch` 只装在 `C:\Program Files\Python311`；
  2. `start_all.py` 的后端腿再把 `sys.executable` 原样传下去，于是**在缺依赖的解释器上
     起了一台永远说不出话的后端**，健康门却因为判据缺位照样放行。

修法口径：启动器逐台探测候选解释器缺哪些后端依赖，用第一台齐全的；一台都不齐就
**拒绝起后端**并把每台缺什么都打出来 —— 而不是起一台"看着活着、点试听必失败"的。
`gsv` 腿早就这么办了（钉死 `.venv` 解释器，注释写着"系统 python 缺 ffmpeg 模块"），
本文件把同一口径补到后端腿。
"""

import argparse
import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import start_all  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_pick(monkeypatch):
    """解释器选定值按测试逐枚重算：模块级缓存会把上一枚的结论漏给下一枚。"""
    monkeypatch.setattr(start_all, "_BACKEND_PICK", {})
    monkeypatch.setattr(start_all, "_LAUNCHER_PICK", {})


def _fake_probe(missing_by_exe: dict):
    """把探测桩成字典里写明的缺失清单；字典没点名的解释器＝齐全（少写噪音）。

    值写 `None` 表示"这台压根探不出来"（起不来），用来喂不合格分支。
    """
    seen = []

    def probe(exe, timeout=None):
        seen.append(exe)
        return missing_by_exe.get(exe, [])

    return probe, seen


def _ns(**kw):
    base = {"only": "backend,frontend,gsv", "restart": "", "restart_all": False,
            "reload": False, "no_health": True, "no_pause": True,
            "boot_timeout": 1, "model_timeout": 1, "front_timeout": 1,
            "gsv_timeout": 1}
    base.update(kw)
    return argparse.Namespace(**base)


class TestProbe:
    def test_probe_source_is_import_availability_only(self):
        """探测子进程只准问 `find_spec`：真 `import torch` 会把启动白拖数十秒，
        而且在缺包的机器上报错形式五花八门，不如一句缺失清单。"""
        code = start_all.PROBE_SRC
        assert "find_spec" in code
        for heavy in ("import torch", "import fastapi", "importlib.import_module",
                      "exec("):
            assert heavy not in code, f"探测源码里出现了真 import：{heavy}"
        for dep in start_all.BACKEND_DEPS:
            assert dep in code, f"依赖清单里的 {dep} 没进探测源码"

    def test_probe_argv_targets_the_candidate_not_the_launcher(self):
        """探测必须打到**那台候选**解释器上，不能拿启动器自己的口径下结论。"""
        argv = start_all.probe_argv(r"C:\somewhere\python.exe")
        assert argv[0] == r"C:\somewhere\python.exe"
        assert argv[1:3] == ["-X", "utf8"], "子进程输出按 utf-8 取，坑见 PY-344"
        assert argv[-2:] == ["-c", start_all.PROBE_SRC]

    def test_probe_real_machine_self_consistency(self):
        """真跑一次子进程：报出的缺失清单与本地 find_spec 复算逐字相同。"""
        expect = [m for m in start_all.BACKEND_DEPS
                  if importlib.util.find_spec(m) is None]
        assert start_all.probe_python(sys.executable) == expect

    def test_probe_of_a_nonexistent_interpreter_is_not_a_pass(self):
        """起不来（非零退出/无输出）判 None＝不合格，绝不能判成"没缺包"。"""
        assert start_all.probe_python(str(Path(sys.executable).parent / "没有这台.exe")) is None


class TestCandidates:
    PY_0P_SAMPLE = (
        " -V:3.14 *        C:\\Users\\someone\\AppData\\Local\\Programs\\Python"
        "\\Python314\\python.exe\n"
        " -V:3.12          C:\\Users\\someone\\AppData\\Local\\Programs\\Python"
        "\\Python312\\python.exe\n"
        " -V:3.11          C:\\Program Files\\Python311\\python.exe\n"
        " -V:ContinuumAnaconda3  D:\\Anaconda3\\python.exe\n"
    )

    def test_parse_keeps_paths_with_spaces_intact(self):
        """`py -0p` 每行是「标志 路径」，而路径**可以带空格**（`C:\\Program Files\\...`）。

        按空白切词取尾项会把这台机器上唯一齐全的解释器切成
        `Files\\Python311\\python.exe` —— 探测必然失败，好候选就被自己的解析器否掉
        （19:14 真机跑 3.14 时实测到的红，单元桩盖不到，因为它绕过了这个解析）。
        """
        out = start_all.parse_py_launcher_output(self.PY_0P_SAMPLE)
        assert out == [
            r"C:\Users\someone\AppData\Local\Programs\Python\Python314\python.exe",
            r"C:\Users\someone\AppData\Local\Programs\Python\Python312\python.exe",
            r"C:\Program Files\Python311\python.exe",
            r"D:\Anaconda3\python.exe"]
        assert all(p.lower().endswith("python.exe") for p in out), out

    def test_parse_ignores_garbage_lines(self):
        assert start_all.parse_py_launcher_output(
            " -V:3.11 *        \n\nno exe here\npy: No runtime installed\n") == []

    def test_real_candidates_are_absolute_paths(self):
        """真机锁（单元桩盖不到的那类）：候选清单里不许出现被切碎的路径。

        `C:\\Program Files\\...` 这类带空格路径一旦被按空白切词，剩下的
        `Files\\Python311\\python.exe` 不是绝对路径 —— 探测必失败，好候选被自己否掉。
        """
        cands = start_all.candidate_pys()
        assert cands, "候选清单为空"
        for p in cands:
            assert Path(p).is_absolute(), f"候选不是绝对路径（路径被切碎）: {p!r}"

    def test_order_is_env_then_self_then_local_installs(self, monkeypatch):
        monkeypatch.setenv("PYY_PYTHON", r"C:\custom\python.exe")
        monkeypatch.setattr(start_all, "py_launcher_installs", lambda: [
            r"C:\Program Files\Python311\python.exe",
            r"C:\py314\python.exe", sys.executable])
        cands = start_all.candidate_pys()
        assert cands[0] == r"C:\custom\python.exe", "人工指定的解释器没排第一"
        assert cands[1] == sys.executable, "当前解释器没排第二（它是最省事的候选）"
        assert r"C:\Program Files\Python311\python.exe" in cands
        assert len(cands) == len({c.lower() for c in cands}), "候选没去重＝同一路径探两遍"

    def test_env_override_equal_to_self_does_not_duplicate(self, monkeypatch):
        monkeypatch.setenv("PYY_PYTHON", sys.executable)
        monkeypatch.setattr(start_all, "py_launcher_installs", lambda: [])
        assert start_all.candidate_pys() == [sys.executable]


class TestPick:
    def test_first_clean_interpreter_wins_and_probe_order_is_candidate_order(self):
        probe, seen = _fake_probe({r"C:\bad\python.exe": ["edge_tts", "torch"]})
        out = start_all.pick_backend_python(
            candidates=[r"C:\bad\python.exe", r"C:\ok\python.exe"], probe=probe)
        assert out["ok"] is True and out["exe"] == r"C:\ok\python.exe"
        assert seen == [r"C:\bad\python.exe", r"C:\ok\python.exe"]

    def test_stops_probing_after_a_hit(self):
        """探到齐全即停：每台都要起一次子进程，继续探就是白等。"""
        probe, seen = _fake_probe({r"C:\a\python.exe": [], r"C:\b\python.exe": []})
        start_all.pick_backend_python(
            candidates=[r"C:\a\python.exe", r"C:\b\python.exe"], probe=probe)
        assert seen == [r"C:\a\python.exe"]

    def test_all_missing_refuses_and_keeps_evidence_per_candidate(self):
        probe, _ = _fake_probe({r"C:\a\python.exe": ["edge_tts"],
                                r"C:\b\python.exe": ["torch", "fastapi"]})
        out = start_all.pick_backend_python(
            candidates=[r"C:\a\python.exe", r"C:\b\python.exe"], probe=probe)
        assert out["ok"] is False
        assert out["tried"] == [(r"C:\a\python.exe", ["edge_tts"]),
                                (r"C:\b\python.exe", ["torch", "fastapi"])]

    def test_unprobeable_candidate_counts_as_unusable(self):
        probe, _ = _fake_probe({r"C:\dead\python.exe": None})
        out = start_all.pick_backend_python(candidates=[r"C:\dead\python.exe"], probe=probe)
        assert out["ok"] is False

    def test_real_machine_can_pick_a_usable_interpreter(self):
        """事故锚（不 skip）：本机必须选得出齐全的一台，且选中的那台真的齐全。

        这条若变红，说明启动器会拒绝起后端 —— 那是真故障，不是环境噪声。
        """
        out = start_all.pick_backend_python()
        assert out["ok"] is True, (
            f"本机没有任何一台解释器装齐后端依赖: {out['tried']}")
        assert start_all.probe_python(out["exe"]) == []


class TestLegUsesThePick:
    def test_backend_leg_argv0_is_the_picked_interpreter(self, monkeypatch):
        """后端腿不得再用裸 `python`，也不得无条件沿用 `sys.executable`。"""
        monkeypatch.setattr(start_all, "pick_backend_python",
                            lambda candidates=None, probe=None: {
                                "exe": r"C:\picked\python.exe", "ok": True, "tried": []})
        lg = start_all.leg_by_key("backend")
        assert lg.argv[0] == r"C:\picked\python.exe"
        assert lg.argv[1:4] == ["-X", "utf8", "-m"], "uvicorn 启动参数被顺手动过"

    def test_pick_is_computed_once_per_process(self, monkeypatch):
        calls = []

        def _pick(candidates=None, probe=None):
            calls.append(1)
            return {"exe": sys.executable, "ok": True, "tried": []}

        monkeypatch.setattr(start_all, "pick_backend_python", _pick)
        for _ in range(4):
            start_all.backend_python()
        assert len(calls) == 1, "每问一次腿都重探一遍解释器 = 启动白等数秒"


class TestOrchestrateRefuses:
    def _harness(self, monkeypatch, tmp_path, pick):
        seen = {"spawned": [], "lines": []}
        monkeypatch.setattr(start_all, "say", lambda msg="": seen["lines"].append(str(msg)))
        monkeypatch.setattr(start_all, "hdr", lambda msg="": seen["lines"].append(str(msg)))
        monkeypatch.setattr(start_all, "LOG_DIR", tmp_path)
        monkeypatch.setattr(start_all, "free_gb", lambda: 99.0)
        monkeypatch.setattr(start_all, "decide", lambda lg, restart: "launch")
        monkeypatch.setattr(start_all, "spawn_window",
                            lambda lg, **kw: seen["spawned"].append(lg.key) or True)
        monkeypatch.setattr(start_all, "wait_for_launch_banner", lambda *a, **k: True)
        monkeypatch.setattr(start_all, "health_gate", lambda *a, **k: True)
        monkeypatch.setattr(start_all, "pick_backend_python",
                            lambda candidates=None, probe=None: pick)
        monkeypatch.setattr(start_all.time, "sleep", lambda s: None)
        return seen

    def test_refuses_backend_but_still_starts_other_legs(self, monkeypatch, tmp_path):
        seen = self._harness(monkeypatch, tmp_path, {
            "exe": r"C:\bad\python.exe", "ok": False,
            "tried": [(r"C:\bad\python.exe", ["edge_tts", "torch"]),
                      (r"C:\other\python.exe", ["fastapi"])]})
        ok = start_all.orchestrate(_ns(only="backend,frontend"))
        assert "backend" not in seen["spawned"], "缺依赖的解释器照样起 = 事故重演"
        assert "frontend" in seen["spawned"], "拒一条腿不该连带放弃其它腿"
        assert ok is False, "后端没起来却返回成功"
        text = "\n".join(seen["lines"])
        assert "edge_tts" in text and "torch" in text, f"没说清缺哪些依赖：{text}"
        assert r"C:\bad\python.exe" in text, "没点名被判不合格的解释器"

    def test_launches_backend_with_the_picked_interpreter(self, monkeypatch, tmp_path):
        seen = self._harness(monkeypatch, tmp_path, {
            "exe": r"C:\ok\python.exe", "ok": True,
            "tried": [(r"C:\bad\python.exe", ["edge_tts"]), (r"C:\ok\python.exe", [])]})
        ok = start_all.orchestrate(_ns(only="backend"))
        assert "backend" in seen["spawned"]
        assert ok is True
        text = "\n".join(seen["lines"])
        assert r"C:\ok\python.exe" in text, "没自证用的是哪台解释器"


# ======================================================= 启动器自身那台解释器（PY-354 第二层）
# 后端腿换了合格解释器还不够：**主控台自己**也是 PATH 上那台 python 起的。
# 19:25 现场重放（`…\Python314\python.exe start_all.py --only backend --restart backend`）里，
# 3.14 缺 psutil，于是 `describe_pid()` 读不到命令行，`decide()` 把**自家后端**
# 判成"非本产品进程"→ blocked → 该腿跳过、什么都没重启，而进程照样回 rc=0。
# 两件事都要修：① 看不见的东西不得说成"外部进程"，且"没起"不能算成功；
#              ② 主控台换到自己那台合格的解释器上跑（换台一次，带环保护）。
class TestLauncherOwnDeps:
    def test_psutil_is_declared_as_launchers_own_dependency(self):
        """启动器的端口归因（`describe_pid`）与内存读数（`free_gb`）都吃 psutil。

        它**不在** `BACKEND_DEPS` 里是有意的：后端自己那几处 psutil 用法全带
        try/except 降级（`backend/api/workflows.py:153`、`backend/runtime_guard.py:76`、
        `modules/tts_engine/model_preloader.py:83`），缺了只是少点遥测，不会变哑；
        而启动器缺了会**下错判断**，所以按各自的判据分开列。
        """
        assert "psutil" in start_all.LAUNCHER_DEPS
        assert "psutil" not in start_all.BACKEND_DEPS

    def test_launcher_probe_argv_targets_the_candidate(self):
        argv = start_all.probe_launcher_argv(r"C:\somewhere\python.exe")
        assert argv[0] == r"C:\somewhere\python.exe"
        assert argv[1:3] == ["-X", "utf8"]
        assert argv[-2:] == ["-c", start_all.LAUNCHER_PROBE_SRC]
        assert "find_spec" in start_all.LAUNCHER_PROBE_SRC
        assert "psutil" in start_all.LAUNCHER_PROBE_SRC

    def test_the_two_probes_ask_their_own_question(self, monkeypatch):
        """后端探测问后端清单，启动器探测问启动器清单 —— 串了就没法被别的用例发现。

        变异 MI（把 `probe_launcher_python` 改成探 `BACKEND_DEPS`）在本机**行为完全
        一样**（3.11 两样都齐），所以只有把"实际发出去的探测源码"钉住才抓得到。
        串档的后果：换了台没装 torch 的机器，主控台会被判不合格而不换台，
        于是端口占用又读不到命令行。
        """
        sent = []

        def spy(exe, argv, timeout=30.0):
            sent.append(argv[-1])
            return []

        monkeypatch.setattr(start_all, "_read_probe", spy)
        start_all.probe_python(r"C:\x\python.exe")
        start_all.probe_launcher_python(r"C:\x\python.exe")
        assert len(sent) == 2, sent
        assert "torch" in sent[0] and "psutil" not in sent[0]
        assert "psutil" in sent[1] and "torch" not in sent[1]

    def test_real_machine_has_a_console_qualified_interpreter(self):
        """事故锚（不 skip）：本机必须选得出一台带 psutil 的解释器给主控台用。"""
        out = start_all.pick_launcher_python()
        assert out["ok"] is True, f"本机没有一台解释器能让启动器正确归因: {out['tried']}"
        assert start_all.probe_launcher_python(out["exe"]) == []

    def test_attribute_pid_reads_itself_on_the_real_path(self):
        """真机锁：本进程当然可归因，且报出的原文里带着解释器路径。

        单元桩替不了这条 —— 19:25 那次就是桩没覆盖 `import psutil` 失败这一支，
        而真机上这一支恰好是常态（PATH 上的裸 python 没装 psutil）。
        """
        desc, attributable = start_all.attribute_pid(os.getpid())
        assert attributable is True, f"读自己的命令行都读不到: {desc}"
        assert Path(sys.executable).name.lower() in desc.lower(), desc


class TestReexecTarget:
    CUR = r"C:\console\python.exe"

    def test_stays_when_self_is_the_qualified_interpreter(self, monkeypatch):
        monkeypatch.delenv(start_all.REEXEC_ENV, raising=False)
        assert start_all.reexec_target(current=self.CUR, want=self.CUR) == ""

    def test_stays_when_case_or_separator_differs(self, monkeypatch, tmp_path):
        """`C:\\A\\python.exe` 与 `c:\\a\\PYTHON.EXE` 是同一台：按大小比较会无限换台。"""
        monkeypatch.delenv(start_all.REEXEC_ENV, raising=False)
        twin = str(Path(self.CUR))
        assert start_all.reexec_target(
            current=twin.upper().replace("C:", "c:"), want=twin).lower() == ""

    def test_switches_when_a_different_qualified_interpreter_exists(self, monkeypatch):
        monkeypatch.delenv(start_all.REEXEC_ENV, raising=False)
        assert start_all.reexec_target(
            current=self.CUR, want=r"C:\Program Files\Python311\python.exe"
        ) == r"C:\Program Files\Python311\python.exe"

    def test_stays_when_no_interpreter_qualifies(self, monkeypatch):
        """一台都不合格时不换台 —— 空跑一圈子进程只会让启动更慢，且什么也修不好。"""
        monkeypatch.delenv(start_all.REEXEC_ENV, raising=False)
        assert start_all.reexec_target(current=self.CUR, want="") == ""

    def test_env_flag_disables_further_switching(self, monkeypatch):
        """换台后的那一次必须自认"够了"：否则父子无限对换（19:20 起草时先想到的一条）。"""
        monkeypatch.setenv(start_all.REEXEC_ENV, "1")
        assert start_all.reexec_target(current=self.CUR, want=r"C:\other\python.exe") == ""

    def test_reexec_argv_keeps_script_and_user_arguments(self):
        argv = start_all.reexec_argv(
            r"C:\py311\python.exe", r"D:\x\start_all.py",
            ["--only", "backend", "--restart", "backend"])
        assert argv == [r"C:\py311\python.exe", "-X", "utf8", r"D:\x\start_all.py",
                        "--only", "backend", "--restart", "backend"]


class TestUnattributableIsNotForeign:
    """读不到命令行 ≠ 不是本产品。这是 19:25 那次"重启没发生却报成功"的判据层。"""

    def _harness(self, monkeypatch, tmp_path, attrs: dict):
        seen = {"spawned": [], "lines": []}
        monkeypatch.setattr(start_all, "say", lambda msg="": seen["lines"].append(str(msg)))
        monkeypatch.setattr(start_all, "hdr", lambda msg="": seen["lines"].append(str(msg)))
        monkeypatch.setattr(start_all, "LOG_DIR", tmp_path)
        monkeypatch.setattr(start_all, "free_gb", lambda: 99.0)
        monkeypatch.setattr(start_all, "owning_pids", lambda port: [4242])
        monkeypatch.setattr(start_all, "attribute_pid",
                            lambda pid, reader=None: attrs[str(pid)])
        monkeypatch.setattr(start_all, "spawn_window",
                            lambda lg, **kw: seen["spawned"].append(lg.key) or True)
        monkeypatch.setattr(start_all, "wait_for_launch_banner", lambda *a, **k: True)
        monkeypatch.setattr(start_all, "pick_backend_python",
                            lambda candidates=None, probe=None: {
                                "exe": r"C:\ok\python.exe", "ok": True, "tried": []})
        return seen

    SIGNS = "backend.main:app"

    def test_readable_but_foreign_owner_is_still_blocked(self, monkeypatch, tmp_path):
        seen = self._harness(monkeypatch, tmp_path, {
            "4242": ("cmd=C:\\OtherApp\\server.exe", True)})
        ok = start_all.orchestrate(_ns(only="backend", restart=self.SIGNS))
        assert ok is False, "该腿没起来却报成功"
        assert "backend" not in seen["spawned"]
        text = "\n".join(seen["lines"])
        assert "非本产品" in text

    def test_unreadable_owner_is_reported_as_unattributable_not_foreign(
            self, monkeypatch, tmp_path):
        seen = self._harness(monkeypatch, tmp_path, {
            "4242": ("pid=4242 命令行不可读(启动器缺 psutil)", False)})
        ok = start_all.orchestrate(_ns(only="backend", restart=self.SIGNS))
        text = "\n".join(seen["lines"])
        assert "非本产品" not in text, f"看不见却断言是外部进程：{text}"
        assert "读不到它在跑什么" in text, f"没自曝判断不了：{text}"
        assert "backend" not in seen["spawned"], "归因不明照样动手 = 红线"
        assert ok is False, "什么都没重启却返回成功（19:25 现场重放的假绿）"

    def test_unattributable_block_is_named_in_the_conclusion(self, monkeypatch, tmp_path):
        seen = self._harness(monkeypatch, tmp_path, {
            "4242": ("pid=4242 命令行不可读(拒绝访问)", False)})
        start_all.orchestrate(_ns(only="backend"))
        text = "\n".join(seen["lines"])
        assert "未起" in text and "backend" in text, f"结论里没点名被跳过的腿：{text}"


class TestMemoryReadoutHonesty:
    def test_missing_memory_reads_as_unavailable_not_none(self, monkeypatch):
        """`可用内存 NoneGB` 是一句假话的形状：读者只能猜是没测到还是 0。"""
        monkeypatch.setattr(start_all, "free_gb", lambda: None)
        assert "None" not in start_all.memory_text()
        assert "读不到" in start_all.memory_text()

    def test_present_memory_reads_as_the_number(self, monkeypatch):
        monkeypatch.setattr(start_all, "free_gb", lambda: 12.5)
        assert start_all.memory_text() == "12.5GB"


class TestConsoleReexecWiring:
    """换台这一步的接线：什么时候换、换成什么、退出码怎么回来。"""

    def _run_spy(self):
        calls = []

        def run(argv, **kw):
            calls.append((argv, kw))
            return SimpleNamespace(returncode=7)

        return run, calls

    def test_switches_and_forwards_child_exit_code(self, monkeypatch):
        monkeypatch.delenv(start_all.REEXEC_ENV, raising=False)
        monkeypatch.setattr(start_all, "launcher_python",
                            lambda: r"C:\Program Files\Python311\python.exe")
        monkeypatch.setattr(start_all, "say", lambda msg="": None)
        run, calls = self._run_spy()
        # current 显式给一台**不合格**的：本测试模拟的是"控制台开在 3.14 上"那台机器，
        # 不写死就会随跑 pytest 的解释器不同而时换时不换（19:40 实测到的桩缺陷）。
        rc = start_all.reexec_console(args=_ns(), argv=["--only", "backend"],
                                      script=r"D:\x\start_all.py",
                                      current=r"C:\py314\python.exe", run=run)
        assert rc == 7, "换台后的退出码没回传（父窗口会把它当成功）"
        assert len(calls) == 1, "换台不止一次 = 可能自我循环"
        argv, kw = calls[0]
        assert argv == [r"C:\Program Files\Python311\python.exe", "-X", "utf8",
                        r"D:\x\start_all.py", "--only", "backend"]
        assert kw["env"][start_all.REEXEC_ENV] == "1", "没给子进程留环保护标记"
        assert kw["shell"] is False

    def test_no_switch_when_self_is_qualified(self, monkeypatch):
        monkeypatch.delenv(start_all.REEXEC_ENV, raising=False)
        monkeypatch.setattr(start_all, "launcher_python", lambda: sys.executable)
        run, calls = self._run_spy()
        assert start_all.reexec_console(args=_ns(), argv=[], script=r"D:\x\s.py",
                                        run=run) is None
        assert calls == [], "合格的主控台不该多起一个自己"

    def test_run_leg_child_never_switches(self, monkeypatch):
        """子窗口跑的是主控台定死的 argv；再换台就是拿别人的窗口去探路。"""
        monkeypatch.delenv(start_all.REEXEC_ENV, raising=False)
        monkeypatch.setattr(start_all, "launcher_python",
                            lambda: r"C:\Program Files\Python311\python.exe")
        run, calls = self._run_spy()
        args = _ns(run_leg="backend")
        got = start_all.reexec_console(args=args, argv=[], script=r"D:\x\s.py",
                                       current=r"C:\py314\python.exe", run=run)
        assert got is None and calls == []

    def test_already_switched_child_never_switches_again(self, monkeypatch):
        monkeypatch.setenv(start_all.REEXEC_ENV, "1")
        monkeypatch.setattr(start_all, "launcher_python",
                            lambda: r"C:\Program Files\Python311\python.exe")
        run, calls = self._run_spy()
        got = start_all.reexec_console(args=_ns(), argv=[], script=r"D:\x\s.py",
                                       current=r"C:\py314\python.exe", run=run)
        assert got is None and calls == []

    def test_orchestrate_reports_the_console_interpreter(self, monkeypatch, tmp_path):
        """自证：主控台跑在哪台解释器上、它够不够格读端口归属。"""
        seen = {"lines": []}
        monkeypatch.setattr(start_all, "say", lambda msg="": seen["lines"].append(str(msg)))
        monkeypatch.setattr(start_all, "hdr", lambda msg="": seen["lines"].append(str(msg)))
        monkeypatch.setattr(start_all, "LOG_DIR", tmp_path)
        monkeypatch.setattr(start_all, "free_gb", lambda: 99.0)
        monkeypatch.setattr(start_all, "decide", lambda lg, restart: "launch")
        monkeypatch.setattr(start_all, "spawn_window", lambda lg, **kw: True)
        monkeypatch.setattr(start_all, "wait_for_launch_banner", lambda *a, **k: True)
        monkeypatch.setattr(start_all, "pick_launcher_python",
                            lambda candidates=None, probe=None: {
                                "exe": r"C:\console\python.exe", "ok": True, "tried": []})
        monkeypatch.setattr(start_all, "pick_backend_python",
                            lambda candidates=None, probe=None: {
                                "exe": r"C:\ok\python.exe", "ok": True, "tried": []})
        start_all.orchestrate(_ns(only="frontend"))
        text = "\n".join(seen["lines"])
        assert r"C:\console\python.exe" in text, "没自证主控台跑在哪台解释器上"


# ================================================= U-1（2026-09-27）：旧入口下线后的唯一口径守卫
# 原 tests/test_py358_legacy_entrypoints.py 退役（产品裁定：旧三枚本身就是"用户不知道点
# 哪个"的根源）。其中关于 `--print-python` 唯一口径 / 绝不回退裸 python 的活体断言
# 逐字搬到本节继续测 start_all.py；关于旧入口文本形状的静态断言改为"文件不在盘上"守卫。
ROOT = Path(__file__).resolve().parents[1]
PY311 = r"C:\Program Files\Python311\python.exe"


def _run_py(args, python=PY311, timeout=120, env=None):
    return subprocess.run([python, "-X", "utf8"] + args, capture_output=True,
                          encoding="utf-8", errors="replace", timeout=timeout,
                          cwd=str(ROOT), shell=False, env=env)


def test_legacy_entrypoints_are_gone():
    """旧三枚必须不在盘上 —— 这条在 git rm 之前必然是红的，红本身就是"删除发生过"的证据。"""
    for name in ("start.bat", "start.ps1", "start_gui.py"):
        assert not (ROOT / name).exists(), f"旧入口 {name} 又回到盘上：'点哪个'的问题会回来"


def test_entry_shells_have_no_second_startup_logic():
    """两枚壳只做转发：不得自带后端启动行（裸 uvicorn = PY-358 守的那扇后门的新形状）。"""
    for name in ("start-all.cmd", "一键启动.bat"):
        src = (ROOT / name).read_text(encoding="utf-8")
        assert "start_all.py" in src or "start-all.cmd" in src, f"{name} 没交到唯一实现"
        assert "uvicorn" not in src.lower(), f"{name} 里出现第二套后端启动行"


def test_entry_shells_announce_single_entry_banner():
    """横幅要中文、一行、具体：唯一入口 + 旧入口下线日期，用户双击即看得见。"""
    for name in ("start-all.cmd", "一键启动.bat"):
        src = (ROOT / name).read_text(encoding="utf-8")
        assert "唯一入口" in src and "2026-09-27" in src, f"{name} 横幅没说唯一入口/下线日期"


def test_orchestrate_banner_declares_single_entry(monkeypatch, tmp_path):
    """启动横幅自证唯一入口：主控台第一屏就要把"旧入口已下线"说出来。"""
    lines = []
    monkeypatch.setattr(start_all, "say", lambda msg="": lines.append(str(msg)))
    monkeypatch.setattr(start_all, "hdr", lambda msg="": lines.append(str(msg)))
    monkeypatch.setattr(start_all, "LOG_DIR", tmp_path)
    monkeypatch.setattr(start_all, "free_gb", lambda: 99.0)
    monkeypatch.setattr(start_all, "decide", lambda lg, restart: "launch")
    monkeypatch.setattr(start_all, "spawn_window", lambda lg, **kw: True)
    monkeypatch.setattr(start_all, "wait_for_launch_banner", lambda *a, **k: True)
    monkeypatch.setattr(start_all, "pick_backend_python",
                        lambda candidates=None, probe=None: {
                            "exe": r"C:\ok\python.exe", "ok": True, "tried": []})
    monkeypatch.setattr(start_all.time, "sleep", lambda s: None)
    start_all.orchestrate(_ns(only="frontend"))
    text = "\n".join(lines)
    assert "唯一入口" in text and "一键启动.bat" in text, f"启动横幅没宣告唯一入口: {text}"


# ------------------------------------ 以下四枚逐字搬自 test_py358_legacy_entrypoints.py
# 它们测的是 `--print-python` 出口本身（唯一后端解释器口径），与被删的旧入口无关。
def test_print_python_success_paths_and_zero():
    """打桩=只起探测子进程：正常机器上应打印选定解释器的绝对路径且 rc=0。"""
    r = _run_py([str(ROOT / "start_all.py"), "--print-python"])
    assert r.returncode == 0, r.stdout + r.stderr
    lines = [l.strip() for l in (r.stdout or "").splitlines() if l.strip()]
    assert len(lines) == 1, f"stdout 必须恰好一行可执行路径，拿到: {lines}"
    assert Path(lines[0]).is_absolute(), lines
    assert Path(lines[0]).name.lower() in ("python.exe", "pythonw.exe", "python"), lines
    assert Path(lines[0]).exists(), lines


_STUB_ALL_CANDIDATES_BAD = (
    "import sys; sys.path.insert(0, {root!r}); import start_all; "
    "start_all.candidate_pys = lambda: [{bad!r}]; "
    "raise SystemExit(start_all.main(['--print-python']))"
)


def test_print_python_failure_is_nonzero_and_silent_on_exe():
    """打桩把候选清单钉成一台假解释器（探测子进程起不来=不合格）⇒
    rc≠0、不打印任何可执行路径、给出原因与指路（start-all / setup_deps.py）。"""
    code = _STUB_ALL_CANDIDATES_BAD.format(
        root=str(ROOT), bad=str(ROOT / "tests" / "_no_such_python_xyz.exe"))
    r = _run_py(["-c", code])
    assert r.returncode != 0, (r.stdout, r.stderr)
    assert (r.stdout or "").strip() == "", f"失败时 stdout 必须为空: {r.stdout}"
    out = (r.stdout or "") + (r.stderr or "")
    for line in out.splitlines():
        assert not line.strip().lower().endswith(
            ("python.exe", "pythonw.exe")), f"失败时不得打印可执行路径: {line}"
    assert "setup_deps" in out or "start-all" in out, out


def test_print_python_works_without_launcher_deps(monkeypatch, capsys):
    """出口必须能在**没装依赖的解释器**上跑（正是那种机器上要问它）：
    全程只走 subprocess 探测，不 import backend、不 import psutil。"""
    monkeypatch.setattr(start_all, "pick_backend_python",
                        lambda **kw: {"exe": PY311, "ok": True, "tried": []})
    assert start_all.main(["--print-python"]) == 0
    assert capsys.readouterr().out.strip() == PY311


def test_print_python_refuses_without_printing_exe(monkeypatch, capsys):
    def boom(**kw):
        raise AssertionError("探测失败时不得起后端")

    monkeypatch.setattr(start_all, "pick_backend_python",
                        lambda **kw: {"exe": sys.executable, "ok": False,
                                      "tried": [(sys.executable, ["edge_tts"])]})
    monkeypatch.setattr(start_all, "orchestrate", boom)
    rc = start_all.main(["--print-python"])
    assert rc != 0
    out = capsys.readouterr()
    assert out.out.strip() == "", f"失败时 stdout 不得打印任何解释器: {out.out}"
    assert sys.executable not in out.out, out.out
    assert "edge_tts" in out.out + out.err


# ------------------------------------------- DeprecationWarning 尾巴（同样搬自 py358）
def test_importing_start_all_under_error_deprecationwarning():
    """`invalid escape sequence '\\W'`（docstring 里的 `C:\\Windows\\...`）：
    用 compile() 复检 —— 走 import 会被 __pycache__ 吞掉警告，判据不可靠。"""
    code = ("src=open('start_all.py',encoding='utf-8').read();"
            "compile(src,'start_all.py','exec'); import start_all")
    r = subprocess.run([PY311, "-X", "utf8", "-W", "error::DeprecationWarning",
                        "-c", code], capture_output=True, encoding="utf-8",
                       errors="replace", timeout=120, cwd=str(ROOT), shell=False)
    assert r.returncode == 0, r.stderr
    assert "DeprecationWarning" not in r.stderr, r.stderr
    assert r.stderr == "", repr(r.stderr)
