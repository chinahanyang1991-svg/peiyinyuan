# -*- coding: utf-8 -*-
"""Voice-library refill: corpus, variant families, DSP and audio IO helpers.

Local-generated speech variants for the 4 non-human categories + human.
Engine: edge-tts (online neural TTS) + light local DSP (pitch shift, vibrato,
tremolo, comb, chorus, tanh drive). Output is always intelligible human
speech carrying a category-colored vocal design (per user ruling 2026-09-22).
"""
import os, re, json
import numpy as np
import soundfile as sf

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
AUDIO_ROOT = os.path.join(ROOT, "data", "voices", "library", "refill")
SELF_DIR = os.path.dirname(os.path.abspath(__file__))
TARGET_SR = 48000

CAT_CN = {"animal": "动物", "electronic": "电子", "mechanical": "机械",
          "anime": "动漫", "human": "人声"}

# ---------------- texts (2-3 short spoken sentences, human-speech style) ----
TEXTS = {
"animal": [
 "我家猫今早踩了三下闹钟，才肯去吃饭。", "隔壁的狗又在楼道里迎客人了。",
 "小鸟飞到窗台，歪着头看我写作业。", "仓鼠把瓜子藏进腮帮，走得歪歪扭扭。",
 "池塘的青蛙一到傍晚就开演唱会。", "老黄犬趴在门口，尾巴轻轻扫地。",
 "白鹅伸长脖子，把院门守得严严实生。", "鹦鹉学会了打招呼，见人就点头。",
 "小猫追着毛线团，从客厅跑到阳台。", "牧场的小牛犊第一次站起来，腿直打颤。",
 "刺猬缩成一团，怎么哄都不肯出来。", "海豚跃出水面，绕着船游了半圈。",
 "蜜蜂围着桂花树转，嗡嗡声一直没停。", "乌龟慢慢爬过晒谷场，谁也不理。",
 "兔子耳朵一转，听见远处有车来了。", "狐狸在雪地里走出一道细细的脚印。",
 "喜鹊在屋脊上叫，说今天有客上门。", "小猫打呼噜的声音，像一台小发动机。",
 "金鱼的尾巴散开，在水里慢慢摇扇子。", "螳螂举起前爪，一副认真的样子。",
 "羊群挤在一起，把牧羊犬急得直绕圈。", "布谷鸟在山那边数着春天的日子。",
 "鹿抬起头 listen 了一瞬，又低头喝水。", "小狗学会了握手，爪子放在我手心。",
 "猫头鹰夜里值班，眼睛亮得像两盏灯。", "蜻蜓点了一下水面，荡开三圈波纹。",
 "松鼠把松果埋进土里，转头就忘了地方。", "动物园开放日，孩子们围着长颈鹿喊。",
],
"electronic": [
 "系统提示：固件升级完成，请重启设备。", "屏幕刷新率调到一百二十赫兹以后更顺滑。",
 "路由器闪了两下蓝灯，网络重新连上了。", "扫地机器人转了个弯，躲开了拖鞋。",
 "耳机开了降噪，地铁的噪音一下子退远。", "电池充满百分比，指示灯从橙变绿。",
 "语音助手应答：已为您打开客厅的灯。", "服务器风扇提速，日志滚动得很快。",
 "相机对焦框锁定的那一刻，快门轻轻响。", "无人机悬停半秒，桨声稳定得像一根弦。",
 "智能手表提醒我，久坐一个小时了。", "扫码成功的提示音，短促又干脆。",
 "音箱切到下一首，人声更靠中间了。", "投影仪校正完梯形，字幕边缘很锐利。",
 "电磁炉档位调低，汤面重新平静下来。", "游戏手柄震动了一下，提示能量已满。",
 "电子秤报出体重，语气毫无波澜。", "自动门感应到人，向两边安静地滑开。",
 "导航说前方三百米右转，语速不快。", "微波炉叮的一声，提醒米饭热好了。",
 "摄像头画面里出现移动物体，开始录像。", "充电宝还剩四格电，够再充一次手机。",
 "电视菜单调出音频延迟补偿，口型对上了。", "键盘逐轴触发，咔哒声连成一串。",
 "智能门锁验证通过，电机轻轻收回插舌。", "屏幕录制开始倒计时，三、二、一。",
 "耳机定位转头，声音从左边移到了右边。", "机器人的指示灯转成绿色，表示待机完成。",
],
"mechanical": [
 "发动机点火，转速慢慢爬上一千八。", "齿轮咬合发出低沉的咔咔声。",
 "液压杆顶起吊臂，稳稳停在空中。", "老式挂钟整点报时，钟摆左右摇摆。",
 "火车过道口，车轮和铁轨撞击出节奏。", "缝纫机的踏板连着小轮，哒哒哒地走线。",
 "千斤顶摇到第三圈，车身离了地。", "链条传动把纸箱送上分拣台。",
 "柴油机突突突地响，排气管冒着白烟。", "刹车片磨合完毕，制动踏板回弹干脆。",
 "冲压机一下一下地落，钢板切成零件。", "风箱拉动炉火，铁匠举起锤子。",
 "自行车链条上了油，蹬起来安静许多。", "起重机钢缆绷紧，集装箱缓缓升起。",
 "活塞往复运动，曲轴转得又稳又匀。", "齿轮箱降速之后，扭力明显大了。",
 "铆钉枪连着响了十下，钢板固定牢了。", "门锁的弹簧舌一弹，门就合上了。",
 "涡轮增压介入，发动机声音变得绵密。", "传送带调好张紧度，不再打滑。",
 "手摇发电机转起来，灯泡渐渐亮了。", "挖掘机挖斗收拢，一铲沙石倒进车厢。",
 "空压机打气到八个压，自动停机。", "蒸汽机车的汽笛拉响，轮子缓缓转动。",
 "扳手卡住螺母，三圈半拧紧。", "电钻换了冲击档，墙面开始吃进钻头。",
 "吊车的配重压住尾部，臂架没有抖。", "棘轮扳手空转的声音，哒哒哒特别轻。",
],
"anime": [
 "今天的试镜我一定要拿到主角，等着瞧吧。", "才、才不是在等你呢，我只是恰好路过。",
 "无论重来多少次，我都会站在这里。", "少年握紧拳头，对着夕阳许下誓言。",
 "反派轻笑一声，披风在夜风里展开。", "魔法少女转身，光之缎带划出弧线。",
 "学园祭开幕，礼堂里的灯光全亮了。", "机甲驾驶舱合拢，系统逐条自检完成。",
 "侦探推了推眼镜，说真相只有一个。", "我并不是为了赢，只是为了不辜负约定。",
 "前辈回过头，把围巾解下来递给我。", "勇气不是不害怕，是害怕还要往前。",
 "变身的光柱升上夜空，云层被撕开。", "剑与盾靠在墙边，旅人围炉坐下。",
 "转学第一天，我在天台念出了咒语。", "命运之轮开始转动，谁都别想逃。",
 "伙伴倒下时，我第一次喊哑了嗓子。", "傲娇地哼了一声，她把便当塞过来。",
 "异世界的传送门在图书馆深处发光。", "赛车冲过终点，尾翼擦着火花过去。",
 "他说要成为海贼……不，要去看世界的尽头。", "月光下的屋顶，白猫绅士行了个礼。",
 "热血社团的黄昏，球棒挥了一百次。", "时间停止的那一秒，她回头微笑。",
 "巨龙的阴影掠过城墙，号角接连吹响。", "临别的车窗上，画着一个小小的笑脸。",
 "我把勋章收进抽屉，故事才刚开头。", "舞台灯光亮起的那一刻，歌声替我说出口。",
],
"human": [
 "早上好，今天的通勤路上风有点凉。", "晚上我打算煮碗面，加个荷包蛋。",
 "这本书读到一半，忍不住想讲给你听。", "周末去公园走走，顺便拍几张照片。",
 "雨停之后，楼下的栀子花全开了。", "好久不见，你看上去气色不错。",
 "我把咖啡放凉了才喝，味道刚好合适。", "工作辛苦啦，先坐下歇一会儿吧。",
 "昨晚睡得很沉，今天早上精神挺好。", "超市打折，我买了些水果和蔬菜。",
 "孩子放学回来，第一件事是洗手。", "电话那头的朋友说，春天就来这边玩。",
 "傍晚散步的时候，听见远处有人在唱歌。", "这份文件我先看一遍，回头再找你确认。",
 "茶泡得有些久，微微发苦。", "老照片翻出来，一下子想起许多事。",
 "今天例会改到下午三点，地点没变。", "阳光照在桌面上，猫一样的东西在打盹。",
 "他的语速不快，但每句都说得很稳。", "我们约在地铁站口，不见不散。",
 "新年快到了，该写几张贺卡了。", "这家面馆的汤底浓郁，值得再来一次。",
 "她在讲台上深呼吸，然后开口讲故事。", "夜里加班回家，街道安静得能听见风声。",
 "朋友聚会，大家抢着说最近的新鲜事。", "晨跑三公里，出汗之后整个人都轻了。",
 "把窗子推开，屋里的空气换了个遍。", "他笑了笑，没有立刻回答这个问题。",
],
}

# ---------------- variant families: voice + prosody + local DSP -------------
# dsp keys: shift (semitones), vib(freq,depth), trem(freq,depth),
#           comb(delay_samples,gain), cho(depth_samples,gain), drive(tanh k)
FAMILIES = {
"animal": [
 ("kit",  "幼兽尖细", "zh-CN-YunxiaNeural",    "+12%", "+120Hz", {"vib":[5.2,0.12],"shift":2}),
 ("vix",  "狐音尖亮", "zh-CN-XiaoyiNeural",    "+16%", "+60Hz",  {"shift":3,"vib":[4.6,0.10]}),
 ("bird", "雀鸣快速", "zh-CN-XiaoxiaoNeural",  "+25%", "+140Hz", {"shift":4,"vib":[7.0,0.07]}),
 ("pup",  "幼犬欢快", "zh-CN-liaoning-XiaobeiNeural", "+8%", "+30Hz", {"vib":[3.8,0.08]}),
 ("beast","巨兽低吼", "zh-CN-YunjianNeural",   "+3%",  "-40Hz",  {"drive":1.6,"trem":[2.6,0.18]}),
 ("purr", "呼噜猫腔", "zh-CN-XiaoyiNeural",    "+10%", "+90Hz",  {"shift":1,"vib":[4.4,0.20]}),
 ("frog", "蛙鸣胸腔", "zh-CN-YunjianNeural",   "+6%",  "-70Hz",  {"comb":[110,0.35]}),
 ("hive", "蜂群高频", "zh-CN-XiaoxiaoNeural",  "+20%", "+110Hz", {"shift":5,"trem":[6.5,0.15]}),
],
"electronic": [
 ("nav",  "导航合成", "zh-CN-YunyangNeural",   "+4%",  "+0Hz",   {"comb":[32,0.35],"cho":[4,0.25]}),
 ("pad",  "柔光音色", "zh-CN-XiaoxiaoNeural",  "+8%",  "+40Hz",  {"cho":[6,0.35],"shift":2}),
 ("modem","拨号急促", "zh-CN-YunxiNeural",     "+28%", "+10Hz",  {"comb":[24,0.40]}),
 ("holo", "全息清冷", "zh-TW-HsiaoChenNeural", "+6%",  "+70Hz",  {"cho":[5,0.30],"comb":[38,0.25]}),
 ("arc",  "街机明亮", "zh-CN-XiaoyiNeural",    "+20%", "+110Hz", {"shift":3,"comb":[28,0.30]}),
 ("servo","伺服低频", "zh-CN-YunjianNeural",   "+3%",  "-20Hz",  {"drive":1.25,"comb":[44,0.30]}),
 ("sat",  "卫星广播", "zh-HK-HiuGaaiNeural",   "+10%", "+20Hz",  {"comb":[30,0.45],"cho":[3,0.20]}),
 ("drone","遥控冷静", "zh-CN-YunyangNeural",   "+2%",  "-10Hz",  {"cho":[7,0.40]}),
],
"mechanical": [
 ("diesel","柴油低沉", "zh-CN-YunjianNeural",  "+2%",  "-60Hz",  {"drive":2.2}),
 ("piston","活塞节奏", "zh-CN-YunxiNeural",    "+15%", "-30Hz",  {"trem":[6.2,0.22],"comb":[60,0.25]}),
 ("hydra", "液压平稳", "zh-CN-YunyangNeural",  "+6%",  "-15Hz",  {"comb":[70,0.30],"cho":[4,0.15]}),
 ("chain", "链条摩擦", "en-US-BrianNeural",    "+18%", "+0Hz",   {"drive":1.5,"trem":[7.5,0.18]}),
 ("clock", "钟摆清亮", "zh-HK-HiuMaanNeural",  "+12%", "+25Hz",  {"comb":[26,0.40]}),
 ("gear",  "齿轮箱腔", "zh-CN-liaoning-XiaobeiNeural","+8%","+5Hz",{"comb":[52,0.35],"drive":1.3}),
 ("turbo", "涡轮绵密", "zh-TW-YunJheNeural",   "+4%",  "-25Hz",  {"cho":[5,0.30],"drive":1.4}),
 ("hinge", "锈铰吱呀", "zh-CN-XiaoyiNeural",   "+6%",  "+80Hz",  {"vib":[2.4,0.22],"comb":[36,0.25]}),
],
"anime": [
 ("tsun", "傲娇少女", "zh-CN-XiaoyiNeural",   "+14%", "+55Hz",  {"vib":[5.0,0.10]}),
 ("cool", "冷静前辈", "zh-HK-HiuGaaiNeural",  "+6%",  "+30Hz",  {}),
 ("sho",  "热血少年", "zh-CN-YunxiaNeural",   "+10%", "+90Hz",  {"shift":2}),
 ("lord", "低沉反派", "zh-CN-YunjianNeural",  "+2%",  "-70Hz",  {"cho":[5,0.28],"drive":1.35}),
 ("onee", "温柔姐姐", "zh-CN-XiaoxiaoNeural", "+4%",  "+40Hz",  {"vib":[4.2,0.06]}),
 ("komo", "呆萌副角", "zh-CN-liaoning-XiaobeiNeural","+18%","+70Hz",{"shift":3}),
 ("pilot","机甲驾驶员","zh-TW-YunJheNeural",  "+8%",  "-15Hz",  {"comb":[34,0.22]}),
 ("magi", "魔法少女", "zh-CN-XiaoyiNeural",   "+22%", "+130Hz", {"shift":4,"vib":[6.2,0.14]}),
 ("det",  "冷酷侦探", "zh-CN-YunyangNeural",  "+3%",  "-20Hz",  {}),
 ("chan","元气歌姬",  "zh-TW-HsiaoYuNeural",  "+12%", "+60Hz",  {"shift":1,"vib":[5.6,0.12]}),
],
"human": [
 ("news","新闻沉稳","zh-CN-YunyangNeural","+4%","+0Hz",{}),
 ("warm","温和女声","zh-CN-XiaoxiaoNeural","+6%","+15Hz",{}),
 ("buddy","邻家少年","zh-CN-YunxiNeural","+10%","+10Hz",{}),
 ("mature","成熟男声","zh-CN-YunjianNeural","+3%","-10Hz",{}),
 ("live","活泼少女","zh-CN-XiaoyiNeural","+12%","+35Hz",{}),
 ("story","说书人","zh-HK-HiuMaanNeural","+5%","+10Hz",{}),
 ("calm","安静叙述","zh-TW-HsiaoChenNeural","+4%","+0Hz",{}),
 ("boy","男孩清亮","zh-CN-YunxiaNeural","+9%","+55Hz",{}),
],
}

def build_jobs(category, start_idx, count):
    fams = FAMILIES[category]; texts = TEXTS[category]; nF = len(fams)
    jobs = []
    for i in range(start_idx, start_idx + count):
        fam = fams[(i + i // len(texts)) % nF]
        jobs.append({
            "idx": i,
            "slug": "%s-%s-%03d" % (category, fam[0], i),
            "fam_key": fam[0], "fam_label": fam[1],
            "voice": fam[2], "rate": fam[3], "pitch": fam[4], "dsp": fam[5],
            "text": texts[i % len(texts)],
        })
    return jobs

# ---------------- audio IO + DSP --------------------------------------------
def load_any(path):
    x, sr = sf.read(path, always_2d=False, dtype="float32")
    if x.ndim > 1: x = x.mean(axis=1)
    return np.asarray(x, dtype=np.float32), sr

def to_target_sr(x, sr):
    if sr == TARGET_SR: return x.copy(), sr
    import librosa
    return librosa.resample(x, orig_sr=sr, target_sr=TARGET_SR).astype(np.float32), TARGET_SR

def trim_edges(x, sr, pad_ms=150):
    peak = float(np.abs(x).max()) or 1.0
    th = 0.02 * peak
    idx = np.where(np.abs(x) > th)[0]
    if len(idx) == 0: return x
    a, b = idx[0], idx[-1]
    p = int(sr * pad_ms / 1000)
    return x[max(0, a - p): min(len(x), b + 1 + p)]

def apply_dsp(x, sr, spec):
    x = x.copy()
    if spec.get("shift"):
        import librosa
        x = librosa.effects.pitch_shift(x, sr=sr, n_steps=float(spec["shift"])).astype(np.float32)
    t = np.arange(len(x), dtype=np.float32) / sr
    if spec.get("vib"):
        f, d = spec["vib"]
        x = x * (1.0 + d * np.sin(2 * np.pi * f * t))
    if spec.get("trem"):
        f, d = spec["trem"]
        x = x * (1.0 - d / 2 + (d / 2) * (1 + np.sin(2 * np.pi * f * t)))
    if spec.get("comb"):
        dl, g = int(spec["comb"][0]), float(spec["comb"][1])
        if dl < len(x):
            y = np.zeros_like(x); y[dl:] = x[:-dl]; x = (x + g * y) / (1 + g)
    if spec.get("cho"):
        dl, g = int(spec["cho"][0]), float(spec["cho"][1])
        d = np.maximum(1, int(sr * 0.008))
        mod = (np.sin(2 * np.pi * 0.8 * t) * 0.5 + 0.5) * d
        idx = np.clip((np.arange(len(x)) - mod.astype(np.int32)), 0, len(x) - 1)
        x = (x + g * x[idx]) / (1 + g)
    if spec.get("drive"):
        k = float(spec["drive"])
        if k > 1.0:
            x = np.tanh(k * x / 0.9) / np.tanh(k * 0.9 / 0.9 * 0.9)  # mild saturation
            x = np.tanh(k * np.clip(x, -3, 3) * 0.35) / np.tanh(k * 0.35)
    return x.astype(np.float32)

def normalize_peak(x, target=0.80):
    p = float(np.abs(x).max())
    return x if p == 0 else (x * (target / p)).astype(np.float32)

def save_pcm(x, sr, path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    xi = np.clip(np.round(x * 32767.0), -32768, 32767).astype("<i2")
    sf.write(path, xi, sr, subtype="PCM_16", format="WAV")

def slug_ok(s):
    return re.fullmatch(r"[a-z0-9\-_]+", s) is not None
