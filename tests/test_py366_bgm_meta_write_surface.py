"""
PY-366 回归测试①：占位素材重生不得改写受版本控制的 `data/bgm/meta.json`。

根因（主智能体已实测定罪）：
  `modules/mixer/bgm_store.py` 的 `ensure_builtin()` 在占位 wav 缺失时会重新生成素材，
  于是 `created` 非空 -> 走 `save_meta()`；而 `save_meta()` 每次盖 `updated_at=当下`、
  每条占位记录每次盖 `created_at=当下`，两者都落在**受版本控制**的 `meta.json` 里。
  结果：每跑一次全量回归门，检出树里的 `data/bgm/meta.json` 必然漂移，
  "干净工作树才能跑门禁"这个不变式永远不成立。

本文件的断言都刻意做成"退回旧行为就会红"：
  * 用固定假时钟把两次调用的"当下"拉开到不同值 —— 旧实现会把 `created_at`/`updated_at`
    改写成第二个值，md5 因此变化（M1/M2 变异各打红一条以上）。
  * 反例约束：用户上传（`placeholder: False`）与 `force=True` 是真数据变更，必须照旧写盘。

测试全程跑在 `tmp_path` 里，绝不在仓库 `data/` 造素材。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

import modules.mixer.bgm_store as bgm_store_mod
from modules.mixer.bgm_store import BgmStore, LIBRARY_SR

STAMP_FIRST = "2020-01-01T00:00:00"
STAMP_SECOND = "2099-12-31T23:59:59"


class _FixedClock:
    """替掉 bgm_store 里的 `time`：只用到 strftime，固定返回给定时刻。"""

    def __init__(self, stamp: str):
        self.stamp = stamp

    def strftime(self, fmt: str, *args) -> str:  # noqa: D102
        return self.stamp


@pytest.fixture
def clock(monkeypatch):
    def _set(stamp: str):
        monkeypatch.setattr(bgm_store_mod, "time", _FixedClock(stamp))
    return _set


def _md5(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()


def _created_at_map(meta: dict) -> dict:
    out = {}
    for bucket in ("bgm", "sfx"):
        for row in meta.get(bucket, []):
            out[row["id"]] = row.get("created_at")
    return out


def _write_source_wav(path: Path, seconds: float = 0.2, sr: int = 44100) -> Path:
    t = np.arange(int(seconds * sr), dtype=np.float32) / sr
    tone = (np.sin(2 * np.pi * 220 * t) * 0.3).astype(np.float32)
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), np.stack([tone, tone], axis=1), sr, subtype="PCM_16")
    return path


# ---------------------------------------------------------------------------
# a. 占位素材缺失后重生：created_at 沿用首次时刻，内容语义相同则逐字节不变
# ---------------------------------------------------------------------------
class TestPlaceholderRegenerationIsInert:

    def test_first_call_generates_assets_and_meta(self, tmp_path, clock):
        clock(STAMP_FIRST)
        store = BgmStore(root=tmp_path / "data")
        assert not store.meta_path.exists()

        result = store.ensure_builtin()

        # 5 枚占位素材（3 BGM + 2 SFX）第一次全部新生成
        assert len(result["created"]) == 5, result
        assert result["existing"] == [], result
        assert store.meta_path.exists(), "首次必须写出 meta.json"
        for asset_id in ("bgm_warm_pad", "bgm_suspense_pad", "bgm_daylight_pad"):
            assert (store.bgm_dir / f"{asset_id}.wav").exists()
        for asset_id in ("sfx_chapter_start", "sfx_chapter_end"):
            assert (store.sfx_dir / f"{asset_id}.wav").exists()

        meta = json.loads(store.meta_path.read_text(encoding="utf-8"))
        created = _created_at_map(meta)
        assert set(created) == {
            "bgm_warm_pad", "bgm_suspense_pad", "bgm_daylight_pad",
            "sfx_chapter_start", "sfx_chapter_end"}
        assert all(v == STAMP_FIRST for v in created.values())
        assert meta["updated_at"] == STAMP_FIRST

    def test_second_call_after_wav_deleted_leaves_meta_untouched(
            self, tmp_path, clock):
        clock(STAMP_FIRST)
        store = BgmStore(root=tmp_path / "data")
        store.ensure_builtin()

        meta_path = store.meta_path
        bytes_before = meta_path.read_bytes()
        md5_before = _md5(meta_path)
        created_before = _created_at_map(json.loads(bytes_before.decode("utf-8")))

        # 模拟"占位素材缺失"（新检出工作树里 wav 被 gitignore 挡住）
        removed = 0
        for wav in list(store.bgm_dir.glob("*.wav")) + list(store.sfx_dir.glob("*.wav")):
            wav.unlink()
            removed += 1
        assert removed == 5, "前置条件：5 枚占位 wav 都要被删掉才会触发改写路径"

        # 换一只"当下"时刻：旧实现会把 created_at / updated_at 都盖成它
        clock(STAMP_SECOND)
        result = store.ensure_builtin()

        assert len(result["created"]) == 5, \
            "前置条件：素材确实重新生成了（否则等于没测这条路径）"
        for wav_name in ("bgm_warm_pad.wav", "bgm_suspense_pad.wav",
                         "bgm_daylight_pad.wav"):
            assert (store.bgm_dir / wav_name).exists()

        meta_after = json.loads(meta_path.read_text(encoding="utf-8"))
        assert _created_at_map(meta_after) == created_before, \
            "created_at 含义是'首次入池时刻'，重生占位素材不得改写它"
        assert all(v == STAMP_FIRST for v in _created_at_map(meta_after).values())

        # 除 updated_at 外语义相同 -> 直接跳过写盘：逐字节不变
        assert meta_path.read_bytes() == bytes_before, \
            "语义未变的 meta.json 必须一个字节都不动（否则检出树每跑一次门就脏一次）"
        assert _md5(meta_path) == md5_before
        assert meta_after["updated_at"] == STAMP_FIRST

        assert not (store.bgm_dir / "meta.json.tmp").exists()
        assert list(store.bgm_dir.glob("*.tmp")) == [], "不得残留 .json.tmp 写入痕迹"

    def test_direct_save_meta_of_unchanged_content_skips_write(
            self, tmp_path, clock):
        clock(STAMP_FIRST)
        store = BgmStore(root=tmp_path / "data")
        store.ensure_builtin()
        before = store.meta_path.read_bytes()

        clock(STAMP_SECOND)
        meta = store.load_meta()
        wrote = store.save_meta(meta)

        assert wrote is False, "内容语义相同时 save_meta 应报告'没写'"
        assert store.meta_path.read_bytes() == before
        assert list(store.bgm_dir.glob("*.tmp")) == []

    def test_key_order_difference_does_not_trigger_a_write(self, tmp_path, clock):
        """规范化比较：同一内容、key 顺序不同 -> 仍视为未变，不写盘。"""
        clock(STAMP_FIRST)
        store = BgmStore(root=tmp_path / "data")
        store.ensure_builtin()
        before = store.meta_path.read_bytes()

        meta = json.loads(before.decode("utf-8"))
        # 只改 key 顺序（内容一模一样）：手工编辑过的 meta.json 就是这个形状
        meta["bgm"] = [dict(reversed(list(row.items()))) for row in meta["bgm"]]
        meta["updated_at"] = STAMP_SECOND

        clock(STAMP_SECOND)
        wrote = store.save_meta(meta)
        assert wrote is False, "key 顺序差异不得被误判成内容变更"
        assert store.meta_path.read_bytes() == before


# ---------------------------------------------------------------------------
# b. 用户上传/转码那条路径：真数据变更必须照旧写盘
# ---------------------------------------------------------------------------
class TestUploadPathStillWrites:

    def test_add_audio_file_really_persists(self, tmp_path, clock):
        clock(STAMP_FIRST)
        store = BgmStore(root=tmp_path / "data")
        store.ensure_builtin()
        meta_path = store.meta_path
        bytes_before = meta_path.read_bytes()
        ids_before = {r["id"] for r in store.load_meta()["bgm"]}

        src = _write_source_wav(tmp_path / "src" / "userasset.wav")
        clock(STAMP_SECOND)
        record = store.add_audio_file(src, name="PY-366 上传素材", moods=["测试"])

        assert record["placeholder"] is False, record
        assert record["created_at"] == STAMP_SECOND

        assert meta_path.read_bytes() != bytes_before, \
            "用户上传是真数据变更，必须真的写盘（不许被语义比较静默掉）"
        disk = json.loads(meta_path.read_text(encoding="utf-8"))
        assert record["id"] in {r["id"] for r in disk["bgm"]}
        assert record["id"] not in ids_before
        row = next(r for r in disk["bgm"] if r["id"] == record["id"])
        assert row["name"] == "PY-366 上传素材"
        assert row["placeholder"] is False
        assert disk["updated_at"] == STAMP_SECOND, "写盘时 updated_at 同步更新"
        assert not (store.bgm_dir / "meta.json.tmp").exists()

        # 占位素材的 created_at 不受上传影响
        created = _created_at_map(disk)
        assert created["bgm_warm_pad"] == STAMP_FIRST

    def test_add_audio_file_sfx_bucket_persists_too(self, tmp_path, clock):
        clock(STAMP_FIRST)
        store = BgmStore(root=tmp_path / "data")
        store.ensure_builtin()
        bytes_before = store.meta_path.read_bytes()
        src = _write_source_wav(tmp_path / "src" / "cue.wav")

        clock(STAMP_SECOND)
        rec = store.add_audio_file(src, name="PY-366 音效", kind="sfx")
        assert rec["placeholder"] is False
        assert store.meta_path.read_bytes() != bytes_before
        disk = json.loads(store.meta_path.read_text(encoding="utf-8"))
        assert rec["id"] in {r["id"] for r in disk["sfx"]}
        assert disk["updated_at"] == STAMP_SECOND


# ---------------------------------------------------------------------------
# b'. force=True 是真指令：照旧无条件写盘，但 created_at 仍是首次时刻
# ---------------------------------------------------------------------------
class TestForceStillWrites:

    def test_ensure_builtin_force_rewrites_but_keeps_created_at(
            self, tmp_path, clock):
        clock(STAMP_FIRST)
        store = BgmStore(root=tmp_path / "data")
        store.ensure_builtin()
        bytes_before = store.meta_path.read_bytes()

        clock(STAMP_SECOND)
        result = store.ensure_builtin(force=True)

        assert len(result["created"]) == 5, result
        meta_after = json.loads(store.meta_path.read_text(encoding="utf-8"))
        assert store.meta_path.read_bytes() != bytes_before, \
            "force=True 必须照旧写盘"
        assert meta_after["updated_at"] == STAMP_SECOND
        assert all(v == STAMP_FIRST for v in _created_at_map(meta_after).values()), \
            "force 重写素材也不该抹掉'首次入池'时刻"
        assert not (store.bgm_dir / "meta.json.tmp").exists()

    def test_ensure_builtin_with_wavs_present_is_a_no_op(self, tmp_path, clock):
        clock(STAMP_FIRST)
        store = BgmStore(root=tmp_path / "data")
        store.ensure_builtin()
        bytes_before = store.meta_path.read_bytes()

        clock(STAMP_SECOND)
        result = store.ensure_builtin()
        assert result["created"] == [] and len(result["existing"]) == 5, result
        assert store.meta_path.read_bytes() == bytes_before
