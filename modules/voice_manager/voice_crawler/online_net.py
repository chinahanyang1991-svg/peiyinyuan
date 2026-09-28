"""
在线音色源的公共网络层（VOICE-ONLINE #89）

这里只解决三件事，都是历史缺陷的直接对应：

1. **必须有超时**：以前 `httpx.Client(timeout=30.0)` 配 `2 ** attempt` 的退避重试，
   单个源最坏要 30+1+30+2+30 ≈ 93 秒才失败一次；而调用点在 `async def` 路由里，
   同步 socket 一挂就把整个事件循环拖住 —— 表现就是"完全无响应"。
   现在：连接 5s / 读 10s（env 可覆盖），每端点 1 次尝试，失败立刻换下一端点，
   整体再包一层 `deadline_s` 硬预算。
2. **端点真实可达性要说实话**：本环境实测 `huggingface.co` 连接失败（curl code=000
   "Failed to connect to huggingface.co port 443"），镜像 `hf-mirror.com` 与
   `api.github.com` 正常返回条目。所以官方端点不再"沉默失败"，而是被记进冷却表
   并在响应里如实标为不可用（含真实错误文本），同时把可用端点排在前面。
   冷却到期会自动再试一次（网络状态会变，不把结论写死在代码里）。
3. **绝对不编数据**：以前 API 打不通就返回一份手写 "well-known" 列表
   （downloads=999999），前端看起来"搜到了"，下载必然失败 —— 这正是
   "在线音色库拿不到音色文件"的后半段原因。现在失败只返回空列表 + 结构化原因。

音频"落到磁盘"的逻辑也放这里：搜索到的条目必须真的能下载、能解码、能试听。
"""

import os
import time
import logging
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

try:
    import httpx
    HAS_HTTPX = True
except ImportError:  # pragma: no cover - 依赖缺失时上层给出结构化错误
    HAS_HTTPX = False


# ---------------------------------------------------------------- 超时预算
def _env_f(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "") or default)
    except (TypeError, ValueError):
        return default


# 实测（本机，直连不可达的 huggingface.co）：墙钟耗时 ≈ 配置连接超时的 2 倍 ——
#   connect=3.0 -> ConnectTimeout wall=6.65s
#   connect=5.0 -> ConnectTimeout wall=11.08s
#   connect=12.0 -> ConnectTimeout wall=24.07s
# 因为 DNS/连接工作在 anyio 无法取消的线程里，超时只能等它自己回来。
# 所以连接阶段必须钉死在小值：3s ⇒ 死端点最坏 ~7s，还能在源预算内换到镜像。
CONNECT_TIMEOUT_S = _env_f("VOICE_ONLINE_CONNECT_TIMEOUT_S", 3.0)
READ_TIMEOUT_S = _env_f("VOICE_ONLINE_READ_TIMEOUT_S", 10.0)
WRITE_TIMEOUT_S = _env_f("VOICE_ONLINE_WRITE_TIMEOUT_S", 10.0)
POOL_TIMEOUT_S = _env_f("VOICE_ONLINE_POOL_TIMEOUT_S", 5.0)

# 一次 search 的总预算（含所有端点尝试）。路由层还会再包一层 asyncio.wait_for。
SEARCH_DEADLINE_S = _env_f("VOICE_ONLINE_SEARCH_DEADLINE_S", 12.0)
DOWNLOAD_DEADLINE_S = _env_f("VOICE_ONLINE_DOWNLOAD_DEADLINE_S", 60.0)
# 下载体积上限：在线库拿的是试听样本，几十 MB 足够；防止把盘打爆。
MAX_DOWNLOAD_BYTES = int(_env_f("VOICE_ONLINE_MAX_DOWNLOAD_BYTES", 25 * 1024 * 1024))
# 死端点冷却窗口（秒）：窗口内直接跳过，不再消耗超时预算。
BAD_ENDPOINT_COOLDOWN_S = _env_f("VOICE_ONLINE_BAD_ENDPOINT_COOLDOWN_S", 300.0)

AUDIO_SUFFIXES = (".wav", ".mp3", ".flac", ".ogg", ".m4a", ".opus")
_WAV_TARGET_RATE = 48000


def http_timeout() -> "httpx.Timeout":
    return httpx.Timeout(
        CONNECT_TIMEOUT_S,
        read=READ_TIMEOUT_S,
        write=WRITE_TIMEOUT_S,
        pool=POOL_TIMEOUT_S,
    )


def request_timeout(remaining: float) -> "httpx.Timeout":
    """按剩余预算收紧读/写/池，但**连接阶段始终钉死** `CONNECT_TIMEOUT_S`。

    传裸 float 会把 connect 也放大成剩余预算（实测一个死端点因此吃掉 24s，
    而整个源预算才 12s）—— 这条必须显式。
    """
    left = max(0.5, float(remaining))
    return httpx.Timeout(min(left, READ_TIMEOUT_S + CONNECT_TIMEOUT_S),
                         connect=CONNECT_TIMEOUT_S,
                         read=min(left, READ_TIMEOUT_S),
                         write=min(left, WRITE_TIMEOUT_S),
                         pool=min(left, POOL_TIMEOUT_S))


def make_client(headers: Dict[str, str]) -> Optional["httpx.Client"]:
    """带真实超时的客户端；httpx 不可用时返回 None（调用方必须给出结构化错误）。"""
    if not HAS_HTTPX:
        return None
    return httpx.Client(
        timeout=http_timeout(),
        headers=headers,
        follow_redirects=True,
        limits=httpx.Limits(max_keepalive_connections=4, max_connections=8),
    )


def base_of(url: str) -> str:
    """https://host/a/b -> https://host（端点键，供冷却表使用）。"""
    parts = url.split("/")
    if len(parts) >= 3:
        return parts[0] + "//" + parts[2]
    return url


def classify_error(err: BaseException) -> Tuple[str, str]:
    """把异常分成可读 reason + 原始错误文本（前端按 reason 出文案，原文兜底）。"""
    text = f"{type(err).__name__}: {err}"
    name = type(err).__name__.lower()
    code = getattr(getattr(err, "response", None), "status_code", 0) or 0
    if "timeout" in name or "timeout" in text.lower():
        return "timeout", text
    if HAS_HTTPX and isinstance(err, httpx.HTTPStatusError):
        if code in (401, 403):
            return "forbidden", text
        if code == 404:
            return "not_found", text
        if code == 429:
            return "rate_limited", text
        return "http_error", text
    if "connectserver" in name or "connect" in name or "resolver" in name:
        return "unreachable", text
    if "ssl" in name or "tls" in name:
        return "tls_error", text
    if "toomanyredirects" in name:
        return "redirect_loop", text
    if "decoding" in name:
        return "bad_payload", text
    return "network_error", text


# 只有这些原因说明"端点本身有问题"，才值得换下一个端点/进冷却表
FAILOVER_REASONS = frozenset({
    "timeout", "unreachable", "tls_error", "network_error", "redirect_loop", "http_error",
})


# ---------------------------------------------------------------- 端点池
class EndpointPool:
    """按真实可达性排序的候选端点 + 失败冷却。

    排序完全由**当次实测**决定：某端点连接失败/超时就进冷却
    （`BAD_ENDPOINT_COOLDOWN_S` 秒），成功的端点提到最前。代码里不写死
    "谁死了"，只写死"死了要如实上报"。
    """

    def __init__(self, bases: List[str], env_var: str = ""):
        self._lock = threading.Lock()
        self._bad_until: Dict[str, float] = {}
        self._ok_rank: Dict[str, float] = {}
        self._default = [b.rstrip("/") for b in bases if b]
        self._env_var = env_var

    def env_bases(self) -> List[str]:
        """env 显式指定的端点优先级最高（部署时可换成内网镜像）。"""
        raw = os.getenv(self._env_var, "") if self._env_var else ""
        if not raw:
            # HF 生态的通用变量名，尊重它：HF_ENDPOINT 常被用来指镜像
            raw = os.getenv("HF_ENDPOINT", "")
        return [b.strip().rstrip("/") for b in raw.replace(" ", ",").split(",") if b.strip()]

    def ordered(self) -> List[str]:
        """env 指定端点 → 存活端点（近期成功优先）→ 冷却中的端点（兜底再试）。"""
        with self._lock:
            now = time.monotonic()
            live = [b for b in self._default if self._bad_until.get(b, 0.0) <= now]
            down = [b for b in self._default if b not in live]
            live.sort(key=lambda b: self._ok_rank.get(b, 1.0))
            picked = self.env_bases() + live + down
        out, seen = [], set()
        for b in picked:
            if b and b not in seen:
                seen.add(b)
                out.append(b)
        return out

    def note_ok(self, base: str):
        with self._lock:
            b = base.rstrip("/")
            self._ok_rank[b] = 0.0
            self._bad_until.pop(b, None)

    def note_bad(self, base: str, reason: str):
        with self._lock:
            b = base.rstrip("/")
            self._bad_until[b] = time.monotonic() + BAD_ENDPOINT_COOLDOWN_S
            self._ok_rank[b] = 9.0
        logger.warning(f"[online] 端点不可用，冷却 {BAD_ENDPOINT_COOLDOWN_S:.0f}s: {b} ({reason})")

    def status(self) -> List[dict]:
        """只读诊断：每个端点当前状态（含真实剩余秒数）。"""
        with self._lock:
            now = time.monotonic()
            out = []
            for b in self._default:
                until = self._bad_until.get(b, 0.0)
                cooling = until > now
                out.append({
                    "endpoint": b,
                    "state": "cooling_down" if cooling else "candidate",
                    "recently_failed": cooling,
                    "retry_in_s": round(until - now, 1) if cooling else 0,
                })
            return out


# ---------------------------------------------------------------- GET / 下载
def get_json(client, url: str, params: dict = None, deadline_s: float = None,
             pool: Optional[EndpointPool] = None, path: str = "") -> Tuple[Any, dict]:
    """带预算的 GET+JSON。

    给了 `pool` + `path` 时按端点顺序展开成多个候选 URL，各试 **1 次**
    （失败即换端点，不在同一端点上退避重试 —— 那正是 93 秒卡死的来源）。

    Returns: (data 或 None, record)
      record = {ok, url, http_code, elapsed_ms, reason, error, attempts}
    """
    record = {"ok": False, "url": url, "http_code": 0, "elapsed_ms": 0.0,
              "error": "", "reason": "", "attempts": []}
    if client is None:
        record.update(reason="no_http_client", error="httpx 未安装，无法访问在线音色源")
        return None, record

    deadline = time.monotonic() + (deadline_s if deadline_s is not None else SEARCH_DEADLINE_S)
    candidates = [f"{b}{path}" for b in pool.ordered()] if (pool and path) else [url]

    for candidate in candidates:
        left = deadline - time.monotonic()
        if left <= 0:
            record["reason"] = record["reason"] or "deadline_exceeded"
            record["error"] = record["error"] or "在线源整体超时预算已用尽"
            break
        t0 = time.monotonic()
        try:
            resp = client.get(candidate, params=params,
                              timeout=request_timeout(left))
            elapsed = (time.monotonic() - t0) * 1000.0
            record["elapsed_ms"] = round(record["elapsed_ms"] + elapsed, 1)
            record["attempts"].append({"url": candidate, "http_code": resp.status_code,
                                        "elapsed_ms": round(elapsed, 1)})
            resp.raise_for_status()
            data = resp.json()
            failed = [a for a in record["attempts"][:-1] if a.get("error")]
            record.update(ok=True, url=candidate, http_code=resp.status_code,
                          reason="failover" if failed else "", error="",
                          failed_attempts=failed)
            if pool is not None:
                pool.note_ok(base_of(candidate))
            return data, record
        except Exception as e:  # noqa: BLE001 - 必须兜住所有网络异常才能给出结构化结果
            elapsed = (time.monotonic() - t0) * 1000.0
            reason, text = classify_error(e)
            code = getattr(getattr(e, "response", None), "status_code", 0) or 0
            record["elapsed_ms"] = round(record["elapsed_ms"] + elapsed, 1)
            record["attempts"].append({"url": candidate, "http_code": code,
                                        "elapsed_ms": round(elapsed, 1), "error": text[:300]})
            record["reason"], record["error"] = reason, text[:400]
            # 4xx = 端点活着、只是这个资源/参数不行：换端点没意义，直接返回
            if 400 <= code < 500:
                record["http_code"] = code
                return None, record
            if pool is not None and reason in FAILOVER_REASONS:
                pool.note_bad(base_of(candidate), f"{reason}: {text[:160]}")

    return None, record


def download_file(client, url: str, dest: Path, pool: Optional[EndpointPool] = None,
                  path: str = "", max_bytes: int = None,
                  deadline_s: float = None) -> Tuple[bool, dict]:
    """流式下载到 dest（.part 落地后原子改名），体积与时间双上限。"""
    cap = max_bytes or MAX_DOWNLOAD_BYTES
    record = {"ok": False, "url": url, "bytes": 0, "elapsed_ms": 0.0, "http_code": 0,
              "reason": "", "error": "", "attempts": [], "max_bytes": cap}
    if client is None:
        record.update(reason="no_http_client", error="httpx 未安装，无法下载")
        return False, record

    deadline = time.monotonic() + (deadline_s if deadline_s is not None else DOWNLOAD_DEADLINE_S)
    candidates = [f"{b}{path}" for b in pool.ordered()] if (pool and path) else [url]
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    t0 = time.monotonic()

    for candidate in candidates:
        if time.monotonic() >= deadline:
            record["reason"] = record["reason"] or "deadline_exceeded"
            break
        try:
            with client.stream("GET", candidate,
                               timeout=request_timeout(max(1.0, deadline - time.monotonic()))) as resp:
                record["attempts"].append({"url": candidate, "http_code": resp.status_code})
                record["http_code"] = resp.status_code
                if resp.status_code >= 400:
                    err = httpx.HTTPStatusError(f"HTTP {resp.status_code}",
                                                request=resp.request, response=resp)
                    reason, text = classify_error(err)
                    record.update(reason=reason, error=text[:300])
                    if 400 <= resp.status_code < 500:
                        break
                    if pool is not None and reason in FAILOVER_REASONS:
                        pool.note_bad(base_of(candidate), f"{reason}: {text[:160]}")
                    continue
                total, truncated = 0, False
                with open(tmp, "wb") as fh:
                    for chunk in resp.iter_bytes():
                        total += len(chunk)
                        if total > cap:
                            truncated = True
                            break
                        fh.write(chunk)
                if truncated:
                    record.update(reason="too_large",
                                  error=f"文件超过上限 {cap} 字节，已中止", bytes=total)
                    break
                record.update(ok=True, url=candidate, bytes=total)
                os.replace(tmp, dest)
                record["elapsed_ms"] = round((time.monotonic() - t0) * 1000.0, 1)
                if pool is not None:
                    pool.note_ok(base_of(candidate))
                return True, record
        except Exception as e:  # noqa: BLE001
            reason, text = classify_error(e)
            record.update(reason=reason, error=text[:300])
            if pool is not None and reason in FAILOVER_REASONS:
                pool.note_bad(base_of(candidate), f"{reason}: {text[:160]}")

    record["elapsed_ms"] = round((time.monotonic() - t0) * 1000.0, 1)
    try:
        if tmp.exists():
            tmp.unlink()
    except OSError:
        pass
    return False, record


# ---------------------------------------------------------------- 音频探针 / 归一化
MAGIC_HINTS = {
    "RIFF": "wav", "FLaC": "flac", "OggS": "ogg", "fLaC": "flac",
}


def probe_audio(path: Path) -> dict:
    """读真实音频属性：magic / 字节数 / 采样率 / 声道 / 时长 / 容器。"""
    out = {"path": str(path), "bytes": 0, "magic": "", "magic_hint": "",
           "sample_rate": 0, "channels": 0, "duration_s": 0.0, "format": ""}
    path = Path(path)
    if not path.exists():
        return out
    out["bytes"] = path.stat().st_size
    with open(path, "rb") as fh:
        head = fh.read(16)
    out["magic"] = head[:4].hex()
    try:
        tag = head[:4].decode("latin-1")
    except Exception:
        tag = ""
    if tag == "ID3\x03" or (head[0] == 0xFF and (head[1] & 0xE0) == 0xE0):
        tag = "mp3"
    out["magic_hint"] = MAGIC_HINTS.get(tag, tag)
    try:
        import soundfile as sf
        info = sf.info(str(path))
        out.update(sample_rate=int(info.samplerate), channels=int(info.channels),
                   duration_s=round(float(info.duration), 3), format=str(info.format))
    except Exception as e:  # noqa: BLE001 - 探针失败不该拖垮下载
        logger.debug(f"[online] soundfile 读取 {path} 失败: {e}")
    return out


def _to_mono(audio):
    import numpy as np
    arr = np.asarray(audio, dtype=np.float32)
    return arr.mean(axis=1).astype(np.float32) if arr.ndim > 1 else arr


def normalize_to_wav(src: Path, dst: Path, target_rate: int = _WAV_TARGET_RATE,
                     max_seconds: float = None) -> dict:
    """把任意下载到的容器归一化成 48k 单声道 PCM16 wav（与试听缓存同规格）。

    `max_seconds` 给定时只保留前 N 秒（在线样本可能几十分钟，试听用不了那么多）。
    失败抛 RuntimeError，由调用方转成结构化错误并清理部分文件。
    """
    import numpy as np
    src, dst = Path(src), Path(dst)

    try:
        import soundfile as sf
    except ImportError as e:  # pragma: no cover
        raise RuntimeError(f"soundfile 不可用，无法解码下载音频: {e}")

    audio, sr = None, 0
    try:
        audio, sr = sf.read(str(src), dtype="float32")
    except Exception:
        audio = None

    if audio is None:
        # mp3/m4a 之类 soundfile 读不了的容器：ffmpeg 转一道
        from modules.tts_engine.ffmpeg_utils import resolve_ffmpeg
        import subprocess
        ffmpeg = resolve_ffmpeg()
        if not ffmpeg:
            raise RuntimeError("soundfile 解码失败且 ffmpeg 不可用，无法处理该音频格式")
        tmp_raw = dst.with_name(dst.stem + ".raw.wav")
        tmp_raw.parent.mkdir(parents=True, exist_ok=True)
        # encoding 必须钉死：ffmpeg 会把手上的路径原样回写进 stderr，中文文件名在
        # Windows 默认 GBK 下解不开就会抛 UnicodeDecodeError —— 那一下盖掉的是下面
        # 那句真正的"ffmpeg 解码失败: <原因>"（PY-117 同族：错误原因没了）。
        r = subprocess.run(
            [ffmpeg, "-y", "-v", "error", "-i", str(src),
             "-ac", "1", "-ar", str(target_rate), "-c:a", "pcm_s16le", str(tmp_raw)],
            capture_output=True, text=True, timeout=120,
            encoding="utf-8", errors="replace")
        if r.returncode != 0 or not tmp_raw.exists():
            try:
                tmp_raw.unlink(missing_ok=True)
            except OSError:
                pass
            raise RuntimeError(f"ffmpeg 解码失败: {(r.stderr or '').strip()[:240]}")
        audio, sr = sf.read(str(tmp_raw), dtype="float32")
        try:
            tmp_raw.unlink()
        except OSError:
            pass

    arr = _to_mono(audio)
    if arr.size == 0:
        raise RuntimeError("解码后为空音频")
    sr = int(sr or target_rate)
    cropped = False
    if max_seconds and arr.size > int(max_seconds * sr):
        arr = arr[: int(max_seconds * sr)]
        cropped = True
    if sr != target_rate:
        try:
            import librosa
            arr = librosa.resample(arr, orig_sr=sr, target_sr=target_rate)
        except Exception:
            n = int(arr.shape[0] * target_rate / sr)
            idx = np.linspace(0, arr.shape[0] - 1, n)
            arr = np.interp(idx, np.arange(arr.shape[0]), arr).astype(np.float32)
        sr = target_rate
    peak = float(np.max(np.abs(arr)))
    if peak > 0.99:
        arr = (arr * (0.98 / peak)).astype(np.float32)
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".tmp.wav")
    sf.write(str(tmp), arr, sr, subtype="PCM_16")
    os.replace(str(tmp), str(dst))
    return {"sample_rate": sr, "channels": 1, "frames": int(arr.size),
            "duration_s": round(float(arr.size) / float(sr), 3),
            "peak": round(peak, 4), "cropped": bool(cropped)}


def pick_audio_paths(entries: List[dict], prefer: Tuple[str, ...] = (".wav", ".flac", ".mp3"),
                     cap: int = 12) -> List[dict]:
    """从文件清单里挑出最可能是"可直接试听的人声样本"的音频文件。

    小文件优先（试听样本不需要 100MB），按扩展名偏好排序。
    返回 [{path, size, url_path}]，url_path 由调用方拼 resolve/raw 前缀。
    """
    scored = []
    for e in entries or []:
        path = str((e or {}).get("path") or (e or {}).get("rfilename") or "").strip("/")
        if not path:
            continue
        low = path.lower()
        if not low.endswith(AUDIO_SUFFIXES):
            continue
        ext = "." + low.rsplit(".", 1)[-1]
        if ext not in prefer:
            continue
        size = (e.get("size") or 0) or ((e.get("lfs") or {}).get("size") or 0)
        rank = prefer.index(ext) if ext in prefer else 9
        scored.append({"path": path, "size": int(size or 0), "ext": ext,
                       "_rank": rank, "_size_key": int(size) if size else 5 * 1024 * 1024})
    scored.sort(key=lambda d: (d["_rank"], d["_size_key"]))
    out = []
    for d in scored[:cap]:
        out.append({"path": d["path"], "size": d["size"], "ext": d["ext"]})
    return out
