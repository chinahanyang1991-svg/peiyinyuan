# tools/voice_library —— 非人声音色采集管线 (VA-3)

把 **animal / electronic / mechanical**（以及补充的 human）公开音频，采集→质检→归一→
注册成 `voice_profiles` 里 `source='sample'` 的音色，给音色库五类筛选提供真实素材。

anime 类**刻意不抓取**真实动画角色音频（版权红线），由主程序用语音设计生成，
理由见 `scout.py::ANIME_NOTE` 与 `REPORT.md`。

## 文件

| 文件 | 作用 |
|---|---|
| `scout.py` | 网络采集器：Wikimedia Commons 检索/许可过滤/下载/预质检，产出 `manifest.json` |
| `ingest.py` | 通用入库器：QC 门 → 48kHz 单声道 PCM16 归一 → `VoiceProfile` 落库（幂等） |
| `selftest.py` | 离线自检：numpy 合成 3 条 wav（含 1 条静音坏样本），断言 2 入库 1 拒收 + 幂等 |
| `verify_library.py` | **入库后全库复检**：逐条核 `source='sample'` 行的主样本+试听文件（存在/可解码/48kHz mono PCM16/0.3s≤dur≤30s/peak∈[0.02,1.0]），不合格即删 DB 行并删其两个文件；默认 dry-run，`--apply` 才真删 |
| `manifest_batch3.json` | 分类树深采（`scout.py --deep`）产出的条目清单 |
| `manifest.json` | 一轮采集产出的条目清单（`ingest.py` 的输入） |
| `scout_report.json` / `ingest_report.json` | 运行留痕：许可跳过分布、网络异常、逐条 QC 指标 |

## 跑一轮

```bash
cd /d/程序开发/PYY/peiyinyuan
export PYTHONPATH=. PYTHONIOENCODING=utf-8        # Windows GBK 终端必须
python tools/voice_library/scout.py --target 12   # 联网采集 -> manifest.json
python tools/voice_library/ingest.py --manifest tools/voice_library/manifest.json           # dry-run
python tools/voice_library/ingest.py --manifest tools/voice_library/manifest.json --apply   # 真入库
python tools/voice_library/verify_library.py --apply  # 入库后全库复检: 删不可用的 sample 行+其文件
python tools/voice_library/selftest.py                                              # 离线自检, 25 项断言
```

**深采模式（分类树遍历）**：`scout.py --deep` 走 Wikimedia Commons 的 CategoryTree，
对 animal/mechanical/electronic 各自配置的根分类向下展开子树、逐子类配额均匀地攒候选，
并在下载前用 `imageinfo` 的 `playtime_seconds`/`size`/许可字段做廉价预筛（明确 too_long
的长录音根本不下，省带宽），只把过资格门的候选送去下载+预质检：

```bash
python tools/voice_library/scout.py --deep --categories animal,electronic,mechanical \
  --target 120 --cap-per-cat 800 --per-cat-ingest-cap 100 \
  --incoming tools/voice_library/incoming_batch3 \
  --out tools/voice_library/manifest_batch3.json \
  --report tools/voice_library/scout_report_batch3.json
python tools/voice_library/ingest.py --manifest tools/voice_library/manifest_batch3.json           # dry-run
python tools/voice_library/ingest.py --manifest tools/voice_library/manifest_batch3.json --apply    # 真入库
```

许可白名单（只收 PD/CC0/CC BY，**CC BY-SA/GFDL 一律拒**）、限速 `>=1.2s/请求` 与检索模式
完全一致；`--deep` 不开时 `scout.py` 的旧 `--target` 检索路径行为不变（向后兼容）。
署名完整性：`imageinfo` 一并取 `Author`/`Credit`，`best_attribution()` 按
`Attribution -> Artist -> Author -> Credit` 取值，因此 CC BY 条目不会缺署名
（Commons 本身标注「no machine-readable author」的 PD 文件仍为空，无署名义务）。
开跑前还会把库内已有 `source='sample'` 的 page_url 播种进去重集合，跨批次重跑同样幂等。

**入库后复检** `verify_library.py`：遍历 `voice_profiles` 全部 `source='sample'` 行，
逐条核主样本+试听文件的存在/可解码/`48kHz 单声道 PCM16`/`0.3s<=dur<=30s`/`peak∈[0.02,1.0]`；
任一不过即判不可用 → 删 DB 行并删其两个文件。默认 dry-run 只报告，加 `--apply` 才真删。

`ingest.py` 不带 `--apply` 只跑质检并打印，**不写盘、不写库**；`--limit N` 限条数，
`--library-root` / `--db` 可把产物改道到临时目录（自检就是这么隔离的，绝不碰生产库）。

幂等口径：默认重复条目 **skip**；`--refresh-existing` 则对已入库键按同一 uuid5 主键
**原地刷新** name/description/style_tags/meta（文件路径同值，不新增文件）——
命名或描述规范升级后回写存量行用它，日常重跑不要用。

## 质检门（全过才入库）

| 门 | 判据 | 拒因码 |
|---|---|---|
| 可解码 | soundfile 直读，失败退回 ffmpeg（PATH 或 imageio-ffmpeg）解临时 wav | `undecodable` |
| 时长 | `0.3s <= frames/sr <= 30s` | `too_short` / `too_long` |
| 非静音 | `peak >= 0.02` | `silent` |
| 体积 | `44B <= size <= 80MB` | `too_small_bytes` / `too_large_bytes` |
| 类别 | 属于五类枚举 | `bad_category` |
| 幂等 | `meta.page_url`（缺失退 `source_url`）已存在 → skip | — |

采样率一律**记录**（`meta.source_sample_rate`），不做"因为采样率低就拒收"的判断。

## 产物布局

```
data/voices/library/<category>/<uuid>.wav   主样本: 48kHz 单声道 PCM16
                                            -> model_path 与 reference_audio_path 同值
data/voices/library/previews/<uuid>.wav     试听副本: <=5s, 取整条 RMS 最大的窗口
                                            -> sample_audio_path
data/voices/library/incoming/               上游原始下载（预质检失败即删，便于复核许可）
```

`uuid = uuid5(NAMESPACE_URL, page_url)`，所以同一上游文件重跑必然落到同一路径同一主键 →
**幂等**由"主键 + meta.url 去重"双重保证。

## 入库字段口径

```
source='sample'  engine='sample'  category=<五类之一>  language=NULL
style_tags=[分类中文标签, 许可标签, '音效库', 采集提示词]
description=<名称>｜<出处>｜<许可>｜<署名>｜QC 指标（时长/峰值/RMS/原采样率->48k）
download_url=<上游直链>   is_downloaded=1
meta={license, attribution, page_url, duration_s, peak, rms, sample_rate,
      source_sample_rate, category, ingested_by:'voice_library_v1', verified:true, ...}
```

坑位记录：`voice_profiles.language` 列带 Python 端默认值 `"zh"`，SQLAlchemy 在 INSERT
时把显式传入的 `None` 也回填成 `'zh'`；样本类音色不是语音，`ingest._write_profile` 在
flush 后必须再 `obj.language=None; flush()` 用 UPDATE 落回 NULL（`selftest` 第 2 组断言
就是守这条的）。

## 合规口径

* 只接受 **CC0 / Public domain / CC BY（无 SA、无 NC）**；`CC BY-SA`、`GFDL`、
  `unknown`、"版权状态不明" 一律**跳过并计数**（`scout_report.json` 的
  `rejected_licenses` / `skipped`）。
* 署名信息（Commons `Artist`/`Attribution`）与文件页 URL 全部落到 `meta`，
  UI 里可直接挂出处与许可。
* 限速 `>=1.2s/请求`，重试 3 次带退避，UA 标明本地工具、个人使用、不再分发。

## 前端/后端接线（本次未改后端）

后端 `GET /voices?category=animal`（`backend/api/voices.py` 的 `category` Query 参数 →
`voice_service.list_voices(category=...)`）与前端 `VoiceStudioPage.tsx` 的类别下拉
都已就绪，`source='sample'` 的行入库后即可被类别筛选出来。这些行是**成品音频样本**，
`engine='sample'` 不在 TTS 引擎枚举里，因此不会被当成可合成音色调用；要用它们驱动
零样本克隆，需要另外把 `reference_audio_path` 交给克隆链路（VA-4 范畴）。
