"""
OpenVoice v1 适配器 —— 本地**即时**克隆档 (#76 S3)

实测依据: PYY\\归档-非程序件\\local-clone\\SPIKE-LOCAL-CLONE.md（09-28 起归档在此）
- 选型结论: MIT 许可（码 + 权重），278MB 权重，CPU 可跑，克隆**不需要参考音频转写**
  （音色由 256 维 Speaker Embedding 表达，与 GSV 的 prompt_text 零样本路线正交）。
- 冷启动 base 7.24s + converter 6.57s ≈ 13.8s；抽 SE（= "克隆"这步）0.08~0.57s/7.5s 参考。
- 稳定档 RTF 3.5~4.5（2~4 torch 线程），**未达 RTF<2 的即时目标**，与 GSV(CPU≈4.2) 同量级；
  分工见报告：OpenVoice 卖"秒级上传即得可用音色 + 无需转写"，GSV 卖"有转写时质量更稳"。

工程形态（与 voxcpm_adapter 同一 vendoring 路线）:
- 上游源码整体放在 `modules/tts_engine/openvoice/`（16 个 .py + LICENSE，**未改一行上游文件**）；
- 权重 `data/models/openvoice/{base_speakers/ZH,converter}/`（离线，运行时不联网）；
- `online` 不设（默认 False）→ 走 model_preloader 槽位，与 voxcpm/indextts 同侧。

契约要点（照 gptsovits_adapter 的纪律）:
- PY-304: `requires_model_object = True`，`load_model` 任何失败**带原因上抛**，绝不
  "_loaded=True + _model=None" 的半死形态，也不回落 np.zeros 静音替身；
- PY-117: 空音频 / peak<0.02 双门判失败；输出偏轻（实测 peak 0.14~0.44）→ 确定性抬峰；
- pitch: OpenVoice 无音高旋钮（音色由 SE 决定）→ 显式报错，不静默丢弃。
- US-C4: `supports_seed = True` 且**真兑现** —— 推理在全局 RNG 上，seed 在互斥闸内
  `apply_global_seed` 灌入 ⇒ 同 seed 同产物、换 seed 才是真重抽（质量门重试可用）。
"""

import logging
import os
import re
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

import numpy as np

from modules.tts_engine.engines.base_tts import BaseTTSEngine

logger = logging.getLogger(__name__)

# ---------- 内部路径解析（与 voxcpm_adapter 同构） ----------
_MODULE_DIR = Path(__file__).resolve().parent          # modules/tts_engine/engines/
_TTS_DIR = _MODULE_DIR.parent                          # modules/tts_engine/
_OPENVOICE_DIR = _TTS_DIR / "openvoice"                # vendored 上游源码包
_PROJECT_ROOT = _TTS_DIR.parent.parent                 # peiyinyuan/
_DEFAULT_MODEL_DIR = _PROJECT_ROOT / "data" / "models" / "openvoice"

# 让 `from openvoice.api import ...` 可解析（vendored 包不是 site-packages 依赖）
if _OPENVOICE_DIR.exists() and str(_TTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TTS_DIR))

# ---------- 权重相对路径（缺一即判"未就绪"，见 load_model） ----------
BASE_CFG = ("base_speakers", "ZH", "config.json")
BASE_CKPT = ("base_speakers", "ZH", "checkpoint.pth")
BASE_SRC_SE = ("base_speakers", "ZH", "zh_default_se.pth")
CONV_CFG = ("converter", "config.json")
CONV_CKPT = ("converter", "checkpoint.pth")
_WEIGHT_FILES = (BASE_CFG, BASE_CKPT, BASE_SRC_SE, CONV_CFG, CONV_CKPT)

# ---------- 输出治理（与 gptsovits_adapter 同口径） ----------
NORMALIZE_TARGET_PEAK = 0.7
NORMALIZE_TRIGGER_PEAK = 0.5
SILENCE_PEAK_FLOOR = 0.02
# 峰值上界：>0.99 即贴边（PCM16 域 1.0 是硬顶），交给 P8 限幅之前先确定性回抬，
# 否则 clip_ratio 门（WF-19 口径 0.001）会在成品阶段才炸，归因成本高一档。
PEAK_CEILING = 0.99
# 音色转换保真度：tau 越小越贴源音色、越大越贴目标音色（OV-P1-1 的 fidelity 旋钮雏形）
DEFAULT_TAU = 0.3
# ---------- VITS 噪声系数（上游 `api.py:90` 硬编码，这里提到适配器可控） ----------
# PY-323 网格实测（同 seed、同一 14 字句、只动一个变量，见 agent-ops/py323/REPORT.md）：
#   noise_scale 0.333/0.45/0.667/0.8 -> 带调拼音保真度恒 1.00（**不是**字准率的旋钮）
#   noise_scale_w 0.2/0.4/0.6/0.8   -> 0.83/1.00/1.00/1.00（调小 0.2 会**掉分**）
# 所以默认值保持上游原值（实测最优区间内），提上来的意义是"不让上游硬编码挡住调参"，
# 并让运维可以在不改 vendored 源码的前提下回退/试值。
DEFAULT_NOISE_SCALE = 0.667
DEFAULT_NOISE_SCALE_W = 0.6
NOISE_SCALE_ENV = "PEIYINYUAN_OPENVOICE_NOISE_SCALE"
NOISE_SCALE_W_ENV = "PEIYINYUAN_OPENVOICE_NOISE_SCALE_W"
SPEED_RANGE = (0.5, 2.0)

# torch 线程数：S2 网格实测 2~4 线程最稳（12 线程在争抢机器上 RTF 抖到 12.35）
THREADS_ENV = "PEIYINYUAN_OPENVOICE_THREADS"
DEFAULT_TORCH_THREADS = 2
MODEL_DIR_ENV = "PEIYINYUAN_OPENVOICE_DIR"

_CJK_RE = re.compile(r"[㐀-䶿一-鿿]")
_LATIN_WORD_RE = re.compile(r"[A-Za-z]{2,}")


def _pick_language(text: str) -> str:
    """只决定 `language=` 这一个字符串，**不打任何语种标签**。

    PY-323 复测结论（先前注释是错的，这里换成实测过的口径）：
    - 语种标签 `[ZH]/[EN]` 由**上游自己加**：`api.py:82` 对每个切句执行
      `t = f'[{mark}]{t}[{mark}]'`，之后才进 `get_text()`。调用方再打一次就
      变成 `[[ZH]…[ZH]]` 嵌套，符号表（68 symbols）里 `[` 会被当普通字符拼进
      序列 —— 实测嵌套产物时长缩到 **1.51s**（正常 2.7~2.8s）、ASR 判 **en /
      lang_p 0.233**（正常 zh / ≥0.96）。所以"给文本手工加 `[ZH]`"是**负优化**，
      未打标签才是正确用法。
    - 本函数只需要选对 `language`：含 CJK -> chinese（走 `cjke_cleaners2` +
      `mandarin` 前端；夹带的英文字母按 `_latin_to_bopomofo` 逐字母拼读，
      不会像 GSV 那样把字母吃掉）；纯拉丁 -> english（走 `english_to_ipa2`，
      注意仍是中文基座的口音）。
    - `api.py:43-46` 的 language_marks 只有 EN/ZH 两个档，别的值直接 assert 失败。
    """
    if _CJK_RE.search(text or ""):
        return "chinese"
    return "english" if _LATIN_WORD_RE.search(text or "") else "chinese"


def _watermark_free_converter_cls():
    """造一个**不联网拉水印模型**的 ToneColorConverter 子类（不改上游文件）。

    上游 `api.py:102-110` 的 `__init__` 有两处坑：
      1. `kwargs.get('enable_watermark', True)` 之后又 `super().__init__(*args, **kwargs)`，
         父类 `OpenVoiceBaseClass.__init__(config_path, device)` 不认这个键
         → 传 enable_watermark=False 反而 TypeError（上游 bug）；
      2. 默认分支无条件 `import wavmark`，其权重需联网拉，本机离线必炸。
    而 `add_watermark()` 上游自带 `if self.watermark_model is None: return audio`
    （`api.py:162-164`）—— 所以只跳水印、其余逻辑全部走上游原路。

    合规提示：水印关掉意味着产物无合成来源标记，公开上线前应恢复 wavmark
    （见 LOCAL-CLONE-REPORT.md R-4）。
    """
    from openvoice.api import OpenVoiceBaseClass, ToneColorConverter

    class NoWatermarkConverter(ToneColorConverter):
        def __init__(self, config_path, device="cpu"):
            OpenVoiceBaseClass.__init__(self, config_path, device=device)
            self.watermark_model = None
            self.version = getattr(self.hps, "_version_", "v1")

    return NoWatermarkConverter


def _env_float(name: str, default: float, lo: float = 0.05,
               hi: float = 2.0) -> float:
    """读浮点型 ENV；缺失/非法/越界一律回落默认（不让一个错字把推理打歪）。"""
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        val = float(raw)
    except ValueError:
        logger.warning(f"[openvoice] {name}={raw!r} 不是浮点数，沿用 {default}")
        return default
    if not (lo <= val <= hi):
        logger.warning(f"[openvoice] {name}={val} 越出 [{lo},{hi}]，沿用 {default}")
        return default
    return val


def _apply_noise_scale_override(base, noise_scale: float,
                                noise_scale_w: float) -> tuple:
    """把 VITS 两个噪声系数钉进基座实例的 `model.infer`（**不改上游源码**）。

    上游 `api.py:90` 把 `noise_scale=0.667, noise_scale_w=0.6` 写成字面量，调用方
    没有入口。这里只在实例上包一层：仍走上游 `BaseSpeakerTTS.tts()` 的原文
    （前端、打标、切句、拼接一概不动），只是 `infer` 收到的这两个 kwarg 被替换。
    与"在适配器里重写一个 tts()"相比，这个做法不会被上游函数体漂移背刺。

    幂等：重复调用只会替换最外层包装，生效值以最后一次为准（返回该值）。
    """
    model = base.model
    prev = getattr(model, "_ov_noise_override", None)
    inner = getattr(model.infer, "_ov_inner", model.infer)
    if prev is not None and prev == (noise_scale, noise_scale_w):
        return prev

    def infer(*args, **kwargs):
        kwargs["noise_scale"] = noise_scale
        kwargs["noise_scale_w"] = noise_scale_w
        return inner(*args, **kwargs)

    infer._ov_inner = inner
    model.infer = infer
    model._ov_noise_override = (noise_scale, noise_scale_w)
    return model._ov_noise_override


# ==================== 输出峰值治理（SPIKE §5 双门口径，确定性） ====================

def peak_of(audio: np.ndarray) -> float:
    """绝对值峰值（空数组 → 0.0）。单一取值口径，供断言与日志复用。"""
    a = np.asarray(audio)
    return float(np.max(np.abs(a))) if a.size else 0.0


def _peak_gate(audio: np.ndarray, *, where: str = "输出",
               trigger: float = NORMALIZE_TRIGGER_PEAK,
               target: float = NORMALIZE_TARGET_PEAK,
               floor: float = SILENCE_PEAK_FLOOR,
               ceiling: float = PEAK_CEILING) -> tuple:
    """确定性峰值归一 + 近静音硬失败（**输出路径的唯一闸门**）。

    三段口径（照 `SPIKE-LOCAL-CLONE.md` §5，与 gptsovits_adapter 的
    `NORMALIZE_TRIGGER_PEAK/TARGET_PEAK` 同值）：

    1. `peak < floor(0.02)` → **抛错**，绝不产出静音文件（PY-117 双门同口径）。
       实测该引擎"没干活"时就是吐一条贴 0 的直线（S3 冒烟里 `out_peak=0` 那类），
       放过去 = 用户拿到一个"成功但没声音"的 wav；
    2. `peak < trigger(0.5)` → 按 `target/peak` 整体等比抬到 0.7。**等比**是关键：
       只改增益不改波形包络，所以归一前后相关系数 = 1（单测 `test_peak_gate_
       preserves_waveform_shape` 就是钉这一条，防止有人日后换成压限/整形）；
    3. `peak > ceiling(0.99)` → 等比回抬到 target。贴边产物交 P8 限幅前不处理，
       clip_ratio 门（WF-19 的 0.001）会在成品阶段才炸，归因成本高得多。

    `trigger <= peak <= ceiling` 时**一个样本都不动**，原样返回。
    Returns: (audio: float32 ndarray, peak_before: float, action: str)
    """
    a = np.asarray(audio, dtype=np.float32)
    if a.size == 0:
        raise RuntimeError(f"openvoice {where} 为空数组 —— 不以静音补齐 (PY-117)")
    peak = float(np.max(np.abs(a)))
    if not np.isfinite(peak) or peak < floor:
        raise RuntimeError(
            f"openvoice {where} 近静音 (peak={peak:.4f} < {floor}) "
            f"—— 判失败，不出静音文件 (PY-117 双门同口径)")
    if peak < trigger:
        # 实测该引擎短句峰偏轻(0.140~0.438)，确定性等比抬峰，不影响 P8 限幅
        return a * np.float32(target / peak), peak, f"lift {peak:.3f} -> {target}"
    if peak > ceiling:
        return a * np.float32(target / peak), peak, f"backoff {peak:.3f} -> {target}"
    return a, peak, ""


class _LoadedOpenVoice:
    """load_model 成功后的一次性容器。

    `self._model` 指向它 —— PY-304 的"模型对象在手才算就绪"由此成立；
    缺任何一个子模型都不会构造出来。
    """

    __slots__ = ("base", "converter", "src_se", "base_sr", "conv_sr", "model_path")

    def __init__(self, base, converter, src_se, base_sr, conv_sr, model_path):
        self.base = base
        self.converter = converter
        self.src_se = src_se
        self.base_sr = int(base_sr)
        self.conv_sr = int(conv_sr)
        self.model_path = str(model_path)


class OpenVoiceAdapter(BaseTTSEngine):
    """OpenVoice v1：秒级参考音频 → 立即可用音色（本地推理，无转写依赖）"""

    engine_name = "openvoice"
    engine_display = "OpenVoice 即时克隆档"
    # ZH 基座只暴露 chinese/english 两个 language mark（其余语种需另下 base_speakers）
    supported_languages = ["zh", "en"]
    supports_cloning = True
    supports_design = False
    supports_streaming = False
    supports_emotion = False
    # US-C4: 真兑现，不是挂名 —— VITS 的噪声/流模型都取全局 torch RNG，
    # 在 `_serial_lock` 内 `apply_global_seed(seed)` 即同 seed 同产物。
    supports_seed = True
    sample_rate = 22050
    model_size = "278MB"
    # PY-304: 真实权重适配器
    requires_model_object = True

    # 单实例 CPU 推理：并发只会互相拖垮（S2 实测），进程内一次只跑一句
    _serial_lock = threading.RLock()

    def __init__(self, model_path: str = None, device: str = "cpu"):
        super().__init__()
        self.model_path = model_path or os.getenv("PEIYINYUAN_OPENVOICE_DIR") \
            or str(_DEFAULT_MODEL_DIR)
        self.device = device
        self.load_detail: Optional[dict] = None
        # voice_id → (参考音频路径, transcript)；transcript 对本引擎非必需
        self._cloned_voices: dict = {}
        # voice_id → 256 维 Speaker Embedding（"克隆"的产物，惰性抽 + 缓存）
        self._voice_se: dict = {}
        self._torch_threads_before = None
        # 实际生效的 (noise_scale, noise_scale_w)，load_model 时写入
        self._noise_scales: Optional[tuple] = None

    # ================= 加载 =================
    def _weights(self, root: str) -> dict:
        return {name: os.path.join(root, *rel) for name, rel in (
            ("base_cfg", BASE_CFG), ("base_ckpt", BASE_CKPT),
            ("src_se", BASE_SRC_SE), ("conv_cfg", CONV_CFG),
            ("conv_ckpt", CONV_CKPT))}

    def _apply_thread_budget(self) -> int:
        """设定 CPU 推理线程数并记住原值（unload 时恢复）。

        `torch.set_num_threads` 是**进程级**开关，会影响同进程的其它引擎，
        所以只在 openvoice 常驻期间生效、卸载即还原；越界值夹到 [1, cpu_count]。
        """
        import torch
        raw = os.getenv(THREADS_ENV, "").strip()
        want = int(raw) if raw.isdigit() else DEFAULT_TORCH_THREADS
        n = max(1, min(want, os.cpu_count() or 1))
        if self._torch_threads_before is None:
            self._torch_threads_before = int(torch.get_num_threads())
        torch.set_num_threads(n)
        return n

    def load_model(self, model_path: str = None) -> None:
        """加载 ZH 基座 + 音色转换器 + 源 SE。任何失败带原因上抛（PY-304）。"""
        self._model = None
        self._loaded = False
        self._voice_se.clear()
        path = model_path or self.model_path
        stages = {}
        _wall0 = time.monotonic()
        try:
            if not _OPENVOICE_DIR.exists():
                raise RuntimeError(
                    f"OpenVoice 源码缺失: {_OPENVOICE_DIR}"
                    "（应为 vendored 上游包，见 modules/tts_engine/openvoice/）")
            w = self._weights(str(path))
            missing = [p for p in w.values() if not os.path.isfile(p)]
            if missing:
                raise FileNotFoundError(
                    f"OpenVoice 权重不完整: {path} 缺 {missing}"
                    "（权重 MIT 许可，需离线预置到 data/models/openvoice/）")
            try:
                import torch
                from openvoice.api import BaseSpeakerTTS
            except ImportError as e:
                raise RuntimeError(
                    f"OpenVoice 依赖未安装: {e} "
                    "(需 torch/librosa/soundfile/pypinyin/jieba/cn2an/"
                    "inflect/unidecode/eng_to_ipa)") from e

            n_threads = self._apply_thread_budget()

            t0 = time.monotonic()
            base = BaseSpeakerTTS(w["base_cfg"], device=self.device)
            base.load_ckpt(w["base_ckpt"])
            stages["base_speaker_s"] = round(time.monotonic() - t0, 2)
            # 上游 api.py:90 把噪声系数写死，这里提到实例级可配（默认=上游原值）
            self._noise_scales = _apply_noise_scale_override(
                base, _env_float(NOISE_SCALE_ENV, DEFAULT_NOISE_SCALE),
                _env_float(NOISE_SCALE_W_ENV, DEFAULT_NOISE_SCALE_W))
            stages["noise_scales"] = list(self._noise_scales)

            t0 = time.monotonic()
            converter = _watermark_free_converter_cls()(w["conv_cfg"],
                                                        device=self.device)
            converter.load_ckpt(w["conv_ckpt"])
            stages["converter_s"] = round(time.monotonic() - t0, 2)

            t0 = time.monotonic()
            src_se = torch.load(w["src_se"], map_location=self.device,
                                weights_only=True)
            stages["src_se_s"] = round(time.monotonic() - t0, 3)

            base_sr = int(base.hps.data.sampling_rate)
            conv_sr = int(converter.hps.data.sampling_rate)
            self._model = _LoadedOpenVoice(base, converter, src_se,
                                           base_sr, conv_sr, path)
            if base_sr != self.sample_rate:
                # 采样率以类属性对外承诺（转码侧据此写 wav），不一致即视为装配错误
                raise RuntimeError(
                    f"OpenVoice 基座采样率 {base_sr} 与适配器声明 "
                    f"{self.sample_rate} 不符，权重与代码版本不匹配")
            self._loaded = True
            stages["weights_s"] = round(sum(
                stages[k] for k in ("base_speaker_s", "converter_s", "src_se_s")), 2)
            stages["threads"] = n_threads
            stages["wall_s"] = round(time.monotonic() - _wall0, 2)
            self.load_detail = {
                "message": (f"CPU {n_threads} 线程，权重 278MB，"
                            f"冷启动 {stages['wall_s']}s"
                            f"（权重 {stages['weights_s']}s = 基座 "
                            f"{stages['base_speaker_s']}s + 转换器 "
                            f"{stages['converter_s']}s，余量为 torch/openvoice 首次 import（numba/librosa 冷启动））"),
                "stages": stages,
                "device": self.device,
            }
            logger.info(f"[OpenVoice] 模型加载完成: {path} {stages}")
        except Exception as e:
            self._model = None
            self._loaded = False
            self.load_detail = {"message": f"加载失败: {type(e).__name__}: {e}",
                                "stages": stages}
            logger.error(f"[OpenVoice] 模型加载失败（不回落静音替身，PY-304）: {e}")
            raise
        finally:
            self._restore_voices()

    def unload_model(self):
        """释放权重并还原进程级 torch 线程数。"""
        self._model = None
        self._loaded = False
        self._voice_se.clear()
        if self._torch_threads_before is not None:
            try:
                import torch
                torch.set_num_threads(self._torch_threads_before)
            except Exception:  # pragma: no cover
                pass
            self._torch_threads_before = None
        super().unload_model()

    # ================= 音色表 =================
    def _restore_voices(self):
        """从 DB 恢复本引擎的克隆音色（PY-303/PY-304 通道）。

        与 voxcpm 的差别：只认 `engine == "openvoice"` 的行 —— 音色库里几百条
        voxcpm/edgetts 行不该跑到本引擎来（SE 惰性抽取虽便宜，也不该白做），
        更重要的是别让"参考音频属于别家引擎"的行在这里被冒认。
        """
        try:
            from modules.voice_manager.voice_store import voice_store
            restored = 0
            for v in voice_store.list_all():
                if getattr(v, "engine", None) != self.engine_name:
                    continue
                ref = (getattr(v, "reference_audio_path", None)
                       or getattr(v, "model_path", None))
                if v.source in ("cloned", "downloaded") and ref \
                        and os.path.exists(ref):
                    self._cloned_voices[v.id] = (ref, (v.meta or {}).get("transcript"))
                    restored += 1
            if restored:
                logger.info(f"[OpenVoice] 从 DB 恢复 {restored} 个 openvoice 音色 "
                            f"(SE 惰性抽取)")
        except Exception as e:  # pragma: no cover - DB 未初始化等
            logger.warning(f"[OpenVoice] 恢复音色失败 (可能 DB 尚未初始化): {e}")

    def clone_voice(self, audio_path: str, transcript: str = None,
                    voice_id: str = None) -> str:
        """"克隆" = 从用户自传的授权参考音频抽 256 维音色向量(SE)。

        真实开销 0.08~0.57s/7.5s 音频（SPIKE §实测），所以这一步**当场做**：
        读不出音频就当刻失败归因，而不是留到合成期给用户一个坏音色。
        transcript 本引擎用不到（GSV 才需要），仅为与其它引擎保持同签名。
        """
        if voice_id is None:
            voice_id = f"cloned_{uuid.uuid4().hex[:8]}"
        if not self._loaded:
            self.load_model()
        self._require_model("克隆")
        if not audio_path or not os.path.isfile(audio_path):
            raise FileNotFoundError(
                f"openvoice 克隆失败: 参考音频不存在 {audio_path!r}")

        with self._serial_lock:
            se = self._extract_se(audio_path)
        self._voice_se[voice_id] = se
        self._cloned_voices[voice_id] = (audio_path, transcript)
        logger.info(f"[OpenVoice] 音色已克隆: {voice_id} <- "
                    f"{os.path.basename(audio_path)} (SE {tuple(se.shape)})")
        return voice_id

    def _extract_se(self, ref_path: str):
        conv = getattr(self._model, "converter", None)
        if conv is None:
            raise RuntimeError("openvoice 未就绪：converter 缺失")
        try:
            # 上游 extract_se 内部 librosa.load(sr=22050) 会重采样，48k 上传件可直接吃
            se = conv.extract_se([ref_path.replace("\\", "/")])
        except Exception as e:
            raise RuntimeError(
                f"openvoice 抽取音色向量失败 {os.path.basename(ref_path)!r}: "
                f"{type(e).__name__}: {e}") from e
        n_el = getattr(se, "numel", None)
        size = int(n_el()) if callable(n_el) else int(getattr(se, "size", 0) or 0)
        if se is None or size == 0:
            raise RuntimeError(
                f"openvoice 音色向量为空 {ref_path!r} —— 判失败，不静默降级为基座音色 "
                f"(PY-117)")
        return se

    def _se_for_voice(self, voice_id: str):
        """取（或惰性抽 + 缓存）某音色的目标 SE。voice_id 无行 → 明确报错。"""
        cached = self._voice_se.get(voice_id)
        if cached is not None:
            return cached
        entry = self._cloned_voices.get(voice_id)
        if entry is None:
            entry = self._lookup_ref_from_db(voice_id)
            if entry is not None:
                self._cloned_voices[voice_id] = entry
        if entry is None:
            raise RuntimeError(
                f"openvoice 是克隆引擎, 必须绑定含参考音频的音色行 "
                f"(voice_id={voice_id!r} 查无此行)")
        ref = entry[0]
        if not ref or not os.path.isfile(ref):
            raise RuntimeError(
                f"openvoice: 音色 {voice_id!r} 参考音频缺失: {ref!r}")
        with self._serial_lock:
            se = self._extract_se(ref)
        self._voice_se[voice_id] = se
        return se

    def _lookup_ref_from_db(self, voice_id: str):
        try:
            from modules.voice_manager.voice_store import voice_store
            prof = voice_store.get(voice_id)
        except Exception as e:  # pragma: no cover
            logger.warning(f"[OpenVoice] 音色 {voice_id} 查库失败: {e}")
            return None
        if prof is None:
            return None
        ref = (getattr(prof, "reference_audio_path", None)
               or getattr(prof, "model_path", None))
        if not ref or not os.path.isfile(ref):
            return None
        if getattr(prof, "engine", None) not in (None, self.engine_name):
            # 音色行明写着别家引擎：不是我的 SE 语义，拒绝冒认（PY-116 路由口径）
            raise RuntimeError(
                f"openvoice: 音色 {getattr(prof, 'name', voice_id)!r} 属于引擎 "
                f"{prof.engine!r}，不由 openvoice 合成")
        return ref, (prof.meta or {}).get("transcript")

    def list_voices(self) -> list:
        voices = [{"id": "openvoice_default", "name": "OpenVoice 中文基座",
                   "language": "zh", "gender": "neutral", "source": "builtin"}]
        for vid, (apath, transcript) in self._cloned_voices.items():
            voices.append({"id": vid, "name": f"克隆音色-{vid[-4:]}",
                           "source": "cloned", "language": "zh",
                           "audio_path": apath, "transcript": transcript or ""})
        return voices

    # ================= 合成 =================
    def synthesize(self, text: str, voice_id: str = None, speed: float = 1.0,
                   pitch: float = 0.0, emotion: str = "neutral",
                   seed: Optional[int] = None) -> np.ndarray:
        if not self._loaded:
            self.load_model()
        self._require_model("合成")
        if pitch not in (0, 0.0, None):
            raise ValueError(
                f"openvoice 不支持 pitch 偏移(收到 {pitch})——基座无该旋钮"
                f"(音色/语调由参考音频的 SE 决定), 请勿静默下发")
        if seed is not None:
            try:
                seed = int(seed)
            except (TypeError, ValueError):
                raise ValueError(
                    f"openvoice 收到无法转成整数的 seed={seed!r}（US-C4）")
        body = (text or "").strip()
        if not body:
            raise ValueError("openvoice 收到空文本，无法合成 (PY-117 不以静音顶替)")

        speed_req = float(speed if speed is not None else 1.0)
        speed_f = max(SPEED_RANGE[0], min(SPEED_RANGE[1], speed_req))
        if speed_f != speed_req:
            logger.warning(
                f"[openvoice] 请求 speed={speed_req} 已夹到 {speed_f}"
                f"（{list(SPEED_RANGE)}，对应 length_scale=1/speed）")
        lang = _pick_language(body)

        with self._serial_lock:
            # US-C4: 播种必须在互斥闸内 —— 闸外播种会被并发的另一路推理吃掉，
            # "同 seed 同产物"当场失效。
            self.apply_global_seed(seed)
            if voice_id in (None, "", "default", "openvoice_default"):
                tgt_se = None       # 基座自带音色（试听/兜底路径，非克隆）
            else:
                tgt_se = self._se_for_voice(voice_id)
            audio, sr = self._render(body, speed_f, lang, tgt_se)

        if audio.size == 0:
            raise RuntimeError(
                f"openvoice 返回空音频 (text={body[:24]!r}) —— 不以静音补齐 (PY-117)")
        if sr != self.sample_rate:
            import soxr
            audio = np.asarray(soxr.resample(audio, sr, self.sample_rate),
                               dtype=np.float32)
        audio, _peak, _action = _peak_gate(audio, where=f"text={body[:24]!r}")
        if _action:
            logger.info(f"[openvoice] 峰值门处置: {_action}")
        return np.asarray(audio, dtype=np.float32)

    def _render(self, text: str, speed: float, language: str, tgt_se):
        """基座合成 → （可选）音色转换。返回 (float32 mono, sr)。"""
        m = self._model
        base_wav = np.asarray(
            m.base.tts(text, None, speaker=self._base_speaker(),
                       language=language, speed=speed),
            dtype=np.float32)
        if tgt_se is None:
            return base_wav, m.base_sr
        return self._convert(base_wav, m.src_se, tgt_se)

    def _base_speaker(self) -> str:
        """ZH 基座的 speakers 表只有 {'default': 0}；其它权重表按 config 取首个。"""
        speakers = getattr(self._model.base, "hps", None)
        speakers = getattr(speakers, "speakers", None) or {"default": 0}
        return "default" if "default" in speakers else next(iter(speakers))

    def _convert(self, wav: np.ndarray, src_se, tgt_se):
        """把基座输出转成目标音色。

        上游 `convert()` 只吃**文件路径**（内部 librosa.load），故先落临时 wav；
        临时文件在同一次调用内删除，不污染音色目录。
        """
        m = self._model
        fd, tmp_path = tempfile.mkstemp(prefix="openvoice_src_", suffix=".wav")
        os.close(fd)
        try:
            import soundfile as sf
            sf.write(tmp_path, wav, m.base_sr, subtype="PCM_16")
            out = m.converter.convert(audio_src_path=tmp_path.replace("\\", "/"),
                                      src_se=src_se, tgt_se=tgt_se,
                                      output_path=None, tau=DEFAULT_TAU)
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:  # pragma: no cover
                pass
        return np.asarray(out, dtype=np.float32), m.conv_sr

    # ================= 内部 =================
    def _require_model(self, op: str) -> None:
        if self._model is None:
            raise RuntimeError(
                f"OpenVoice 模型未加载，无法{op}（_model=None, 权重路径 "
                f"{self.model_path}）；加载失败原因见 EngineLoadError / "
                f"/api/system/model_status")

    def get_info(self) -> dict:
        info = super().get_info()
        info["requires_transcript"] = False   # 与 GSV 的关键差异（产品可据此少要一步）
        info["tau"] = DEFAULT_TAU
        info["noise_scales"] = list(self._noise_scales) if self._noise_scales else [
            DEFAULT_NOISE_SCALE, DEFAULT_NOISE_SCALE_W]
        return info
