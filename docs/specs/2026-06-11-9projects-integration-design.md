# 配音员 v2.0 — 9大外部项目整合设计文档

**日期**: 2026-06-11  
**版本**: 2.0.0  
**状态**: 待确认

---

## 1. 概述

整合以下9个开源项目到配音员程序中，实现功能升级和版本迭代：

| # | 项目 | 核心价值 | 整合方式 |
|---|------|---------|---------|
| 1 | VoxCPM-main | 2B参数TTS，LoRA微调，CLI批量 | 升级现有adapter |
| 2 | ebook2audiobook | M4B有声书，章节标记，Docker | 升级book_parser+workflow |
| 3 | audiblez | 轻量Kokoro TTS，M4B输出 | 升级kokoro_adapter |
| 4 | Scrapling | 反反爬虫，自适应解析 | 重写voice_crawler |
| 5 | anime-master | JS动画引擎 | 新建前端动画系统 |
| 6 | awesome-design-md | 设计规范令牌 | 升级global.css |
| 7 | react-bits | React动画组件 | 新增前端动画组件 |
| 8 | claude-supermemory | 持久记忆，转录处理 | 新建memory模块 |
| 9 | claude-workflow-studio | 可视化工作流编排 | 升级workflow模块 |

---

## 2. 整体架构

```
┌──────────────────────────────────────────────────────────┐
│                    前端 (React + Vite + Electron)          │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌─────────────┐ │
│  │ 动画系统 │ │ 设计令牌 │ │ 动画组件 │ │ 记忆面板     │ │
│  │(anime)   │ │(design)  │ │(r-bits)  │ │(supermemory)│ │
│  └──────────┘ └──────────┘ └──────────┘ └─────────────┘ │
│  7页面 → 新增: MemoryPage, WorkflowPage                   │
├──────────────────────────────────────────────────────────┤
│                   API 层 (FastAPI, 9个路由)                │
│  books | voices | tasks | dubbing | workflows | memory   │
│  llm   | mineru | system (新增: memory, 升级: workflows)  │
├──────────────────────────────────────────────────────────┤
│                 服务层 (6个Service)                        │
│  book_service | voice_service | task_service              │
│  dubbing_service | memory_service (新增)                 │
├──────────────────────────────────────────────────────────┤
│                 模块层 (11个模块)                          │
│  tts_engine(6引擎) | book_parser(8格式) | voice_manager   │
│  workflow(升级) | memory(新增) | llm_client | mineru      │
│  text_processor | dubbing_studio | workflow_engine        │
├──────────────────────────────────────────────────────────┤
│                 数据层 (SQLite, 6张表)                     │
│  Book | Voice | Task | Project | Memory(新增) | Setting   │
└──────────────────────────────────────────────────────────┘
```

---

## 3. 模块详细设计

### 3.1 TTS引擎升级 (VoxCPM-main + audiblez)

**涉及文件**:
- `modules/tts_engine/engines/voxcpm_adapter.py` — 扩展
- `modules/tts_engine/engines/kokoro_adapter.py` — 扩展
- `modules/tts_engine/batch_synthesizer.py` — 新建
- `modules/tts_engine/m4b_builder.py` — 新建
- `modules/text_processor/text_normalizer.py` — 新建

**VoxCPM 升级点**:

| 功能 | 来源代码 | 实现 |
|------|---------|------|
| LoRA微调 | `voxcpm/core.py` L317-365 | `load_lora()`, `unload_lora()`, `set_lora_enabled()` |
| CLI批量 | `voxcpm/cli.py` | `batch_synthesizer.py` — 批量文本→音频 |
| TextNormalizer | `voxcpm/utils/text_normalize.py` | `text_normalizer.py` — 数字/符号规范化 |
| ZipEnhancer降噪 | `voxcpm/zipenhancer.py` | adapter初始化时加载，synthesize可选降噪 |
| 4种推理模式 | `voxcpm/core.py` L197-313 | 已支持，暴露参数到上层API |

**audiblez/Kokoro 升级点**:

| 功能 | 实现 |
|------|------|
| M4B输出 | 扩展 `audio_postprocess.py`，新增 `to_m4b()` 方法 |
| 性能优化 | 参考 audiblez CUDA配置，优化推理batch size |
| 多语音包 | `voice_store.py` 支持 Kokoro 多语言包管理 |

**M4B构建器** (`modules/tts_engine/m4b_builder.py`):

```python
class M4BBuilder:
    """M4B有声书构建器（参考 ebook2audiobook + audiblez）"""
    
    def build(self, audio_segments: list, metadata: dict, output_path: str) -> str:
        """
        1. 合并WAV音频段 → 单文件
        2. 生成 ffmetadata 章节标记文件
        3. ffmpeg: -i concat.wav -i metadata.txt -c:a aac -b:a 128k output.m4b
        """
    
    def _write_ffmetadata(self, chapters: list, output: str):
        """写入ffmpeg兼容的章节元数据"""
        # ;FFMETADATA1
        # title=书名
        # artist=作者
        # [CHAPTER]
        # TIMEBASE=1/1000
        # START=0
        # END=120000
        # title=第一章 xxx
```

---

### 3.2 电子书处理管道升级 (ebook2audiobook)

**涉及文件**:
- `modules/book_parser/parsers/mobi_parser.py` — 章节拆分
- `modules/book_parser/parsers/epub_parser.py` — 章节拆分
- `modules/book_parser/parsers/html_parser.py` — 新建
- `modules/book_parser/metadata_extractor.py` — 扩展
- `modules/book_parser/parser_factory.py` — 注册HTML
- `docker/Dockerfile.backend` — 升级

**核心变更**:

1. **MOBI/EPUB章节拆分**: 当前整本塞入一个章节，改为按 `<h1>`/`<h2>` 标签拆分
2. **HTML格式支持**: 新增 `html_parser.py`，BeautifulSoup 提取文本
3. **元数据升级**: 从 EPUB 提取封面/作者/ISBN/出版日期
4. **Docker升级**: 预装 mobi/ebooklib/ffmpeg 依赖

**MOBI章节拆分逻辑** (`mobi_parser.py` 新增 `_split_by_headings()`):

```python
def _split_by_headings(self, soup: BeautifulSoup) -> list[dict]:
    """按HTML标题标签拆分章节（参考 ebook2audiobook）"""
    chapters = []
    current_title = "前言"
    current_text = []
    
    for elem in soup.body.descendants if soup.body else []:
        if elem.name in ('h1', 'h2', 'h3') and elem.get_text(strip=True):
            if current_text:
                chapters.append({
                    "title": current_title,
                    "content": '\n'.join(current_text),
                    "char_count": sum(len(t) for t in current_text),
                })
            current_title = elem.get_text(strip=True)
            current_text = []
        elif elem.name is None and elem.strip():
            current_text.append(elem.strip())
    
    if current_text:
        chapters.append({...})
    return chapters
```

**支持的电子书格式**: EPUB, MOBI, AZW3, PDF, TXT, DOCX, HTML (新增) → 8种格式

---

### 3.3 在线音色库爬虫升级 (Scrapling)

**涉及文件**:
- `modules/voice_manager/voice_crawler/hf_crawler.py` — 重写
- `modules/voice_manager/voice_crawler/github_crawler.py` — 升级
- `modules/voice_manager/voice_crawler/adaptive_crawler.py` — 新建

**Scrapling 核心能力映射**:

| Scrapling组件 | 映射到 |
|---------------|--------|
| `spiders/session.py` 会话管理 | `hf_crawler.py` 已有 httpx.Client，升级session复用 |
| `fetchers/stealth_chrome.py` 反反爬 | `adaptive_crawler.py` — 绕过 Cloudflare/速率限制 |
| `parser.py` 自适应解析 | `hf_crawler.py` 的 `_extract_language()` 升级为自适应字段提取 |
| `core/translator.py` 翻译 | 新增：英文模型描述→中文翻译 |

**关键升级**:

1. **反反爬虫**: 升级 `httpx.Client` → 添加 User-Agent 轮换、请求间隔、重试策略
2. **自适应解析**: 当 API 返回结构变化时，自动尝试多种字段路径提取数据
3. **多源搜索**: HF API + GitHub API + ModelScope API 三源并行搜索
4. **模型详情缓存**: 对已获取详情的模型缓存7天，减少API调用

**爬虫架构**:

```python
class AdaptiveCrawler:
    """自适应多源爬虫（参考 Scrapling 架构）"""
    sources = ["huggingface", "github", "modelscope"]
    
    async def search_all(self, query: str, language: str) -> list:
        """并行搜索所有源，合并去重"""
        tasks = [self._search_source(s, query, language) for s in self.sources]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        return self._merge_deduplicate(results)
    
    def _search_source(self, source: str, query: str, language: str):
        """单个源搜索，失败自动降级"""
        try:
            crawler = self._get_crawler(source)
            return crawler.search(query, language)
        except Exception:
            return crawler._fallback_search(query, language)
```

---

### 3.4 工作流引擎升级 (claude-workflow-studio)

**涉及文件**:
- `modules/workflow/conversion_workflow.py` — 扩展
- `modules/workflow/pipeline.py` — 扩展可视化
- `backend/api/workflows.py` — 新增 CRUD
- `backend/models/workflow.py` — 新建（如果不存在）
- `frontend/src/routes/WorkflowPage.tsx` — 新建

**参考 claude-workflow-studio 架构**:

| WorkflowStudio组件 | 映射到 |
|-------------------|--------|
| `models/Workflow.ts` 工作流定义 | `backend/models/workflow.py` |
| `models/Task.ts` 任务定义 | `backend/models/task.py` (已有) |
| `services/WorkflowService.ts` | `modules/workflow/workflow_service.py` (新建) |
| `services/TaskService.ts` | `backend/services/task_service.py` (已有) |
| `routes/workflows.ts` CRUD API | `backend/api/workflows.py` (升级) |

**升级内容**:

1. **工作流CRUD**: 保存/加载/编辑/删除 工作流模板
2. **阶段可视化**: 前端展示9阶段流水线实时进度
3. **工作流变量**: 每个阶段可配置参数（TTS引擎/语速/情感等）
4. **历史记录**: 每次运行的详细日志和产物

**工作流模型**:

```python
# backend/models/workflow.py
class WorkflowTemplate(Base):
    __tablename__ = "workflow_templates"
    id = Column(String, primary_key=True)
    name = Column(String, nullable=False)
    description = Column(String)
    stages = Column(JSON)  # [{"name": "import", "config": {...}}, ...]
    engine = Column(String, default="voxcpm")
    output_format = Column(String, default="m4b")
    created_at = Column(DateTime)
    updated_at = Column(DateTime)
```

**API端点新增**:

```python
# backend/api/workflows.py (升级)
GET    /api/workflows/templates          # 列出工作流模板
POST   /api/workflows/templates          # 创建模板
GET    /api/workflows/templates/{id}     # 获取模板详情
PUT    /api/workflows/templates/{id}     # 更新模板
DELETE /api/workflows/templates/{id}     # 删除模板
POST   /api/workflows/templates/{id}/run # 运行模板
```

---

### 3.5 记忆模块 (claude-supermemory)

**涉及文件**:
- `modules/memory/__init__.py` — 新建
- `modules/memory/memory_service.py` — 新建
- `modules/memory/memory_store.py` — 新建
- `modules/memory/transcript_formatter.py` — 新建
- `backend/models/memory.py` — 新建
- `backend/api/memory.py` — 新建
- `backend/services/memory_service.py` — 新建
- `frontend/src/routes/MemoryPage.tsx` — 新建

**功能设计**:

| 功能 | 来源 | 实现 |
|------|------|------|
| 项目记忆 | `supermemory/save-project-memory.js` | 保存配音项目的配置/音色/设置 |
| 会话记忆 | `supermemory/add-memory.js` | 保存用户操作历史 |
| 记忆搜索 | `supermemory/search-memory.js` | 全文搜索历史记忆 |
| 转录格式化 | `supermemory/transcript-formatter.js` | 语音→结构化文本 |

**数据模型**:

```python
class Memory(Base):
    __tablename__ = "memories"
    id = Column(String, primary_key=True)
    project_id = Column(String, nullable=True)  # 关联项目
    memory_type = Column(String)  # "project_config" | "voice_profile" | "operation_log"
    title = Column(String)
    content = Column(Text)
    tags = Column(JSON)  # ["配音", "中文", "男声"]
    created_at = Column(DateTime)
    updated_at = Column(DateTime)
```

**API端点**:

```python
GET    /api/memory/                    # 列出记忆
POST   /api/memory/                    # 创建记忆
GET    /api/memory/search?q=xxx        # 搜索记忆
GET    /api/memory/{id}                # 获取记忆
DELETE /api/memory/{id}                # 删除记忆
POST   /api/memory/transcript/format   # 转录格式化
```

---

### 3.6 前端升级 (anime-master + react-bits + awesome-design-md)

**涉及文件**:
- `frontend/src/styles/global.css` — 升级设计令牌
- `frontend/src/styles/animations.css` — 新建
- `frontend/src/components/animations/` — 新建
- `frontend/src/components/common/` — 扩展
- `frontend/src/routes/` — 新增2页面

**awesome-design-md 设计令牌升级**:

参考 Stripe/Vercel/Linear 的设计规范，升级CSS变量：

```css
/* 升级后的设计令牌 */
:root {
  /* 间距系统 (参考 Linear) */
  --space-1: 4px;  --space-2: 8px;  --space-3: 12px;
  --space-4: 16px; --space-6: 24px; --space-8: 32px;
  
  /* 字重系统 (参考 Stripe) */
  --font-normal: 400; --font-medium: 500; --font-semibold: 600;
  
  /* 动画缓动 (参考 Vercel) */
  --ease-out: cubic-bezier(0.16, 1, 0.3, 1);
  --ease-in-out: cubic-bezier(0.65, 0, 0.35, 1);
  --duration-fast: 150ms; --duration-normal: 300ms; --duration-slow: 500ms;
  
  /* 阴影层级 (参考 Apple) */
  --shadow-xs: 0 1px 2px rgba(0,0,0,0.04);
  --shadow-sm: 0 1px 3px rgba(0,0,0,0.08);
  --shadow-md: 0 4px 12px rgba(0,0,0,0.1);
  --shadow-lg: 0 8px 24px rgba(0,0,0,0.12);
}
```

**anime-master 动画系统集成**:

```typescript
// frontend/src/components/animations/useAnime.ts
import anime from 'animejs';

export function usePageEnter() {
  // 页面进入动画：淡入 + 上移
  useEffect(() => {
    anime({
      targets: '.page-enter',
      opacity: [0, 1],
      translateY: [20, 0],
      duration: 400,
      easing: 'easeOutCubic',
    });
  }, []);
}

export function useStaggerList(items: any[]) {
  // 列表交错动画
  useEffect(() => {
    anime({
      targets: '.stagger-item',
      opacity: [0, 1],
      translateX: [-10, 0],
      delay: anime.stagger(50),
      duration: 300,
    });
  }, [items]);
}
```

**react-bits 动画组件集成**:

选取最实用的动画组件，精简集成：

| 组件 | 用途 |
|------|------|
| `FadeIn` | 页面/卡片淡入 |
| `SlideUp` | 内容上滑进入 |
| `ScaleIn` | 弹窗缩放进入 |
| `Typewriter` | 文字打字机效果（配音预览） |
| `Pulse` | 按钮/状态指示灯脉冲 |
| `Shimmer` | 加载骨架屏闪光 |

**前端页面变更**:
- 新增: `WorkflowPage.tsx` — 工作流可视化编排
- 新增: `MemoryPage.tsx` — 项目记忆管理
- 升级: 所有7个现有页面应用动画系统

---

### 3.7 完整文件变更清单

```
新增文件 (18个):
  modules/tts_engine/batch_synthesizer.py
  modules/tts_engine/m4b_builder.py
  modules/text_processor/text_normalizer.py
  modules/book_parser/parsers/html_parser.py
  modules/voice_manager/voice_crawler/adaptive_crawler.py
  modules/memory/__init__.py
  modules/memory/memory_service.py
  modules/memory/memory_store.py
  modules/memory/transcript_formatter.py
  backend/models/memory.py
  backend/models/workflow.py
  backend/api/memory.py
  backend/services/memory_service.py
  modules/workflow/workflow_service.py
  frontend/src/styles/animations.css
  frontend/src/components/animations/useAnime.ts
  frontend/src/routes/WorkflowPage.tsx
  frontend/src/routes/MemoryPage.tsx

修改文件 (15个):
  modules/tts_engine/engines/voxcpm_adapter.py
  modules/tts_engine/engines/kokoro_adapter.py
  modules/tts_engine/audio_postprocess.py
  modules/book_parser/parsers/mobi_parser.py
  modules/book_parser/parsers/epub_parser.py
  modules/book_parser/metadata_extractor.py
  modules/book_parser/parser_factory.py
  modules/voice_manager/voice_crawler/hf_crawler.py
  modules/voice_manager/voice_crawler/github_crawler.py
  modules/workflow/conversion_workflow.py
  modules/workflow/pipeline.py
  backend/api/workflows.py
  backend/main.py
  frontend/src/App.tsx
  frontend/src/styles/global.css

删除文件 (1个):
  test_mobi_temp.py (临时测试文件)
```

---

## 4. 测试计划

- 现有 123 个测试需全部通过
- 新增测试:
  - `test_m4b_builder.py` — M4B构建测试
  - `test_memory.py` — 记忆模块 CRUD 测试
  - `test_adaptive_crawler.py` — 爬虫多源测试
  - `test_workflow_template.py` — 工作流模板测试
  - `test_html_parser.py` — HTML解析测试
  - `test_frontend_animations.py` — 前端动画组件渲染测试

---

## 5. 风险与依赖

| 风险 | 缓解措施 |
|------|---------|
| LoRA权重文件不存在 | 降级为普通模式，不影响基础合成 |
| ffmpeg 未安装 | M4B Builder 检测并提示安装 |
| HF API 限流 | 回退到预设模型列表（已有） |
| anime.js 加载失败 | 降级为纯 CSS 动画 |
| 新增模块导入冲突 | 使用 try/except 包裹非关键导入 |

---

## 6. 版本里程碑

| 版本 | 内容 | 预计文件数 |
|------|------|-----------|
| v2.0.0 | TTS引擎升级 + 电子书管道升级 | ~8文件 |
| v2.1.0 | 爬虫升级 + 工作流升级 | ~8文件 |
| v2.2.0 | 记忆模块 + 前端升级 | ~12文件 |
| v2.3.0 | 测试完善 + 文档更新 | ~5文件 |

---

## 7. 确认清单

- [ ] TTS引擎升级方案 (LoRA/CLI/M4B/TextNormalizer)
- [ ] 电子书管道升级 (章节拆分/HTML/元数据/Docker)
- [ ] 爬虫升级 (反反爬/自适应/多源/缓存)
- [ ] 工作流升级 (CRUD/可视化/变量/历史)
- [ ] 记忆模块 (项目记忆/搜索/转录格式化)
- [ ] 前端升级 (动画系统/设计令牌/动画组件)
- [ ] 文件变更清单 (18新增 + 15修改 + 1删除)
- [ ] 测试计划 (6个新测试文件)
- [ ] 风险缓解 (5个风险点)