"""
音色管理服务
连接 API 层与音色管理模块

升级波次 · 泳道 C 的契约（US-C2 / US-C4）：
  * `engine` 是**注册表键**，不是"音色来源"。合法取值只有
    `engine_registry.known_engine_names()`；本模块所有写库路径都在**写入前**校验，
    非法值直接 ValueError（API 层落 400），不再让 "cloned" 这种来源名渗进 engine 列。
  * 历史上渗进去的坏值由 `audit_voice_engine_values()` / `migrate_voice_engine_values()`
    收口（只读扫描 + 显式 apply，改写前把受影响行整体备份成 JSON）。
    **本泳道不对生产库执行迁移** —— 命令行交给主线在停机窗口跑。
  * US-C4: `VoiceProfile.seed` 落"最近一次成功合成的 seed"，由
    `record_last_successful_seed()` 写、试听端点回传。
"""

import os
import uuid
import random
import logging
import tempfile
from typing import Optional, Tuple
from pathlib import Path
from fastapi import UploadFile

from modules.tts_engine.engine_registry import engine_registry
from modules.voice_manager.voice_store import voice_store
from modules.voice_manager.voice_cloner import VoiceCloner
from modules.voice_manager.voice_designer import voice_designer
from modules.voice_manager.voice_crawler.hf_crawler import HuggingFaceCrawler
from modules.voice_manager.voice_crawler.github_crawler import GitHubCrawler
from modules.voice_manager.audio_voice_analyzer import audio_analyzer

logger = logging.getLogger(__name__)

# US-C2: 历史坏值 → 应当改成的合法注册表键。
# "cloned"/"uploaded"/"designed" 是**来源**（voice_profiles.source 列的取值），
# 曾被误写进 engine 列（本文件历史上的 `engine="cloned"` 就是这条），注册表里没有
# 这些工厂 ⇒ 用到就 EngineLoadError。这三类音色走参考音频/文本前缀，实际承载引擎是 voxcpm。
ENGINE_VALUE_FIXES = {
    "cloned": "voxcpm",
    "uploaded": "voxcpm",
    "designed": "voxcpm",
    "downloaded": "voxcpm",
    "": "voxcpm",
}


class VoiceService:
    """音色管理服务"""

    def __init__(self):
        self.cloner = VoiceCloner()
        self.hf_crawler = HuggingFaceCrawler()
        self.github_crawler = GitHubCrawler()

    def list_voices(self, language: str = None, gender: str = None,
                    source: str = None, engine: str = None,
                    style_tag: str = None, category: str = None,
                    limit: int = 100) -> list:
        """列出音色"""
        voices = voice_store.list(
            language=language, gender=gender,
            source=source, engine=engine,
            style_tag=style_tag, category=category, limit=limit,
        )
        return [v.to_dict() for v in voices]

    def get_voice(self, voice_id: str) -> Optional[dict]:
        """获取音色详情"""
        voice = voice_store.get(voice_id)
        return voice.to_dict_full() if voice else None

    def search_voices(self, keyword: str) -> list:
        """搜索音色"""
        voices = voice_store.search(keyword)
        return [v.to_dict() for v in voices]

    @staticmethod
    def _validated_clone_engine(engine: str) -> str:
        """克隆引擎白名单：必须**已注册**且**声明支持克隆**。

        #76/US-C2: 过去 clone 恒写 `engine="voxcpm"`，用户无从选择本地档；
        反过来若写进一个不存在/不支持克隆的引擎名，音色行会**建成**，
        却在 PY-116 句级路由时必然 `EngineLoadError`（坏音色比 400 更糟）。
        故在写库**之前**拦下。
        """
        from modules.tts_engine.engine_registry import engine_registry
        name = (engine or "voxcpm").strip().lower()
        known = sorted(getattr(engine_registry, "_engine_factories", {}))
        inst = engine_registry.get(name)
        if inst is None:
            raise ValueError(
                f"未知的克隆引擎 {engine!r}（未注册或依赖未安装）；"
                f"可选: {known}")
        if not getattr(inst, "supports_cloning", False):
            raise ValueError(
                f"引擎 '{name}' 不支持语音克隆（上传参考音频只接受声明 "
                f"supports_cloning 的引擎；已注册: {known}）")
        return name

    def clone_voice(self, audio_file, name: str,
                    transcript: str = None, language: str = "zh",
                    gender: str = None, engine: str = "voxcpm") -> dict:
        """克隆音色（`engine` 决定这条参考音频将来由哪个引擎发声）"""
        engine = self._validated_clone_engine(engine)
        # 保存上传的音频文件到临时目录
        ext = os.path.splitext(audio_file.filename or "audio.wav")[1] or ".wav"
        with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
            tmp.write(audio_file.file.read())
            tmp_path = tmp.name

        try:
            result = self.cloner.clone(
                audio_path=tmp_path,
                name=name,
                transcript=transcript,
                language=language,
                gender=gender,
                engine_name=engine,
            )
            return result
        finally:
            # 清理临时文件
            if os.path.exists(tmp_path):
                os.remove(tmp_path)

    def design_voice(self, description: str, name: str = None,
                     category: str = None) -> dict:
        """语音设计"""
        return voice_designer.design(description, name, category=category)

    def preview_design(self, description: str) -> dict:
        """预览语音设计特征"""
        return voice_designer.preview_features(description)

    def search_online(self, query: str = "",
                      language: str = None,
                      source: str = "huggingface") -> list:
        """搜索在线音色库（#89 后：委托 `voice_online`，只要条目列表）。

        旧版本在源不可达时会返回一份**手写**的 well-known 假列表（downloads=999999），
        前端"搜到了"却永远下不动 —— 现在失败就是空列表 + `search_all()["sources"]`
        里的真实原因。需要状态请直接用 `voice_online.search_all()`。
        """
        from backend.services import voice_online
        return voice_online.search_all(query, language,
                                       source or "all")["results"]

    def get_online_voice_detail(self, model_name: str, source: str = "huggingface") -> dict:
        """在线条目详情（含可下载音频清单）。"""
        from backend.services import voice_online
        return voice_online.detail(model_name, source)

    def download_voice(self, model_name: str, engine: str = "voxcpm") -> dict:
        """下载在线音色样本并登记（#89 重写）。

        旧实现的两处致命点已去掉：
          1. 先建 `is_downloaded=0` 的行再下载 —— 失败就留下一条永远播不出的坏行；
          2. 用 `huggingface_hub.snapshot_download()` 直连官方端点 —— 本机实测
             `huggingface.co` code=000 连不上，于是 100% 走到上面那条坏路径。
        现在改成"镜像 resolve 拉真音频 → 解码归一化 → 质量门 → 成功才写 1 行"，
        详见 `backend/services/voice_online.py`。
        """
        from backend.services import voice_online
        return voice_online.download_and_register(model_name, engine)

    def analyze_audio(self, audio_file: UploadFile, name: str = "") -> dict:
        """
        分析上传的音频文件，提取音色特征
        """
        ext = Path(audio_file.filename).suffix.lower() if audio_file.filename else ".wav"
        if ext not in ('.wav', '.mp3', '.flac', '.m4a', '.ogg', '.webm'):
            raise ValueError(f"不支持的音频格式: {ext}")

        with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tmp:
            content = audio_file.file.read()
            tmp.write(content)
            tmp_path = tmp.name

        try:
            features = audio_analyzer.analyze(tmp_path, name=name)
            profile = audio_analyzer.features_to_voice_profile(features)
            return profile
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    def analyze_and_save_voice(
        self, audio_file: UploadFile, name: str, language: str = "zh",
        engine: str = None
    ) -> dict:
        """
        分析音频文件并保存为音色

        US-C2: `engine` 这里**以前写死成 "cloned"** —— 而 "cloned" 是本表 `source`
        列的取值、注册表里并没有叫它的工厂，于是这条行一旦被句级路由/预热/试听
        按引擎取用，必然 `EngineLoadError("引擎未注册")`（还会往预热槽里塞一个
        坏引擎名）。现在：来源与引擎彻底分开，engine 走注册表白名单解析，
        并回显实际落库值。
        """
        # 0. 先解析引擎名：坏值不进临时文件、不进库
        resolved_engine = engine_registry.resolve_engine_name(engine)

        # 1. 保存音频文件到音色目录
        voice_dir = Path(tempfile.gettempdir()) / "peiyinyuan_voices"
        voice_dir.mkdir(parents=True, exist_ok=True)

        ext = Path(audio_file.filename).suffix.lower() if audio_file.filename else ".wav"
        audio_path = voice_dir / f"{uuid.uuid4().hex}{ext}"
        content = audio_file.file.read()
        audio_path.write_bytes(content)

        # 2. 分析音频特征
        try:
            features = audio_analyzer.analyze(str(audio_path), name=name)
            profile = audio_analyzer.features_to_voice_profile(features)
        except Exception as e:
            audio_path.unlink(missing_ok=True)
            raise

        # 3. 保存音色记录
        tags = profile.get("style_tags", [])
        voice = voice_store.create(
            name=name,
            source="uploaded",
            engine=resolved_engine,
            sample_audio_path=str(audio_path),
            language=language or profile.get("language", "zh"),
            gender=profile.get("gender", "unknown"),
            age_group=profile.get("age_group", "adult"),
            style_tags=",".join(tags) if tags else None,
            description=f"从音频 {audio_file.filename} 分析生成",
            # 修补（第2任，HEAD 既有缺陷非本泳道引入）：VoiceProfile 的扩展列叫 `meta`，
            # 不是 Book/Project 的 `extra_data`；原来传 extra_data 会让 `voice_store.create`
            # 把 kwargs 摊进模型时抛 "'extra_data' is an invalid keyword argument"，
            # 端点恒 500 —— US-C2 的"合法 engine 落库"分支因此根本走不到。
            meta={"acoustic_features": profile.get("acoustic_features", {})},
        )
        return {
            "voice_id": voice.id,
            "name": voice.name,
            "gender": voice.gender,
            "language": voice.language,
            "engine": resolved_engine,
            "style_tags": tags,
            "acoustic_features": profile.get("acoustic_features", {}),
        }

    def delete_voice(self, voice_id: str) -> bool:
        """删除音色"""
        return voice_store.delete(voice_id)

    def _preview_usable(self, path: str) -> bool:
        """试听样本是否通过 P0-4 质量双门（静音地板 + 谱平坦度退化音）。

        判不了（解码失败 / 采样率拿不到）时**放行**，与 audio_quality 的口径一致：
        这两道门是给"确定坏了"的产物定罪的，不给解码器能力定罪。
        """
        from modules.voice_manager.audio_quality import assess_file
        report, err = assess_file(path)
        if report is None:
            logger.warning(f"[Voice] 试听质量门未参与判定 {path}: {err}")
            return True
        if not report["ok"]:
            logger.warning(f"[Voice] 试听样本未过质量门，跳过 {path}: "
                           f"{report['code']} {report['reason']}")
        return bool(report["ok"])

    def get_preview_audio(self, voice_id: str) -> Optional[str]:
        """获取音色试听音频路径（只给通过质量双门的样本）"""
        voice = voice_store.get(voice_id)
        if not voice:
            return None
        # 优先使用 sample_audio_path，其次 model_path
        candidates = [voice.sample_audio_path]
        if voice.model_path and voice.model_path.endswith(('.wav', '.mp3', '.flac')):
            candidates.append(voice.model_path)
        for path in candidates:
            if not path:
                continue
            if not os.path.exists(path):
                continue
            if self._preview_usable(path):
                return path
        return None

    def is_preview_grade(self, path: str) -> Tuple[bool, Optional[dict], Optional[str]]:
        """#95：判定"本模块现合成并落盘的试听产物"是否够格直接发给播放器。

        与 `_preview_usable()` 的差别是**故意**的：
          * 旧方法判的是历史自带的样本，"解码器读不动"时放行（不给解码能力定罪），
            改成从严会把存量音色一脚踢回 404 —— 那条口径不能动；
          * 生成产物是我们自己刚写的 WAV，读不开/太轻/太短就是坏了，必须判失败
            并由调用方淘汰缓存（PY-117：不许"有文件就算成功"）。

        判据：可解码 → 双门（静音地板 + 谱平坦度）→ `peak ≥ PREVIEW_MIN_PEAK`
        → 时长落在 `[PREVIEW_MIN_DURATION_S, PREVIEW_MAX_DURATION_S]`。
        返回 `(ok, report, error)`。
        """
        from modules.voice_manager.audio_quality import assess_file
        from backend.services.voice_preview import (PREVIEW_MAX_DURATION_S,
                                                    PREVIEW_MIN_DURATION_S,
                                                    PREVIEW_MIN_PEAK)

        report, err = assess_file(path)
        if report is None:
            return False, None, f"解码失败: {err}"
        if not report.get("ok"):
            return False, report, f"未过双门: {report.get('code')} {report.get('reason')}"
        peak = float(report.get("peak") or 0.0)
        if peak < PREVIEW_MIN_PEAK:
            return False, report, (f"峰值 {peak:.6f} 低于试听地板 {PREVIEW_MIN_PEAK}")
        sr = report.get("sample_rate")
        if sr:
            duration_s = float(report.get("samples") or 0) / float(sr)
            if not (PREVIEW_MIN_DURATION_S <= duration_s <= PREVIEW_MAX_DURATION_S):
                return False, report, f"时长 {duration_s:.2f}s 不在试听合理区间"
        return True, report, None

    def get_statistics(self) -> dict:
        """获取音色库统计"""
        return voice_store.get_statistics()


# ======================= US-C4: seed 落库与回传 =======================

PREVIEW_SEED_SPACE = 1_000_000     # 0..999_999，人手可读、日志可比对


def make_preview_seed(engine) -> Optional[int]:
    """给一次试听/克隆合成准备**基准 seed**（US-C4 生产者侧）。

    只对声明 `supports_seed = True` 的引擎播种：
      * `voxcpm` / `openvoice` —— seed 在引擎互斥闸内生效，同 seed 同产物；
      * `kokoro` / `edgetts` / `sample` / `gptsovits` —— `supports_seed = False`，
        收到非空 seed 会**显式 400**（`base_tts._reject_seed`），所以这里返回
        None 让调用方**连 seed 字段都不发**（US-C4 调用纪律第 2 条）。

    重试轮次（质量门未过换 seed）由 `synthesize_with_gate` 用 `seed + attempt`
    递进，本函数只负责首抽。
    """
    if not getattr(engine, "supports_seed", False):
        return None
    return random.randrange(PREVIEW_SEED_SPACE)


def get_voice_seed(voice_id: str) -> Optional[int]:
    """取这条音色"最近一次成功合成的 seed"（US-C4）；无列/无行/无值都返回 None。"""
    try:
        from backend.models.base import get_session
        from backend.models.voice import VoiceProfile
        session = get_session()
        try:
            row = session.query(VoiceProfile.seed).filter(
                VoiceProfile.id == voice_id).first()
            if row is None:
                return None
            value = row[0]
            return int(value) if value is not None else None
        finally:
            session.close()
    except Exception as e:
        # 老库尚未跑轻量列迁移时会 no such column —— 不是错误，只是"还没有 seed"
        logger.debug(f"[Voice] 读取 seed 失败 {voice_id}: {type(e).__name__}: {e}")
        return None


def record_last_successful_seed(voice_id: str, seed) -> bool:
    """记下"最近一次**成功**产出该音色试听/成品的 seed"（US-C4，可复现重试的锚点）。

    best-effort：`seed=None`（引擎不兑现 seed）或音色行不存在都返回 False 且**不抛**——
    质量门已经过了，没处记 seed 不该让整次合成失败。
    """
    if seed is None:
        return False
    try:
        from backend.models.base import get_session
        from backend.models.voice import VoiceProfile
        session = get_session()
        try:
            voice = session.get(VoiceProfile, voice_id)
            if voice is None:
                return False
            voice.seed = int(seed)
            session.add(voice)
            session.commit()
            logger.info(f"[Voice] 音色 {voice_id} 记 last_successful_seed={int(seed)}")
            return True
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
    except Exception as e:
        logger.warning(f"[Voice] 记录 seed 失败 {voice_id}: {type(e).__name__}: {e}")
        return False


# ======================= US-C2: 存量坏 engine 值审计/迁移 =======================
#
# 为什么需要它：`voice_profiles.engine` 历史上被写进过**来源名**（"cloned"/"uploaded"
# …，见本文件升级前的 `engine="cloned"`）。写入路径已修，但**存量行不会自己变干净**，
# 而这些行一旦被按引擎取用就是 `EngineLoadError`（外加污染预热槽）。
#
# 安全口径（刻意为之）：
#   * 只连**显式指定**的 db_path；扫描用 SQLite 只读 URI（`mode=ro`），
#     写只在 `apply=True` 时发生；
#   * 改写前把**受影响行的整行**备份成 JSON（含原 engine 值）⇒ 可逐行回滚；
#   * 映射表之外的未知值默认**不动**（只上报），要一并回退才显式给 `map_unknown_to`；
#   * 幂等：跑第二次时合法行已经不会被再次命中。

def _voice_engine_rows(db_path: str):
    """只读扫描：返回 (全部 engine 取值计数, 非法行列表)。"""
    import sqlite3

    known = set(engine_registry.known_engine_names())
    uri = Path(db_path).resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        conn.row_factory = sqlite3.Row
        cols = {r[1] for r in conn.execute("PRAGMA table_info(voice_profiles)")}
        if "engine" not in cols:
            raise RuntimeError("voice_profiles 表缺 engine 列，先跑 init_db 再看这里")
        rows = conn.execute("SELECT * FROM voice_profiles").fetchall()
    finally:
        conn.close()

    distributions, illegal = {}, []
    for r in rows:
        value = r["engine"]
        key = "" if value is None else str(value)
        distributions[key] = distributions.get(key, 0) + 1
        if key not in known:
            illegal.append({k: r[k] for k in r.keys()})
    return distributions, illegal


def audit_voice_engine_values(db_path: str = None) -> dict:
    """**只读**审计：库里现有哪些 engine 值、哪些非法（不改一个字节）。"""
    from backend.models.base import get_db_path
    db_path = db_path or get_db_path()
    distributions, illegal = _voice_engine_rows(db_path)
    known = engine_registry.known_engine_names()
    return {
        "db_path": str(db_path),
        "known_engines": known,
        "engine_values": dict(sorted(distributions.items())),
        "total_rows": sum(distributions.values()),
        "illegal_rows": len(illegal),
        "illegal_engines": sorted({str(r.get("engine")) for r in illegal}),
        "samples": [{k: r.get(k) for k in ("id", "name", "source", "engine")}
                    for r in illegal[:10]],
    }


def migrate_voice_engine_values(db_path: str = None, apply: bool = False,
                                backup_path: str = None,
                                map_unknown_to: str = None) -> dict:
    """把非法 engine 值改成合法注册表键；`apply=False` 时只出计划（演练模式）。

    Args:
        db_path: 目标库；不给则用应用默认库（**生产库请只跑 apply=False**，
            真正的 apply 由主线在停机窗口执行，见 WAVE1-C-REPORT 的待执行清单）。
        apply: False = 只报告；True = 备份后改写。
        backup_path: 受影响整行的 JSON 备份落点（apply=True 时必填）。
        map_unknown_to: 映射表之外的未知值是否也一并回退到该引擎（默认不回退）。
    Returns:
        {"plan": [...], "applied": int, "backup_file": str|None, ...}
    """
    import json
    import sqlite3
    from backend.models.base import get_db_path

    db_path = str(db_path or get_db_path())
    fallback = None
    if map_unknown_to:
        fallback = engine_registry.resolve_engine_name(map_unknown_to)
    distributions, illegal = _voice_engine_rows(db_path)
    known = set(engine_registry.known_engine_names())

    plan, untouched = [], []
    for row in illegal:
        current = "" if row.get("engine") is None else str(row.get("engine"))
        target = ENGINE_VALUE_FIXES.get(current) or fallback
        if target is None:
            untouched.append({"id": row.get("id"), "engine": current,
                              "reason": "不在映射表且未给 map_unknown_to，保持原值"})
            continue
        if target not in known:
            # 映射表本身被改坏时宁可不动，也不能写出新非法值
            untouched.append({"id": row.get("id"), "engine": current,
                              "reason": f"目标 '{target}' 也不在注册表，保持原值"})
            continue
        plan.append({"id": row.get("id"), "name": row.get("name"),
                     "source": row.get("source"), "from": current, "to": target,
                     "row": row})

    result = {
        "db_path": db_path, "apply": bool(apply),
        "illegal_before": len(illegal), "planned": len(plan),
        "left_untouched": untouched, "changes": [
            {k: item[k] for k in ("id", "name", "source", "from", "to")}
            for item in plan],
        "applied": 0, "backup_file": None,
        "engine_values_after": None,
    }
    if not apply or not plan:
        return result
    if not backup_path:
        raise ValueError("migrate_voice_engine_values: apply=True 必须给 backup_path "
                         "（受影响行的整行备份，回滚靠它）")

    backup_path = str(backup_path)
    Path(backup_path).parent.mkdir(parents=True, exist_ok=True)
    with open(backup_path, "w", encoding="utf-8") as f:
        json.dump({"db_path": db_path,
                   "restored_from": "本文件即迁移前整行快照；按 id 回写 engine 列即可回滚",
                   "rows": [item["row"] for item in plan]},
                  f, ensure_ascii=False, indent=2, default=str)
    result["backup_file"] = backup_path

    conn = sqlite3.connect(db_path)
    try:
        with conn:
            for item in plan:
                conn.execute("UPDATE voice_profiles SET engine = ? WHERE id = ?",
                             (item["to"], item["id"]))
        result["applied"] = len(plan)
    finally:
        conn.close()
    after, _ = _voice_engine_rows(db_path)
    result["engine_values_after"] = dict(sorted(after.items()))
    logger.info(f"[Voice][US-C2] 已迁移 {result['applied']} 行非法 engine 值，"
                f"备份见 {backup_path}")
    return result


# 全局单例
voice_service = VoiceService()