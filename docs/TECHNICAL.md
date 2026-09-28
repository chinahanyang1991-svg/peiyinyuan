# 配音员（peiyinyuan）程序技术文档

> 基线：git HEAD `b61c445`，2026-09-22。全部结论以磁盘代码为准（证据格式 `文件:行号`），不照抄 README（README 已确认严重过时，见 §13）。
> 本文档由主线于终验（RUN-VERIFY）+ 全盘审计（DISK-AUDIT）当次实测生成；运行时数字（延迟/计数）均标注采样时刻。

---

## 1. 系统定位与总览

**一句话**：本地优先的电子书→有声书全自动转换系统——导入电子书（epub/mobi/azw3/pdf/txt/md/docx/html），九阶段流水线完成解析、清洗、分句、对白分析、角色分配、TTS 合成（9 枚可发声引擎 + 1 枚 sample 回放档，注册表 10 键；多说话人 cast 匹配、BGM/SFX 混音）、后处理与封装（wav/flac/mp3/m4b），配 React Web 前端与桌面壳。

分层：

| 层 | 位置 | 说明 |
|---|---|---|
| API 层 | `peiyinyuan/backend/` | FastAPI，32 个 py / 6,875 行；9 组路由 + 83 端点 |
| 领域层 | `peiyinyuan/modules/` | 127 个 py / 29,113 行（含 vendored 模型源码）：解析/文本处理/cast/TTS引擎/音色管理/混音/工作流 |
| 数据层 | `peiyinyuan/data/` | SQLite 主库 `peiyinyuan.db`（9 表）+ 资源库 `resource.db`（4 表）+ 文件资产（books/voices/models/output/bgm） |
| 前端 | `peiyinyuan/frontend/` | React 18.3 + TS 5.5 + Vite 5.4，src 55 文件 / 16,151 行 |
| 引擎侧车 | 进程外 | GPT-SoVITS api_v2（:9880 HTTP）、MinerU（容器/子进程 :8001）、freellmapi LLM（:3001，可选） |
| 运维/质保 | `agent-ops/`、`peiyinyuan/qa/` | 多智能体台账 LANES.md、FINAL-E2E 快检驱动、P1..P9/C1..C6 harness |

部署形态三种：开发（**唯一入口**：双击 `一键启动.bat` / `start-all.cmd`，同一实现 `start_all.py`）、Docker（`docker/docker-compose.yml`，5 服务 + llm/mineru 两 profile）、Electron 桌面壳（存在但极小、非主链路，待评估）。旧三枚入口 `start.bat` / `start.ps1` / `start_gui.py` 已于 2026-09-27 下线（保留多入口本身就是"用户不知道点哪个"的根源），git 历史里还能找回。

## 2. 快速上手

```bash
# 后端（Python ≥3.10，实测 3.11.0rc2）
cd <你的工作目录>\PYY\peiyinyuan
pip install -e ".[dev]"                    # 引擎档按需追加：".[voxcpm,edgetts,codecs]"
uvicorn backend.main:app --reload --port 8000

# 前端（dev 只绑 localhost——IPv6 陷阱：探活请用 http://localhost:5173 而非 127.0.0.1）
cd frontend && npm install && npm run dev   # :5173，/api 反代 :8000（ws:true）

# GPT-SoVITS 质量克隆档（可选，独立运维，不由主程序启停）
# 必须用 GPT-SoVITS 自己的 venv 解释器：系统 Python311 无 ffmpeg 模块，
# 裸 `python api_v2.py` 会在 `TTS.py:18 import ffmpeg` 处 ModuleNotFoundError 崩（09-22 09:46 实测）。
cd <你的工作目录>\PYY\GPT-SoVITS && .venv\Scripts\python.exe api_v2.py -a 127.0.0.1 -p 9880 -c GPT_SoVITS/configs/tts_infer.yaml
```

- **一键启动器（#90 LAUNCHER-V3，统一入口）**：双击 `peiyinyuan/start-all.cmd` 或 `python -X utf8 start_all.py`。三腿各一枚**带标题的可见控制台窗口**（`PY-Backend:8000` / `PY-Frontend:5173` / `PY-GSV:9880`，GSV 用 `GPT-SoVITS\.venv\Scripts\python.exe`），stdout 同时 tee 到 `peiyinyuan/logs/<leg>-<port>.log`；可选模块 MinerU:8001 / freellmapi:3001 / Redis:6379 **只探不启**，没在跑就标 `SKIP` 并给原因。起完跑健康门（`/api/health`=200、`model_status` 里 voxcpm 真 `ready`、`localhost:5173`=200、`:9880/docs`=200），逐项 PASS/FAIL。端口占用**先归因**（PID + 命令行原文 + 父子树）再决定复用/清理，只动自家签名（`backend.main:app` / `vite` / `api_v2.py`），**绝不盲杀**（`:9881` 那对本机第二实例不在管辖内）。常用开关：`--only leg`、`--restart leg`、`--status`、`--health-only`、`--reload`（**仅开发期**：后端默认不带热重载，见 §15.3）、`--model-timeout N`。`agent-ops/cold-start.cmd [gsv|backend|frontend]` 已改为委托它的 shim（保留旧入口不双实现）。**起后端之前先自陈两件事**（PY-354 / PY-357，09-24 深夜）：① 逐台探测候选解释器（`PYY_PYTHON` → 当前解释器 → `py -0p` 装表）缺哪些 `BACKEND_DEPS`，用第一台齐全的，一台都不齐则**拒起该腿**并打印每台缺什么、在哪些路径上试过（拿缺库的 Python 起后端＝一台"能打开但说不出话"的服务，试听/合成必 500）；② 对选中的那台再跑一次产品自己的解析口 `modules/tts_engine/ffmpeg_utils.probe()`（cwd 钉在仓库根），报 `编码器 (mp3/m4b 导出): 可用 —— 来源 …` 或 `!! … 缺 mp3/m4b …会被拒 (503)`。②是**软依赖只警告**：`imageio_ffmpeg` 是 optional extra，缺了照样起后端（wav/flac 不受影响）、不翻转退出码，与①的硬门分开（`start_all.py:109` 的 `BACKEND_DEPS` 对硬门，`:142` 的 `SOFT_DEPS` 对警告）。

- Swagger：`/api/docs`，ReDoc：`/api/redoc`（`backend/main.py:51-52`）。
- 冒烟三探针：`GET /api/health`、`GET /api/system/model_status`、跑一本书到下载。
- 常见启动失败归因：VoxCPM 权重缺失（`data/models/voxcpm2`）、GSV :9880 不可达（适配器 5s 探活即失败归因，不等 600s）、ffmpeg 三档兜底全空（见 §7.7）。**"模型没装载"自 #86（ENG-PRELOAD，09-22 10:28）起不再是设计行为**：启动即无条件预装载，`skipped_reason` 只在用户显式设 `PYY_PRELOAD_MODEL=0` 或显式设 `PYY_PRELOAD_MIN_FREE_GB` 时出现；装载失败改由 `state=failed` + `error`（真实异常原文 + 尝试次数 + 装载结束可用内存）上报，最多重试 `RETRY_LIMIT=2` 次（共 3 次尝试）。排查口径：`GET /api/system/model_status` 看 `slots.<引擎>.{state,error,attempts,gate}`。
- 注意：`pyproject.toml:116` 的 `[project.scripts] peiyinyuan = "backend.main:app"` 指向 ASGI 对象，`peiyinyuan` 命令行**不能**起服务，必须用 uvicorn。

## 3. 架构与执行链路

### 3.1 请求→成品全链路
`POST /api/tasks/convert`（`backend/api/tasks.py:69`）→ 校验格式枚举（未知→422，:75-79）与 ffmpeg 可用（→503）→ `asyncio.create_task(run_task())`（:111）**进程内执行** → `run_conversion`（`modules/workflow/conversion_workflow.py:359`）建 pipeline → 九阶段顺序跑 → 进度回调 `_emit`（:295）双写 WS 广播 + DB → P9 写盘 → `GET /tasks/{id}/download`（`tasks.py:366`，RFC 5987 中文文件名，PY-119）。

**Celery 是死轨（已彻底摘除）**：全仓库无一处 `delay()`/`apply_async()` 调用（09-22 实测 grep，09-28 复跑仍为空）；`backend/tasks/`（`celery_app.py`+`convert_task.py`）已不在盘上，compose 里无 `celery-worker` 服务，三枚旧入口的 worker 启动腿于 09-27 整体下线。`celery` 现在只出现在 `tools/setup_deps.py:142` 的环境检查清单里作指示器，不得再写成任务腿（`tests/test_u10_dead_code_gone.py` 两条测试守着）。文档与运维按"进程内 asyncio"理解。

### 3.2 并发模型
- 事件循环内：路由、pipeline 调度、`asyncio.gather` 在线并发合成（路径 A）。
- `run_in_executor` 线程池：同步 TTS/解析 handler（`modules/workflow/pipeline.py:171-182`）、本地引擎逐句合成（路径 C）。
- 守护线程：`model_preloader` per-engine 槽位预热（`modules/tts_engine/model_preloader.py:93-168`）。
- 跨进程：GPT-SoVITS HTTP、MinerU 子进程/容器。
- 加载互斥：`engine_registry._load_lock`（`engine_registry.py:53`）双检锁，防预热线程与任务线程各加载一份模型。

### 3.3 暂停/恢复/取消
三层：DB status 落库 + pipeline `asyncio.Event`（`pipeline.py:43-46`，非忙等）+ `_cancelled_tasks` 集合；生效边界在 stage 与句/块之间（WF-07：取消先记意图再落终态，取消优先于失败判定，`conversion_workflow.py:1449-1462`）。

### 3.4 状态持久化与对账
进程内 tracker（`progress_tracker.py`）+ DB 行（`conversion_tasks.updated_at`=心跳，`models/task.py:66-68`）+ 启动对账 PY-311（`main.py:79-102`：进程刚起活跃集必空→所有非终态行判僵尸落 failed；`PYY_STARTUP_RECONCILE=0` 可关）。

### 3.5 失败语义红线（PY-117/118/304 演进史）
1. `mock_tts` 静音替身已停用（`conversion_workflow.py:1158-1163`）——**绝不用静音冒充成功**；
2. 单句空数组/静音 peak 判失败并带上标归因（:1282-1293）；零产出 raise 而非输出静音成品（:1464-1467）；
3. 引擎 `load_model` 未抛错但实际未就绪（`_loaded=False`/`_model is None`）同样上抛 `EngineLoadError`（`engine_registry.py:210-222`，PY-304 防"半死引擎"）。

## 4. 数据层

### 4.1 主库 `data/peiyinyuan.db`（SQLAlchemy 2.0 + WAL）
连接配置：`pool_size=10/max_overflow=20/pre_ping/recycle=3600`（`backend/models/base.py:37-45`）；pragma `journal_mode=WAL, synchronous=NORMAL, busy_timeout=5000`（:48-54）。实测体积 116MB（2026-09-22），主体是 chapters.content 全文。

| 表 | 行数(09-22 实测) | 关键列 | 关系 |
|---|---|---|---|
books | 89 | id(uuid), title, format, file_path, cover_path, total_chars, extra_data JSON | 1:N chapters，cascade delete-orphan |
chapters | 8,135 | id, book_id FK, title, "index", content, char_count | N:1 books |
voice_profiles | 517 | id, name, source(builtin/downloaded/cloned/designed/sample), category(human/animal/electronic/anime/mechanical), engine, language, gender, age_group, style_tags, sample_audio_path, reference_audio_path, meta, seed | 被 roles/assignments 反向引用（无 ORM relationship） |
dubbing_projects | 77 | id, book_id, mode(single/multi/mixed), status(draft/ready/converting/done/**book_missing**), global_settings JSON(含 mix), extra_data JSON(含 cast) | 1:N roles、1:N voice_assignments（cascade）；book_id 有 FK 但 SQLite 默认不开外键，删书失效由服务层写 status（WF-10，`book_service.py`） |
roles | 135 | id, project_id, name, role_type, voice_id | N:1 project/voice |
voice_assignments | 0 | 片段级区间：chapter_id, voice_id, role_id, start_char, end_char, settings | 消费点 `conversion_workflow.py:1008-1028`（PY-114） |
conversion_tasks | 136 | status 六态, current_stage(九阶段+loading_model), output_path, settings/logs JSON, updated_at=心跳 | 逻辑引用 project_id/book_id，无 FK；序列化派生 format_used（PY-124 真产物口径） |
workflow_templates | 0 | stages JSON, engine | 独立 |
memories | 1 | memory_type, title, content, tags | 独立 |

**迁移策略（无 alembic）**：`create_all` 补表 + 启动期 PRAGMA 比对后 `ALTER TABLE ADD COLUMN`（只加不删改，`base.py:98-129`），结果入 `LAST_ADDED_COLUMNS` 供启动日志；幂等种子：5 条内置音色 + 12 条 Edge 中文音色（`base.py:201-306`）、category NULL 回填 human（:132-140）。建表清单三处登记（base.py×2 + models/__init__.py）由 `qa/harness/run_db_schema.py` 看护（2G-1 教训）。
序列化双口径：`to_dict`（详情全量）vs `to_list_dict`（列表 200 字预览、不查 content，WF-09 去 N+1，`models/book.py`）。

### 4.2 资源库 `data/resource.db`（裸 sqlite3，非 ORM）
schema 单一事实源 `tools/resource_scout/rs_db.py:18-109`。四表：resources(5) / voices(25) / voice_samples(26, `verified` 列**只由 verify_samples.py 写**) / run_log(509 审计流水)。
跨库打通唯一消费点：`modules/mixer/bgm_store.py:420-470 external_sfx()` 以 `mode=ro` URI 只读探测外部音效表，缺失即返回 `[]`，绝不创建/修改；对外经 `GET /api/bgm` 以 `ext:<table>:<name>` 暴露。离线导入方向：`voice_samples WHERE verified=1` → `voice_profiles`（`import_to_voices.py`）。

### 4.3 历史遗留（已坐实，勿用）
`data/app.db`（0 字节）、`data/db/`（空目录）为遗留壳；当前 DB 路径实际有两条并行来源——`Config.DB_PATH`（`config.py:19`）与 `base.py:24-28` 硬编码 `data/peiyinyuan.db`，以后者为准。

## 5. API 表面（83 端点 + 1 WebSocket）

挂载：`backend/main.py:190-213`（`register_routes()` 模块导入末尾调用，路由延迟 import 防循环）。

| 组 | 前缀 | 数 | 代表端点 |
|---|---|---|---|
| health/system | 无 | 5 | `/api/health`、`/api/system/info`、`/api/system/liveness`（PY-348：读 `logs/liveness.log` 最后几条，把"上次它是什么时候没的、像自退还是像被强杀"做成可查的）、`/api/system/model_status`（预热 slots 状态机 idle/loading/ready/failed，L14）、`/api/system/engines`（指令#1/#3：两档清单 + `paid`/`key_present`，**绝不出 Key**，清单拉不到时回 `status:"degraded"` 而不是 200+空） |
| books | `/api/books` | 11 | import / import/batch / 列表(GROUP BY 计数) / 章节 CRUD / `chapters/{id}/content` 懒加载(:190) / merge / split / DELETE /{id}(级联+book_missing 标记) |
| voices | `/api/voices` | 14 | 列表(五类/引擎/来源筛选,limit≤500 超限422) / statistics(须先于 /{id} 声明,WF-05) / clone(引擎白名单) / analyze / design / online search+download(HF/GitHub) / preview(回 `X-Last-Successful-Seed` 头,US-C4) |
| dubbing | `/api/dubbing` | 10 | 项目/角色/分配 CRUD、settings(mix 清洗校验)、preview（**缺陷：硬编码 voxcpm，见 §13**） |
| tasks | `/api/tasks` | 10 | convert / pause / resume / cancel / progress(HTTP) / **WS `/{id}/progress`**(:245，终态或心跳超期断连) / download |
| cast/bgm/mix | `/api` | 13 | analyze-cast(后台任务化) / cast / suggestions(Top-N 带打分拆解) / confirm / 角色改删 / cast preview / mix-settings 读写 / bgm 曲库+上传+试听 |
| workflows | `/api/workflows` | 8 | templates CRUD、`/system/status`（**缺陷：三处失真，见 §13**）、`POST /create` 为已废弃明确拒绝位（WF-17） |
| llm | `/api/llm` | 8 | detect-chapters / polish / optimize-ssml / analyze-sentiment / parse-voice / ready / health / **provider**（指令#3：当前接的是哪家大模型 + 缺哪个 env 才可用 + 全量 presets，同样**不回显 Key**） |
| mineru | `/api/mineru` | 2 | parse / health |
| memory | `/api/memory` | 5 | CRUD + search |

**契约纪律**（写前端必读）：
- 路由顺序：静态子路径必须先于动态段（`voices.py:2-6`、`books.py:190`，WF-05 事故换来的）。
- 错误语义：422（枚举白名单 + 未知 query 拒收，WF-21）、415（格式）、403（内置 BGM 占位不可删，WF-06）、503（引擎/编码器不可用）、409/404；FastAPI 结构化 detail 由前端 `detailText()` 扁平化消费。
- 电子书格式：`config.py:58` 声明 5 种，`parser_factory.py:46-55` FORMAT_MAP 实含 8 种（html/htm/docx），两边须同源（run_p1_import.py 护栏）；`.chm` 已移除（PY-102）。
- 输出格式：`SUPPORTED_OUTPUT_FORMATS = ("wav","flac","mp3","m4b")`（`conversion_workflow.py:31`），未知格式请求阶段 422，不再静默降级。

## 6. 九阶段流水线（P1..P9）

枚举与调度：`modules/workflow/pipeline.py:18-27`；真实接线 `conversion_workflow.py:338-355`（`create_pipeline` 注册 9 handler）。

| 阶段 | 实现位置 | 要点 |
|---|---|---|
| P1 import | :664 | 校验 book 存在，写 title/author/language |
| P2 parse | :680 | **裁定不做二次全量解析**（PY-104，:682-694）：O(章数) 校验 content 非空 + char_count 一致 + 懒回填（:702-724）；仅可用章节=0 时按 file_path 重建内存缓存不回写 DB（:726-745） |
| P3 clean | :759 | `TextCleaner.clean` 页码/水印/广告清洗，重算 char_count/total_chars |
| P4 split | :818 | `SentenceSplitter.split`；策略 `_resolve_split_policy`（:782）从引擎集合取**最严** SplitPolicy；`has_pronounceable_content`（:37-54）丢弃零可发音片段（WF-08 防标点带崩整书） |
| P5 analyze | :901 | `DialogueDetector`+`ToneDetector`→is_dialogue/speaker/tone/emotion；有确认 cast 时 `normalize_speakers` 别名归一（:940-947），认不出即清空留 speaker_raw |
| P6 assign | :954 | 三层优先级：①single→default_voice ②multi+cast→`assign_with_cast`（角色级 speed/pitch/gain_db）③multi 无 cast→名表查 speaker 未命中归旁白（PY-113）；④`_overlay_segment_assignments`（:181）用 voice_assignments 字符区间覆盖（PY-114，start==0&end==0 视为整章）；cast 门禁 `_cast_meta_present`（:120）不过则逐字保持旧行为 |
| P7 synthesize | :1133 | 句级引擎路由 + 三路径（见下表）；模型获取 `_acquire_engine`（:1074-1131）：预热槽位幂等 + 等**本引擎槽**终态 + 断言 `snap.engine==engine_name`（US-C1 防假 ready），每 5s 发 loading_model 心跳 |
| P8 postprocess | :1478 | 去静音→峰值门限剔除（`SILENCE_PEAK_THRESHOLD=1e-3`）→归一→淡入淡出；有 `global_settings.mix` 才走 `postprocess_with_mix`（BGM 铺底+闪避→SFX→角色/主增益→软限幅+LUFS），无配置**完全直通**；两种都写 task_log 留痕（WF-13） |
| P9 package | :1603 | 拼接(gap 300ms)→**能量守门**（overall_peak + active_ratio<0.01 即 raise，:1655-1670）→**全局唯一一次**归一到 peak 0.891(-1dBFS)（:1672-1686，PY-128）→按格式写盘（:1723-1748）→返回 format_used/chapter_timeline_ms；M4B 走章节轨+ftyp 校验（`m4b_builder.py`） |

**P7 三路径**：

| 路径 | 条件 | 机制 | 实测 |
|---|---|---|---|
| A 在线并发 | 全引擎 online 且无 gsv | 块内 `asyncio.gather`，并发 `PYY_EDGE_CONCURRENCY`(默认6≤16) | RTF 0.10 |
| B gsv 分组 | 含 gptsovits | 按 (chapter,voice) 稳定分组背靠背、组首预热 ref、乱序执行按 index 归位（:1443-1444）、换 ref 次数入账 task_log | 量化省 · `count_gsv_reference_switches`(:87) |
| C 顺序 | 本地引擎且无 gsv | 逐句 executor，每 10 句让出事件循环 | — |

## 7. TTS 引擎层

### 7.1 十引擎矩阵（注册表 `engine_registry.py:67-124`，全 lazy import，缺依赖只 warning）

档位（指令#1）：`online` 档默认 `edgetts`、`local` 档默认 `voxcpm`，口径唯一入口 `modules/tts_engine/tier.py`；azure/minimax 是**付费端口**，在 `UNVERIFIED_CLOUD_ENGINES` 名单里 —— 无 Key 时不会被选为任何档的默认，也不会自动升档。

| 引擎名 | 采样率 | 克隆 | 设计 | 流式 | 情感 | seed | online | 定位 |
|---|---|---|---|---|---|---|---|---|
| voxcpm | 48000 | ✓ | ✓ | ✓ | ✓ | ✓ | ✗ | **本地档默认**（`DEFAULT_LOCAL_ENGINE`；VoxCPM2 vendored 于 `modules/tts_engine/voxcpm/`，权重 `data/models/voxcpm2`，30 语言；实测装载 ~141.6s CPU / 常驻 ~5.2GB、合成 ~51.4s 机器时间/秒音频） |
| edgetts | 24000 | ✗ | ✗ | ✗ | ✗ | ✗ | ✓ | **在线档默认**（`DEFAULT_ONLINE_ENGINE`，PY-116 + 指令#1，零权重，12 内置中文音色，重试退避 1/3/6s；实测 0.3s/句、88字4句端到端 4.1s） |
| gptsovits | 32000 | ✓ | ✗ | ✗ | ✗ | — | ✓ | 质量克隆档（HTTP 客户端→`127.0.0.1:9880`，`PEIYINYUAN_GSV_URL` 可覆盖；类级串行锁；payload 白名单 `TTS_PARAM_SPECS`；text_lang 自测 US-B1；cut0+fragment_interval 0.02 US-B2） |
| openvoice | 22050 | ✓ | ✗ | ✗ | ✗ | ✓ | ✗ | 本地即时克隆档（#76 落地=OpenVoice v1，MIT 278MB，**克隆不需转写**；`PEIYINYUAN_OPENVOICE_THREADS` 默认 2——12 线程实测 RTF 抖到 12.35；peak 三段治理） |
| indextts | 24000 | ✓ | ✗ | ✗ | ✓8维 | ✗ | ✗ | IndexTTS-2 |
| mosstts | 24000 | ✓ | ✗ | ✗ | ✓prompt | ✗ | ✗ | MOSS-TTS |
| kokoro | 24000 | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | 82M 轻量多语档（**中文实测** PY-367：`zf_xiaoxiao/zf_xiaoni/zm_yunjian` 三枚各出声 5.475/5.7/5.6s，peak 0.5036/0.529/0.4521，`all_zero=false`；权重 315MB 已在 HF 缓存，`huggingface.co` 本机直连不通、须 `HF_ENDPOINT=https://hf-mirror.com` 首次取件；中文支路要 `misaki[zh]`，只装 `kokoro` 一枚会在合成期 `ModuleNotFoundError: ordered_set`） |
| sample | 48000 | ✗ | ✗ | ✗ | ✗ | ✗ | ✓ | **样本库直插**（VA-4：五类非人声音色绑定句直接输出主样本 wav，speed/pitch/emotion 有意忽略） |
| azure | 24000 | ✗ | ✗ | — | ✓style | ✗ | ✓ | **云端付费·端口**（指令#3：`AZURE_SPEECH_KEY`，缺 Key 时 `unavailable_reason()` 直说且一帧不发；`verified_note`=端点路由 09-24 无 Key 实测 401 可达，**请求体形态未实测** → 在 `UNVERIFIED_CLOUD_ENGINES` 名单里，永不自动升为默认） |
| minimax | 32000 | ✗ | ✗ | — | ✓ | ✗ | ✓ | 同上（`MINIMAX_API_KEY`；无 Key POST `/v1/t2a_v2` 实测回 1004 提示要 Key；解码侧 hex/base64/URL 三态容错） |

契约基类 `engines/base_tts.py:11-204`：`load_model/synthesize/synthesize_stream/synthesize_batch/clone_voice/design_voice/list_voices/get_info`；seed 契约 `_reject_seed`(:40-50)——不支持引擎收到 seed 显式拒绝而非忽略；`requires_model_object` PY-304 就绪判据(:95-105)；引擎名白名单 `known_engine_names`(:243)（US-C2，写库前校验）。

### 7.2 引擎选择三条独立链
0. **档位（指令#1，"这一本走在线还是本地"）**：`modules/tts_engine/tier.py` 是唯一口径源，settings 里的 `tier` 决定 `default_engine(tier)`（`online`→edgetts / `local`→voxcpm）。落点：启动预热（`preload_local_at_start`，在线档开机不再装载 ~5GB 权重）、任务 prewarm（跳过在线引擎）、P4 分句策略、P7 句级路由、项目设置白名单、`POST /tasks/convert` 边界 422、模板运行/创建。在线档下若某枚音色绑的是本地引擎，P7 会改由在线引擎发声并**计入 `tier_voice_downgraded` + 写进任务日志**（音色身份未保留，需要原嗓子就切本地档）。
1. **音色行→引擎**（"这句谁发声"）：唯一入口 `resolve_profile_conditioning`（`voice_manager/conditioning.py:155-235`，P0-1 把四处分散的"读音色拼条件"收口为纯函数；优先级 overrides>layers>meta>行字段>defaults>内置；speed∈[0.5,2.0]、pitch∈[-12,+12]）。
2. **参数叠加**：`cast_pipeline_hooks.py:329 synth_params_for_sentence`，句级>全局。
**`Config.TTS_ENGINE` 只是全局默认**（由 `derive_tier_and_engine` 从 `TTS_TIER`+`TTS_ENGINE` 两个环境变量推出，只设引擎名时档位由该引擎的在线性反推），不决定实际路由——别指望改配置换引擎。

### 7.3 预热与缓存
- `model_preloader`：per-engine EngineSlot 状态机（idle/loading/ready/failed）+ **启动即无条件预装载**（#86 ENG-PRELOAD）。门限口径：`PYY_PRELOAD_MIN_FREE_GB` **未设置即不拦**（旧默认 6.0GB 已废，改为 `_LEGACY_DEFAULT_GATE_GB` 仅用于"解释"）；只有用户显式设该变量、或显式 `PYY_PRELOAD_MODEL=0`，才会跳过。装载失败按 `RETRY_LIMIT=2` 带上限重试（退避 `RETRY_DELAY_S=10s`），终态写真实异常原文。槽位载荷除旧 7 键外新增 `attempts / retry_limit / gate`（`gate` 记 `applied / min_free_gb / source / free_gb / free_gb_after`，供 `/api/system/model_status` 诚实地说清"这次到底有没有被门限拦、拦的理由是什么"）。环境变量在**调用期**读取，不再 import 期定死（`main.py` 启动钩子）。
- `clone_prompt_cache.py`（335 行）：克隆 prompt 特征 LRU + npz 落盘（`data/cache/clone_prompt/`，P0-2）。
- `ref_fitting.py`：参考音频按引擎能量窗裁剪（US-C3）。
- `audio_quality.py`：质量双门 + `synthesize_with_gate` 重试（P0-4）。

### 7.4 音频链
`ffmpeg_utils.py:76-124` 三级兜底探测（PATH→imageio-ffmpeg 静态二进制→报 503）+ `run_ffmpeg` 统一错误语义（PY-302）；`m4b_builder.py`（317 行）章节轨+封装+ftyp 校验；`audio_mix.py`（721 行）BGM 铺底/闪避/SFX/角色增益/软限幅/K 加权 LUFS 自实现。

## 8. 多人有声书（cast）子系统

| 层 | 文件 | 关键实现 |
|---|---|---|
| 抽取 | `modules/cast_analyzer/cast_extractor.py`(873 行) | 跨章聚合：对白段:393、说话人解歧:406、代词线索:494-515、最近提及回溯:581-608、人名判定:637、别名合并:737、拒绝规则:810、旁白候选:846 |
| 画像 | `cast_profiler.py` | LLM 批量:146 + **无 LLM 兜底画像**:227（`_DisabledLLM` 降级） |
| 匹配 | `voice_matcher.py` | 四维打分 gender40/age30/style20/lang10 + 同义词表:35-47；旁白固定画像:102-105；`auto_assign`:210 **Top1 且 score≥40 才自动**，否则留白让用户选（不硬塞不合格音色） |
| 编排 | `backend/services/cast_service.py`(955 行) | start_analysis 后台任务化、别名表落 `project.extra_data.cast`、进度走 /tasks 通道 |
| 管线钩子 | `cast_pipeline_hooks.py`(494 行) | P5 normalize_speakers / P6 assign_with_cast:232 / P7 synth_params / P8 postprocess_with_mix:384 |
| 前端 | `CastWizard.tsx`(821 行)+RoleCardGrid+MixPanel+VirtualList | 向导式：分析→画像→推荐→确认 |

## 9. 文本与解析层

- 六解析器 + `ParserFactory`（`parser_factory.py:46-55`）：epub(ebooklib+XHTML 回退)/mobi+azw3(自研解包含 DRM 判定)/pdf(pymupdf，必要时 MinerU)/txt(chardet 嗅探)/docx(mineru office 通路)/html(bs4)。
- 章节检测 `chapter_detector.py`：正则+数字标题连续段+目录块剥离+版权页识别；LLM 可选支路（`/api/llm/detect-chapters`）。
- 清洗 `text_cleaner.py`：页码/水印/广告三级判定。
- 分句 `sentence_splitter.py`：SplitPolicy 五参数对象（DEFAULT/GPTSOVITS 两策略），引号占位符 `__QUOTE_n__` 有已知低估（PY-316 OPEN）。
- 对白 `dialogue_detector.py` / 语气 `tone_detector.py` / 多音字 pypinyin / 数字 cn2an。
- MinerU 路由：`mineru_tools/`（pdf_classifier 决定直解 vs MinerU；`mineru_subprocess.py` 支持本地 start/stop 与容器两条路）。

## 10. 前端

React 18.3.1 + TS 5.5.3 + Vite 5.4.0 + react-router 6.26 + zustand 4.5 + axios 1.7 + animejs + lucide-react（`frontend/package.json`）。src 55 文件/16,151 行：9 路由页（最大 DubbingPage 1015 行、CastWizard 821）+ `ui/` 设计系统 11 件（**页面只准从 `@/components/ui` 引入**）+ `common/` 过渡遗留层（待删）。

- **设计系统（UI-100）**：`styles/tokens.css` 唯一令牌源（:root 浅色 + `[data-theme=dark]`，Linear/Vercel 色板）；层叠 tokens→base→components→layout→pages；三档响应式（768/767.98/1280）；`prefers-reduced-motion` 支持。遗留缺口：DubbingPage/CastWizard 未全量 i18n（UI-01）、pages.css 旧断点未收敛（UI-03）——见 `frontend/UI-100-STATUS.md`。
- **i18n**：自研运行时 `src/i18n/index.tsx`（t/tOr/tList/tEnum + Intl format），zh/en 双语，缺键回退链 当前语言→zh→键名；枚举机器码经 tEnum 不外泄。
- **状态**：prefsStore（主题/语言/侧栏/**配音模式 ttsTier**，localStorage `pyy.prefs`）vs appStore（backendReady/gpuAvailable/ttsEngine/**ttsTier/ttsModes**——`ttsModes` 是 `GET /api/system/engines` 一次开机探到的引擎清单，不进 30s 轮询）。档位的**用户偏好**在 prefsStore（ConvertPage + 页脚用它），**项目档位**在 `dubbing_projects.global_settings.tier`（DubbingPage 用它，切档即落库，免得同页试听读到的还是旧档）——两条不互相回写。
- **API client `services/api.ts`(511 行) 三条契约不变量**：① `voiceApi.listAll` total 感知翻页（后端 limit≤500）；② `slotFor()` 按引擎查 model slots（顶层主槽"轮流说谎"，L14）；③ `SEED_HONORING_ENGINES={voxcpm,openvoice}` seed 白名单门 + `castApi.previewWithMeta` 用原生 fetch 读 `X-Last-Successful-Seed` 响应头。错误扁平化 `detailText()`。
- **WS**：`hooks/useWebSocket.ts`，终态自动关闭。
- **vite 拓扑与已知环境约束**：`vite.config.ts` 仅 port 5173 + `/api→http://localhost:8000`(ws:true)；**未绑 host**——dev 只听 localhost（IPv6 ::1），故外部探活必须用 `localhost:5173`，用 `127.0.0.1` 会误判宕机并催生孤儿 5174 实例；proxy target 写 localhost 而 Node≥17 优先解析 ::1，与后端 0.0.0.0(仅 IPv4) 存在理论双栈坑，运维建议统一 127.0.0.1 显式化。
- 命令：`npm run dev` / `build`(=tsc && vite build, sourcemap 开) / `typecheck` / `lint` / `electron:dev|build`。**前端无自动化测试**（playwright 目录空，vitest 未装）——写测试是待办不是现状。

## 11. 配置与运维

- 唯一配置类 `backend/config.py:11-90`（类属性 + os.getenv，无 pydantic-settings，无 .env 加载调用点）。环境变量一律带默认值，关键：`PEIYINYUAN_HOST/PORT`(0.0.0.0/8000)、**`TTS_TIER(online)` 与 `TTS_ENGINE`（成对，推导口径在 `derive_tier_and_engine`：只设引擎名则档位由该引擎的在线性反推；两者都不设 = 在线档 edgetts）**、**`TTS_PRELOAD_LOCAL`（在线档要把本地模型也开机装载时才设 true；本地档自动预热，不需设）**、`TTS_DEVICE(auto)`、`MAX_UPLOAD_SIZE_MB(500)`、`MAX_BOOK_CHARS(2,000,000)`、`FFMPEG_PATH`、`DEFAULT_AUDIO_FORMAT(m4b)`、`PYY_PRELOAD_MODEL(1)`、`PYY_PRELOAD_MIN_FREE_GB`（**无默认值**：不设置即不启用内存门、启动必预装载；设置才拦，#86）、`PYY_EDGE_CONCURRENCY(6)`、`PYY_STARTUP_RECONCILE(1)`、`PEIYINYUAN_GSV_URL`、`PEIYINYUAN_OPENVOICE_THREADS(2)`、`PYY_CORS_ORIGINS(*时自动关 credentials)`。付费云端档的 Key：`AZURE_SPEECH_KEY` / `MINIMAX_API_KEY`（缺 Key 该引擎 `unavailable_reason()` 直说，且**任何响应/日志都不回显 Key**，只回 `key_present` 布尔）。
- **`PEIYINYUAN_DATA_DIR`（PY-366，可选；默认 = `<repo>/data`）**：整棵 data 根的唯一开关。`Config.DATA_DIR / VOICE_DIR / OUTPUT_DIR / DB_PATH`、`backend/models/base.get_db_path()`、`modules/mixer/bgm_store.DEFAULT_ROOT`（即 `BgmStore()` 的默认根）都走这一个入口。**用途是给门禁/测试**：跑回归门时把它指到一个可写 scratch（Git Bash `export PEIYINYUAN_DATA_DIR=/d/.../scratch`，必须在启动解释器之前设好——`Config` 类体在 import 期求值），这样无论跑多少次都不会在检出树里建 `data/peiyinyuan.db*`、也不会改写受版本控制的 `data/bgm/meta.json`。**不设它时路径与改动前逐字节一致**，生产实例与启动器都不设它，行为不变。**不吃这个开关的两处**（独立验收实测列出）：引擎参考音频目录 `modules/tts_engine/engines/index_tts_adapter.py:343`、`modules/tts_engine/engines/moss_tts_adapter.py:335-339` 仍硬写 `data/voices/{indextts,mosstts}_ref` 并 `mkdir`，`data/models/`、`data/resources/` 也按仓库根算——所以「整棵 data 根可重定向」只对库/音色/输出/bgm 成立，模型权重与引擎参考音频不跟走（好处是设了开关的门禁跑不会去 scratch 找模型；代价是那次运行看到的是空库 23 条内置音色而非生产 1039 条）。
- 端口拓扑：8000 后端 / 5173 前端 dev / 80 Docker 前端 / 6379 Redis / 9880 GSV api_v2 / 8001 MinerU 容器 / 3001 freellmapi。**9881 不是本产品端口**——只是本机 GPT-SoVITS 第二实例的实测端口，代码中仅出现在两条测试头注的"不许连"纪律里。
- 引擎子进程三形态（§3.2/§7.3）：in-process（默认）/ 外部 HTTP（GSV，主程序只当客户端）/ 容器或子进程（MinerU、LLM sidecar）。
- Docker：`docker/docker-compose.yml`；PY-301 教训——compose 相对路径以 `docker/` 为基准，兄弟目录须写 `../../`；freellmapi 只能是可选 profile 不能硬依赖。
- 备份：核心=两个 .db + data/books + data/voices + data/models(可重下)；WAL/SHM 是运行中 SQLite 的组成部分，**服务运行期禁止单独删除**。
- 日志：`PYY_LOG_FILE` 未设时仅 stderr；第三方压 WARNING（`main.py:41-42`）。

## 12. 测试与质量工程（四层金字塔）

1. **单元**：`tests/` 26 文件，静态 460 个 `def test_`（09-22 grep 实测；账本历史值 138/441/500 分别是不同基线的**运行时**计数，不可混引）。约定 `testpaths=["tests"]`——根目录 8 个 `test_*.py` 不被收集。测试隔离契约：不碰权重/:9880/:9881/生产库（头注声明）。
2. **阶段 harness**：`qa/harness/run_p1..p9、run_c1..c6、run_db_schema、run_e1`，20 脚本合计 ~500 check（最大 run_c4_cast_service 1027 行/109 断言）；`harness_lib.Result.check` 语义 + `README-assertions.md` 断言治理（禁恒真、期望取生产单一事实源、修订附不可达性证明+可达性验算+变异三段）。
3. **终局门**：`agent-ops/final-e2e/run_final_e2e.py`（v4，284 行）——独立 :8020 起临时 uvicorn（绝不碰 :8000）→ 导入→九阶段→cast 断言含"林晚晴"→ m4b+wav 双合成→下载解码硬判据（时长>60s、peak∈[0.2,0.99]、clip<0.001、U+FFFD=0、CJK 比≥0.75）→ statistics=200 + books?limit=50 热态<0.5s → **PASS 后自清理本轮四件套并自证书 404**（book_gone；FAIL 保留取证；清理失败判 FAIL/CLEANUP-FAILED）。
4. **常设快检**：管家 cron 每小时复跑 v4 快检防回退（09-22 07:00 起，之前 15 分钟一跑），序号链 #1-#27 无断档全绿；判据=EXIT=0 ∧ verdict=PASS ∧ red_items=[] ∧ cleanup.book_gone=true。

验收基线快照（09-22 00:42 全量 E2E，`agent-ops/final-e2e/out/evidence/FE_final_result.json`）：15/15 硬检 PASS；四章字数守恒 diff=0；m4b 1,952,930B/155.86s/peak0.907/clip0 @48kHz；wav 7,481,372B/155.86s/peak0.891/clip0 @24kHz。

## 13. 已知缺口清单（磁盘坐实，12 条）

| # | 事实 | 证据 |
|---|---|---|
| 1 | ~~README 严重过时（宣称 2 引擎，实际 10；`ssml_generator.py` 不存在；`.chm` 已移除）~~（**09-28 整篇重写**：格式表按 `parsers/` 盘上 7 个文件、引擎按注册表 9+1、安装命令改为 `pip install -e ".[dev,edgetts,voxcpm,codecs]"`、结构树逐目录对过盘、并明确"没有 Redis/Celery 这回事"） | `README.md` vs `engine_registry.py:111-124` |
| 2 | ~~Celery 死轨（全套件在、零调用点）~~（**已摘除**：`backend/tasks/` 不在盘上、compose 无 `celery-worker`、入口无 worker 腿，`celery` 只留 `tools/setup_deps.py:142` 指示器；compose 里的 `redis` 服务同属无调用方的遗留） | `api/tasks.py` 用 asyncio.create_task |
| 3 | `GET /api/workflows/system/status` 三处失真：`from modules.tts_engine.engines import EngineRegistry` 必炸被裸 except 吞→tts_model_loaded 恒 false；`count_tasks(status="waiting")` 非法状态→恒 0；自跑 `subprocess ffmpeg -version` 只看 PATH 重蹈 WF-15 假阴性；psutil/torch 未声明依赖无兜底→缺 torch 时 500 | `backend/api/workflows.py:48-90`（09-22 直读复核坐实） |
| 4 | `POST /dubbing/projects/{id}/preview` 硬编码 `get_loaded("voxcpm")`，不看所配音色引擎（**09-24 指令#1 已修**：改按项目 `global_settings.tier` 取 `default_engine(resolve_tier(...))`，`dubbing.py:137-140`） | `backend/api/dubbing.py:133`（坐实） |
| 5 | ~~空包三处：`modules/dubbing_studio/`、`modules/workflow_engine/`、`backend/utils/`~~（**09-28 已删**：三处都只有 0 字节 `__init__.py`、全仓零代码引用，`git rm` 出库；`代码引用面`由 `tests/test_deps_manifest_aligned.py::test_empty_shell_packages_stay_gone` 钉住） | wc -l 全 0 |
| 6 | 依赖双偏离：torch/psutil/requests 未声明但无条件 import；声明下界与实装差 1-2 大版本 | pyproject vs pip 实测 |
| 7 | ~~`[project.scripts]` 指向 ASGI 对象，CLI 起不了服务~~（**09-28 已删**该 scripts 段：`backend.main:app` 是 ASGI 实例不是 callable，装出来的 `peiyinyuan.exe` 必炸；入口一律走 `一键启动.bat`/`start_all.py`） | `pyproject.toml:116` |
| 8 | `config.py` 注释（引擎枚举/格式列表）落后实现 | :26/:58 |
| 9 | 账本测试数不可沿用（138/441/500 各自基线），静态 460 | 见 §12 |
| 10 | 前端零自动化测试 | `frontend/tests/playwright/` 空 |
| 11 | `qa/harness/tmp/`（含 2f/drift worktree 快照拷贝） | **09-22 07:10 batch1 已删**，195MB；结论留存 `qa/verify-round*.md` |
| 12 | `data/app.db`(0B)/`data/db/` 遗留壳；DB 路径两条并行来源 | **壳已删**；路径双源仍在（§4.3，且均不可 env 覆盖） |
| 13 | 生产库历史引用腐坏：12 本死书带 2,354 章节、`foreign_key_check` 5 行、2 条 `book_id` NULL 的 project、孤儿 `conversion_tasks` 行 | 死书确为 6 月（12 条全 `2026-06-12`，源文件在 `%TEMP%` pytest 目录，1 条还指着老安装路径 `（旧机器上的）配音员\`）。**孤儿 task 行归因更正：实测 created_at 在 09-20/21/22，不是 6 月**。09-22 08:19 按用户裁决**只清 task 行**：62 条中删 61（在线备份 `pre_orphan_purge_*.db` + 61 条 INSERT 回滚 SQL + 变异检验「快照 61→回滚 122、integrity ok」），**留 1 条**（`f32c7c44…` 的 wav 仍在盘，删行会造出新孤儿）。死书/章节/NULL project 与 5 行 FK 违规按裁决保留未动。**第二轮（08:51，用户追加令"清理那两条孤儿任务记录"）**：剩余 2 行逐 id 删净 → `conversion_tasks` 62→**60**、**孤儿 0**（08:52:14 直读，`integrity_check`=ok、FK 违规仍 5）；带产物那行的 `第一章 初入京城_f32c7c44….wav`(1,620,140B) **只移到 `agent-ops/disk-audit/quarantine_orphan_outputs/20260922-085121/` 未删**（同名书在库里有 4 本活行，删文件需另行批准）。回滚件同样三层齐备（在线备份 + 2 条 INSERT + 快照变异检验 60→62 复原，且该检验当场抓出首版回滚 SQL 的列名引号 bug）。`data/output` 余 **30 件音频**全部对应在册 task 行（列目录 31 条＝30 音频 + `.gitkeep`，计数只数音频）。清后复证：E2E v4 @08:52:28 `PASS` 15/15 且跑后 tasks 仍 60、孤儿仍 0；pytest `500 passed, 8 warnings in 78.07s` EXIT=0（`agent-ops/disk-audit/pytest_post_purge2.txt`） |
| 14 | `run_final_e2e.py` 自净化删了 project/book/两条转换 task，**唯独漏掉本轮 `analyze-cast` 那条 task 行** → book 一删它即成孤儿，每跑一轮净增 1 | **09-22 08:33 已修**：cleanup 清单加 `task_cast`，并新增 `cast_task_gone` 自证（仍 200 就记进 errors→翻 FAIL），使该缺口不能无声复发。爬升主力是**管家 15 分钟快检**（库内 `cast_done` 串 created_at 20:12/20:27/20:42…00:07 等间距，约 4 条/小时），非 6 月遗留。**时间戳口径坑**：全库 `created_at = datetime.utcnow`（朴素 UTC），本地=+8h——`bf22d7f4` 的 `00:20:08` 实为 08:20:08 本地，跨日归因必须换算，否则会把今天的行记到昨天。修前盘证：08:19 备份无该 id，08:21 未修版跑后 tasks 61→62。修后两连跑（08:33:30、08:34:24，各 `VERDICT: PASS` 15/15、`task_cast=200`、`cast_task_gone=true`、errors []）：tasks 62→62→62、orphans 2→2、books 89 持平、`integrity_check` ok。回滚：`agent-ops/` 不在 git 内，改动为 3 处纯增量（deletions 列表化 + `if ctid` 追加 + 自证 8 行），逆向删除即复原；`run_final_e2e` 未被 tests/backend/qa 任何文件引用（grep 0 命中），故不涉及 pytest 面 |

用户待决四项：② L9 fal.ai/MiniMax API Key（工单 #74）；③ RTF<2 静息窗复测；④ #3 single_speaker 人耳核验；⑤ `agent-ops/disk-audit/quarantine_orphan_outputs/20260922-085121/第一章 初入京城_f32c7c44….wav`（1.6MB）是否硬删（现仅移出 `data/output`，可原样放回）。本轮新出的两项已于 09-22 08:1x 裁决并执行、08:4x 追加令收尾：**D 表 79MB 全部保留**（"都别删"）、**库内孤儿 task 行两轮清至 0**（见条 13）。清理与修复已落两枚 commit：`6087350`（launcher v2.1）、`e968277`（1,296 个无引用文件删除），未 push。
波次 2/3 backlog：US-A1..A4、P7 seed、cast 400 用例、角色栅格虚拟化；CODE-SLIM #73（根目录一次性诊断脚本已随 B-⑧ 删 28 件，均 git 跟踪可复原；后端死代码收口仍在进行）。

## 14. 仓库与数据资产地图（PYY 全盘；清理前 06:25 实测 11,899MB → 清理后 07:30 复采 **11,194MB**）

两批安全清理（`agent-ops/disk-audit/cleanup_manifest_batch{1,2}.json`，07:10:12 / 07:26:01）合计释放 **≈711MB**；下表为**清理后**体积。

| 目录 | 体积 | 身份 |
|---|---|---|
| `peiyinyuan/` | 7,002MB | 主程序仓（唯一应用 git 仓，master，无 remote） |
| ├ data/models | 5,010MB | TTS 权重（voxcpm2 4,731 + openvoice 279 等，禁动） |
| ├ data/voices | 676MB | 916 件被 voice_profiles 引用（含 450 sample 五类库）；B-① incoming 89 件 staging 已删 |
| ├ data/books | 322MB | 78 本导入书源文件（用户数据，禁动） |
| ├ data/output | 215MB / **31 文件** | 全部 has-task、**孤儿 0**（B-⑨ 删 21 件 47.4MB 并回收同名 task 行） |
| ├ frontend/node_modules | 580MB | 前端依赖（dev 在跑，禁动，本单未碰） |
| ├ qa/ | **3MB** | 仅存 `verify-round*.md` + `harness/*.py` + FINAL-STATUS 台账；harness/tmp 195MB 与 round 证据目录 24.7MB 已删（结论留存于 md） |
| `GPT-SoVITS/` | 4,067MB | 质量克隆档运行时（:9880 在服务，含 .venv 1,572MB + GPT_SoVITS 2,381MB，禁动）；B-⑥ `_dlcache`/`out`/`logs`/`spike_*.py` 16.7MB 已删，`spike_out`/`spike_refs` 各 2MB 留作 D 表候选 |
| `react-bits-main/` | **已删** 62MB | B-③（与 Scrapling 5 + awesome-design 3 同批，删单 66.4MB） |
| `agent-ops/` | 10MB | 管家中枢：LANES.md + final-e2e 驱动 + 盘审产物 |
| `MinerU-master/` | 22MB | PDF 解析侧车源码（MINERU_ROOT 指它，禁动） |
| `VoxCPM-main/` `freellmapi-main/` | 6/5MB | config 默认路径与 docker compose 引用的兄弟目录（禁动） |
| `Scrapling-main/` `awesome-design-md-main/` | **已删** 5/3MB | B-④ 调研参考（无运行时引用） |
| `arch-upgrade/` | 24MB | 四仓架构分析：`repos/` 24MB 浅克隆为 D 表候选，ANALYSIS 报告为在用证据 |
| 顶层散文件 | 0 | PYY 根**无任何散落文件**；原堆在 peiyinyuan/ 根的 28 个一次性诊断脚本已随 B-⑧ 删除（git 跟踪，可 `git checkout` 复原，§13 条 12/#73） |

**09-28 追记（同一次采样的两处收口，体积以当次 `du -sm` 为准）**：

1. 上表 259 行的口径是 09-22 那次清理的快照。当日 07:30 复采全仓 11,194MB，08:03 复采 `peiyinyuan/` 为 **8,000MB**（PYY 合计 12,134MB）——多出的 ≈1GB 全在 `data/`（音色试听产物与导入书增长），代码树本身未涨。
2. PYY 根现只保留**程序运行需要它在那里的目录**：`peiyinyuan` `GPT-SoVITS` `MinerU-master` `VoxCPM-main` `freellmapi-main` `agent-ops`，其余非程序件整体 `mv` 进 `归档-非程序件/`（可逆，移回即复原，未做任何删除）：

| 归档项 | 体积 | 身份（上表对应行） |
|---|---|---|
| `归档-非程序件/wf-test/` | 58MB | WF 审计输入与报告；E2E 实际用的 7 件样书已另收进仓内 `qa/harness/fixtures/wf_inputs/`（69K），故测试不再指它 |
| `归档-非程序件/arch-upgrade/` | 24MB | 四仓架构分析报告 + `repos/` 浅克隆（代码注释里 `WAVE1-C-REPORT.md` 等指此） |
| `归档-非程序件/local-clone/` | 4MB | #76 本地即时克隆 spike 与报告（`openvoice_adapter.py` / `voice_cloner.py` 的实测依据） |
| `归档-非程序件/deadcode-audit/` | 1MB | CODE-SLIM 第一轮删单 |
| `归档-非程序件/vendor-research/` | 1MB | B-② 删 318MB 后的残余报告（上表"已删"指的是克隆体，非报告） |

## 15. 启动路径与故障排查（09-22 实修）

### 15.1 启动入口与实测端口

**唯一入口：`一键启动.bat`（双击即用，`start-all.cmd` 是同一实现的命令行壳，实现在 `start_all.py`）**：它是唯一自带解释器探测与健康门的入口（PY-354）。旧三枚 `start.bat` / `start.ps1` / `start_gui.py` **已于 2026-09-27 下线**（保留多入口本身就是"用户不知道点哪个"的根源），git 历史里还能找回；本节 15.2 的故障表按当时口径书写，保留作历史证据。

| 入口 | 形态 | 起什么 |
|---|---|---|
| `一键启动.bat` / `start-all.cmd` / `start_all.py`（**唯一入口**，三壳一实现） | 全栈主控台 + 健康门 | `:8000` uvicorn + `:5173` vite + `:9880` GSV；逐台探测依赖齐全的解释器；两枚壳都不含第二套启动逻辑，只转发到 `start_all.py` |

- 后端解释器口径唯一：外部脚本取解释器路径一律走 `python start_all.py --print-python`（PY-358），拿不到就拒绝起后端并指路 `tools/setup_deps.py`（09-28 从仓库根移入 `tools/`），绝不回退裸 `python`/裸 `uvicorn`。
- 后端实际绑定 = `start_all.py` 里写死的 `--host 127.0.0.1`（旧入口的 `PYY_BACKEND_HOST` 覆盖能力随之下线）。注意 `backend/config.py` 的 `PEIYINYUAN_HOST` 默认仍是 `0.0.0.0`——只有走 launcher 才享受收紧后的绑定，直接 `uvicorn backend.main:app` 不带你参数时会监听全部网卡。
- 前端 vite 只绑 `localhost`/`::1`：**探活必须 `curl localhost:5173`**，`127.0.0.1:5173` 会假阴性。
- `:9881` 不是产品端口（§11）；`:9880` 是 GPT-SoVITS api_v2 侧车，由邻居目录自持，主程序只做客户端。

### 15.2 本轮"程序无法启动"的四个独立根因（逐条修）

| # | 症状 | 根因 | 处置 |
|---|---|---|---|
| 1 | 打开首页白屏，控制台 `Failed to fetch dynamically imported module: .../src/pages/HomePage.tsx`，该模块 **HTTP 500** | 一个存活 23 小时的 vite dev server，其 **esbuild 子进程已死**（vite 自身输出 `The service is no longer running`）。TSX 转换在 esbuild 里炸，vite 主进程还活着所以端口仍是 200——**端口探活全绿而页面全崩** | 先证伪"是我清理坏的"（`@esbuild/win32-x64` 与 `node_modules` 完好），再对前后端做真实冷启动：vite 609ms 就绪、首页浏览器实点渲染正常（`agent-ops/disk-audit/coldstart_*.log`） |
| 2 | `start.bat` 每次都拉一个立刻崩溃重启的 worker 窗口 | Celery **死轨**：代码里零 `.delay(`/`apply_async`、无 broker，且实装 `redis 8.0.0` 缺 `async_timeout` 导致 worker 连 import 都过不去；任务实际走 `asyncio.create_task`（`api/tasks.py:111`） | 从三个入口整体摘掉 worker 腿（启动/停止/状态/菜单/GUI 卡片），`celery` 仅保留为 `check_environment()` 的依赖指示器 |
| 3 | 后端与前端曾以 `0.0.0.0`/`--host 0.0.0.0` 全网卡监听 | 本机单用户场景无需暴露，且与"CORS `*` 无鉴权"叠加成面（§13 条 5 之外） | 统一改默认回环 + env 覆盖（见 15.1） |
| 4 | `python start_gui.py cli backend` 退出码 2、`invalid choice: 'cli'` | argparse 取参切片错位（`sys.argv[1:]` 把子命令本身喂给了 choices） | 改 `sys.argv[2:]`；随后**用修好的 CLI 真起了一次后端**并 `:8000/api/health` 200，证明 launcher 可用而非只改代码（`launcher_backend.log`） |

### 15.3 运维坑（本轮踩过，写死）

- **杀 `--reload` 的 uvicorn 只杀父进程不够**：子进程会带着继承来的 socket 继续在 `:8000` 服务，`netstat` 仍显示 LISTENING 而你以为已经停了。要么整树杀（`taskkill /pid X /t /f`，v2.1 的 `:kill_port` 已改），要么停完 `curl` 复验。
- 进程管理用 `powershell Stop-Process`（bash 里 `taskkill //PID` 会被判成 UNC 路径）。
- **旧"6.0GB 空闲内存门"已作废（#86，09-22 10:28）**：`PYY_PRELOAD_MIN_FREE_GB` 无默认值，不设置即启动无条件预装载 voxcpm；09-22 10:46 实测 CPU 档冷启动 `load_seconds=122.9`（权重 4.7GB，装载结束 RSS 4.36GB、可用内存 7.26GB→4.57GB）。**别再把"开机 3.xGB 空闲所以跳过预热"当设计行为写进报告**——现在装载失败必须是 `state=failed` + 真实 `error`。若确实要保留门限，显式设该变量即可（尊重用户值）。
- **`--reload` 会打断装载**：uvicorn 的 watchfiles 监听根在 `<工作目录>\PYY`（含 `agent-ops/`），装载途中**任何**智能体往这棵树里写 `.py` 都会触发重启、把 2 分钟量级的冷启动砍在半路，表现为 `/api/health` 30s 不可达、`model_status` 空。长装载请用 `start_all.py --no-reload`（或装载完再 `--status` 复验）。
- 路由探活两个坑：`/api/books` 会 307 跳到 `/api/books/`（curl 须 `-L`）；`/api/books/statistics` 不存在（被 `/api/books/{book_id}` 吃掉，返回"书籍不存在"）。
- 生产库只读检查用绝对路径 URI：`sqlite3.connect('file:D:/程序开发/PYY/peiyinyuan/data/peiyinyuan.db?mode=ro', uri=True)`；相对路径在非项目 cwd 下报 `unable to open database file`。
- `main.py` 启动对账（`PYY_STARTUP_RECONCILE`）会把 running 而进程已亡的任务改成 failed——重启后看到一批 failed 是**对账生效**，不是数据丢了。

### 15.4 冷启动 SOP（判"能不能用"的唯一口径）

```bash
cd <你的工作目录>\PYY\peiyinyuan
python start_all.py                    # 唯一入口（双击 一键启动.bat 等价）；一条命令起后端+前端+GSV
curl -s http://127.0.0.1:8000/api/health  # 200
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:5173/   # 200，注意 localhost
cd ../agent-ops/final-e2e && python run_final_e2e.py              # VERDICT: PASS 15/15
```
端口 200 只证明进程在，**不证明页面能用**（15.2 条 1 的教训）：终判要浏览器实点首页 + 一枚真任务。09-22 08:02 复采：`:8000=200`、`:5173=200`、E2E 两轮 PASS（07:41 / 07:59）、pytest 500 passed（07:47）。

## 附录 A：契约与术语速查

- 九阶段名 = `PipelineStage` 枚举（pipeline.py:18-27）+ `loading_model` 伪阶段；任务六态 queued/running/paused/completed/failed/cancelled（`tasks.py:35-36`）。
- 音色 taxonomy：source×5 / category×5 / prompt_mode×3；`voice_id≡id`（PY-111）；外部音效键 `ext:<table>:<name>`。
- 缺陷编号体系：PY-1xx/2xx/3xx（功能缺陷）、WF-xx（工作流审计）、US-Bx/Cx（升级规格泳道）、P0-x（VoiceStudio 移植机制）、F-R4-x（round-4 修复）、C1..C6/E1（harness 项）。
- 产物命名：`{书名}_{task_uuid}.{ext}`——**最后一个 uuid 才是 task id**（孤儿归因规则）。

## 附录 B：运维自查五板斧

1. 三端口：`curl 127.0.0.1:8000/api/health`、`curl localhost:5173`（**别用 127.0.0.1 探 vite**）、`:9880/docs`（GSV）。
2. 预热态：`/api/system/model_status`（slots 按引擎查）。
3. 库面：`sqlite3 file:...peiyinyuan.db?mode=ro`（**永远只读 URI**）核 books/voices。
4. 快检：`cd agent-ops/final-e2e && python run_final_e2e.py` 四判据。
5. 台账：`agent-ops/LANES.md`（泳道状态机 RUNNING→GATE-CHECK→GATE-PASSED，管家唯一记忆）。
