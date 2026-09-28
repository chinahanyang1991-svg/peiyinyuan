"""网络采集器 (VA-3) —— 只抓"许可明确且允许再分发"的公开音频，产出 manifest.json。

上游优先级:
  a) Wikimedia Commons MediaWiki API  (list=search filetype:audio + prop=imageinfo 取
     直链与 extmetadata 的 LicenseShortName/Artist)  —— 本仓库主力来源；
  b) OpenGameArt.org API (JSON, filter[licenses]=cc0) —— 兜底，站点不可用时如实记录。

纪律:
  * 只接受 CC0 / Public domain / CC BY(无 SA/NC)；CC BY-SA、GFDL、"unknown"、
    非自由许可一律跳过并计入 skip 统计 (版权不明 = 不入库)。
  * 礼貌限速: 相邻 HTTP 请求间隔 >= MIN_INTERVAL(默认 1.2s)，失败重试 3 次并退避。
  * User-Agent 明确标注本地工具。
  * 每个下载文件先过 ingest.qc_file 预质检 (可解码/时长/峰值)，坏样本当场删除，
    不进 manifest —— 真正的入库质检仍由 ingest.py --apply 独立再跑一遍。
  * anime 分支保留入口但不抓取任何真实动画角色音频 (版权风险, 见 ANIME_NOTE)。

CLI:
    python tools/voice_library/scout.py --out tools/voice_library/manifest.json
    python tools/voice_library/scout.py --categories animal,electronic --target 10
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import uuid as uuidlib
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import requests

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
import ingest as IN  # noqa: E402

ROOT = IN.ROOT
API = "https://commons.wikimedia.org/w/api.php"
OGA_ENDPOINTS = ("https://opengameart.org/api/query/",
                 "https://opengameart.org/oaupl/v2/assets")
USER_AGENT = ("peiyinyuan-voice-library/1.0 (local desktop dubbing tool; "
              "one-shot personal-use fetch; no redistribution)")
MIN_INTERVAL = 1.2           # 秒, 相邻请求最小间隔 (礼貌限速)
MAX_RETRIES = 3
MAX_BYTES = 12 * 1024 * 1024  # 12MB 以上多半是整曲/长录音, 不下
# 深采带宽门: playtime 明确超界直接不下(省带宽); playtime 缺失时, 只有"小体积压缩
# 音频"才当作廉价的"下载后再判定"候选, 大文件没有时长依据就不赌。
CAND_MAX_BYTES = MAX_BYTES
COMPRESS_SAFE_BYTES = 3 * 1024 * 1024   # <=3MB 且无时长 -> 下载代价小, 交给预质检裁决
AUDIO_MIME_OK = re.compile(r"^audio/(wav|x-wav|mpeg|mp3|flac|x-flac|ogg|opus)$|"
                           r"^application/ogg$")
SKIP_MIME = re.compile(r"webm|mp4|video")
CN = IN.CATEGORIES

# 自由且可再分发 -> 允许; 其余(含 CC BY-SA / GFDL / unknown) -> 跳过并记录
ALLOW_RE = re.compile(r"^(cc0|public domain|pd|cc by (\d(\.\d)?)?|cc-by(?!-sa)( \d(\.\d)?)?)$",
                      re.I)
ANIME_NOTE = (
    "anime 类不抓取任何真实动画角色台词/声优音频：这些录音受著作权与表演者权双重"
    "保护，CC0/PD 池里也不存在合规素材，抓取即侵权。改由主程序用语音设计"
    "(TTS voice design / 音色描述) 生成原创二次元音色，本采集器只保留 scout_anime() "
    "入口占位，产出 0 条。")

_LAST_REQUEST = [0.0]


# --------------------------------------------------------------------------- #
# 礼貌 HTTP
# --------------------------------------------------------------------------- #
def _throttle():
    wait = MIN_INTERVAL - (time.monotonic() - _LAST_REQUEST[0])
    if wait > 0:
        time.sleep(wait)
    _LAST_REQUEST[0] = time.monotonic()


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT,
                      "Accept-Encoding": "gzip, deflate",
                      "Api-User-Agent": USER_AGENT})
    return s


def api_get(sess, params, errors, tag):
    """MediaWiki API GET, 带限速 + 3 次退避重试; 失败返回 None (如实记录不抛)。"""
    base = dict(params)
    base["format"] = "json"
    for attempt in range(MAX_RETRIES):
        _throttle()
        try:
            r = sess.get(API, params=base, timeout=45)
        except Exception as e:
            errors.append("%s: %s: %s" % (tag, type(e).__name__, str(e)[:120]))
            time.sleep(2.0 * (attempt + 1))
            continue
        if r.status_code == 429:
            errors.append("%s: HTTP 429 限流, 退避重试" % tag)
            time.sleep(6.0 * (attempt + 1))
            continue
        if r.status_code != 200:
            errors.append("%s: HTTP %s" % (tag, r.status_code))
            time.sleep(2.0 * (attempt + 1))
            continue
        try:
            d = r.json()
        except Exception as e:
            errors.append("%s: 非 JSON 响应 %s" % (tag, str(e)[:80]))
            continue
        if "error" in d:
            errors.append("%s: API error %s" % (tag, json.dumps(d["error"])[:160]))
            return None
        return d
    errors.append("%s: 重试 %d 次仍失败" % (tag, MAX_RETRIES))
    return None


# --------------------------------------------------------------------------- #
# Wikimedia Commons
# --------------------------------------------------------------------------- #
def commons_search(sess, query, limit, errors):
    """list=search filetype:audio -> 文件标题列表。"""
    titles, cont = [], None
    while len(titles) < limit:
        p = {"action": "query", "list": "search", "srsearch": query,
             "srnamespace": "6", "srlimit": str(min(50, limit - len(titles)))}
        if cont:
            p["srcontinue"] = cont
        d = api_get(sess, p, errors, "search:%s" % query[:40])
        if not d:
            break
        titles += [x["title"] for x in d.get("query", {}).get("search", [])]
        cont = d.get("continue", {}).get("srcontinue")
        if not cont:
            break
    return titles[:limit]


def strip_html(s):
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = (s.replace("&nbsp;", " ").replace("&#97;?", "?").replace("&#39;", "'")
         .replace("&amp;", "&").replace("&quot;", '"'))
    return re.sub(r"\s+", " ", s).strip()


def flat_meta(ii):
    """把 imageinfo.metadata (getID3 展平列表) 变成 {name: value}。"""
    out = {}
    for m in (ii or {}).get("metadata") or []:
        if isinstance(m, dict) and "name" in m:
            out[m["name"]] = m.get("value")
    return out


def parse_playtime(ii):
    """尽量在下载前估出音频秒数; 拿不到返回 None。
    优先级: playtime_seconds -> playtime_string(mm:ss / h:mm:ss) -> WAV 用 size 反推。"""
    fl = flat_meta(ii)
    ps = fl.get("playtime_seconds")
    try:
        if ps is not None:
            v = float(ps)
            if v > 0:
                return v
    except (TypeError, ValueError):
        pass
    s = fl.get("playtime_string")
    if isinstance(s, str) and ":" in s:
        try:
            parts = [float(p) for p in s.split(":")]
            v = 0.0
            for p in parts:
                v = v * 60 + p
            if v > 0:
                return v
        except (TypeError, ValueError):
            pass
    mime = (ii or {}).get("mime", "")
    if mime in ("audio/wav", "audio/x-wav") and ii.get("size"):
        sr = fl.get("sample_rate") or fl.get("samplerate")
        ch = fl.get("channels")
        bits = fl.get("samplebits") or fl.get("bits_per_sample") or 16
        try:
            sr, ch, bits = float(sr), float(ch), float(bits)
            if sr > 0 and ch > 0:
                return max(0.0, (ii["size"] - 44.0) / (sr * ch * bits / 8.0))
        except (TypeError, ValueError):
            pass
    return None


def commons_info(sess, titles, errors):
    """批量 imageinfo: 直链/大小/mime + LicenseShortName/Artist/Attribution + 时长估计。"""
    out = {}
    for i in range(0, len(titles), 40):
        chunk = titles[i:i + 40]
        d = api_get(sess, {"action": "query", "titles": "|".join(chunk),
                           "prop": "imageinfo",
                           "iiprop": "url|size|mime|metadata|extmetadata",
                           "iiextmetadatafilter":
                               "LicenseShortName|License|Artist|Attribution|"
                               "Author|Credit|ImageDescription|ObjectName|"
                               "CopyrightStatus"},
                    errors, "imageinfo x%d" % len(chunk))
        if not d:
            continue
        for p in d.get("query", {}).get("pages", {}).values():
            ii = p.get("imageinfo")
            if not ii:
                continue
            em = ii[0].get("extmetadata", {})
            gv = lambda k: strip_html(em.get(k, {}).get("value", ""))
            out[p["title"]] = {
                "url": ii[0].get("url"), "size": ii[0].get("size") or 0,
                "mime": ii[0].get("mime") or "",
                "license": gv("LicenseShortName") or gv("License") or "unknown",
                "artist": gv("Artist"), "attribution": gv("Attribution"),
                "author": gv("Author"), "credit": gv("Credit"),
                "desc": gv("ImageDescription"), "object_name": gv("ObjectName"),
                "playtime": parse_playtime(ii[0]),
                "page_url": "https://commons.wikimedia.org/wiki/" + quote(p["title"]),
            }
    return out


def best_attribution(m) -> str:
    """署名链: Attribution -> Artist -> Author -> Credit。
    CC BY 依法要求署名，BioAcoustica 一类文件只在 Credit 里给出处，
    因此必须兜底到 Credit，否则署名栏会是空的。"""
    for k in ("attribution", "artist", "author", "credit"):
        v = (m or {}).get(k) or ""
        v = v.strip()
        if v:
            return v[:200]
    return ""


def license_ok(lic: str):
    """True = 允许再分发入库。返回 (allowed, 归类原因)。"""
    l = (lic or "").strip()
    if not l or l.lower() in ("unknown", "n/a", "-"):
        return False, "license_unknown"
    if ALLOW_RE.match(l):
        return True, "allowed"
    return False, "license_not_allowed"


def _stem(title: str) -> str:
    t = re.sub(r"^File:", "", title)
    t = re.sub(r"\.(wav|ogg|oga|mp3|flac|opus|m4a|weba?|webm)$", "", t, flags=re.I)
    t = re.sub(r"[_/]+", " ", t)
    return re.sub(r"\s+", " ", t).strip(" -")


def title_to_name(title: str, category: str, hint: str = "") -> str:
    """名字只陈述上游标题, 采集提示词进 tags —— 避免"查狗叫却拿到达摩"这类错标。"""
    return ("%s-%s" % (CN.get(category, category), _stem(title)[:56]))[:70]


def title_family(title: str) -> str:
    """同一条录音的多个变体 (Chainsaw 2/3/6, mmc-01/02) 归成一个粗粒度"家族"。"""
    s = re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", _stem(title).lower())
    return s[:22] or s


PRONUNCIATION_RE = re.compile(r"^(LL-[A-Za-z0-9-]+|\w+-LL-Q)", re.I)


# --------------------------------------------------------------------------- #
# 深采: 资格门 + Commons CategoryTree 遍历
# --------------------------------------------------------------------------- #
def eligibility(m, category):
    """下载前的廉价裁决 (省带宽). 返回 (verdict, reason)
    verdict: 'ok' 可下载 / 'skip' 跳过. reason 记入统计."""
    if not m or not m.get("url"):
        return "skip", "no_url"
    key = (m["url"] or "").split("?")[0]
    mime = m.get("mime") or ""
    if SKIP_MIME.search(mime) or not (
            AUDIO_MIME_OK.match(mime) or
            re.search(r"\.(wav|ogg|oga|mp3|flac|opus)$", key, re.I)):
        return "skip", "not_free_audio_format"
    if category != "human" and PRONUNCIATION_RE.search(
            re.sub(r"^File:", "", m.get("_title", ""))):
        return "skip", "pronunciation_file"
    size = m.get("size") or 0
    if size and size > CAND_MAX_BYTES:
        return "skip", "oversize"
    ok, why = license_ok(m.get("license"))
    if not ok:
        return "skip", why           # license_unknown / license_not_allowed
    pt = m.get("playtime")
    if pt is not None:
        # 有明确时长 -> 直接按时长门拦掉, 绝不浪费带宽
        if pt > IN.MAX_DURATION_S + 0.5:
            return "skip", "dur_gate_too_long"
        if pt < IN.MIN_DURATION_S - 0.05:
            return "skip", "dur_gate_too_short"
    else:
        # 无时长 -> 只有小体积才当作廉价"下载后再判"的候选
        if size > COMPRESS_SAFE_BYTES:
            return "skip", "no_duration_oversize"
    return "ok", "eligible"


def category_tree(sess, root, max_depth, errors, max_cats=120):
    """BFS 走 CategoryTree, 返回 [(title, depth)]; 含 root 自身。"""
    seen = set([root])
    order = [(root, 0)]
    frontier = [root]
    depth = 0
    while frontier and depth < max_depth and len(order) < max_cats:
        nxt = []
        for cat in frontier:
            cont = None
            while True:
                p = {"action": "query", "list": "categorymembers",
                     "cmtitle": cat, "cmtype": "subcat", "cmlimit": "100"}
                if cont:
                    p["cmcontinue"] = cont
                d = api_get(sess, p, errors, "tree:%s" % cat[:28])
                if not d:
                    break
                for x in d.get("query", {}).get("categorymembers", []):
                    t = x["title"]
                    if t not in seen and len(seen) < max_cats:
                        seen.add(t)
                        order.append((t, depth + 1))
                        nxt.append(t)
                cont = d.get("continue", {}).get("cmcontinue")
                if not cont:
                    break
                if len(order) >= max_cats:
                    break
            if len(order) >= max_cats:
                break
        frontier = nxt
        depth += 1
    return order


def category_files(sess, cat, cap, errors):
    """列出一个分类下的 File: 成员标题 (分页)."""
    out, cont = [], None
    while len(out) < cap:
        p = {"action": "query", "list": "categorymembers", "cmtitle": cat,
             "cmtype": "file", "cmlimit": "50"}
        if cont:
            p["cmcontinue"] = cont
        d = api_get(sess, p, errors, "files:%s" % cat[:28])
        if not d:
            break
        out += [x["title"] for x in d.get("query", {}).get("categorymembers", [])]
        cont = d.get("continue", {}).get("cmcontinue")
        if not cont:
            break
    return out[:cap]


def enumerate_pool(sess, category, roots, cap_per_cat, errors, report,
                   stop_at, max_cats=40, budget_s=240):
    """遍历每个 root 的 CategoryTree, 批量取 imageinfo, 过资格门 -> 候选池.
    根类深扫 (cap_per_cat), BFS 子类浅扫 (deep_subcat_cap) 以免组合爆炸;
    budget_s 挂钟预算内熔断。候选按来源子类打标签, 供上层轮询配额均匀。"""
    import time as _t
    pool = []
    seen_titles = set()
    lic_hist = report["rejected_licenses"]
    subcap = report.get("deep_subcat_cap", 60)
    t0 = _t.monotonic()
    report["enum_budget_hit"] = False
    for root in roots:
        tree = category_tree(sess, root, report.get("deep_max_depth", 2),
                             errors, max_cats=max_cats)
        for cat, depth in tree:
            if len(pool) >= stop_at:
                return pool
            if _t.monotonic() - t0 > budget_s:
                report["enum_budget_hit"] = True
                report["errors"].append("enum_budget:%s t>%.0fs" % (category, budget_s))
                return pool
            cap = cap_per_cat if depth == 0 else min(cap_per_cat, subcap)
            titles = category_files(sess, cat, cap, errors)
            if not titles:
                continue
            fresh = [t for t in titles if t not in seen_titles]
            for t in fresh:
                seen_titles.add(t)
            infos = commons_info(sess, fresh, errors)
            kept_here = 0
            for t in fresh:
                m = infos.get(t)
                if not m:
                    continue
                m["_title"] = t
                verdict, why = eligibility(m, category)
                if verdict == "skip":
                    if why in ("license_not_allowed", "license_unknown"):
                        lic_hist[m.get("license") or "?"] = \
                            lic_hist.get(m.get("license") or "?", 0) + 1
                    report["pool_skipped"][why] = report["pool_skipped"].get(why, 0) + 1
                    continue
                pool.append({"title": t, "cat": cat, "depth": depth,
                             "key": (m["url"] or "").split("?")[0], "info": m})
                kept_here += 1
            report["pool_by_source_cat"][cat] = \
                report["pool_by_source_cat"].get(cat, 0) + kept_here
    return pool


def search_pool(sess, category, queries, per_query, errors, report):
    """关键词检索兜底: 走既有 search+imageinfo, 但同样过资格门 (含时长预筛)。"""
    pool = []
    for hint, query in queries:
        titles = commons_search(sess, query, per_query, errors)
        if not titles:
            continue
        infos = commons_info(sess, titles, errors)
        taken = 0
        for t in titles:
            if taken >= report.get("per_query_cap", 3):
                break
            m = infos.get(t)
            if not m:
                continue
            m["_title"] = t
            verdict, why = eligibility(m, category)
            if verdict == "skip":
                if why in ("license_not_allowed", "license_unknown"):
                    lic = m.get("license") or "?"
                    report["rejected_licenses"][lic] = report["rejected_licenses"].get(lic, 0) + 1
                report["pool_skipped"][why] = report["pool_skipped"].get(why, 0) + 1
                continue
            pool.append({"title": t, "cat": "search:%s" % hint, "depth": 9,
                         "key": (m["url"] or "").split("?")[0], "info": m})
            taken += 1
    return pool


# 深采分类根 (按实测存在且有音频的分类; CategoryTree 会自动向下展开子树)
DEEP_ROOTS = {
    "animal": [
        "Category:Files from BioAcoustica",
        "Category:Audio files of animal sounds from the United States Fish and Wildlife Service",
        "Category:Cat sounds", "Category:Dogs barking", "Category:Sounds of birds",
        "Category:Roosters crowing", "Category:Neighing horses", "Category:Stridulation",
        "Category:Singing birds", "Category:Bird sounds from GvNL",
        "Category:Wildlife Sounds in the British Library",
    ],
    "mechanical": [
        "Category:Steam whistles", "Category:Steam locomotive whistles",
        "Category:Sounds of ticking", "Category:Foghorns",
        "Category:Sounds of warning sirens", "Category:Air raid sirens",
        "Category:School bells", "Category:Vehicle horns", "Category:Automobile horns",
        "Category:Sounds of drum machines", "Category:Thunder sheets",
        "Category:Sound effects", "Category:Pneumatic tools",
    ],
    "electronic": [
        "Category:Sine wave tones", "Category:Reference tones",
        "Category:Synthetic sounds", "Category:Synthesizer sound",
        "Category:Shepard tone", "Category:Dial tones", "Category:Beep sounds",
        "Category:Computer generated beep sounds", "Category:White noise",
        "Category:Colors of noise", "Category:Dual-tone multi-frequency signaling",
    ],
}
# 兜底检索 (主要给机械类凑量; 走同一资格门 + 时长预筛)
DEEP_SEARCH = {
    "animal": [("bird", "filetype:audio bird call"), ("frog", "filetype:audio frog chorus"),
               ("owl", "filetype:audio owl hoot"), ("dog", "filetype:audio dog bark"),
               ("cat", "filetype:audio cat meow"), ("horse", "filetype:audio horse gallop"),
               ("bee", "filetype:audio bee buzz"), ("insect", "filetype:audio cricket chirp")],
    "mechanical": [("chainsaw", "filetype:audio chainsaw"), ("clock", "filetype:audio clock chime"),
                   ("typewriter", "filetype:audio typewriter typing"),
                   ("printer", "filetype:audio printer printing"),
                   ("sewing", "filetype:audio sewing machine"), ("engine", "filetype:audio engine idling"),
                   ("motor", "filetype:audio electric motor"), ("gear", "filetype:audio gear shifting"),
                   ("drill", "filetype:audio power drill"), ("hammer", "filetype:audio hammer hitting"),
                   ("train", "filetype:audio train wheels"), ("shutter", "filetype:audio camera shutter"),
                   ("siren", "filetype:audio siren"), ("bell", "filetype:audio bell ringing"),
                   ("lathe", "filetype:audio machinery workshop")],
    "electronic": [("beep", "filetype:audio beep tone"), ("sine", "filetype:audio sine tone"),
                   ("sweep", "filetype:audio frequency sweep"), ("glitch", "filetype:audio glitch"),
                   ("arcade", "filetype:audio arcade sound"), ("modem", "filetype:audio modem"),
                   ("dial", "filetype:audio dial tone"), ("synth", "filetype:audio synthesizer"),
                   ("8bit", "filetype:audio 8-bit chiptune"), ("boot", "filetype:audio startup sound")],
}


def scout_deep_category(sess, category, target, incoming, seen_urls, report):
    """深采一类: 分类树池 + 检索兜底池 -> 轮询取用 -> 下载 -> 真预质检."""
    roots = DEEP_ROOTS.get(category, [])
    stop_at = int(target * 1.8) + 12
    report["deep_max_depth"] = report.get("deep_max_depth", 2)
    report["per_query_cap"] = report.get("per_query_cap", 3)
    report["deep_subcat_cap"] = report.get("deep_subcat_cap", 60)
    enum_budget = report.get("enum_budget_s", 300)
    pool = enumerate_pool(sess, category, roots, report.get("cap_per_cat", 120),
                          report["errors"], report, stop_at, budget_s=enum_budget)
    report["pool_from_categories"] = len(pool)
    print("[deep:%s] 分类树候选池 = %d (来源子类 %d 个%s)" %
          (category, len(pool), len(report["pool_by_source_cat"]),
           ", 枚举预算熔断" if report.get("enum_budget_hit") else ""))
    # 不够 -> 关键词检索兜底补池
    if len(pool) < stop_at and DEEP_SEARCH.get(category):
        pool += search_pool(sess, category, DEEP_SEARCH[category],
                            report.get("search_limit", 30), report["errors"], report)
        print("[deep:%s] + 检索兜底 -> 候选池 %d" % (category, len(pool)))

    # 轮询取用: 每个来源子类每轮最多贡献 1 条 -> 逐子类配额均匀
    by_cat = {}
    for c in pool:
        by_cat.setdefault(c["cat"], []).append(c)
    families = {}
    got, dl_tried = [], 0
    rounds = 0
    while len(got) < target and rounds < 1000:
        progressed = False
        for cat, lst in by_cat.items():
            if len(got) >= target:
                break
            cat_taken = report["cat_taken"].get(cat, 0)
            if cat_taken >= report.get("per_cat_ingest_cap", 12):
                continue
            while lst:
                c = lst.pop(0)
                if c["key"] in seen_urls:
                    continue
                if PRONUNCIATION_RE.search(re.sub(r"^File:", "", c["title"])):
                    report["skipped"]["pronunciation_file"] += 1
                    continue
                fam = title_family(c["title"])
                if families.get(fam, 0) >= report.get("family_cap", 2):
                    report["skipped"]["same_family_cap"] += 1
                    continue
                dl_tried += 1
                m = c["info"]
                dest = incoming / ("%s%s" % (uuidlib.uuid4().hex[:16],
                                             ext_for(m["mime"], c["key"])))
                if not download(sess, m["url"], dest, report["errors"]):
                    report["skipped"]["download_failed"] += 1
                    break
                qc = IN.qc_file(str(dest))
                if not qc["ok"]:
                    k = "pre_qc_" + qc["reason"].split(":")[0]
                    report["skipped"][k] = report["skipped"].get(k, 0) + 1
                    report["pre_qc_rejected"].append({"title": c["title"],
                                                      "reason": qc["reason"]})
                    dest.unlink(missing_ok=True)
                    break
                seen_urls.add(c["key"])
                families[fam] = families.get(fam, 0) + 1
                report["cat_taken"][cat] = cat_taken + 1
                got.append({
                    "category": category,
                    "name": title_to_name(c["title"], category),
                    "source_url": c["key"], "page_url": m["page_url"],
                    "license": m["license"],
                    "attribution": best_attribution(m),
                    "local_file": str(dest),
                    "duration_s": qc["metrics"]["duration_s"], "peak": qc["metrics"]["peak"],
                    "rms": qc["metrics"]["rms"],
                    "source_sample_rate": qc["metrics"]["sample_rate"],
                    "source_bytes": qc["metrics"]["bytes"],
                    "source_site": "wikimedia_commons",
                    "upstream_title": c["title"],
                    "tags": [re.sub(r"^Category:", "", c["cat"])[:12]],
                    "age_group": human_age_group(c["title"]) if category == "human" else None,
                })
                report["downloaded"] += 1
                progressed = True
                if len(got) % 15 == 0:
                    print("[deep:%s] 已攒 %d/%d (下载尝试 %d, 子类轮询第 %d 轮)" %
                          (category, len(got), target, dl_tried, rounds), flush=True)
                break
        if not progressed:
            break
        rounds += 1
    report["pool_deep"] = report.get("pool_deep", []) + [
        {"category": category, "pool": len(pool),
         "downloads": dl_tried, "kept": len(got)}]
    return got


# --------------------------------------------------------------------------- #
# 下载 + 预质检
# --------------------------------------------------------------------------- #
def download(sess, url, dest: Path, errors):
    _throttle()
    for attempt in range(MAX_RETRIES):
        try:
            with sess.get(url, timeout=90, stream=True) as r:
                if r.status_code in (403, 429):
                    time.sleep(4.0 * (attempt + 1))
                    continue
                if r.status_code == 404:
                    errors.append("download 404 %s" % url[:90])
                    return False
                if r.status_code != 200:
                    errors.append("download HTTP%d %s" % (r.status_code, url[:80]))
                    break
                got = 0
                with open(dest, "wb") as fh:
                    for blk in r.iter_content(65536):
                        fh.write(blk)
                        got += len(blk)
                        if got > MAX_BYTES:
                            errors.append("download 超限 %s" % url[:80])
                            return False
            return dest.exists() and dest.stat().st_size > 44
        except Exception as e:
            errors.append("download %s: %s" % (type(e).__name__, str(e)[:90]))
            time.sleep(2.0 * (attempt + 1))
    return False


def ext_for(mime, url):
    m = re.search(r"\.(wav|ogg|oga|mp3|flac|opus|weba)$", url, re.I)
    if m:
        return "." + m.group(1).lower()
    return {"audio/wav": ".wav", "audio/x-wav": ".wav", "audio/mpeg": ".mp3",
            "audio/mp3": ".mp3", "audio/flac": ".flac", "audio/x-flac": ".flac",
            "application/ogg": ".ogg", "audio/ogg": ".ogg"}.get(mime, ".bin")


# --------------------------------------------------------------------------- #
# OpenGameArt 兜底源
# --------------------------------------------------------------------------- #
def opengameart_search(sess, keyword, want, incoming: Path, errors):
    """CC0 过滤的 OGA 检索; 站点不可用/无 JSON 时返回 [] 并记录 (不编造)。"""
    items = []
    for ep in OGA_ENDPOINTS:
        _throttle()
        try:
            r = sess.get(ep, params={"search": keyword, "type": "sound",
                                     "filter[licenses]": "cc0", "limit": 20},
                         timeout=30)
        except Exception as e:
            errors.append("opengameart %s: %s" % (ep.split("/")[2],
                                                  type(e).__name__))
            continue
        ct = r.headers.get("content-type", "")
        if r.status_code != 200 or "json" not in ct:
            errors.append("opengameart %s -> HTTP%s %s (接口不可用, 跳过该源)" %
                          (ep, r.status_code, ct.split(";")[0] or "?"))
            continue
        try:
            data = r.json()
        except Exception:
            errors.append("opengameart %s 响应非 JSON" % ep)
            continue
        for a in (data.get("assets") or data.get("assetsList") or
                  (data if isinstance(data, list) else [])):
            for f in (a.get("files") or []):
                fu = f.get("url") or ""
                if not re.search(r"\.(wav|ogg|mp3|flac)$", fu, re.I):
                    continue
                lic = (f.get("license") or a.get("license") or "cc0")
                items.append({
                    "name_hint": keyword, "title": a.get("title") or fu.rsplit("/", 1)[-1],
                    "url": fu, "page_url": a.get("url") or fu,
                    "license": {"cc0": "CC0", "cc-by": "CC BY 4.0",
                                "ccby": "CC BY 4.0"}.get(str(lic).lower(), str(lic)),
                    "attribution": a.get("username") or "OpenGameArt",
                    "size": f.get("filesize") or 0, "source_site": "opengameart",
                })
        if items:
            break
    return items


# --------------------------------------------------------------------------- #
# 采集计划
# --------------------------------------------------------------------------- #
PLAN = {
    "animal": [
        ("猫叫", "filetype:audio cat meow"), ("狗叫", "filetype:audio dog bark"),
        ("鸟鸣", "filetype:audio bird call"), ("马嘶", "filetype:audio horse neigh"),
        ("牛叫", "filetype:audio cow moo"), ("猪叫", "filetype:audio pig oink"),
        ("鸭叫", "filetype:audio duck quack"), ("蛙鸣", "filetype:audio frog croak"),
        ("狮吼", "filetype:audio lion roar"), ("鸡鸣", "filetype:audio rooster crow"),
        ("猫呼噜", "filetype:audio cat purring"), ("狼嚎", "filetype:audio wolf howl"),
        ("虫鸣", "filetype:audio cricket chirp"), ("蜂鸣", "filetype:audio bee buzz"),
    ],
    "electronic": [
        ("激光", "filetype:audio laser sound"), ("电击", "filetype:audio taser stun gun"),
        ("电子提示音", "filetype:audio beep sound"), ("街机", "filetype:audio arcade sound"),
        ("芯片音", "filetype:audio 8-bit chiptune sound"),
        ("合成器", "filetype:audio synthesizer sound"), ("故障音", "filetype:audio glitch noise"),
        ("调制解调器", "filetype:audio modem handshake"),
        ("盖革计数", "filetype:audio geiger counter"), ("开机音", "filetype:audio power on sound"),
        ("电话拨号", "filetype:audio telephone dial tone"), ("机器人", "filetype:audio robot voice"),
    ],
    "mechanical": [
        ("引擎启动", "filetype:audio engine start"), ("马达", "filetype:audio motor running"),
        ("换挡", "filetype:audio gear shifting"), ("钟表滴答", "filetype:audio clock ticking"),
        ("链锯", "filetype:audio chainsaw"), ("汽笛", "filetype:audio steam whistle"),
        ("风钻", "filetype:audio pneumatic drill"), ("缝纫机", "filetype:audio sewing machine"),
        ("打字机", "filetype:audio typewriter"), ("火车轮轨", "filetype:audio train wheels"),
        ("摩托车", "filetype:audio motorcycle exhaust"), ("喷射引擎", "filetype:audio jet engine"),
        ("快门", "filetype:audio camera shutter"), ("3D 打印机", "filetype:audio 3d printer"),
    ],
    "human": [
        ("老年男声", "filetype:audio elderly man speech"),
        ("老年女声", "filetype:audio elderly woman speaking"),
        ("老人录音", "filetype:audio old man voice recording"),
        ("儿童声", "filetype:audio child speech"),
        ("男童声", "filetype:audio boy speaking voice"),
        ("女童声", "filetype:audio girl speaking voice"),
        ("老人采访", "filetype:audio senior citizen interview"),
        ("童声中文", "filetype:audio 儿童 普通话 朗读"),
        ("老人中文", "filetype:audio 老年 普通话 采访"),
        ("童声样张", "filetype:audio children voices sample"),
    ],
}


def human_age_group(title: str):
    """人声缺口补样: 年龄段只认上游标题本身, 不认检索提示词。"""
    t = title.lower()
    if re.search(r"child|kid|boy|girl|children|infant|童|儿", t):
        return "child"
    if re.search(r"elderly|senior|aged|old (?:man|woman|lady|gentleman|person)|"
                 r"grandparent|grandmother|grandfather|\d+[- ]years? old|老年|老翁", t):
        return "senior"
    return None


def scout_category(sess, category, target, incoming: Path, seen_urls: set,
                   report: dict, per_query_cap=4, family_cap=2):
    """按 PLAN 检索 -> 许可过滤 -> 下载 -> 预质检, 攒够 target 条为止。"""
    got = []
    families = {}
    for hint, query in PLAN.get(category, []):
        if len(got) >= target:
            break
        titles = commons_search(sess, query, 30, report["errors"])
        if not titles:
            continue
        infos = commons_info(sess, titles, report["errors"])
        picked_this_query = 0
        for t in titles:
            if len(got) >= target or picked_this_query >= per_query_cap:
                break
            m = infos.get(t)
            if not m or not m.get("url"):
                continue
            key = (m["url"] or "").split("?")[0]
            if key in seen_urls:
                continue
            if category != "human" and PRONUNCIATION_RE.search(re.sub(r"^File:", "", t)):
                report["skipped"]["pronunciation_file"] += 1
                continue
            fam = title_family(t)
            if families.get(fam, 0) >= family_cap:
                report["skipped"]["same_family_cap"] += 1
                continue
            if SKIP_MIME.search(m["mime"]) or not (
                    AUDIO_MIME_OK.match(m["mime"]) or
                    re.search(r"\.(wav|ogg|oga|mp3|flac|opus)$", key, re.I)):
                report["skipped"]["not_free_audio_format"] += 1
                continue
            if m["size"] and m["size"] > MAX_BYTES:
                report["skipped"]["oversize"] += 1
                continue
            ok, why = license_ok(m["license"])
            if not ok:
                report["skipped"][why] = report["skipped"].get(why, 0) + 1
                report["rejected_licenses"][m["license"]] = \
                    report["rejected_licenses"].get(m["license"], 0) + 1
                continue
            pt = m.get("playtime")
            if pt is not None and (pt > IN.MAX_DURATION_S + 0.5 or
                                   pt < IN.MIN_DURATION_S - 0.05):
                report["skipped"]["dur_gate"] = report["skipped"].get("dur_gate", 0) + 1
                continue
            dest = incoming / ("%s%s" % (uuidlib.uuid4().hex[:16],
                                          ext_for(m["mime"], key)))
            if not download(sess, m["url"], dest, report["errors"]):
                report["skipped"]["download_failed"] += 1
                continue
            qc = IN.qc_file(str(dest))
            if not qc["ok"]:
                k = "pre_qc_" + qc["reason"].split(":")[0]
                report["skipped"][k] = report["skipped"].get(k, 0) + 1
                report["pre_qc_rejected"].append({"title": t, "reason": qc["reason"]})
                dest.unlink(missing_ok=True)
                continue
            seen_urls.add(key)
            families[fam] = families.get(fam, 0) + 1
            picked_this_query += 1
            got.append({
                "category": category,
                "name": title_to_name(t, category),
                "source_url": key,
                "page_url": m["page_url"],
                "license": m["license"],
                "attribution": best_attribution(m),
                "local_file": str(dest),
                "duration_s": qc["metrics"]["duration_s"],
                "peak": qc["metrics"]["peak"],
                "rms": qc["metrics"]["rms"],
                "source_sample_rate": qc["metrics"]["sample_rate"],
                "source_bytes": qc["metrics"]["bytes"],
                "source_site": "wikimedia_commons",
                "upstream_title": t,
                "tags": [hint],
                "age_group": human_age_group(t) if category == "human" else None,
            })
            report["downloaded"] += 1
    return got


def scout_anime(report):
    """anime 分支入口 —— 刻意不抓取, 详见 ANIME_NOTE / REPORT.md。"""
    report["anime_policy"] = ANIME_NOTE
    print("[scout] anime: 按红线跳过真实动画角色音频抓取 (0 条), 交主程序语音设计生成")
    return []


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=str(HERE / "manifest.json"))
    ap.add_argument("--incoming", default=str(IN.DEFAULT_LIBRARY / "incoming"))
    ap.add_argument("--categories", default="animal,electronic,mechanical,human")
    ap.add_argument("--target", type=int, default=12, help="每类目标条数 (8~15)")
    ap.add_argument("--report", default=str(HERE / "scout_report.json"))
    # --- 深采 (分类树遍历) 开关, 不开则走旧 PLAN 检索路径, 行为不变 ---
    ap.add_argument("--deep", action="store_true",
                    help="分类树遍历深采: 走 Commons CategoryTree 逐子类配额均匀, "
                         "下载前用 imageinfo 时长/许可/体积预筛省带宽")
    ap.add_argument("--deep-max-depth", type=int, default=2, dest="deep_max_depth",
                    help="CategoryTree 向下展开层数 (默认 2)")
    ap.add_argument("--cap-per-cat", type=int, default=120, dest="cap_per_cat",
                    help="每个分类最多枚举多少个 File 成员 (默认 120)")
    ap.add_argument("--per-cat-ingest-cap", type=int, default=12, dest="per_cat_ingest_cap",
                    help="同一来源子类最多贡献多少条入库 (配额均匀, 默认 12)")
    ap.add_argument("--family-cap", type=int, default=2, dest="family_cap",
                    help="同一条录音变体最多取几条 (默认 2)")
    args = ap.parse_args(argv)

    max_target = 400 if args.deep else 40
    target = max(1, min(int(args.target), max_target))
    cats = [c.strip() for c in args.categories.split(",") if c.strip()]
    incoming = Path(args.incoming)
    incoming.mkdir(parents=True, exist_ok=True)
    sess = make_session()
    report = {"started_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
              "mode": "deep" if args.deep else "search",
              "target_per_category": target, "user_agent": USER_AGENT,
              "min_interval_s": MIN_INTERVAL, "downloaded": 0,
              "skipped": {"not_free_audio_format": 0, "oversize": 0,
                          "download_failed": 0, "pronunciation_file": 0,
                          "same_family_cap": 0, "license_not_allowed": 0},
              "rejected_licenses": {}, "pre_qc_rejected": [], "errors": [],
              "per_category": {}, "counts": {},
              "cat_taken": {}, "pool_skipped": {}, "pool_by_source_cat": {},
              "pool_deep": [], "deep_max_depth": args.deep_max_depth,
              "cap_per_cat": args.cap_per_cat, "per_cat_ingest_cap": args.per_cat_ingest_cap,
              "family_cap": args.family_cap, "per_query_cap": args.per_cat_ingest_cap}
    items, seen_urls = [], set()
    # 幂等播种: 跳过 DB 里已入库的 101 条, 深采不重复下载同一上游文件
    try:
        _db_sess = IN.open_session(args.db if hasattr(args, "db") else str(IN.DEFAULT_DB))
        for k in IN.existing_keys(_db_sess, IN._VoiceProfile()):
            seen_urls.add(k)
            seen_urls.add(str(k).split("?")[0])
        _db_sess.close()
        print("[scout] 已播种 DB 现有 sample URL %d 个 (幂等去重)" % len(seen_urls))
    except Exception as e:
        report["errors"].append("db_seed: %s: %s" % (type(e).__name__, str(e)[:100]))
        print("[scout] 播种 DB URL 失败(不影响采集): %s" % str(e)[:80])
    for c in cats:
        if c == "anime":
            got = scout_anime(report)
        elif args.deep and c in DEEP_ROOTS:
            print("[scout] === DEEP %s (%s) 目标 %d 条 ===" % (c, CN.get(c, c), target))
            got = scout_deep_category(sess, c, target, incoming, seen_urls, report)
        elif c in PLAN:
            print("[scout] === %s (%s) 目标 %d 条 ===" % (c, CN.get(c, c), target))
            got = scout_category(sess, c, target, incoming, seen_urls, report)
        else:
            print("[scout] 未知/无深采分类根的类别, 跳过: %s" % c)
            got = []
        report["counts"][c] = len(got)
        print("[scout] %s -> %d 条通过预质检" % (c, len(got)))
        items += got

    # OGA 兜底: 仅旧检索模式且未达标时启用 (站点不可用时只在 errors 里留痕)。
    # 深采模式下不接 OGA (已确认 404 死源), 也不误触发检索兜底。
    if not args.deep:
        for c in cats:
            if c in ("anime",) or report["counts"].get(c, 0) >= max(8, target):
                continue
            need = target - report["counts"].get(c, 0)
            for hint, kw in PLAN.get(c, [])[:4]:
                if need <= 0:
                    break
                for cand in opengameart_search(sess, kw.replace("filetype:audio ", ""),
                                               need, incoming, report["errors"])[:need]:
                    ok, why = license_ok(cand["license"])
                    if not ok:
                        report["skipped"][why] = report["skipped"].get(why, 0) + 1
                        continue
                    dest = incoming / ("oga%s%s" % (uuidlib.uuid4().hex[:12],
                                                     ext_for("", cand["url"])))
                    if not download(sess, cand["url"], dest, report["errors"]):
                        continue
                    qc = IN.qc_file(str(dest))
                    if not qc["ok"]:
                        k = "pre_qc_" + qc["reason"].split(":")[0]
                        report["skipped"][k] = report["skipped"].get(k, 0) + 1
                        dest.unlink(missing_ok=True)
                        continue
                    items.append({**cand, "category": c,
                                  "name": ("%s-%s %s" % (CN[c], hint, cand["title"]))[:70],
                                  "duration_s": qc["metrics"]["duration_s"],
                                  "peak": qc["metrics"]["peak"], "rms": qc["metrics"]["rms"],
                                  "source_sample_rate": qc["metrics"]["sample_rate"],
                                  "source_bytes": qc["metrics"]["bytes"],
                                  "upstream_title": cand["title"], "tags": [hint],
                                  "age_group": None})
                    report["counts"][c] = report["counts"].get(c, 0) + 1
                    report["downloaded"] += 1
                    need -= 1

    report["finished_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    Path(args.out).write_text(json.dumps(items, ensure_ascii=False, indent=1),
                              encoding="utf-8")
    Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=1),
                                 encoding="utf-8")
    print("-" * 78)
    print("[scout] manifest 条目 %d -> %s" % (len(items), args.out))
    print("[scout] 各类: %s" % json.dumps(report["counts"], ensure_ascii=False))
    print("[scout] 许可跳过: %s" % json.dumps(
        {k: v for k, v in report["skipped"].items() if v}, ensure_ascii=False))
    if args.deep:
        print("[scout] 池内预筛(省带宽): %s" % json.dumps(
            {k: v for k, v in report["pool_skipped"].items() if v}, ensure_ascii=False))
        print("[scout] 深采逐类 池/下载/留: %s" % json.dumps(
            report["pool_deep"], ensure_ascii=False))
    print("[scout] 网络/接口异常 %d 条 (详见 %s)" % (len(report["errors"]), args.report))
    if report["errors"]:
        for e in report["errors"][:6]:
            print("        - %s" % e[:110])
    print("[scout] 下一步: python tools/voice_library/ingest.py --manifest %s --apply"
          % args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
