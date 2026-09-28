"""
BGM / 音效素材库（多人有声书批次 A）

设计依据: specs/2026-09-20-multi-cast-audiobook-design.md 4.2

- 库目录: data/bgm/（音乐）+ data/bgm/sfx/（音效），索引 data/bgm/meta.json
- meta.json 字段: id / 名称 / 情绪标签 / 时长 / 是否可循环 / 固有响度
- 程序自带占位素材（numpy 合成，非真实录音，已在 name/description 标注 "占位"）:
    3 段正弦和弦垫 BGM + 2 个章节提示音效
- 用户上传: 有 ffmpeg 时转 wav 48k 立体声入库；无 ffmpeg 时用 soundfile +
  AudioPostProcessor.resample 兜底（不静默降质，meta 里记 source=ffmpeg|soundfile）
- 智能体4 的 data/resource.db 音效表只读打通（表缺失时返回空，不影响本库）
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple, Union

import numpy as np

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
META_VERSION = 1
PLACEHOLDER_TAG = "占位"
META_LIST_BUCKETS = ("bgm", "sfx")


def _default_data_root() -> Path:
    """data 根的唯一入口 = `Config.DATA_DIR`（可被 `PEIYINYUAN_DATA_DIR` 覆盖）。

    这里过去硬写 `PROJECT_ROOT / "data"`，等于给 PY-366 留了第二条不受开关管制的
    检出树写入路径 —— `BgmStore()` 的默认根正是 API 层（`backend/api/cast.py`）
    每次列素材库都在用的那只。取不到 Config 时（脱离仓库单独引用本模块）退到
    `<repo>/data`，但先仍尊重同一个环境变量，别让开关在这里漏掉。
    """
    try:
        from backend.config import Config

        return Path(Config.DATA_DIR)
    except Exception as exc:  # pragma: no cover - 只有脱离仓库运行才会走到
        logger.warning("[bgm] 取不到 Config.DATA_DIR，改用 env/仓库默认 data 根: %s", exc)
        return Path(os.getenv("PEIYINYUAN_DATA_DIR") or (PROJECT_ROOT / "data"))


DEFAULT_ROOT = _default_data_root()

LIBRARY_SR = 48000                     # 入库统一采样率（spec 4.2）


# --------------------------------------------------------------- ffmpeg (WF-15)
def ffmpeg_probe() -> Dict:
    """ffmpeg 单点探测（委派 modules/tts_engine/ffmpeg_utils）。

    旧实现 `shutil.which("ffmpeg")` 只看 PATH —— 本仓库的 ffmpeg 由
    imageio-ffmpeg 自带二进制（或 Config.FFMPEG_PATH）提供时，which 返回 None，
    于是 /api/bgm 报 ffmpeg_available:false（假阴性），上传转码也白走 soundfile
    兜底。P8/P9、M4BBuilder 早已统一走 ffmpeg_utils.probe()，此处补齐。
    """
    from modules.tts_engine.ffmpeg_utils import probe

    return probe()


def ffmpeg_executable() -> Optional[str]:
    """可用的 ffmpeg 可执行文件路径；无则 None。"""
    from modules.tts_engine.ffmpeg_utils import resolve_ffmpeg

    return resolve_ffmpeg()


# 内置占位 BGM（正弦和弦垫）：id 稳定，供 mix_settings.bgm.ref 直接引用
BUILTIN_BGM: List[Dict] = [
    {
        "id": "bgm_warm_pad",
        "name": f"{PLACEHOLDER_TAG}BGM-温暖弦乐垫",
        "description": "numpy 合成的正弦和弦垫，仅用于混音链路联调，非真实录音素材",
        "moods": ["温暖", "平静", "抒情"],
        "seconds": 24.0,
        "freqs": [130.81, 196.00, 261.63, 329.63, 392.00],
        "tremolo_hz": 0.18,
        "level_db": -21.0,
        "loopable": True,
    },
    {
        "id": "bgm_suspense_pad",
        "name": f"{PLACEHOLDER_TAG}BGM-悬疑低音垫",
        "description": "numpy 合成的低音和弦垫（小三和弦 + 慢速起伏），占位素材",
        "moods": ["悬疑", "紧张", "阴沉"],
        "seconds": 30.0,
        "freqs": [73.42, 110.00, 130.81, 174.61],
        "tremolo_hz": 0.09,
        "level_db": -23.0,
        "loopable": True,
    },
    {
        "id": "bgm_daylight_pad",
        "name": f"{PLACEHOLDER_TAG}BGM-明亮日间垫",
        "description": "numpy 合成的明亮和弦垫，用于日常/过渡段落，占位素材",
        "moods": ["明亮", "日常", "轻快"],
        "seconds": 18.0,
        "freqs": [196.00, 246.94, 293.66, 392.00, 493.88],
        "tremolo_hz": 0.32,
        "level_db": -20.0,
        "loopable": True,
    },
]

# 内置占位音效（章节提示音）
BUILTIN_SFX: List[Dict] = [
    {
        "id": "sfx_chapter_start",
        "name": f"{PLACEHOLDER_TAG}音效-章节开始提示",
        "description": "两音上行提示音（numpy 合成，指数包络），占位素材",
        "notes": [(880.0, 0.16), (1318.5, 0.42)],
        "level_db": -16.0,
    },
    {
        "id": "sfx_chapter_end",
        "name": f"{PLACEHOLDER_TAG}音效-章节结束提示",
        "description": "两音下行收尾提示音（numpy 合成，指数包络），占位素材",
        "notes": [(1046.5, 0.18), (659.25, 0.5)],
        "level_db": -16.0,
    },
]


# ---------------------------------------------------------------- numpy 合成
def _chord_pad(freqs: Sequence[float], seconds: float, sr: int = LIBRARY_SR,
               tremolo_hz: float = 0.15, level_db: float = -21.0) -> np.ndarray:
    """正弦和弦垫（立体声）：detune 双振荡 + 慢速颤音 + 首尾淡入淡出。全向量化。"""
    n = int(round(seconds * sr))
    t = np.arange(n, dtype=np.float32) / float(sr)
    stack = np.zeros(n, dtype=np.float32)
    for i, f in enumerate(freqs):
        detune = 1.0 + 0.0009 * (1 if i % 2 else -1)
        osc = np.sin(2 * np.pi * f * t) + 0.55 * np.sin(2 * np.pi * f * detune * t)
        osc += 0.18 * np.sin(2 * np.pi * 2 * f * t)          # 二次谐波，避免过薄
        stack += osc.astype(np.float32) / (len(freqs) or 1)

    lfo = 1.0 + 0.16 * np.sin(2 * np.pi * tremolo_hz * t).astype(np.float32)
    env = _fade_envelope(n, sr, attack_s=1.6, release_s=2.2)
    mono = (stack * lfo * env).astype(np.float32)

    # 立体声去相关：右声道延迟若干样本，制造宽度（仍是确定性生成，可复现）
    delay = max(1, int(0.00035 * sr))
    right = np.concatenate([np.zeros(delay, dtype=np.float32), mono[:-delay]])
    stereo = np.stack([mono, (right * 0.97).astype(np.float32)], axis=1)
    return _scale_to_db(stereo, level_db)


def _chime(notes: Sequence[Tuple[float, float]], sr: int = LIBRARY_SR,
           level_db: float = -16.0) -> np.ndarray:
    """提示音：若干带指数衰减的音（立体声复制），全向量化。"""
    pieces = []
    for freq, dur in notes:
        n = int(round(dur * sr))
        t = np.arange(n, dtype=np.float32) / float(sr)
        decay = np.exp(-3.2 * t).astype(np.float32)
        tone = (np.sin(2 * np.pi * freq * t) + 0.35 * np.sin(2 * np.pi * 2 * freq * t))
        pieces.append((tone * decay).astype(np.float32))
    mono = np.concatenate(pieces) if pieces else np.zeros(1, dtype=np.float32)
    fade = _fade_envelope(len(mono), sr, attack_s=0.004, release_s=0.06)
    mono = (mono * fade).astype(np.float32)
    stereo = np.stack([mono, mono], axis=1)
    return _scale_to_db(stereo, level_db)


def _fade_envelope(n: int, sr: int, attack_s: float, release_s: float) -> np.ndarray:
    attack = max(1, int(attack_s * sr))
    release = max(1, int(release_s * sr))
    if attack + release >= n:                      # 极短素材：整体当淡入淡出
        half = max(1, n // 2)
        env = np.ones(n, dtype=np.float32)
        env[:half] = np.linspace(0.0, 1.0, half, dtype=np.float32)
        env[-half:] = np.linspace(1.0, 0.0, half, dtype=np.float32)
        return env
    env = np.ones(n, dtype=np.float32)
    env[:attack] = np.linspace(0.0, 1.0, attack, dtype=np.float32)
    env[-release:] = np.linspace(1.0, 0.0, release, dtype=np.float32)
    return env


def _scale_to_db(data: np.ndarray, target_db: float) -> np.ndarray:
    rms = float(np.sqrt(np.mean(data.astype(np.float64) ** 2))) if data.size else 0.0
    if rms < 1e-9:
        return data.astype(np.float32)
    return (data * (10 ** (target_db / 20.0) / rms)).astype(np.float32)


def _measure(data: np.ndarray, sr: int) -> Dict:
    frames = int(data.shape[0]) if data.ndim > 1 else int(len(data))
    flat = data.reshape(-1) if data.ndim > 1 else data
    rms = float(np.sqrt(np.mean(flat.astype(np.float64) ** 2))) if flat.size else 0.0
    peak = float(np.max(np.abs(flat))) if flat.size else 0.0
    return {
        "duration_s": round(frames / float(sr), 3) if sr else 0.0,
        "loudness_db": round(20 * np.log10(rms + 1e-12), 2),   # 固有响度（dBFS RMS）
        "peak_db": round(20 * np.log10(peak + 1e-12), 2),
        "sample_rate": int(sr),
        "channels": int(data.shape[1]) if data.ndim > 1 else 1,
    }


# ---------------------------------------------------------------- 文件 IO
def _write_wav(path: Path, data: np.ndarray, sr: int) -> None:
    import soundfile as sf
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = data if data.ndim == 2 else data[:, None]
    peak = float(np.max(np.abs(payload))) or 1.0
    if peak > 0.985:                                    # 写盘安全余量
        payload = payload * (0.985 / peak)
    sf.write(str(path), payload, sr, subtype="FLOAT")


def _read_wav(path: Path) -> Tuple[np.ndarray, int]:
    import soundfile as sf
    data, sr = sf.read(str(path), dtype="float32", always_2d=True)
    return data, int(sr)


def _mono(data: np.ndarray) -> np.ndarray:
    return data.mean(axis=1).astype(np.float32) if data.ndim > 1 and data.shape[1] > 1 \
        else (data[:, 0].astype(np.float32) if data.ndim > 1 else data.astype(np.float32))


def _ascii_id(prefix: str, text: str) -> str:
    slug = "".join(ch if ch.isalnum() and ord(ch) < 128 else "_" for ch in text)
    slug = "_".join(part for part in slug.split("_") if part)[:40] or "item"
    digest = hashlib.md5(text.encode("utf-8")).hexdigest()[:6]
    return f"{prefix}_{slug}_{digest}"


class BgmStore:
    """data/bgm 素材库（音乐 + 音效）与 meta.json 索引。"""

    def __init__(self, root: Optional[Path] = None):
        self.root = Path(root) if root else DEFAULT_ROOT
        self.bgm_dir = self.root / "bgm"
        self.sfx_dir = self.bgm_dir / "sfx"
        self.meta_path = self.bgm_dir / "meta.json"
        self.resource_db = self.root / "resource.db"

    # ------------------------------------------------------------- meta
    def load_meta(self) -> Dict:
        if not self.meta_path.exists():
            return self._empty_meta()
        try:
            meta = json.loads(self.meta_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("[bgm] meta.json 解析失败，重建空索引: %s", exc)
            return self._empty_meta()
        if not isinstance(meta, dict):
            return self._empty_meta()
        meta.setdefault("version", META_VERSION)
        meta.setdefault("bgm", [])
        meta.setdefault("sfx", [])
        return meta

    @staticmethod
    def _empty_meta() -> Dict:
        return {"version": META_VERSION, "bgm": [], "sfx": [], "updated_at": None}

    # ---------------------------------------------------- 语义比较（PY-366）
    @classmethod
    def _canon(cls, value):
        """规范化的比较用形式：dict 按 key 排序（防止 key 顺序被误判成内容变更）。"""
        if isinstance(value, dict):
            return {k: cls._canon(v) for k, v in
                    sorted(value.items(), key=lambda kv: str(kv[0]))}
        if isinstance(value, list):
            return [cls._canon(v) for v in value]
        return value

    @classmethod
    def _normalized(cls, payload: Dict) -> Dict:
        """去掉 `updated_at` 后的规范形式；素材桶按 id 排序（重生记录会被
        "摘掉再追加"，顺序变了不代表内容变了）。"""
        out: Dict = {}
        for key, val in payload.items():
            if key == "updated_at":
                continue
            if key in META_LIST_BUCKETS and isinstance(val, list):
                out[key] = sorted((cls._canon(item) for item in val),
                                  key=lambda item: str(item.get("id", ""))
                                  if isinstance(item, dict) else str(item))
            else:
                out[key] = cls._canon(val)
        return out

    def save_meta(self, meta: Dict, force: bool = False) -> bool:
        """落盘 meta.json，返回**是否真的写了盘**。

        PY-366：`meta.json` 是受版本控制的跟踪文件。每次重生占位素材都会把
        `updated_at` 盖成"当下"并无条件写盘 ⇒ 跑一次门就脏一次，"干净工作树才能
        跑门禁"永不成立。现在落盘前做语义比较：除 `updated_at` 外逐字段与磁盘现状
        相同就**直接跳过**（既不碰 meta.json，也不留 `.json.tmp` 痕迹，且不改动
        调用方 meta 里的 `updated_at` —— 内存里也不留下"写过"的假象）。

        真数据变更（用户上传/转码 `add_audio_file`）内容必然不同，照旧写盘；
        `force=True` 是无条件重写的显式指令，同样照旧写盘。
        """
        meta["version"] = META_VERSION
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
        if not force and self.meta_path.exists():
            candidate = dict(meta)
            candidate["updated_at"] = stamp
            if self._normalized(candidate) == self._normalized(self.load_meta()):
                return False
        meta["updated_at"] = stamp
        self.meta_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.meta_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.meta_path)
        return True

    # ------------------------------------------------------------- 内置占位素材
    def ensure_builtin(self, force: bool = False) -> Dict:
        """生成/补齐自带占位 BGM 与音效；返回 {created, existing, meta_path}。"""
        meta = self.load_meta()
        created: List[str] = []
        existing: List[str] = []

        def ingest(entry: Dict, data: np.ndarray, subdir: Optional[Path]) -> bool:
            path = (subdir or self.bgm_dir) / f"{entry['id']}.wav"
            if path.exists() and not force:
                existing.append(entry["id"])
                return False
            _write_wav(path, data, LIBRARY_SR)
            stats = _measure(data, LIBRARY_SR)
            bucket = "sfx" if subdir else "bgm"
            record = dict(entry)
            record.update(stats)
            record["file"] = path.relative_to(self.bgm_dir).as_posix()
            record["placeholder"] = True
            record["source"] = "builtin_numpy"
            # `created_at` 的语义是"这条目第一次入池的时刻"：占位素材被重新生成
            # 不等于条目重生，必须沿用已有时刻。否则时间字段进了受版本控制的
            # meta.json，每跑一次回归门就漂一次 (PY-366)。
            previous = next((r for r in meta[bucket]
                             if r.get("id") == entry["id"]), None) or {}
            record["created_at"] = (previous.get("created_at")
                                    or time.strftime("%Y-%m-%dT%H:%M:%S"))
            meta[bucket] = [r for r in meta[bucket] if r.get("id") != entry["id"]] + [record]
            created.append(entry["id"])
            return True

        for spec in BUILTIN_BGM:
            data = _chord_pad(spec["freqs"], spec["seconds"], LIBRARY_SR,
                              spec["tremolo_hz"], spec["level_db"])
            ingest({k: spec[k] for k in ("id", "name", "description", "moods", "loopable")},
                   data, None)

        for spec in BUILTIN_SFX:
            data = _chime(spec["notes"], LIBRARY_SR, spec["level_db"])
            ingest({"id": spec["id"], "name": spec["name"], "description": spec["description"],
                    "moods": ["提示"], "loopable": False}, data, self.sfx_dir)

        if created or force:
            self.save_meta(meta, force=force)
        elif not self.meta_path.exists():
            self.save_meta(meta)
        return {"created": created, "existing": existing,
                "meta_path": str(self.meta_path), "bgm_dir": str(self.bgm_dir)}

    # ------------------------------------------------------------- 查询
    def list_bgm(self, mood: Optional[str] = None) -> List[Dict]:
        rows = self.load_meta()["bgm"]
        if mood:
            rows = [r for r in rows if mood in (r.get("moods") or [])]
        return sorted(rows, key=lambda r: r.get("id", ""))

    def list_sfx(self) -> List[Dict]:
        return sorted(self.load_meta()["sfx"], key=lambda r: r.get("id", ""))

    def get(self, bgm_id: str) -> Optional[Dict]:
        meta = self.load_meta()
        for bucket in ("bgm", "sfx"):
            for row in meta[bucket]:
                if row.get("id") == bgm_id:
                    return row
        return None

    def resolve(self, ref: str, kind: Optional[str] = None) -> Optional[Path]:
        """ref 可以是库内 id，也可以是绝对/相对文件路径。"""
        if not ref:
            return None
        row = self.get(ref)
        if row and row.get("file"):
            path = self.bgm_dir / row["file"]
            if not path.exists():
                path = self.sfx_dir / Path(row["file"]).name
            return path if path.exists() else None
        candidate = Path(ref)
        if not candidate.is_absolute():
            candidate = PROJECT_ROOT / ref
        return candidate if candidate.exists() else None

    def read(self, ref: str, target_sr: int = LIBRARY_SR,
             mono: bool = True) -> Optional[Tuple[np.ndarray, int]]:
        """读取素材为 (ndarray, sr)；已重采样到 target_sr。找不到返回 None。"""
        path = self.resolve(ref)
        if path is None:
            return None
        data, sr = _read_wav(path)
        if sr != target_sr:
            from modules.tts_engine.audio_postprocess import AudioPostProcessor
            proc = AudioPostProcessor(sample_rate=sr)
            if data.ndim > 1 and data.shape[1] > 1:
                data = np.stack([proc.resample(data[:, c], target_sr)
                                 for c in range(data.shape[1])], axis=1)
            else:
                data = proc.resample(_mono(data), target_sr)
            sr = target_sr
        if mono:
            data = _mono(data)
        return (data, sr)

    # ------------------------------------------------------------- 上传入库
    def add_audio_file(self, src: Union[str, Path], name: Optional[str] = None,
                       moods: Optional[Sequence[str]] = None, kind: str = "bgm",
                       loopable: Optional[bool] = None) -> Dict:
        """
        用户上传素材入库：优先 ffmpeg 转 wav 48k 立体声，无 ffmpeg 时 soundfile 兜底。
        kind: bgm | sfx
        """
        src = Path(src)
        if not src.exists():
            raise FileNotFoundError(f"素材文件不存在: {src}")
        display = name or src.stem
        bgm_id = _ascii_id("sfx" if kind == "sfx" else "bgm", f"{display}_{src.name}")
        target_dir = self.sfx_dir if kind == "sfx" else self.bgm_dir
        target = target_dir / f"{bgm_id}.wav"
        target.parent.mkdir(parents=True, exist_ok=True)

        converter = "soundfile"
        ff = ffmpeg_probe()
        ffmpeg_exe = ff.get("path")
        ffmpeg_source = ff.get("source")
        if ffmpeg_exe:
            cmd = [ffmpeg_exe, "-y", "-hide_banner", "-loglevel", "error", "-i", str(src),
                   "-ar", str(LIBRARY_SR), "-ac", "2", str(target)]
            try:
                subprocess.run(cmd, check=True, capture_output=True, timeout=180)
                converter = "ffmpeg"
            except Exception as exc:
                logger.warning("[bgm] ffmpeg 转码失败，改用 soundfile: %s", exc)
                target.unlink(missing_ok=True)
        else:
            logger.warning("[bgm] 未探测到可用 ffmpeg，上传转码降级 soundfile: %s",
                           ff.get("error"))
        if converter == "soundfile":
            data, sr = _read_wav(src) if src.suffix.lower() in (".wav", ".aiff", ".flac") \
                else (None, None)
            if data is None:
                raise RuntimeError(f"无 ffmpeg 且文件不是 soundfile 可读格式: {src.suffix}")
            if sr != LIBRARY_SR:
                from modules.tts_engine.audio_postprocess import AudioPostProcessor
                proc = AudioPostProcessor(sample_rate=sr)
                data = np.stack([proc.resample(data[:, c], LIBRARY_SR)
                                 for c in range(data.shape[1])], axis=1)
            _write_wav(target, data, LIBRARY_SR)

        data, sr = _read_wav(target)
        meta = self.load_meta()
        record = {
            "id": bgm_id,
            "name": display,
            "description": f"用户上传（{converter} 转 wav {LIBRARY_SR} {'立体声' if data.shape[1] == 2 else '单声道'}）",
            "moods": list(moods or []),
            "loopable": bool(data.shape[0] / sr >= 8.0) if loopable is None else bool(loopable),
            "file": target.relative_to(self.bgm_dir).as_posix(),
            "placeholder": False,
            "source": converter,
            "ffmpeg_source": ffmpeg_source if converter == "ffmpeg" else None,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        record.update(_measure(data, sr))
        bucket = "sfx" if kind == "sfx" else "bgm"
        meta[bucket] = [r for r in meta[bucket] if r.get("id") != bgm_id] + [record]
        self.save_meta(meta)
        return record

    # ------------------------------------------------------------- 智能体4 资源库
    def external_sfx(self, table_candidates: Sequence[str] = ("sfx", "sound_effects",
                                                             "sounds", "foley")) -> List[Dict]:
        """
        只读打通 data/resource.db 的音效表（智能体4 负责建表）。
        库或表不存在时返回空列表，绝不创建、绝不修改。
        """
        if not self.resource_db.exists():
            return []
        import sqlite3
        try:
            con = sqlite3.connect(f"file:{self.resource_db.as_posix()}?mode=ro", uri=True,
                                  timeout=5)
            con.row_factory = sqlite3.Row
        except Exception as exc:
            logger.warning("[bgm] resource.db 只读打开失败: %s", exc)
            return []
        try:
            names = {r["name"] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            table = next((t for t in table_candidates if t in names), None)
            if not table:
                return []
            cols = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
            path_col = next((c for c in ("path", "file", "file_path", "wav_path") if c in cols),
                            None)
            if not path_col:
                return []
            name_col = next((c for c in ("name", "title", "label") if c in cols), path_col)
            out = []
            for row in con.execute(f"SELECT * FROM {table} LIMIT 500"):
                rel = row[path_col]
                if not rel:
                    continue
                p = Path(rel)
                if not p.is_absolute():
                    p = (self.root / rel)
                if not p.exists():
                    continue
                out.append({
                    "id": f"ext:{table}:{row[name_col]}",
                    "name": str(row[name_col]),
                    "file": str(p),
                    "moods": [],
                    "external": True,
                })
            return out
        except Exception as exc:
            logger.warning("[bgm] resource.db 音效表读取失败: %s", exc)
            return []
        finally:
            con.close()
