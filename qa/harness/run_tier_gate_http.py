"""[PY-323(a′) D③] 语种档分发门的 HTTP 真机复跑（任务 #104）

运行: python -X utf8 qa/harness/run_tier_gate_http.py

为什么还要这一跑：门本身有 41 例单测，但**真 HTTP 出口从没验证过**
（`X-Preview-Voice-Binding: tier_downgrade` 是前端徽标的唯一依据）。
`agent-ops/LANES.md` 22:4x D③ 记的就是这一格。

口径（四处容易做错的地方）：
  1. 门只在 `engine=openvoice AND tier=nonmandarin_ref` 这一格收口，且
     `fallback = Config.TTS_ENGINE`。所以测试后端**显式**以 `TTS_ENGINE=sample`
     起，并 `PYY_PRELOAD_MODEL=0` 关掉启动预热。为什么不是 edgetts（第一版栽过）：
     `edgetts_adapter.py:138-144` 的 uuid→shortName 映射**只从 `engine='edgetts'`
     的行建**，声明成 openvoice 的探针行永远解析不到，产品按"拒绝静默退化为默认
     音色"显式抛错（这是正确行为），于是 502 红在夹具而不是红在产品。兜底也不能
     留 voxcpm：要真装载 ~5GB，探针不该抢 #86 那道容量门。
  2. `sample` 引擎的语义是"回放该行自己的主样本"（`sample_playback.py`），
     零权重、不联网、`online=True` 绕开预加载单槽 —— 所以探针行必须带一枚**真实
     存在且非静音**的主样本（从库里 `engine='sample'` 的行里挑），否则它会
     按 PY-117 显式 raise，又是红在夹具。
  3. 反例刻意**不**用「openvoice + mandarin_ref」：那格门放行、真去装载 openvoice
     权重，属重引擎实测（PY-323 本体做过），在探针里再跑一遍只会撞机器。
     改判据为「非 openvoice 引擎 + 同一档」——若响应头是**无条件**发的，这格就会露馅。
  4. 写活库要有可逆层：跑前 `backup()` 快照（WAL 活库禁 copy2），探针行自造自删，
     产物按 `{voice_id}.high.*` 精确删，收尾复采三个计数回到基线才算完。
     探针只读主样本，写它的行会被删——库内原有 sample 行一个字节都不动。
"""
import ctypes
import os
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import harness_lib as H  # noqa: E402
from backend.config import Config  # noqa: E402

DB = H.PROJECT_ROOT / "data" / "peiyinyuan.db"
EVIDENCE_DIR = H.PROJECT_ROOT / "agent-ops" / "tier-gate-http"
PREVIEW_DIR = Path(Config.VOICE_DIR) / "previews_generated"
PROBE_ENGINE = "openvoice"          # 会被门拦下的声明引擎
FALLBACK_ENGINE = "sample"          # 测试后端的 Config.TTS_ENGINE，= 门的 fallback
AVAIL_FLOOR_GB = 4.0                # 轻量档点火门（本探针不碰重引擎）
LOG_NAME = "tier_gate_http_backend.log"


def avail_gb():
    k = ctypes.windll.kernel32

    class M(ctypes.Structure):
        _fields_ = [("l", ctypes.c_ulong), ("load", ctypes.c_ulong),
                    ("tp", ctypes.c_ulonglong), ("ap", ctypes.c_ulonglong),
                    ("tvp", ctypes.c_ulonglong), ("apv", ctypes.c_ulonglong),
                    ("tv", ctypes.c_ulonglong), ("av", ctypes.c_ulonglong),
                    ("aev", ctypes.c_ulonglong)]

    m = M()
    m.l = ctypes.sizeof(M)
    if not k.GlobalMemoryStatusEx(ctypes.byref(m)):
        return -1.0, -1
    return m.ap / 2 ** 30, m.load


def conn():
    return sqlite3.connect(str(DB), timeout=30)


def counts():
    with conn() as c:
        cur = c.execute("SELECT COUNT(*) FROM voice_profiles").fetchone()[0]
        ov = c.execute("SELECT COUNT(*) FROM voice_profiles WHERE engine='openvoice'").fetchone()[0]
        sm = c.execute("SELECT COUNT(*) FROM voice_profiles WHERE engine='sample'").fetchone()[0]
    files = len(list(PREVIEW_DIR.glob("*"))) if PREVIEW_DIR.exists() else 0
    return {"voices_total": cur, "voices_openvoice": ov, "voices_sample": sm,
            "preview_files": files}


def pick_master():
    """从库里 `engine='sample'` 的行挑一枚真实存在且非静音的主样本给探针行用。

    归因：`FALLBACK_ENGINE=sample` 的引擎语义是"回放该行自己的主样本"，主样本缺失
    或近静音它会按 PY-117 显式 raise —— 那时红的是夹具，不是产品。
    """
    with conn() as c:
        rows = c.execute("SELECT id, model_path, sample_audio_path FROM voice_profiles"
                         " WHERE engine='sample' ORDER BY created_at").fetchall()
    for vid, mp, sp in rows:
        p = mp or sp
        if p and os.path.isfile(p):
            st = H.audio_stats(p)
            if st.get("duration_s", 0) > 0.6 and st.get("peak", 0) > 0.05:
                return p, st
    return None, None


def insert_probe(tier, engine=PROBE_ENGINE, meta=None, master=None):
    vid = str(uuid.uuid4())
    with conn() as c:
        c.execute(
            "INSERT INTO voice_profiles (id, name, source, engine, language, gender,"
            " age_group, description, meta, audio_language_tier, sample_audio_path,"
            " created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (vid, f"PY323-D3-探针-{tier}-{vid[:8]}", "probe", engine, "zh", "female",
             "adult", "语种档分发门 HTTP 探针，跑完即删", meta, tier, master,
             time.strftime("%Y-%m-%d %H:%M:%S"), time.strftime("%Y-%m-%d %H:%M:%S")))
        c.commit()
    with conn() as c:
        got = c.execute("SELECT engine, audio_language_tier, sample_audio_path"
                        " FROM voice_profiles WHERE id=?", (vid,)).fetchone()
    return vid, got


def delete_probe(vid):
    with conn() as c:
        c.execute("DELETE FROM voice_profiles WHERE id=?", (vid,))
        c.commit()
    removed = []
    for suffix in (".wav", ".high.wav", ".json", ".high.json"):
        p = PREVIEW_DIR / f"{vid}{suffix}"
        if p.exists():
            p.unlink()
            removed.append(p.name)
    return removed


def get_preview(vid, wait_s=180):
    """真 HTTP GET 高档试听，返回 (status, 响应头 dict, 落盘 wav 路径或 None)。"""
    url = (f"{H.BASE}/api/voices/{vid}/preview?quality=high&regenerate=1"
           f"&wait={wait_s}")
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=wait_s + 30) as resp:
            body = resp.read()
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}, body
    except urllib.error.HTTPError as e:
        body = e.read()
        return e.code, {k.lower(): v for k, v in e.headers.items()}, body
    except Exception as e:
        return 0, {"error": f"{type(e).__name__}: {e}"}, b""


def main():
    H.LOGS.mkdir(parents=True, exist_ok=True)
    with H.Tee(H.LOGS / "tier_gate_http.log"):
        return _run()


def _run():
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    suite = H.Suite("TIER-gate-http", "qa/harness/run_tier_gate_http.py")

    gb, load = avail_gb()
    base = counts()
    H.kv("容量", f"avail {gb:.2f}GB / load {load}%")
    H.kv("基线计数", base)

    # ---- 用例 0: 点火门（不过门就整单自曝 DEGRADED，不冒充绿也不造红） ----
    r0 = suite.new("点火容量门: 轻量档 avail≥4GB 才写活库")
    if gb < AVAIL_FLOOR_GB:
        r0.degrade(f"avail {gb:.2f}GB < {AVAIL_FLOOR_GB}GB -> 本轮不跑（不抢内存、不写库）")
        H.kv("机器负载%", load)
        suite.finish(exit_when_fail=False)
        return 0
    r0.check("过轻量档点火门", gb >= AVAIL_FLOOR_GB, f"avail {gb:.2f}GB")

    # ---- 用例 1: 可逆层（快照 + 基线） ----
    r1 = suite.new("可逆层: 活库 backup() 快照 + 三个基线计数")
    snap = EVIDENCE_DIR / f"pre_probe_{ts}.db"
    src = sqlite3.connect(str(DB))
    dst = sqlite3.connect(str(snap))
    with dst:
        src.backup(dst)
    src.close()
    dst.close()
    r1.check("快照已落盘且非空", snap.exists() and snap.stat().st_size > 1024,
             f"{snap.name} {snap.stat().st_size/2**20:.1f}MB")
    r1.check("基线里没有 openvoice 行（门今天不可能自然触发）",
             base["voices_openvoice"] == 0, f"openvoice={base['voices_openvoice']}")
    suite.set_meta("snapshot", str(snap))
    suite.set_meta("baseline", base)

    # ---- 用例 1b: 夹具前置（没主样本就别起后端，避免红在产品上） ----
    r1b = suite.new("夹具前置: 探针行必须有可回放的 sample 主样本")
    master, mstats = pick_master()
    if not master:
        r1b.degrade("库里没有 `engine='sample'` 且主样本在盘非静音的行 -> 本轮不起后端"
                    "（夹具缺失，不是产品缺陷）")
        suite.finish(exit_when_fail=False)
        return 0
    r1b.check("挑到主样本", bool(master), os.path.basename(master))
    r1b.check("主样本非静音且时长够门（sample 引擎近静音会显式 raise，那是红在夹具）",
              mstats.get("duration_s", 0) > 0.6 and mstats.get("peak", 0) > 0.05,
              f"duration={mstats.get('duration_s')}s peak={mstats.get('peak')}")
    suite.set_meta("master", {"path": master, "duration_s": mstats.get("duration_s"),
                              "peak": mstats.get("peak")})

    # ---- 用例 2: 起测试后端（显式 sample 全局档 + 关预热） ----
    proc = None
    try:
        r2 = suite.new("测试后端: TTS_ENGINE=sample 起在 :8137 且配置真生效")
        env = dict(os.environ)
        env.update({"PYTHONIOENCODING": "utf-8", "TTS_ENGINE": FALLBACK_ENGINE,
                    "PYY_PRELOAD_MODEL": "0", "PEIYINYUAN_PORT": str(H.TEST_PORT)})
        backend_log = EVIDENCE_DIR / LOG_NAME
        proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "backend.main:app", "--host", "127.0.0.1",
             "--port", str(H.TEST_PORT), "--no-access-log"],
            cwd=str(H.PROJECT_ROOT), env=env,
            stdout=open(backend_log, "w", encoding="utf-8"), stderr=subprocess.STDOUT)
        t0 = time.time()
        health = None
        while time.time() - t0 < 90:
            try:
                with urllib.request.urlopen(f"{H.BASE}/api/health", timeout=2) as resp:
                    import json as _json
                    health = _json.loads(resp.read().decode("utf-8"))
                    break
            except Exception:
                if proc.poll() is not None:
                    break
                time.sleep(0.5)
        r2.check("health 200", bool(health), f"用时 {time.time()-t0:.1f}s")
        r2.check("兜底引擎配置真生效（否则降级后仍走 voxcpm，探针会去抢重引擎）",
                 (health or {}).get("tts_engine") == FALLBACK_ENGINE,
                 f"health.tts_engine={(health or {}).get('tts_engine')}")
        suite.set_meta("ready_s", round(time.time() - t0, 1))

        # ---- 用例 3: 正例 openvoice + nonmandarin_ref ----
        r3 = suite.new("正例: openvoice+非普通话档 -> 换兜底引擎且响应头留痕")
        vid_pos, row_pos = insert_probe("nonmandarin_ref", master=master)
        H.kv("探针行(正例)", f"{vid_pos[:8]} engine/tier 回读={row_pos}")
        r3.check("落库回读 = 声明 openvoice + 档 nonmandarin_ref + 带主样本",
                 row_pos[:2] == ("openvoice", "nonmandarin_ref") and row_pos[2] == master,
                 str(row_pos))
        st, hd, body = get_preview(vid_pos)
        H.kv("正例响应", f"status={st} origin={hd.get('x-preview-origin')} "
                         f"engine={hd.get('x-preview-engine')} "
                         f"binding={hd.get('x-preview-voice-binding')} bytes={len(body)}")
        r3.check("HTTP 200 且有音频字节（不是静音顶替）",
                 st == 200 and len(body) > 10_000, f"status={st} bytes={len(body)}")
        r3.check("响应头 X-Preview-Voice-Binding == tier_downgrade（前端徽标的唯一依据）",
                 hd.get("x-preview-voice-binding") == "tier_downgrade",
                 str(hd.get("x-preview-voice-binding")))
        r3.check("实际发声引擎 = 兜底 sample，不是声明的 openvoice",
                 hd.get("x-preview-engine") == FALLBACK_ENGINE,
                 str(hd.get("x-preview-engine")))
        wav_pos = PREVIEW_DIR / f"{vid_pos}.high.wav"
        if wav_pos.exists():
            stt = H.audio_stats(wav_pos)
            r3.check("产物过双门口径（时长>0.2s 且 peak>0.05，非静音）",
                     stt.get("duration_s", 0) > 0.2 and stt.get("peak", 0) > 0.05,
                     f"duration={stt.get('duration_s')}s peak={stt.get('peak')}")
            r3.check("产物就是该引擎回放的自身主样本（证明 sample 兜底真跑了，不是缓存顶包）",
                     abs(stt.get("duration_s", 0) - mstats.get("duration_s", 0)) < 0.3,
                     f"产物 {stt.get('duration_s')}s vs 主样本 {mstats.get('duration_s')}s")
        else:
            r3.degrade("产物 wav 不在预期键位，峰值断言本轮未覆盖", str(wav_pos))
        txt = backend_log.read_text(encoding="utf-8", errors="replace") if backend_log.exists() else ""
        r3.check("原因留痕：后端日志有『语种档降级』WARNING（不许静默换嗓子）",
                 "语种档降级" in txt, f"log={backend_log.name}")
        suite.set_meta("positive", {"id": vid_pos, "status": st,
                                    "binding": hd.get("x-preview-voice-binding"),
                                    "engine": hd.get("x-preview-engine"),
                                    "origin": hd.get("x-preview-origin")})

        # ---- 用例 4: 反例 sample + 同一档 -> 不改路由、不发降级头 ----
        r4 = suite.new("反例: 非 openvoice 引擎 + 同一档 -> 门不得乱拦")
        vid_neg, row_neg = insert_probe("nonmandarin_ref", engine=FALLBACK_ENGINE,
                                        master=master)
        r4.check("落库回读 = sample + nonmandarin_ref",
                 row_neg[:2] == (FALLBACK_ENGINE, "nonmandarin_ref"), str(row_neg[:2]))
        st2, hd2, body2 = get_preview(vid_neg)
        H.kv("反例响应", f"status={st2} engine={hd2.get('x-preview-engine')} "
                         f"binding={hd2.get('x-preview-voice-binding')} bytes={len(body2)}")
        r4.check("HTTP 200（同一兜底引擎，路由未变）",
                 st2 == 200 and len(body2) > 10_000, f"status={st2} bytes={len(body2)}")
        r4.check("响应头不得出现 tier_downgrade（证明它不是一条无条件头）",
                 hd2.get("x-preview-voice-binding") != "tier_downgrade",
                 f"binding={hd2.get('x-preview-voice-binding')!r}")
        r4.check("对照组标 own_engine（该行的嗓子就是它自己声明的引擎）",
                 hd2.get("x-preview-voice-binding") == "own_engine",
                 f"binding={hd2.get('x-preview-voice-binding')!r}")
        suite.set_meta("negative", {"id": vid_neg, "status": st2,
                                    "binding": hd2.get("x-preview-voice-binding"),
                                    "engine": hd2.get("x-preview-engine")})
    finally:
        if proc is not None:
            H.stop_backend(proc)

    # ---- 用例 5: 清理自证（探针不该在库里和盘上留下痕迹） ----
    r5 = suite.new("清理自证: 探针行与产物删净，三个计数回到基线")
    ids = [m.get("id") for m in (suite.meta.get("positive"), suite.meta.get("negative"))
           if m and m.get("id")]
    removed = [delete_probe(v) for v in ids]
    after = counts()
    H.kv("删除产物", removed)
    H.kv("收尾计数", after)
    with conn() as c:
        left = c.execute("SELECT COUNT(*) FROM voice_profiles WHERE source='probe'").fetchone()[0]
    r5.check("探针行全部删除（含按 source='probe' 反查残留）",
             left == 0, f"source=probe 残留 {left} 行")
    r5.check("voices 总数回到基线", after["voices_total"] == base["voices_total"],
             f"{base['voices_total']} -> {after['voices_total']}")
    r5.check("openvoice 行数回到基线 0", after["voices_openvoice"] == 0,
             str(after["voices_openvoice"]))
    r5.check("sample 行数回到基线（反例行就是 sample，漏删只有这一格看得见）",
             after["voices_sample"] == base["voices_sample"],
             f"{base['voices_sample']} -> {after['voices_sample']}")
    r5.check("previews_generated 文件数回到基线（产物没留在盘上）",
             after["preview_files"] == base["preview_files"],
             f"{base['preview_files']} -> {after['preview_files']}")
    suite.set_meta("after", after)
    suite.set_meta("probe_ids", ids)
    suite.set_meta("removed_artifacts", removed)

    rc = suite.finish(exit_when_fail=False)
    H.kv("退出码", f"{rc}（0=无 FAIL/DEGRADED；harness 不据 rc 谎报，判定读上面清单）")
    return rc


if __name__ == "__main__":
    sys.exit(main())
