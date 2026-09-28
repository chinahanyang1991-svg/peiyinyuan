# 配音员 (PeiYinYuan) 程序架构说明

> 产出方式：`architecture-visualization:explore` → system-modeler 路由。
> 配套图源：`peiyinyuan-architecture.dot`（Graphviz，容器+模块层，L2/L3 混合，已按层级标注）。
> 范围：`PYY\peiyinyuan`，当前状态（current-state），全部基于磁盘代码证据。

## 一句话定位

本地优先的 **电子书 → 有声书** 一体化桌面/Web 应用：FastAPI 后端编排多阶段流水线，React/Electron 前端，多引擎 TTS（VoxCPM 为主），SQLite + 本地文件存储。（证据：README.md、backend/main.py、pyproject.toml）

## 分层结构（4 层 + 侧车）

| 层 | 位置 | 职责 | 关键证据 |
|---|---|---|---|
| 前端 | `frontend/` | React 18 + TS + Vite SPA，`src/routes/` 10 个页面组件（9 页 + NotFoundPage）；Zustand 状态；axios API 封装；useWebSocket 进度订阅；Electron 桌面壳 | `src/routes/*.tsx`、`src/services/api.ts`、`src/store/appStore.ts`、`electron/main.js` |
| API 层 | `backend/api/` | 9 个路由组：books / voices / dubbing / tasks / workflows / llm / mineru_parse / memory / cast；REST + WebSocket `/api/tasks/{id}/progress` | `backend/main.py:334-342`、`backend/api/tasks.py` |
| 业务/数据层 | `backend/services/` + `backend/models/` | 9 个 Service（book/voice/voice_online/voice_preview/cast/dubbing/task/memory/output_reconcile）+ SQLAlchemy 模型：Book+Chapter、VoiceProfile、DubbingProject+Role+VoiceAssignment、ConversionTask、WorkflowTemplate、Memory；SQLite `data/peiyinyuan.db` | `backend/models/*.py`、`backend/config.py` |
| 核心引擎 | `modules/` | 可独立复用的 Python 库（进程内调用，非服务）——book_parser / text_processor / tts_engine / voice_manager / cast_analyzer / mixer / memory / llm_client / mineru_tools / workflow | 见下节 |
| 侧车 | `docker/` | freellmapi(LLM 网关)、mineru(PDF 解析)；`redis` 服务是 Celery 时代的遗留，代码里已零调用方（09-28 实测 grep） | `docker/docker-compose.yml` |

## 核心执行链路（有直接证据）

```
POST /api/tasks/convert
  → task_service.create_task (SQLite)
  → asyncio.create_task(run_task)            ← backend/api/tasks.py:143（进程内执行，非 Celery）
  → ConversionWorkflow.run_conversion        ← modules/workflow/conversion_workflow.py:64
  → Pipeline.run 顺序执行 9 阶段:
      import → parse → clean → split → analyze → assign → synthesize → postprocess → package
                                                        ↑ modules/tts_engine（asyncio.to_thread 防阻塞事件循环）
  → ProgressTracker / get_progress
  → WebSocket 循环推送 → frontend useWebSocket
```

暂停/恢复/取消：`asyncio.Event` 原语，在 stage 边界与句间检查点生效（`pipeline.py:60-86`）。

## modules/ 引擎清单

- **workflow**：`pipeline.py`（9 阶段调度）、`conversion_workflow.py`（阶段→模块的真实接线）、`progress_tracker.py`、`cast_pipeline_hooks.py`、`workflow_service.py`。~~`task_queue.py`、`task_scheduler.py`~~ 从未存在于盘上（本文件 09-27 前的旧列举，09-28 按 `ls modules/workflow/` 校正）
- **book_parser**：`parser_factory` + 各格式 parser（epub/mobi(含 azw3)/pdf/txt/md/html/docx 共 7 个 parser 文件）、`chapter_detector`、`metadata_extractor`、`text_cleaner`；~~CHM~~ 无解析器，已移除（PY-102）
- **text_processor**：分句、对话角色识别、语气检测、多音字校正、数字中文化、文本归一化。~~SSML 生成~~ 无 `ssml_generator.py`；SSML 由 `engines/azure_tts_adapter.py` 适配层与 `/api/llm/optimize-ssml` 负责
- **tts_engine**：`engine_registry.py` 线程安全懒加载单例，注册表 10 键 = **voxcpm / kokoro / gptsovits / indextts / mosstts / openvoice / edgetts / azure / minimax** 九枚可发声引擎 + `sample` 回放档（`engines/*_adapter.py` 均存在）；内置 `voxcpm/` 模型源码；`m4b_builder`、`audio_postprocess`、`stream_synthesizer`；模型权重放 `data/models/voxcpm2`
- **voice_manager**：内置音色库、`voice_cloner`、`voice_designer`（自然语言设计）、`voice_store`、`voice_crawler`（HuggingFace/GitHub）
- **llm_client**：`llm_service` + `freellmapi_client`（对接 freellmapi 容器/`../freellmapi-main`）
- **mineru_tools**：MinerU API 客户端/子进程、PDF 分类、Office 解析（对接 `../MinerU-master` / mineru 容器）
- **memory**：transcript_formatter + `backend/api/memory.py`（记忆功能，README 未列，属新增能力）

## 配置与部署

- 单一 `Config` 类，环境变量覆盖（`backend/config.py`）：TTS_ENGINE、GPU auto 检测（torch.cuda）、Redis/Celery URL、爬虫开关、`DATA_LOCAL_ONLY=true` 本地优先隐私默认。
- 三种运行形态：开发（**唯一入口**：双击 `一键启动.bat` / `start-all.cmd`，同一实现 `start_all.py`，一条命令起 uvicorn + vite + GSV）、Docker Compose（7 服务）、Electron 桌面壳。旧三枚入口 `start.bat` / `start.ps1` / `start_gui.py` 已于 2026-09-27 下线，git 历史里还能找回。

## 与 README 的差异（README 略旧）

README 未提及但代码存在：`/api/llm`、`/api/mineru`、`/api/memory` 路由；indextts/mosstts 引擎；MemoryPage/WorkflowPage 页面；mineru Docker 服务。

## 未确认项 / 待验证

1. ~~**Celery 双轨**~~ **已定案：Celery 是死轨**（09-22 实测 grep：全仓零 `delay()`/`apply_async()` 调用点；`backend/tasks/`、compose 的 `celery-worker`、以及三枚旧入口里的 worker 启动腿已于 09-27 整体摘除，`celery` 只作为 `tools/setup_deps.py` 的环境检查指示器保留）。转换任务实际走 `asyncio.create_task`（`backend/api/tasks.py`）。
2. ~~`modules/dubbing_studio/` 仅有 `__init__.py`~~ **09-28 已删**：该目录只有一枚 0 字节 `__init__.py`、全仓零代码引用，`git rm` 出库；编排逻辑实际在 workflow 层。守卫测试 `tests/test_deps_manifest_aligned.py::test_empty_shell_packages_stay_gone`。
3. Electron 是否自动 spawn 后端进程未逐行验证 `electron/main.js`。[I]
4. `srt`/时间轴、ACX 响度标准等导出细节未核查。
