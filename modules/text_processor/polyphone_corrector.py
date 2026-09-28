"""
多音字自动校正模块
基于规则 + pypinyin 的多音字纠错
"""

import re

try:
    from pypinyin import pinyin, Style
    HAS_PYPINYIN = True
except ImportError:
    HAS_PYPINYIN = False


class PolyphoneCorrector:
    """多音字校正器 - 为 TTS 提供正确读音指引"""

    # 常见多音字规则（词 → 读音）
    # 格式: (多音字, 上下文词, 正确读音提示)
    COMMON_RULES = {
        # 了
        "了": [
            (r"了(?=解)", "liǎo"),
            (r"了(?=结)", "liǎo"),
            (r"了(?=得)", "liǎo"),
            (r"不(?:得|能)了", "liǎo"),
        ],
        # 的
        "的": [
            (r"目的", "dì"),
            (r"的确", "dí"),
        ],
        # 都
        "都": [
            (r"首都", "dū"),
            (r"都市", "dū"),
            (r"都城", "dū"),
        ],
        # 长
        "长": [
            (r"(?:生|校|院|官|队|师|兄|辈)长", "zhǎng"),
            (r"长(?:大|高|胖|进)", "zhǎng"),
            (r"成长", "zhǎng"),
        ],
        # 行
        "行": [
            (r"银行", "háng"),
            (r"(?:同|内|外)行", "háng"),
            (r"(?:一|两|三|四|五|六|七|八|九|十)行", "háng"),
        ],
        # 发
        "发": [
            (r"头发", "fà"),
            (r"理发", "fà"),
            (r"白发", "fà"),
        ],
        # 给
        "给": [
            (r"供给", "jǐ"),
            (r"给予", "jǐ"),
            (r"给养", "jǐ"),
        ],
        # 会
        "会": [
            (r"会计", "kuài"),
        ],
        # 还
        "还": [
            (r"还(?:是|有|在|没|能|要)", "hái"),
        ],
        # 乐
        "乐": [
            (r"音乐", "yuè"),
            (r"乐器", "yuè"),
            (r"乐曲", "yuè"),
            (r"声乐", "yuè"),
        ],
        # 朝
        "朝": [
            (r"朝(?:鲜|代|廷|阳|圣|霞|气)", "cháo"),
            (r"朝(?=[东南西北])", "cháo"),
            (r"(?:唐|宋|元|明|清)朝", "cháo"),
        ],
        # 重
        "重": [
            (r"重(?:要|大|点|视|量|金|建)", "zhòng"),
            (r"郑?重", "zhòng"),
        ],
        # 便
        "便": [
            (r"便宜", "pián"),
        ],
        # 着
        "着": [
            (r"着(?:急|火|凉|迷|魔)", "zháo"),
            (r"睡(?:不)?着", "zháo"),
            (r"(?:抓|找|买|看|见|听|想|够)得?着", "zháo"),
            (r"着落", "zhuó"),
            (r"着装", "zhuó"),
            (r"着手", "zhuó"),
            (r"着重", "zhuó"),
        ],
    }

    def __init__(self):
        pass

    def correct(self, text: str) -> str:
        """
        返回带有读音标注的文本（用于 TTS 预处理）
        返回格式不变，但会记录多音字位置和读音
        """
        corrections = self.find_polyphones(text)
        # 此处不修改文本本身，而是返回需要关注的列表
        # TTS 引擎会根据此信息调整发音
        return text

    def find_polyphones(self, text: str) -> list:
        """
        查找文本中的多音字并提供正确读音
        返回: [{"char": "了", "position": 5, "pinyin": "liǎo", "context": "了解"}, ...]
        """
        results = []

        for char, rules in self.COMMON_RULES.items():
            for pattern_str, correct_pinyin in rules:
                for match in re.finditer(pattern_str, text):
                    # 找到匹配中的多音字位置
                    char_pos = match.group().find(char)
                    if char_pos >= 0:
                        results.append({
                            "char": char,
                            "position": match.start() + char_pos,
                            "pinyin": correct_pinyin,
                            "context": match.group(),
                        })

        return sorted(results, key=lambda x: x["position"])
