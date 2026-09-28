"""
ffmpeg 统一探测与调用入口 (PY-302 / PY-125 / PY-126 / PY-127)

背景 (round-1b 实测):
    - `M4BBuilder.available` 与 `_stage_package` 各自裸调 `subprocess.run(["ffmpeg", ...])`,
      只要 PATH 里没有 ffmpeg 就全盘失败;
    - `_write_mp3_via_ffmpeg` 干脆不探测, 直接抛 WinError 2 的 FileNotFoundError,
      上层拿到的是无上下文的原始异常 (PY-127)。

解析优先级 (第一个真正可执行并跑通 `-version` 的候选胜出):
    1. `Config.FFMPEG_PATH` / 环境变量 `FFMPEG_PATH` —— 显式指定绝对路径时最优先;
    2. PATH 中的 `ffmpeg`;
    3. pip 包 `imageio-ffmpeg` 自带的静态可执行文件 (`get_ffmpeg_exe()`)。
       这是无需管理员权限、跨平台的兜底: `pip install imageio-ffmpeg`
       (pyproject 中为 optional-dependencies `codecs` 组)。

只探测一次并缓存结果 (`_CACHE`)，避免每次打包都 spawn 进程。
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from typing import Optional

logger = logging.getLogger(__name__)

# 需要 ffmpeg 才能产出的目标格式
FFMPEG_FORMATS = ("mp3", "m4b")

# 与 issues-round1b / PROTOCOL 对齐的提示语（GBK 终端安全，无 emoji）
INSTALL_HINT = ("安装 ffmpeg 并加入 PATH，或安装 Python 包: "
                "pip install imageio-ffmpeg (本仓库已支持其自带可执行文件)")

# (path, source, version) —— 解析成功后写入；失败写入 _CACHE["error"]
_CACHE: dict = {}


def _run_version(exe: str, timeout: float = 6.0) -> Optional[str]:
    """执行 `<exe> -version`，成功返回版本首行，失败返回 None。"""
    try:
        # GBK 终端下 text=True 用本地编码解码 ffmpeg 输出会抛 UnicodeDecodeError
        # (2c 冒烟实测: _readerthread 'gbk' codec can't decode byte 0x80)，
        # 统一强制 utf-8 + replace。
        res = subprocess.run([exe, "-version"], capture_output=True,
                             text=True, encoding="utf-8", errors="replace",
                             timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    if res.returncode != 0 or not res.stdout:
        return None
    return res.stdout.splitlines()[0].strip()


def _try(exe: str, source: str) -> Optional[dict]:
    if not exe:
        return None
    exe = exe.strip().strip('"')
    # 显式路径必须是真实存在的文件；PATH 名字交给 which() 解析
    if os.sep in exe or (os.altsep and os.altsep in exe):
        if not os.path.isfile(exe):
            return None
    else:
        found = shutil.which(exe)
        if not found:
            return None
        exe = found
    version = _run_version(exe)
    if not version:
        return None
    return {"path": exe, "source": source, "version": version}


def probe(force: bool = False) -> dict:
    """探测 ffmpeg，返回 {path, source, version, error}。

    path 为 None 表示不可用，error 给出原因与修复建议。
    """
    if _CACHE and not force:
        return dict(_CACHE)

    result = {"path": None, "source": None, "version": None, "error": None}
    tried = []

    cfg = ""
    try:
        from backend.config import Config
        cfg = str(getattr(Config, "FFMPEG_PATH", "") or "").strip()
    except Exception as e:  # pragma: no cover - 配置不可用时退化为自动探测
        logger.warning(f"[ffmpeg] 读取 Config.FFMPEG_PATH 失败，转为自动探测: {e}")

    candidates = []
    if cfg and cfg.lower() not in ("ffmpeg", "ffmpeg.exe"):
        candidates.append((cfg, "config"))
    candidates.append(("ffmpeg", "path"))
    candidates.append((None, "imageio-ffmpeg"))

    for exe, source in candidates:
        if exe is None:
            try:
                import imageio_ffmpeg
                exe = imageio_ffmpeg.get_ffmpeg_exe()
            except Exception as e:
                tried.append(f"imageio-ffmpeg: 导入失败 ({type(e).__name__}: {e})")
                continue
        hit = _try(exe, source)
        if hit:
            result = dict(hit, error=None)
            logger.info(f"[ffmpeg] 使用 {source} 来源: {result['path']} "
                        f"({result['version']})")
            break
        tried.append(f"{source}: {exe or '未提供'} 不可执行或 -version 失败")

    if result["path"] is None:
        result["error"] = f"未找到可用的 ffmpeg ({'; '.join(tried)})。{INSTALL_HINT}"
        logger.warning(f"[ffmpeg] {result['error']}")

    _CACHE.clear()
    _CACHE.update(result)
    return dict(result)


def resolve_ffmpeg() -> Optional[str]:
    """返回可用的 ffmpeg 可执行文件绝对路径；不可用返回 None。"""
    return probe().get("path")


def is_available() -> bool:
    return bool(resolve_ffmpeg())


def unavailable_reason() -> str:
    err = probe().get("error")
    return err or "ffmpeg 可用"


def requires_ffmpeg(fmt: Optional[str]) -> bool:
    return (fmt or "").strip().lower().lstrip(".") in FFMPEG_FORMATS


class FFmpegUnavailableError(RuntimeError):
    """缺少 ffmpeg 导致的编码失败 —— 语义上属于环境问题 (5xx / 任务失败)，
    消息自带修复建议，不再是裸 FileNotFoundError (PY-127)。"""

    def __init__(self, want_format: str, detail: str = ""):
        self.want_format = want_format
        self.detail = detail
        msg = (f"输出格式 {want_format} 需要 ffmpeg 编码器，但当前环境未找到可用的 "
               f"ffmpeg。{unavailable_reason()}")
        if detail:
            msg += f" (原始错误: {detail})"
        super().__init__(msg)


def run_ffmpeg(args: list, *, desc: str = "ffmpeg", timeout: Optional[float] = None,
               want_format: str = "") -> subprocess.CompletedProcess:
    """统一的 ffmpeg 调用：自动补上解析出的可执行文件路径并翻译错误语义。

    - ffmpeg 缺失 -> FFmpegUnavailableError（明确 5xx 语义 + 修复建议）
    - 执行失败 -> RuntimeError，带 stderr 尾部，不再静默
    """
    import shlex

    exe = resolve_ffmpeg()
    if not exe:
        raise FFmpegUnavailableError(want_format or desc)
    cmd = [exe] + [str(a) for a in args]
    try:
        res = subprocess.run(cmd, capture_output=True,
                             text=True, encoding="utf-8", errors="replace",
                             timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(
            f"{desc} 超时 (>{timeout}s): {shlex.join(cmd) if hasattr(shlex, 'join') else cmd}"
        ) from e
    except OSError as e:
        # 探测通过但执行仍失败（例如文件被删）：翻译为同类语义
        raise FFmpegUnavailableError(want_format or desc, detail=str(e)) from e
    if res.returncode != 0:
        raise RuntimeError(
            f"{desc} 失败 (rc={res.returncode}): {shlex.join(cmd) if hasattr(shlex, 'join') else cmd}\n"
            f"stderr: {(res.stderr or '')[-800:]}"
        )
    return res


def reset_cache() -> None:
    """仅供测试/Harness：清掉探测缓存，强制重新解析。"""
    _CACHE.clear()
