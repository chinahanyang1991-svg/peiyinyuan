"""
Edge-TTS 在线神经音色引擎适配器 (PY-116 快速档)

背景: VoxCPM2 在纯 CPU 上 RTF≈57~172 (qa/FINAL-STATUS.md PY-116)，整书合成
以小时/天计。edge-tts 走微软在线神经音色:
  - 中文音质档位最高 (晓晓/云健/云扬等官方神经音色)
  - 单句网络往返 ~1-2.5s，且为 IO 等待，可并发 -> 实测整书提速 1~2 个数量级
  - 限制: 需联网、不支持克隆。克隆音色仍由 voxcpm 档承担（工作流按
    "句子绑定音色的 engine 字段"逐句路由，见 conversion_workflow._stage_synthesize）。

契约: synthesize 返回 float32 mono numpy (sample_rate=24000)。
PY-304/PY-117 语义: 加载失败/合成空/解码失败一律带原因上抛，绝无静音替身。
"""

import asyncio
import io
import os
import time
from typing import Optional

import numpy as np

from modules.tts_engine.engines.base_tts import BaseTTSEngine

# 在线中文音色集（shortName -> 展示元数据；性别/年龄段与 VoiceProfile 词表一致，
# 使 cast voice_matcher 的 40/30/20/10 打分天然覆盖 edge 音色）
EDGE_ZH_VOICES = [
    {"voice": "zh-CN-XiaoxiaoNeural", "name": "Edge-晓晓(女·温暖)",
     "gender": "female", "age_group": "adult",
     "style_tags": ["温柔", "标准", "通用", "旁白"],
     "description": "微软神经女声·温暖知性，适合小说旁白"},
    {"voice": "zh-CN-XiaoyiNeural", "name": "Edge-晓伊(女·活泼)",
     "gender": "female", "age_group": "young",
     "style_tags": ["活泼", "少女", "清脆"],
     "description": "微软神经女声·活泼明亮，适合青春文学"},
    {"voice": "zh-CN-YunjianNeural", "name": "Edge-云健(男·磁性)",
     "gender": "male", "age_group": "adult",
     "style_tags": ["磁性", "浑厚", "沉稳", "旁白"],
     "description": "微软神经男声·低沉浑厚，适合历史纪实"},
    {"voice": "zh-CN-YunxiNeural", "name": "Edge-云希(男·青年)",
     "gender": "male", "age_group": "young",
     "style_tags": ["明亮", "青年", "通用"],
     "description": "微软神经男声·清爽青年音，适合现代题材"},
    {"voice": "zh-CN-YunyangNeural", "name": "Edge-云扬(男·新闻)",
     "gender": "male", "age_group": "adult",
     "style_tags": ["专业", "庄重", "新闻", "旁白"],
     "description": "微软神经男声·专业播报腔，适合严肃旁白"},
    {"voice": "zh-CN-YunxiaNeural", "name": "Edge-云夏(童声)",
     "gender": "male", "age_group": "child",
     "style_tags": ["童声", "儿童"],
     "description": "微软神经童声，适合儿童角色"},
    {"voice": "zh-CN-liaoning-XiaobeiNeural", "name": "Edge-晓北(东北女声)",
     "gender": "female", "age_group": "adult",
     "style_tags": ["幽默", "东北口音", "泼辣"],
     "description": "辽宁口音神经女声，幽默泼辣"},
    {"voice": "zh-CN-shaanxi-XiaoniNeural", "name": "Edge-晓妮(陕西女声)",
     "gender": "female", "age_group": "adult",
     "style_tags": ["明亮", "陕西口音"],
     "description": "陕西口音神经女声，明亮朴实"},
    {"voice": "zh-TW-HsiaoChenNeural", "name": "Edge-曉臻(台式女声)",
     "gender": "female", "age_group": "adult",
     "style_tags": ["温柔", "台湾口音"],
     "description": "台湾国语神经女声"},
    {"voice": "zh-TW-HsiaoYuNeural", "name": "Edge-曉雨(台式女声)",
     "gender": "female", "age_group": "young",
     "style_tags": ["甜美", "台湾口音"],
     "description": "台湾国语神经女声·甜美"},
    {"voice": "zh-TW-YunJheNeural", "name": "Edge-雲澤(台式男声)",
     "gender": "male", "age_group": "adult",
     "style_tags": ["沉稳", "台湾口音"],
     "description": "台湾国语神经男声"},
    {"voice": "zh-HK-WanLungNeural", "name": "Edge-圓龍(粵語男声)",
     "gender": "male", "age_group": "adult",
     "style_tags": ["粤语言", "沉稳"],
     "description": "粤语神经男声"},
]

DEFAULT_EDGE_VOICE = "zh-CN-XiaoxiaoNeural"

# 单句在线合成的最长等待与重试（网络抖动不应让整本书任务陪葬，
# 但连续失败仍必须上抛 —— PY-117 语义不变）
_RETRY_TIMES = 3
_RETRY_BACKOFF_S = (1.0, 3.0, 6.0)
_PER_TRY_TIMEOUT_S = float(os.getenv("PYY_EDGE_TRY_TIMEOUT", "60"))


class EdgeTTSAdapter(BaseTTSEngine):
    """微软 Edge 在线神经音色适配器（无本地权重）。"""

    engine_name = "edgetts"
    engine_display = "Edge 神经音色(在线·最快)"
    supported_languages = ["zh", "en", "ja", "ko", "yue", "fr", "de", "es"]
    supports_cloning = False
    supports_design = False
    supports_streaming = False
    supports_emotion = False
    sample_rate = 24000
    model_size = "0B (cloud)"
    # 无模型对象可预热: registry.is_loaded 走 _loaded 即可，
    # 但保留 _model=模块引用以便诊断。
    requires_model_object = True
    # workflow 路由依据: 该引擎为 IO 等待型，允许按句并发
    online = True
    supports_parallel = True

    def __init__(self):
        super().__init__()
        # voice_id(Edge shortName 或 DB profile uuid) -> Edge shortName
        self._uuid_to_edge: dict = {}
        self._voices_restored = False

    # ---------- 生命周期 ----------
    @classmethod
    def unavailable_reason(cls) -> str:
        """在线档的说话前提只有 `edge_tts` 一个包 —— 它过去只在 `load_model()` 里
        import，于是缺包的解释器（PY-354：PATH 上的裸 python 解析到 3.14）照样被
        注册表与健康门报成"可用"，用户点试听才吃到 RuntimeError。只查 import。"""
        import importlib.util
        missing = [m for m in ("edge_tts",) if importlib.util.find_spec(m) is None]
        if missing:
            return f"依赖库未安装: {', '.join(missing)} (pip install edge-tts)"
        return ""

    def load_model(self, model_path: str = None) -> None:
        try:
            import edge_tts
        except ImportError as e:
            raise RuntimeError(
                f"edge-tts 库未安装，无法使用在线神经音色: {e} (pip install edge-tts)") from e
        self._model = edge_tts
        self._loaded = True

    def unload_model(self):
        self._model = None
        self._loaded = False
        self._uuid_to_edge.clear()
        self._voices_restored = False

    # ---------- 音色解析 ----------
    def _restore_edge_voices(self):
        """从 DB 恢复 engine='edgetts' 音色的 uuid -> Edge shortName 映射。

        与 VoxCPMAdapter._restore_voices 同一模式: 工作流传来的 voice_id 是
        VoiceProfile.uuid，必须在引擎侧解析成真实 Edge 音色名，否则全部退化
        成默认音色（多角色同质化）。
        """
        if self._voices_restored:
            return
        try:
            from modules.voice_manager.voice_store import voice_store
            for v in voice_store.list_all(engine="edgetts"):
                ev = (v.meta or {}).get("edge_voice")
                if ev:
                    self._uuid_to_edge[v.id] = ev
        except Exception as e:
            # DB 尚未初始化时不致命（直传 shortName 的调用路径仍可用），
            # 但不得置 _voices_restored —— 下次合成还会重试。
            print(f"[EdgeTTS] 恢复 edge 音色映射失败(将按 uuid 直传重试): {e}")
            return
        self._voices_restored = True

    def _edge_shortnames(self) -> set:
        """PY-355 第二张合法表：uuid 映射的全部 value（DB `meta.edge_voice` 全量，
        与 uuid 同源、离线、零网络 —— 不像写死清单那样需要人肉同步）。"""
        return set(self._uuid_to_edge.values())

    def _try_resolve_voice(self, voice_id: Optional[str]) -> Optional[str]:
        """voice_id -> 真实 Edge 音色名；认不出返回 None（不抛错的版本，见 PY-360）。"""
        self._restore_edge_voices()
        if not voice_id:
            return DEFAULT_EDGE_VOICE
        if voice_id in self._uuid_to_edge:
            return self._uuid_to_edge[voice_id]
        # 直传合法 Edge shortName 的判据（PY-355）：
        # ① uuid 映射的 value 集合 —— 库里每行 meta.edge_voice 都算合法，清单外的
        #    真目录名（zh-HK-HiuGaai/HiuMaan 两行）不再被自己拒了；旧分支
        #    `":" in voice_id` 已删：真目录 322 名含冒号的有 0 名，那一支恒不命中，
        #    反而会放行 "Hello:Neural" 这类畸形名；
        # ② EDGE_ZH_VOICES 种子目录兜底 —— 本进程还没读到任何映射行时
        #    （seed 晚于启动 / 测试桩库只有 1 行，见 PY-360 的用例口径），12 名行为不漂。
        if voice_id in self._edge_shortnames():
            return voice_id
        known = {v["voice"] for v in EDGE_ZH_VOICES}
        if voice_id in known:
            return voice_id
        # DB uuid 但映射里没有（如本进程启动后才入库）：刷新一次再判
        self._voices_restored = False
        self._restore_edge_voices()
        if voice_id in self._uuid_to_edge:
            return self._uuid_to_edge[voice_id]
        if voice_id in self._edge_shortnames():
            return voice_id
        # 两张表都不认：维持 PY-117 纪律，明确拒绝，绝不静默退化为默认音色。
        return None

    def resolves_voice(self, voice_id) -> bool:
        """BaseTTSEngine.resolves_voice: Edge 只认自己库里的音色（uuid 映射或 shortName）。"""
        return self._try_resolve_voice(voice_id) is not None

    def _resolve_voice(self, voice_id: Optional[str]) -> str:
        resolved = self._try_resolve_voice(voice_id)
        if resolved is None:
            raise RuntimeError(
                f"voice_id {voice_id!r} 不是 Edge 音色库中的有效音色"
                f"（uuid 映射缺失且不是 Edge shortName）—— 拒绝静默退化为默认音色")
        return resolved

    # ---------- 参数映射 ----------
    @staticmethod
    def _rate_of(speed: float) -> str:
        pct = int(round((float(speed) - 1.0) * 100))
        pct = max(-50, min(100, pct))
        return f"{pct:+d}%"

    @staticmethod
    def _pitch_of(pitch: float) -> str:
        hz = int(round(float(pitch) * 2))  # 契约 -10..10 -> -20..+20Hz
        hz = max(-30, min(50, hz))
        return f"{hz:+d}Hz"

    # ---------- 合成 ----------
    async def _fetch_mp3(self, text: str, voice: str, rate: str, pitch: str) -> bytes:
        communicate = self._model.Communicate(
            text=text, voice=voice, rate=rate, pitch=pitch)
        buf = bytearray()
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                buf.extend(chunk["data"])
        return bytes(buf)

    def synthesize(
        self,
        text: str,
        voice_id: str = None,
        speed: float = 1.0,
        pitch: float = 0.0,
        emotion: str = "neutral",
        seed=None,
    ) -> np.ndarray:
        # US-C4: 本引擎 supports_seed=False（云端合成，产物不受我方 seed 控制）——
        # 收到显式 seed 必须显式拒绝，静默收下等于给上层假造"已换 seed 重试"。
        self._reject_seed(seed)
        if not self._loaded:
            self.load_model()
        voice = self._resolve_voice(voice_id)
        rate, p = self._rate_of(speed), self._pitch_of(pitch)

        last_err: Optional[BaseException] = None
        for attempt in range(_RETRY_TIMES):
            try:
                mp3 = asyncio.run(asyncio.wait_for(
                    self._fetch_mp3(text, voice, rate, p), _PER_TRY_TIMEOUT_S))
                if not mp3:
                    raise RuntimeError("edge-tts 未返回任何音频字节")
                audio, sr = self._decode_mp3(mp3)
                if audio.size == 0:
                    raise RuntimeError("mp3 解码得到空数组")
                return audio
            except Exception as e:
                last_err = e
                if attempt < _RETRY_TIMES - 1:
                    time.sleep(_RETRY_BACKOFF_S[attempt])
        raise RuntimeError(
            f"Edge-TTS 合成失败 (voice={voice}, text={text[:24]!r}, "
            f"{_RETRY_TIMES} 次重试): {type(last_err).__name__}: {last_err}") from last_err

    def _decode_mp3(self, mp3: bytes) -> "tuple[np.ndarray, int]":
        """mp3 bytes -> float32 mono。优先 soundfile(内置 mp3 支持)，兜底 ffmpeg 管道。"""
        try:
            import soundfile as sf
            data, sr = sf.read(io.BytesIO(mp3), dtype="float32", always_2d=True)
            arr = data.mean(axis=1) if data.shape[1] > 1 else data[:, 0]
            return np.ascontiguousarray(arr, dtype=np.float32), int(sr)
        except Exception:
            pass
        # 兜底: ffmpeg 解码到 f32le mono（resolves via PY-302 探测链）
        import subprocess
        from modules.tts_engine.ffmpeg_utils import resolve_ffmpeg
        exe = resolve_ffmpeg()
        p = subprocess.run(
            [exe, "-hide_banner", "-loglevel", "error", "-i", "pipe:0",
             "-f", "f32le", "-acodec", "pcm_f32le", "-ac", "1", "pipe:1"],
            input=mp3, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if p.returncode != 0:
            raise RuntimeError(
                f"mp3 解码失败 (soundfile+ffmpeg 均不可用): "
                f"{p.stderr.decode('utf-8', 'ignore')[:200]}")
        return np.frombuffer(p.stdout, dtype=np.float32).copy(), 24000

    def list_voices(self) -> list:
        return [
            {"id": v["voice"], "name": v["name"], "engine": self.engine_name,
             "gender": v["gender"], "age_group": v["age_group"],
             "style_tags": v["style_tags"], "language": "zh",
             "edge_voice": v["voice"], "online": True}
            for v in EDGE_ZH_VOICES
        ]

    def probe(self) -> dict:
        """轻量连通性自检（供 model_status / 前端档位说明使用）。"""
        ok = True
        err = ""
        try:
            self.load_model()
            audio = self.synthesize("连通性测试。", DEFAULT_EDGE_VOICE)
            ok = audio.size > 0
        except Exception as e:
            ok = False
            err = f"{type(e).__name__}: {e}"
        return {"engine": self.engine_name, "reachable": ok, "error": err}
