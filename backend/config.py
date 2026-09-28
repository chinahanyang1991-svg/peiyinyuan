"""
配音员 - 全局配置管理
支持环境变量覆盖 + 配置文件
"""

import os
from pathlib import Path

from modules.book_parser.parser_factory import ParserFactory
from modules.tts_engine import tier as _tier


def derive_tier_and_engine(engine_env: str = None, tier_env: str = None) -> tuple:
    """把两个环境变量算成 `(档位, 引擎)` —— 纯函数，测试不必改进程 env。

    指令#1（双模式）：**档位是开关，引擎是档位的默认值**。
      - 什么都没设 => 在线档 + edgetts（本机实测单句 0.3s；本地 voxcpm 不再启动即吃 5GB）
      - 只设 TTS_ENGINE（旧部署口径，如 docker 里的 voxcpm）=> 档位由该引擎的在线性反推，
        行为与升级前一致（voxcpm 仍是本地档 + 启动预热）
      - 同时设 TTS_TIER 与 TTS_ENGINE => 档位听 TTS_TIER，引擎用显式那只
    """
    engine = (engine_env or "").strip()
    tier = ((tier_env or "").strip().lower() or _tier.tier_for_engine(engine)
            or _tier.DEFAULT_TIER)
    if engine:
        return tier, engine
    # 不给引擎名时的默认值就是档位常量本身（`tier.default_engine` 那条"配置里的
    # 引擎名与本档位在线性一致才作数"的规则在这里不适用 —— 我们正是从 env 算它）。
    return tier, (_tier.DEFAULT_ONLINE_ENGINE if tier == _tier.TIER_ONLINE
                  else _tier.DEFAULT_LOCAL_ENGINE)


class Config:
    """全局配置类（单例模式）"""

    # ---------- 目录配置 ----------
    BASE_DIR = Path(__file__).parent.parent
    # data 根的唯一入口（PY-366）：以前这一行把 `data` 写死在代码旁边，头注释却
    # 声称"支持环境变量覆盖"，于是任何一次测试都在离代码最近的 data 目录里初始化 ——
    # 在隔离工作树里跑全量回归门时，它会在检出树里建 peiyinyuan.db*，并因占位素材
    # 缺失而改写受版本控制的 data/bgm/meta.json。
    # 现在门禁/测试可以把整棵 data 根指到 scratch（PEIYINYUAN_DATA_DIR=<可写临时目录>）。
    # **不设该变量时路径与旧实现逐字节一致**（生产实例与启动器都不设它，行为不变）。
    DATA_DIR = Path(os.getenv("PEIYINYUAN_DATA_DIR") or (BASE_DIR / "data"))
    VOICE_DIR = DATA_DIR / "voices"
    OUTPUT_DIR = DATA_DIR / "output"
    DB_PATH = DATA_DIR / "peiyinyuan.db"

    # ---------- 服务器配置 ----------
    # 实际监听由启动器的 uvicorn 参数决定；这里只是"本机单用户"口径的默认值，
    # 不再写 0.0.0.0（该默认会让人误以为 API 无鉴权地暴露在整个局域网）。
    HOST = os.getenv("PEIYINYUAN_HOST", "127.0.0.1")
    PORT = int(os.getenv("PEIYINYUAN_PORT", "8000"))

    # ---------- TTS 引擎配置 ----------
    # 取值必须是 EngineRegistry 的键；本构建实测可用 voxcpm/edgetts/openvoice，
    # kokoro/indextts 缺 python 依赖（注册但不可用，见 unavailable_reason()）
    #
    # 指令#1（双模式）：口径全部在 `derive_tier_and_engine` 里（那里才是可测的纯函数），
    # 类体只负责把两个 env 灌进去。
    TTS_TIER, TTS_ENGINE = derive_tier_and_engine(os.getenv("TTS_ENGINE"),
                                                  os.getenv("TTS_TIER"))
    TTS_DEVICE = os.getenv("TTS_DEVICE", "auto")          # auto | cuda | cpu
    TTS_BATCH_SIZE = int(os.getenv("TTS_BATCH_SIZE", "4"))
    TTS_SAMPLE_RATE = int(os.getenv("TTS_SAMPLE_RATE", "48000"))

    # ---------- 模型路径配置 ----------
    VOXCPM_MODEL_PATH = os.getenv(
        "VOXCPM_MODEL_PATH",
        str(BASE_DIR.parent / "VoxCPM-main" / "src" / "voxcpm"),
    )
    KOKORO_MODEL_PATH = os.getenv("KOKORO_MODEL_PATH", "")

    # ---------- 网络爬虫配置 ----------
    CRAWL_ENABLED = os.getenv("CRAWL_ENABLED", "true").lower() == "true"
    CRAWL_INTERVAL_HOURS = int(os.getenv("CRAWL_INTERVAL_HOURS", "24"))
    CRAWL_SOURCES = os.getenv("CRAWL_SOURCES", "huggingface,github").split(",")

    # ---------- Redis / Celery 配置 ----------
    REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    CELERY_BROKER_URL = os.getenv("CELERY_BROKER_URL", REDIS_URL)
    CELERY_RESULT_BACKEND = os.getenv("CELERY_RESULT_BACKEND", REDIS_URL)

    # ---------- 文件上传限制 ----------
    MAX_UPLOAD_SIZE_MB = int(os.getenv("MAX_UPLOAD_SIZE_MB", "500"))
    MAX_BOOK_CHARS = int(os.getenv("MAX_BOOK_CHARS", "2000000"))  # 200万字
    # U-8: 上传改流式落盘后，写满磁盘从"内存里 OOM"变成"写到一半 ENOSPC"，
    # 收前留一道余量门（500MB 上传 + 解析期临时件）。
    MIN_FREE_DISK_MB = int(os.getenv("MIN_FREE_DISK_MB", "512"))

    # ---------- 音频处理配置 ----------
    FFMPEG_PATH = os.getenv("FFMPEG_PATH", "ffmpeg")
    DEFAULT_AUDIO_FORMAT = os.getenv("DEFAULT_AUDIO_FORMAT", "m4b")

    # ---------- 支持的文件格式 ----------
    # 单一事实源 = ParserFactory.FORMAT_MAP：手抄一份必然漂移（旧抄本缺 .html/.docx）。
    # .chm 无解析器故不声明 (PY-102)；.md 自 WF-20 起在册。
    SUPPORTED_EBOOK_FORMATS = sorted(ParserFactory.FORMAT_MAP.keys())
    SUPPORTED_AUDIO_FORMATS = [".m4b", ".mp3", ".wav", ".flac", ".ogg"]
    SUPPORTED_AUDIO_UPLOAD = [".wav", ".mp3", ".flac", ".m4a", ".ogg"]

    # ---------- 用户数据隐私 ----------
    DATA_LOCAL_ONLY = os.getenv("DATA_LOCAL_ONLY", "true").lower() == "true"
    ENCRYPT_USER_DATA = os.getenv("ENCRYPT_USER_DATA", "false").lower() == "true"

    @classmethod
    def is_gpu_available(cls) -> bool:
        """检测 GPU 是否可用"""
        try:
            import torch
            return torch.cuda.is_available()
        except ImportError:
            return False

    @classmethod
    def get_optimal_device(cls) -> str:
        """获取最优运行设备"""
        if cls.TTS_DEVICE != "auto":
            return cls.TTS_DEVICE
        return "cuda" if cls.is_gpu_available() else "cpu"

    @classmethod
    def validate(cls) -> list:
        """验证配置完整性，返回问题列表"""
        issues = []
        if not cls.VOXCPM_MODEL_PATH or not Path(cls.VOXCPM_MODEL_PATH).exists():
            issues.append(f"VoxCPM 模型路径不存在: {cls.VOXCPM_MODEL_PATH}")
        if cls.TTS_ENGINE == "kokoro" and not cls.KOKORO_MODEL_PATH:
            issues.append("使用 Kokoro 引擎但未设置 KOKORO_MODEL_PATH")
        if cls.TTS_TIER not in ("online", "local"):
            issues.append(f"TTS_TIER 取值非法: {cls.TTS_TIER}（可选 online | local）")
        return issues
