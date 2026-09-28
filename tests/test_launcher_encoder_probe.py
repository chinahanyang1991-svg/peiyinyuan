r"""启动器必须提前告诉你「这台起的后端能不能出 mp3」（PY-357，2026-09-24 18:31 现场）。

现象：用户拿到的报错原文是
`输出格式 mp3 需要 ffmpeg 编码器，当前环境未找到可用的 ffmpeg：… imageio-ffmpeg:
导入失败 (ModuleNotFoundError: No module named 'imageio_ffmpeg')`。

归因（已复采，见 qa/LEDGER.md §36 与 issues.md PY-357）：那台后端是**修复前的启动器**
用 PATH 上的裸 `python`（3.14）起来的 —— `imageio-ffmpeg` 只装在 `C:\Program Files\Python311`
的 user site。换台之后这条路真的能出 mp3（现场复跑：产品自己的 `_write_mp3_via_ffmpeg`
产出 21,986 B、`ID3` 头、解码 rc=0 的文件）。

但同类缺口还留着：`BACKEND_DEPS` 里**没有** `imageio_ffmpeg`（那是 pyproject 的
optional extra `codecs`，不是 import 依赖），所以启动器仍可能挑中一台"能说话、不能出
mp3"的解释器，而且全程一声不吭 —— 用户要到点导出的那一刻才撞上 503。

修法口径（与 PY-354 的分工一致）：
  - 这是**软**依赖：缺了只影响 mp3/m4b 两种格式，wav/flac 照旧。所以只报**警告**，
    绝不拒起后端腿（拒 = 违反"程序能跑"红线）。
  - 探测走**产品自己的解析口** `ffmpeg_utils.probe()`，而不是"某个包在不在"的代理判断：
    三档来源（Config.FFMPEG_PATH → PATH ffmpeg → imageio-ffmpeg）任一能用都不该吓唬用户。
"""

import argparse
import importlib.util
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import start_all  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    """探测结果按测试逐枚重算：进程级缓存会把上一枚的结论漏给下一枚。"""
    monkeypatch.setattr(start_all, "_BACKEND_PICK", {})
    monkeypatch.setattr(start_all, "_LAUNCHER_PICK", {})
    monkeypatch.setattr(start_all, "_ENCODER_PROBE", {})


def _ns(**kw):
    base = {"only": "backend,frontend,gsv", "restart": "", "restart_all": False,
            "reload": False, "no_health": True, "no_pause": True,
            "boot_timeout": 1, "model_timeout": 1, "front_timeout": 1,
            "gsv_timeout": 1}
    base.update(kw)
    return argparse.Namespace(**base)


def _enc_lines(lines):
    """只看编码器那几行：其它行由别的用例管，混在一起会让"多打一行"这种回归看不见。"""
    hit = [l for l in lines if "编码器" in l]
    if not hit:
        return []
    i = lines.index(hit[0])
    return [l for l in lines[i:] if "编码器" in l or l.strip().startswith(("修法", "后端", "不代表"))]


class TestProbeSource:
    def test_asks_the_products_own_resolver_not_a_package_proxy(self):
        """探测源码必须真调 `ffmpeg_utils.probe()`。

        换成 `find_spec("imageio_ffmpeg")` 会误报：本机 PATH 上没有 ffmpeg，但
        `Config.FFMPEG_PATH` 或 PATH 兜底一旦可用，包不在也照样出 mp3 —— 按包判断
        就是在对着能用的用户喊"你缺东西"。
        """
        src = start_all.ENCODER_PROBE_SRC
        assert "ffmpeg_utils" in src and "probe()" in src
        assert "find_spec" not in src, "又退回包名代理判断了"
        # 输出形状：一行 JSON，含 path/source/error
        for k in ("path", "source", "error"):
            assert f"'{k}'" in src or f'"{k}"' in src, f"探测源码没带出 {k}"

    def test_argv_targets_the_chosen_interpreter(self):
        argv = start_all.probe_encoder_argv(r"C:\somewhere\python.exe")
        assert argv[0] == r"C:\somewhere\python.exe"
        assert argv[1:3] == ["-X", "utf8"], "子进程输出按 utf-8 取，坑见 PY-344"
        assert argv[-2:] == ["-c", start_all.ENCODER_PROBE_SRC]


class TestProbeRealPath:
    def test_real_interpreter_answers_with_the_same_shape_as_the_module(self):
        """真机锁（不 skip）：拿当前解释器跑一次，结论要和直接调 `probe()` 一致。

        桩盖不到的是"这条探测源码在真机上能不能跑通"——cwd、sys.path、GBK 输出
        都在这条路上（同一族坑见 PY-344：解不出来就得判不可得，不能判没缺）。
        """
        out = start_all.probe_encoder(sys.executable)
        assert isinstance(out, dict), "真机探测读不出结果"
        assert set(out) >= {"path", "source", "error"}
        if out["path"]:
            assert Path(out["path"]).is_file(), f"报出来的可执行文件不在盘上: {out}"
            assert out["error"] in (None, ""), out
        else:
            assert out["error"], "不可用却没给原因"

    def test_a_broken_interpreter_is_unavailable_not_available(self, monkeypatch):
        """探测起不来/输出不成形 ⇒ None（判"读不到"），绝不能当成"编码器没问题"。"""
        monkeypatch.setattr(start_all, "_read_probe_json", lambda *a, **k: None)
        assert start_all.probe_encoder(r"C:\dead\python.exe") is None

    def test_probe_is_computed_once_per_process(self, monkeypatch):
        calls = []

        def spy(exe, timeout=None):
            calls.append(exe)
            return {"path": "x", "source": "path", "error": None}

        monkeypatch.setattr(start_all, "probe_encoder", spy)
        monkeypatch.setattr(start_all, "pick_backend_python",
                            lambda candidates=None, probe=None: {
                                "exe": r"C:\ok\python.exe", "ok": True, "tried": []})
        for _ in range(3):
            start_all.encoder_probe()
        assert len(calls) == 1, "每问一次就重跑一次子进程 = 启动白等"


class TestReportOnlyRefusesNothing:
    """这一族是这条修复的**红线**：编码器缺了只能警告，后端腿必须照起。"""

    def _harness(self, monkeypatch, tmp_path, enc):
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
                            lambda candidates=None, probe=None: {
                                "exe": r"C:\ok\python.exe", "ok": True, "tried": []})
        monkeypatch.setattr(start_all, "probe_encoder",
                            lambda exe, timeout=None: enc)
        monkeypatch.setattr(start_all.time, "sleep", lambda s: None)
        return seen

    OK = {"path": r"C:\ffmpeg\bin\ffmpeg.exe", "source": "path", "error": None}
    MISSING = {"path": None, "source": None,
               "error": ("未找到可用的 ffmpeg (path: ffmpeg 不可执行或 -version 失败; "
                         "imageio-ffmpeg: 导入失败 (ModuleNotFoundError: No module named "
                         "'imageio_ffmpeg'))。安装 ffmpeg 并加入 PATH，或安装 Python 包: "
                         "pip install imageio-ffmpeg")}

    def test_available_encoder_names_the_source(self, monkeypatch, tmp_path):
        seen = self._harness(monkeypatch, tmp_path, self.OK)
        ok = start_all.orchestrate(_ns(only="backend"))
        enc = _enc_lines(seen["lines"])
        assert enc, "压根没报编码器结论"
        line = "\n".join(enc)
        assert "mp3" in line, f"没交代导出格式能用到什么：{line}"
        assert "path" in line, f"没自证用的是哪一档来源：{line}"
        assert "!!" not in line, f"可用却报了警告：{line}"
        assert ok is True

    @pytest.mark.parametrize("enc,expect", [
        (MISSING, "缺 mp3"),
        (None, "读不到"),
    ], ids=["encoder-missing", "probe-unreadable"])
    def test_missing_or_unreadable_warns_but_still_launches(self, monkeypatch, tmp_path,
                                                            enc, expect):
        seen = self._harness(monkeypatch, tmp_path, enc)
        ok = start_all.orchestrate(_ns(only="backend"))
        text = "\n".join(seen["lines"])
        assert "backend" in seen["spawned"], (
            f"缺编码器就拒起后端 = 把 mp3 一种格式的缺，扩大成整个程序不能用：{text}")
        assert ok is True, "软依赖缺失不该翻转退出码（wav/flac 仍可转换）"
        line = "\n".join(_enc_lines(seen["lines"]))
        assert expect in line, f"该说清的话没说（期望含「{expect}」）：{line}"
        assert "wav" in line, f"没告诉用户哪些格式不受影响：{line}"
        assert "503" in line, f"没说清用户会在哪一步撞上：{line}"

    def test_encoder_hint_survives_the_healthy_case_without_filling_the_screen(
            self, monkeypatch, tmp_path):
        """齐全时只打一行；缺了才展开修法。缺省路径的噪音也是一种回归。"""
        seen = self._harness(monkeypatch, tmp_path, self.OK)
        start_all.orchestrate(_ns(only="backend"))
        assert len(_enc_lines(seen["lines"])) == 1

    def test_the_refused_leg_does_not_also_claim_the_encoder_is_fine(
            self, monkeypatch, tmp_path):
        """后端压根没起时不该再报编码器结论（那台解释器不是被选中的那台）。"""
        seen = self._harness(monkeypatch, tmp_path, self.OK)
        monkeypatch.setattr(start_all, "pick_backend_python",
                            lambda candidates=None, probe=None: {
                                "exe": r"C:\bad\python.exe", "ok": False,
                                "tried": [(r"C:\bad\python.exe", ["edge_tts"])]})
        start_all.orchestrate(_ns(only="backend"))
        text = "\n".join(seen["lines"])
        assert _enc_lines(seen["lines"]) == [], f"腿被拒起却还在自证编码器：{text}"
        assert "mp3" not in text, f"腿被拒起却还在自证编码器：{text}"


class TestEncoderIsNotABootGate:
    def test_imageio_ffmpeg_stays_out_of_the_hard_dependency_list(self):
        """`imageio_ffmpeg` **不进** `BACKEND_DEPS`：进了就等于"缺它就拒起后端"。

        它是 pyproject 的 optional extra `codecs`（`modules/tts_engine/ffmpeg_utils.py:31`
        的 `FFMPEG_FORMATS = ("mp3", "m4b")` 才是它服务的对象），且还有 PATH ffmpeg
        这一档兜底 —— 按硬依赖处理会误拒一大批本来能跑的机器。
        """
        assert "imageio_ffmpeg" not in start_all.BACKEND_DEPS
        assert "imageio_ffmpeg" in start_all.SOFT_DEPS

    def test_soft_dependency_is_declared_with_what_it_costs(self):
        """软依赖必须自带"缺了什么后果"，否则警告只是一句"缺包"，用户无法决策。"""
        entry = start_all.SOFT_DEPS["imageio_ffmpeg"]
        assert entry["formats"] == ("mp3", "m4b")
        assert "pip install imageio-ffmpeg" in entry["fix"]

    def test_probe_runs_from_the_repo_root_so_the_import_resolves(self):
        """探测源码靠 `from modules.tts_engine import ...`，前提是把仓库根当 cwd。

        这条钉住那个前提（不是顺手写的）：cwd 报错树时会读到另一份代码，
        而"另一份"恰恰是 PY-345 那类哑实例的来源。
        """
        assert Path(start_all.ENCODER_CWD).is_dir()
        assert (Path(start_all.ENCODER_CWD) / "modules/tts_engine/ffmpeg_utils.py").is_file()
        assert importlib.util.find_spec("modules.tts_engine.ffmpeg_utils")
