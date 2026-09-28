"""
cast_analyzer: 多人有声书角色分析（批次 A 纯新增）

- cast_extractor: 全书 -> 角色候选表（流式逐章）
- cast_profiler : 候选 + 样句 -> 性别/年龄段/身份/性格（LLM 批量 + 规则全表兜底）
- voice_matcher : 角色画像 + VoiceProfile -> 音色推荐（纯函数打分 40/30/20/10）

批次 B 接线示例（本包不含 IO，不依赖 backend）:
    extractor = CastExtractor()
    for idx, chapter in enumerate(chapters):        # 一次只在内存里持有一章
        extractor.process_chapter(chapter.content, idx)
    cast = CastProfiler().profile(extractor.finalize())
    suggestions = voice_matcher.suggest(cast, voice_profiles)
"""

from modules.cast_analyzer.cast_extractor import (
    NARRATOR,
    CastCandidate,
    CastExtractor,
)
from modules.cast_analyzer.cast_profiler import (
    CastProfiler,
    profiles_to_role_extra_data,
)
from modules.cast_analyzer import voice_matcher
from modules.cast_analyzer.voice_matcher import (
    auto_assign,
    rank_voices,
    score_voice,
    suggest,
)

__all__ = [
    "CastExtractor",
    "CastCandidate",
    "CastProfiler",
    "NARRATOR",
    "profiles_to_role_extra_data",
    "voice_matcher",
    "suggest",
    "score_voice",
    "rank_voices",
    "auto_assign",
]
