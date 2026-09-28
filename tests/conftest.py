"""
Pytest 共享配置
"""
import pytest
import os
import sys

# Add project root to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


@pytest.fixture
def sample_chinese_text():
    return (
        "第一章 初入京城。\n\n"
        "李小明第一次来到京城，看到繁华的街道，心中充满了期待。"
        "他深吸一口气，暗暗对自己说：\u201c我一定要在这里闯出一番天地！\u201d"
        "这时，一个身穿黑衣的老者走到他面前，问道：\u201c年轻人，你是新来的吧？\u201d"
        "李小明点了点头。老者笑了笑，说：\u201c跟我来吧。\u201d\n\n"
        "第二章 意外相逢。\n\n"
        "第二天一早，李小明被窗外的喧闹声吵醒。"
        "他揉了揉惺忪的睡眼，推开窗户，只见楼下围了一大群人。"
        "\u201c发生什么事了？\u201d他好奇地问旁边的伙计。\n\n"
        "伙计摇了摇头说：\u201c听说是有位大人物要来。\u201d"
    )


@pytest.fixture
def sample_english_text():
    return (
        "Chapter 1: The Beginning.\n\n"
        "It was a dark and stormy night. The rain pounded against the windows "
        "as Dr. Frankenstein worked tirelessly on his creation. "
        "\"It's alive!\" he exclaimed with terror and amazement.\n\n"
        "Chapter 2: The Escape.\n\n"
        "The creature wandered through the village, frightened and confused. "
        "Children screamed at the sight of him. Adults fled in panic. "
        "\"Why do they fear me?\" the creature wondered sadly."
    )


@pytest.fixture
def sample_book_text():
    return (
        "第一章 初入京城。\n\n"
        "李小明第一次来到京城。\n\n"
        "更多精彩内容请访问 www.example.com\n\n"  # AD
        "第二章 意外相逢。\n\n"
        "第二天一早。\n\n"
        "扫码下载APP获取更多章节\n\n"  # AD
        "\u2014\u2014 本文由某某整理发布\n\n"  # AD
        "第三章 峰回路转。\n\n"
        "事情出现了转机。"
    )


@pytest.fixture
def sample_dialogue_text():
    return (
        "李小明笑着说：\u201c你好，我是新来的。\u201d\n"
        "老者点点头：\u201c欢迎欢迎，等你好久了。\u201d\n"
        "旁边一个红衣女子插话道：\u201c听说你很厉害？\u201d\n"
        "李小明谦虚地说：\u201c哪里哪里，还需要前辈们多多指教。\u201d\n"
        "老者摆了摆手：\u201c好了好了，都别站着了，坐下说话。\u201d"
    )