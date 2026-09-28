"""
导入期文本质量扫描 (PY-326)

背景：解码链路统一走 errors='replace'（txt_parser 的编码兜底、mobi_parser 的
chardet 复核都是），于是**源文件本身**自带的 U+FFFD / U+25A1 占位符会被解析器
"忠实搬运"进章节正文，一路无感地送到分句与合成——用户听到的杂音其实写在原文件里，
而界面上完全看不出这本书的原文就是脏的。实测样本：data/books 里《寻秦记》精排
未删版 MOBI 的源载荷自带 20 枚 U+FFFD + 151 枚方框，解析器新增 0。

本模块只负责**把脏看得见**：逐章统计四类指标并把结果放进 Book.extra_data，
不改写正文、不做清洗（清洗策略是另外的裁定）。

判定口径与 qa/harness/run_encoding_matrix.py 一致：红线看的是"解码器**自己**
不得制造乱码"，所以自带数与产出数分开记账，不在这里做归因结论。
"""

import re

FFFD = "\ufffd"                      # U+FFFD 替换符（errors='replace' 的可见痕迹）
CJK_BOX = "□"                        # U+25A1 方框（缺字形占位符）

# C0/C1 控制符，排除正文里合法的 \n \t \r
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\u0080-\u009f]")
# 真·双重编码签名：latin-1 误读 UTF-8 后必然出现的 [ÃÂ]+补充区 / â€+符号 连串
_DOUBLE_ENCODING_RE = re.compile(
    r"[ÃÂ][\u00a0-\u00ff]|â€[\u00a0-\u00ffŒœŠšŸ˜™]"
)

# 四类指标：键名 -> 中文名（只用于日志，界面文案走前端 i18n）
INDICATORS = {
    "fffd": "U+FFFD 替换符",
    "cjk_box": "U+25A1 方框占位符",
    "control_char": "控制符",
    "double_encoding": "双重编码连串",
}

_EMPTY = {"chars": 0, "fffd": 0, "cjk_box": 0, "control_char": 0,
          "double_encoding": 0, "chapters_with_fffd": 0}

_WORST_KEEP = 5          # 日志/界面只指认最脏的前若干章


def scan_text(text: str) -> dict:
    """扫描单段文本，返回四类指标计数（chars 为该段字符数）。"""
    text = text or ""
    return {
        "chars": len(text),
        "fffd": text.count(FFFD),
        "cjk_box": text.count(CJK_BOX),
        "control_char": len(_CONTROL_RE.findall(text)),
        "double_encoding": len(_DOUBLE_ENCODING_RE.findall(text)),
        "chapters_with_fffd": 1 if FFFD in text else 0,
    }


def merge(a: dict, b: dict) -> dict:
    """两枚计数相加（逐章累加用，避免把整本书拼成一个巨串再扫）。"""
    out = dict(a)
    for key, value in b.items():
        out[key] = out.get(key, 0) + value
    return out


def scan_chapters(chapters, title: str = "", author: str = "") -> dict:
    """逐章累加扫描整本书正文，并单独扫标题/作者（元数据乱码同样可见）。

    返回 {"stats": 合计, "meta": 元数据计数, "dirty": 是否任一指标非零,
          "worst_chapters": 乱码最多的前若干章（给日志与界面指认位置）}
    """
    stats = dict(_EMPTY)
    worst = []
    for chapter in chapters or []:
        one = scan_text(chapter.get("content") or "")
        stats = merge(stats, one)
        found = one["fffd"] + one["cjk_box"] + one["control_char"] + one["double_encoding"]
        if found:
            worst.append({"index": chapter.get("index"),
                          "title": (chapter.get("title") or "")[:40],
                          "fffd": one["fffd"], "cjk_box": one["cjk_box"],
                          "control_char": one["control_char"],
                          "double_encoding": one["double_encoding"]})
    meta = scan_text(f"{title or ''}\n{author or ''}")
    meta["chapters_with_fffd"] = 1 if meta["fffd"] else 0
    return {
        "stats": stats,
        "meta": {k: v for k, v in meta.items() if k != "chars"},
        "dirty": bool(stats["fffd"] or stats["cjk_box"] or stats["control_char"]
                      or stats["double_encoding"] or meta["fffd"] or meta["cjk_box"]),
        "worst_chapters": sorted(worst, key=lambda w: -(w["fffd"] + w["cjk_box"]))[:_WORST_KEEP],
    }


def findings(stats: dict) -> list:
    """把非零指标摊成 [(键名, 计数)]，供上层生成文案。"""
    return [(key, int((stats or {}).get(key) or 0)) for key in INDICATORS
            if int((stats or {}).get(key) or 0) > 0]


def summarize(report: dict) -> str:
    """人读摘要（写日志用）。含章位指认，便于直接定位到原文件。"""
    stats = (report or {}).get("stats") or {}
    found = findings(stats)
    if not found:
        return "正文未见乱码"
    parts = ", ".join(f"{INDICATORS[key]}×{count}" for key, count in found)
    worst = (report or {}).get("worst_chapters") or []
    where = ""
    if worst:
        first = worst[0]
        where = f"；最脏章 [{first['index']}]「{first['title']}」" \
                f"({first['fffd']} FFFD/{first['cjk_box']} 方框)"
    return f"{parts}（自带 vs 解析新增的归因见 qa/harness/run_encoding_matrix.py）{where}"
