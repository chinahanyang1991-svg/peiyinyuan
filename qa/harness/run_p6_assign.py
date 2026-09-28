"""
[P6] assign 阶段实测: dubbing 项目/角色/音色 -> 每句映射到 voice_id
运行: python qa/harness/run_p6_assign.py
      PYY_TEST_PORT=8346 python qa/harness/run_p6_assign.py
      可选: PYY_MODEL_READY_TIMEOUT(默认 540s) / PYY_CLONE_TIMEOUT(默认 540s)
            PYY_P6_ADOPT_BUDGET(默认 300s)

真实路径: /api/voices/clone 建音色 -> /api/dubbing/projects 建项目
          -> /roles + /roles/assign-voice 绑定 -> ConversionWorkflow._stage_assign
          使用真实入库句子 + 真实角色分配

归因分层修订 (round-2 续测1g, F-R4-5)
--------------------------------------------------------------------------------
背景: 上一轮同一脚本 10/12 (479.9s) 而 2d 报 12/12 (276.0s) —— 差异 100% 来自
      `POST /voices/clone` 同步等 TTS 冷启动(F-R4-4)，客户端 300s 超时后
      "用例红"，与本阶段要验的 assign 语义无关。即 "P6 的绿是热态相关的"。
本轮规则 (两条, 都必须成立):
  ENV 层  : clone 的 客户端超时(code=0)/5xx 一律记 **ENV(BLOCKED)** 并写进
            meta.env_clone_events + 当次冷启动秒数, **不得记成产品 FAIL**;
            超时后按 name 查回服务端建成的音色行(收敛), 既不漏删孤儿也不假装没发生。
  逻辑层  : 音色分配/逐句 voice_id 的断言 **照常执行, 不得跳过** ——
            clone 全部 ENV 化时用库内既有真音色兜底绑定, meta.voice_binding_source
            记录兜底来源, 保证 distinct/role_name 这些判据始终有鉴别力。
  收尾    : 按"运行前 id 快照求差集"回收本轮产生的音色行 (F-R4-4 的孤儿行不在
            voice_ids 里, 只按 voice_ids 删必漏)。**不按名字扫** —— 名字扫会删掉
            他轮留在库里的证据行 (3c 的 bea5ea7c harness_甲 = PY-111 现场)。
  PY-111  : 字段名一致性之外再加两条可判据: 同一 payload 上 id 与 voice_id **取值必须同一**,
            且 clone 返回的主键值在列表接口里可寻 (F-R4-4 后两套键名同时在场,
            旧的"调用方需两套取值逻辑"结论已不成立, 判据必须随之收紧)。
"""
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness_lib as H  # noqa: E402

ROLES = ["旁白", "林昭", "周主事", "老掌柜"]
ADOPT_BUDGET_S = float(os.environ.get("PYY_P6_ADOPT_BUDGET", "300"))
MODEL_BUDGET_S = float(os.environ.get("PYY_MODEL_READY_TIMEOUT", "540"))
CLONE_TIMEOUT_S = float(os.environ.get("PYY_CLONE_TIMEOUT", "540"))


def list_voices(timeout=60):
    """GET /api/voices/ -> (code, voices list)。"""
    code, body = H.http("GET", "/voices/", timeout=timeout)
    voices = (body or {}).get("voices", []) if isinstance(body, dict) else []
    return code, (voices if isinstance(voices, list) else [])


def adopt_voice_by_name(name, budget=ADOPT_BUDGET_S, poll=5.0):
    """clone 客户端超时后服务端仍可能建成 (F-R4-4 实证) —— 按 name 查回 id。

    两个作用: (1) 用例能拿到真实音色继续跑逻辑断言; (2) finally 能删掉这一行,
    不再留下 3c 记的那种"孤儿音色行"。返回 None 表示预算内确实没建成。
    """
    t0 = time.time()
    while time.time() - t0 < budget:
        _, voices = list_voices()
        hit = [v for v in voices if v.get("name") == name]
        if hit:
            vid = hit[0].get("id") or hit[0].get("voice_id")
            H.kv(f"按 name 查回 {name}", f"id={vid} (等待 {time.time()-t0:.1f}s)")
            return vid
        time.sleep(poll)
    H.kv(f"按 name 查回 {name}", f"预算 {budget}s 内未出现")
    return None


def clone_one(nm, ref_path, gender, env_events):
    """带 ENV 归因的克隆。返回 (voice_id|None, clone 响应里的 voice dict|None, attempts)

    分类规则 (F-R4-5):
      200 且返回 voice 对象           -> 成功 (产品行为)
      code=0(客户端超时) 或 code>=500  -> ENV 传输失败: 先按 name 收敛查回, 再重试一次
      其余 4xx                        -> 产品契约失败: 如实交回上层判 FAIL
    """
    attempts = []
    for k in (1, 2):
        t0 = time.time()
        code, body = H.http(
            "POST", "/voices/clone",
            data={"name": nm, "language": "zh", "gender": gender},
            files={"audio": (ref_path.name, ref_path.read_bytes(), "audio/wav")},
            timeout=CLONE_TIMEOUT_S)
        dt = round(time.time() - t0, 1)
        v = body.get("voice") if isinstance(body, dict) else None
        attempts.append({"try": k, "code": code, "elapsed_s": dt,
                         "body_head": str(body)[:140]})
        H.kv(f"clone {nm} (第{k}次, {dt}s)", f"{code} {str(body)[:160]}")
        if code == 200 and isinstance(v, dict):
            return (v.get("id") or v.get("voice_id")), v, attempts
        if H.is_env_transport_failure(code):
            adopted = adopt_voice_by_name(nm)
            attempts[-1]["adopted_id"] = adopted
            env_events.append({
                "name": nm, "try": k, "http_code": code, "client_elapsed_s": dt,
                "class": "ENV_transport",
                "server_later_succeeded": bool(adopted),
                "note": "clone 同步等 TTS 冷启动(F-R4-4): 客户端超时/5xx 属环境传导, "
                        "不记产品逻辑 FAIL"})
            if adopted:
                return adopted, None, attempts
            continue
        return None, None, attempts        # 4xx: 真产品契约, 交给逻辑层判定
    return None, None, attempts


def library_fallback_voices(need, exclude):
    """从库内既有真音色里取 need 个可绑定音色 (保证逻辑断言不因 ENV 被跳过)。

    注意 (verify-round4 §2): 列表接口 to_dict 不导出 reference_audio_path,
    只有 GET /api/voices/{id} 的 to_dict_full 才给路径 -> 参考音频实存性必须逐个查详情。
    """
    _, voices = list_voices()
    picked = []
    for v in voices:
        if len(picked) >= need:
            break
        vid = v.get("id") or v.get("voice_id")
        if not vid or vid in exclude or vid in [p[0] for p in picked]:
            continue
        if (v.get("source") or "") not in ("cloned", "downloaded"):
            continue
        _, dv = H.http("GET", f"/voices/{vid}", timeout=60)
        full = (dv or {}).get("voice") if isinstance(dv, dict) else None
        if not isinstance(full, dict):
            full = dv if isinstance(dv, dict) else {}
        ref = full.get("reference_audio_path")
        if not ref or not Path(ref).exists():
            continue
        picked.append((vid, {"source": v.get("source"), "name": v.get("name"),
                             "reference_audio_path": ref}))
    return picked


def classify_clone_outcome(direct_200, n_env_events, want=2):
    """F-R4-5 的分层归因表 (纯函数, 便于离线变异检验 —— 三种形状都能喂出来看色)。

    返回 (kind, desc):
      "ok"   : want 次 clone 全部直接 200 且全程 0 条 ENV 传输事件 -> 产品判定, 期望 PASS
      "env"  : 出现过 clone 超时/5xx (含"超时后服务端仍建行、按 name 收敛查回") -> BLOCKED,
               **绝不记产品 FAIL**, 且逻辑层断言照常继续执行
      "fail" : 无任何 ENV 事件却拿不满 want 个音色 -> 这才是真实产品判定, 期望 FAIL
    """
    if n_env_events:
        return "env", f"克隆出 {want} 个可用音色 (ENV 归因: clone 超时/5xx, 非 assign 逻辑)"
    if direct_200 >= want:
        return "ok", f"克隆出 {want} 个可用音色 (clone 请求 200)"
    return "fail", f"克隆出 {want} 个可用音色"


def main():
    log = H.LOGS / "p6_assign.log"
    with H.Tee(log):
        su = H.Suite("P6-assign", "run_p6_assign.py")
        bid = pid = None
        voice_ids = []
        be = H.start_backend(H.LOGS / "p6_backend.log")
        env_events = []          # F-R4-5: clone 的 ENV 传输事件 (不记产品 FAIL)
        bind_voices = []         # 逻辑层实际绑定的音色 (clone 或库内兜底)
        pre_existing_voice_ids = None   # 克隆前的音色 id 快照, 用于收尾扫残留(F-R4-4)
        gate = {}
        # ---------- 用例0: ENV 前置层 —— 把冷启动从逻辑断言里剥离 (F-R4-5) ----------
        r0 = su.new("ENV 前置: 等 /api/system/model_status=ready + 冷启动秒数记账 (F-R4-5)")
        r0.evidence_add(log)
        try:
            import psutil
            free_gb = round(psutil.virtual_memory().available / 2 ** 30, 2)
        except Exception:
            free_gb = None
        su.set_meta("env", {
            "port": H.TEST_PORT,
            "PYY_PRELOAD_MODEL": os.environ.get("PYY_PRELOAD_MODEL", "(默认 1)"),
            "PYY_PRELOAD_MIN_FREE_GB": os.environ.get("PYY_PRELOAD_MIN_FREE_GB", "(默认 6)"),
            "free_memory_gb_at_start": free_gb,
            "model_ready_budget_s": MODEL_BUDGET_S,
            "clone_timeout_s": CLONE_TIMEOUT_S,
            "adopt_budget_s": ADOPT_BUDGET_S,
        })
        gate = H.wait_model_ready(total_timeout=MODEL_BUDGET_S)
        # 当次冷启动秒数: 服务端状态机自报值 (ready/failed 才有), 落进 artifact
        su.set_meta("model_state_before_clone", gate.get("state"))
        su.set_meta("model_cold_start_seconds", gate.get("load_seconds"))
        su.set_meta("model_wait_seconds", gate.get("waited_s"))
        su.set_meta("model_stop_reason", gate.get("stop_reason"))
        r0.check("/api/system/model_status 可达且回状态机字段 (PY-115)",
                 gate.get("endpoint_code") == 200 and bool(gate.get("state")),
                 f"code={gate.get('endpoint_code')} state={gate.get('state')} "
                 f"samples={gate.get('samples')}")
        if gate.get("state") == "ready":
            r0.check(f"模型在 {MODEL_BUDGET_S:.0f}s 预算内 ready (当次冷启动 "
                     f"{gate.get('load_seconds')}s) -> clone 不再同步付冷启动", True,
                     f"load_seconds={gate.get('load_seconds')} waited={gate.get('waited_s')}s")
        else:
            # ENV 层: 记 BLOCKED, 绝不记产品 FAIL; 冷启动秒数/原因如实入 artifact
            r0.block("模型在预算内未 ready (ENV 层, 非产品 FAIL)",
                     f"state={gate.get('state')} budget={MODEL_BUDGET_S:.0f}s "
                     f"waited={gate.get('waited_s')}s 服务端自报加载秒数={gate.get('load_seconds')} "
                     f"skipped_reason={gate.get('skipped_reason')} error={str(gate.get('error'))[:120]} "
                     f"-> clone 将同步付冷启动(F-R4-4), 本用例其余断言按 ENV 兜底继续")
        r0.notes.append("F-R4-5: 上一轮 10/12 的两条 FAIL 全部由 clone 同步等冷启动传导; "
                        "本用例把该因素显式等待 + 记账, 使跨轮次可比。")

        # ---------- 用例1: 音色可用 (逻辑层, 断言照常执行) ----------
        r = su.new("音色可用: /api/voices/ 与 /api/voices/clone")
        r.evidence_add(log)
        try:
            code, body = H.http("GET", "/voices/", timeout=60)
            H.kv("GET /voices/ ->", f"{code} total={body.get('total') if isinstance(body, dict) else body}")
            voices = (body or {}).get("voices", []) if isinstance(body, dict) else []
            if code == 200 and isinstance(body, dict):
                # 仅在列表确实返回 200 时建快照; 失败时保持 None, 让 finally 退回
                # "只删本轮登记的 id", 绝不按空集做全表差集(会误删他人证据行).
                pre_existing_voice_ids = {v.get("id") or v.get("voice_id")
                                          for v in voices} - {None}
                H.kv("运行前音色 id 快照",
                     f"{len(pre_existing_voice_ids)} 个 (供差集扫残留, F-R4-4)")
            for v in voices[:6]:
                H.kv("  音色", f"{v.get('id')} {v.get('name')} src={v.get('source')} "
                              f"ref={v.get('reference_audio_path')}")
            # 用真实参考音频克隆两个音色
            ref = H.PROJECT_ROOT / "data/voices/ref_6197bfa960504aba8acd57477c1f8b22.wav"
            if not ref.exists():
                cands = sorted(H.PROJECT_ROOT.glob("data/voices/*.wav"))
                ref = cands[0] if cands else None
            if ref is None:
                r.block("无参考音频可用于克隆", "data/voices 为空")
            else:
                H.kv("参考音频", f"{ref} ({ref.stat().st_size}B)")
                clone_keys = set()
                clone_resp = None
                direct_200 = 0
                for i, nm in enumerate(["harness_甲", "harness_乙"]):
                    vid_, v_, attempts = clone_one(nm, ref, "male" if i else "female", env_events)
                    if v_ and isinstance(v_, dict):
                        clone_keys |= set(v_.keys())
                        H.kv("  clone 响应字段", sorted(v_.keys()))
                        clone_resp = v_
                        direct_200 += 1
                    if vid_:
                        voice_ids.append(vid_)
                if clone_resp is None and gate.get("state") == "ready":
                    # 前两次都被 ENV 吃掉: 模型此时已热, 再探一次让 PY-111 判据可观测
                    vid_, v_, _ = clone_one("harness_丙_复探", ref, "female", env_events)
                    if v_ and isinstance(v_, dict):
                        clone_keys |= set(v_.keys())
                        clone_resp = v_
                        direct_200 += 1
                        H.kv("  复探 clone 响应字段", sorted(v_.keys()))
                    if vid_:
                        voice_ids.append(vid_)
                def pk(row):
                    """一个音色资源身上的两个主键键名 -> set(存在的值)。"""
                    if not isinstance(row, dict):
                        return set()
                    return {row.get("id"), row.get("voice_id")} - {None}

                _, vb = H.http("GET", "/voices/", timeout=60)
                all_rows = (vb or {}).get("voices", []) if isinstance(vb, dict) else []
                mine = [v for v in all_rows if pk(v) & set(voice_ids)]
                list_keys = set(mine[0].keys()) if mine else set()
                H.kv("列表接口中该音色的字段", sorted(list_keys))
                H.kv("本次 clone 统计", f"直接 200 成功 {direct_200} 次 / "
                                     f"ENV 传输事件 {len(env_events)} 条 / 拿到音色 id {len(voice_ids)} 个")
                if clone_resp is None:
                    # PY-111 主键一致性需要一次真实 200 响应才可观测 -> 记 ENV, 不记 PASS/FAIL
                    r.block("同一音色在 clone 与 list 接口中主键字段名一致 (PY-111)",
                            f"本轮 0 次 clone 返回 200 (ENV 事件 {len(env_events)} 条), "
                            f"clone 侧字段集不可观测 -> 按 ENV 记账, 不判 PASS 也不判 FAIL")
                else:
                    row = mine[0] if mine else {}
                    r.check("同一音色在 clone 与 list 接口中主键字段名一致 (PY-111)",
                            bool(mine) and ("id" in clone_keys) == ("id" in list_keys),
                            f"POST /voices/clone -> {sorted(clone_keys)} ; "
                            f"GET /voices/ -> {sorted(list_keys)}")
                    # 收敛判据: 两个键名同时挂在同一个资源上时, 值必须是同一个主键
                    divergent = [f"{src} 响应 id={i} 但 voice_id={v}"
                                 for src, payload in (("clone", clone_resp), ("list", row))
                                 for i, v in [(payload.get("id"), payload.get("voice_id"))]
                                 if i is not None and v is not None and i != v]
                    r.check("两套主键键名同时在场时取值同一 (PY-111 收敛判据)",
                            bool(row) and not divergent,
                            f"clone={sorted(pk(clone_resp))} list={sorted(pk(row))} "
                            f"背离={divergent}" if divergent or not row else
                            f"clone/list 两侧 id==voice_id (调用方不再需要两套取值逻辑)")
                    r.check("clone 返回的主键值在列表接口里可寻 (同一资源同一个 id)",
                            bool(pk(clone_resp) & {x for v in all_rows for x in pk(v)}),
                            f"clone 主键={sorted(pk(clone_resp))} 命中列表行={len(mine)}")
                kind, desc = classify_clone_outcome(direct_200, len(env_events))
                su.set_meta("clone_verdict_layer", kind)
                if kind == "ok":
                    r.check(desc, len(voice_ids) == 2,
                            f"voice_ids={voice_ids} 直接 200={direct_200} 次 ENV 事件=0")
                elif kind == "env":
                    # F-R4-5 核心: 环境超时不得记成产品 FAIL, 也不得跳过后面的逻辑断言
                    r.block(desc, f"voice_ids={voice_ids} 直接 200={direct_200} 次 "
                                  f"env_events={env_events}")
                    r0.notes.append(f"clone ENV 事件 {len(env_events)} 条, 详见 meta.env_clone_events")
                else:
                    r.check(desc, len(voice_ids) == 2,
                            f"voice_ids={voice_ids} 直接 200={direct_200} 次 "
                            f"(无 ENV 传输事件 -> 这是真实产品判定)")
                su.set_meta("env_clone_events", env_events)
                su.set_meta("clone_direct_200_count", direct_200)

            # 逻辑层兜底: clone 因 ENV 拿不满 2 个 -> 用库内既有真音色补齐, 断言不跳过
            bind_voices = list(dict.fromkeys(voice_ids))
            if len(bind_voices) < 2:
                extra = library_fallback_voices(2 - len(bind_voices), set(bind_voices))
                H.kv("音色兜底(F-R4-5)", f"clone 只得 {len(bind_voices)} 个 -> "
                                     f"库内既有音色补 {len(extra)} 个: "
                                     f"{[(v[1].get('source'), v[1].get('name')) for v in extra]}")
                bind_voices += [v[0] for v in extra]
                su.set_meta("voice_binding_source",
                            "clone+library_fallback" if voice_ids else "library_fallback")
            else:
                su.set_meta("voice_binding_source", "clone")
            su.set_meta("bind_voices", bind_voices)
            r.check("前置: 可绑定的不同音色 >= 2 (逻辑层断言得以执行)", len(set(bind_voices)) >= 2,
                    f"来源={su.meta.get('voice_binding_source')} 音色={bind_voices}")

            # ---------- 书籍与句子 ----------
            r = su.new("真实入库正文 -> 句子集")
            code, body = H.http("POST", "/books/import",
                                files={"file": ("p6_sample.txt",
                                                (H.FIXTURES / "sample_small.txt").read_bytes(),
                                                "text/plain")}, timeout=300)
            if code != 200:
                r.block("导入失败", str(body)[:200])
            else:
                bid = body["book"]["id"]
                _, cb = H.http("GET", f"/books/{bid}/chapters", timeout=120)
                # WF-09 起列表只下发预览，正文走懒加载出口补（见 fill_chapter_content）
                cb["chapters"] = H.fill_chapter_content(cb.get("chapters", []))
                from modules.text_processor.sentence_splitter import SentenceSplitter
                from modules.text_processor.dialogue_detector import DialogueDetector
                from modules.text_processor.tone_detector import ToneDetector
                from modules.cast_analyzer.cast_extractor import looks_like_name
                sp, dd, td = SentenceSplitter(), DialogueDetector(
                    name_gate=looks_like_name), ToneDetector()
                sentences = []
                for ch in cb.get("chapters", []):
                    for s in sp.split(ch["content"] or "", "zh"):
                        seg = dd.detect_dialogues(s)
                        hk = next((d.get("role_hint") for d in seg
                                   if d.get("type") == "dialogue" and d.get("role_hint")), "")
                        sentences.append({"chapter_id": ch["id"], "chapter_title": ch["title"],
                                          "text": s, "char_count": len(s),
                                          "is_dialogue": bool(hk), "speaker": hk or "",
                                          "tone": td.detect(s).get("primary", "neutral")})
                H.kv("句子数", len(sentences))
                H.kv("带 speaker 的句子", sum(1 for s in sentences if s["speaker"]))
                H.kv("speaker 取值", sorted({s["speaker"] for s in sentences if s["speaker"]}))

                # ---------- 项目 + 角色 + 绑定 ----------
                r = su.new("dubbing 项目/角色/音色分配 API")
                code, body = H.http("POST", "/dubbing/projects",
                                    json_body={"name": f"harness_p6_{int(time.time())}",
                                               "book_id": bid, "mode": "multi"}, timeout=60)
                H.kv("create project", f"{code} {str(body)[:160]}")
                if code != 200:
                    r.fail("项目创建失败", str(body)[:200])
                else:
                    pid = body["project"]["id"]
                    _, pj = H.http("GET", f"/dubbing/projects/{pid}", timeout=60)
                    H.kv("自动创建的旁白角色",
                         [(rr["id"], rr["name"], rr.get("role_type"))
                          for rr in pj["project"].get("roles", [])])
                    made = {}
                    for nm in ROLES:
                        code, body = H.http("POST", f"/dubbing/projects/{pid}/roles",
                                            json_body={"name": nm,
                                                       "role_type": "narrator" if nm == "旁白" else "custom"},
                                            timeout=60)
                        if code == 200:
                            made[nm] = body["role"]["id"]
                    H.kv("角色", made)
                    r.check("4 个角色创建成功", len(made) == len(ROLES), str(list(made)))
                    ok_assign = 0
                    for i, nm in enumerate(ROLES):
                        vid = bind_voices[i % max(len(bind_voices), 1)] if bind_voices else "voxcpm_default"
                        code, body = H.http("PUT", f"/dubbing/projects/{pid}/roles/assign-voice",
                                            json_body={"role_id": made[nm], "voice_id": vid},
                                            timeout=60)
                        ok_assign += 1 if code == 200 else 0
                    H.kv("绑定用的音色来源(F-R4-5)", f"{su.meta.get('voice_binding_source')} "
                                          f"-> {bind_voices}")
                    H.kv("assign-voice 成功数", f"{ok_assign}/{len(ROLES)}")
                    r.check("角色音色绑定全部成功", ok_assign == len(ROLES), f"{ok_assign}")
                    _, pj2 = H.http("GET", f"/dubbing/projects/{pid}", timeout=60)
                    all_roles = pj2["project"]["roles"]
                    for rr in all_roles:
                        H.kv(f"  role {rr['name']}", f"id={rr['id']} voice_id={rr.get('voice_id')}")
                    mine2 = [rr for rr in all_roles if rr["id"] in made.values()]
                    r.check("已绑定的角色 voice_id 全部持久化",
                            len(mine2) == len(made) and all(rr.get("voice_id") for rr in mine2),
                            str([(rr['name'], rr.get('voice_id')) for rr in mine2])[:200])
                    dup_names = [rr["name"] for rr in all_roles
                                 if sum(1 for x in all_roles if x["name"] == rr["name"]) > 1]
                    r.check("角色名不与自动创建的旁白重复(唯一性约束)",
                            not dup_names,
                            f"自动创建的 narrator + 手工创建的同名角色共存: "
                            f"重复名={sorted(set(dup_names))} 角色总数={len(all_roles)} "
                            f"-> 项目里出现两个旁白, 其中一个永远没有音色")

                    # ---------- _stage_assign 真实映射 ----------
                    r = su.new("pipeline._stage_assign 的逐句 voice_id 映射")
                    from modules.workflow.conversion_workflow import ConversionWorkflow
                    cw = ConversionWorkflow()
                    ctx = {"task_id": "harness-p6", "sentences": [dict(s) for s in sentences]}
                    out = cw._stage_assign(ctx, pid)
                    sents = out["sentences"]
                    assigned = [s for s in sents if s.get("voice_id")]
                    distinct = {s.get("voice_id") for s in sents if s.get("voice_id")}
                    H.kv("句子", len(sents))
                    H.kv("映射到 voice_id 的句子", f"{len(assigned)} ({len(assigned)/max(len(sents),1):.0%})")
                    H.kv("用到的不同 voice_id", sorted(str(x) for x in distinct))
                    by_role = {}
                    for s in sents:
                        by_role[s.get("role_name", "-")] = by_role.get(s.get("role_name", "-"), 0) + 1
                    H.kv("role_name 分布", by_role)
                    for s in sents[:8]:
                        H.kv("  句", f"{s['text'][:24]!r} spk={s.get('speaker')!r} "
                                    f"-> {str(s.get('voice_id'))[:10]} {s.get('role_name','')}")
                    r.check("每句都有 voice_id", len(assigned) == len(sents),
                            f"{len(assigned)}/{len(sents)}")
                    H.kv("本用例音色绑定来源(F-R4-5 归因)",
                         f"{su.meta.get('voice_binding_source')} -> {bind_voices}")
                    r.check("多角色模式下确实出现多个不同音色 (逻辑层, 与 clone 是否成功无关)",
                            len(distinct) >= 2,
                            f"distinct={len(distinct)} 绑定音色={bind_voices} "
                            f"来源={su.meta.get('voice_binding_source')} "
                            f"-> 若这里红而 clone 侧是 ENV, 才是 assign 语义本身的缺陷")
                    spk_set = {s.get("speaker") for s in sents if s.get("speaker")}
                    H.kv("analyze 产出的 speaker 集合", sorted(spk_set))
                    H.kv("项目角色名集合", sorted(made))
                    H.kv("speaker 与角色名交集", sorted(spk_set & set(made)))
                    r.check("speaker 能命中项目角色名 (analyze->assign 的连接键)",
                            bool(spk_set & set(made)),
                            f"speaker={sorted(spk_set)} vs 角色名={sorted(made)} "
                            f"交集为空 -> _stage_assign 的对白->角色 查表 100% 未命中")
                    rnames = {s.get("role_name") for s in sents}
                    r.check("role_name 随句子变化", len(rnames) > 1,
                            f"role_name 取值集合={sorted(str(x) for x in rnames)} "
                            f"-> 52 句全部落到同一个 narrator")

                    # ---------- VoiceAssignment (按章节/字符区间) 是否被消费 ----------
                    r = su.new("/assignments 写入的分段音色是否参与合成")
                    code, body = H.http("PUT", f"/dubbing/projects/{pid}/assignments",
                                        json_body={"assignments": [
                                            {"chapter_id": cb["chapters"][0]["id"],
                                             "voice_id": bind_voices[0] if bind_voices else "v0",
                                             "role_id": made.get("林昭"),
                                             "start_char": 0, "end_char": 999999,
                                             "settings": {"emotion": "angry"}}
                                        ]}, timeout=60)
                    H.kv("PUT /assignments", f"{code} {str(body)[:120]}")
                    rows = H.db_rows("SELECT COUNT(*) c FROM voice_assignments WHERE project_id=?",
                                     (pid,))
                    H.kv("voice_assignments 行数", rows[0]["c"])
                    src = (H.PROJECT_ROOT / "modules/workflow/conversion_workflow.py"
                           ).read_text(encoding="utf-8")
                    seg = src.split("def _stage_assign", 1)[1].split("async def _stage_synthesize", 1)[0]
                    uses = "assignments" in seg or "VoiceAssignment" in seg
                    r.check("_stage_assign 读取 voice_assignments", uses,
                            f"函数体出现 'assignments' 次数 = {seg.count('assignments')}; "
                            f"DB 已有 {rows[0]['c']} 行分段分配, 但 pipeline 完全不读")

                    # ---------- project 不存在时的行为 ----------
                    r = su.new("project_id 无效时的降级行为")
                    ctx2 = {"task_id": "harness-p6b",
                            "sentences": [dict(s) for s in sentences]}
                    out2 = cw._stage_assign(ctx2, "not-exist-project")
                    vs = {s.get("voice_id") for s in out2["sentences"]}
                    H.kv("voice_id 集合", vs)
                    r.check("无效项目 -> 全部 voice_id=None (交给引擎默认音色)",
                            vs == {None}, str(vs)[:120])
        finally:
            if pid:
                H.http("DELETE", f"/dubbing/projects/{pid}")
            if bid:
                H.http("DELETE", f"/books/{bid}")
            for vid in voice_ids:
                H.http("DELETE", f"/voices/{vid}")
            # F-R4-4 残留清扫: 客户端超时后服务端仍会建行, 这类行不在 voice_ids 里.
            # 只按"运行前 id 快照"求差集删除, 绝不按名字扫 —— 名字扫会删掉他轮留下的证据行.
            swept = []
            if pre_existing_voice_ids is not None:
                _, vb = H.http("GET", "/voices/", timeout=60)
                now = [(v.get("id") or v.get("voice_id"))
                       for v in (vb or {}).get("voices", [])] if isinstance(vb, dict) else []
                for oid in [x for x in now if x and x not in pre_existing_voice_ids]:
                    H.http("DELETE", f"/voices/{oid}", timeout=30)
                    swept.append(oid)
            print(f"[cleanup] 本轮残留音色回收 {len(swept)} 个 (F-R4-4 孤儿行): {swept}; "
                  f"快照内 {len(pre_existing_voice_ids or [])} 个既有音色(含他轮证据行)未触碰")
            H.stop_backend(be)
        su.finish(exit_when_fail=False)


if __name__ == "__main__":
    main()
