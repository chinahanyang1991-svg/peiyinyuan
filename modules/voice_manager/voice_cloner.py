"""
语音克隆引擎
基于 VoxCPM 的用户音色克隆 + 本地音色管理

F-R4-4 (verify-round4 §9): `POST /voices/clone` 过去同步调用
`engine_registry.get_loaded(engine_name)` —— 而 get_loaded 会触发 `load_model()`，
实测冷启动 460s 量级（本轮 §7: 客户端 timeout=300 已超时、服务端随后才建成音色），
于是调用方超时重试就造出重复/孤儿 voice_profiles 行。
现在: 克隆 = **参考音频校验 + sha256 幂等护栏 + 落盘 + 入库**，模型只在**合成期**才需要
（唯一真正用到参考音频的地方是 `synthesize()` 的 `reference_wav_path`，
 而引擎 `load_model()` 之后的 `_restore_voices()` 会从 DB 重建音色表 —— PY-303 已实证）。
只有当引擎**已经**热在内存里时才顺手登记，让当前进程立刻可用；绝不为了登记去加载模型。
"""

import hashlib
import logging
import os
import threading
import uuid
from pathlib import Path
from typing import Optional

from modules.tts_engine.engine_registry import engine_registry
from modules.voice_manager.language_tier import tier_from_language
from modules.voice_manager.ref_fitting import prepare_reference
from modules.voice_manager.voice_store import voice_store

logger = logging.getLogger(__name__)

# F-R4-4: "查重 -> 落盘 -> 建行" 必须原子，否则客户端超时重试并发打两次
# /voices/clone 时两个请求会同时通过查重、各建一行（孤儿行回来了）。
# 单进程 uvicorn 部署下，一把进程级锁即足够（多 worker 需换成 DB 唯一索引）。
_clone_dedupe_lock = threading.Lock()


def _sha256_file(path: str) -> str:
    """参考音频内容指纹（幂等护栏的键）。分块读，50MB 上传也不吃内存。"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class VoiceCloner:
    """语音克隆引擎"""

    # 支持的参考音频格式
    SUPPORTED_AUDIO_FORMATS = ('.wav', '.mp3', '.flac', '.m4a', '.ogg', '.webm')
    # 最小音频时长（秒）
    MIN_AUDIO_DURATION = 3.0
    # 推荐音频时长
    RECOMMENDED_DURATION = 10.0

    def __init__(self):
        self.voice_dir = voice_store.voice_dir

    def validate_audio(self, audio_path: str,
                       engine_name: str = None) -> dict:
        """
        验证参考音频是否合格
        返回: {"valid": bool, "message": str, "duration": float, "quality": dict}

        三段判据，顺序从便宜到昂贵：
        1. 存在性 + 扩展名白名单；
        2. 时长 —— **地板按引擎分档取**（US-C3 `ref_window_for`），不再一律用
           `MIN_AUDIO_DURATION=3.0`：GSV/voxcpm 档下限本就是 3.0s（这些引擎的
           行为一字不变），而 openvoice 档是 1~10s，音色库 9 条 human master 里
           有 6 条落在 1.88~2.56s，用 3.0s 一刀切会把它们挡在门外（实测口径见
           `PYY\\归档-非程序件\\local-clone\\LOCAL-CLONE-REPORT.md` §6（09-28 起归档在此）。
           上限**不在这里判** —— 过长交给 `ref_fitting` 裁能量窗（clone 里做），
           直接拒收等于把"能治"的素材说成"不合格"。
        3. **质量双门**（P0-4，见 modules/voice_manager/audio_quality.py）：
           - 门 1 静音地板：整段有效峰值 < -50 dBFS（线性 0.00316）判失败 ——
             抓"模型没产出内容"的死渲染；
           - 门 2 退化音：分帧谱平坦度（几何均值/算术均值）**过低**（< 1e-7）判失败 ——
             峰值很健康、但频谱塌成一条谐波线的"响嗡嗡声"只有这一项能抓到。
             方向别记反：平坦度越小越退化，越大越像噪声。
        质量门读不出采样率/解不开码时**不判**（report=None 即跳过）—— 这两道门是给
        "确定坏了"的产物定罪的，不是给解码器能力定罪的；时长那一段已经用 pydub 兜底过。
        """
        if not os.path.exists(audio_path):
            return {"valid": False, "message": "音频文件不存在"}

        ext = os.path.splitext(audio_path)[1].lower()
        if ext not in self.SUPPORTED_AUDIO_FORMATS:
            return {"valid": False, "message": f"不支持的格式: {ext}，支持: {self.SUPPORTED_AUDIO_FORMATS}"}

        # 获取时长
        try:
            import soundfile as sf
            info = sf.info(audio_path)
            duration = info.duration
        except Exception:
            try:
                from pydub import AudioSegment
                audio = AudioSegment.from_file(audio_path)
                duration = audio.duration_seconds
            except Exception:
                return {"valid": False, "message": "无法读取音频文件"}

        min_s = self.duration_floor(engine_name)
        if duration < min_s:
            label = engine_name or "默认"
            return {
                "valid": False,
                "message": f"音频太短 ({duration:.1f}秒)，引擎 '{label}' 的克隆参考至少"
                           f"需要 {min_s} 秒（太短采不到稳定音色特征，且不做时间拉伸）",
                "duration": duration,
                "engine_floor_s": min_s,
            }

        # 质量双门（P0-4）
        from modules.voice_manager.audio_quality import assess_file
        report, err = assess_file(audio_path)
        if report is not None and not report["ok"]:
            return {
                "valid": False,
                "message": f"音频质量不合格（{report['code']}: {report['reason']}），"
                           f"不能作为克隆参考",
                "duration": duration,
                "quality": report,
            }
        if report is None:
            logger.warning(f"[Clone] 质量门未参与判定 {audio_path}: {err}")

        if duration < self.RECOMMENDED_DURATION:
            return {
                "valid": True,
                "message": f"音频时长 {duration:.1f} 秒，建议 {self.RECOMMENDED_DURATION} 秒以上效果更佳",
                "duration": duration,
                "quality": report,
            }

        return {"valid": True, "message": "音频验证通过", "duration": duration,
                "quality": report}

    def clone(
        self,
        audio_path: str,
        name: str,
        transcript: str = None,
        language: str = "zh",
        gender: str = None,
        engine_name: str = "voxcpm",
    ) -> dict:
        """
        克隆音色（F-R4-4: 不等待模型加载）
        1. 验证参考音频（时长地板按引擎分档）
        2. sha256 + 名字 + 引擎 幂等护栏 —— 命中则直接返回既有 voice_id
        3. **ref_fitting 适配（US-C3，#76 S3 接线）**：入库前跑 `prepare_reference` ——
           过长就裁"30ms 帧 RMS 能量最高的连续 6~8s 窗"**另存** `ref_{uuid}_fit.wav`
           （原件永不被改，sha256 幂等护栏因此不受影响），过短/解不开码即拒绝入库；
           四条 checks 连同度量进 `voice_profiles.meta["ref_checks"]`
        4. 参考音频落盘 + 建 voice_profiles 行（meta 记指纹/transcript/ref_checks）
        5. 引擎**已**加载时才顺手登记（当前进程立即可用）；未加载则什么都不做，
           合成期 `load_model()` → `_restore_voices()` 会按 DB 行把参考音频取回
        返回: {"voice_id", "name", "reference_sha256", "deduplicated",
               "engine_ready", "clone_mode", "reference_audio_path",
               "ref_verdict", "ref_checks"}
        """
        # 1. 验证音频（时长地板按引擎分档：openvoice 1s / GSV·voxcpm 3s）
        validation = self.validate_audio(audio_path, engine_name=engine_name)
        if not validation["valid"]:
            raise ValueError(validation["message"])

        # 2+3+4. 指纹 + 幂等护栏 + fit + 建行，整段原子（F-R4-4：并发超时重试只建一行）
        with _clone_dedupe_lock:
            ref_sha256 = _sha256_file(audio_path)
            hit = self.find_existing_clone(name=name, engine_name=engine_name,
                                           ref_sha256=ref_sha256)
            if hit:
                logger.info(
                    f"[Clone] 幂等命中: 同名 '{name}' + 同参考音频 {ref_sha256[:12]} "
                    f"已存在 voice_id={hit['id']}，复用不新建 (F-R4-4)")
                return {
                    "voice_id": hit["id"],
                    "name": hit["name"],
                    "reference_sha256": ref_sha256,
                    "deduplicated": True,
                    "engine_ready": self.engine_is_loaded(engine_name),
                    "clone_mode": "dedup-reuse",
                    "reference_audio_path": hit["reference_audio_path"],
                    "created_at": hit["created_at"],
                }

            # 3. ref_fitting：先诊断，再决定"直接用原件 / 裁一刀另存 / 退回重录"
            ref_uid = uuid.uuid4().hex
            prep = prepare_reference(str(audio_path), engine=engine_name,
                                     dst_dir=str(self.voice_dir), stem=ref_uid)
            stored_audio, ref_checks, ref_verdict = self._commit_reference(
                prep, audio_path, self.voice_dir, ref_uid, engine_name)
            fitted = ref_verdict == "fitted"
            fit_info = prep.get("fit") or {}
            fitted_note = (f"，入库窗 {fit_info['seconds']:.1f}秒"
                           if fitted and fit_info.get("seconds") else "")

            voice = voice_store.create(
                name=name,
                source="cloned",
                engine=engine_name,
                language=language,
                gender=gender,
                description=f"从参考音频克隆 (原始时长: {validation.get('duration', 0):.1f}秒"
                            f"{fitted_note}）",
                model_path=str(stored_audio),
                reference_audio_path=str(stored_audio),
                download_url="",
                # PY-323(a′): 语种档先按**声明**给（表单里的 language 指参考音频语种）。
                # 声明不可信时由离线工具 tag_language_tier.py 用声学复判改标。
                audio_language_tier=tier_from_language(language),
                meta={
                    # 指纹始终是**原件**的 —— fit 另存新文件，故 F-R4-4 幂等护栏不受影响
                    "reference_sha256": ref_sha256,
                    "transcript": transcript,
                    "duration_seconds": validation.get("duration"),
                    # 克隆期未加载模型（F-R4-4），参考音频在合成期由 _restore_voices 取回
                    "clone_mode": "deferred-model",
                    # ---- US-C3 ref_fitting 结果（审计口径：入库那一刻量到的） ----
                    "ref_checks": ref_checks,
                    "ref_verdict": ref_verdict,
                    "ref_metrics": prep.get("metrics"),
                    "ref_window": prep.get("window"),
                    "ref_fitted_seconds": (fit_info.get("seconds") if fitted else None),
                    "ref_source_path": (str(audio_path) if fitted else None),
                    "ref_original_duration_s": validation.get("duration"),
                    # PY-323(a′): 档的来源。basis=declared 表示只信了表单，
                    # 声学复判改标后由 tag_language_tier.py 覆写成 basis=acoustic。
                    "language_tier": {"tier": tier_from_language(language),
                                      "basis": "declared",
                                      "declared_language": language,
                                      "acoustic": None},
                },
            )
            new_voice_id, new_voice_name = voice.id, voice.name

        # 5. 只在引擎已经热着时登记；未热则留给合成期（绝不触发 load_model，也不占查重锁）
        engine_ready = self._register_if_loaded(engine_name, new_voice_id,
                                                stored_audio, transcript)
        logger.info(
            f"[Clone] 已建音色 {new_voice_id} name='{name}' ref={Path(stored_audio).name} "
            f"sha256={ref_sha256[:12]} verdict={ref_verdict} engine_ready={engine_ready} "
            f"(克隆期未等待模型加载，F-R4-4)")

        return {
            "voice_id": new_voice_id,
            "name": new_voice_name,
            "reference_sha256": ref_sha256,
            "deduplicated": False,
            "engine_ready": engine_ready,
            "clone_mode": "registered" if engine_ready else "deferred-model",
            "reference_audio_path": stored_audio,
            "ref_verdict": ref_verdict,
            "ref_checks": ref_checks,
        }

    # ---- US-C3: ref_fitting 的入库裁决 ----
    @staticmethod
    def duration_floor(engine_name: str = None) -> float:
        """该引擎档的参考音频时长地板；未知引擎退回类常量 `MIN_AUDIO_DURATION`。

        `ref_window_for` 的 min 为 0 的档（如 sample 回放档 0.3s 其实由它自己管）
        同样退回类常量 —— 克隆参考没有"0 秒也行"的道理。
        """
        try:
            from modules.voice_manager.ref_fitting import ref_window_for
            min_s = float(ref_window_for(engine_name)[0])
        except Exception:  # pragma: no cover - ref_fitting 不可用时不改变既有行为
            return float(VoiceCloner.MIN_AUDIO_DURATION)
        return min_s if min_s > 0 else float(VoiceCloner.MIN_AUDIO_DURATION)

    @staticmethod
    def _block_reason(prep: dict, verdict: str) -> str:
        """把阻断级 checks 拼成给用户的 400 文案（说清"缺什么"，不只说"不合格"）。"""
        blocked = [c for c in (prep.get("checks") or [])
                   if (not c.get("ok")) and c.get("severity") == "block"]
        why = "；".join(f"{c['name']}: {c['detail']}" for c in blocked) or "未知阻断项"
        fit = prep.get("fit") or {}
        hint = ""
        if verdict == "needs_longer_sample":
            hint = "（太短的参考**不做时间拉伸**——倍速/变调会把这个人的音色改成" \
                   "另一个人的听感；请录长一点重传）"
        elif fit.get("source_path"):
            hint = f"（原件 {fit['source_path']} 未改动）"
        return f"参考音频未通过入库检查 (verdict={verdict}): {why}{hint}"

    @staticmethod
    def _commit_reference(prep: dict, audio_path: str, voice_dir, ref_uid: str,
                          engine_name: str = None) -> tuple:
        """把 `prepare_reference` 的结果落成"真正入库的那串字节"。

        - `needs_longer_sample` / `unusable` → **抛 ValueError**（宁可退回让用户重录，
          绝不做时间拉伸把"这个人的音色"改成"另一个人的听感"）；
        - `fitted` → 裁好的 `ref_{uid}_fit.wav` 已在 voice_dir，对它**再量一次** checks
          （入库的到底是哪串字节，checks 就量哪串），仍不过则同样不入库；
        - `ok` → 原件 `copy2` 成 `ref_{uid}{ext}`（与升级前完全同一条路径）。
        Returns: (stored_path: str, checks: list, verdict: str)
        Raises: ValueError —— 阻断级（severity=="block"）未过，不入库。
        """
        from modules.voice_manager.ref_fitting import diagnose, load_mono

        verdict = prep.get("verdict")
        if verdict in ("needs_longer_sample", "unusable"):
            raise ValueError(VoiceCloner._block_reason(prep, verdict))
        if verdict not in ("ok", "fitted"):
            raise ValueError(VoiceCloner._block_reason(prep, verdict or "none"))

        fit = prep.get("fit") or {}
        if verdict == "fitted":
            stored = fit.get("path")
            if not stored or not os.path.isfile(stored):
                raise ValueError(VoiceCloner._block_reason(prep, "fitted"))
            try:
                x, sr = load_mono(stored)
                recheck = diagnose(None, engine_name, x=x, sr=sr)
            except Exception as e:  # pragma: no cover - 裁完读不出=环境坏了
                raise ValueError(f"ref_fitting 裁出的副本无法复验 "
                                 f"({type(e).__name__}: {e}) —— 不入库") from e
            blocked = [c for c in recheck["checks"]
                       if (not c.get("ok")) and c.get("severity") == "block"]
            if blocked or recheck["verdict"] in ("needs_longer_sample", "unusable"):
                raise ValueError(VoiceCloner._block_reason(
                    {"checks": recheck["checks"], "verdict": recheck["verdict"],
                     "metrics": recheck.get("metrics"), "fit": fit}, "fitted"))
            return str(stored), recheck["checks"], "fitted"

        # verdict == "ok"：原件按既有流程复制进音色目录
        import shutil
        ext = os.path.splitext(str(audio_path))[1] or ".wav"
        dst = Path(voice_dir) / f"ref_{ref_uid}{ext}"
        Path(voice_dir).mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(audio_path), str(dst))
        blocked = [c for c in (prep.get("checks") or [])
                   if (not c.get("ok")) and c.get("severity") == "block"]
        if blocked:
            raise ValueError(VoiceCloner._block_reason(prep, verdict))
        return str(dst), prep.get("checks") or [], verdict

    # ---- F-R4-4 辅助 ----
    @staticmethod
    def engine_is_loaded(engine_name: str) -> bool:
        """引擎模型是否已加载 —— 纯缓存查询，不实例化、更不加载模型。"""
        try:
            return bool(engine_registry.is_loaded(engine_name))
        except Exception:
            return False

    def find_existing_clone(self, name: str, engine_name: str,
                            ref_sha256: str) -> Optional[dict]:
        """按 (名字 + 引擎 + 参考音频 sha256) 查既有克隆行。

        只有**参考文件仍在盘上**的行才算命中（否则复用它等于交付一个坏音色）。
        旧数据 meta 里没有指纹（F-R4-4 之前建的行）→ 不命中，正常走新建。
        """
        from backend.models.base import get_session
        from backend.models.voice import VoiceProfile

        session = get_session()
        try:
            rows = (session.query(VoiceProfile)
                    .filter_by(source="cloned", name=name, engine=engine_name)
                    .all())
            for v in rows:
                meta = v.meta or {}
                if meta.get("reference_sha256") != ref_sha256:
                    continue
                if not v.reference_audio_path or not os.path.exists(v.reference_audio_path):
                    continue
                return {"id": v.id, "name": v.name,
                        "reference_audio_path": v.reference_audio_path,
                        "created_at": v.created_at.isoformat() if v.created_at else None}
        finally:
            session.close()
        return None

    def _register_if_loaded(self, engine_name: str, voice_id: str,
                            ref_path: str, transcript: str) -> bool:
        """引擎已在内存里热着才登记参考音频，返回是否登记成功。

        未加载时返回 False —— 这**不是**缺陷：`load_model()` 结束时 `_restore_voices()`
        会按 DB 行（source=cloned + reference_audio_path）重建音色表，
        合成期 `model.generate(reference_wav_path=...)` 照样吃到这条参考音频（PY-303 实证）。
        """
        if not self.engine_is_loaded(engine_name):
            return False
        engine = engine_registry.get(engine_name)
        if engine is None or not engine.is_loaded():
            return False
        try:
            engine.clone_voice(audio_path=ref_path, transcript=transcript,
                               voice_id=voice_id)
            return True
        except TypeError:
            # 该适配器无 voice_id 形参（indextts/mosstts）—— 不猜它的内部 id，
            # 交给 DB 恢复通道，保持返回语义诚实
            logger.warning(f"[Clone] 引擎 '{engine_name}' 的 clone_voice 不接受 voice_id，"
                           f"仅入库不登记（合成期由 DB 恢复）")
            return False
        except Exception as e:
            logger.warning(f"[Clone] 登记到已加载引擎 '{engine_name}' 失败(不影响入库): "
                           f"{type(e).__name__}: {e}")
            return False
