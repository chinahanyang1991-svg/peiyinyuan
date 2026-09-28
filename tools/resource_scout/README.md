# 人声资源库 (tools/resource_scout)

给配音员项目建一个"程序可直接调用"的开放授权人声参考库：
每条样本是一个真实的 5-20 s WAV，可以直接喂给 VoxCPM 的
`generate(reference_wav_path=...)` 做音色克隆。

- 只读生产代码：本目录不改 `backend/ modules/ frontend/ tests/ qa/`。
- 输出物：`data/resource.db`（专用资源库）+ `data/resources/`（WAV + manifest.jsonl）。
- 统计数字全部来自脚本实际输出，落盘在 `tools/resource_scout/logs/`。

## 当前状态（先看这里）

| 项 | 实际值 | 来源 |
|---|---|---|
| 真实入库样本 | 0 | `data/resource.db` 未生成，`data/resources/` 为空 |
| 阻塞原因 | 会话内网络被 OS 级拒绝（hf-mirror / hf / github / openslr / modelscope / librivox） | `BLOCKERS.md` B1 |
| 已建库结构 | 4 张表 + 8 个源的采集配置 + 3 段可复跑脚本 | 本文件下方 |
| 可用清单 | 8 个源 / 计划 245 个音色，全部 CC0 / CC-BY-4.0 / Apache-2.0 | `logs/available_sources.md`（由 `export_manifest.py` 生成） |
| 端到端离线自测 | 11/11 入库、4/4 噪声夹具被拒、verify 100% 通过、注册 11 条 VoiceProfile | `logs/acceptance_selftest.txt`、`logs/acceptance_verify.txt`、`logs/acceptance_import_dryrun.txt` |
| 生产库影响 | `data/peiyinyuan.db` 中 `source='downloaded'` 行数 0 | 注册验证只在 `logs/selftest/prod_copy.db` 副本上 `--apply` |

网络恢复后按「复跑」一节执行即可得到真实库，无需改代码。

## 目录

```
tools/resource_scout/
  build_library.py      采集：探测 -> Range 取样 -> 取窗 -> 重采样 -> 入库
  verify_samples.py     校验：重新打开磁盘字节，量化门禁后才写 verified=1
  import_to_voices.py   注册：resource.db -> voice_profiles（默认 --dry-run）
  selftest_offline.py   离线端到端自测（合成夹具，不进资源库）
  export_manifest.py    导出可用清单 logs/available_sources.{md,json}
  discover_sources.py   只读元数据探活（列仓库/许可/门禁/文件树）
  probe_formats.py      只读探测：真实 parquet 里有哪些列
  rs_net.py             带预算/续传/HTTP Range 的下载层
  rs_db.py              resource.db schema 与 upsert
  rs_audio.py           解码、取窗、重采样、RMS/F0/削波指标、属性归一
  BLOCKERS.md           阻塞与取舍记录
  logs/                 所有统计输出
```

## resource.db 结构

- `resources`  一个上游语料一条：`id, source_key, provider, dataset, source_url, page_url, license, license_url, attribution, languages, harvest_kind, status, note, files_seen, bytes_downloaded, sample_count, discovered_at, last_sync_at`
- `voices`     一个说话人一条：`id, voice_key, resource_id, source_key, speaker_id, name_hint, gender, age_band, language, style, emotion, attr_source, sample_count, verified_count, created_at`
  - `attr_source` 标注属性来自 `metadata` 还是 `acoustic`（声学反推），供智能体2 判断可信度。
- `voice_samples` 一条参考切片：`id, uid, voice_id, resource_id, source_key, speaker_id, gender, age_band, language, style, emotion, path, path_22k, sample_rate, channels, duration_s, rms, peak, noise_floor, voiced_ratio, f0_median, clipping, src_sample_rate, src_format, license, source_url, page_url, manifest_path, verified, verify_note, created_at, verified_at`
- `run_log` 每一次 build/verify/import 的一行流水：`id, ts, script, action, target, status, bytes, note`

任务要求的字段（gender / age_band / language / style / sample_rate / duration_s /
rms / path / license / source_url / verified / created_at）全在 `voice_samples`。

## 落盘格式

每条样本两份 16-bit PCM WAV：

```
data/resources/<source_key>/<speaker_id>/<name>.wav         48 kHz  -> VoiceProfile.reference_audio_path（克隆用）
data/resources/<source_key>/<speaker_id>/<name>_22k.wav     22.05 kHz -> VoiceProfile.sample_audio_path（试听用）
data/resources/manifest.jsonl                                每行一条，含 license 与 source_url
```

## 带宽做法（这是能"少下载"的关键）

parquet 数据集一片动辄 60-500 MB，而一条参考切片只要 5-20 s。
`rs_net.RangeReader` 实现 `io.RawIOBase` 的 seek/read，
让 pyarrow 只按 HTTP `Range` 读一个 row group 的字节（hf-mirror 与
cas-bridge.xethub.hf.co 都返回 `Accept-Ranges: bytes`，实测证据在
`tools/resource_scout/logs/probe/`），于是：

- 一个 row group ≈ 2-25 MB，能拿到该 shard 内几十条候选切片；
- `--budget-mb`（默认 2600）是硬闸门，超了立刻 `BudgetExceeded` 停止，不会无上限爬；
- 每个源另有 `max_file_mb` / `max_rg_mb`，直接跳过巨型 shard 与 `.tar`；
- `kind=files` 的源（`sdialog/voices-libritts`）逐对象取 1.4 MB WAV，最便宜。

## 许可边界

只收录 CC0 / CC-BY(-4.0) / Apache-2.0 / CDLA-permissive；
NC、ND、gated 一律不收。被排除的情感语料与代价见 `BLOCKERS.md` B4。
`manifest` 与 DB 里每条样本都带 `license` + `source_url` + `attribution`，
导出清单在 `logs/available_sources.md`。

## 属性归一（与现有代码对齐）

`rs_audio.py` 把上游五花八门的写法收敛到项目已有词表
（对齐 `backend/models/voice.py` 与 `modules/voice_manager/voice_store.py`）：

- `gender`: `male|female|neutral`（` masculine/female`、`M/F`、`kobieta/mężczyzna` 等都映射；CV 的 `other_or_declining` -> `neutral`）
- `age_band`: `child|young|adult|senior`，再映射到 `VoiceProfile.age_group`
  （child->child，young->young，adult->adult，senior->senior）
- `language`: `zh|en|...`（`cmn_hans_cn`、`zh-CN`、`en_US` 归一）
- `style`: 平静 / 开心 / 悲伤 / 激昂 / 严肃 / 温柔 / 叙述
  —— 优先取上游标签，缺失时用 `estimate_style()` 按能量、语速、F0 方差反推。
- `gender` 缺失时用 `estimate_gender_from_f0()`（自相关基音跟踪）兜底。

风格/性别只是标签；真正决定音色的是 WAV 本身，所以标签错也不会污染合成结果。

## 注册映射（resource.db -> voice_profiles）

`import_to_voices.py` 只做一件事：把 `verified=1` 的样本变成 `VoiceProfile` 行。

| VoiceProfile 字段 | 取值 |
|---|---|
| `id` | `uuid5(namespace, resource_uid)`，重复执行只更新不新增 |
| `source` | `downloaded`（与 builtin/cloned/designed 并存，不覆盖内置音色） |
| `engine` | `voxcpm` |
| `language` / `gender` / `age_group` | 归一后的样本属性 |
| `reference_audio_path` | 48 kHz 主样本（克隆直接吃这个） |
| `sample_audio_path` | 22.05 kHz 试听 |
| `is_downloaded` / `download_url` | `1` / 上游 `source_url` |
| `name` | `开放音色-<性别>-<年龄段>-<风格>-<source_key 首段(<=10)>-<speaker(<=18)>`，截断到 70 字符 |
| `style_tags` | `[风格映射标签(如 平静/沉稳/旁白), 中文|英文, 许可标记(CCBY音色/公有领域/Apache)]` |
| `description` | `开源语料参考音色 (<source_key> / <speaker>)。授权: ...；来源: ...；参考音频 xx.xs @48000Hz，RMS 0.xxxx，峰值 0.xxx。导入时间 ...` |
| `meta` | `resource_uid, source_key, speaker_id, license, source_url, page_url, attribution, duration_s, rms, peak, sample_rate, f0_median, age_band, style, emotion, attr_source, verified_at, imported_by` |

写入用生产 ORM：先把 `backend.models.base.get_db_path` 绑到 `--db` 指定路径，
再按 `VoiceProfile.__table__.columns.keys()` 过滤字段，
所以未来 `voice.py` 加字段不会写脏数据，删字段也不会报错。

## 用法

```bash
export PYTHONIOENCODING=utf-8
cd PYY/peiyinyuan

# 0. 看清单 / 只探元数据
python tools/resource_scout/build_library.py --list-sources
python tools/resource_scout/export_manifest.py
python tools/resource_scout/build_library.py --probe

# 1. 采集（默认 150 个音色、每音色 2 条、预算 2600 MB）
python tools/resource_scout/build_library.py --max-voices 120 --samples-per-voice 2
python tools/resource_scout/build_library.py --dry-run --budget-mb 300      # 只取样不写盘
python tools/resource_scout/build_library.py --sources cv17_zh,fleurs_zh --limit 40

# 2. 校验（唯一能写 verified=1 的地方；不通过只改标记，不删文件）
python tools/resource_scout/verify_samples.py
python tools/resource_scout/verify_samples.py --only-unverified

# 3. 注册（默认 --dry-run，只打印将要写入的 VoiceProfile）
python tools/resource_scout/import_to_voices.py --dry-run --limit 12
python tools/resource_scout/import_to_voices.py --apply            # 写 data/peiyinyuan.db
python tools/resource_scout/import_to_voices.py --apply --gender female --language zh

# 4. 没网时的端到端自测（夹具有人声质感：基频+共振峰+分音节包络）
#    无参数，固定跑 11 正向 + 4 负向夹具，输出到 logs/selftest/
python tools/resource_scout/selftest_offline.py
python tools/resource_scout/verify_samples.py --db tools/resource_scout/logs/selftest/resource.db
python tools/resource_scout/import_to_voices.py --resource-db tools/resource_scout/logs/selftest/resource.db --dry-run
```

离线自测的实际输出：

```
正向样本入库 11/11 | 负向样本被拒 4/4 | F0 性别判定一致 10/11
[verify] 通过 11 / 失败 0 | 库内样本 11, verified=1 11 (100.0%)
[verify] 音色 11 个, 其中拥有 verified 样本的音色 11 个
  gender {'female': 7, 'male': 4}   age_band {'adult': 4, 'child': 2, 'senior': 2, 'young': 3}
  language {'en': 6, 'zh': 5}       style {'叙述': 1, '平静': 7, '开心': 3}
[import] 可注册音色 11 个 -> VoiceProfile (engine=voxcpm source=downloaded)
```

`10/11` 的那 1 例是 `spk_m_child`：男童基音本就接近女声（F0 285.7 Hz），
声学兜底会判成 female；真实采集时 Common Voice / FLEURS 自带 gender 列，
元数据优先，不受此影响。
`悲伤`/`激昂` 在合成夹具上无法复现（纯谐波信号没有情感韵律），
不要据此认为风格链路有问题——它们靠上游标签 + prompt 控制。

## 复跑（网络恢复后，最小成本验证链路）

```bash
python tools/resource_scout/build_library.py --sources voices_libritts --limit 6 --budget-mb 120
python tools/resource_scout/verify_samples.py
python tools/resource_scout/import_to_voices.py --dry-run --limit 6
```

`voices_libritts` 是首选试路源：一个对象 1.4 MB，不需要 Range 解析。
跑通后再 `python tools/resource_scout/build_library.py` 全量扩到 60-150 音色。

## 给智能体2 的接入说明

不需要改任何合成代码：跑一次 `import_to_voices.py --apply`，
新音色就以 `source='downloaded'` 出现在 `voice_store.list(language=..., gender=...)`
的结果里（同一个 `voice_profiles` 表，和内置音色平级），
`casting_service` / `tts_service` 会像用内置音色一样用它们，
VoxCPM 的克隆路径读的就是 `VoiceProfile.reference_audio_path`。
