"""PY-364 同源缺口的另一半：在线音色库**下载登记**路径的入库门（红→绿）。

定罪现场（与 tools/resource_scout/import_to_voices.py::intake_gate 同一判据，
但以磁盘为准）：
    backend/services/voice_online.py::_register_voice() 是在线下载唯一写库点
    （voice_online.py:415 create + :430 update），现状**只写 sample_audio_path，
    从不写 reference_audio_path**；engine 是参数透传（route voices.py:446 与
    download_and_register voice_online.py:434 的默认值都是 "voxcpm"）。
    于是用户点下载 hexgrad/Kokoro-82M 样本、引擎留默认 voxcpm，就再造一枚
    c7b3ebd6 那样的行：voxcpm（零样本克隆档）+ 参考音频 NULL = 挑了等于没挑。

本文件锁四件事（全部 tmp_path + 临时 sqlite + 打桩网络/下载，不打生产库、不打 :8000）：
  ① 下载"成功"但 wav 不在盘上 → 拒登记、返回可见原因（status/reason/message），
     且**不留下任何半截行**；
  ② 正常下载（普通语料仓 + voxcpm）→ 登记成功，且 reference_audio_path **真被写上**
     且文件在盘（现状不写 → 这一枚即第二层缺陷定罪）；
  ③ 已有的正常行在同一次下载中逐字段不漂（下载路径只碰自己的新行）；
  ④ 行写了却没落上 reference_audio_path（update 被静默吞掉）→ 事后门拒登记，
     行删除、文件清理，不留孤儿。
另锁仓库归属冲突（PY-364 复发形态）：Kokoro 模型仓样本想登记成 voxcpm → 拒。
"""
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import backend.models.base as base  # noqa: E402
from backend.services import voice_online  # noqa: E402
from backend.services.voice_service import voice_service as svc  # noqa: E402
from modules.tts_engine.engine_registry import engine_registry  # noqa: E402
from modules.voice_manager import audio_quality  # noqa: E402
from modules.voice_manager.voice_crawler import online_net  # noqa: E402
from modules.voice_manager.voice_store import voice_store  # noqa: E402

KOKORO_REPO = "hexgrad/Kokoro-82M"          # 别的引擎的模型仓（PY-364 现场）
CORPUS_REPO = "open-zh-lab/female-narration"  # 普通语料仓：克隆档可吃


def _wav_bytes(n=9000) -> bytes:
    return b"RIFF" + (n - 8).to_bytes(4, "little") + b"WAVE" + b"\x00" * (n - 12)


class _Harness:
    """下载/解码打桩：leave_wav=False 模拟"下载声称成功但 wav 不在盘"。"""

    def __init__(self):
        self.leave_wav = True

    def download_sample(self, repo, raw_path, file_path=None, max_bytes=None,
                        deadline_s=None):
        Path(raw_path).write_bytes(_wav_bytes())
        return {"ok": True, "file_path": file_path or "samples/a.wav",
                "candidates": ["samples/a.wav"],
                "url": "https://hf-mirror.test/%s" % repo, "http_code": 200,
                "elapsed_ms": 3.0, "language": "zh", "description": ""}

    def probe_audio(self, path):
        p = Path(path)
        exists = p.is_file()
        # 口径对齐真实现（online_net.probe_audio）：magic 是 hex 字符串，meta 可 JSON 化
        return {"path": str(p), "bytes": p.stat().st_size if exists else 0,
                "magic": "52494646" if exists else "",
                "magic_hint": "wav" if exists else "",
                "format": "PCM_16", "sample_rate": 48000, "channels": 1,
                "duration_s": 5.0 if exists else 0.0}

    def normalize_to_wav(self, src, dst, target_rate=48000, max_seconds=None):
        if self.leave_wav:
            Path(dst).write_bytes(_wav_bytes())
        return {"duration_s": 5.0, "sample_rate": 48000, "channels": 1}


def _ids(db: Path) -> set:
    conn = sqlite3.connect(db)
    try:
        return {r[0] for r in conn.execute("SELECT id FROM voice_profiles")}
    finally:
        conn.close()


def _row(db: Path, vid: str):
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        r = conn.execute("SELECT * FROM voice_profiles WHERE id=?", (vid,)).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


@pytest.fixture
def env(tmp_path, monkeypatch):
    db = tmp_path / "g2.db"
    online = tmp_path / "online"
    online.mkdir()
    # 临时库绑定：get_engine/get_session 都经模块级 get_db_path + 全局单例
    monkeypatch.setattr(base, "get_db_path", lambda: str(db))
    monkeypatch.setattr(base, "_engine", None)
    monkeypatch.setattr(base, "_SessionLocal", None)
    monkeypatch.setenv("VOICE_ONLINE_DIR", str(online))

    class _Eng:
        @staticmethod
        def unavailable_reason():
            return None

    monkeypatch.setattr(engine_registry, "known_engine_names", lambda: {"voxcpm", "kokoro"})
    monkeypatch.setattr(engine_registry, "get", lambda name=None: _Eng())

    h = _Harness()
    monkeypatch.setattr(svc.hf_crawler, "download_sample", h.download_sample)
    monkeypatch.setattr(svc.github_crawler, "download_sample", h.download_sample)
    monkeypatch.setattr(online_net, "HAS_HTTPX", True)
    monkeypatch.setattr(online_net, "probe_audio", h.probe_audio)
    monkeypatch.setattr(online_net, "normalize_to_wav", h.normalize_to_wav)
    monkeypatch.setattr(audio_quality, "assess_file",
                        lambda path: ({"ok": True, "rms": 0.05, "peak": 0.6}, None))

    # 首次触碰库（builtin/edge 播种）后取基线 id 集合
    voice_store.count()
    yield SimpleNamespace(db=db, h=h, tmp=tmp_path, online=online,
                          base_ids=_ids(db))


def _dl(repo, engine="voxcpm"):
    return voice_online.download_and_register(
        repo, engine=engine, source="huggingface",
        file_path="samples/a.wav", name="G2 测试音色")


# ------------------------------------------------------------------ ① 拒登记 ---

def test_wav_missing_on_disk_is_rejected_with_visible_reason(env):
    """下载链路全"成功"，但最终 wav 不在盘上 → 克隆档引擎必须拒登记，且原因可见。"""
    env.h.leave_wav = False
    res = _dl(CORPUS_REPO, engine="voxcpm")
    assert res.get("ok") is False, res
    assert res.get("reason") == "intake_gate_failed", res
    assert "PY-364" in res.get("message", "")
    assert res.get("status") == 422, "路由层要能据此给前端结构化 422（照 _fail 口径）"
    # 不留半截行、不留孤儿文件
    assert _ids(env.db) == env.base_ids
    assert list(env.online.glob("*.wav")) == []
    assert list(env.online.glob("*.part")) == []


def test_kokoro_repo_refused_as_voxcpm_even_with_wav_on_disk(env):
    """PY-364 复发形态：Kokoro 模型仓样本 + 默认 engine=voxcpm，wav 在盘也拒（仓库归属冲突）。"""
    res = _dl(KOKORO_REPO, engine="voxcpm")
    assert res.get("ok") is False, res
    assert res.get("reason") == "intake_gate_failed", res
    assert "kokoro" in res.get("message", "").lower()
    assert _ids(env.db) == env.base_ids


# ------------------------------------------------------------------ ② 正常登记 ---

def test_good_download_registers_reference_audio_path(env):
    """第二层缺陷定罪：现状 _register_voice 从不写 reference_audio_path。
    修好后：克隆档引擎的下载行必须把在盘 wav 同时写成试听**和**参考音频。"""
    res = _dl(CORPUS_REPO, engine="voxcpm")
    assert res.get("ok") is True, res
    row = _row(env.db, res["voice_id"])
    assert row is not None
    assert row["engine"] == "voxcpm" and row["source"] == "downloaded"
    ref = row["reference_audio_path"]
    assert ref, "reference_audio_path 必须真被写上（否则这枚又是挑了等于没挑）"
    assert Path(ref).is_file() and Path(ref).stat().st_size >= 1024
    assert row["sample_audio_path"] == ref == res["path"]
    assert _ids(env.db) - env.base_ids == {res["voice_id"]}


def test_preset_engine_kokoro_download_still_registers(env):
    """防过度拦截：kokoro 是预设说话人档，不吃参考音频也照常登记（M2 变异在这红）。"""
    res = _dl(KOKORO_REPO, engine="kokoro")
    assert res.get("ok") is True, res
    row = _row(env.db, res["voice_id"])
    assert row is not None and row["engine"] == "kokoro"
    assert _ids(env.db) - env.base_ids == {res["voice_id"]}


# ------------------------------------------------------------------ ③ 不漂 ---

def test_existing_good_row_does_not_drift(env):
    """已有正常行（在盘 ref 的 downloaded/voxcpm）在同一次下载前后逐字段一致。"""
    ref_wav = env.tmp / "old_ref.wav"
    ref_wav.write_bytes(_wav_bytes())
    old = voice_store.create(name="PY-364 修正后的正常行", source="downloaded",
                             engine="voxcpm", reference_audio_path=str(ref_wav),
                             sample_audio_path=str(ref_wav))
    before = _row(env.db, old.id)
    res = _dl(CORPUS_REPO, engine="voxcpm")
    assert res.get("ok") is True, res
    assert _row(env.db, old.id) == before  # 逐字段（含 updated_at / meta 原文）


# ------------------------------------------------------------------ ④ 事后门 ---

def test_update_dropping_reference_leaves_no_half_row(env):
    """行没落上 reference_audio_path（update 被吞）→ 事后门拒登记：删行、清文件。"""
    orig_update = voice_store.update

    def _drop_ref(voice_id, **kw):
        kw.pop("reference_audio_path", None)
        return orig_update(voice_id, **kw)

    monkeypatched = _drop_ref
    voice_store.update = monkeypatched
    try:
        res = _dl(CORPUS_REPO, engine="voxcpm")
    finally:
        voice_store.update = orig_update
    assert res.get("ok") is False, res
    assert res.get("reason") == "intake_gate_failed", res
    assert "reference_audio_path" in res.get("message", "")
    assert _ids(env.db) == env.base_ids, "拒登记不得留下半截行"
    assert list(env.online.glob("*.wav")) == [], "回滚后不得留下无主 wav"
