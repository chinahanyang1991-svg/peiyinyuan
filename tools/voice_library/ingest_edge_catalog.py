"""把微软 Edge/Azure 神经音色目录灌进本地音色库（含逐条可用性实测）。

为什么不需要"抓取第三方音色站"：`edge_tts.list_voices()` 直接返回微软自己公开的
音色目录（实测 322 枚 / 142 语言），而这正是本程序 edgetts 引擎**已经在调用**的服务；
第三方目录站（aitts.cc 之类）只是这份目录的转抄 + 试听页，去抓它既不增加信息量，
又要碰别人站点的抓取限制。所以合法且信息量最大的做法是回到权威源：
  1) 取目录 → 2) **每枚音色真合成一次**（拿到音频、解码、量时长/峰值）
  → 3) 只有实测能出声且过门的才入库 → 4) 行形状沿用既有 12 枚 Edge 行的契约。

入库行契约（与 `edgetts_adapter._restore_edge_voices` 对齐，勿改字段）：
  source='builtin'、engine='edgetts'、meta.edge_voice=ShortName、
  sample_audio_path=<实测音频>、reference_audio_path=NULL
—— reference 留空是必须的：`voxcpm._restore_voices` 按 source 注册克隆参考音频，
  给在线音色挂上参考会把 322 枚在线声混进本地克隆池。

用法:
  python -X utf8 tools/voice_library/ingest_edge_catalog.py                 # 只出计划
  python -X utf8 tools/voice_library/ingest_edge_catalog.py --synthesize    # 逐条实测合成
  python -X utf8 tools/voice_library/ingest_edge_catalog.py --apply        # 写库(三层可逆)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sqlite3
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

DB = REPO / "data" / "peiyinyuan.db"
BACKUP_ROOT = REPO / "data" / "backups"
VOICE_DIR = REPO / "data" / "voices" / "edge_catalog"
CATALOG = REPO.parent / "agent-ops" / "edge-tts-voices.json"
MANIFEST = REPO / "tools" / "voice_library" / "edge_catalog" / "edge_manifest.json"
NAMESPACE = uuid.UUID("6f1e2a44-7c3b-4d5e-9a1b-2c3d4e5f6a7b")
MIN_DUR_S, MIN_PEAK = 0.8, 0.02
PER_TRY_TIMEOUT_S = 40

# 各语言用一句本语言的话，避免拿中文句去测阿拉伯语音色造成假阴性
TEXTS = {
    "zh": "这是一次音色可用性检验，声音清晰自然。",
    "ja": "これはボイスの確認です。はっきりと聞こえますか。",
    "ko": "음성 확인을 위한 시험 문장입니다.",
    "en": "This is a voice availability check for the library.",
}
DEFAULT_TEXT = TEXTS["en"]

COLS = ['id', 'name', 'source', 'engine', 'language', 'gender', 'age_group',
        'style_tags', 'model_path', 'sample_audio_path', 'description',
        'reference_audio_path', 'is_downloaded', 'download_url', 'meta',
        'created_at', 'updated_at', 'category', 'seed']


def text_for(locale: str) -> str:
    for k, v in TEXTS.items():
        if locale.lower().startswith(k):
            return v
    return DEFAULT_TEXT


def load_catalog() -> list[dict]:
    raw = json.loads(CATALOG.read_text(encoding="utf-8"))
    out = []
    for v in raw:
        sn = v["ShortName"]
        out.append({
            "short_name": sn,
            "locale": v["Locale"],
            "gender": (v.get("Gender") or "").lower() or None,
            "content_categories": (v.get("VoiceTag") or {}).get("ContentCategories") or [],
            "personalities": (v.get("VoiceTag") or {}).get("VoicePersonalities") or [],
            "primary_locale": sn.rsplit("-", 1)[0] if sn.count("-") >= 2 else v["Locale"],
        })
    return out


def synthesize_one(voice: str, text: str, dst_wav: Path) -> dict:
    """真合成一次 → mp3 → 解码 → 落 48k/mono 之外的原生采样率 WAV，并量时长峰值。"""
    import edge_tts
    import soundfile as sf

    t0 = time.time()
    buf = bytearray()

    async def run():
        comm = edge_tts.Communicate(text=text, voice=voice)
        async for chunk in comm.stream():
            if chunk["type"] == "audio":
                buf.extend(chunk["data"])
    asyncio.run(asyncio.wait_for(run(), PER_TRY_TIMEOUT_S))
    if not buf:
        return {"ok": False, "reason": "NO_AUDIO_BYTES"}
    mp3 = dst_wav.with_suffix(".mp3")
    mp3.write_bytes(bytes(buf))
    try:
        data, sr = sf.read(str(mp3), always_2d=False)
    except Exception as e:
        return {"ok": False, "reason": f"DECODE_FAIL:{type(e).__name__}"}
    finally:
        mp3.unlink(missing_ok=True)
    if data.ndim > 1:
        data = data.mean(axis=1)
    sf.write(str(dst_wav), data, sr)
    dur = round(len(data) / sr, 3)
    peak = round(float(abs(data).max()), 4) if len(data) else 0.0
    return {"ok": True, "dur_s": dur, "peak": peak, "sample_rate": sr,
            "bytes": dst_wav.stat().st_size, "s": round(time.time() - t0, 2)}


def row_from_voice(v: dict, wav: Path, m: dict, now: str) -> dict:
    tags = [f"Edge-{v['locale']}"] + [p.strip() for p in v["personalities"] if p.strip()][:3]
    desc = "｜".join(filter(None, [
        f"微软神经音色 {v['short_name']}",
        f"性别:{v['gender'] or '未标'}",
        "场景:" + "/".join(c.strip() for c in v["content_categories"][:3]) if v["content_categories"] else "",
        "实测:" + (f"{m['dur_s']}s/峰值{m['peak']}" if m.get("ok") else "未合成")]))
    meta = {"edge_voice": v["short_name"], "online": True,
            "license": "Microsoft-Edge-TTS-online-service",
            "attribution": "Microsoft Azure Cognitive Services Speech 公开神经音色目录",
            "catalog_captured_at": datetime.now().strftime("%Y-%m-%d"),
            "speech_verify": m}
    return {
        "id": str(uuid.uuid5(NAMESPACE, "pyy-edge:" + v["short_name"].lower())),
        "name": f"Edge-{v['short_name']}", "source": "builtin", "engine": "edgetts",
        "language": v["locale"].split("-")[0].lower(), "gender": v["gender"],
        "age_group": None, "style_tags": json.dumps(tags, ensure_ascii=False),
        "model_path": None, "sample_audio_path": str(wav),
        "description": desc[:500], "reference_audio_path": None,
        "is_downloaded": 0, "download_url": None,
        "meta": json.dumps(meta, ensure_ascii=False),
        "created_at": now, "updated_at": now, "category": "human", "seed": None,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--synthesize", action="store_true", help="逐条真合成实测（写音频文件）")
    ap.add_argument("--apply", action="store_true", help="写库（三层可逆）")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 枚（调试用）")
    args = ap.parse_args()

    catalog = load_catalog()
    con = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)
    have = {str(json.loads(r[0] or "{}").get("edge_voice") or "").lower()
            for r in con.execute("SELECT meta FROM voice_profiles WHERE engine='edgetts'")}
    total_before = con.execute("SELECT count(*) FROM voice_profiles").fetchone()[0]
    con.close()
    todo = [v for v in catalog if v["short_name"].lower() not in have]
    if args.limit:
        todo = todo[:args.limit]
    print(f"目录 {len(catalog)} 枚 | 库内已有 Edge {len(have)} 枚 | 本次待处理 {len(todo)} 枚",
          flush=True)

    VOICE_DIR.mkdir(parents=True, exist_ok=True)
    import soundfile as _sf

    def measure(w: Path) -> dict:
        """从盘上实测（时长/峰值）——不信任"文件存在"本身。"""
        try:
            data, sr = _sf.read(str(w), always_2d=False)
            if data.ndim > 1:
                data = data.mean(axis=1)
            return {"ok": True, "dur_s": round(len(data) / sr, 3),
                    "peak": round(float(abs(data).max()), 4) if len(data) else 0.0,
                    "sample_rate": sr, "bytes": w.stat().st_size, "cached": True}
        except Exception as e:
            return {"ok": False, "reason": f"CACHED_DECODE_FAIL:{type(e).__name__}"}

    verified, rejected = [], []
    for i, v in enumerate(todo, 1):
        wav = VOICE_DIR / f"edge_{v['short_name']}.wav"
        if args.synthesize or not wav.exists():
            wav.unlink(missing_ok=True)
            try:
                m = synthesize_one(v["short_name"], text_for(v["locale"]), wav)
            except Exception as e:
                m = {"ok": False, "reason": f"{type(e).__name__}:{e}"}
        else:
            m = measure(wav)
        if m.get("ok") and m.get("dur_s", 0) >= MIN_DUR_S and m.get("peak", 0) >= MIN_PEAK:
            verified.append((v, wav, m))
        else:
            rejected.append({"short_name": v["short_name"], "why": m.get("reason") or
                             f"BELOW_GATE:dur={m.get('dur_s')},peak={m.get('peak')}"})
        if i % 20 == 0 or i == len(todo):
            print(f"  [{i}/{len(todo)}] 过门 {len(verified)} 拒 {len(rejected)}", flush=True)

    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST.write_text(json.dumps(
        [{"short_name": v["short_name"], "locale": v["locale"], "gender": v["gender"],
          "local_file": str(w), "verify": m} for v, w, m in verified],
        ensure_ascii=False, indent=1), encoding="utf-8")
    plan = {"catalog_total": len(catalog), "already_in_db": len(have),
            "processed": len(todo), "verified": len(verified), "rejected": len(rejected),
            "reject_reasons": rejected[:20], "manifest": str(MANIFEST),
            "library_rows_before": total_before, "library_rows_after": total_before + len(verified)}
    print(json.dumps(plan, ensure_ascii=False, indent=1))
    if not args.apply:
        print("\n[dry-run] 未写库。加 --apply 执行。")
        return 0

    now = datetime.now().isoformat(sep=" ", timespec="seconds")
    rows = [row_from_voice(v, w, m, now) for v, w, m in verified]
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    outdir = REPO.parent / "agent-ops" / f"edge-ingest-{ts}"
    outdir.mkdir(parents=True, exist_ok=True)
    backup = BACKUP_ROOT / f"peiyinyuan-preEdge-{ts}.db"
    src = sqlite3.connect(DB)
    dst = sqlite3.connect(backup)
    src.backup(dst)
    dst.close()

    def lit(x):
        if x is None:
            return "NULL"
        if isinstance(x, (int, float)):
            return str(x)
        return "'" + str(x).replace("'", "''") + "'"
    (outdir / "delete_inserted.sql").write_text(
        "\n".join(f"DELETE FROM voice_profiles WHERE id='{r['id']}';" for r in rows),
        encoding="utf-8")
    (outdir / "plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=1),
                                      encoding="utf-8")
    cur = src.cursor()
    ins = 0
    for r in rows:
        cur.execute(f"INSERT OR IGNORE INTO voice_profiles ({','.join(COLS)}) "
                    f"VALUES ({','.join('?' * len(COLS))})", [r.get(c) for c in COLS])
        ins += cur.rowcount
    src.commit()
    src.close()
    print(json.dumps({"backup": str(backup), "inserted_rows": ins,
                      "rollback_dir": str(outdir)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
