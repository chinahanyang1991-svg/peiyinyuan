"""
mixer: BGM/音效素材库 + 章节混音器（批次 A 纯新增）

设计依据: docs/superpowers/specs/2026-09-20-multi-cast-audiobook-design.md 4.2

批次 B 在 P8 _stage_postprocess 末尾的调用形状:
    mixer = AudioMix(sample_rate=post.sample_rate, post=post)
    settings = AudioMix.settings_from_project(project.global_settings, chapter_index)
    audio = mixer.mix(segments, settings)      # settings 为空 -> 与现状逐样本一致
"""

from modules.mixer.audio_mix import (
    CEILING_DB,
    DEFAULT_DUCK_DB,
    DEFAULT_TARGET_LUFS,
    AudioMix,
    VocalSegment,
)
from modules.mixer.bgm_store import (
    BUILTIN_BGM,
    BUILTIN_SFX,
    LIBRARY_SR,
    PLACEHOLDER_TAG,
    BgmStore,
)

__all__ = [
    "AudioMix",
    "VocalSegment",
    "BgmStore",
    "BUILTIN_BGM",
    "BUILTIN_SFX",
    "LIBRARY_SR",
    "PLACEHOLDER_TAG",
    "CEILING_DB",
    "DEFAULT_DUCK_DB",
    "DEFAULT_TARGET_LUFS",
]
