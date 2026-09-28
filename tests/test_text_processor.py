"""
文本处理模块单元测试
"""
import pytest
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from modules.text_processor.sentence_splitter import SentenceSplitter
from modules.text_processor.tone_detector import ToneDetector
from modules.text_processor.dialogue_detector import DialogueDetector


class TestSentenceSplitter:
    """分句引擎测试"""

    def setup_method(self):
        self.splitter = SentenceSplitter()

    def test_split_chinese_simple(self):
        """中文基本分句"""
        text = "今天天气真好。我们出去玩吧！你准备好了吗？"
        result = self.splitter.split(text, "zh")
        assert len(result) == 3

    def test_split_chinese_complex(self):
        """[2H 裁定: 契约已变更 → 按新契约改写测试，产品码不动] PY-108

        旧断言 `len(result) >= 2` 断的正是 PY-108 修掉的那个缺陷：
        QUOTE_CONTENT 缺中文弯引号 U+201C/U+201D 配对时，
        `他说：“你好。”然后笑了笑。` 会在**引号内**的 。 处被强切成 2 句，
        导致引号破对、对白归属丢失（issues.md PY-108, commit c0c55ee）。
        弯引号配对集补全后，引号内的 。 不再是分割点，本例全句只剩一个句末标点
        → 得 1 个完整韵律单元，符合设计口径
        （qa/harness/run_p4_split.py §1 "引号内内容不被切开" 同一判定，0 破对）。
        为避免"放宽"，改写为成对的正/反断言：引号内不切 + 引号外仍必须切 + 不丢字。
        """
        text = "他说：“你好。”然后笑了笑。"
        result = self.splitter.split(text, "zh")

        # (1) 引号内的 。 不切：整句为一个韵律单元，弯引号成对未被破
        assert result == [text], f"PY-108 契约: 引号内不得强切, 实得 {result}"
        assert result[0].count("“") == result[0].count("”") == 1

        # (2) 引号外的句末标点仍是强分割点（没有因为 PY-108 而退化）
        longer = "他说：“你好。”然后笑了笑。窗外下起了雨。"
        two = self.splitter.split(longer, "zh")
        assert len(two) == 2, f"引号外的句末标点应继续分句: {two}"
        assert two[0].startswith("他说：“你好。”") and two[0].endswith("笑了笑。")
        assert two[1] == "窗外下起了雨。"
        # (3) 引号保护占位符不外泄、字符不丢
        assert all("__QUOTE" not in s for s in two)
        assert sum(len(s) for s in two) == len(longer)

    def test_split_english(self):
        """英文基本分句"""
        text = "Hello world. How are you? I am fine."
        result = self.splitter.split(text, "en")
        assert len(result) == 3

    def test_split_long_sentence(self):
        """[2H 裁定: 契约已变更 → 按新契约改写测试，产品码不动] PY-107

        旧断言 `len(result) >= 2` 断的同样是 PY-107 修掉的缺陷：
        旧实现对**每个**句子无条件在 CN_WEAK_BREAKS(，,；;、) 处二次强拆，
        40 字 5 小句的普通叙述被切成 5 个以逗号结尾的碎片 —— issues.md PY-107
        实测 36/36 句以逗号结尾、TTS 调用放大 36 倍且韵律破碎（兼红线"卡顿"源）。
        现契约(commit af5becb)：仅当句子"超过 WEAK_SPLIT_THRESHOLD(80 字) 且完全无句末标点"
        才逗号弱拆后回并到 WEAK_MERGE_TARGET(100 字)；普通句子只在超过
        MAX_SENTENCE_LENGTH(150 字) 时按标点边界分段。本例 40 字且以 。 结尾
        → 1 个完整韵律单元属预期（qa/harness/run_p4_split.py §2 以
        "逗号结尾占比 <5%" + "句数/句末标点 ∈[0.8,2.5]" 判定，同一口径）。
        为排除"改过头导致超长句不拆了"这一真回归风险，(2)(3) 两段把红线写成断言。
        """
        # (1) 新契约：句末有 。 的 40 字逗号长句 = 1 个韵律单元，不再逐逗号强拆
        normal = "她走到窗前，推开窗户，望着远处的山峦，心中涌起一股莫名的感动，泪水模糊了双眼。"
        assert len(normal) < self.splitter.WEAK_SPLIT_THRESHOLD
        one = self.splitter.split(normal, "zh")
        assert one == [normal], f"PY-107 契约: 普通逗号长句不得强拆, 实得 {one}"

        # (2) 红线：无句末标点的退化长句(>80 字)仍必须弱拆，且单元不落在逗号上
        degraded = "，".join(["她走到窗前推开窗户望着远处的山峦"] * 9)   # 152 字, 句末无 。
        parts = self.splitter.split(degraded, "zh")
        assert len(degraded) > self.splitter.WEAK_SPLIT_THRESHOLD
        assert len(parts) >= 2, f"退化长句未弱拆(PY-107 修复被改过头): {parts}"
        assert all(not p.endswith(("，", ",", "、", "；")) for p in parts), \
            f"韵律单元以逗号结尾: {parts}"
        assert all(len(p) <= self.splitter.WEAK_MERGE_TARGET for p in parts), \
            f"回并后仍超过 WEAK_MERGE_TARGET: {[len(p) for p in parts]}"
        assert sum(p.count("她走到窗前") for p in parts) == 9, "弱拆回并丢字了"

        # (3) 红线：> MAX_SENTENCE_LENGTH 的句子仍按标点边界分段，无超长残留、不丢字
        overlong = "，".join(["她走到窗前推开窗户望远处"] * 12) + "。"   # >150 字的单句
        many = self.splitter.split(overlong, "zh")
        assert len(overlong) > self.splitter.MAX_SENTENCE_LENGTH
        assert len(many) >= 2, f"超长句不拆了(真回归): {many}"
        assert all(len(m) <= self.splitter.MAX_SENTENCE_LENGTH for m in many), \
            f"仍有超长句残留: {[len(m) for m in many]}"
        assert sum(m.count("她走到窗前") for m in many) == 12, "分段丢字"

    def test_split_empty_text(self):
        """空文本应返回空列表"""
        assert self.splitter.split("", "zh") == []
        assert self.splitter.split("   ", "zh") == []

    def test_split_mixed_language(self):
        """中英文混合文本"""
        text = "Hello世界。I love you! 你真好。"
        result = self.splitter.split(text, "zh")
        # Mixed text defaults to CJK mode
        assert len(result) >= 3


class TestToneDetector:
    """语气检测器测试"""

    def setup_method(self):
        self.detector = ToneDetector()

    def test_detect_question(self):
        """应检测疑问语气"""
        result = self.detector.detect("你吃饭了吗？")
        assert result["primary"] == "question"

    def test_detect_excited(self):
        """应检测兴奋语气"""
        result = self.detector.detect("太好了！太棒了！")
        assert result["primary"] == "excited"

    def test_detect_sad(self):
        """应检测悲伤语气"""
        result = self.detector.detect("唉，真是难过……")
        assert result["primary"] == "sad"

    def test_detect_neutral(self):
        """应检测中性语气"""
        result = self.detector.detect("今天天气不错。")
        assert result["primary"] == "neutral"

    def test_detect_angry_keyword(self):
        """应通过关键词检测愤怒"""
        result = self.detector.detect("你这个混蛋！")
        # ！结尾同时匹配 excited，但语气词典中"混蛋"明确指示 angry
        assert result["primary"] in ("angry", "excited")
        assert result["confidence"] > 0.7

    def test_confidence_scores(self):
        """confidence 应在 0-1 范围"""
        texts = ["你好。", "为什么？", "太棒了！", "唉……"]
        for text in texts:
            result = self.detector.detect(text)
            assert 0 <= result.get("confidence", 0) <= 1

    def test_empty_text(self):
        """空文本应返回中性"""
        result = self.detector.detect("")
        assert result["primary"] == "neutral"


class TestDialogueDetector:
    """对话检测器测试"""

    def setup_method(self):
        self.detector = DialogueDetector()

    def test_detect_quoted_dialogue(self, sample_dialogue_text):
        """应检测引号内对话"""
        result = self.detector.detect_dialogues(sample_dialogue_text)
        dialogues = [d for d in result if d["type"] == "dialogue"]
        assert len(dialogues) >= 4

    def test_detect_role_hints(self, sample_dialogue_text):
        """应识别角色提示"""
        result = self.detector.detect_dialogues(sample_dialogue_text)
        dialogues = [d for d in result if d["type"] == "dialogue"]
        # At least some dialogues should have role hints
        with_hints = [d for d in dialogues if d.get("role_hint")]
        assert len(with_hints) > 0

    def test_extract_roles(self, sample_dialogue_text):
        """应从对话中提取角色列表"""
        dialogues = self.detector.detect_dialogues(sample_dialogue_text)
        roles = self.detector.extract_roles(dialogues)
        assert len(roles) > 0
        # Should find role names
        role_names = [r["name"] for r in roles]
        assert any(name in role_names for name in ["李小明", "老者", "红衣女子"])

    def test_narration_detection(self, sample_dialogue_text):
        """应检测旁白段落"""
        result = self.detector.detect_dialogues(sample_dialogue_text)
        narrations = [d for d in result if d["type"] == "narration"]
        assert len(narrations) > 0

    def test_empty_text(self):
        """空文本应返回空列表"""
        result = self.detector.detect_dialogues("")
        assert result == []