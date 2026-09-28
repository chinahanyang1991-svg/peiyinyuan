"""
配音档位（在线 / 本地）的单一事实源 —— 指令#1"两套可供用户选择 模式切换"的落点。

为什么要有这一份（而不是继续散在字面量里）：审计报告与 `docs/TECHNICAL.md` 都记着一条
事实 —— 合成主链上写死了两处 `"voxcpm"`（`conversion_workflow.py:1190/420`），所以
"改配置换引擎"在过去**是无效的**。本模块把那两处收成一处真值，并回答三个问题：

1. **这一档默认哪个引擎**（真值在 `backend/config.py:31-38`，本模块只负责解释与校验）：
   在线档 = `edgetts`（本机实测：单句 0.3s、88 字 4 句端到端 4.1s）；
   本地档 = `voxcpm`（本机实测：48kHz、CPU 装载 141.6s、1 秒音频烧 51.4 秒机器时）。
2. **付费云端档为什么不当默认**：`azure`/`minimax` 两枚端点已注册，但**本机无 Key、
   一次真合成都没跑过**（只实测到"路由可达、缺鉴权"这一层）。未实测的引擎自动上位 =
   拿用户一整本书去替我没做过的验证兜底。要用它们显式设 `TTS_ENGINE=azure` 即可。
3. **切到在线档时那些"绑着本地引擎的音色"怎么办**：不静默换嗓子 —— 由
   `conversion_workflow._stage_synthesize` 统计降级枚数并写进任务日志与 context。
"""

import os
from typing import Optional

TIER_ONLINE = "online"
TIER_LOCAL = "local"
TIERS = (TIER_ONLINE, TIER_LOCAL)

DEFAULT_TIER = TIER_ONLINE
DEFAULT_ONLINE_ENGINE = "edgetts"
DEFAULT_LOCAL_ENGINE = "voxcpm"

# 已注册但**未实测真合成**的付费云端引擎：可显式选，不自动上位（见模块文档第 2 条）
UNVERIFIED_CLOUD_ENGINES = ("azure", "minimax")


def _config_tier() -> str:
    from backend.config import Config
    return getattr(Config, "TTS_TIER", "") or ""


def tier_for_engine(engine: str) -> str:
    """给定引擎名反推它属于哪一档（`Config` 用它把旧的 `TTS_ENGINE` 口径接进来）。

    未注册/未给/拿不准 → `local`：那是升级前的既有行为，保守侧偏好不能变成"默认去联网"。
    """
    name = (engine or "").strip().lower()
    if not name:
        return ""
    try:
        return TIER_ONLINE if is_online_engine(name) else TIER_LOCAL
    except Exception:                            # pragma: no cover - 注册表不可用
        return TIER_LOCAL


def resolve_tier(value: str = None, env: Optional[dict] = None) -> str:
    """把（可能为空的）档位取值规整成 `online`/`local`；非法即报错，不猜。"""
    raw = value
    if raw is None and env is not None:
        raw = env.get("TTS_TIER")
    if raw is None:
        raw = _config_tier()
    if raw is None or raw == "":
        raw = os.getenv("TTS_TIER") or DEFAULT_TIER
    name = str(raw).strip().lower()
    if name not in TIERS:
        raise ValueError(f"未知配音档位 '{raw}'，可选: {', '.join(TIERS)}")
    return name


def is_online_engine(name: str) -> bool:
    """该引擎是不是在线（IO 等待型、无本地权重）。未注册返回 False。"""
    from modules.tts_engine.engine_registry import engine_registry
    inst = engine_registry.get(name)
    return bool(inst is not None and getattr(inst, "online", False))


def default_engine(tier: str = None, env: Optional[dict] = None) -> str:
    """档位 → 该档应使用的引擎名（已对白名单校验）。

    `Config.TTS_ENGINE` 只有在**它的在线性与本档位一致**时才作数：
    档位是在线而配置写着 voxcpm（本地），说明那是旧部署留下的引擎名，
    本档位仍应走在线默认 —— 否则"切了在线档却没切"就成了不可见的行为。

    Raises:
        ValueError: 引擎名不在注册表白名单里（与 `resolve_engine_name` 同一纪律）。
    """
    from modules.tts_engine.engine_registry import engine_registry
    name = resolve_tier(tier, env)
    configured = ""
    try:
        from backend.config import Config
        configured = (getattr(Config, "TTS_ENGINE", "") or "").strip().lower()
    except Exception:                            # pragma: no cover - 配置不可用
        configured = ""
    if configured and engine_registry.is_known_engine(configured):
        if is_online_engine(configured) == (name == TIER_ONLINE):
            return configured
    if name == TIER_ONLINE:
        return DEFAULT_ONLINE_ENGINE
    return DEFAULT_LOCAL_ENGINE


def check_engine_for_tier(tier: str, engine: str) -> str:
    """显式指定引擎时的档位一致性检查：返回告警原因，一致则返回 ""。

    句级音色路由本来就能让一本书混用两家引擎，所以这里**只告警不拦**——
    拦下来会把"在线档里夹一枚本地克隆音色"这种合理用法打死。
    """
    if not engine:
        return ""
    want_online = tier == TIER_ONLINE
    if is_online_engine(engine) == want_online:
        return ""
    return (f"档位 '{tier}' 与引擎 '{engine}' 的在线性不一致"
            f"（{'该引擎是本地的，会占本地模型内存' if want_online else '该引擎是在线的，不占本地权重'}）")


def preload_local_at_start(env: Optional[dict] = None) -> bool:
    """在线档默认**不再**在启动时装载 5GB 级的本地权重（指令#2 的容量侧收益）。

    本地档维持原行为（无条件预热），因为那是它的主引擎；显式 `TTS_PRELOAD_LOCAL=true`
    可把在线档也拉回"启动即预热"，口径与升级前一致。
    """
    environ = os.environ if env is None else env
    raw = (environ.get("TTS_PRELOAD_LOCAL") or "").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    return resolve_tier(env=environ) == TIER_LOCAL


def describe(env: Optional[dict] = None) -> dict:
    """给 API/前端的一份子述（含"这一档默认哪个引擎、那枚引擎可用吗"）。"""
    from modules.tts_engine.engine_registry import engine_registry
    active = resolve_tier(env=env)
    out = {"tier": active, "tiers": list(TIERS), "modes": []}
    for tier in TIERS:
        engine = default_engine(tier, env=env)
        inst = engine_registry.get(engine)
        reason = inst.unavailable_reason() if inst is not None else "引擎未注册或依赖未安装"
        out["modes"].append({
            "tier": tier,
            "engine": engine,
            "engine_display": getattr(inst, "engine_display", engine),
            "available": not reason,
            "reason": reason or None,
            "online": is_online_engine(engine),
            "unverified_cloud": engine in UNVERIFIED_CLOUD_ENGINES,
        })
    return out
