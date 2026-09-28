# VA-3 音色库采集管线 —— 首轮真实运行报告

- 运行时间：2026-09-21 08:56 ~ 09:26（本地，UTC 00:56 ~ 01:26），Windows + Git Bash
- 上游：Wikimedia Commons MediaWiki API（`list=search filetype:audio` + `prop=imageinfo`）
- 工具：`tools/voice_library/{scout,ingest,selftest}.py`，产物在 `data/voices/library/**`
- 口径：**所有入库数均以 DB 实查为准**（本文末 §6 贴查询结果），网络失败与遗留问题如实记录

## 1. 实际入库数（按类）

本轮 manifest 41 条，全部通过 ingest 质检门并落库（`source='sample'`）：

| 类别 | 本轮 manifest 入库 | DB 现有 sample 行 |
|---|---|---|
| animal 动物 | 12 | 15 |
| electronic 电子 | 12 | 16 |
| mechanical 机械数码 | 12 | 14 |
| human 人声 | 5 | 9 |
| anime 动漫 | 0（按红线不抓取，见 §5） | 0 |
| **合计** | **41** | **54** |

DB 比本轮 manifest 多出的 **13 行**来自同一管线更早的一批（09:06 UTC 前后，另一并发进程
用当时尚未定稿的 43 条 manifest 跑了一次 `--apply`，见 §7-③）。这 13 行同样通过质检、
许可均合规（CC0/PD/CC BY），只是命名用旧规则（名称里嵌检索提示词）且含同一条录音的多余
变体。本轮**未删除任何行**，改用 `ingest --apply --refresh-existing` 把已入库行按同一
uuid5 主键原地刷新为新命名/新描述（`UPD 41 / SAVE 0 / skip 0`：其中 30 行是并发批次的重合行、
11 行是本轮 `--apply` 刚写入的行），主键与文件路径不变、不产生新文件，
最终 41 行的名称/描述与定稿 manifest 完全一致。

音频落盘核验（DB 驱动逐行 `soundfile.info`）：DB 现有 54 行 sample → **规格违规 0 条**；
主样本全部 48kHz / 单声道 / PCM16、`model_path == reference_audio_path`、试听副本全部 ≤5s。
体积：本轮 41 行主样本 38.9 MB + 副本 14.9 MB；含遗留 13 行的 54 行合计 46.4 MB + 18.6 MB；
上游原始下载（`incoming/`）41 个文件 15.5 MB。

## 2. 质检拒绝率与原因分布

三道门，各自的拒绝都留了痕：

**(a) 许可门（下载前，候选条目级）** —— 淘汰 **94** 条：

| 被拒许可 | 条数 | 说明 |
|---|---|---|
| CC BY-SA 4.0 | 53 | ShareAlike 具有衍生作品同许可义务，非任务允许集 |
| CC BY-SA 3.0 | 36 | 同上 |
| CC BY-SA 2.5 | 2 | 同上 |
| GFDL | 1 | 自由但带文档复制义务，不适合当音效素材 |
| Copyrighted free use | 1 | **非自由再分发**，直接拒 |
| PDM-owner | 1 | 版权状态表述不明 → 按"不明即跳过"拒 |

另有前置淘汰：`oversize`（>12MB，多半整曲）37、`same_family_cap`（同一条录音的第 3+ 个变体）4、
`pronunciation_file`（LL-Q 词条读音，不是目标声音）1、`not_free_audio_format`（webm 等不可解码容器）1。

**(b) 下载后预质检门（scout 内，共下载 102 次）** —— **拒 61 / 留 41 = 拒绝率 59.8%**：

| 拒因 | 条数 |
|---|---|
| `too_long`（>30s 长录音/环境声） | 59 |
| `too_short`（<0.3s） | 1 |
| `silent`（peak<0.02） | 1 |

**(c) 入库质检门（ingest，最终真门）** —— 本轮 41 条 **拒 0**（因 (b) 已把坏样本挡在
manifest 之外，这正是分层的意义；(c) 仍逐条独立复测，绝不信任上游结论）。
门本身的有效性由离线自检证明：3 条合成样本（2 好 1 静音）→ 2 入库 1 拒收，
外加 5 条坏样本（too_long / too_short / undecodable / bad_category / no_local_file）
→ **全部拒收、0 入库、库行数不变**。

采样率不做拒收判据、只记录：本轮 41 条来源采样率分布 44100×31、48000×6、8000×3、16000×1，
统一 resample（soxr）到 48000 存主样本。

## 3. 许可分布（本轮入库 41 条，全部可再分发）

| 许可 | 条数 | style_tags 标签 |
|---|---|---|
| Public domain | 14 | 公有领域 |
| CC0 | 13 | 公有领域 |
| CC BY 4.0 | 9 | CCBY音效 |
| CC BY 2.5 | 3 | CCBY音效 |
| CC BY 3.0 | 2 | CCBY音效 |
| 合计 | **41**（PD+CC0 = 27 占 65.9%；CC BY 系 14 占 34.1%） | |

署名与文件页已逐条写入 `meta.attribution` / `meta.page_url`，UI 挂出处即用，无需再回溯。
DB 现有 54 行（含 §7-③ 的 13 行）许可同样全部落在 CC0/PD/CC BY 内。

## 4. 示例条目（5 条，DB 实查）

1. `动物-Meow of a Siamese cat - freemaster2` id=`b8cf877c-bac5-59fd-9c75-57968a8197eb`
   animal / CC0 / 1.517s / peak 1.000 / 44100→48000Hz / 署名 freemaster2
   `data/voices/library/animal/b8cf877c-...wav` + `previews/b8cf877c-...wav`
2. `动物-Guira guira 340477` id=`df022e94-76e1-55b0-8659-cb090bc24daf`
   animal / CC0 / 6.125s / peak 0.366 / 48000Hz / 署名 Tiago Lubiana（咕咕鸟叫）
3. `电子-Laser (Gravity Sound)` id=`e753a936-06c8-545c-b954-dfb5e986412f`
   electronic / CC BY 4.0 / 4.075s / peak 0.586 / 44100→48000Hz / 署名 Gravity Sound
4. `机械数码-Clock ticking` id=`5ea0e9a0-05c4-5777-bde5-d84e3329fc4b`
   mechanical / Public domain / 18.051s / peak 0.362 / 44100→48000Hz / 署名 natalie
5. `人声-Child-implant-users-imitation-of-happy--and-sad-sounding...`
   id=`e45e5dbc-ecac-5ef5-82b8-f1c1e93d4118` human / CC BY 3.0 / 2.561s / peak 1.035 /
   `age_group=child` / 署名 Wang D, Trehub S, Volkova A, van Lieshout P（儿童语音研究语料）

## 5. anime 类：刻意不抓取（已保留入口）

`scout.py::scout_anime()` 是留好的分支入口，但**产出 0 条**，理由（代码内 `ANIME_NOTE` 同步记述）：

- 动画角色台词/声优表演受**著作权 + 表演者权**双重保护，即便标了 CC0 的搬运也几乎必为
  未授权转载，无法在"许可不明即跳过"的纪律下自证合规；
- Commons / OGA 的 PD 池里不存在这类素材，硬抓等于把侵权文件灌进生产音色库；
- 二次元音色的正解是**原创**：由主程序用语音设计（音色描述 + TTS）生成，属于 VA-2 已就绪的
  `source='designed'` 路径，不需要外部录音。

因此 anime 类保持 0 行是设计结果，不是失败。

## 6. DB 查询结果（最终口径）

```sql
SELECT category, COUNT(*) FROM voice_profiles WHERE source='sample' GROUP BY category;
```

```
animal       15
electronic   16
human         9
mechanical   14
TOTAL        54      # 其中 41 = 本轮 manifest 入库；13 = 更早并发批次遗留（见 §7-③）
```

一致性复查（全 54 行）：`language IS NULL` 54/54、`engine='sample'` 54/54、
`model_path == reference_audio_path` 54/54、音频规格违规 0、
`meta.ingested_by='voice_library_v1'` 且 `meta.verified=true` 54/54。

类别筛选链路**实测**（直调 `VoiceService.list_voices(category=...)`，即
`GET /voices?category=` 走的那条路，未改任何后端代码）：

```
category=animal      -> 15 行 (sample 15)
category=electronic  -> 16 行 (sample 16)
category=mechanical  -> 14 行 (sample 14)
category=human       -> 56 行 (sample 9  + 既有 builtin/cloned/designed/downloaded 47)
category=anime       -> 0 行  (见 §5, 设计结果)
```

## 7. 遗留问题（如实清单）

1. **human 类的"老年"缺口没补上。** 目标 3~5 条里只拿到儿童声 4 条（研究语料，`age_group=child`）
   + 1 条无法判定年龄的 CC0 朗读（`Korky Paul audio intro`，`age_group=NULL`）。
   Commons 上"老人声"几乎没有 PD/CC-BY 单独录音，检索到的高相关结果都是
   CC BY-SA 或视频内嵌音轨，被许可门挡掉。**建议**：老年/儿童中文音色改走语音设计
   （VA-4 与 anime 同路径），别指望公开语料。
2. **OpenGameArt 兜底源整轮不可用。** `/api/query/` 与 `/oaupl/v2/assets` 均
   `HTTP 404 text/html`（共 8 条错误，human 类未达 12 条目标时触发过兜底）。
   代码路径已实现并留痕，接口恢复后即可启用；**本轮 41 条 100% 来自 Wikimedia Commons**，
   没有单点冗余。
3. **13 行更早并发批次的遗留行**仍在库中（旧命名、含同录音多余变体，
   以及 1 条明确错标：`机械数码-链锯 LL-Q1860 (eng)-Saschajoon-Chainsaw Man`
   —— 那是英文词条读音，不是电锯声）。现在的 scout 已加 `pronunciation_file` 与
   `same_family_cap` 两道门，同类问题不会再进。**未删除**（不是本轮产物、且属生产表，
   交由主控定夺）；要清库只需按 page_url 反查后删除该 13 行，或
   `DELETE FROM voice_profiles WHERE source='sample' AND name LIKE '%-% %' AND id NOT IN (本轮 41 主键)`。
4. **动物类命名仍是英文上游标题**（如 `动物-Maullido de gata hembra joven`，西语）。
   中文可读名需要一层标题翻译/人工改名，属产品体验问题，不在采集管线职责内。
5. **`engine='sample'` 不在 TTS 引擎枚举内**，所以这些行是"可试听、可按类筛出"的成品素材，
   不能被 `synthesize()` 当引擎音色调用。要把它们变成可用的音色能力，需要把
   `reference_audio_path` 接进零样本克隆链路 —— 即 VA-4 的活。
6. `data/voices/library/incoming/` 保留了 41 个上游原始下载（15.5MB，.gitignore 已覆盖），
   便于许可复核；确认后可整目录删除，不影响已入库行（主样本/副本已另存）。

## 8. 复现命令

```bash
cd /d/程序开发/PYY/peiyinyuan
export PYTHONPATH=. PYTHONIOENCODING=utf-8
python tools/voice_library/scout.py --categories animal,electronic,mechanical,human --target 12
python tools/voice_library/ingest.py --manifest tools/voice_library/manifest.json                  # dry-run
python tools/voice_library/ingest.py --manifest tools/voice_library/manifest.json --apply         # 真入库(幂等)
python tools/voice_library/ingest.py --manifest tools/voice_library/manifest.json --apply --refresh-existing
python tools/voice_library/selftest.py                                                            # 离线自检 25/25 PASS
```

红线自查：本次写入仅限 `tools/voice_library/**`（`ingest.py`/`scout.py`/`manifest.json` 为
跟踪文件的修改，`selftest.py`/`README.md`/`REPORT.md`/三份 run 报告 json 为新增）、
`data/voices/library/**`（音频，.gitignore 覆盖），以及对 `voice_profiles` 中
`source='sample'` 行的 INSERT/UPDATE。`backend/`、`modules/`、`frontend/`、`qa/`
**我一个字节都没写**（这些路径上的 `M` 状态属于同期其他智能体的改动，与本管线无关）。
未执行任何 git 提交/暂存操作，提交由主控统一处理。
