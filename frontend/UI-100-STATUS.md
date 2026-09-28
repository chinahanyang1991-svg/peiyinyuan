# UI-100 前端全面重制 · 收尾报告

日期：2026-09-28（本轮续作，接手段 2 起点）
范围：仅 `frontend/` 目录；未做任何 git 写操作；未触碰 backend/modules/tools/qa。

## 一、各阶段完成度

| 阶段 | 状态 | 说明 |
|---|---|---|
| 1 基线 | 完成 | 接手时 `npx tsc --noEmit` 红：ConvertPage 4 处 TS2345（缺字典键 convert.formatRecommended / notFound / notFoundDesc / noProjects），已在 zh.ts+en.ts 补齐后转绿。 |
| 2 壳层 | 完成（验证前任成果） | AppShell 已接全部 9 条路由：侧栏+移动端抽屉、主题切换(light/dark)、语言切换(zh/en)、后端健康探针；prefsStore 持久化、syncPrefsToDom 预渲染同步。本轮新增文本全部走字典。 |
| 3 页面重制 | 基本完成 | 重做：HomePage、ImportPage、EditPage、LibraryPage、VoiceStudioPage、MemoryPage、WorkflowPage（Library/VoiceStudio 优先）。ConvertPage 前任已迁移，仅做过基线修复。DubbingPage/CastWizard 未重做（见遗留 UI-01/UI-02，按"宁可少做一页"纪律规避回归风险）。每 2-3 页构建一次，全程绿。 |
| 4 响应式+i18n | 完成 | layout.css 断点改为规范档位：抽屉/单栏 <768px、侧栏 >=768px、>=1280 桌面（pages.css 内部网格断点见 UI-03）。所有新页面文本经 t()/tEnum()/tList()/format()，组件内无硬编码中文。乱码自检：U+FFFD 与连续 Ã/æ 序列命中 0。 |
| 5 终验 | 完成 | tsc 0 错误；vite build 成功（9.79s，见摘录）。dev server 端口 5173（沿用已运行实例）。截图 20 张存于 `../qa/round-3-ui100/`。点击走查：语言切换(home-en/voices-en)、主题切换(theme-dark-home/library)、五类筛选(voices-filter-动物/动漫人物/机械数码)、375px 移动端导航（发现抽屉被遮罩挡住的 z-index 缺陷，已修复 layout.css 并复测通过：点选导航后抽屉正常关闭）。 |

## 二、契约保持清单（逐项核过）

- 音色五分类筛选：human/animal/electronic/anime/mechanical ChipButton 组，DOM 验证"动物"筛选返回 31 个真实音色且计数行同步。
- 试听入口：`voiceApi.previewUrl` = `/api/voices/{id}/preview`，整页 100 个 `<audio>` 元素 src 正确（高度 34px 可见）。
- 克隆/设计/在线搜索入口全部保留：clone(Dropzone+transcript)、design(描述生成)、online(关键词 Chip + searchOnline/download)。
- 库页契约：taskApi.list/delete、`/api/tasks/{id}/download` 下载、状态/格式筛选、跳转换页 `/convert/:taskId`。
- 导入契约：bookApi.import 逐文件循环、支持格式过滤、批量进度条、成功后跳 `/edit/:bookId`。
- 编辑契约：章节勾选/重命名/mergeChapters 合并、正文保存重算 total_chars、跳配音 `/dubbing/new?bookId=`。
- 记忆/工作流：`/memory/`、`/memory/search?q=`、POST/DELETE；`/workflows/templates` GET/DELETE。路由表 9 条未改动。
- 枚举机器码不外泄：tEnum 统一翻译 stage/voiceCategory/voiceSource 等后端字符串。

## 三、遗留缺陷

| 编号 | 位置 | 描述 |
|---|---|---|
| UI-01 | `src/routes/DubbingPage.tsx`（整页，约 1665 行） | 未重制。硬编码中文约 140 行（如 :71-77 模式/角色名标签），9 处原生 `alert()`（:328、:347、:358、:377、:395、:406、:419、:437、:439），依赖旧 global.css 过渡层。功能在新壳内可用，多角色编排为早前浏览器轮已验证路径，重写风险大于收益，按纪律留待下轮。 |
| UI-02 | `src/components/cast/CastWizard.tsx` | 未迁移 i18n（硬编码中文约 104 行），:342 使用原生 `window.confirm`，应改 useConfirm。 |
| UI-03 | `src/styles/pages.css` :745、:759、:784、:817 | 页内网格断点为 1200/980/860/640，与规范三档(>=1280 / 768-1279 / <768)不完全对齐；实测表现正常，待统一收敛。 |
| UI-04 | 截图 `dubbing.png` 现象 | dubbing 路由停在"加载项目详情…"且页脚显示"后端未连接"。判定为数据/健康探针问题（该任务 ID 数据缺失 + 探针时序），非本轮 UI 改动所致；UI 层无异常。 |

## 四、build / tsc 输出摘录（第 5 阶段终验，含 z-index 修复后）

```
$ npx tsc --noEmit        # 无输出，退出码 0
$ npm run build
✓ 1619 modules transformed.
dist/index.html                    0.53 kB │ gzip:   0.40 kB
dist/assets/index-DX-XgMJG.css    79.66 kB │ gzip:  14.26 kB
dist/assets/DubbingPage-CMUVTc3a.js 69.48 kB │ gzip:  19.61 kB
dist/assets/index-cNqUDfJJ.js    295.16 kB │ gzip: 102.31 kB
✓ built in 9.79s
```

## 五、截图清单（20 张，`qa/round-3-ui100/`）

home、import、convert、edit、dubbing、library、voices、memory、workflows（1440 宽路由全量）；home-en、voices-en（英文态）；theme-dark-home、theme-dark-library（深色态）；voices-filter-动物、voices-filter-动漫人物、voices-filter-机械数码（五类抽样）；tablet-1024-library；mobile-375-home、mobile-375-drawer、mobile-375-voices。
