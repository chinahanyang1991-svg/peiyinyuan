# -*- coding: utf-8 -*-
"""
VA-4 语音设计种子：anime 类 + 老年/中文儿童人声

背景（归档-非程序件/vendor-research/VOICE-STUDIO-ANALYSIS.md + 音色库工程裁定）：
- anime 类音色因版权**不抓取**，改由"语音设计"文本描述生成；
- 老年/中文儿童人声在 CC 语料里几乎采不到，同样走设计路线。
本脚本只写 voice_profiles 行（source='designed', engine='voxcpm'），
不加载模型：VoxCPM 适配器启动时经 _restore_voices 把这些行注册回内存，
synth 时以 "(描述)文本" 前缀生效。

幂等：按 name 去重。默认 dry-run，--apply 才写库。
描述用"声音原型"（傲娇少女/热血少年…），不写真实角色名（商标/肖像风险为零）。

用法:  python tools/voice_library/seed_design_voices.py [--apply]
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

# (name, category, language, gender, age_group, style_tags, description)
SEEDS = [
    # ---------------- anime 原型（12） ----------------
    ("动漫-傲娇大小姐", "anime", "zh", "female", "young", ["动漫", "傲娇"],
     "十七八岁少女，音调偏高且清亮，语速偏快，句尾常带不服气的上扬，外冷内热，娇蛮中带一点羞怯"),
    ("动漫-温柔大姐姐", "anime", "zh", "female", "young", ["动漫", "治愈"],
     "二十多岁女性，音色温暖柔软，语速舒缓，气息绵长，像深夜电台一样令人放松，带轻轻的笑意"),
    ("动漫-热血少年", "anime", "zh", "male", "young", ["动漫", "热血"],
     "十五六岁少年，声音明亮有冲劲，音量饱满，情绪激动时略微嘶吼，说话干脆不拖泥带水"),
    ("动漫-冷静天才少年", "anime", "zh", "male", "young", ["动漫", "冷静"],
     "少年男性，音调中等偏低，语调几乎无起伏，吐字精确冷静，语速均匀，透出疏离的自信"),
    ("动漫-神秘御姐", "anime", "zh", "female", "adult", ["动漫", "神秘"],
     "成熟女性，声线低沉有磁性，语速慢，尾音略带走心感，气声较多，营造神秘危险的气质"),
    ("动漫-元气萝莉", "anime", "zh", "female", "child", ["动漫", "可爱"],
     "七八岁女孩，音调很高很幼态，咬字略圆，语速快，情绪夸张起伏大，像小动物一样活泼"),
    ("动漫-憨厚壮汉", "anime", "zh", "male", "adult", ["动漫", "憨厚"],
     "成年男性，嗓音粗厚浑，共鸣靠后，语速慢半拍，憨直老实，笑声洪亮"),
    ("动漫-邪魅反派", "anime", "zh", "male", "adult", ["动漫", "反派"],
     "成年男性，低沉优雅，句与句之间带玩味的停顿，尾音上挑，阴柔危险，像猫逗老鼠"),
    ("动漫-机械女仆", "anime", "zh", "female", "young", ["动漫", "机械"],
     "女性合成腔，语调礼貌平直带轻微等离感，元音过度干净，偶尔在句尾加一声短促的电子提示音式的收音"),
    ("动漫-古风侠女", "anime", "zh", "female", "young", ["动漫", "古风"],
     "青年女性，字正腔圆带古白话腔调，语气英气爽朗，行腔有顿挫，如武侠小说里的女侠"),
    ("动漫-话痨小狐狸", "anime", "zh", "neutral", "child", ["动漫", "俏皮"],
     "拟人小兽音色，偏童声但带鼻音的俏皮感，语速极快，爱在句中加语气词，古灵精怪"),
    ("动漫-慵懒猫系少女", "anime", "zh", "female", "young", ["动漫", "慵懒"],
     "青年女性，半睡半醒的松弛感，音调中低，句尾拖长下沉，气息慵懒，带一点鼻音"),
    # ---------------- 老年（4） ----------------
    ("人声-苍老爷爷", "human", "zh", "male", "senior", ["老年"],
     "七八十岁男性，嗓音干瘦沙哑，声带松弛导致气声明显，语速缓慢，句间有多次停顿换气"),
    ("人声-慈祥奶奶", "human", "zh", "female", "senior", ["老年"],
     "七八十岁女性，音色柔和略带颤抖，齿音因缺牙略漏风，语速慢，语气慈祥絮叨"),
    ("人声-退休老教师", "human", "zh", "male", "senior", ["老年", "儒雅"],
     "六十多岁男性，吐字清晰讲究，音调不高但中气足，带长期讲课形成的从容顿挫，少量痰音"),
    ("人声-邻里大妈", "human", "zh", "female", "senior", ["老年", "爽利"],
     "六十多岁女性，音量大、语调起伏夸张，说话连珠炮，带市井热络感，尾音常用感叹"),
    # ---------------- 中文儿童（4） ----------------
    ("人声-小男孩", "human", "zh", "male", "child", ["儿童"],
     "六七岁男孩，音调高，咬字尚不完全清晰有轻微奶音，语速快且气息短，兴奋时会喊起来"),
    ("人声-小女孩", "human", "zh", "female", "child", ["儿童"],
     "五六岁女孩，音色嫩亮，句尾常带询问式上扬，吐字圆润带奶音，笑声多"),
    ("人声-少年队长", "human", "zh", "male", "child", ["少年"],
     "十二三岁男孩，变声初期声音略哑不稳，努力装成熟的低腔调，语气认真有组织领导感"),
    ("人声-早熟少女", "human", "zh", "female", "child", ["少年"],
     "十三四岁女孩，变声后清亮音色，语气平静略带敏感，语速不快，用词比实际年龄成熟"),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正写库（默认只打印）")
    args = ap.parse_args()

    from modules.voice_manager.voice_store import voice_store

    existing = {v.name for v in voice_store.list(limit=2000)}
    todo = [s for s in SEEDS if s[0] not in existing]
    print(f"种子总数 {len(SEEDS)}, 已存在 {len(SEEDS) - len(todo)}, 待写入 {len(todo)}")
    if not args.apply:
        for name, cat, *_ in todo:
            print(f"  [dry] {cat:<11} {name}")
        print("加 --apply 写库")
        return

    for name, cat, lang, gender, age, tags, desc in todo:
        v = voice_store.create(
            name=name, source="designed", engine="voxcpm",
            language=lang, gender=gender, age_group=age,
            style_tags=tags, description=desc, category=cat,
        )
        print(f"  [ok] {v.id} {cat}/{name}")
    print(f"完成: 写入 {len(todo)} 行 (voxcpm 启动时经 _restore_voices 自动注册)")


if __name__ == "__main__":
    main()
