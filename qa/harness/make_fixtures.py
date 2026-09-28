"""
生成 Round-1 pipeline 实测所需 fixtures (真实文件, 非 mock)。

产出 (qa/harness/fixtures/):
  sample_small.txt     600字级中文样本: 对白/页眉页码/广告水印/数字/中英混排
  sample_gbk.txt       GBK 编码 TXT (测 encoding 参数)
  sample_nochapter.txt 无任何章节标记的纯正文 (测 ChapterDetector 兜底)
  sample.epub          3 章 EPUB (ebooklib)
  sample.pdf           6 页 PDF, 含页眉页码 (PyMuPDF)
  sample_empty.txt     0 字节
  sample_bad.xyz       非法扩展名
  sample_corrupt.epub  .epub 后缀但内容为垃圾字节
  real/                -> 指向 data/books 中真实书籍的副本 (mobi/azw3)

运行: python qa/harness/make_fixtures.py
"""
import sys
import shutil
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness_lib as H  # noqa: E402

FIX = H.FIXTURES

# 600 字量级样本: 故意混入 页眉/页码/广告行/URL/数字/英文/对白
TXT_BODY = """第一章 初入京城

第 1 页 共 200 页
http://www.biquge-tw.com 更多精彩内容请访问本站
本书由用户上传整理发布，章节错误请举报

  林昭拖着行李走进客栈大堂，木质地板吱呀作响。"掌柜的，可还有空房？"他问道。
  老掌柜抬起头，眯着眼打量了他一番，慢悠悠地说："空房是有，只是价格不便宜。"
  "多少？"林昭皱了皱眉。
  "一晚二十两。"
  林昭愣住了——二十两！他在江南一整年的开销也不过三十两银子。这京城的花销，当真惊人。
  "怎么，嫌贵？"老掌柜嗤笑一声，"前门外头的悦来客栈，十两一间，你去住便是。"
  "不必了。"林昭从袖中摸出银锭，放在柜台上，"要二楼最里侧的那间。"

第二章 夜半来客

第 2 页 共 200 页
求月票！求订阅！求推荐票！扫码关注公众号更新更快

  夜里三更，窗外忽然传来一声轻响。林昭猛地睁眼，手已按上枕下的短刀。
  "谁？"他低喝。
  无人应答。月光从窗纸的破洞里漏进来，在地上投下一小片惨白。
  He walked to the window and pushed it open. 夜风灌入，吹得桌上的烛火剧烈摇晃。
  门外传来脚步声，很轻，像是踩在棉花上。他数了数，一共七步，然后停在第三步与第四步之间。
  "林公子，"一个女声在门外响起，声音低得几乎听不见，"有人要我给你送一样东西。"
  "谁让你来的？"
  "我不知道。"那女子答道，"他给了我一百两银子，只说要我把这个交给你。"
  门缝底下塞进来一张折叠的纸条。林昭弯腰捡起，指尖触到纸面时微微一颤——纸上只写了八个字：三月十五，朱雀大街。

第三章 旧事重提

第 3 页 共 200 页
更多精彩本书首发网站请牢记 手机 readers 电脑阅读

  第二天清早，林昭在茶馆里见到了那位老熟人。
  "五年不见，你倒是半点没变。"说话的是户部的周主事，依旧是那副笑面模样。
  "该变的都变了。"林昭给自己倒了杯茶，"周兄今日约我，不只是为了叙旧吧？"
  周主事放下茶盏，压低声音："昨夜宫里出了事，死的人是你父亲当年的同僚。"
  林昭端杯的手停在半空，茶汤泛起一圈细小的涟漪。
  "16 个人，"周主事竖起手指，"当年参与审理那件案子的，一共 16 个人，如今只剩不到一半。"
  "你是说，有人在逐个清算？"
  "我是说，"周主事盯着他的眼睛，"你若再查下去，下一个就是你。"
"""


def curlify(text):
    """ASCII 双引号 -> 中文弯引号 “ ” (真实中文电子书的实际写法)。"""
    out = []
    open_q = True
    for ch in text:
        if ch == '"':
            out.append("\u201c" if open_q else "\u201d")
            open_q = not open_q
        else:
            out.append(ch)
    return "".join(out)


def make_txts():
    (FIX / "sample_small.txt").write_text(TXT_BODY, encoding="utf-8")
    (FIX / "sample_gbk.txt").write_text(TXT_BODY, encoding="gbk")
    plain = "。".join(
        "他走在长长的街道上，路灯把影子拉得很长。风从巷口灌进来，吹落了几片枯叶。"
        "她站在门口等了很久，久到连她自己都以为不会有人来了。"
        "远处传来钟声，一下一下，沉入夜色。他想是时候做出选择了。"
        "桌上的茶已经凉了，他没有去喝。窗外的雨还在下，屋檐的水滴连成一线。"
        "第二天清晨，他留下一封信，转身离开了这座城市。街上行人稀少，雾气未散。"
        "她说过的话在耳边回响，一句一句，清晰得近乎刻薄。他停下脚步，抬头看天。"
        "天很蓝，蓝得不像刚下过雨。他忽然笑了一下，笑意却没有到达眼底。"
    ) * 6
    (FIX / "sample_nochapter.txt").write_text(plain, encoding="utf-8")
    (FIX / "sample_empty.txt").write_text("", encoding="utf-8")
    (FIX / "sample_bad.xyz").write_text("hello", encoding="utf-8")
    (FIX / "sample_corrupt.epub").write_bytes(b"PK\x03\x04" + b"NOT-A-REAL-EPUB" * 40)


def make_epub():
    try:
        from ebooklib import epub
    except ImportError:
        print("[skip] ebooklib 未安装")
        return
    book = epub.EpubBook()
    book.set_identifier("pyy-harness-001")
    book.set_title("测试书_三个清晨")
    book.set_language("zh")
    book.add_author("测试作者")
    chapters = []
    titles = ["第一章 清晨", "第二章 正午", "第三章 日暮"]
    bodies = [
        "第 1 页\n求推荐票\n\n林晚推开窗，清晨的雾气涌进来。她想，今天或许会下雨。\n"
        "巷口的豆浆铺照例排着长队，老板吆喝了一声，声音被雾吞掉一半。",
        "第 2 页\nhttp://www.ad-site.com 更多精彩\n\n正午的日头毒，蝉在树上叫得声嘶力竭。\n"
        "\"你当真要走？\"阿贵蹲在门槛上，头也不抬。",
        "第 3 页\n本书由网络整理发布\n\n日暮时分，码头上只剩几条空船。\n"
        "林晚把信塞进衣兜，信纸被汗浸得发软。她说不上伤心，也说不上高兴。",
    ]
    for i, (t, b) in enumerate(zip(titles, bodies)):
        ch = epub.EpubHtml(title=t, file_name=f"c{i+1}.xhtml", lang="zh")
        ch.content = f"<html><body><h2>{t}</h2><p>{b}</p></body></html>"
        book.add_item(ch)
        chapters.append(ch)
    book.toc = chapters
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = ["nav"] + chapters
    epub.write_epub(str(FIX / "sample.epub"), book, {})


def make_curly():
    """真实中文电子书用弯引号，本 fixture 用于检验 analyze/split 对标点写法的鲁棒性。"""
    (FIX / "sample_curly.txt").write_text(curlify(TXT_BODY), encoding="utf-8")
    try:
        from ebooklib import epub
    except ImportError:
        print("[skip] ebooklib 未安装")
        return
    book = epub.EpubBook()
    book.set_identifier("pyy-harness-002")
    book.set_title("测试书_三个清晨_弯引号版")
    book.set_language("zh")
    book.add_author("测试作者")
    chapters = []
    titles = ["第一章 清晨", "第二章 正午", "第三章 日暮"]
    bodies = [
        "第 1 页\n求推荐票\n\n林晚推开窗，清晨的雾气涌进来。她想，今天或许会下雨。\n"
        "巷口的豆浆铺照例排着长队，老板吆喝了一声，声音被雾吞掉一半。",
        "第 2 页\nhttp://www.ad-site.com 更多精彩\n\n正午的日头毒，蝉在树上叫得声嘶力竭。\n"
        "\u201c你当真要走？\u201d阿贵蹲在门槛上，头也不抬。\n"
        "\u201c时候到了，总要走的。\u201d他低下头，手指抠着门槛的裂缝。",
        "第 3 页\n本书由网络整理发布\n\n日暮时分，码头上只剩几条空船。\n"
        "林晚把信塞进衣兜，信纸被汗浸得发软。她说不上伤心，也说不上高兴。\n"
        "\u201c你还回来吗？\u201d身后有人喊了一声。她没有回头。",
    ]
    for i, (t, b) in enumerate(zip(titles, bodies)):
        ch = epub.EpubHtml(title=t, file_name=f"c{i+1}.xhtml", lang="zh")
        ch.content = f"<html><body><h2>{t}</h2><p>{b}</p></body></html>"
        book.add_item(ch)
        chapters.append(ch)
    book.toc = chapters
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = ["nav"] + chapters
    epub.write_epub(str(FIX / "sample_curly.epub"), book, {})


def make_pdf():
    try:
        import fitz  # PyMuPDF
    except ImportError:
        print("[skip] PyMuPDF 未安装")
        return
    doc = fitz.open()
    pages = [
        ("第一章 清晨", "林晚推开窗，清晨的雾气涌进来。她想，今天或许会下雨。",
         "清晨的巷口很静，只有豆浆铺的炉火在响。她数着台阶，一级，两级，三级。"),
        ("", "正午的日头毒，蝉在树上叫得声嘶力竭。阿贵蹲在门槛上，头也不抬。",
         "\"你当真要走？\"他终于问了一句，声音闷在嗓子里。"),
        ("第二章 正午", "日暮时分，码头上只剩几条空船。林晚把信塞进衣兜。",
         "信纸被汗浸得发软。她说不上伤心，也说不上高兴。"),
        ("", "江水拍打船舷，一下一下。远处有汽笛声，拖得很长。",
         "她忽然想起母亲说过的话，想起那句没来得及回应的告别。"),
        ("第三章 日暮", "船开了，岸上的灯一盏盏亮起来。林晚始终没有回头。",
         "水上的风比岸上大，吹得她睁不开眼。"),
        ("", "她在船舱里坐了一夜，天亮时到了另一座城。",
         "那座城的清晨，和这座城的日暮，原来是同一个时辰。"),
    ]
    for i, (title, a, b) in enumerate(pages):
        page = doc.new_page()
        if title:
            page.insert_text((72, 72), title, fontname="china-s", fontsize=18)
        page.insert_text((72, 120), a, fontname="china-s", fontsize=12)
        page.insert_text((72, 160), b, fontname="china-s", fontsize=12)
        # 页眉 + 页码 (真实书籍常见噪声)
        page.insert_text((72, 40), "测试书_三个清晨", fontname="china-s", fontsize=8)
        page.insert_text((280, 780), f"- {i+1} -", fontname="helv", fontsize=8)
    doc.save(str(FIX / "sample.pdf"))
    doc.close()


def link_real_books():
    """把 data/books 里的真实书籍 (mobi/azw3) 复制进 fixtures, 保证 P2 用真实文件测。"""
    dst = FIX / "real"
    dst.mkdir(exist_ok=True)
    picks = {
        ".mobi": H.PROJECT_ROOT / "data/books/12e2d032-a3dd-4ea7-9d1e-3a64067ed223.mobi",
        ".azw3": H.PROJECT_ROOT / "data/books/009b5b31-fab2-4336-a1f5-9c975d6d8778.azw3",
    }
    for ext, src in picks.items():
        if src.exists():
            target = dst / f"real_sample{ext}"
            if not target.exists():
                shutil.copy2(src, target)
            print(f"[ok] real fixture: {target} ({target.stat().st_size} bytes)")
        else:
            print(f"[skip] 缺真实样本 {src}")


if __name__ == "__main__":
    make_txts()
    make_epub()
    make_curly()
    make_pdf()
    link_real_books()
    print("--- fixtures ---")
    for p in sorted(FIX.rglob("*")):
        if p.is_file():
            print(f"{p.stat().st_size:>10}  {H.rel(p)}")
