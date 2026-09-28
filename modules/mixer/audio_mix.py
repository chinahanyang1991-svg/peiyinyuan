"""
章节混音器（多人有声书批次 A）

设计依据: specs/2026-09-20-multi-cast-audiobook-design.md 4.2

输入: P8 输出的旁白/对白段序列（ndarray, 统一 sr）+ MixSettings(dict)
步骤: ① 段间隙(对白后/旁白后, 可配) -> ② 拼接章节音轨 -> ③ BGM 循环铺底 + RMS 侧链闪避
      -> ④ 音效点(章节头/尾/自定义) -> ⑤ 角色/主增益 -> ⑥ 软限幅(峰值 <= -1dBFS) + LUFS 归一

硬约束:
- 全部 numpy 向量化, 禁止逐样本 Python 循环（允许的循环: 帧级 attack/release、
  乐句级 loop 拼接、块级 FFT 卷积, 迭代次数与样本数无关）
- BGM 经 AudioPostProcessor.resample 统一到轨道 sr
- 闪避: 人声出现时 BGM 压低 >= duck_db(默认 8dB), 100ms 攻/释 + 250ms 保持窗
  （保持窗跨音节谷与句间间隙, 防止 BGM 在人声中间被放回满档产生抽气感）
- 无 bgm 且无 sfx 且各增益为 1(0dB) -> 直通, 逐样本与现状一致（保护 single 模式回归）

MixSettings 存 project.global_settings["mix"]:
{
  "bgm":  {"ref": "<bgm_id 或路径>", "gain_db": -18, "ducking": true, "duck_db": 8},
  "sfx":  {"chapter_marks": true, "start_ref": "sfx_chapter_start", "end_ref": "sfx_chapter_end",
           "items": [{"at": "chapter_start|chapter_end|<秒>", "file": "<id 或路径>", "gain_db": 0}]},
  "role_gains": {"<role_id 或角色名>": {"gain_db": 0, "speed": 1.0, "pitch": 0.0}},
  "master": {"gain_db": 0, "target_lufs": -16},
  "gaps":   {"dialogue": 250, "narration": 400, "default": 300}   # 不给则走 concat_audios 现状路径
}
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple, Union

import numpy as np

from modules.mixer.bgm_store import BgmStore
from modules.tts_engine.audio_postprocess import AudioPostProcessor

logger = logging.getLogger(__name__)

CEILING_DB = -1.0                      # 软限幅峰值上限（spec 4.2 ⑥）
DEFAULT_DUCK_DB = 8.0                  # 人声出现时 BGM 压低量（spec 4.2 ③）
DEFAULT_BGM_GAIN_DB = -18.0
DEFAULT_TARGET_LUFS = -16.0
DEFAULT_MAX_ADJUST_DB = 18.0             # 响度归一单次最大调整量（防极端拉升）
DEFAULT_GAP_MS = 300                   # 与 P8 现状 concat gap 一致
DEFAULT_FRAME_MS = 10.0
DEFAULT_ATTACK_S = 0.10
DEFAULT_RELEASE_S = 0.15
DEFAULT_HOLD_S = 0.25                  # 人声结束后维持 duck 的时长（跨音节谷/句间间隙）
VAD_THRESHOLD_DB = -45.0
K_WEIGHT_OFFSET = -0.691               # LUFS = -0.691 + 10*log10(mean(y^2))
K_TAPS_LEN = 1 << 11                   # K 加权级联 FIR 截断长度（2048 抽头，
                                       # 截断误差相对能量 ~1e-19，远小于响度精度需求）

# ITU-R BS.1770 K 加权：第一级（高频 shelving）数字系数
K_SHELF_COEFFS = {
    48000: ([1.53512485958697, -2.69169618940638, 1.19839281085285],
            [1.0, -1.69065929318241, 0.73248077421585]),
    44100: ([1.53098471205493, -2.65407085966642, 1.16937694694353],
            [1.0, -1.66361603787797, 0.71345446929313]),
}
K_HP_F0 = 100.9                        # 第二级（RLB 高通）截止频率

SegmentLike = Union[np.ndarray, Dict, "VocalSegment"]


@dataclass
class VocalSegment:
    """一段人声（句/段级），role 相关设置来自 Role.extra_data.mix。"""

    audio: np.ndarray
    role: str = ""
    role_id: str = ""
    kind: str = "dialogue"              # dialogue / narration
    gain_db: float = 0.0

    @property
    def key(self) -> str:
        return self.role_id or self.role


def _as_segment(item: SegmentLike) -> VocalSegment:
    if isinstance(item, VocalSegment):
        return item
    if isinstance(item, dict):
        audio = item.get("audio", item.get("wave", item.get("data")))
        return VocalSegment(
            audio=np.asarray(audio, dtype=np.float32),
            role=str(item.get("role") or item.get("speaker") or item.get("name") or ""),
            role_id=str(item.get("role_id") or ""),
            kind=str(item.get("kind") or item.get("type") or "dialogue"),
            gain_db=float(item.get("gain_db", 0.0) or 0.0),
        )
    return VocalSegment(audio=np.asarray(item, dtype=np.float32))


def _db_to_lin(db: float) -> float:
    return float(10.0 ** (float(db) / 20.0))


def _num(value, default: float = 0.0) -> float:
    """
    C4-3: 读侧安全取值——脏配置（字符串/None/字典）一律回落默认值，绝不抛异常。

    is_passthrough 在 GET mix-settings 每次读库时都会跑，历史脏 JSON 不能让接口 500；
    写侧的合法性由 validate_settings 负责（先校验后落库）。
    """
    if isinstance(value, bool) or value is None:
        return default
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    return v if np.isfinite(v) else default


def _round_db(value: float, nd: int = 2) -> Optional[float]:
    """dB 值 -> 可 JSON 序列化的 float；非有限值（如静音的 -inf）返回 None。"""
    v = float(value)
    return round(v, nd) if np.isfinite(v) else None


class AudioMix:
    """章节混音器。所有权重按 sr 对齐，输出与输入同形状（1D 单声道 / (n,2) 立体声）。"""

    def __init__(self, sample_rate: int = 48000,
                 post: Optional[AudioPostProcessor] = None,
                 store: Optional[BgmStore] = None,
                 frame_ms: float = DEFAULT_FRAME_MS,
                 attack_s: float = DEFAULT_ATTACK_S,
                 release_s: float = DEFAULT_RELEASE_S,
                 vad_threshold_db: float = VAD_THRESHOLD_DB):
        self.sample_rate = int(sample_rate)
        self.post = post or AudioPostProcessor(sample_rate=self.sample_rate)
        self.store = store or BgmStore()
        self.frame_ms = float(frame_ms)
        self.attack_s = float(attack_s)
        self.release_s = float(release_s)
        self.vad_threshold_db = float(vad_threshold_db)
        self.last_stats: Dict = {}

    # --------------------------------------------------------------- 直通判定
    @staticmethod
    def is_passthrough(settings: Optional[Dict]) -> bool:
        """
        无 BGM、无 SFX、所有增益为 1（0dB）、LUFS 目标未设 -> 走现状路径。

        C4-3: 本函数在读路径上每次都会跑（GET /cast/mix-settings、hooks 入口），
        遇到历史脏 JSON / 非预期类型时**不得抛异常**：数值走 _num()，
        子结构不是对象时按"有内容"保守判为非直通（交给 validate_settings 报因）。
        """
        if not settings:
            return True
        if not isinstance(settings, dict):
            return False

        def _block(key):
            """返回 (是否该判非直通, 子对象)。"""
            val = settings.get(key)
            if not val:
                return False, {}
            if not isinstance(val, dict):
                return True, {}
            return False, val

        stop, bgm = _block("bgm")
        if stop or bgm.get("ref"):
            return False
        stop, sfx = _block("sfx")
        if stop or sfx.get("items") or sfx.get("chapter_marks"):
            return False
        stop, gains = _block("role_gains")
        if stop:
            return False
        for gain in (gains or {}).values():
            if not isinstance(gain, dict):
                if gain:
                    return False
                continue
            if abs(_num(gain.get("gain_db"), 0.0)) > 1e-6:
                return False
        stop, master = _block("master")
        if stop:
            return False
        if abs(_num(master.get("gain_db"), 0.0)) > 1e-6:
            return False
        if master.get("target_lufs") is not None:
            return False
        return True

    # --------------------------------------------------------------- 对外主入口
    def mix(self, segments: Sequence[SegmentLike],
            settings: Optional[Dict] = None) -> np.ndarray:
        """段序列 -> 成品音轨（①②步 + mix_track 的 ③~⑥步）。"""
        segs = [_as_segment(s) for s in (segments or [])]
        segs = [s for s in segs if s.audio is not None and len(s.audio) > 0]
        settings = settings or {}

        if self.is_passthrough(settings):
            # 现状行为：AudioPostProcessor.concat_audios(gap=300ms)，逐样本一致
            track = self.post.concat_audios([s.audio for s in segs], gap_ms=DEFAULT_GAP_MS)
            self.last_stats = {"passthrough": True, "segments": len(segs),
                               "path": "concat_audios", "samples": int(len(track))}
            return track

        track = self.assemble(segs, settings)
        return self.mix_track(track, settings)

    def assemble(self, segments: Sequence[SegmentLike],
                 settings: Optional[Dict] = None) -> np.ndarray:
        """① 按说话人插段间隙 + ② 拼接章节音轨（一次性分配，无逐样本循环）。"""
        settings = settings or {}
        segments = [s for s in (_as_segment(item) for item in (segments or []))
                    if s.audio is not None and len(s.audio) > 0]
        gaps = settings.get("gaps") or {}
        # gaps 配置单位是毫秒，写入轨道前必须换算成样本数（旧实现按样本数直接用，
        # 300ms 只插了 300 个样本 -> 轨道比现状 concat 短 5%）
        default_gap = self._ms_to_samples(int(gaps.get("default", DEFAULT_GAP_MS)))
        gap_of = {"dialogue": self._ms_to_samples(int(gaps.get("dialogue", DEFAULT_GAP_MS))),
                  "narration": self._ms_to_samples(int(gaps.get("narration", DEFAULT_GAP_MS)))}
        role_gains = settings.get("role_gains") or {}

        pieces: List[np.ndarray] = []
        cursor_gap = 0
        for seg in segments:
            gain_db = float(seg.gain_db or 0.0)
            conf = role_gains.get(seg.key) or role_gains.get(seg.role) or {}
            if conf:
                gain_db += float(conf.get("gain_db", 0.0) or 0.0)
            audio = seg.audio
            if abs(gain_db) > 1e-6:
                audio = (audio * np.float32(_db_to_lin(gain_db))).astype(np.float32)
            else:
                audio = audio.astype(np.float32, copy=False)
            if pieces and cursor_gap:
                pieces.append(np.zeros(cursor_gap, dtype=np.float32))
            pieces.append(audio)
            cursor_gap = int(gap_of.get(seg.kind, default_gap))

        if not pieces:
            return np.array([], dtype=np.float32)
        total = sum(int(len(p)) for p in pieces)
        track = np.empty(total, dtype=np.float32)
        pos = 0
        for p in pieces:                      # 段级循环（次数 = 段数），非逐样本
            track[pos:pos + len(p)] = p
            pos += len(p)
        return track

    def mix_track(self, track: np.ndarray, settings: Optional[Dict] = None) -> np.ndarray:
        """已拼接的人声轨道 -> 成品（③ BGM+闪避 ④ 音效 ⑤ 主增益 ⑥ 限幅/LUFS）。"""
        settings = settings or {}
        stats: Dict = {"passthrough": False, "sr": self.sample_rate}
        track = np.asarray(track, dtype=np.float32)
        if track.ndim > 1:
            track = track.mean(axis=1).astype(np.float32)   # 混音按单声道人声轨处理
        if track.size == 0:
            self.last_stats = {"passthrough": False, "empty": True}
            return track

        if self.is_passthrough(settings):
            self.last_stats = {"passthrough": True, "path": "mix_track",
                               "samples": int(track.size)}
            return track

        out = track.copy()
        n = out.shape[0]

        # ③ BGM 铺底 + RMS 侧链闪避
        bgm_conf = settings.get("bgm") or {}
        if bgm_conf.get("ref"):
            bed, duck_info = self._build_bgm(bgm_conf, n, out, stats)
            if bed is not None:
                out = (out + bed).astype(np.float32)
                stats["bgm"] = duck_info
            else:
                stats.setdefault("warnings", []).append(
                    f"BGM 素材缺失，已跳过铺底: {bgm_conf.get('ref')}")

        # ④ 音效点
        sfx_layer, sfx_info = self._build_sfx(settings.get("sfx") or {}, n)
        if sfx_layer is not None:
            out = (out + sfx_layer).astype(np.float32)
        stats["sfx"] = sfx_info

        # ⑤ 主增益
        master = settings.get("master") or {}
        gain_db = float(master.get("gain_db", 0.0) or 0.0)
        if abs(gain_db) > 1e-6:
            out = (out * np.float32(_db_to_lin(gain_db))).astype(np.float32)
        stats["master_gain_db"] = gain_db

        # ⑥ LUFS 归一 -> 软限幅（限幅会吃掉一点响度，故限幅后再复测一次写入 stats）
        target = master.get("target_lufs", DEFAULT_TARGET_LUFS)
        lufs_info: Dict = {}
        if target is not None:
            target = float(target)
            before = self.estimate_lufs(out)
            out, applied, capped = self.normalize_lufs(out, target, measured=before)
            lufs_info = {"before": _round_db(before), "target": target,
                         "applied_db": round(float(applied), 2), "adjust_capped": bool(capped)}
        ceiling = _db_to_lin(CEILING_DB)
        raw_peak = float(np.max(np.abs(out))) if out.size else 0.0
        out = self.soft_limit(out, ceiling)
        final_peak = float(np.max(np.abs(out))) if out.size else 0.0
        if lufs_info:
            lufs_info["after"] = _round_db(self.estimate_lufs(out))
            if lufs_info.get("adjust_capped"):
                stats.setdefault("warnings", []).append(
                    f"响度归一被单次调整上限 {DEFAULT_MAX_ADJUST_DB}dB 截断，"
                    f"实测 {lufs_info.get('after')} LUFS 未达目标 {lufs_info['target']}")
            stats["lufs"] = lufs_info
        if raw_peak > ceiling:                          # 与是否设 LUFS 目标无关
            stats.setdefault("warnings", []).append(
                f"输入过载（峰值 {round(20 * np.log10(raw_peak + 1e-12), 2)}dBFS > "
                f"{CEILING_DB}dBFS），软限幅已介入")
        stats["peak"] = {"before_db": _round_db(20 * np.log10(raw_peak + 1e-12)),
                         "after_db": _round_db(20 * np.log10(final_peak + 1e-12)),
                         "ceiling_db": CEILING_DB, "clipped_samples": int(np.sum(np.abs(out) > 1.0))}
        stats["duration_s"] = round(n / float(self.sample_rate), 3)
        stats["samples_out"] = int(out.shape[0])
        self.last_stats = stats
        return out.astype(np.float32, copy=False)

    # --------------------------------------------------------------- BGM / 闪避
    def _build_bgm(self, bgm_conf: Dict, n: int, vocal: np.ndarray,
                   stats: Dict) -> Tuple[Optional[np.ndarray], Dict]:
        ref = bgm_conf.get("ref")
        read = self.store.read(ref, target_sr=self.sample_rate, mono=True)
        if read is None:
            return None, {"ref": ref, "loaded": False}
        bgm, sr = read
        if sr != self.sample_rate:                     # store.read 已重采样，双保险
            bgm = AudioPostProcessor(sample_rate=sr).resample(bgm, self.sample_rate)
        gain_db = float(bgm_conf.get("gain_db", DEFAULT_BGM_GAIN_DB) or 0.0)
        loopable = bool(bgm_conf.get("loop", True))
        bed = self._tile(bgm, n, loopable)
        bed = (bed * np.float32(_db_to_lin(gain_db))).astype(np.float32)

        info = {"ref": ref, "loaded": True, "gain_db": gain_db, "sr": self.sample_rate,
                "source_samples": int(len(bgm)), "looped": bool(len(bgm) < n)}
        if bgm_conf.get("ducking", True):
            duck_db = float(bgm_conf.get("duck_db", DEFAULT_DUCK_DB) or DEFAULT_DUCK_DB)
            curve, vad = self._duck_curve(vocal, n, duck_db)
            bed = (bed * curve.astype(np.float32)).astype(np.float32)
            info["duck_db"] = duck_db
            info["vad"] = vad
        stats["bgm_present"] = True
        return bed, info

    def _tile(self, source: np.ndarray, n: int, loopable: bool = True,
              crossfade_s: float = 0.8) -> np.ndarray:
        """把素材铺到 n 个样本：不足则等功率交叉循环，过长则截断（含首尾淡入淡出）。"""
        out = np.zeros(n, dtype=np.float32)
        src_len = int(len(source))
        if src_len == 0 or n == 0:
            return out
        if not loopable or src_len >= n:
            out[: min(n, src_len)] = source[: min(n, src_len)]
            return out
        x = min(src_len // 3, int(crossfade_s * self.sample_rate))
        if x <= 0:
            reps = int(np.ceil(n / src_len))
            return np.tile(source, reps)[:n].astype(np.float32)
        fade_out = np.sqrt(np.linspace(1.0, 0.0, x, dtype=np.float32))
        fade_in = np.sqrt(np.linspace(0.0, 1.0, x, dtype=np.float32))
        pos = 0
        first = True
        while pos < n:
            take = min(src_len, n - pos)
            seg = source[:take].copy()
            if not first and take >= x:
                seg[:x] *= fade_in
            if pos + take < n and take >= x:
                seg[-x:] *= fade_out
            out[pos:pos + take] += seg
            first = False
            pos += take - (x if (pos + take < n and take >= x) else 0)
        return out

    def _frame_rms_db(self, x: np.ndarray) -> np.ndarray:
        """帧级 RMS(dB)。用累积和在 O(1) 内存/帧求得，无逐样本循环。"""
        hop = self._hop()
        frames = int(np.ceil(len(x) / hop))
        padded = np.zeros(frames * hop, dtype=np.float64)
        padded[:len(x)] = x.astype(np.float64)
        csum = np.concatenate([[0.0], np.cumsum(padded * padded)])
        idx = np.arange(frames) * hop
        energy = (csum[idx + hop] - csum[idx]) / float(hop)
        return 10.0 * np.log10(energy + 1e-12)

    def _duck_curve(self, vocal: np.ndarray, n: int, duck_db: float) -> Tuple[np.ndarray, Dict]:
        """
        RMS 侧链闪避曲线（样本级增益包络）。
        帧级 attack/release 递推（迭代次数 = 帧数），再 np.repeat 展开到样本。
        """
        hop = self._hop()
        frames = int(np.ceil(n / hop))
        rms_db = self._frame_rms_db(vocal)
        active = rms_db > self.vad_threshold_db
        duck_lin = _db_to_lin(-abs(duck_db))
        # 人声帧之后保持一段 duck，避免音节谷/句间间隙把 BGM 放回满档造成抽气
        hold_frames = max(1, int(round(DEFAULT_HOLD_S / (self.frame_ms / 1000.0))))
        hold = np.zeros(frames, dtype=bool)
        if active.any():
            idx = np.flatnonzero(active)
            span = np.clip(idx[:, None] + np.arange(hold_frames)[None, :], 0, frames - 1)
            hold[span.ravel()] = True
        target = np.where(hold, duck_lin, 1.0)

        a_coef = 1.0 - np.exp(-hop / float(self.sample_rate) / max(1e-4, self.attack_s))
        r_coef = 1.0 - np.exp(-hop / float(self.sample_rate) / max(1e-4, self.release_s))
        curve = np.empty(frames, dtype=np.float64)
        state = 1.0
        for i in range(frames):                     # 帧级循环（~18k 次/3 分钟），非逐样本
            tgt = target[i]
            coef = a_coef if tgt < state else r_coef
            state += (tgt - state) * coef
            curve[i] = state
        # 帧级一阶平滑已足够，整帧同一增益后按帧展开到样本
        samples = np.repeat(curve, hop)[:n]
        act = curve[active] if active.any() else np.array([1.0])
        vad = {"frames": frames, "voice_frames": int(active.sum()),
               "voice_ratio": round(float(active.mean()) if frames else 0.0, 4),
               "hop_samples": hop,
               "min_gain_db": round(float(20 * np.log10(min(curve.min(), 1.0))), 2),
               "voice_median_gain_db": round(float(20 * np.log10(np.median(act))), 2),
               "voice_mean_gain_db": round(float(20 * np.log10(np.mean(act))), 2)}
        return samples, vad

    # --------------------------------------------------------------- 音效
    def _build_sfx(self, sfx_conf: Dict, n: int) -> Tuple[Optional[np.ndarray], Dict]:
        items: List[Dict] = list(sfx_conf.get("items") or [])
        if sfx_conf.get("chapter_marks"):
            items = [{"at": "chapter_start", "file": sfx_conf.get("start_ref", "sfx_chapter_start"),
                      "gain_db": sfx_conf.get("start_gain_db", -6.0)}] + items
            items = items + [{"at": "chapter_end", "file": sfx_conf.get("end_ref", "sfx_chapter_end"),
                              "gain_db": sfx_conf.get("end_gain_db", -6.0)}]
        if not items:
            return None, {"count": 0, "placed": []}
        layer = np.zeros(n, dtype=np.float32)
        placed: List[Dict] = []
        for item in items:
            ref = item.get("file") or item.get("ref")
            read = self.store.read(ref, target_sr=self.sample_rate, mono=True) if ref else None
            if read is None:
                placed.append({"at": item.get("at"), "ref": ref, "loaded": False})
                continue
            data, _sr = read
            at = item.get("at", "chapter_start")
            if at == "chapter_start":
                pos = 0
            elif at == "chapter_end":
                pos = max(0, n - len(data))
            else:
                pos = int(max(0.0, float(at)) * self.sample_rate)
            if pos >= n:
                pos = max(0, n - len(data))
            take = min(len(data), n - pos)
            gain = _db_to_lin(float(item.get("gain_db", 0.0) or 0.0))
            layer[pos:pos + take] += (data[:take] * np.float32(gain)).astype(np.float32)
            placed.append({"at": at, "ref": ref, "loaded": True, "start_sample": pos,
                           "samples": int(take)})
        return layer, {"count": len(items), "placed": placed}

    # --------------------------------------------------------------- LUFS / 限幅
    def estimate_lufs(self, x: np.ndarray) -> float:
        """K 加权响度估计（BS.1770 思路，块频域卷积，无逐样本循环）。"""
        if x is None or len(x) == 0:
            return float("-inf")
        mean_sq = self._weighted_mean_square(x, self.sample_rate)
        if mean_sq <= 0:
            return float("-inf")
        return float(K_WEIGHT_OFFSET + 10.0 * np.log10(mean_sq))

    def normalize_lufs(self, x: np.ndarray, target_lufs: float,
                       measured: Optional[float] = None) -> Tuple[np.ndarray, float, bool]:
        """
        响度归一到 target_lufs（BS.1770 思路的 K 加权积分响度）。
        返回 (音频, 实际施加的 dB, 是否被单次调整上限截断)。
        measured 可由调用方传入（mix_track 已经测过一次，重复测量要再跑一遍 FFT）。
        """
        if x is None or len(x) == 0:
            return x, 0.0, False
        if measured is None or not np.isfinite(measured):
            measured = self.estimate_lufs(x)
        if not np.isfinite(measured) or measured < -70.0:
            return x, 0.0, False                 # 近静音：不拉升，避免把噪声底抬出来
        delta = float(target_lufs) - float(measured)
        clamped = abs(delta) > DEFAULT_MAX_ADJUST_DB
        delta = max(-DEFAULT_MAX_ADJUST_DB, min(DEFAULT_MAX_ADJUST_DB, delta))
        if abs(delta) < 1e-3:
            return x, 0.0, clamped
        return (x * np.float32(_db_to_lin(delta))).astype(np.float32, copy=False), delta, clamped

    @staticmethod
    def soft_limit(x: np.ndarray, ceiling: float = _db_to_lin(CEILING_DB),
                   knee_ratio: float = 0.75) -> np.ndarray:
        """
        软限幅：拐点以下完全线性（不改节目电平），拐点以上 tanh 压缩到 ceiling。
        返回峰值必然 <= ceiling（末尾再做一次硬保险）。
        """
        if x is None or len(x) == 0:
            return x
        peak = float(np.max(np.abs(x)))
        if peak <= ceiling:
            return x
        t = ceiling * knee_ratio
        magnitude = np.abs(x)
        over = magnitude > t
        compressed = t + (ceiling - t) * np.tanh((magnitude - t) / max(1e-9, ceiling - t))
        out = np.where(over, compressed, magnitude).astype(np.float64)
        out *= np.sign(x)
        out_peak = float(np.max(np.abs(out))) if out.size else 0.0
        if out_peak > ceiling and out_peak > 0:
            out *= ceiling / out_peak                 # 硬保险，保证峰值不超上限
        return out.astype(np.float32)

    # --------------------------------------------------------------- K 加权实现
    def _ms_to_samples(self, ms: float) -> int:
        """毫秒 -> 样本数（按轨道 sr）。"""
        return max(0, int(round(float(ms) * self.sample_rate / 1000.0)))

    def _hop(self) -> int:
        return max(1, int(round(self.sample_rate * self.frame_ms / 1000.0)))

    @staticmethod
    def _biquad_impulse(b: Sequence[float], a: Sequence[float], n: int = 1024) -> np.ndarray:
        """双二阶单位冲激响应（长度 n 的循环，一次性、可缓存，不参与音频逐样本处理）。"""
        a0 = float(a[0]) or 1.0
        bb = [float(v) / a0 for v in b]
        aa = [float(v) / a0 for v in a]
        taps = np.zeros(n, dtype=np.float64)
        x1 = x2 = y1 = y2 = 0.0
        for i in range(n):
            x = 1.0 if i == 0 else 0.0
            y = bb[0] * x + bb[1] * x1 + bb[2] * x2 - aa[1] * y1 - aa[2] * y2
            x2, x1 = x1, x
            y2, y1 = y1, y
            taps[i] = y
        return taps

    @classmethod
    def _k_weight_taps(cls, sr: int) -> Tuple[np.ndarray, str]:
        cache = getattr(cls, "_taps_cache", None)
        if cache is None:
            cache = cls._taps_cache = {}
        if sr in cache:
            return cache[sr]
        hp_b, hp_a = cls._rlb_highpass(sr)
        method = "highpass_only"
        if sr in K_SHELF_COEFFS:
            sh_b, sh_a = K_SHELF_COEFFS[sr]
            taps = np.convolve(cls._biquad_impulse(sh_b, sh_a),
                               cls._biquad_impulse(hp_b, hp_a))
            method = "shelf_plus_highpass"
        else:
            taps = cls._biquad_impulse(hp_b, hp_a)
            method = "highpass_only_approx"
        taps = taps[:K_TAPS_LEN]
        cache[sr] = (taps, method)
        return cache[sr]

    @staticmethod
    def _rlb_highpass(sr: int) -> Tuple[List[float], List[float]]:
        k = float(np.tan(np.pi * K_HP_F0 / sr))
        q = 0.7071067811865476                      # Butterworth
        norm = 1.0 / (1.0 + k / q + k * k)
        b = [norm, -2.0 * norm, norm]
        a = [1.0, 2.0 * (k * k - 1.0) * norm, (1.0 - k / q + k * k) * norm]
        return b, a

    def _weighted_mean_square(self, x: np.ndarray, sr: int,
                              block: int = 1 << 15) -> float:
        """
        K 加权滤波后求 mean(y^2)：overlap-save 分块频域卷积。

        - FFT 长度固定取 2 的幂：size = block + 抽头数 这种非 2 幂长度会让 pocketfft
          慢一个数量级（3 分钟轨道实测 7.2s -> 0.3s）。
        - 内存上界 = 1 个块 + 输入的一份 float32 拷贝，与音频总长无关。
        - 循环次数 = 块数（3 分钟约 265 块），不做逐样本 Python 循环。
        """
        taps, _method = self._k_weight_taps(sr)
        x = np.ascontiguousarray(np.asarray(x, dtype=np.float32))
        n = int(x.shape[0])
        if n == 0:
            return 0.0
        lab = max(0, int(len(taps)) - 1)
        size = 1 << max(12, int(np.ceil(np.log2(block + lab))))    # FFT 长度：2 的幂
        valid = size - lab                                          # 每块有效样本数
        freq_taps = np.fft.rfft(taps.astype(np.float32), n=size)
        padded_x = np.concatenate([np.zeros(lab, dtype=np.float32), x])
        total = 0.0
        for start in range(0, n, valid):                            # 块级循环
            seg = padded_x[start:start + size]
            if seg.shape[0] < size:                                 # 尾块补零
                buf = np.zeros(size, dtype=np.float32)
                buf[:seg.shape[0]] = seg
                seg = buf
            y = np.fft.irfft(np.fft.rfft(seg) * freq_taps, n=size)
            take = min(valid, n - start)
            y = y[lab:lab + take]                                   # 丢弃回绕污染段
            total += float(np.dot(y, y))
        return total / float(n)

    def lufs_method(self) -> str:
        return self._k_weight_taps(self.sample_rate)[1]

    # --------------------------------------------------------------- 配置工具
    @staticmethod
    def settings_from_project(project_settings: Optional[Dict],
                              chapter_index: int = 0) -> Dict:
        """从 project.global_settings['mix'] 取章节级配置（可被 chapter_overrides 覆盖）。"""
        mix = dict(((project_settings or {}).get("mix")) or {})
        overrides = mix.pop("chapter_overrides", None)
        if isinstance(overrides, dict):
            key = str(chapter_index)
            if key in overrides and isinstance(overrides[key], dict):
                mix.update(overrides[key])
        return mix

    @staticmethod
    def validate_settings(settings: Optional[Dict]) -> List[str]:
        """
        给 API 层用的轻量校验，返回问题列表（空表示通过）。

        C4-3 两条硬要求：
        1) 任何输入（含字符串、null、嵌套错类型）都**不得抛异常**——它是写侧守门，
           抛出去就变成 500，调用方无法区分"用户填错"和"服务坏了"；
        2) 必须在落库前把问题全部列出（API 先 validate 再 commit），不允许
           "先写库再由读侧炸"。
        """
        problems: List[str] = []
        if settings is None:
            return problems
        if not isinstance(settings, dict):
            return ["mix 配置必须是对象"]

        def _num(label: str, container: Dict, key: str, lo: float, hi: float) -> None:
            if key not in container or container[key] is None:
                return
            raw = container[key]
            if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
                problems.append(f"{label} 必须是数字")
                return
            try:
                val = float(raw)
            except (TypeError, ValueError):
                problems.append(f"{label} 必须是数字，收到 {raw!r}")
                return
            if not np.isfinite(val):
                problems.append(f"{label} 不能是 NaN/Inf")
            elif not (lo <= val <= hi):
                problems.append(f"{label} 需在 {lo} ~ {hi} 之间，收到 {val}")

        def _obj(label: str):
            val = settings.get(label)
            if val is None:
                return {}
            if not isinstance(val, dict):
                problems.append(f"{label} 必须是对象")
                return None
            return val

        bgm = _obj("bgm")
        if bgm is not None:
            ref = bgm.get("ref")
            if ref is not None and not isinstance(ref, str):
                problems.append("bgm.ref 必须是字符串（bgm_id 或音频路径）")
            if isinstance(ref, str) and not ref.strip():
                problems.append("bgm.ref 不能是空字符串")
            _num("bgm.gain_db", bgm, "gain_db", -60.0, 12.0)
            _num("bgm.duck_db", bgm, "duck_db", 0.0, 40.0)      # 压低量取正数
            _num("bgm.fade_in_s", bgm, "fade_in_s", 0.0, 30.0)
            _num("bgm.fade_out_s", bgm, "fade_out_s", 0.0, 30.0)
            if "ducking" in bgm and not isinstance(bgm["ducking"], bool):
                problems.append("bgm.ducking 必须是布尔")
        sfx = _obj("sfx")
        if sfx is not None:
            if "chapter_marks" in sfx and not isinstance(sfx["chapter_marks"], bool):
                problems.append("sfx.chapter_marks 必须是布尔")
            items = sfx.get("items")
            if items is not None and not isinstance(items, list):
                problems.append("sfx.items 必须是数组")
            elif isinstance(items, list):
                for i, item in enumerate(items):
                    if not isinstance(item, dict):
                        problems.append(f"sfx.items[{i}] 必须是对象")
                        continue
                    if not str(item.get("file") or "").strip():
                        problems.append(f"sfx.items[{i}].file 不能为空")
                    at = item.get("at")
                    if at is not None and not isinstance(at, (int, float, str)):
                        problems.append(f"sfx.items[{i}].at 需是秒数或 chapter_start/chapter_end")
                    elif isinstance(at, str) and at.strip() not in ("chapter_start", "chapter_end"):
                        try:
                            float(at)
                        except ValueError:
                            problems.append(f"sfx.items[{i}].at 无法解析: {at!r}")
                    _num(f"sfx.items[{i}].gain_db", item, "gain_db", -60.0, 12.0)
        master = _obj("master")
        if master is not None:
            _num("master.gain_db", master, "gain_db", -60.0, 12.0)
            if master.get("target_lufs") is not None:
                _num("master.target_lufs", master, "target_lufs", -40.0, -6.0)
        gains = _obj("role_gains")
        if gains is not None:
            for name, conf in gains.items():
                if not isinstance(conf, dict):
                    problems.append(f"role_gains.{name} 必须是对象")
                    continue
                _num(f"role_gains.{name}.gain_db", conf, "gain_db", -60.0, 12.0)
                _num(f"role_gains.{name}.speed", conf, "speed", 0.5, 2.0)
                _num(f"role_gains.{name}.pitch", conf, "pitch", -12.0, 12.0)
        gaps = _obj("gaps")
        if gaps is not None:
            for key in ("default", "dialogue", "narration"):
                _num(f"gaps.{key}", gaps, key, 0.0, 10000.0)
        return problems
