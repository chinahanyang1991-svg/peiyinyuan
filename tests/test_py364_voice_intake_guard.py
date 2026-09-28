"""PY-364：本地音色池入库护栏 + Kokoro 错行修正脚本（红→绿）。

现场（现采自生产库 data/peiyinyuan.db，只读）：
    id=c7b3ebd6-2b20-4ab8-9555-bb3971ecdec8  开放音色-Kokoro女声(af_heart)
    source='downloaded'  engine='voxcpm'  reference_audio_path=NULL
    meta.origin = {source: huggingface, repo: hexgrad/Kokoro-82M,
                   file_path: samples/af_heart_0.wav}
    981 枚 downloaded/voxcpm 里唯一没有参考路径的一枚；其样本 wav 也已不在盘上。
它可挑可绑，但 voxcpm 是零样本克隆引擎、只照参考音频说话
（voxcpm_adapter.py::_restore_voices 的 `and v.reference_audio_path` 把它挡在装载外），
挑它 = 拿到 voxcpm 默认声。

两层修法都要被本文件锁住：
  1) tools/resource_scout/import_to_voices.py 的 intake_gate / build_profiles：
     要写成需要参考音频的引擎（voxcpm/openvoice/gptsovits）的下载类音色，
     必须 ① 本地参考 wav 真在盘上且路径已写入；② 来源仓库不属于别的引擎的
     模型仓库 —— 否则拒登记并打印理由（不许静默、不许照样入池）。
     同时**不得**过度拦截：有在盘 wav、仓库不冲突的正常行必须照常登记。
  2) tools/fix_py364_kokoro_row.py：默认 dry-run 不写；--apply 先备份原行再
     改标（engine: voxcpm→kokoro + meta.py364 印记）；幂等；--rollback 逐字段还原。

夹具全部走 tmp_path 临时库/临时文件，绝不碰生产库。
"""
import json
import os
import sqlite3
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine  # noqa: E402

from backend.models.voice import VoiceProfile  # noqa: E402


def _load(mod_name, rel_path):
    import importlib.util
    spec = importlib.util.spec_from_file_location(mod_name, ROOT / rel_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


itv = _load("itv_py364", "tools/resource_scout/import_to_voices.py")
fix = _load("fix_py364", "tools/fix_py364_kokoro_row.py")

TARGET_ID = fix.TARGET_ID  # c7b3ebd6-2b20-4ab8-9555-bb3971ecdec8

KOKORO_URL = "https://huggingface.co/hexgrad/Kokoro-82M/resolve/main/samples/af_heart_0.wav"


# ---------------------------------------------------------------- 护栏原语 ---

def test_intake_gate_rejects_missing_local_wav(tmp_path):
    """要写成 voxcpm 却没有本地参考 wav（路径空 / 指向不存在的文件）→ 拒。"""
    assert itv.intake_gate("voxcpm", "", {}) != None            # noqa: E711 空路径
    missing = str(tmp_path / "nope.wav")
    assert itv.intake_gate("voxcpm", missing, {}) != None       # noqa: E711 盘上无此文件
    tiny = tmp_path / "tiny.wav"
    tiny.write_bytes(b"RIFF" + b"\x00" * 8)
    assert itv.intake_gate("voxcpm", str(tiny), {}) != None     # noqa: E711 占位空文件


def test_intake_gate_rejects_foreign_engine_repo():
    """仓库指向别的引擎的模型仓（hexgrad/Kokoro-82M → kokoro）却登记成 voxcpm → 拒。"""
    reason = itv.intake_gate("voxcpm", None, {}, download_url=KOKORO_URL)
    assert reason is not None and "kokoro" in reason.lower()
    # 即便参考音频在盘上（这里用 None 跳过该项检查），仓库归属冲突也必须单独被拒
    assert itv.intake_gate("voxcpm", None, {"origin": {"repo": "hexgrad/Kokoro-82M"}}) is not None
    # 引擎与仓库一致（kokoro 档收 Kokoro 仓样本、且不要求参考音频）→ 放行
    assert itv.intake_gate("kokoro", None, {"origin": {"repo": "hexgrad/Kokoro-82M"}}) is None


def test_intake_gate_allows_good_downloaded_voice(tmp_path):
    """防过度拦截：在盘真 wav + 普通数据集 URL + voxcpm → 放行。"""
    wav = tmp_path / "ref.wav"
    wav.write_bytes(b"RIFF" + b"\x00" * 4090 + b"\x64\x00")
    assert itv.intake_gate("voxcpm", str(wav),
                           {"source_url": "https://example.org/corpora/zh-female"}) is None


# ------------------------------------------------- build_profiles 登记路径 ---

def _mk_args(engine="voxcpm"):
    return SimpleNamespace(
        gender=None, language=None, style="all", multiple_samples=False,
        samples_per_voice=3, one_per_voice=True, limit=0,
        namespace=uuid.UUID("6f6f6f6f-0000-4000-8000-0e57c0000001"),
        source="downloaded", engine=engine)


def _mk_row(tmp_path, i, path, source_url):
    wav = tmp_path / ("s%d.wav" % i)
    if path == "__ondisk__":
        wav.write_bytes(b"RIFF" + b"\x00" * 4090 + b"\x64\x00")
        path = str(wav)
    return {
        "uid": "u%d" % i, "voice_key": "k%d" % i, "source_key": "src_zh",
        "speaker_id": "spk%d" % i, "gender": "female", "age_band": "adult",
        "style": "叙述", "emotion": "", "language": "zh", "license": "CC0-1.0",
        "path": path, "path_22k": path if path.startswith(str(tmp_path)) else "",
        "duration_s": 6.2, "rms": 0.05, "peak": 0.6, "sample_rate": 48000,
        "f0_median": 180.0, "attr_source": "auto", "verified_at": "2026-09-01",
        "source_url": source_url, "page_url": source_url,
        "attribution": "", "resource_license": "CC0-1.0",
        "resource_source_url": source_url,
    }


class _FakeDB:
    def __init__(self, rows):
        self._rows = rows

    def rows(self, sql, j):
        return self._rows


def test_build_profiles_rejects_missing_wav_and_prints_reason(tmp_path, capsys):
    """无本地 wav 的行必须被拒登记、且打印明确理由（不许静默跳过）。"""
    rows = [
        _mk_row(tmp_path, 1, "__ondisk__", "https://example.org/corpora/a"),
        _mk_row(tmp_path, 2, str(tmp_path / "gone.wav"), "https://example.org/corpora/b"),
    ]
    picked, rejected = itv.build_profiles(_FakeDB(rows), _mk_args())
    assert len(picked) == 1 and len(rejected) == 1
    assert rejected[0][0]["name"].startswith("开放音色")
    out = capsys.readouterr().out
    assert "拒绝登记" in out and "PY-364" in out


def test_build_profiles_rejects_foreign_repo_despite_ondisk_wav(tmp_path):
    """wav 在盘上，但仓库是别的引擎的模型仓（Kokoro 复发场景）→ 照样拒。"""
    rows = [_mk_row(tmp_path, 1, "__ondisk__", KOKORO_URL)]
    picked, rejected = itv.build_profiles(_FakeDB(rows), _mk_args())
    assert picked == [] and len(rejected) == 1
    assert "kokoro" in rejected[0][1].lower()


def test_build_profiles_keeps_normal_downloaded_voice(tmp_path):
    """防过度拦截：在盘 wav + 无冲突仓库的 980 枚同构行必须照常登记。"""
    rows = [_mk_row(tmp_path, i, "__ondisk__", "https://example.org/corpora/c%d" % i)
            for i in (1, 2, 3)]
    picked, rejected = itv.build_profiles(_FakeDB(rows), _mk_args())
    assert len(picked) == 3 and rejected == []


# --------------------------------------------------------- 修正脚本夹具 ---

def _prod_like_row():
    """现采生产行的最小复形（字段与 2026-09-27 现采逐一对齐）。"""
    return dict(
        id=TARGET_ID, name="开放音色-Kokoro女声(af_heart)", source="downloaded",
        category="human", engine="voxcpm", language="zh", gender=None,
        age_group=None, style_tags=[], model_path="",
        sample_audio_path=str(Path("data/voices/online") / (TARGET_ID + ".wav")),
        description="来自 huggingface: hexgrad/Kokoro-82M",
        reference_audio_path=None, is_downloaded=1,
        download_url="https://huggingface.co/hexgrad/Kokoro-82M",
        meta={"origin": {"source": "huggingface", "repo": "hexgrad/Kokoro-82M",
                         "file_path": "samples/af_heart_0.wav"},
              "quality": {"ok": True}, "duration_s": 4.95,
              "registered_at": "2026-09-22T11:43:39"},
        seed=None, audio_language_tier=None)


@pytest.fixture
def voice_db(tmp_path):
    """临时 voice_profiles 库（ORM 建表 + ORM 插行，序列化口径与生产一致）。"""
    db = tmp_path / "copy.db"
    engine = create_engine(f"sqlite:///{db.as_posix()}")
    VoiceProfile.__table__.create(bind=engine)
    from sqlalchemy.orm import sessionmaker
    s = sessionmaker(bind=engine)()
    s.add(VoiceProfile(**_prod_like_row()))
    s.commit()
    s.close()
    engine.dispose()
    return db


def _raw_row(db, vid=TARGET_ID):
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        r = conn.execute("SELECT * FROM voice_profiles WHERE id=?", (vid,)).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def test_fix_dry_run_writes_nothing(voice_db, tmp_path, capsys):
    before = _raw_row(voice_db)
    bak = tmp_path / "bak.json"
    rc = fix.main(["--db", str(voice_db), "--backup", str(bak)])
    assert rc == 0
    assert _raw_row(voice_db) == before          # 一行未动
    assert not bak.exists()                      # 备份也没写
    out = capsys.readouterr().out
    assert "dry-run" in out.lower() and "voxcpm" in out


def test_fix_apply_marks_and_backs_up(voice_db, tmp_path, capsys):
    before = _raw_row(voice_db)
    bak = tmp_path / "bak.json"
    rc = fix.main(["--db", str(voice_db), "--apply", "--backup", str(bak)])
    assert rc == 0
    after = _raw_row(voice_db)
    assert after["engine"] == "kokoro"           # 不再冒充 voxcpm 可选音色
    assert "py364" in json.loads(after["meta"])
    assert after["reference_audio_path"] is None # 不撒谎补路径（文件已不在盘上）
    saved = json.loads(bak.read_text(encoding="utf-8"))
    assert saved["row"]["engine"] == "voxcpm"
    assert saved["row"]["meta"] == before["meta"]  # 备份 = 完整原文（含逐字 meta 文本）


def test_fix_apply_idempotent(voice_db, tmp_path, capsys):
    bak = tmp_path / "bak.json"
    assert fix.main(["--db", str(voice_db), "--apply", "--backup", str(bak)]) == 0
    capsys.readouterr()
    snap = _raw_row(voice_db)
    rc = fix.main(["--db", str(voice_db), "--apply", "--backup", str(bak)])
    out = capsys.readouterr().out
    assert rc == 0 and "已经是目标态" in out
    assert _raw_row(voice_db) == snap            # 第二跑逐字段不变（含 updated_at）


def test_fix_rollback_restores_every_field(voice_db, tmp_path):
    before = _raw_row(voice_db)
    bak = tmp_path / "bak.json"
    assert fix.main(["--db", str(voice_db), "--apply", "--backup", str(bak)]) == 0
    assert _raw_row(voice_db) != before
    assert fix.main(["--db", str(voice_db), "--rollback", "--backup", str(bak)]) == 0
    assert _raw_row(voice_db) == before          # 逐字段（逐字）还原


def test_fixed_row_leaves_voxcpm_selectable_pool(voice_db, tmp_path):
    """理想判据：修正后这一枚不再被当作 voxcpm 可选音色，且护栏会永久拒它。"""
    conn = sqlite3.connect(voice_db)
    q = ("SELECT id FROM voice_profiles "
         "WHERE engine='voxcpm' AND source='downloaded' AND id=?")
    assert conn.execute(q, (TARGET_ID,)).fetchone()          # 修前在池里
    fix.main(["--db", str(voice_db), "--apply",
              "--backup", str(tmp_path / "bak.json")])
    assert conn.execute(q, (TARGET_ID,)).fetchone() is None  # 修后出池
    row = conn.execute("SELECT * FROM voice_profiles WHERE id=?",
                       (TARGET_ID,)).fetchone()
    cols = [d[1] for d in conn.execute("PRAGMA table_info(voice_profiles)")]
    r = dict(zip(cols, row))
    conn.close()
    # 以 voxcpm 身份再过一遍入库护栏：参考路径缺失 + 仓库归属冲突，双重拒绝
    assert itv.intake_gate(r["engine"], r["reference_audio_path"],
                           json.loads(r["meta"]),
                           download_url=r["download_url"]) is None   # kokoro 档不要求参考音频
    assert itv.intake_gate("voxcpm", r["reference_audio_path"],
                           json.loads(r["meta"]),
                           download_url=r["download_url"]) is not None
