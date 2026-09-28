# 配音员 (PeiYinYuan)

> 电子书转有声书工作台 — 导入 → 校对 → 定音色 → 合成 → 导出

**使用者请看 [`使用说明.md`](使用说明.md)**（怎么启动、怎么用书、出问题怎么办）。
本文件是给开发/部署看的项目说明。

## 功能特性

- **多格式电子书导入** — EPUB、MOBI、AZW3、PDF、TXT、Markdown、HTML、DOCX（CHM 无解析器，不支持），智能章节检测
- **双档配音模式** — 在线档（默认，微软 Edge 神经音色，单句 1~2.5s，不吃内存，需联网）与本地档（VoxCPM 2B / Kokoro / GPT-SoVITS / IndexTTS / MossTTS / OpenVoice 等，离线、保留克隆原声，约需 5GB 内存）
- **多人有声书编排** — 对白自动分角色、音色-角色-章节分配、语速/音调/情感、BGM 混音
- **音色管理** — 内置+爬取音色库（在册千余条）、自然语言音色设计、零样本克隆
- **文本智能处理** — 多语言分句、对白角色识别、语气检测、多音字校正、数字中文化（SSML 由引擎适配层与 LLM 优化端点负责，不在文本处理模块内）
- **专业导出** — M4B（带章节标记）/ MP3 / WAV / FLAC，元数据嵌入
- **实时进度** — WebSocket 推九阶段流水线进度，支持暂停/继续/取消
- **一键启动器** — `一键启动.bat` 同起后端+前端+GPT-SoVITS，逐腿探测解释器与依赖、过健康门才放行

## 快速开始

### 环境要求

- Python 3.10+（**实测口径：依赖只装在 `C:\Program Files\Python311` 那台**；启动器会逐台探测，缺依赖的解释器直接拒起）
- Node.js 18+
- FFmpeg（mp3/m4b 打包；PATH 里没有时会自动用 `imageio-ffmpeg` 自带的静态可执行文件）
- NVIDIA GPU（可选，仅本地档加速用）

> 没有 Redis/Celery 这回事：整本转换与 API 同进程，走 asyncio，不再有 worker 腿。

### 安装

```bash
cd peiyinyuan

# 后端依赖（这一条与启动器的解释器预检口径一致：在线档 + 本地大模型 + 编解码）
pip install -e ".[dev,edgetts,voxcpm,codecs]"

# 前端
cd frontend && npm install
```

只想装最小可跑集（在线档）：`pip install -e ".[edgetts,codecs]"` —— 这样装本机启动器会因缺 `torch` 拒起后端（引擎注册表在启动期就要 import torch），属于已知设计而不是 bug。

### 启动（唯一入口）

```bash
# Windows：双击仓库根的 一键启动.bat
# 命令行等价：
python start_all.py            # 全腿
python start_all.py --only backend
```

`start_all.py` 是唯一实现，`一键启动.bat` / `start-all.cmd` 只是两枚不含第二套启动逻辑的壳。
起好之后：前端 http://localhost:5173 、后端文档 http://127.0.0.1:8000/api/docs 、GSV http://127.0.0.1:9880/docs 。

> 旧三枚入口 `start.bat` / `start.ps1` / `start_gui.py` 已于 2026-09-27 下线（多入口本身就是"用户不知道点哪个"的根源），git 历史里还能找回。
> 手工分别起前后端属于绕过解释器口径与健康门的调试路径，不是产品入口。

### Docker（实验性）

`docker/docker-compose.yml` 是并行的另一套口径，**不含 GPT-SoVITS 腿**，也不走原生启动器的解释器预检。日常使用请走 `一键启动.bat`；Docker 只在有容器环境时评估。

## 项目结构（与磁盘一致）

```
peiyinyuan/
├── 一键启动.bat / start-all.cmd / start_all.py   # 入口：两壳 + 唯一实现
├── 使用说明.md                                    # 面向使用者
├── backend/                      # FastAPI 后端：路由 + 模型 + 服务
│   ├── main.py                   # 应用装配 + 9 枚路由注册 + 启动对账
│   ├── config.py                 # 全局配置（DATA_DIR/PORT/TIER 均可 env 覆盖）
│   ├── api/                      # books cast dubbing llm memory mineru_parse tasks voices workflows
│   ├── models/                   # book/voice/task/project/memory + base.py（建表 + 轻量列/索引迁移）
│   └── services/                 # book/voice/voice_online/voice_preview/cast/dubbing/task/memory/output_reconcile
├── modules/                      # 能力层（引擎适配、解析、文本、混音、工作流）
│   ├── book_parser/              # 各格式解析 + 章节检测 + 清洗
│   ├── text_processor/           # 分句/对话识别/语气/多音字/数字/归一化
│   ├── tts_engine/               # engines/ 9 枚引擎适配器（+基类/云基类/试听回放）+ 注册表 + 档位 + ffmpeg_utils
│   │   ├── voxcpm/  openvoice/   # vendored 上游模型源码（默认不重构）
│   ├── voice_manager/            # 音色库、存储、克隆、设计、crawler（在线检索下载）
│   ├── cast_analyzer/            # 角色抽取与音色匹配
│   ├── mixer/                    # BGM 与混音
│   ├── memory/                   # 项目记忆
│   ├── llm_client/               # 大模型分析与云端引擎侧车
│   ├── mineru_tools/             # PDF 解析侧车
│   └── workflow/                 # pipeline + conversion_workflow(9 阶段) + 进度/钩子
├── frontend/                     # React + TypeScript + Vite（不是 Vue）
│   └── src/{routes(10 页) components i18n store hooks services lib styles}
├── tests/                        # pytest 76 枚 test_*.py（回归门）
├── qa/                           # 台账 LEDGER.md、issues、harness 探针与夹具
├── tools/                        # 装机脚本 setup_deps.py、音色库脚本、资源检索
├── docs/                         # TECHNICAL.md、architecture/、specs/、使用说明/开发者文档
├── docker/                       # 实验性容器编排
├── data/                         # 【用户数据】peiyinyuan.db、output/ 产物、models/ 权重、backups/
├── logs/                         # 运行日志（gitignore）
└── agent-ops/                    # 排查现场档案（gitignore）
```

数据落点全在 `data/` 一枚目录里，备份拷它即可。

## 工作流架构

转换任务经过 9 阶段 Pipeline：

```
导入 → 解析 → 清洗 → 分句 → 分析 → 分配 → 合成 → 后处理 → 打包
```

- **导入**: 验证书籍存在并加载元数据
- **解析**: 加载已解析的章节内容
- **清洗**: 去除广告/水印/页码/格式化文本
- **分句**: 多语言智能分句（中/英/日/韩）
- **分析**: 对话角色识别 + 语气情感检测
- **分配**: 根据配音项目配置将音色映射到句子
- **合成**: TTS 引擎将文本转为音频（在线档/本地档在此分叉）
- **后处理**: 音量归一化 + 去静音 + 淡入淡出 + BGM 混音
- **打包**: 拼接所有音频输出为有声书文件

## API 概览

| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/health` | GET | 健康检查（含档位、实例身份、db 路径） |
| `/api/system/info` | GET | 系统信息 |
| `/api/books/import` | POST | 导入电子书（流式落盘） |
| `/api/books/` | GET | 书籍列表 |
| `/api/books/{id}/chapters` | GET | 章节列表 |
| `/api/voices/` | GET | 音色列表（支持筛选） |
| `/api/voices/clone` | POST | 语音克隆 |
| `/api/voices/design` | POST | 语音设计 |
| `/api/cast/analyze` | POST | 角色分析 |
| `/api/dubbing/projects` | POST/GET | 配音项目 CRUD |
| `/api/tasks/convert` | POST | 启动转换 |
| `/api/tasks/{id}/progress` | WS | 实时进度推送 |
| `/api/workflows/…` | GET/POST | 模板与模型状态 |

完整 API 文档在 `/api/docs`（Swagger UI）。

## 测试

```bash
set PEIYINYUAN_DATA_DIR=D:\某个空目录     # 把测试写入面隔离出 data/
python -m pytest -q
```

## 许可证

本项目自身代码采用 **Apache-2.0**，全文见 [`LICENSE`](LICENSE)。

仓库内有两处**内嵌的上游源码**，各自保留原许可证、不受本项目许可证覆盖：
`modules/tts_engine/voxcpm/`（OpenBMB VoxCPM，Apache-2.0）与
`modules/tts_engine/openvoice/`（MyShell OpenVoice，MIT）。
依赖条款、同级外挂引擎与模型权重的归属一并登记在
[`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md)。
