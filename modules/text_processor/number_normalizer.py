"""
数字/日期/单位/货币标准化模块
将数字转换为中文/英文朗读形式，提高 TTS 自然度
"""

import re


class NumberNormalizer:
    """数字标准化器 - 将各类数字格式转换为适合朗读的文本"""

    def __init__(self):
        pass

    def normalize(self, text: str, language: str = "zh") -> str:
        """标准化文本中的所有数字表达"""
        if language == "zh":
            return self._normalize_chinese(text)
        else:
            return self._normalize_english(text)

    def _normalize_chinese(self, text: str) -> str:
        """中文化数字标准化"""

        # 1. 日期: 2024-01-15 → 2024年1月15日
        text = re.sub(
            r'(\d{4})[-/](\d{1,2})[-/](\d{1,2})',
            r'\1年\2月\3日',
            text,
        )

        # 2. 时间: 14:30 → 14点30分
        text = re.sub(
            r'(\d{1,2}):(\d{2})(?::(\d{2}))?',
            lambda m: f"{m.group(1)}点{m.group(2)}分" + (f"{m.group(3)}秒" if m.group(3) else ""),
            text,
        )

        # 3. 百分比: 50% → 百分之五十
        def _percent_repl(m):
            num = int(m.group(1))
            return f"百分之{self._num_to_chinese(num)}" if num <= 100 else m.group(0)

        text = re.sub(r'(\d+(?:\.\d+)?)%', _percent_repl, text)

        # 4. 货币: ￥100.50 → 一百元五角
        text = re.sub(
            r'[￥¥](\d+(?:\.\d{1,2})?)',
            lambda m: self._money_to_chinese(m.group(0)),
            text,
        )

        # 5. 温度: 25°C → 25摄氏度
        text = re.sub(r'(\d+(?:\.\d+)?)°[Cc]', r'\1摄氏度', text)
        text = re.sub(r'(\d+(?:\.\d+)?)°[Ff]', r'\1华氏度', text)

        # 6. 电话号码: 123-4567-8901 → 保持原样（数字逐一读出更自然）
        phone_pattern = re.compile(r'(\d{3,4})[-\s](\d{3,4})[-\s](\d{4})')
        text = phone_pattern.sub(r'\1-\2-\3', text)

        # 7. 小数: 3.14 → 三点一四
        text = re.sub(
            r'(?<!\d)(\d+)\.(\d+)(?!\d)',
            lambda m: f"{self._num_to_chinese(int(m.group(1)))}点"
                       f"{''.join(self._digit_to_chinese(d) for d in m.group(2))}",
            text,
        )

        return text

    def _normalize_english(self, text: str) -> str:
        """英文数字标准化"""
        # 日期
        text = re.sub(
            r'(\d{4})[-/](\d{1,2})[-/](\d{1,2})',
            r'\1 \2 \3',  # 英文 TTS 通常能直接读数字
            text,
        )
        # 货币
        text = re.sub(r'\$(\d+(?:\.\d{2})?)', r'\1 dollars', text)
        return text

    def _num_to_chinese(self, num: int) -> str:
        """整数转中文"""
        if num == 0:
            return "零"

        digits = "零一二三四五六七八九"
        units = ["", "十", "百", "千"]
        big_units = ["", "万", "亿"]

        if num < 10000:
            return self._num_to_chinese_small(num, digits, units)

        result = []
        big_idx = 0
        n = num
        while n > 0:
            part = n % 10000
            if part > 0:
                part_str = self._num_to_chinese_small(part, digits, units)
                if n >= 10000:
                    part_str += big_units[big_idx]
                result.insert(0, part_str)
            n //= 10000
            big_idx += 1

        return ''.join(result)

    def _num_to_chinese_small(self, num: int, digits: str, units: list) -> str:
        """小于10000的数转中文"""
        if num == 0:
            return ""

        parts = []
        n = num
        unit_idx = 0
        prev_zero = False

        while n > 0:
            digit = n % 10
            if digit > 0:
                if prev_zero:
                    parts.insert(0, "零")
                    prev_zero = False
                parts.insert(0, digits[digit] + (units[unit_idx] if unit_idx > 0 else ""))
            else:
                prev_zero = True
            n //= 10
            unit_idx += 1

        # 处理 "一十" → "十"
        result = ''.join(parts)
        if result.startswith("一十"):
            result = "十" + result[2:]

        return result

    def _digit_to_chinese(self, digit_char: str) -> str:
        """单个数字字符转中文"""
        digits = "零一二三四五六七八九"
        try:
            return digits[int(digit_char)]
        except (ValueError, IndexError):
            return digit_char

    def _money_to_chinese(self, text: str) -> str:
        """货币转中文"""
        text = text.lstrip('￥¥')
        if '.' in text:
            yuan_part, jiao_part = text.split('.')
            jiao_part = (jiao_part + '0')[:2]
        else:
            yuan_part = text
            jiao_part = '00'

        yuan = self._num_to_chinese(int(yuan_part))
        jiao_text = ""
        if jiao_part[0] != '0':
            jiao_text += self._digit_to_chinese(jiao_part[0]) + "角"
        if len(jiao_part) > 1 and jiao_part[1] != '0':
            jiao_text += self._digit_to_chinese(jiao_part[1]) + "分"

        return f"{yuan}元{jiao_text}"