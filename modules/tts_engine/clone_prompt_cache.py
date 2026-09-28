"""
零样本克隆"参考音频编码产物"的两级缓存（P0-2）

问题：VoxCPM2 这类零样本克隆引擎每次合成都要把参考音频重新过一遍音频 VAE
（``modules/tts_engine/voxcpm/model/voxcpm2.py`` 的
``build_prompt_cache() -> _encode_wav(reference_wav_path)``，产出一张
``(T, patch_size, latent_dim)`` 的 latent patch）。做整本书 × 多角色时，同一份参考
音频会被重复编码成百上千次 —— 这是一笔完全可省的固定开销。

方案（两级，容量都很小，刻意不做大）：
    L1 内存 LRU   ：默认 8 条。进程内热点，命中即零成本。
    L2 磁盘 .npz  ：默认 32 条。进程重启 / 引擎重载后仍然命中（"每次会话首句重编码"
                    这笔钱就在这层省掉）。
    编码产物本身用 ``dict[str, np.ndarray]`` 表示 —— 张量在 CPU 上（上游 ``_encode_wav``
    就 ``.cpu()`` 落地），所以 numpy 就够，缓存模块不需要依赖 torch。

缓存键 = 命名空间 + 参考音频绝对路径 + **mtime_ns** + **size** + 额外判别位。
mtime/size 进键 ⇒ 参考文件被替换（重新录音、被别的音色覆盖同名文件、爬虫刷新素材）
后键就变了，**永不命中旧编码**；留下的旧条目由 LRU 老化掉，不需要额外清理任务。

纪律（照上游踩坑后立的规矩）：
    * **best-effort**：缓存层任何异常（磁盘不可写、npz 损坏、版本不符、采样点被别的
      进程占着）都退化成"未命中 → 重新编码"，绝不影响输出，也绝不向上抛。它只是
      延迟优化，不是正确性依赖。
    * **一把锁**：一个 ``threading.Lock`` 护住 L1/L2 的索引；``builder()``（真正的编码，
      慢活）**在锁外**跑，避免不同音色的首句互相排队。同键并发重复编码是可接受的
      （幂等），比串行等待便宜。
    * 关掉磁盘层：环境变量 ``PEIYINYUAN_CLONE_PROMPT_CACHE=0``（只留内存层）。
    * 换目录：``PEIYINYUAN_CLONE_PROMPT_CACHE_DIR``。

清洁室声明：机制参考 VoiceStudio（AGPL-3.0）§5.3 的两级缓存设计（LRU + 磁盘 + mtime
进键），代码为本仓库独立实现，未复制其任何源码文本或注释。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)

# ---------- 容量上限（刻意小：一条 latent patch 序列约几百 KB~几 MB） ----------
MEM_MAX_ENTRIES: int = 8
DISK_MAX_ENTRIES: int = 32

FORMAT_TAG = "pyy-clone-prompt-v1"
DISK_SUBDIR = ("cache", "clone_prompt")
ENV_ENABLED = "PEIYINYUAN_CLONE_PROMPT_CACHE"
ENV_DIR = "PEIYINYUAN_CLONE_PROMPT_CACHE_DIR"
_META_KEY = "__meta__"
_FALSY = {"0", "false", "no", "off"}


def default_disk_dir() -> Path:
    """默认磁盘层目录 = ``data/cache/clone_prompt``（不可用时退到临时目录）。"""
    try:
        from backend.config import Config
        root = Path(Config.DATA_DIR)
    except Exception:                                  # pragma: no cover - 独立脚本环境
        root = Path(os.environ.get("TEMP", "/tmp")) / "peiyinyuan"
    return root.joinpath(*DISK_SUBDIR)


class ClonePromptCache:
    """内存 LRU + 磁盘 npz 的两级缓存（线程安全）。

    缓存的"值"固定是 ``dict[str, np.ndarray]`` —— 即参考音频的编码产物（latent /
    speaker embedding 等昂贵中间量）。上层适配器负责把它变回引擎要的张量类型。
    """

    def __init__(self,
                 mem_max: int = MEM_MAX_ENTRIES,
                 disk_max: int = DISK_MAX_ENTRIES,
                 disk_dir: Optional[os.PathLike] = None,
                 use_disk: Optional[bool] = None):
        self._lock = threading.Lock()
        self._mem_max = max(1, int(mem_max))
        self._disk_max = max(0, int(disk_max))
        self._mem: "OrderedDict[str, Dict[str, np.ndarray]]" = OrderedDict()
        self._disk_index: "OrderedDict[str, Path]" = OrderedDict()
        self._disk_scanned = False
        self._dir = Path(disk_dir) if disk_dir is not None else None
        env = os.environ.get(ENV_ENABLED)
        self._use_disk = (env is None or env.strip().lower() not in _FALSY) \
            if use_disk is None else bool(use_disk)
        if self._dir is None and os.environ.get(ENV_DIR):
            self._dir = Path(os.environ[ENV_DIR])
        self._hits_mem = 0
        self._hits_disk = 0
        self._misses = 0
        self._stores = 0
        self._failures = 0

    # ---------------- 目录 ----------------
    @property
    def disk_dir(self) -> Path:
        if self._dir is None:
            self._dir = default_disk_dir()
        return self._dir

    # ---------------- 键 ----------------
    @staticmethod
    def ref_fingerprint(ref_path: os.PathLike) -> Optional[Tuple[str, int, int]]:
        """参考音频的 (绝对路径, mtime_ns, size)；取不到（文件不存在/被删）返回 None。"""
        try:
            st = os.stat(ref_path)
        except OSError:
            return None
        if not os.path.isfile(str(ref_path)):
            return None
        return (os.path.abspath(str(ref_path)), int(st.st_mtime_ns), int(st.st_size))

    @staticmethod
    def namespace_id(namespace: str) -> str:
        """命名空间前缀（同一份缓存目录里区分不同引擎/模型权重）。"""
        return hashlib.sha1(str(namespace).encode("utf-8")).hexdigest()[:10]

    def build_key(self,
                  namespace: str,
                  ref_path: os.PathLike,
                  extra: Optional[Iterable[Any]] = None) -> Optional[str]:
        """算缓存键；参考音频不可 stat（不存在）时返回 None —— 调用方据此直接内联编码。"""
        fp = self.ref_fingerprint(ref_path)
        if fp is None:
            return None
        nsid = self.namespace_id(namespace)
        tail = "|".join("" if e is None else str(e) for e in (extra or []))
        raw = f"{FORMAT_TAG}|{nsid}|{fp[0]}|{fp[1]}|{fp[2]}|{tail}"
        digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:24]
        return f"{nsid}_{digest}"

    # ---------------- 读写 ----------------
    def get(self, key: Optional[str]) -> Optional[Dict[str, np.ndarray]]:
        """查缓存：内存命中 > 磁盘命中（并回填内存）。未命中返回 None。"""
        if not key:
            return None
        with self._lock:
            entry = self._mem.get(key)
            if entry is not None:
                self._mem.move_to_end(key)
                self._hits_mem += 1
                return entry
            path = self._disk_path_for(key) if self._use_disk else None
            if path is not None and path.exists():
                loaded = self._read_npz(path)
                if loaded is not None:
                    self._disk_index[key] = path       # 别的进程写的/索引里还没有 -> 补登记
                    self._disk_index.move_to_end(key)
                    self._mem[key] = loaded
                    self._mem.move_to_end(key)
                    self._evict_mem_locked()
                    self._hits_disk += 1
                    return loaded
                # 坏文件：摘掉索引，当未命中处理（下次 store 会重写）
                self._disk_index.pop(key, None)
                self._failures += 1
            self._misses += 1
            return None

    def put(self, key: Optional[str], entry: Dict[str, np.ndarray]) -> None:
        """写缓存：内存层必写，磁盘层 best-effort。"""
        if not key or not isinstance(entry, dict) or not entry:
            return
        if not all(isinstance(v, np.ndarray) for v in entry.values()):
            logger.debug("[clone_cache] 跳过非 ndarray 载荷的条目 %s", key)
            return
        with self._lock:
            self._mem[key] = entry
            self._mem.move_to_end(key)
            self._evict_mem_locked()
            if self._use_disk and self._disk_max > 0:
                try:
                    self._ensure_disk_scanned_locked()
                    self._write_npz_locked(key, entry)
                    self._stores += 1
                except Exception as exc:
                    self._failures += 1
                    logger.debug("[clone_cache] 磁盘写入失败（忽略，仅内存缓存）: %s", exc)

    def get_or_build(self,
                     key: Optional[str],
                     builder: Callable[[], Optional[Dict[str, np.ndarray]]]) -> Dict[str, np.ndarray]:
        """命中即返回；未命中则调用 ``builder()`` 现算（**锁外**），算完回填。

        ``builder`` 返回 ``None`` 时不入缓存、抛 ``KeyError``？—— 不：返回 None 视为
        "这一条不值得缓存"，直接把 None 交回调用方，由调用方走内联路径。
        """
        cached = self.get(key)
        if cached is not None:
            return cached
        built = builder() if callable(builder) else None
        if built is not None:
            self.put(key, built)
        return built

    def clear(self, namespace: Optional[str] = None, purge_disk: bool = True) -> int:
        """清缓存；给 ``namespace`` 时只清该引擎/模型的那批（切引擎、删音色时用）。

        默认连磁盘文件一起删：``get()`` 是**按键直接寻址**磁盘文件的，只丢索引不删文件
        会让"已清掉"的旧编码继续被命中。只想丢内存层时传 ``purge_disk=False``。
        """
        nsid = self.namespace_id(namespace) if namespace else None
        removed = 0
        with self._lock:
            keys = {k for k in self._mem if nsid is None or k.startswith(f"{nsid}_")}
            keys |= {k for k in self._disk_index if nsid is None or k.startswith(f"{nsid}_")}
            if not keys and purge_disk:
                # 索引可能还没扫过盘（进程刚起就删音色）：扫一次保证文件真被删
                self._ensure_disk_scanned_locked()
                keys = {k for k in self._disk_index if nsid is None or k.startswith(f"{nsid}_")}
            for k in keys:
                self._mem.pop(k, None)
                path = self._disk_index.pop(k, None)
                removed += 1
                if purge_disk and path is not None:
                    try:
                        path.unlink()
                    except OSError:
                        pass
                    continue
                if purge_disk:
                    try:
                        self._disk_path_for(k).unlink()
                    except OSError:
                        pass
        logger.info("[clone_cache] 已清缓存 %d 条 (namespace=%s, purge_disk=%s)",
                    removed, namespace or "*", purge_disk)
        return removed

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "mem_entries": len(self._mem),
                "mem_max": self._mem_max,
                "disk_entries": len(self._disk_index),
                "disk_max": self._disk_max,
                "disk_enabled": bool(self._use_disk and self._disk_max > 0),
                "disk_dir": str(self.disk_dir),
                "hits_mem": self._hits_mem,
                "hits_disk": self._hits_disk,
                "misses": self._misses,
                "stores": self._stores,
                "failures": self._failures,
            }

    # ---------------- 内部：LRU / 磁盘 ----------------
    def _evict_mem_locked(self) -> None:
        while len(self._mem) > self._mem_max:
            self._mem.popitem(last=False)

    def _disk_path_for(self, key: str) -> Path:
        return self.disk_dir / f"{key}.npz"

    def _ensure_disk_scanned_locked(self) -> None:
        """首次用时扫盘建索引（按 mtime 排序 = LRU 视角的"最近用过"顺序）。"""
        if self._disk_scanned:
            return
        self._disk_scanned = True
        try:
            self.disk_dir.mkdir(parents=True, exist_ok=True)
            files: List[Tuple[float, Path]] = [
                (p.stat().st_mtime, p) for p in self.disk_dir.glob("*.npz")
            ]
        except OSError as exc:
            logger.debug("[clone_cache] 磁盘层不可用（只留内存层）: %s", exc)
            self._use_disk = False
            return
        for _, path in sorted(files, key=lambda t: t[0]):
            self._disk_index[path.name[: -len(".npz")]] = path
        self._prune_disk_locked()

    def _write_npz_locked(self, key: str, entry: Dict[str, np.ndarray]) -> None:
        self._ensure_disk_scanned_locked()
        if not self._use_disk:
            return
        target = self._disk_path_for(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        meta = {"format": FORMAT_TAG, "shapes": {k: list(v.shape) for k, v in entry.items()},
                "dtypes": {k: str(v.dtype) for k, v in entry.items()}}
        # 先写同名 .npz 临时文件再 os.replace：np.savez 只给"不带 .npz 后缀"的文件名补
        # 后缀，所以临时名必须自己带上 .npz，否则落盘位置和我们 replace 的位置不一致。
        tmp = target.with_name(f"{target.stem}.{os.getpid()}.{threading.get_ident()}.npz")
        payload = dict(entry)
        payload[_META_KEY] = np.array(json.dumps(meta), dtype=np.str_)
        try:
            np.savez(tmp, allow_pickle=False, **payload)
            os.replace(tmp, target)
        finally:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass
        self._disk_index[key] = target
        self._disk_index.move_to_end(key)
        self._prune_disk_locked()

    def _read_npz(self, path: Path) -> Optional[Dict[str, np.ndarray]]:
        try:
            with np.load(path, allow_pickle=False) as data:
                meta_raw = data[_META_KEY] if _META_KEY in data.files else None
                entry = {k: data[k] for k in data.files if k != _META_KEY}
            if meta_raw is not None:
                meta = json.loads(str(meta_raw))
                if meta.get("format") != FORMAT_TAG:
                    return None
                for k, shape in (meta.get("shapes") or {}).items():
                    if k not in entry or list(entry[k].shape) != list(shape):
                        return None
            return entry or None
        except Exception:
            return None

    def _prune_disk_locked(self) -> None:
        while len(self._disk_index) > self._disk_max:
            _, oldest = self._disk_index.popitem(last=False)
            try:
                oldest.unlink()
            except OSError:
                pass


# 全局单例（与 voice_store / engine_registry 的用法一致）
clone_prompt_cache = ClonePromptCache()

__all__ = ["ClonePromptCache", "clone_prompt_cache",
           "MEM_MAX_ENTRIES", "DISK_MAX_ENTRIES", "FORMAT_TAG", "default_disk_dir"]
