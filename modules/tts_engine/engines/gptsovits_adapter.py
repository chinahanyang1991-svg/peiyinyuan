"""
GPT-SoVITS api_v2 HTTP 适配器 —— 质量克隆档 (PY-116 三档布局 / #61)

实测依据: PYY\\GPT-SoVITS\\SPIKE-REPORT.md (2026-09-21)
- 判定"条件可用": CPU warm RTF 均值 ~6, 不满足实时试听, 只进离线整书合成;
  试听/速度档继续走 edgetts。
- 端点契约: POST /tts (JSON) -> 200 audio/wav (32kHz mono 16bit) / 400 JSON。
- 服务由运维侧独立启动 (api_v2.py -p 9880)，本适配器只做 HTTP 客户端，
  online=True -> workflow 不占用 model_preloader 单槽。

边界（契约要求，缺一不可）:
- 全局串行锁: api_v2 单实例 CPU 并发会互相拖垮，同一进程一次只发一个请求；
- 超时 >=600s（长句冷启动实测可达 84s+）；
- pitch 无对应参数 —— 显式报错，不静默丢弃；
- 输出偏轻 (peak 0.10~0.16) -> 确定性峰值归一（仅在 peak<0.5 时抬到 0.7）。
"""

import io
import logging
import os
import re
import threading

import numpy as np

from modules.text_processor.sentence_splitter import GPTSOVITS_SPLIT_POLICY
from modules.tts_engine.engines.base_tts import BaseTTSEngine

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "http://127.0.0.1:9880"
REQUEST_TIMEOUT_S = 600
HEALTH_TIMEOUT_S = 5
# 语言表与 GPT-SoVITS `TTS.py:289` 对齐 —— `auto` 是一等公民（US-B1/M3）。
# 走 auto 时服务端才跑 LangSegmenter 做句内语种切分；走 zh 分支会把非英文段
# 强判为 zh，`text/chinese2.py:62-70` 的 replace_punctuation 用
# `re.sub(r"[^\u4e00-\u9fa5" + punct + r"]+", "", ...)` **删掉所有拉丁字母**。
SUPPORTED_TEXT_LANGS = {
    "zh", "en", "ja", "ko", "yue", "auto", "auto_yue",
    "all_zh", "all_en", "all_ja", "all_ko", "all_yue",
}
# US-B2 / M2：cut5 会在每个逗号处硬切，且服务端对每个 fragment 尾部拼
# fragment_interval 秒静音（TTS.py:1563-1573），污染 duration_ms/M4B 时基。
# 改 cut0（服务端不切，句子由我方 SplitPolicy 决定）+ fragment_interval 收到 0.02
# （不能 0：TTS.py:1563 的 >0 分支会跳过零样本拼接，句边界会糊）。
CONTRACT_DEFAULTS = {
    "prompt_text": "",
    "text_split_method": "cut0",
    "batch_size": 1,
    "media_type": "wav",
    "streaming_mode": 0,
    "seed": 42,
    "top_k": 15, "top_p": 1, "temperature": 1,
    "speed_factor": 1.0,
    "fragment_interval": 0.02,
    "parallel_infer": True,
    "repetition_penalty": 1.35,
}
NORMALIZE_TARGET_PEAK = 0.7
NORMALIZE_TRIGGER_PEAK = 0.5

# ---- US-B4: payload 白名单与值域（参数一处收口，与 api_v2.py TTS_Request 同源）----
_TEXT_SPLIT_METHODS = {"cut0", "cut1", "cut2", "cut3", "cut4", "cut5"}
_MEDIA_TYPES = {"wav", "ogg", "aac", "raw"}
# 每个键描述类型/枚举/区间；_validate_payload 据此拦截拼错的键名与越界值。
TTS_PARAM_SPECS = {
    "text": {"type": str, "required": True},
    "text_lang": {"type": str, "enum": SUPPORTED_TEXT_LANGS, "required": True},
    "ref_audio_path": {"type": str, "required": True},
    "aux_ref_audio_paths": {"type": list},
    "prompt_lang": {"type": str, "enum": SUPPORTED_TEXT_LANGS, "required": True},
    "prompt_text": {"type": str},
    "top_k": {"type": int},
    "top_p": {"type": (int, float)},
    "temperature": {"type": (int, float)},
    "text_split_method": {"type": str, "enum": _TEXT_SPLIT_METHODS},
    "batch_size": {"type": int},
    "batch_threshold": {"type": (int, float)},
    "split_bucket": {"type": bool},
    "speed_factor": {"type": (int, float), "range": (0.5, 2.0)},
    "fragment_interval": {"type": (int, float)},
    "seed": {"type": int},
    "media_type": {"type": str, "enum": _MEDIA_TYPES},
    "streaming_mode": {"type": (int, bool)},
    "parallel_infer": {"type": bool},
    "repetition_penalty": {"type": (int, float)},
    "sample_steps": {"type": int},
    "super_sampling": {"type": bool},
    "overlap_length": {"type": int},
    "min_chunk_length": {"type": int},
}

# ---- US-B1/M3: 由待合成文本自测语种（纯 re，无外部模型/依赖）----
_CJK_RE = re.compile(r"[㐀-䶿一-鿿぀-ヿ가-힯]")
_LATIN_WORD_RE = re.compile(r"[A-Za-z]{2,}")   # ≥2 连字母才算"拉丁实词"（避开 B超/A股）


def _detect_text_lang(text: str) -> str:
    """由**待合成文本本身**判断 text_lang，而非参考音频语种（US-B1/M3）。

    - 含 CJK 且含拉丁实词 -> "auto"（服务端 LangSegmenter 句内切分，不删字母）
    - 纯拉丁实词、无 CJK   -> "en"
    - 其余（纯 CJK/数字/符号） -> "zh"
    """
    if not text:
        return "zh"
    has_cjk = bool(_CJK_RE.search(text))
    latin_words = _LATIN_WORD_RE.findall(text)
    if has_cjk:
        return "auto" if latin_words else "zh"
    return "en" if latin_words else "zh"


class GPTSoVITSAdapter(BaseTTSEngine):
    """GPT-SoVITS 零/少样本克隆引擎（HTTP 客户端形态）"""

    engine_name = "gptsovits"
    engine_display = "GPT-SoVITS 克隆档"
    supported_languages = ["zh", "en", "ja", "ko", "yue"]
    supports_cloning = True
    supports_design = False
    supports_streaming = False
    supports_emotion = False
    sample_rate = 32000
    model_size = "local-server"
    online = True
    # US-B2/M7: 适配器暴露分句策略，供 conversion_workflow._stage_split 按引擎注入
    split_policy = GPTSOVITS_SPLIT_POLICY

    # api_v2 单实例串行（类级锁: 多实例也共享同一服务端）
    _serial_lock = threading.Lock()

    def __init__(self, base_url: str = None):
        super().__init__()
        self.base_url = (base_url or os.environ.get("PEIYINYUAN_GSV_URL")
                         or DEFAULT_BASE_URL).rstrip("/")
        # US-B4: 由工作流注入的降级通报口 fn(message:str)；未注入时仅走 logger。
        self._log_sink = None

    def load_model(self, model_path: str = None) -> None:
        """无本地权重可加载 —— 探活服务端，起不来就在此刻失败归因，
        而不是等 600s 超时。"""
        import requests
        try:
            r = requests.get(f"{self.base_url}/docs", timeout=HEALTH_TIMEOUT_S)
        except Exception as e:
            raise RuntimeError(
                f"GPT-SoVITS 服务不可达: {self.base_url} "
                f"({type(e).__name__}: {e})。请先启动: "
                f"python api_v2.py -a 127.0.0.1 -p 9880 -c "
                f"GPT_SoVITS/configs/tts_infer.yaml") from e
        if r.status_code != 200:
            raise RuntimeError(
                f"GPT-SoVITS 探活失败: GET {self.base_url}/docs -> "
                f"{r.status_code}")
        self._loaded = True

    # -------- 内部 --------
    def _ref_for_voice(self, voice_id: str):
        """克隆行 -> (ref_audio_path, prompt_text, prompt_lang)。
        参考音频与转写来自 voice_cloner/PY-303 恢复的同款字段。"""
        from modules.voice_manager.voice_store import voice_store
        prof = voice_store.get(voice_id) if voice_id else None
        if prof is None:
            raise RuntimeError(
                f"gptsovits 是克隆引擎, 必须绑定含参考音频的音色行 "
                f"(voice_id={voice_id!r} 查无此行)")
        ref = (getattr(prof, "reference_audio_path", None)
               or getattr(prof, "model_path", None) or "")
        if not ref or not os.path.isfile(ref):
            raise RuntimeError(
                f"gptsovits: 音色 {getattr(prof, 'name', voice_id)!r} "
                f"参考音频缺失: {ref!r}")
        meta = getattr(prof, "meta", None) or {}
        prompt_text = meta.get("transcript") or ""
        prompt_lang = meta.get("prompt_lang") or prof.language or "zh"
        return ref, prompt_text, prompt_lang

    # -------- US-B4: 降级通报口（复用工作流的 _append_task_log）--------
    def set_log_sink(self, fn) -> None:
        """注入 fn(message:str) -> None 通报口；工作流用它把降级事实写进 task_log。"""
        self._log_sink = fn

    def _emit(self, notices: list) -> None:
        """合成**前**把每一条降级/互斥落 logger.warning + task_log（PY-117 不静默）。"""
        for msg in notices:
            logger.warning(f"[gptsovits] {msg}")
            if self._log_sink:
                try:
                    self._log_sink(msg)
                except Exception:                              # pragma: no cover
                    logger.debug("gptsovits log_sink 写入失败", exc_info=True)

    def _validate_payload(self, payload: dict) -> None:
        """payload 白名单校验，白名单从 TTS_PARAM_SPECS 派生（US-B4/M10-1）。
        键名拼错/类型不符/枚举越界/区间越界一律**明确抛错**，不再静默下发。"""
        for key, val in payload.items():
            spec = TTS_PARAM_SPECS.get(key)
            if spec is None:
                raise ValueError(
                    f"gptsovits payload 未知参数键 {key!r}（疑似拼写错误）—— "
                    f"允许键见 TTS_PARAM_SPECS（{sorted(TTS_PARAM_SPECS)}）")
            exp = spec.get("type")
            if exp is not None and not isinstance(val, exp):
                raise ValueError(
                    f"gptsovits 参数 {key!r} 类型应为 {exp}，实得 "
                    f"{type(val).__name__}={val!r}")
            enum = spec.get("enum")
            if enum is not None and val not in enum:
                raise ValueError(
                    f"gptsovits 参数 {key!r}={val!r} 不在允许集合 {sorted(enum)}")
            rng = spec.get("range")
            if rng is not None and not (rng[0] <= val <= rng[1]):
                raise ValueError(
                    f"gptsovits 参数 {key!r}={val} 越出区间 {rng}")
        for key, spec in TTS_PARAM_SPECS.items():
            if spec.get("required") and key not in payload:
                raise ValueError(f"gptsovits payload 缺少必填参数 {key!r}")

    # -------- US-B5: 服务端单槽 ref 缓存预热（把 84s 冷成本挪出合成路径）--------
    def warm_reference(self, ref_path: str) -> bool:
        """GET /set_refer_audio 预置服务端 ref 特征缓存（api_v2.py:517-523 /
        TTS.py:1144-1150 单槽）。组首句前调一次；失败**只 warn 不抛**——
        后续 /tts 会按 ref_audio_path 自行重算，预热只是提速手段。"""
        import requests
        try:
            with self._serial_lock:  # 与合成串行，避免与 /tts 抢同一单槽
                r = requests.get(f"{self.base_url}/set_refer_audio",
                                 params={"refer_audio_path": ref_path},
                                 timeout=REQUEST_TIMEOUT_S)
        except Exception as e:
            logger.warning(
                f"[gptsovits] /set_refer_audio 预热失败（不致命，合成时自会重算）: "
                f"{ref_path!r} ({type(e).__name__}: {e})")
            return False
        if r.status_code != 200:
            logger.warning(
                f"[gptsovits] /set_refer_audio 返回 {r.status_code}"
                f"（不致命）: {r.text[:120]!r}")
            return False
        return True

    def _post_tts(self, payload: dict) -> bytes:
        import requests
        with self._serial_lock:
            try:
                r = requests.post(f"{self.base_url}/tts", json=payload,
                                  timeout=REQUEST_TIMEOUT_S)
            except Exception as e:
                raise RuntimeError(
                    f"gptsovits 请求异常 ({self.base_url}/tts, "
                    f"timeout={REQUEST_TIMEOUT_S}s): {type(e).__name__}: {e}"
                ) from e
        if r.status_code != 200:
            raise RuntimeError(
                f"gptsovits /tts 返回 {r.status_code}: "
                f"{r.text[:200]!r} (text={payload['text'][:24]!r})")
        return r.content

    # -------- 合成 --------
    def synthesize(self, text: str, voice_id: str = None, speed: float = 1.0,
                   pitch: float = 0.0, emotion: str = "neutral") -> np.ndarray:
        if pitch not in (0, 0.0, None):
            raise ValueError(
                f"gptsovits 不支持 pitch 偏移(收到 {pitch})——契约上无该端点参数"
                f"(音色由参考音频决定), 请勿静默下发")
        ref, prompt_text, prompt_lang = self._ref_for_voice(voice_id)
        notices: list = []

        # US-B1/M3: 文本语种由**句子自身**决定（prompt_lang 只描述参考音频）
        text_lang = _detect_text_lang(text)
        if text_lang not in SUPPORTED_TEXT_LANGS:            # 兜底，理论上不到
            text_lang = "zh"
        if prompt_lang not in SUPPORTED_TEXT_LANGS:
            notices.append(f"参考音频语种 {prompt_lang!r} 不在服务端语言表，按 'zh' 下发")
            prompt_lang = "zh"

        # US-B3/M5: 零转写止血 —— 空 prompt_text 走 prompt-free，批量退化 naive +
        # 韵律锚定丢失。warn + task_log，但**不判硬错**（TTS.py:1132 只在 v3 raise）。
        if not (prompt_text and prompt_text.strip()):
            notices.append(
                f"prompt-free 合成（音色 voice_id={voice_id!r} 无参考转写 prompt_text）—— "
                f"服务端批量退化为 naive、韵律锚定丢失 (t2s_model.py:583)，"
                f"建议为该克隆音色补转写")

        # US-B4/M10-2: speed 夹取要归因；speed≠1.0 与服务端分桶互斥（M11）
        speed_req = float(speed)
        speed_f = max(0.5, min(2.0, speed_req))
        if speed_f != speed_req:
            notices.append(
                f"请求 speed={speed_req} 已夹到服务端区间 {speed_f}（[0.5, 2.0]）")
        if speed_f != 1.0:
            notices.append(
                f"speed_factor={speed_f}≠1.0 —— 服务端 split_bucket（分桶批推理）"
                f"已自动关闭 (TTS.py:1111-1113)，本句所在批将变慢")

        payload = dict(CONTRACT_DEFAULTS)
        payload.update({
            "text": text, "text_lang": text_lang,
            "ref_audio_path": ref,
            "prompt_text": prompt_text, "prompt_lang": prompt_lang,
            "speed_factor": speed_f,
        })
        # US-B4: 白名单校验（键名拼错/越界即刻报错，不静默下发）
        self._validate_payload(payload)
        # 降级/互斥必须**合成前**落 task_log（PY-117：所有降级必须归因）
        self._emit(notices)
        wav_bytes = self._post_tts(payload)

        import soundfile as sf
        y, sr = sf.read(io.BytesIO(wav_bytes), dtype="float32", always_2d=True)
        audio = np.asarray(y.mean(axis=1), dtype=np.float32)
        if sr != self.sample_rate and audio.size:
            import soxr
            audio = np.asarray(soxr.resample(audio, sr, self.sample_rate),
                               dtype=np.float32)
        if audio.size == 0:
            raise RuntimeError(
                f"gptsovits 返回空音频 (text={text[:24]!r})"
                f" —— 不以静音补齐 (PY-117)")
        peak = float(np.max(np.abs(audio)))
        if peak < 0.02:
            raise RuntimeError(
                f"gptsovits 输出近静音 (peak={peak:.4f}, text={text[:24]!r})"
                f" —— 判失败 (PY-117 双门同口径)")
        if peak < NORMALIZE_TRIGGER_PEAK:
            # 实测该引擎输出偏轻(0.10~0.16)，确定性抬峰，不影响 P8 限幅
            audio = audio * (NORMALIZE_TARGET_PEAK / peak)
        return audio
