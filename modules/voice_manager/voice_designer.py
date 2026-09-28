"""
语音设计引擎
基于 VoxCPM 的文本描述生成音色
"""

import re
import uuid
from typing import Optional

from modules.tts_engine.engine_registry import engine_registry
from modules.voice_manager.voice_store import voice_store


class VoiceDesigner:
    """语音设计引擎 - 自然语言 → 音色"""

    # 按语言识别
    LANGUAGE_PATTERNS = [
        (r'(中文|普通话|国语|汉语|mandarin|chinese)', 'zh'),
        (r'(英文|英语|english)', 'en'),
        (r'(日文|日语|日本語|japanese)', 'ja'),
        (r'(韩文|韩语|한국어|korean)', 'ko'),
        (r'(法文|法语|français|french)', 'fr'),
        (r'(德文|德语|deutsch|german)', 'de'),
        (r'(四川话|川普)', 'zh-sichuan'),
        (r'(粤语|广东话|cantonese)', 'zh-yue'),
    ]

    # 按性别识别
    GENDER_PATTERNS = [
        (r'(男|man|male|先生)', 'male'),
        (r'(女|woman|female|女性|女士|小姐|girl)', 'female'),
    ]

    # 按年龄段识别
    AGE_PATTERNS = [
        (r'(儿童|小孩|孩子|小朋友|儿童|宝宝|child|kid)', 'child'),
        (r'(少年|青年|teen|young|学生)', 'young'),
        (r'(中年|adult|成年)', 'adult'),
        (r'(老年|老人|爷爷|奶奶|senior|elder)', 'senior'),
    ]

    # 按风格识别
    STYLE_KEYWORDS = [
        '温柔', '磁性', '活泼', '沉稳', '甜美', '可爱',
        '专业', '权威', '亲切', '严肃', '搞笑', '慵懒',
        '沙哑', '清亮', '温暖', '知性', '元气', '成熟',
        '激情', '冷静', '幽默', '浪漫',
        '低沉', '高亢', '动感', '优雅',
    ]

    def __init__(self):
        pass

    def parse_description(self, description: str) -> dict:
        """
        从文本描述中解析语音特征
        返回: {"language": str, "gender": str, "age_group": str, "style_tags": list}
        """
        result = {
            "language": "zh",
            "gender": None,
            "age_group": "adult",
            "style_tags": [],
        }

        # 语言识别
        for pattern, lang in self.LANGUAGE_PATTERNS:
            if re.search(pattern, description, re.IGNORECASE):
                result["language"] = lang
                break

        # 性别识别
        for pattern, gender in self.GENDER_PATTERNS:
            if re.search(pattern, description, re.IGNORECASE):
                result["gender"] = gender
                break

        # 年龄段识别
        for pattern, age in self.AGE_PATTERNS:
            if re.search(pattern, description, re.IGNORECASE):
                result["age_group"] = age
                break

        # 风格标签提取
        for keyword in self.STYLE_KEYWORDS:
            if keyword in description:
                result["style_tags"].append(keyword)

        # 方言/口音检测 → 也加入 style_tags
        for pattern, _ in self.LANGUAGE_PATTERNS:
            match = re.search(pattern, description, re.IGNORECASE)
            if match:
                region = match.group(0)
                if region not in result["style_tags"]:
                    result["style_tags"].append(region)

        # 语速提取
        speed_match = re.search(r'语速[：:]*[较稍]?(快|慢|缓|急)', description)
        if speed_match:
            result["preferred_speed"] = speed_match.group(1)

        # 年龄数字提取
        age_match = re.search(r'(\d{1,2})\s*岁', description)
        if age_match:
            age = int(age_match.group(1))
            if age < 12:
                result["age_group"] = "child"
            elif age < 25:
                result["age_group"] = "young"
            elif age < 55:
                result["age_group"] = "adult"
            else:
                result["age_group"] = "senior"

        return result

    def design(
        self,
        description: str,
        name: str = None,
        engine_name: str = "voxcpm",
        category: str = None,
    ) -> dict:
        """
        根据文本描述生成音色

        Args:
            description: 语音描述 ("30岁温柔女声，四川话，语速稍慢")
            name: 音色名称
            engine_name: 使用的 TTS 引擎
            category: 五类归属 (human/animal/electronic/anime/mechanical)，
                VA-4: 动漫人物与老年/儿童人声走"语音设计"文本路线而非抓取
        """
        # 1. 解析描述
        features = self.parse_description(description)

        if name is None:
            # 自动生成名称
            lang_label = {"zh": "中文", "en": "English", "ja": "日本語"}.get(features["language"], "")
            gender_label = {"male": "男声", "female": "女声"}.get(features["gender"], "")
            name = f"{lang_label}{gender_label}-{uuid.uuid4().hex[:6]}"

        # 2. 获取引擎
        engine = engine_registry.get_loaded(engine_name)
        if engine is None:
            raise RuntimeError(f"引擎不可用: {engine_name}")

        if not engine.supports_design:
            raise RuntimeError(f"{engine_name} 不支持语音设计功能")

        # 3. 先创建音色记录获取统一 UUID
        voice = voice_store.create(
            name=name,
            source="designed",
            engine=engine_name,
            language=features["language"],
            gender=features["gender"],
            age_group=features["age_group"],
            style_tags=features["style_tags"],
            description=description,
            **({"category": category} if category else {}),
        )

        # 4. 以统一 voice_id 调用引擎语音设计（确保 adapter 和 DB 使用同一 ID）
        engine.design_voice(description, voice_id=voice.id)

        return {
            "voice_id": voice.id,
            "name": voice.name,
            "features": features,
        }

    def preview_features(self, description: str) -> dict:
        """
        预览：仅解析特征，不生成音色
        """
        return self.parse_description(description)


# 全局单例
voice_designer = VoiceDesigner()