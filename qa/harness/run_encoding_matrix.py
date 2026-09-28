"""[红线②] 全格式无乱码 —— 逐格式实测（任务 #103）

运行: python -X utf8 qa/harness/run_encoding_matrix.py

口径（为什么这样测，别再走回头路）：
  红线② 原文是「全格式无乱码」。此前做过的两件事都**不等价**于它：
    · 17:5x 波次 D 段 = 扫 **DB 文本列**（查历史写坏的音色名），不覆盖解析链；
    · 管家快检的乱码探针 = 只测**一本书走完全链路后的成品文本**。
  本 harness 补中间那一格：**导入期解析入口本身**（`book_service.py:162` 调的
  `ParserFactory.parse` 就是生产真路径），对 data/books 里的真书按格式逐个跑；
  外加 TXT 编码族（utf-8 / utf-8-sig / utf-16le / utf-16be / gbk / gb18030 /
  big5 / ascii）逐编码**关键句逐字还原**断言；再加未支持后缀必须走"干净拒绝"
  而不是产出一堆乱码。
  探针判据沿用管家快检同一条定义（U+FFFD / CJK 占比 / latin-1 签名，见
  `agent-ops/final-e2e/run_final_e2e.py:106-110`），另补两条更硬的：C0 控制符、
  双编码连串 `[À-ÿ]{3,}`。
  不起后端、不写 DB：避免与在飞任务抢容量，也避免污染 books 基线。
"""
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import harness_lib as H  # noqa: E402
from modules.book_parser.parser_factory import ParserFactory  # noqa: E402

BOOKS = H.PROJECT_ROOT / "data" / "books"
PER_FORMAT = 2                 # 每种格式取最小的 N 本
BIGGEST_CAP_MB = 25            # "最大真书"那一枚的字节上限（再大的单本解析要几分钟）
SCRIPT_RATIO_FLOOR = 0.65      # 正文里 CJK+ASCII 字母 的最低占比（低于=二进制垃圾）

MOJI_RUN = re.compile(r"[À-ÿŒœŠšŸŽž]{3,}")
CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
PUA = re.compile(r"[\ue000-\uf8ff]")
# 真正的"双重编码"签名：Ã/Â 后面还跟着一个 latin-1 补充字符（é→Ã© 那一族），
# 或 â€ 接符号（’→â€™）。**不能**拿单字判——本轮第一版把汉字"烫/房"当签名，
# 结果 6 本真小说全被判红（小说里当然有"房间/滚烫"），那是判据错不是产品错。
DBL_ENC = re.compile(r"[ÃÂ][\u00a0-\u00ff]|â€[\u00a0-\u00ffŒœŠšŸ˜™]")
# 管家快检同集，只作**报告**（不参与判定），保证两轮数字可比
LATIN1_SIG = "ÃÂâ€†‡烫房"


def probe(text: str) -> dict:
    ns = "".join(text.split())
    cjk = sum(1 for x in ns if "\u4e00" <= x <= "\u9fff")
    lat = sum(1 for x in ns if x.isascii() and x.isalpha())
    return {
        "chars": len(ns),
        "fffd": text.count("\ufffd"),
        "cjkbox": text.count("□"),
        "ctrl": len(CTRL.findall(text)),
        "moji_runs": len(MOJI_RUN.findall(text)),
        "dbl": len(DBL_ENC.findall(text)),
        "pua": len(PUA.findall(text)),
        "cjk": cjk,
        "latin": lat,
        "script_ratio": round((cjk + lat) / max(len(ns), 1), 3),
        "sigs_info": [x for x in LATIN1_SIG if x in text],
    }


def clean(p: dict, carried_fffd: int = 0) -> bool:
    """红线判据：解码器**自己**不得制造乱码。

    `carried_fffd` = 源文件字节里本来就有的 U+FFFD 个数（源损坏，另案 PY-326 记
    "导入期不可见"），它不算解析器的错；超出源自带数量的部分才算。
    """
    return (p["fffd"] <= carried_fffd and p["ctrl"] == 0
            and p["moji_runs"] == 0 and p["dbl"] == 0)


def source_payload(path: Path):
    """读"解析器真正会看到的字节"，数其中自带的乱码字符。

    返回 None = 该格式无法在不重跑解码器的前提下归因（如实标 unknown，不猜）。
    """
    ext = path.suffix.lower()
    try:
        if ext in (".txt", ".md", ".html", ".htm"):
            blobs = [path.read_bytes()]
        elif ext in (".epub", ".docx"):
            import zipfile
            with zipfile.ZipFile(path) as z:
                blobs = [z.read(n) for n in z.namelist()
                         if n.lower().endswith((".xhtml", ".html", ".htm", ".xml"))]
        elif ext in (".mobi", ".azw3"):
            import mobi
            tmp, _ = mobi.extract(str(path))
            blobs = [p.read_bytes() for p in Path(tmp).rglob("*.html")]
        else:
            return None
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}
    raw = b"".join(blobs)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return {"error": "源载荷不是 utf-8，自带乱码数无法按字节判"}
    return {"fffd_src": text.count("\ufffd"), "cjkbox_src": text.count("□")}


def collect_candidates(ext: str):
    files = sorted((p for p in BOOKS.glob(f"*{ext}") if p.is_file()),
                   key=lambda p: p.stat().st_size)
    picks = files[:PER_FORMAT]
    big = [p for p in files if p.stat().st_size <= BIGGEST_CAP_MB * 1024 * 1024]
    if big and big[-1] not in picks:
        picks.append(big[-1])
    return picks


def parse_one(path: Path):
    t0 = time.time()
    data = ParserFactory.parse(str(path))
    return data, round(time.time() - t0, 1)


def txt_detected_encoding(path: Path) -> str:
    """只为**报告**探测结果而二次实例化 TxtParser（断言走的是 ParserFactory 真路径）。"""
    from modules.book_parser.parsers.txt_parser import TxtParser
    p = TxtParser(str(path))
    p.parse()
    return getattr(p, "encoding", "?")


def main():
    log = H.LOGS / "encoding_matrix.log"
    with H.Tee(log):
        su = H.Suite("ENC-matrix", "run_encoding_matrix.py")

        # ---------- 0: 声明面 vs 实现面 ----------
        r = su.new("支持格式清单: 前端 accept 字面量 == 后端 FORMAT_MAP == API 拒绝门")
        src = (H.PROJECT_ROOT / "frontend/src/routes/ImportPage.tsx").read_text(encoding="utf-8")
        m = re.search(r"SUPPORTED_FORMATS\s*=\s*\[([^\]]*)\]", src)
        front = sorted(set(re.findall(r"\.[a-z0-9]+", m.group(1)))) if m else []
        back = sorted(ParserFactory.FORMAT_MAP.keys())
        H.kv("前端 ImportPage", front)
        H.kv("后端 FORMAT_MAP", back)
        r.check("两侧格式清单逐项相等", bool(front) and front == back,
                f"diff={sorted(set(front) ^ set(back))}")
        api_src = (H.PROJECT_ROOT / "backend/api/books.py").read_text(encoding="utf-8")
        r.check("API 拒绝门读同一张表（不是第二份字面量清单）",
                "ext not in ParserFactory.FORMAT_MAP" in api_src, "books.py:105")

        # ---------- 1: TXT 编码族逐编码精确还原 ----------
        r = su.new("TXT 编码族: 逐编码解码后关键句逐字无损 + FFFD=0")
        simp = [
            "第一章 春风又绿江南岸",
            "林昭拖着行李走进客栈大堂，外面的雨下得很大。",
            "「下一个就是你。」她说。",
            "第二章 朱雀大街",
            "他走在长街上，风把落叶卷起来。",
        ]
        trad = [
            "第一章 春風又綠江南岸",
            "林昭拖著行李走進客棧大堂，外面的雨下得很大。",
            "「下一個就是你。」她說。",
            "第二章 朱雀大街",
            "他走在長街上，風把落葉捲起來。",
        ]
        txt_s = "\n\n".join(simp)
        txt_t = "\n\n".join(trad)
        # (显示名, 文本, 该文本的关键句清单, codec) —— 惰性构造：某 codec 编不出该文本
        # 时只记覆盖缺口，绝不让 harness 在构造阶段整体崩掉（上一版就栽在这里）。
        encodes = [
            ("utf-8", txt_s, simp, "utf-8"),
            ("utf-8-sig", txt_s, simp, "utf-8-sig"),
            ("utf-16-le", txt_s, simp, "utf-16"),
            ("utf-16-be", txt_s, simp, "utf_16_be_bom"),
            ("gbk", txt_s, simp, "gbk"),
            ("gb18030", txt_s, simp, "gb18030"),
            ("big5(繁体样本)", txt_t, trad, "big5"),
        ]

        def make_bytes(text: str, codec: str):
            if codec == "utf_16_be_bom":
                return b"\xfe\xff" + text.encode("utf_16_be")
            return text.encode(codec)

        tested, skipped = [], []
        for name, text, sents, codec in encodes:
            try:
                payload = make_bytes(text, codec)
            except (UnicodeEncodeError, ValueError) as e:
                skipped.append({"enc": name, "why": f"{type(e).__name__}: {e}"})
                continue
            p = H.TMP / f"enc_probe_{codec}.txt"
            p.write_bytes(payload)
            data, secs = parse_one(p)
            body = "\n".join(c.get("content", "") or "" for c in data.chapters)
            titles = "".join(c.get("title", "") or "" for c in data.chapters)
            pr, pt = probe(body), probe(titles + (data.title or ""))
            lost = [s for s in sents if s not in body]
            detected = txt_detected_encoding(p)
            H.kv(f"{name:16s}", f"{secs:4.1f}s 探测={detected:10s} 章={len(data.chapters)} "
                                f"bytes={len(payload)} FFFD={pr['fffd']} 标题FFFD={pt['fffd']} "
                                f"丢句={len(lost)}")
            tested.append({"enc": name, "detected": detected, "chapters": len(data.chapters),
                           "fffd": pr["fffd"], "lost": lost, "secs": secs})
            r.check(f"{name} 无损（FFFD=0 且 章名/书名干净 且 关键句全部命中）",
                    pr["fffd"] == 0 and clean(pt) and not lost,
                    f"lost={lost[:1]} detected={detected}")
        # 显式指定编码 = API 的 ?encoding= 查询参数走的另一条分支（txt_parser:89，
        # 该分支用 errors='replace' 解码，所以传错编码会**留下 U+FFFD**，必须实测）
        for nm, enc_name, text, sents in [("显式 encoding=gbk", "gbk", txt_s, simp),
                                          ("显式 encoding=big5", "big5", txt_t, trad)]:
            try:
                payload = text.encode(enc_name)
            except UnicodeEncodeError as e:
                skipped.append({"enc": nm, "why": f"{type(e).__name__}: {e}"})
                continue
            p = H.TMP / f"enc_explicit_{enc_name}.txt"
            p.write_bytes(payload)
            t0 = time.time()
            data = ParserFactory.parse(str(p), encoding=enc_name)
            body = "\n".join(c.get("content", "") or "" for c in data.chapters)
            lost = [s for s in sents if s not in body]
            H.kv(nm, f"{round(time.time() - t0, 1)}s FFFD={body.count(chr(0xfffd))} 丢句={lost}")
            r.check(f"{nm} 无损", body.count("\ufffd") == 0 and not lost, str(lost)[:120])
        if skipped:
            r.degrade("部分编码构造不出样本（覆盖缺口，非产品红）", str(skipped)[:300])
        su.set_meta("txt_codecs", {"tested": [x["enc"] for x in tested],
                                   "skipped": skipped})

        # ---------- 2: 真书逐格式 ----------
        per_format_rows = {}
        degenerate_rows = []      # 库里 <100 字的退化残留（不是乱码问题，是数据卫生）
        source_corrupt = []       # 源文件本身就带 U+FFFD 的书 → PY-326 证据
        for ext in sorted(ParserFactory.FORMAT_MAP):
            r = su.new(f"真书解析 {ext}: 正文/章名/元数据 三处乱码探针")
            cands = collect_candidates(ext)
            if not cands:
                r.degrade(f"data/books 内无 {ext} 真书样本", "该格式本轮未覆盖")
                continue
            rows = per_format_rows.setdefault(ext, [])
            for p in cands:
                try:
                    data, secs = parse_one(p)
                except Exception as e:
                    r.fail(f"{ext} {p.name[:12]} 解析抛错", f"{type(e).__name__}: {e}")
                    continue
                chs = data.chapters
                body = "\n".join(c.get("content", "") or "" for c in chs)
                titles = "".join((c.get("title", "") or "") for c in chs)
                meta = (data.title or "") + "|" + (data.author or "")
                pb, pt, pm = probe(body), probe(titles), probe(meta)
                src = source_payload(p)
                carried = src.get("fffd_src") if src and "fffd_src" in src else None
                added = (pb["fffd"] - carried) if carried is not None else None
                H.kv(f"{p.name[:14]:14s}{p.stat().st_size // 1024:7d}KB",
                     f"{secs:6.1f}s 章={len(chs):4d} 字={pb['chars']:8d} FFFD={pb['fffd']}"
                     f"(源自带{carried if carried is not None else '不可归因'}/新增{added}) "
                     f"方框={pb['cjkbox']} ctrl={pb['ctrl']} 连串={pb['moji_runs']} "
                     f"双编码={pb['dbl']} PUA={pb['pua']} 可读={pb['script_ratio']}")
                # <100 字的样本是库里的退化残留（测试导入留下的），它不该用
                # "可读占比"来判，单独收清单；但它**确实**被解析器读进来了，所以
                # 乱码判据照常跑。
                degenerate = pb["chars"] < 100
                if degenerate:
                    degenerate_rows.append({"file": p.name, "ext": ext, "chars": pb["chars"]})
                flag_ok = (clean(pb, pb["fffd"]) and clean(pt) and clean(pm))
                if carried is None:
                    r.degrade(f"{ext} {p.name[:12]} 乱码归属不可判定"
                              f"（{(src or {}).get('error', '该格式拿不到源载荷')}）",
                              f"产出 FFFD={pb['fffd']} 只按'不新增控制符/双编码'判")
                    r.check(f"{ext} {p.name[:12]} 控制符/双重编码连串为 0",
                            flag_ok and len(chs) >= 1,
                            f"fffd={pb['fffd']} 双编码={pb['dbl']} ctrl={pb['ctrl']}")
                else:
                    r.check(f"{ext} {p.name[:12]} 解析器不新增乱码",
                            len(chs) >= 1 and flag_ok and added <= 0
                            and (degenerate or pb["script_ratio"] >= SCRIPT_RATIO_FLOOR),
                            f"新增FFFD={added} 双编码={pb['dbl']} ctrl={pb['ctrl']} "
                            f"titles={ {k: pt[k] for k in ('fffd', 'moji_runs', 'ctrl', 'dbl')} } "
                            f"meta={ {k: pm[k] for k in ('fffd', 'dbl')} } "
                            f"可读={pb['script_ratio']}")
                rows.append({"file": p.name, "kb": p.stat().st_size // 1024, "secs": secs,
                             "chapters": len(chs), "src_carried_fffd": carried,
                             "decoder_added_fffd": added, "src": src, **pb})
                if carried:
                    source_corrupt.append({"file": p.name, "ext": ext, "fffd": carried,
                                           "cjkbox_src": src.get("cjkbox_src"),
                                           "cjkbox_parsed": pb["cjkbox"]})
        su.set_meta("real_books_rows", per_format_rows)
        su.set_meta("degenerate_samples", degenerate_rows)
        su.set_meta("source_already_corrupt", source_corrupt)

        # ---------- 3: 未支持后缀走干净拒绝 ----------
        r = su.new("未支持格式: 拒绝而非产出乱码 (.doc/.rtf/.chm/.azw/.fb2/.pdb/.exe)")
        junk = "第一章 这不是真的书".encode("gbk") + b"\x00\x01\x02binary\xff\xfe"
        for ext in [".doc", ".rtf", ".chm", ".azw", ".fb2", ".pdb", ".exe"]:
            p = H.TMP / f"unsupported{ext}"
            p.write_bytes(junk)
            got = ParserFactory.get_parser(str(p))
            H.kv(f"{ext:6s} get_parser", str(got))
            r.check(f"{ext} 不返回解析器（交由 API 400 干净拒绝）", got is None, str(got))
            try:
                ParserFactory.parse(str(p))
                threw = None
            except Exception as e:
                threw = e
            r.check(f"{ext} parse 直调时抛 ValueError 且消息可读",
                    isinstance(threw, ValueError) and "不支持" in str(threw),
                    f"{type(threw).__name__}: {str(threw)[:80]}")

        # ---------- 4: 覆盖度记账 ----------
        r = su.new("覆盖度记账: 九种格式各跑了几枚、哪些只有合成样本")
        inv = {}
        for ext in sorted(ParserFactory.FORMAT_MAP):
            files = [p for p in BOOKS.glob(f"*{ext}") if p.is_file()]
            inv[ext] = {"库内": len(files), "实跑": len(per_format_rows.get(ext, []))}
        for k, v in inv.items():
            H.kv(k, v)
        covered = sum(1 for v in inv.values() if v["实跑"])
        r.check("九种声明格式里 ≥6 种有真书实跑", covered >= 6,
                f"实跑覆盖 {covered} 种: {sorted(per_format_rows)}")
        empty = sorted(k for k, v in inv.items() if not v["库内"])
        degraded_exts = sorted({re.search(r"真书解析 (\.[a-z0-9]+)", x.name).group(1)
                                for x in su.results
                                if x.verdict == "DEGRADED" and "真书解析" in x.name})
        r.check("无真书样本的格式每一枚都留下了 DEGRADED（不静默跳过）",
                all(e in degraded_exts for e in empty),
                f"缺样本={empty} 已标 DEGRADED={degraded_exts}")
        su.set_meta("inventory", inv)
        su.set_meta("probe_defs", {
            "fffd": "U+FFFD 个数；判定看 '解码器新增数 = 产出数 - 源载荷自带数'",
            "cjkbox": "U+25A1 方框（缺字形占位符）个数，只报不判",
            "ctrl": "C0/C1 控制符（排除 \\n\\t\\r）",
            "moji_runs": "[À-ÿ]{3,} 连串",
            "dbl": r"[ÃÂ][latin-1补充] 或 â€+符号 = 真·双重编码签名",
            "sigs_info": f"管家快检同集 {LATIN1_SIG}（含汉字'烫房'，故只报不判）",
            "script_ratio_floor": SCRIPT_RATIO_FLOOR,
            "per_format": {"最小 2 本": PER_FORMAT, "最大真书上限MB": BIGGEST_CAP_MB},
            "attribution": "mobi/azw3 经 mobi.extract、epub/docx 经 zip 成员、"
                           "txt/md/html 直读；pdf 如实标'不可归因'",
        })

        # ---------- 5: 源自带乱码 / 退化残留（这两类不是解析器的错，但必须可见） ----------
        r = su.new("源文件自带乱码 + 库内退化残留: 必须可见，不许静默进合成")
        for row in source_corrupt:
            H.kv("源自带", f"{row['ext']} {row['file'][:16]} FFFD={row['fffd']} "
                          f"方框(源/解析后)={row['cjkbox_src']}/{row['cjkbox_parsed']}")
        for row in degenerate_rows:
            H.kv("退化样本", f"{row['ext']} {row['file'][:16]} 只有 {row['chars']} 字")
        if source_corrupt:
            r.degrade(f"{len(source_corrupt)} 本书的源文件本身就带 U+FFFD"
                      f"（解析器忠实搬运，非新增）→ 转缺陷条目 PY-326",
                      f"清单见 meta.source_already_corrupt；"
                      f"合计自带 FFFD {sum(x['fffd'] for x in source_corrupt)} 枚、"
                      f"方框 {sum(x['cjkbox_src'] for x in source_corrupt or [])} 枚")
        if degenerate_rows:
            r.degrade(f"库内 {len(degenerate_rows)} 个 <100 字的退化残留文件"
                      f"（可导入成功但内容无意义）", str(degenerate_rows)[:200])
        if not source_corrupt and not degenerate_rows:
            r.check("本轮无源自带乱码、无退化残留", True, "干净")
        su.finish(exit_when_fail=False)


if __name__ == "__main__":
    main()
