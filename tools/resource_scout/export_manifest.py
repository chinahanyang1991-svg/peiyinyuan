"""Export the harvest-ready source catalogue as a human + machine readable list.

This is the "可用清单" deliverable: every corpus that build_library.py can pull
from, with its license, upstream page, exact remote path filters, expected
yield and the bandwidth each clip costs.  It is generated from the same
SOURCES table the harvester uses, so the two can never drift apart.

    python tools/resource_scout/export_manifest.py
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import build_library as B   # noqa: E402
import rs_net as N           # noqa: E402

LOG = HERE / "logs"

# what one accepted clip costs, per harvest kind
COST = {
    "parquet": "1 个 row group 的 HTTP Range 读取（约 2-25 MB，只下载命中的字节）",
    "files": "1 个音频对象（约 1-8 MB，整对象下载）",
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default=str(LOG / "available_sources.json"))
    ap.add_argument("--md", default=str(LOG / "available_sources.md"))
    a = ap.parse_args()

    rows = []
    for s in B.SOURCES:
        rows.append(dict(
            key=s["key"],
            kind=s["kind"],
            repo=s["repo"],
            license=s["license"],
            license_url=s.get("license_url", ""),
            page=s.get("page", ""),
            attribution=s.get("attribution", ""),
            language=s.get("language", ""),
            path_in=list(s.get("path_in", ())),
            path_skip=list(s.get("path_skip", ())),
            max_voices=s.get("per_source_voices"),
            samples_per_voice=s.get("samples_per_voice"),
            cost_per_clip=COST.get(s["kind"], ""),
            note=s.get("note", ""),
        ))

    LOG.mkdir(parents=True, exist_ok=True)
    Path(a.json).write_text(
        json.dumps(dict(
            generated_at=datetime.now(timezone.utc).isoformat(),
            mirror_order=list(N.HF_ENDPOINTS),
            sources=rows), ensure_ascii=False, indent=2),
        encoding="utf-8")

    total_v = sum(r["max_voices"] or 0 for r in rows)
    with open(a.md, "w", encoding="utf-8") as f:
        f.write("# 可用人声资源清单（build_library.py 的 SOURCES 表）\n\n")
        f.write("生成时间: %s\n\n" % datetime.now().strftime("%Y-%m-%d %H:%M"))
        f.write("上游镜像顺序: %s\n\n" % " -> ".join(N.HF_ENDPOINTS))
        f.write("| key | 仓库 | 许可 | 语言 | 采集方式 | 目标音色数 | 每片段带宽 |\n")
        f.write("|---|---|---|---|---|---|---|\n")
        for r in rows:
            f.write("| %s | %s | %s | %s | %s | %s | %s |\n" % (
                r["key"], r["repo"], r["license"], r["language"], r["kind"],
                r["max_voices"], r["cost_per_clip"]))
        f.write("\n合计目标音色 %d 个（每个 1-3 条参考切片），全部为 CC0 / CC-BY / Apache 宽松许可。\n\n" % total_v)
        for r in rows:
            f.write("## %s  (%s)\n\n" % (r["key"], r["license"]))
            f.write("- 仓库: `%s`\n- 页面: %s\n- 署名: %s\n" % (r["repo"], r["page"], r["attribution"]))
            f.write("- 路径过滤: 命中 %s，跳过 %s\n" % (r["path_in"], r["path_skip"] or "无"))
            f.write("- 预期产出: %d 个音色 x %d 条切片\n" % (r["max_voices"], r["samples_per_voice"]))
            f.write("- 带宽: %s\n- 备注: %s\n\n" % (r["cost_per_clip"], r["note"]))

    print("[manifest] 写出 %d 个源 -> %s" % (len(rows), a.md))
    print("[manifest] 合计目标音色 %d 个" % total_v)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
