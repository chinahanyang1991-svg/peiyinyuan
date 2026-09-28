"""
[C4] 多人有声书 批次 B 后端服务实测: cast 任务编排 + cast/bgm/mix API + 真实音色池
运行: set PYTHONIOENCODING=utf-8 && python qa/harness/run_c4_cast_service.py

真实路径（qa/PROTOCOL.md 硬规则 1）:
  - 起真 uvicorn (backend.main:app)，端口自选空闲口，绝不复用/杀别人的进程
  - 用生产导入接口吃含对白 TXT (>=3 说话人) -> 真 dubbing 项目 -> POST analyze-cast
  - 任务走真 task 系统(ConversionTask) + 真进度通道 GET /api/tasks/{id}/progress 轮询到终态
  - 音色池 = DB 里真实的 VoiceProfile（VA-2 后 = category human/NULL 那一档，
    数百枚 source=downloaded 的采集音色，sample_audio_path 指向磁盘真 wav），
    并用 /api/voices/{id}/preview 实取音频字节
  - LLM 侧车不可用时走 spec 4.1 的规则兜底（设计内降级路径，stats 留痕不冒充 LLM）

口径（C4-CAST-PAGE 夹具修复，09-23）：旧夹具把 `GET /voices/` 的**单页**当全量池，
而 WF-12 之后该接口的 `total` 是 SQL COUNT(*)、limit 默认 100 —— 于是"推荐 id 全部来自
真实音色池"与"voice_total=全量"两条断言红在**翻页截断**上（09-22 14:42 那枚产物
13 PASS / 1 FAIL 就是这一格，被红的那 5 枚 id 直采 DB 全部在册）。现改为逐页取全 +
自证"不丢不重"，建议池集合另由**只读 SQL** 复算（对齐 `cast_service._voice_dicts`
的 category∈{human,NULL} 语义，不是拿全库 1039 枚比）。

口径（C4-CAST-BINDING 夹具修复，09-23 第二处）：01:19 那跑把 6 红压到 1 红，剩的
`voice_usable=570 vs 实测可放音 584` **红在夹具而不是产品**——旧探针只判"HTTP 200 且
字节 >=1000"，而那 14 枚（4 builtin + 9 designed + 1 枚 sample 路径写成裸文件名的
Kokoro-82M）盘上没有自己的主样本，试听端点按设计**借了一枚最接近的 Edge 官方嗓子**
回话（`voice_binding=proxy`、`engine=edgetts`、`requested_engine=voxcpm`）。
"有音频字节"证明的是"这个接口能放声"，不是"这枚音色自己会说话"——所以自有嗓子
一律按响应头 `X-Preview-Voice-Binding` 判，取值集合与前端徽标同源
（`frontend/src/services/api.ts` 的 `OWN_VOICE_BINDINGS`，本文件另设一条防漂断言）。

判定: 判定不过即 FAIL，不放宽断言。exit 0 = ALL_GREEN
产物: qa/round-2/artifacts/result_C4-cast-service.json
"""
import json
import os
import socket
import sys
import time
from pathlib import Path


# ---------------- 端口自选（先绑 0 拿空闲口，再起真 uvicorn） ----------------
def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


PORT = int(os.environ.get("PYY_TEST_PORT") or _free_port())
os.environ["PYY_TEST_PORT"] = str(PORT)

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness_lib as H  # noqa: E402

# 产物归到 round-2（结构对齐 result_P*）
ROUND = H.QA_DIR / "round-2"
(ROUND / "artifacts").mkdir(parents=True, exist_ok=True)
(ROUND / "logs").mkdir(parents=True, exist_ok=True)
H.ARTIFACTS = ROUND / "artifacts"
H.LOGS = ROUND / "logs"

FIXTURE = H.FIXTURES / "sample_cast_novel.txt"
EXPECT_GENDERS = {"周建国": "male", "林晚晴": "female", "小满": "female"}
ALIAS_FORMS = {"老周", "周队长"}
GENDERS = ("male", "female", "neutral", "unknown")
AGES = ("child", "young", "adult", "senior", "unknown")

# 试听响应头 X-Preview-Voice-Binding 里"听到的就是这枚音色自己的嗓子"的取值。
# 唯一真值在前端（徽标就是照它画的）：frontend/src/services/api.ts 的
# OWN_VOICE_BINDINGS；本文件下方有一条断言核对两处仍等价，漂了就红。
OWN_VOICE_BINDINGS = {"own_engine", "native"}
BINDING_HEADER = "x-preview-voice-binding"


def frontend_own_bindings():
    """从前端源码里读出 OWN_VOICE_BINDINGS 的字面量集合（防漂用，读不到返回 None）。"""
    import re
    src = (H.PROJECT_ROOT / "frontend" / "src" / "services" / "api.ts")
    try:
        text = src.read_text(encoding="utf-8")
    except OSError:
        return None
    m = re.search(r"OWN_VOICE_BINDINGS[^=]*=\s*new Set\(\[(.*?)\]\)", text, re.S)
    if not m:
        return None
    return {s.strip().strip("'\"") for s in m.group(1).split(",") if s.strip()}


MIX_PAYLOAD = {
    "bgm": {"ref": "bgm_warm_pad", "gain_db": -16.0, "ducking": True, "duck_db": 8.0},
    "sfx": {"chapter_marks": True, "items": []},
    "role_gains": {},
    "master": {"gain_db": -1.0, "target_lufs": -16.0},
}


# ============================================================ 辅助
def jdump(obj, limit=0):
    try:
        s = json.dumps(obj, ensure_ascii=False, default=str)
    except Exception:
        s = str(obj)
    return s if not limit else s[:limit]


def poll_progress(task_id, timeout=240, interval=0.4):
    """轮询真进度通道到终态；返回 {terminal, status, stages, samples, elapsed_s}。"""
    terminal = ("completed", "failed", "cancelled")
    stages, samples = [], []
    t0 = time.time()
    last_status = ""
    last = {}
    while time.time() - t0 < timeout:
        code, body = H.http("GET", f"/tasks/{task_id}/progress")
        if code == 200 and isinstance(body, dict):
            prog = body.get("progress") or {}
            st = str(prog.get("current_stage") or "")
            if st and (not stages or stages[-1] != st):
                stages.append(st)
            last_status = str(prog.get("status") or "")
            last = prog
            samples.append({"t": round(time.time() - t0, 2),
                            "progress": prog.get("progress"),
                            "stage": st, "status": last_status,
                            "processed_chars": prog.get("processed_chars"),
                            "total_chars": prog.get("total_chars")})
            if last_status in terminal:
                break
        time.sleep(interval)
    return {"terminal_status": last_status if last_status in terminal else "",
            "stages": stages, "samples": samples, "last": last,
            "final_code": code, "elapsed_s": round(time.time() - t0, 2)}


def role_attrs(role):
    extra = role.get("extra_data") or {}
    return extra.get("cast") or {}


def gender_rank(voice_gender, role_gender):
    vg = (voice_gender or "").lower()
    rg = (role_gender or "").lower()
    if vg and vg not in ("unknown", "neutral") and rg and rg not in ("unknown", "neutral"):
        if vg == rg:
            return "match"
        if vg in ("male", "female") and rg in ("male", "female"):
            return "conflict"
    return "na"


def top3_table(sug_roles, pool_by_id, play_bytes=None):
    """每角色 Top3 推荐（附真实音色性别/年龄/样本文件是否存在）——报告用。"""
    play_bytes = play_bytes or {}
    rows = []
    for r in sug_roles:
        top = []
        for item in (r.get("suggestions") or [])[:3]:
            v = pool_by_id.get(item.get("voice_id")) or {}
            # 与 App 试听逻辑同形: sample_audio_path 优先，其次音频后缀的 model_path
            cand_path = v.get("sample_audio_path") or ""
            mp = v.get("model_path") or ""
            if not cand_path and mp.lower().endswith((".wav", ".mp3", ".flac")):
                cand_path = mp
            sample = cand_path
            top.append({
                "voice_id": item.get("voice_id"),
                "voice_name": item.get("voice_name"),
                "score": item.get("score"),
                "breakdown": item.get("breakdown"),
                "voice_gender": item.get("gender"),
                "voice_age_group": item.get("age_group"),
                "source": item.get("source"),
                "gender_rank": gender_rank(item.get("gender"),
                                           (r.get("attrs") or {}).get("gender")),
                "sample_on_disk": bool(sample) and Path(sample).exists(),
                "sample_bytes": (Path(sample).stat().st_size
                                 if sample and Path(sample).exists() else 0),
                "preview_bytes": play_bytes.get(item.get("voice_id"), 0),
            })
        rows.append({"role_name": r.get("role_name"), "is_narrator": r.get("is_narrator"),
                     "role_gender": (r.get("attrs") or {}).get("gender"),
                     "role_age_group": (r.get("attrs") or {}).get("age_group"),
                     "top": top})
    return rows


def fetch_all_voices(page_size: int = 500):
    """翻页取**全量**音色池（C4-CAST-PAGE 夹具修复）。

    `GET /voices/` 的 limit 上限 500（默认 100），而 `total` 自 WF-12 起是 SQL COUNT(*)。
    旧夹具拿单页当全量池，于是：建议池成员比对只看前 100 行 -> 5 枚"未知 id"假红、
    `voice_total == len(pool)` 变成"583 vs 100"假红（红的是夹具，不是产品）。
    这里逐页取全，并断言翻页本身不丢不重。
    """
    pool, offset, total, pages = [], 0, None, 0
    while True:
        code, body = H.http("GET", f"/voices/?limit={page_size}&offset={offset}")
        if code != 200:
            raise RuntimeError(f"/voices/ 翻页失败 offset={offset}: {code} {jdump(body, 200)}")
        chunk = (body or {}).get("voices") or []
        total = (body or {}).get("total")
        pool.extend(chunk)
        pages += 1
        offset += len(chunk)
        if not chunk or total is None or offset >= int(total) or pages > 20:
            break
    ids = [v.get("id") for v in pool]
    return pool, total, {"pages": pages, "no_dup": len(ids) == len(set(ids)),
                         "covers_total": total is None or len(pool) == int(total)}


def make_wav(path, seconds=3.0, sr=22050, freq=220.0):
    """生成一段真实可播放的音频（正弦+包络），用于 BGM 上传入库实测。"""
    import numpy as np
    import soundfile as sf
    t = (np.arange(int(seconds * sr)) / sr).astype("float32")
    env = (0.6 + 0.4 * np.sin(2 * np.pi * 1.5 * t)).astype("float32")
    data = (0.25 * env * (np.sin(2 * np.pi * freq * t)
                          + 0.4 * np.sin(2 * np.pi * 2 * freq * t))).astype("float32")
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), data, sr)
    return Path(path)


# ============================================================ 主流程
def main():
    log = H.LOGS / "c4_cast_service.log"
    st = {"book_id": None, "project_id": None, "task_llm": None, "task_nollm": None,
          "cast": None, "suggestions": None, "pool": [], "pool_by_id": {},
          "confirmed": None, "uploaded_bgm": None}
    with H.Tee(log):
        su = H.Suite("C4-cast-service", "run_c4_cast_service.py")
        H.log(f"  [port] 自选空闲端口 = {PORT}")

        # ---------------- 0: 后端启动 + 路由注册 ----------------
        r = su.new("真 uvicorn 启动 + cast/bgm/mix 路由注册")
        r.evidence_add(log)
        be = H.start_backend(H.LOGS / "c4_backend.log", port=PORT, wait=120)
        try:
            code, body = H.http("GET", "/health", timeout=15)
            r.check("GET /api/health 200", code == 200, f"{code} {jdump(body, 160)}")
            r.evidence_add(H.LOGS / "c4_backend.log")
            code, spec = H.http("GET", f"{H.BASE}/openapi.json", timeout=30)
            paths = set((spec or {}).get("paths", {}).keys()) if code == 200 else set()
            need = ["/api/dubbing/projects/{project_id}/analyze-cast",
                    "/api/dubbing/projects/{project_id}/cast",
                    "/api/dubbing/projects/{project_id}/cast/suggestions",
                    "/api/dubbing/projects/{project_id}/cast/confirm",
                    "/api/dubbing/projects/{project_id}/cast/roles/{role_id}",
                    "/api/dubbing/projects/{project_id}/cast/preview",
                    "/api/dubbing/projects/{project_id}/mix-settings",
                    "/api/dubbing/projects/{project_id}/roles/{role_id}",
                    "/api/bgm", "/api/bgm/upload", "/api/bgm/{bgm_id}/preview"]
            missing = [p for p in need if p not in paths]
            r.check("cast/bgm/mix 端点全部注册", not missing,
                    f"缺失 {missing}" if missing else f"{len(need)} 条已在 openapi")
            methods = {p: set(((spec.get("paths", {}).get(p) or {}).keys()))
                       for p in need if p in paths}
            H.kv("cast GET 与 PUT 混挂检查",
                 jdump({k: sorted(v) for k, v in methods.items()}, 400))
            r.check("mix-settings 同时支持 GET/PUT",
                    {"get", "put"} <= (methods.get(
                        "/api/dubbing/projects/{project_id}/mix-settings") or set()),
                    str(sorted(methods.get("/api/dubbing/projects/{project_id}/mix-settings",
                                           set()))))

            # ---------------- 1: fixture 导入 + multi 项目 ----------------
            r = su.new("含对白 TXT 经生产导入接口落库 + multi 项目创建")
            r.evidence_add(log)
            content = FIXTURE.read_bytes()
            code, body = H.http("POST", "/books/import",
                               files={"file": ("c4_cast_novel.txt", content,
                                              "text/plain")},
                               data={"encoding": "utf-8"}, timeout=300)
            if code != 200:
                r.fail("书籍导入 200", f"{code} {jdump(body, 300)}")
            else:
                book = body["book"]
                st["book_id"] = book["id"]
                rows = H.db_rows("SELECT * FROM chapters WHERE book_id=? ORDER BY \"index\"",
                                 (book["id"],))
                H.kv("book_id", book["id"])
                H.kv("章节数", len(rows))
                H.kv("总字数", sum(len(c["content"] or "") for c in rows))
                full_text = "".join(c["content"] or "" for c in rows)
                r.check("chapters >= 2 (流式逐章输入)", len(rows) >= 2, f"{len(rows)} 章")
                r.check("正文含 >= 3 个具名说话人",
                        all(n in full_text for n in EXPECT_GENDERS),
                        str(sorted(EXPECT_GENDERS)))
                r.check("正文含别名形式",
                        any(a in full_text for a in ALIAS_FORMS), str(sorted(ALIAS_FORMS)))
                code2, proj = H.http("POST", "/dubbing/projects",
                                    json_body={"name": f"C4-cast-{int(time.time())}",
                                               "book_id": st["book_id"], "mode": "multi"})
                if code2 != 200:
                    r.fail("multi 项目创建 200", f"{code2} {jdump(proj, 300)}")
                else:
                    st["project_id"] = proj["project"]["id"]
                    r.check("项目 mode=multi",
                            proj["project"].get("mode") == "multi",
                            jdump(proj["project"], 200))
                    # POST /projects 返回 DubbingProject.to_dict()（不含 roles），走详情接口取角色
                    code3, detail = H.http("GET", f"/dubbing/projects/{st['project_id']}")
                    proles = (detail or {}).get("project", {}).get("roles") or []
                    r.check("项目自带唯一旁白",
                            sum(1 for x in proles if x.get("role_type") == "narrator") == 1,
                            f"详情接口 roles={jdump(proles, 240)}")

            # ---------------- 2: analyze-cast 任务创建（真 task 系统） ----------------
            r = su.new("POST analyze-cast -> ConversionTask 真任务行")
            r.evidence_add(log)
            if not st["project_id"]:
                r.block("跳过：无项目")
            else:
                t0 = time.time()
                code, body = H.http("POST", f"/dubbing/projects/{st['project_id']}/analyze-cast",
                                    json_body={"use_llm": True}, timeout=120)
                H.kv("analyze-cast", f"{code} in {time.time()-t0:.2f}s")
                if code != 200:
                    r.fail("analyze-cast 返回 200", f"{code} {jdump(body, 400)}")
                else:
                    task = body.get("task") or {}
                    st["task_llm"] = task.get("id")
                    H.kv("task", jdump(task, 300))
                    r.check("响应携带 task.id", bool(task.get("id")), str(task.get("id")))
                    rows = H.db_rows("SELECT * FROM conversion_tasks WHERE id=?",
                                     (task.get("id"),))
                    r.check("conversion_tasks 落库真行", len(rows) == 1,
                            f"{len(rows)} 行")
                    if rows:
                        s = json.loads(rows[0]["settings"] or "{}")
                        H.kv("settings", jdump(s, 240))
                        r.check("settings.task_type=cast_analysis",
                                s.get("task_type") == "cast_analysis", str(s.get("task_type")))
                        r.check("settings.project_id 回指项目",
                                s.get("project_id") == st["project_id"],
                                str(s.get("project_id")))
                        r.check("output_format=cast（不与转换任务混淆）",
                                rows[0]["output_format"] == "cast", rows[0]["output_format"])
                    code, got = H.http("GET", f"/tasks/{task.get('id')}")
                    r.check("GET /api/tasks/{id} 可见该任务",
                            code == 200 and (got.get("task") or {}).get("id") == task.get("id"),
                            f"{code}")
                    r.check("接口立即返回、不阻塞事件循环",
                            time.time() - t0 < 5.0, f"{time.time()-t0:.2f}s")

                # ---------------- 3: 轮询到终态 ----------------
                r2 = su.new("轮询 /api/tasks/{id}/progress 到终态 + 阶段轨迹")
                r2.evidence_add(log)
                poll = poll_progress(st["task_llm"])
                st["poll_llm"] = poll
                H.kv("终态", f"{poll['terminal_status']} ({poll['elapsed_s']}s)")
                H.kv("阶段轨迹", str(poll["stages"]))
                for s in poll["samples"][::max(1, len(poll["samples"]) // 8)]:
                    H.kv("  样本", jdump(s))
                r2.check("轮询通道 200", poll["final_code"] == 200, str(poll["final_code"]))
                r2.check("到达终态 completed", poll["terminal_status"] == "completed",
                         f"{poll['terminal_status'] or 'TIMEOUT'} last={jdump(poll['last'], 240)}")
                r2.check("进度单调推进且收敛到 1.0",
                         bool(poll["samples"]) and poll["last"].get("progress") == 1.0,
                         f"progress={poll['last'].get('progress')}")
                need_stages = ("cast_extract", "cast_profile", "cast_persist")
                seen = set(poll["stages"])
                r2.check("cast 三阶段全部被观测到", all(x in seen for x in need_stages),
                         f"seen={sorted(seen)}")
                db_task = H.db_rows("SELECT * FROM conversion_tasks WHERE id=?",
                                   (st["task_llm"],))
                if db_task:
                    H.kv("DB 任务行", jdump({k: db_task[0][k] for k in
                                             ("status", "progress", "current_stage",
                                              "processed_chars", "total_chars",
                                              "error_message")}, 300))
                    r2.check("DB 状态同步为 completed",
                             db_task[0]["status"] == "completed", str(db_task[0]["status"]))
                    r2.check("DB 无错误信息",
                             not (db_task[0]["error_message"] or ""),
                             str(db_task[0]["error_message"]))
                    r2.check("PY-121 对齐: DB total_chars 落库(>0)",
                             (db_task[0]["total_chars"] or 0) > 0,
                             f"total_chars={db_task[0]['total_chars']} "
                             f"processed_chars={db_task[0]['processed_chars']}")
                # tracker 已 pop，回落 DB 后仍要能算百分比
                code, after = H.http("GET", f"/tasks/{st['task_llm']}/progress")
                prog = (after or {}).get("progress") or {}
                H.kv("终态后再查进度", jdump(prog, 260))
                r2.check("终态后再查进度仍完整(processed/total 非零)",
                         code == 200 and prog.get("processed_chars", 0) > 0
                         and prog.get("total_chars", 0) > 0, jdump(prog, 240))

                # ---------------- 4: GET cast 角色表 ----------------
                r3 = su.new("GET cast: 角色>=3 + 唯一旁白 + 属性非全 unknown + 别名生效")
                r3.evidence_add(log)
                code, cast = H.http("GET", f"/dubbing/projects/{st['project_id']}/cast")
                if code != 200:
                    r3.fail("GET cast 200", f"{code} {jdump(cast, 300)}")
                else:
                    st["cast"] = cast
                    roles = cast.get("roles") or []
                    H.kv("role_count / character_count",
                         f"{cast.get('role_count')} / {cast.get('character_count')}")
                    H.kv("roles", jdump([(x.get("name"), x.get("role_type"),
                                          (role_attrs(x) or {}).get("gender"),
                                          (role_attrs(x) or {}).get("age_group"),
                                          (role_attrs(x) or {}).get("lines_count"))
                                         for x in roles], 700))
                    names = {x.get("name") for x in roles}
                    r3.check("含 3 个具名说话人",
                             set(EXPECT_GENDERS) <= names, str(sorted(names)))
                    r3.check("角色总数 >= 4 (3 具名 + 旁白)", len(roles) >= 4, f"{len(roles)}")
                    narr = [x for x in roles if (x.get("role_type") or "") == "narrator"]
                    r3.check("旁白唯一", len(narr) == 1 and narr[0]["name"] == "旁白",
                             jdump([(x["name"], x["role_type"]) for x in roles], 300))
                    r3.check("响应 narrator 字段与之一致",
                             cast.get("narrator") == "旁白", str(cast.get("narrator")))
                    attrs = [role_attrs(x) for x in roles]
                    genders = [a.get("gender") for a in attrs]
                    r3.check("属性非全 unknown(至少一个具名角色给出性别)",
                             any(g in ("male", "female") for g in genders), str(genders))
                    r3.check("gender/age_group 取值合法",
                             all(g in GENDERS for g in genders)
                             and all(a.get("age_group") in AGES for a in attrs),
                             jdump([(a.get("gender"), a.get("age_group")) for a in attrs]))
                    for name, want in EXPECT_GENDERS.items():
                        row = next((x for x in roles if x.get("name") == name), None)
                        r3.check(f"规则兜底性别正确 {name}={want}",
                                 row is not None and role_attrs(row).get("gender") == want,
                                 f"got={role_attrs(row).get('gender') if row else None}")
                    r3.check("样句/戏份/章节统计入库",
                             all((a.get("sample_quotes") and a.get("lines_count", 0) > 0)
                                 for a in attrs if a.get("profile_source") != "unknown"),
                             jdump([(a.get("lines_count"), len(a.get("sample_quotes") or []))
                                    for a in attrs]))
                    alias_tbl = (cast.get("cast") or {}).get("aliases") or {}
                    H.kv("别名表", jdump(alias_tbl, 400))
                    hit = {k: v for k, v in alias_tbl.items() if k in ALIAS_FORMS}
                    r3.check("别名归一表生效(老周/周队长 -> 周建国)",
                             hit.get("老周") == "周建国" and hit.get("周队长") == "周建国",
                             jdump(hit))
                    r3.check("别名形式不再独立成角色",
                             not (ALIAS_FORMS & names), str(sorted(ALIAS_FORMS & names)))
                    r3.check("cast 元信息含分析统计(llm 留痕)",
                             bool((cast.get("cast") or {}).get("stats")),
                             jdump((cast.get("cast") or {}).get("stats"), 400))
                    st["cast_meta"] = cast.get("cast") or {}
                    db_roles = H.db_rows("SELECT * FROM roles WHERE project_id=?",
                                         (st["project_id"],))
                    r3.check("Role 表行数 == cast.ROLE 数",
                             len(db_roles) == len(roles), f"DB={len(db_roles)} API={len(roles)}")

                # ---------------- 5: LLM 不可达时的 fallback 契约 ----------------
                r4 = su.new("LLM 不可达: use_llm=false 整表兜底且任务不失败")
                r4.evidence_add(log)
                code, body = H.http("POST", f"/dubbing/projects/{st['project_id']}/analyze-cast",
                                    json_body={"use_llm": False}, timeout=120)
                if code != 200:
                    r4.fail("analyze-cast(use_llm=false) 200", f"{code} {jdump(body, 300)}")
                else:
                    st["task_nollm"] = (body.get("task") or {}).get("id")
                    poll2 = poll_progress(st["task_nollm"])
                    st["poll_nollm"] = poll2
                    H.kv("fallback 任务终态",
                         f"{poll2['terminal_status']} in {poll2['elapsed_s']}s")
                    r4.check("兜底路径任务不失败",
                             poll2["terminal_status"] == "completed",
                             f"{poll2['terminal_status']} last={jdump(poll2['last'], 240)}")
                    code2, cast2 = H.http("GET", f"/dubbing/projects/{st['project_id']}/cast")
                    roles2 = (cast2 or {}).get("roles") or []
                    genders2 = [role_attrs(x).get("gender") for x in roles2]
                    r4.check("兜底仍给出 gender(无 one 为空/非法)",
                             all(g in GENDERS for g in genders2)
                             and sum(1 for g in genders2 if g in ("male", "female")) >= 3,
                             str(genders2))
                    srcs = {role_attrs(x).get("profile_source") for x in roles2}
                    H.kv("profile_source 集合", jdump(sorted(map(str, srcs))))
                    r4.check("画像来源标记为 fallback/builtin（不冒充 LLM）",
                             srcs <= {"fallback", "builtin_narrator"}, jdump(sorted(map(str, srcs))))
                    stats = ((cast2 or {}).get("cast") or {}).get("stats") or {}
                    H.kv("cast.stats", jdump(stats, 400))
                    # U-6/M-4 口径：stats.llm 必须是**标量**标签（前端直接比 "llm"），
                    # 画像明细挪到 llm_stats，兜底原因单独落 llm_reason 供界面显示。
                    r4.check("stats.llm 是标量标签且这里为 rules（不冒充 LLM）",
                             isinstance(stats.get("llm"), str) and stats.get("llm") == "rules",
                             jdump(stats.get("llm")))
                    r4.check("规则兜底必须带人话原因 llm_reason",
                             bool(stats.get("llm_reason")), jdump(stats.get("llm_reason")))
                    llm_stats = stats.get("llm_stats") or {}
                    r4.check("stats 留痕 llm_ready=False 且 llm_calls=0",
                            llm_stats.get("llm_ready") is False
                            and llm_stats.get("llm_calls") == 0,
                            jdump(llm_stats))
                    st["cast"] = cast2
                    st["cast_meta"] = cast2.get("cast")

                # ---------------- 6: suggestions（真实 25 downloaded 池） ----------------
                r5 = su.new("GET cast/suggestions: 建议池=human/NULL + 自有嗓子口径核对 + Top>=3")
                r5.evidence_add(log)
                pool, pool_total, pag = fetch_all_voices()
                assert pag["no_dup"] and pag["covers_total"], jdump(pag)
                downloaded = [v for v in pool if v.get("source") == "downloaded"]
                st["pool"] = pool
                st["pool_by_id"] = {v.get("id"): v for v in pool}
                # 列表接口不带路径（to_dict 语义），真实样本路径按只读 DB 补齐
                for row in H.db_rows("SELECT id, sample_audio_path, model_path FROM "
                                     "voice_profiles"):
                    v = st["pool_by_id"].get(row["id"])
                    if v is not None:
                        v["sample_audio_path"] = row["sample_audio_path"]
                        v["model_path"] = row["model_path"]
                H.kv("音色池", f"total={len(pool)} downloaded={len(downloaded)}")
                r5.check("池中含 >=25 真实 downloaded VoiceProfile",
                         len(downloaded) >= 25, f"{len(downloaded)}")
                # 后端建议池的定义 = cast_service._voice_dicts: category 为 human 或 NULL
                # （VA-2 之后音效类不进推荐），故这里以只读 SQL 独立复算该集合，
                # 而不是拿"全库列表"当池子（那是 1039 枚，含动物/电子/机械）。
                human_ids = {r["id"] for r in H.db_rows(
                    "SELECT id FROM voice_profiles WHERE category='human' OR category IS NULL")}
                H.kv("建议池(只读 SQL 复算)", f"human/NULL={len(human_ids)} 全库={len(pool)}")
                code, sug = H.http("GET",
                                   f"/dubbing/projects/{st['project_id']}/cast/suggestions"
                                   f"?top_n=5")
                if code != 200:
                    r5.fail("GET cast/suggestions 200", f"{code} {jdump(sug, 400)}")
                else:
                    st["suggestions"] = sug
                    sroles = sug.get("roles") or []
                    H.kv("voice_total(后端侧)", sug.get("voice_total"))
                    # "可用"的客观定义 = App 自己能把这枚音色的**本人嗓子**放出来：
                    # 逐条真调试听接口，再按 X-Preview-Voice-Binding 分自有/借声
                    # （字节数只能证明"接口能放声"，见文件头 C4-CAST-BINDING 口径）
                    own_ids, borrowed, play_fail, play_bytes = set(), {}, [], {}
                    origin_hist = {}
                    for v in pool:
                        vid = v.get("id")
                        if vid not in human_ids:
                            continue
                        c_p, hd, b_p = H.http_with_headers(
                            "GET", f"/voices/{vid}/preview", timeout=30)
                        n = len(b_p) if isinstance(b_p, (bytes, bytearray)) else 0
                        play_bytes[vid] = n
                        binding = (hd.get(BINDING_HEADER) or "").strip()
                        origin = (hd.get("x-preview-origin") or "").strip() or "?"
                        origin_hist[origin] = origin_hist.get(origin, 0) + 1
                        if c_p != 200 or n < 1000:
                            play_fail.append({"name": v.get("name"),
                                              "source": v.get("source"), "code": c_p,
                                              "bytes": n, "origin": origin,
                                              "binding": binding})
                        elif binding and binding not in OWN_VOICE_BINDINGS:
                            borrowed[vid] = {"name": v.get("name"), "binding": binding,
                                             "origin": origin,
                                             "served_by": hd.get("x-preview-voice"),
                                             "served_engine": hd.get("x-preview-engine"),
                                             "row_engine": v.get("engine")}
                        else:
                            own_ids.add(vid)
                    playable_ids = own_ids      # 下游"可放音"一律指自有嗓子
                    not_own = sorted(human_ids - own_ids)
                    # 夹具侧独立复算（口径与 cast_service.voice_is_usable 同源但各自实现，
                    # 防"两条判据其实是同一把尺子"）：盘上有 >1KB 音频主样本
                    ondisk_ids = set()
                    for row in H.db_rows("SELECT id, sample_audio_path, model_path "
                                         "FROM voice_profiles "
                                         "WHERE category='human' OR category IS NULL"):
                        for key in ("sample_audio_path", "model_path"):
                            p = str(row[key] or "").strip()
                            if (p and os.path.splitext(p)[1].lower()
                                    in (".wav", ".mp3", ".flac", ".m4a", ".ogg", ".aiff", ".aac")
                                    and os.path.isfile(p) and os.path.getsize(p) > 1024):
                                ondisk_ids.add(row["id"])
                                break
                    H.kv("建议池可用性(按 /voices/{id}/preview 实测)",
                         f"池={len(human_ids)} 自有嗓子={len(own_ids)} "
                         f"借声可听={len(borrowed)} 放不出={len(play_fail)}")
                    H.kv("试听出口(origin)分布", jdump(origin_hist))
                    H.kv("盘上有主样本(夹具侧独立复算)", f"{len(ondisk_ids)} 枚")
                    # PY-330: 自动绑定池 = 自有嗓子 - 回放档(engine='sample')。
                    # 回放档能试听出自己的嗓子（所以它在 own_ids/ondisk_ids 里），
                    # 但 SamplePlaybackEngine 不吃台词，绑上=该角色全书同一句。
                    row_engine = {v.get("id"): str(v.get("engine") or "").lower()
                                  for v in pool}
                    replay_ids = {i for i in ondisk_ids if row_engine.get(i) == "sample"}
                    bindable_expect = ondisk_ids - replay_ids
                    replay_names = sorted(str(v.get("name") or "") for v in pool
                                          if v.get("id") in replay_ids)
                    H.kv("自动绑定池(自有嗓子剔回放档, 夹具侧复算)",
                         f"{len(bindable_expect)} 枚（回放档 {len(replay_ids)} 枚: "
                         f"{replay_names[:6]}）")
                    H.kv("借声清单(听到的不是这枚音色自己的嗓子)",
                         jdump([{"name": b["name"], "binding": b["binding"],
                                 "served_by": b["served_by"], "row_engine": b["row_engine"]}
                                for b in list(borrowed.values())[:20]], 900))
                    H.kv("放不出清单", jdump(play_fail, 500))
                    ages = [v.get("age_group") for v in pool if v.get("source") == "downloaded"]
                    H.kv("downloaded 池年龄标注覆盖",
                         f"adult={ages.count('adult')} young={ages.count('young')} "
                         f"child={ages.count('child')} senior={ages.count('senior')} "
                         f"未标注={sum(1 for a in ages if not a)}")
                    H.kv("池语义字段", jdump({k: sug.get(k) for k in
                                            ("voice_total", "voice_usable", "pool_mode",
                                             "voice_total_pool_used", "top_n")}))
                    r5.check("后端建议池 = 只读 SQL 复算的 human/NULL 集合（未做语言过滤、也不含音效类）",
                             sug.get("voice_total") == len(human_ids),
                             f"voice_total={sug.get('voice_total')} vs SQL {len(human_ids)}"
                             f" / 全库列表 {len(pool)}")
                    r5.check("夹具翻页自证: /voices/ 翻页取全（total 与实际取回条数一致）",
                             pag["no_dup"] and pag["covers_total"]
                             and pool_total is not None and len(pool) == int(pool_total),
                             f"pages={pag['pages']} len(pool)={len(pool)} vs total={pool_total}")
                    r5.check("C4-2: 后端 voice_usable = 试听实测的自有嗓子条数",
                             sug.get("voice_usable") == len(own_ids),
                             f"voice_usable={sug.get('voice_usable')} vs 自有嗓子 "
                             f"{len(own_ids)}（另有 {len(borrowed)} 枚只能借声听，不计入）")
                    r5.check("C4-2: 自有嗓子集合与盘上主样本集合逐条相等（不只比计数）",
                             own_ids == ondisk_ids,
                             f"仅实测自有 {sorted(own_ids - ondisk_ids)[:3]} / "
                             f"仅盘上有样本 {sorted(ondisk_ids - own_ids)[:3]}")
                    unnamed = sorted(f"{b['name']}({b['binding']})"
                                     for b in borrowed.values()
                                     if b["binding"] == "proxy" and not b["served_by"])
                    r5.check("借声必须点名实际听到的嗓子（否则前端徽标无处可指）",
                             not unnamed, jdump(unnamed[:10], 300))
                    fe = frontend_own_bindings()
                    r5.check("前后端『自有嗓子』口径未漂移(= api.ts 的 OWN_VOICE_BINDINGS)",
                             fe == OWN_VOICE_BINDINGS, f"前端读到 {fe} / 夹具用 "
                             f"{OWN_VOICE_BINDINGS}")
                    r5.check("C4-2: 可用池 >=25 且 pool_mode=usable_only",
                             (sug.get("voice_bindable") or 0) >= 25
                             and sug.get("pool_mode") == "usable_only"
                             and sug.get("voice_total_pool_used") == sug.get("voice_bindable"),
                             jdump({k: sug.get(k) for k in
                                    ("voice_usable", "voice_bindable", "pool_mode",
                                     "voice_total_pool_used")}))
                    r5.check("PY-330: 后端 voice_bindable = 自有嗓子减回放档(夹具侧复算)",
                             sug.get("voice_bindable") == len(bindable_expect),
                             f"voice_bindable={sug.get('voice_bindable')} vs 复算 "
                             f"{len(bindable_expect)}（自有 {len(own_ids)} - 回放档 "
                             f"{len(replay_ids)}）")
                    if not replay_ids:
                        H.kv("PY-330 判别力自曝", "池内已无回放档音色 -> bindable==usable，"
                                                  "本断言退化为恒等式，不再能证明门生效")
                    r5.check("每个角色都有建议条目",
                            len(sroles) == len(st["cast"]["roles"]),
                            f"roles={len(sroles)} cast={len(st['cast']['roles'])}")
                    lens = {x["role_name"]: len(x.get("suggestions") or []) for x in sroles}
                    H.kv("每角色建议条数", jdump(lens))
                    r5.check("每角色 Top >= 3", all(v >= 3 for v in lens.values()),
                             jdump(lens))
                    r5.check("Top-N 不超上限", all(v <= 5 for v in lens.values()), jdump(lens))
                    # 打分必须是降序且带 breakdown
                    for x in sroles:
                        sc = [i.get("score") for i in (x.get("suggestions") or [])]
                        if sc != sorted(sc, reverse=True):
                            r5.fail("推荐按分数降序", f"{x['role_name']} {sc}")
                            break
                    else:
                        r5.check("推荐按分数降序", True, "OK")
                    bad_first, conflict_ahead = [], []
                    for x in sroles:
                        rg = (x.get("attrs") or {}).get("gender")
                        if rg not in ("male", "female"):
                            continue
                        ranked = x.get("suggestions") or []
                        ranks = [gender_rank(i.get("gender"), rg) for i in ranked]
                        if ranks and ranks[0] == "conflict":
                            bad_first.append((x["role_name"], rg,
                                              [(i.get("voice_name"), i.get("gender"))
                                               for i in ranked[:3]]))
                        first_conflict = next((k for k, v in enumerate(ranks) if v == "conflict"),
                                              None)
                        last_match = max([k for k, v in enumerate(ranks) if v == "match"],
                                         default=None)
                        if first_conflict is not None and last_match is not None \
                                and first_conflict < last_match:
                            conflict_ahead.append((x["role_name"], ranks))
                        n_match = ranks.count("match")
                        if n_match and ranks[0] != "match":
                            conflict_ahead.append((x["role_name"], "top1 非性别匹配项", ranks))
                    r5.check("已知性别角色首位推荐不冲突", not bad_first, jdump(bad_first))
                    r5.check("性别匹配项排在冲突项之前", not conflict_ahead,
                             jdump(conflict_ahead))
                    nar = next((x for x in sroles if x.get("is_narrator")), None)
                    if nar:
                        top = (nar.get("suggestions") or [])[:5]
                        H.kv("旁白 Top5", jdump([(t.get("voice_name"), t.get("gender"),
                                                 t.get("age_group"), t.get("score"),
                                                 t.get("breakdown")) for t in top], 700))
                        # spec 4.1: 旁白固定画像(沉稳/磁性/浑厚/庄重 + adult)，打分天然把
                        # 旁白推向沉稳成年音。可判定形式 = 采纳位(Top1)成年/未标注 +
                        # Top5 无童声 + Top5 内成年/未标注 >= 2。
                        # 说明: 早期版本要求"Top5 全为成年/未标注"，与真实音库年龄标注
                        # 覆盖率(adult 仅个位数)冲突，属断言过度约束，非放宽——三条子判
                        # 定都比原式更严：Top1 错判、出现童声、成年占比过低任一即 FAIL。
                        # W-3(verify-round3 §6): ""/None/"unknown" 三档宽容是池内
                        # 年龄标注覆盖率决定的（见上注释），但只有"该音色在 DB 里
                        # 本来就未标注/unknown"才算数——推荐结果与池标注不一致(结果
                        # 编造 adult)即红；并加一条 Top1=池内真实可放音音色。
                        pool_age = ((st["pool_by_id"].get(top[0].get("voice_id")) or {})
                                    .get("age_group")) if top else "?"
                        a1 = (top[0].get("age_group") or "") if top else ""
                        r5.check("旁白采纳位(Top1)为成年/未标注(沉稳向)",
                                 bool(top) and a1 in ("adult", "senior", "", None, "unknown")
                                 and (a1 not in ("", None, "unknown")
                                      or pool_age in ("", None, "unknown")),
                                 f"Top1 age_group={a1!r} pool标注={pool_age!r} "
                                 f"name={top[0].get('voice_name') if top else None}")
                        r5.check("W-3: 旁白 Top1 是池内真实且可放音的音色",
                                 bool(top) and top[0].get("voice_id") in st["pool_by_id"]
                                 and top[0].get("voice_id") in playable_ids,
                                 "Top1=" + jdump([(t.get("voice_id"), t.get("voice_name"))
                                                   for t in top[:1]], 160))
                        r5.check("旁白 Top5 绝不出现童声(child)",
                                 all((t.get("age_group") or "") != "child" for t in top),
                                 jdump([t.get("age_group") for t in top]))
                        n_adult = sum(1 for t in top
                                      if (t.get("age_group") or "") in
                                      ("adult", "senior", "", None, "unknown"))
                        r5.check("旁白 Top5 内成年/未标注占比 >= 2/5", n_adult >= 2,
                                 f"{n_adult}/5 {jdump([t.get('age_group') for t in top])}")
                    ids = {i.get("voice_id") for x in sroles for i in (x.get("suggestions") or [])}
                    r5.check("推荐 id 全部来自真实音色池", ids <= set(st["pool_by_id"]),
                             f"未知 id {sorted(ids - set(st['pool_by_id']))[:5]}")
                    r5.check("C4-2: 推荐全部来自自有嗓子音色（非自有 0 条）",
                             ids <= playable_ids,
                             f"越界 {sorted(ids - playable_ids)[:5]} / 非自有嗓子 "
                             f"{len(not_own)} 条")
                    flag_bad = [(i.get("voice_id"), i.get("usable"),
                                 i.get("voice_id") in playable_ids)
                                for x in sroles for i in (x.get("suggestions") or [])
                                if bool(i.get("usable")) != (i.get("voice_id") in playable_ids)]
                    r5.check("每条推荐的 usable 标记与试听实测逐条吻合", not flag_bad,
                             jdump(flag_bad[:5], 400))
                    replay_in_rec = [(x.get("role_name"), i.get("voice_id"))
                                     for x in sroles for i in (x.get("suggestions") or [])
                                     if i.get("voice_id") in replay_ids]
                    r5.check("PY-330: 推荐 Top-N 里 0 条回放档音色（绑上=该角色全书同一句）",
                             not replay_in_rec, jdump(replay_in_rec[:6], 400))
                    rp_bad = [(x.get("role_name"), i.get("voice_id"), i.get("replay_only"))
                              for x in sroles for i in (x.get("suggestions") or [])
                              if bool(i.get("replay_only"))
                              != (i.get("voice_id") in replay_ids)]
                    r5.check("PY-330: 每条推荐的 replay_only 标记与夹具侧复算逐条吻合",
                             not rp_bad, jdump(rp_bad[:5], 400))
                    # 自动绑定是**另一条代码路径**（`_analyze_sync` 用
                    # `_voice_dicts(require_sample=True)`，与建议池各走各的门），
                    # 只盯推荐会漏掉它。但本轮变异检验（把池退回只看 usable）下
                    # 本断言**仍然绿**：mini fixture 只有 4 行自动绑定，没一行排到
                    # 回放档，所以它在 C4 里只是"真书/大池子场景的可见性"，判别力
                    # 由 tests/test_cast_voice_pool.py 承担（同形变异会红 3 条）。
                    auto_rows = [x for x in (st["cast"]["roles"] or [])
                                 if x.get("voice_id")
                                 and ((x.get("extra_data") or {}).get("voice_source")
                                      in (None, "suggested"))]
                    auto_replay = [(x.get("name"),
                                    (st["pool_by_id"].get(x["voice_id"]) or {}).get("engine"))
                                   for x in auto_rows
                                   if (st["pool_by_id"].get(x["voice_id"]) or {}).get("engine")
                                   == "sample"]
                    r5.check("PY-330: 本轮自动绑定落库的角色 0 条是回放档（另一条池路径）",
                             not auto_replay, jdump(auto_replay[:6], 400))
                    if not auto_rows:
                        H.kv("PY-330 自动绑定断言判别力自曝",
                             "落库角色无一携带 voice_id -> 本断言恒真，不能证明门生效")
                    else:
                        H.kv("PY-330 自动绑定检查覆盖",
                             f"{len(auto_rows)} 行带 voice_id 且可比引擎档")
                    tbl = top3_table(sroles, st["pool_by_id"], play_bytes)
                    st["top3"] = tbl
                    H.log("")
                    H.log("  ==== 真实音色池每角色 Top3 推荐（人声样本可应用性证据）====")
                    for row in tbl:
                        H.kv(row["role_name"],
                             f"角色性别={row['role_gender']} 年龄段={row['role_age_group']}")
                        for i, item in enumerate(row["top"], 1):
                            H.log(f"      Top{i}: {item['voice_name']} | "
                                  f"score={item['score']} {jdump(item['breakdown'])} | "
                                  f"音色性别={item['voice_gender']} "
                                  f"年龄段={item['voice_age_group']} "
                                  f"匹配={item['gender_rank']} "
                                  f"样本在盘={item['sample_on_disk']} "
                                  f"({item['sample_bytes']}B) "
                                  f"试听实取={item['preview_bytes']}B src={item['source']}")
                    H.log("")

                # ---------------- 7: 真实样本可用性（红线：人声可用） ----------------
                r6 = su.new("红线-人声可用: 每角色 Top1 推荐音色的真实样本可取回")
                r6.evidence_add(log)
                if not st.get("suggestions"):
                    r6.block("跳过：无建议数据")
                else:
                    ok_all = True
                    for x in st["suggestions"].get("roles") or []:
                        top = (x.get("suggestions") or [])[:1]
                        if not top:
                            r6.fail("每角色至少 1 条推荐", x.get("role_name"))
                            ok_all = False
                            continue
                        vid = top[0].get("voice_id")
                        code2, body2 = H.http("GET", f"/voices/{vid}/preview", timeout=60,
                                             raw=True)
                        size = len(body2) if isinstance(body2, (bytes, bytearray)) else 0
                        H.kv(f"{x['role_name']} <- {top[0].get('voice_name')}",
                             f"HTTP {code2} {size}B")
                        if code2 != 200 or size < 1000:
                            ok_all = False
                            r6.fail(f"角色 {x['role_name']} 的 Top1 音色样本可试听",
                                    f"voice={vid} name={top[0].get('voice_name')} "
                                    f"http={code2} bytes={size}")
                    if ok_all:
                        r6.check("全部角色 Top1 音色均返回真音频(>=1KB)", True,
                                 f"{len(st['suggestions']['roles'])} 角色")
                    dl_with_sample = sum(1 for v in st["pool"]
                                         if v.get("source") == "downloaded" and v.get("has_sample"))
                    r6.check("downloaded 池全部带样本标记", dl_with_sample >= 25,
                             f"{dl_with_sample}/25")

                # ---------------- 8: PUT cast/confirm -> Role 落库 ----------------
                r7 = su.new("PUT cast/confirm -> Role 表落库(行数/voice_id/别名/confirmed)")
                r7.evidence_add(log)
                if not st["project_id"]:
                    r7.block("跳过：无项目")
                else:
                    roles = (st["cast"] or {}).get("roles") or []
                    by_name_sug = {x["role_name"]: (x.get("suggestions") or [])
                                   for x in (st["suggestions"] or {}).get("roles") or []}
                    chosen = {}
                    for role in roles:
                        ranked = by_name_sug.get(role["name"]) or []
                        picked = ranked[0]["voice_id"] if ranked else None
                        if picked:
                            chosen[role["name"]] = picked
                    payload = {"roles": [
                        {"role_id": role["id"], "name": role["name"],
                         "role_type": role.get("role_type"),
                         "voice_id": chosen.get(role["name"]),
                         "aliases": (role.get("extra_data") or {}).get("aliases") or [],
                         "mix": {"gain_db": -2.0, "speed": 1.05, "pitch": 0.0}}
                        for role in roles],
                        "auto_assign_rest": True}
                    H.kv("confirm 载荷角色数", len(payload["roles"]))
                    code, conf = H.http("PUT", f"/dubbing/projects/{st['project_id']}/cast/confirm",
                                        json_body=payload, timeout=120)
                    if code != 200:
                        r7.fail("confirm 200", f"{code} {jdump(conf, 500)}")
                    else:
                        st["confirmed"] = conf
                        H.kv("confirm 结果", jdump({k: v for k, v in (conf or {}).items()
                                                  if k != "roles"}, 400))
                        r7.check("响应 roles 行数 == 提交行数",
                                 len(conf.get("roles") or []) == len(roles),
                                 f"{len(conf.get('roles') or [])} vs {len(roles)}")
                        db_roles = H.db_rows("SELECT * FROM roles WHERE project_id=?",
                                             (st["project_id"],))
                        r7.check("Role 表落库行数断言",
                                 len(db_roles) == len(roles),
                                 f"DB={len(db_roles)} expected={len(roles)}")
                        r7.check("旁白在 Role 表仍唯一",
                                 sum(1 for x in db_roles if x["role_type"] == "narrator") == 1,
                                 jdump([(x["name"], x["role_type"]) for x in db_roles]))
                        got_voices = {r["name"]: r["voice_id"] for r in db_roles}
                        H.kv("DB voice_id 落库", jdump(got_voices, 400))
                        mism = {k: (v, got_voices.get(k)) for k, v in chosen.items()
                                if got_voices.get(k) != v}
                        r7.check("确认的 voice_id 与提交一致", not mism, jdump(mism))
                        r7.check("外键真实：全部 voice_id 存在于 voice_profiles",
                                 all(got_voices.values()),
                                 jdump(got_voices))
                        r7.check("别名表写进 project.extra_data.cast",
                                 bool((conf.get("aliases") or {})),
                                 f"aliases={len(conf.get('aliases') or {})} 条")
                        r7.check("speaker_map 固化(name -> role_id/voice_id)",
                                 all(v.get("role_id") for v in
                                     (conf.get("cast") or {}).get("speaker_map", {}).values()),
                                 jdump((conf.get("cast") or {}).get("speaker_map"), 300))
                        proj = H.http("GET", f"/dubbing/projects/{st['project_id']}")
                        r7.check("确认后项目 status 迁移",
                                code == 200 and (proj[1].get("project") or {}).get("status")
                                in ("ready", "converting", "done"),
                                jdump(proj[1], 240))
                        extra_rows = {r["name"]: json.loads(r["extra_data"] or "{}")
                                      for r in db_roles}
                        r7.check("Role.extra_data 记录 confirmed/局部 mix",
                                all(v.get("confirmed") for v in extra_rows.values())
                                and all((v.get("mix") or {}).get("gain_db") == -2.0
                                        for v in extra_rows.values()),
                                jdump({k: (v.get("confirmed"), v.get("mix"))
                                       for k, v in extra_rows.items()}, 400))
                        narrator = next((x for x in db_roles if x["role_type"] == "narrator"), None)
                        if narrator:
                            code2, body2 = H.http("DELETE",
                                                  f"/dubbing/projects/{st['project_id']}"
                                                  f"/roles/{narrator['id']}")
                            r7.check("旁白不可删除(400)", code2 == 400, f"{code2} {jdump(body2, 200)}")
                        # 手工修正属性（unknown -> 用户补全）
                        hero = next((x for x in db_roles if x["role_type"] != "narrator"), None)
                        if hero:
                            code3, body3 = H.http(
                                "PUT", f"/dubbing/projects/{st['project_id']}/cast/roles/"
                                       f"{hero['id']}",
                                json_body={"age_group": "senior", "identity": "掌柜",
                                           "aliases": ["老朝奉"]})
                            role_out = (body3 or {}).get("role") or {}
                            r7.check("PUT cast/roles/{id} 手工修正属性",
                                     code3 == 200
                                     and (role_out.get("cast") or {}).get("age_group") == "senior"
                                     and (role_out.get("cast") or {}).get("identity") == "掌柜",
                                     f"{code3} {jdump(role_out, 300)}")
                            # W-5(verify-round3 §6): PUT 返回体自说自话不算闭环——
                            # 再 GET 一次 cast，断言手工别名真的写进 Role 行。
                            st["manual_alias_role"] = hero["name"]
                            code4b, cast4 = H.http(
                                "GET", f"/dubbing/projects/{st['project_id']}/cast")
                            row4 = next((x for x in ((cast4 or {}).get("roles") or [])
                                         if x.get("id") == hero["id"]), {})
                            r7.check("W-5: GET 回读 手工别名'老朝奉'已进 Role.aliases",
                                     code4b == 200 and "老朝奉" in (row4.get("aliases") or []),
                                     jdump(row4.get("aliases")))

                # ---------------- 9: mix-settings 往返 ----------------
                r8 = su.new("PUT/GET mix-settings 往返一致 + 非法配置 400")
                r8.evidence_add(log)
                roles = (st["cast"] or {}).get("roles") or []
                rid = {x["name"]: x["id"] for x in roles}
                payload = json.loads(json.dumps(MIX_PAYLOAD))
                payload["role_gains"] = {rid.get("周建国", "x"): {"gain_db": 1.5, "speed": 0.95,
                                                                 "pitch": -0.5}}
                code, put = H.http("PUT", f"/dubbing/projects/{st['project_id']}/mix-settings",
                                   json_body={"mix": payload}, timeout=60)
                if code != 200:
                    r8.fail("PUT mix-settings 200", f"{code} {jdump(put, 400)}")
                else:
                    H.kv("PUT 响应", jdump(put, 300))
                    code, got = H.http("GET", f"/dubbing/projects/{st['project_id']}/mix-settings")
                    H.kv("GET 响应", jdump(got, 500))
                    stored = (got or {}).get("mix") or {}
                    r8.check("往返一致(mix 全等)", stored == payload,
                             "diff: " + jdump({"sent": payload, "got": stored}, 700))
                    r8.check("passthrough 标记 False(有 BGM 配置)",
                            got.get("passthrough") is False, str(got.get("passthrough")))
                    eff = (got or {}).get("chapter_effective") or {}
                    H.kv("chapter_effective", jdump(eff, 400))
                    r8.check("chapter_effective 解析出 bgm/角色增益",
                            bool(eff.get("bgm")) or bool(eff.get("role_gains")),
                            jdump(list(eff.keys())))
                # 非法配置 1: 非数字增益（会让 is_passthrough/float() 在读取时炸）
                poison = {"master": {"gain_db": "很响"}, "role_gains": {"r1": {"speed": "快"}}}
                code, bad = H.http("PUT", f"/dubbing/projects/{st['project_id']}/mix-settings",
                                   json_body={"mix": poison}, timeout=60)
                r8.check("非法 mix 结构(非数字增益) -> 400 + 逐条原因", code == 400,
                         f"{code} {jdump(bad, 300)}")
                code, got3 = H.http("GET", f"/dubbing/projects/{st['project_id']}/mix-settings")
                r8.check("脏配置不得让 GET 500（读侧守门）", code in (200, 404),
                         f"{code} {jdump(got3, 260)}")
                # 非法配置 2: bgm.ref 非字符串 / target_lufs 越界
                code, bad2 = H.http(
                    "PUT", f"/dubbing/projects/{st['project_id']}/mix-settings",
                    json_body={"mix": {"bgm": {"ref": 123},
                                       "master": {"target_lufs": 3.5}}}, timeout=60)
                r8.check("bgm.ref 非字符串 / LUFS 越界 -> 400", code == 400,
                         f"{code} {jdump(bad2, 300)}")
                code, clear = H.http("PUT", f"/dubbing/projects/{st['project_id']}/mix-settings",
                                     json_body={"mix": None}, timeout=60)
                code2, got2 = H.http("GET", f"/dubbing/projects/{st['project_id']}/mix-settings")
                r8.check("mix=null 清空 -> passthrough True(回归保护)",
                         code == 200 and (got2 or {}).get("passthrough") is True
                         and not ((got2 or {}).get("mix") or {}),
                         f"{code}/{code2} {jdump(got2, 260)}")

                # ---------------- 10: BGM 列表/上传/试听 ----------------
                r9 = su.new("GET /api/bgm 占位素材 + 上传入库 + 试听")
                r9.evidence_add(log)
                code, bgm = H.http("GET", "/bgm", timeout=120)
                if code != 200:
                    r9.fail("GET /api/bgm 200", f"{code} {jdump(bgm, 300)}")
                else:
                    rows = bgm.get("bgm") or []
                    sfx = bgm.get("sfx") or []
                    H.kv("bgm/sfx 条数", f"{len(rows)} / {len(sfx)}")
                    H.kv("bgm 首条", jdump(rows[0] if rows else {}, 300))
                    H.kv("bgm_dir", bgm.get("bgm_dir"))
                    r9.check("列表含 >=3 段内置占位素材", len(rows) >= 3, f"{len(rows)}")
                    r9.check("占位素材标记 placeholder + source=builtin_numpy",
                             all(x.get("placeholder") for x in rows),
                             jdump([(x.get("id"), x.get("placeholder")) for x in rows], 300))
                    r9.check("素材时长/响度元数据齐备",
                             all({"duration_s", "peak_db", "loudness_db", "sample_rate",
                                  "channels", "file", "moods"} <= set(x)
                                 for x in rows),
                             jdump(sorted(rows[0].keys()) if rows else []))
                    files = [Path(bgm.get("bgm_dir") or ".") / x["file"] for x in rows]
                    r9.check("占位 wav 真实落盘", all(f.exists() and f.stat().st_size > 1000
                                              for f in files),
                             jdump([(f.name, f.stat().st_size if f.exists() else 0)
                                    for f in files], 300))
                    r9.check("库采样率 48k (spec 4.2)", bgm.get("library_sr") == 48000,
                             str(bgm.get("library_sr")))
                    # W-1(verify-round3 §6): 不止 isinstance(x, list) —— 非空时逐条
                    # 断言结构(id/name/file/external)，防空壳列表或脏行蒙混过关。
                    ext = bgm.get("external_sfx")
                    r9.check("external_sfx 与 resource.db 打通(list, 逐条含 id/name/file 且 external)",
                             isinstance(ext, list) and all(
                                 isinstance(e, dict) and e.get("id") and e.get("name")
                                 and e.get("file") and e.get("external") is True
                                 for e in ext),
                             f"{len(ext or [])} 条: "
                             f"{jdump([(e.get('id'), e.get('file')) for e in (ext or [])[:2]], 260)}")
                    if rows:
                        code2, body2 = H.http("GET", f"/bgm/{rows[0]['id']}/preview",
                                             timeout=60, raw=True)
                        size = len(body2) if isinstance(body2, (bytes, bytearray)) else 0
                        r9.check("GET /api/bgm/{id}/preview 返回真 wav",
                                 code2 == 200 and size > 1000, f"{code2} {size}B")
                # 上传入库
                tmp_wav = H.TMP / "c4_bgm_upload.wav"
                make_wav(tmp_wav, seconds=4.0, sr=22050, freq=196.0)
                up = tmp_wav.read_bytes()
                code, body = H.http("POST", "/bgm/upload",
                                    files={"file": ("c4_bgm_upload.wav", up, "audio/wav")},
                                    data={"name": "C4 上传素材", "kind": "bgm",
                                          "moods": "平静,叙述"}, timeout=180)
                if code != 200:
                    r9.fail("POST /api/bgm/upload 200", f"{code} {jdump(body, 400)}")
                else:
                    rec = (body or {}).get("bgm") or {}
                    st["uploaded_bgm"] = rec.get("id")
                    H.kv("上传入库", jdump(rec, 400))
                    r9.check("上传返回 id 且转成 48k wav",
                             bool(rec.get("id")) and rec.get("sample_rate") == 48000,
                             f"id={rec.get('id')} sr={rec.get('sample_rate')}")
                    r9.check("上传素材出现在列表且 placeholder=False",
                             any(x.get("id") == rec.get("id") and not x.get("placeholder")
                                 for x in (H.http("GET", "/bgm")[1].get("bgm") or [])),
                             str(rec.get("id")))
                    r9.check("上传后不置 placeholder 且时长被实测",
                             (rec.get("duration_s") or 0) > 3.0, str(rec.get("duration_s")))
                code, body = H.http("POST", "/bgm/upload",
                                    files={"file": ("c4_bad.xyz", b"not-audio",
                                                   "application/octet-stream")},
                                    data={"kind": "bgm"}, timeout=60)
                r9.check("非法扩展名 -> 415 明确指引", code == 415, f"{code} {jdump(body, 200)}")

                # ---------------- 11: cast/preview 不以静音冒充 ----------------
                # W-4(verify-round3 §6): 旧版 200 分支 1 条 check、503 分支 3 条，
                # 跨轮次 check 总数漂移易误判"变多=变好"。现在两分支统一为
                # "形态 1 条(分支外公共) + 内容 3 条"；异常码属真失败，只报 1 条
                # 带上下文的 fail(失败轮次不再拿条数当口径)。
                r10 = su.new("POST cast/preview: 引擎不可用给 503, 绝不返回静音冒充成品")
                r10.evidence_add(log)
                t0 = time.time()
                code, body = H.http("POST", f"/dubbing/projects/{st['project_id']}/cast/preview",
                                    json_body={"role_name": "周建国", "text": "这本子是我亲自锁进柜子的。"},
                                    timeout=300, raw=True)
                took = time.time() - t0
                H.kv("cast/preview 耗时", f"{took:.2f}s -> HTTP {code}")
                r10.check("只接受 200(真音频)/503(显式不可用) 两种形态",
                          code in (200, 503), f"{code}")
                if code == 200:
                    size = len(body) if isinstance(body, (bytes, bytearray)) else 0
                    path = H.TMP / "c4_cast_preview.wav"
                    path.write_bytes(body)
                    stats = H.audio_stats(path)
                    H.kv("试听音频", jdump(stats, 300))
                    r10.check("200 时必须是真实有声音频(峰值>0)",
                              stats.get("peak", 0) > 1e-3, f"{size}B peak={stats.get('peak')}")
                    r10.check("200 时载荷尺寸自洽(>=1KB wav)", size >= 1024, f"{size}B")
                    r10.check("200 时长非零(>=0.2s)", stats.get("duration_s", 0) >= 0.2,
                              jdump(stats, 200))
                elif code == 503:
                    txt = body.decode("utf-8", "replace") if isinstance(body, bytes) else str(body)
                    H.kv("503 指引", txt[:400])
                    r10.check("引擎未加载 -> 503 + 明确指引(不返静音)",
                              "引擎" in txt or "TTS" in txt, f"{code} {txt[:160]}")
                    r10.check("C4-5: 503 必须秒回(不在请求内联加载模型, 旧行为 203s)",
                              took < 5.0, f"{took:.2f}s")
                    r10.check("503 带 model_status 供前端提示下一步",
                              "model_status" in txt and "state" in txt, txt[:200])
                else:
                    r10.fail("cast/preview 只接受 200/503",
                             f"{code} {jdump(body, 300)} (耗时 {took:.2f}s)")

                # ---------------- 12: cast 任务与转换任务的进度/列表互不污染 ----------------
                r11 = su.new("任务系统共存: cast 任务出现在 /api/tasks/ 且不带 output_path")
                r11.evidence_add(log)
                code, tl = H.http("GET", "/tasks/?limit=50")
                tasks = (tl or {}).get("tasks") or [] if code == 200 else []
                mine = [t for t in tasks if t.get("id") in
                        {st.get("task_llm"), st.get("task_nollm")}]
                H.kv("列表中的 cast 任务", jdump([(t.get("id")[:8], t.get("status"),
                                                 t.get("output_format")) for t in mine], 300))
                r11.check("两个 cast 任务均在任务列表可见", len(mine) == 2, f"{len(mine)}")
                r11.check("cast 任务无产物路径(不与转换混淆)",
                          all(not t.get("output_path") for t in mine),
                          jdump([t.get("output_path") for t in mine]))
                r11.check("cast 任务 logs 记录了阶段轨迹(PY-120)",
                          all(len(t.get("logs") or []) >= 3 for t in mine),
                          jdump([len(t.get("logs") or []) for t in mine]))

                # ---------------- 13: 二次分析幂等（重跑不产生脏角色） ----------------
                r12 = su.new("重跑 analyze-cast 幂等: 不残留脏角色/行数守恒")
                r12.evidence_add(log)
                before = H.db_rows("SELECT * FROM roles WHERE project_id=?", (st["project_id"],))
                code, body = H.http("POST", f"/dubbing/projects/{st['project_id']}/analyze-cast",
                                    json_body={"use_llm": False}, timeout=120)
                if code != 200:
                    r12.fail("二次 analyze 200", f"{code} {jdump(body, 300)}")
                else:
                    tid = (body.get("task") or {}).get("id")
                    poll3 = poll_progress(tid)
                    H.kv("二次任务终态", f"{poll3['terminal_status']} {poll3['elapsed_s']}s")
                    after = H.db_rows("SELECT * FROM roles WHERE project_id=?", (st["project_id"],))
                    H.kv("角色行数", f"{len(before)} -> {len(after)}")
                    r12.check("二次任务 completed",
                              poll3["terminal_status"] == "completed", str(poll3))
                    r12.check("行数守恒(无重复/脏行)", len(after) == len(before),
                              f"{len(before)} -> {len(after)}")
                    kept = [json.loads(x["extra_data"] or "{}").get("confirmed") for x in after]
                    r12.check("已确认角色的 confirmed 标记不被重跑冲掉",
                              all(kept), jdump(dict(zip([x["name"] for x in after], kept))))
                    # ---- C4-4: 手工成果逐项比对（重跑前快照 -> 重跑后） ----
                    pre = {x["name"]: json.loads(x["extra_data"] or "{}") for x in before}
                    post = {x["name"]: json.loads(x["extra_data"] or "{}") for x in after}
                    pre_voice = {x["name"]: x["voice_id"] for x in before}
                    post_voice = {x["name"]: x["voice_id"] for x in after}
                    manual = {n: (p.get("cast") or {}) for n, p in pre.items()
                              if (p.get("cast") or {}).get("profile_source") == "manual"}
                    H.kv("手工画像角色(profile_source=manual)",
                         jdump({n: {k: v for k, v in c.items()
                                    if k in ("gender", "age_group", "identity")}
                                for n, c in manual.items()}, 300))
                    attr_lost = []
                    for n, c in manual.items():
                        now_cast = (post.get(n, {}).get("cast") or {})
                        for k in ("gender", "age_group", "identity"):
                            if c.get(k) is not None and now_cast.get(k) != c.get(k):
                                attr_lost.append((n, k, c.get(k), now_cast.get(k)))
                    r12.check("重跑保留了手工修正的属性(gender/age_group/identity)",
                              bool(manual) and not attr_lost,
                              f"manual={list(manual)} 丢失={jdump(attr_lost, 300)}")
                    lost_alias = {n: sorted(set((p.get("aliases") or []))
                                            - set((post.get(n, {}).get("aliases") or [])))
                                  for n, p in pre.items()}
                    r12.check("手工追加的别名不被重跑冲掉",
                              not [k for k, v in lost_alias.items() if v],
                              jdump({k: v for k, v in lost_alias.items() if v}))
                    mix_lost = {n: (p.get("mix"), (post.get(n, {}).get("mix")))
                                for n, p in pre.items() if p.get("mix")
                                and post.get(n, {}).get("mix") != p.get("mix")}
                    r12.check("角色局部 mix 不被重跑冲掉", not mix_lost, jdump(mix_lost, 300))
                    voice_diff = {n: (v, post_voice.get(n)) for n, v in pre_voice.items()
                                  if v and post_voice.get(n) != v}
                    r12.check("已确认/手工绑定的 voice_id 不被推荐结果改写",
                              not voice_diff, jdump(voice_diff, 300))
                    # W-2(verify-round3 §6): 原式 all(a>=b) 在过滤集为空时真空真。
                    # 现在显式构造比较集并要求非空 + 至少一行严格变新。
                    cmp_pairs = [(n, p.get("analyzed_at"),
                                  post.get(n, {}).get("analyzed_at") or "")
                                 for n, p in pre.items() if p.get("analyzed_at")]
                    r12.check("W-2: analyzed_at 比较集非空(防空洞真)", bool(cmp_pairs),
                              f"{len(cmp_pairs)}/{len(pre)} 行")
                    r12.check("重跑仍刷新分析产物(analyzed_at 不回退且至少一行严格变新)",
                              all(b >= a for _n, a, b in cmp_pairs)
                              and any(b > a for _n, a, b in cmp_pairs),
                              jdump({n: (a, b) for n, a, b in cmp_pairs}, 300))
                    # W-5 后半: 重跑后手工别名"老朝奉"仍可被解析到规范名——
                    # 判据与 P5 normalize_speakers 同路径: 先查项目别名表，
                    # 未命中再查各 Role 行 aliases（_persist_cast 重跑只按抽取
                    # 候选重建项目表，手工行别名走角色列表这条腿）。
                    code_a, cast_a = H.http(
                        "GET", f"/dubbing/projects/{st['project_id']}/cast")
                    meta_a = ((cast_a or {}).get("cast") or {}).get("aliases") or {}
                    roles_a = (cast_a or {}).get("roles") or []
                    resolved_alias = meta_a.get("老朝奉") or next(
                        (r.get("name") for r in roles_a
                         if "老朝奉" in (r.get("aliases") or [])), None)
                    r12.check("W-5: '老朝奉' 重跑后仍可解析到规范角色名(P5 同路径)",
                              bool(resolved_alias)
                              and resolved_alias == st.get("manual_alias_role"),
                              f"解析={resolved_alias} 表命中={meta_a.get('老朝奉')} "
                              f"预期={st.get('manual_alias_role')}")
                    r12.check("重跑后旁白仍唯一",
                              sum(1 for x in after if x["role_type"] == "narrator") == 1,
                              jdump([(x["name"], x["role_type"]) for x in after]))

                # ---------------- 14: 一键确认的回退分支（PY-332） ----------------
                # 第 8 项那份 confirm 给每个角色都带了 voice_id —— 「用户没定音色、
                # 后端代分」这条回退分支一次都没走到，而 PY-332 的缺陷恰好只住在
                # 这一条分支里（旧实现用逐角色独立 Top1、无去重无门槛）。
                # 这里先把绑定清空再以空 body 确认，逼产品自己重分一遍。
                r13 = su.new("一键确认回退分支: 清空绑定后空 body 重分 -> 一人一枚 (PY-332)")
                r13.evidence_add(log)
                if not st["project_id"]:
                    r13.block("跳过：无项目")
                else:
                    live = H.db_rows("SELECT id, name FROM roles WHERE project_id=?",
                                     (st["project_id"],))
                    cleared = []
                    for x in live:
                        c0 = H.http("PUT",
                                    "/dubbing/projects/%s/cast/roles/%s"
                                    % (st["project_id"], x["id"]),
                                    json_body={"voice_id": ""}, timeout=60)[0]
                        if c0 == 200:
                            cleared.append(x["name"])
                    r13.check("清空全部角色绑定 (PUT cast/roles voice_id='')",
                              bool(live) and len(cleared) == len(live),
                              jdump({"rows": len(live), "cleared": cleared}, 300))
                    mid = H.db_rows("SELECT name, voice_id FROM roles WHERE project_id=?",
                                    (st["project_id"],))
                    unbound = [r["name"] for r in mid if not r["voice_id"]]
                    r13.check("清空后 DB 确有 voice_id IS NULL 的角色（否则本例恒真）",
                              bool(mid) and len(unbound) == len(mid), jdump(unbound, 300))
                    code5, conf5 = H.http("PUT",
                                          "/dubbing/projects/%s/cast/confirm"
                                          % st["project_id"],
                                          json_body={"roles": [],
                                                     "auto_assign_rest": True},
                                          timeout=120)
                    r13.check("空 body 一键确认 200", code5 == 200,
                              "%s %s" % (code5, jdump(conf5, 300)))
                    db_v = H.db_rows("SELECT name, voice_id FROM roles WHERE project_id=?",
                                     (st["project_id"],))
                    got_v = {r["name"]: r["voice_id"] for r in db_v}
                    distinct_v = {v for v in got_v.values() if v}
                    H.kv("回退分支分配结果", jdump(got_v, 400))
                    r13.check("回退分支给每个角色都分到一枚音色",
                              bool(got_v) and all(got_v.values()), jdump(got_v, 400))
                    r13.check("PY-332: 回退分配跨角色去重（distinct 音色数 == 角色数）",
                              len(distinct_v) == len(got_v),
                              "%d 角色 / %d 枚音色" % (len(got_v), len(distinct_v)))
                    # ---- 判别力自曝：本夹具里"逐角色独立 Top1"本来就互不相同时，
                    # 上面那条去重断言不会为 PY-332 变红（判别只在单元层），如实标注。
                    top1 = {}
                    for x in ((st["suggestions"] or {}).get("roles") or []):
                        ranked = x.get("suggestions") or []
                        if ranked:
                            top1[x["role_name"]] = (ranked[0].get("voice_id"),
                                                    ranked[0].get("score"))
                    collide = len(top1) - len({v for v, _s in top1.values()})
                    sub40 = sorted(n for n, (_v, s) in top1.items()
                                   if isinstance(s, (int, float)) and s < 40)
                    H.kv("判别力自曝: 夹具逐角色独立 Top1 的碰撞数",
                         "%d —— 0 表示本项对 PY-332 无判别力，"
                         "去重/门槛的判红在 tests/test_cast_confirm_dedup.py" % collide)
                    H.kv("判别力自曝: 夹具 Top1 低于门槛(40) 的角色",
                         jdump(sub40, 200))
                    r13.check("回退分配的音色全部来自建议池（无越界 id）",
                              distinct_v <= {v for v, _s in top1.values()},
                              jdump(sorted(distinct_v - {v for v, _s in top1.values()})[:5])
                              if distinct_v - {v for v, _s in top1.values()} else "OK")

            H.log("")
        finally:
            # ---------------- 清理（测试数据不留在生产库/素材库） ----------------
            if st.get("uploaded_bgm"):
                try:
                    from modules.mixer import BgmStore
                    store = BgmStore()
                    meta = store.load_meta()
                    row = next((x for x in meta["bgm"] if x.get("id") == st["uploaded_bgm"]), None)
                    meta["bgm"] = [x for x in meta["bgm"] if x.get("id") != st["uploaded_bgm"]]
                    store.save_meta(meta)
                    if row:
                        (store.bgm_dir / row["file"]).unlink(missing_ok=True)
                    H.log(f"  [cleanup] 移除上传素材 {st['uploaded_bgm']}")
                except Exception as exc:
                    H.log(f"  [cleanup] 上传素材清理失败(不影响判定): {exc}")
            if st.get("project_id"):
                code, body = H.http("DELETE", f"/dubbing/projects/{st['project_id']}")
                H.log(f"  [cleanup] DELETE project -> {code}")
            if st.get("book_id"):
                code, body = H.http("DELETE", f"/books/{st['book_id']}")
                H.log(f"  [cleanup] DELETE book -> {code}")
            H.stop_backend(be)
            H.log(f"  [cleanup] 后端已停止 (port {PORT})")

        rc = su.finish(exit_when_fail=False)
        print("")
        print("ALL_GREEN" if rc == 0 else f"RED ({rc})")
        sys.exit(0 if rc == 0 else 1)


if __name__ == "__main__":
    main()
