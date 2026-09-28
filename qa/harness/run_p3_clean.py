"""
[P3] clean 阶段实测: TextCleaner 对页眉/页码/水印/广告样本的真实效果
运行: python qa/harness/run_p3_clean.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness_lib as H  # noqa: E402

NOISE = {
    "url_watermark": "http://www.biquge-tw.com 更多精彩内容请访问本站",
    "ad_miandian": "求月票！求订阅！求推荐票！扫码关注公众号更新更快",
    "ad_shoufa": "更多精彩本书首发网站请牢记 手机 readers 电脑阅读",
    "page_header": "第 1 页 共 200 页",
    "page_footer": "- 12 -",
    "chapter_num_line": "3",
    "compiled_by": "本书由用户上传整理发布，章节错误请举报",
}
BODY_KEEP = [
    "林昭拖着行李走进客栈大堂，木质地板吱呀作响。",
    "老掌柜抬起头，眯着眼打量了他一番，慢悠悠地说：",
    "夜里三更，窗外忽然传来一声轻响。",
    "第二天清早，林昭在茶馆里见到了那位老熟人。",
    "他说过的话在耳边回响，一句一句，清晰得近乎刻薄。",
]


def main():
    log = H.LOGS / "p3_clean.log"
    with H.Tee(log):
        su = H.Suite("P3-clean", "run_p3_clean.py")
        r = su.new("TextCleaner 噪声行去除率")
        r.evidence_add(log)
        from modules.book_parser.text_cleaner import TextCleaner
        tc = TextCleaner()
        sample = "\n".join(list(NOISE.values()) + [""] + BODY_KEEP)
        cleaned = tc.clean(sample)
        H.kv("清洗前", f"{len(sample)} 字 / {len(sample.splitlines())} 行")
        H.kv("清洗后", f"{len(cleaned)} 字 / {len(cleaned.splitlines())} 行")
        H.kv("清洗结果", cleaned.replace("\n", " | "))
        for name, line in NOISE.items():
            left = line in cleaned
            r.check(f"噪声[{name}]被清除", not left,
                    "已删除" if not left else f"仍残留: {line}")
        for b in BODY_KEEP:
            r.check(f"正文保留: {b[:14]}...", b in cleaned, "" if b in cleaned else "误删!")

        # ---------- 真实书籍入库内容是否已被清洗过 (重复清洗) ----------
        r = su.new("clean 阶段与导入清洗的重复执行")
        be = H.start_backend(H.LOGS / "p3_backend.log")
        try:
            code, body = H.http("POST", "/books/import",
                                files={"file": ("p3_sample.txt",
                                                (H.FIXTURES / "sample_small.txt").read_bytes(),
                                                "text/plain")}, timeout=300)
            bid = body["book"]["id"] if code == 200 else None
            r.check("P3 前置: 书籍导入成功", bid is not None, str(body)[:200])
            if bid:
                code2, chs_body = H.http("GET", f"/books/{bid}/chapters", timeout=120)
                chs = H.fill_chapter_content(chs_body.get("chapters", []))
                stored = chs[0]["content"] if chs else ""
                # WF-09 后列表只下发预览：正文没补到手就等于"零输入跑幂等门"，
                # 那种门恒真但不证明任何事，所以先把它钉成一道显式断言。
                r.check("前置: 章节正文可读且非空 (懒加载出口)", bool(stored.strip()),
                        f"{len(chs)} 章, 首章 {len(stored)} 字")
                H.kv("入库首章字数", len(stored))
                H.kv("含广告行(导入即清洗后)",
                     [k for k, v in NOISE.items() if v in stored])
                again = TextCleaner().clean(stored)
                H.kv("再清洗一次后字数", len(again))
                r.check("clean 阶段为幂等(导入时已清洗)", again == stored,
                        f"差异 {len(again)-len(stored)} 字 -> pipeline clean 阶段是重复劳动")
                # 页码/页眉残留统计
                leftover = []
                for ch in chs:
                    for line in (ch["content"] or "").splitlines():
                        s = line.strip()
                        if s and (s.startswith("第 ") and s.endswith(" 页") or s.startswith("- ")
                                  and s.endswith(" -")):
                            leftover.append(s)
                r.check("页眉页码噪声被清除", not leftover, f"残留 {leftover}")

                # ---------- 真实书籍样本: 大文件噪声统计 ----------
                r2 = su.new("真实 MOBI 书籍清洗后残留噪声统计")
                mp = H.FIXTURES / "real" / "real_sample.mobi"
                code3, real = H.http("POST", "/books/import",
                                     files={"file": (mp.name, mp.read_bytes(),
                                                     "application/octet-stream")}, timeout=600)
                if code3 != 200:
                    r2.block("真实书籍导入失败", str(real)[:250])
                else:
                    rbid = real["book"]["id"]
                    _, rc = H.http("GET", f"/books/{rbid}/chapters", timeout=300)
                    rc_chs = H.fill_chapter_content(rc.get("chapters", []))
                    import re
                    pats = {
                        "纯页码行": re.compile(r"^\d{1,4}$"),
                        "第N页": re.compile(r"^第\s*\d+\s*页"),
                        "N/总数页": re.compile(r"^\d+\s*/\s*\d+$"),
                        "URL": re.compile(r"https?://|www\.", re.I),
                        "广告词": re.compile(r"求月票|求订阅|投推荐票|扫码|关注公众号|首发"),
                    }
                    counts = {k: 0 for k in pats}
                    total_lines = 0
                    total_chars = 0
                    for ch in rc_chs:
                        c = ch["content"] or ""
                        total_chars += len(c)
                        for line in c.splitlines():
                            s = line.strip()
                            if not s:
                                continue
                            total_lines += 1
                            for k, p in pats.items():
                                if p.search(s):
                                    counts[k] += 1
                    H.kv("书籍", real["book"]["title"])
                    H.kv("章节/字数", f"{len(rc_chs)} / {total_chars}")
                    H.kv("非空行数", total_lines)
                    H.kv("清洗后仍存在的噪声行", counts)
                    # 这道"零噪声"门在零输入上是恒真的（WF-09 后 content 恒 null 时就一直
                    # 恒真地"通过"），所以先要求真读到正文，再谈噪声。
                    r2.check("真书正文非空（噪声门的前置）", total_chars > 1000 and total_lines > 100,
                             f"{len(rc_chs)} 章 / {total_chars} 字 / {total_lines} 非空行")
                    r2.check("导入清洗后无噪声行残留",
                             sum(counts.values()) == 0,
                             f"合计 {sum(counts.values())} 行噪声残留 "
                             f"(text_cleaner 保守策略 + 页码/纯数字行有意保留)")
                    H.http("DELETE", f"/books/{rbid}")
                    r2.evidence_add(log)
        finally:
            if bid:
                H.http("DELETE", f"/books/{bid}")
            H.stop_backend(be)
        su.finish(exit_when_fail=False)


if __name__ == "__main__":
    main()
