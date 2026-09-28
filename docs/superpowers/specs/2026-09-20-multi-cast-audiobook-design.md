# 多人有声书模式设计（cast 分析 / 音色匹配 / BGM 混音）

日期：2026-09-20 ｜ 状态：已批（用户常设全自治授权）｜ 负责人：智能体5（多人有声书开发）
基线：ecdaa23；红线约束沿用 `qa/PROTOCOL.md`（不卡顿 / 不乱码 / 无杂音 / 人声可用）。

## 1. 目标

电子书导入后：

1. **全内容快速解析 → 角色表**：抽取出书中所有人物（姓名、性别、年龄、职业/身份、戏份）与旁白。
2. **多角色音色选择**：程序按角色属性自动推荐匹配音色，用户可逐角色替换（内置 / 克隆样本 / 资源库 resource.db / 上传自定义）。
3. **多人合成**：对白按角色分配音色，旁白单独轨。
4. **背景音乐 + 音效 + 音量调整**：章节级 BGM 铺底（闪避压低）、场景音效点、每角色音量/语速/音调、主音量与响度目标。

非目标（YAGNI）：波形级时间轴编辑器、多轨 DAW、角色情绪自动表演（留接口不做实现）。

## 2. 关键裁定（为什么不全塞进 9 阶段 Pipeline）

9 阶段 Pipeline 是**无人值守一气呵成**的，而音色分配必须有**用户确认门**。因此：

- **角色分析 = 独立后台任务**（cast 任务），发生在转换之前，复用 task 系统 + WebSocket 进度 + pause/cancel。
- **转换 Pipeline 只做两处轻改**：P6 assign 读确认后的角色映射；P8 postprocess 末尾挂 mix 子步骤（无 mix 配置时行为与现状完全一致，保护 single 模式回归）。

## 3. 现有资产与嵌入点

| 现有 | 复用方式 |
|---|---|
| `DubbingProject.mode=single/multi` | multi 即本模式入口，不加新枚举 |
| `Role(name, role_type, voice_id, extra_data JSON)` | 属性/别名/局部混音设置全部存 extra_data，**无 schema 迁移** |
| `VoiceProfile(gender, age_group, style_tags, source)` | 音色匹配打分输入 |
| `DialogueDetector.detect_dialogues/extract_roles` | cast 抽取的对白/归属信号源 |
| P5 `_stage_analyze`（per-sentence speaker） | 保留，speaker 归一化改用 Role 别名表 |
| P6 `_stage_assign`（name→voice 映射） | 增别名匹配 + 未命中归旁白（已有回退） |
| P7 `_stage_synthesize` | speed/pitch 从全局改为 `sent.get("speed", settings…)` 允许句级覆盖 |
| P8 `_stage_postprocess` + `AudioPostProcessor`（resample/concat/padding/normalize） | 混音器底座 |
| `LLMService`（FreeLLMAPIClient, is_ready） | 角色画像推理，不可走规则兜底 |
| DubbingPage + `/api/dubbing/projects/{id}/roles|assign-voice|settings` | UI 与 API 扩展基座 |

## 4. 新增模块

### 4.1 `modules/cast_analyzer/`

- `cast_extractor.py`
  - 输入：清洗后的全书文本（按章）。**流式逐章处理，内存峰值 = 1 章**（红线：不卡顿）。
  - 用 DialogueDetector 逐句取 `role_hint` + 引号归属启发（"XX说" 前后缀），聚合：
    `{name, lines_count, chapters_touched, sample_quotes[:8], co_speakers}`。
  - 噪声过滤：出现 <2 次、纯代词（他/她/它）、长度 >6 字的候选丢弃；别名合并（"老张"≈"张"、"队长"≈职务称呼入 aliases）。
  - 输出 `CastCandidate` 列表（含"旁白"固定项）。纯函数、无 IO，可离线单测。
- `cast_profiler.py`
  - 输入 CastCandidate + 样句 → LLM 推断 `{gender, age_group, identity(职业/身份), personality_tags[]}`。
  - **批量**：每次 ≤20 角色 + 每人 ≤3 条样句，一次调用出 JSON；失败重试 1 次后走 `_fallback_profile`（代词统计→性别；"少年/孩子/老"→年龄段；"队长/老师/医生"词表→身份；其余 unknown）。
  - LLM 不可用（`is_ready()==False`）时整表直接走 fallback，任务不失败。
- `voice_matcher.py`
  - `suggest(cast, voice_profiles) -> {role_name: [voice_id 排序推荐]}`；打分 = 性别匹配(40) + 年龄段(30) + 风格标签重合(20) + 语言(10)；旁白优先沉稳/磁性与 adult 音。
  - 纯函数，可单测。

### 4.2 `modules/mixer/`

- `bgm_store.py`：BGM 库 `data/bgm/`，`meta.json`（曲名、情绪标签、时长、是否可循环、固有响度）。来源：程序自带 ≥3 段 + 用户上传（ffmpeg 转 wav 48k 立体声入库）。与智能体4 的 resource.db 音效表打通读取。
- `audio_mix.py`
  - 输入：P8 输出的旁白/对白段序列（ndarray、统一 sr）+ `MixSettings`。
  - 步骤：① 段间隙按说话人插静音（对白后 250ms / 旁白后 400ms，可配）→ ② concat 成章节音轨 → ③ BGM 循环铺底、RMS 侧链闪避（人声出现时 BGM −8dB，100ms 攻防）→ ④ 音效点（章节头/尾、可配"场景切换"标记）→ ⑤ 角色/主增益 → ⑥ 软限幅（tanh，峰值 ≤ −1dBFS）+ LUFS 归一（目标 −16 LUFS）。
  - **全部 numpy 向量化，禁止逐样本 Python 循环**；BGM 先 resample 到轨道 sr（复用 AudioPostProcessor.resample）。
  - 无 `MixSettings.bgm` 且无 sfx 且各增益=1 时 → 直通（现状行为）。
- `mix_settings` schema（存 `project.global_settings["mix"]`）：
  ```json
  {"bgm": {"ref": "<bgm_id 或绝对路径>", "gain_db": -18, "ducking": true},
   "sfx": {"chapter_marks": true, "items": [{"at": "chapter_start", "file": "..."}]},
   "role_gains": {"<role_id>": {"gain_db": 0, "speed": 1.0, "pitch": 0.0}},
   "master": {"gain_db": 0, "target_lufs": -16}}
  ```

## 5. 数据流

```
导入书籍 → [cast任务] 逐章抽取→LLM画像→写Role表(extra_data)
        → UI 角色卡（属性+推荐音色，用户确认/替换/上传） → PUT assign-voice
转换任务 P1..P4 不变 → P5 analyze(别名归一 speaker)
  → P6 assign(mode=multi: speaker→Role→voice_id, 附句级 speed/pitch)
  → P7 synth(句级参数) → P8 postprocess →(有mix配置)→ AudioMix
  → P9 package(m4b 不变；BGM 已烙进音轨)
```

## 6. API 扩展（backend/api/dubbing.py + cast 路由）

- `POST /projects/{id}/analyze-cast` → 返回 task_id（后台任务；WS 进度沿用 `/api/tasks/{id}/progress`）。
- `GET /projects/{id}/cast` → 角色表（含属性、aliases、样句、计数）。
- `GET /projects/{id}/cast/suggestions` → 每角色推荐音色 Top5。
- `PUT /projects/{id}/cast/confirm` → 落库确认（写 Role + 别名表进 project.extra_data.cast）。
- `PUT /projects/{id}/mix-settings` / `GET /api/bgm` / `POST /api/bgm/upload`。
- 既有 `preview` 扩展：支持按角色试听（同一引擎小样本合成 ≤2 句）。

## 7. 前端（DubbingPage 改造为向导）

步骤条：`选书 → 角色分析(进度条+失败重试) → 角色卡网格(属性徽章 + 音色下拉[推荐置顶] + 试听 + 上传自定义) → 音乐与混音面板(BGM选择/音量滑杆/闪避开关/角色增益表/主音量/LUFS目标) → 生成(沿用现有任务进度)`。
Electron 端上传走现有 voices 上传通道；列表虚拟化防长剧卡顿。

## 8. 红线映射

- **不卡顿**：cast 流式逐章 + 任务线程化（asyncio.to_thread）；混音 numpy 向量化；前端虚拟列表；LLM 批量单次调用。
- **不乱码**：cast 输入即 P3 清洗后文本，复用已修复的 parser，不新增解析路径。
- **无杂音**：统一 sr、软限幅、LUFS 归一、BGM 闪避防掩蔽；验收含削波=0 与总谐波失真代理指标（峰值分布检查）。
- **人声可用**：角色音色一律走 VoiceProfile 实样本（引擎合成），不做假声源。

## 9. 测试策略（qa/harness，智能体3 回环）

- 新增 `run_c1_cast_extract.py`（fixtures 里含对白小说样本：期望 ≥3 角色+旁白，姓名/性别正确率断言）、`run_c2_matcher.py`（离线纯函数）、`run_c3_mix.py`（给定 2 段合成音 + BGM：输出无削波、时长守恒±5%、BGM 存在段与人声段 RMS 差 ≥6dB）、`run_c4_pipeline_regression.py`（single 模式 + 无 mix 配置 → 与 ecdaa23 结果逐段一致）。
- 判定走真实路径（PROTOCOL 硬规则1）：cast 任务经真 task 系统跑，不留 mock 分支计入指标。

## 10. 实施切分与冲突规避

- **批次 A（纯新增文件，可与智能体2缺陷修复并行）**：`modules/cast_analyzer/*`、`modules/mixer/*`、bgm 资源、c1–c3 测试。
- **批次 B（触碰既有文件，须等智能体2对应缺陷 FIXED/VERIFY_PASS 后）**：`conversion_workflow.py`（P5/P6/P7/P8 钩子）、`backend/api/dubbing.py`、`DubbingPage.tsx`、`services/api.ts`。
- 每批次按 PY-/CAST- 编号提交 git，一次提交一件事。

## 11. 已知代价

- LLM 画像质量依赖 FreeLLMAPIClient 配置；fallback 表覆盖率有限（unknown 属性 UI 上显示"待完善"让用户手填，不算失败）。
- BGM 烙进音轨（非独立轨），m4b 内不能再调混音；重调 = 重跑 P8 起。留 `audio_mix` 输入为段序列的接口，未来可加"只重混不重合成"。
- 说话人归属启发在纯对白体（剧本式）文本上召回高、在意识流文本上偏低——由 LLM 画像兜底复核 aliases。
