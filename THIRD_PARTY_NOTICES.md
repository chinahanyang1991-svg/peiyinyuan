# 第三方件与许可证说明

本项目自身代码采用 Apache-2.0（见 [`LICENSE`](LICENSE)）。本文件登记**仓库内内嵌的上游源码**
和**运行期依赖/可选外挂**各自的条款归属，避免把项目许可证误当成覆盖全部内容。

## 一、随仓库一起分发的内嵌上游源码

这两处不是本项目编写的代码，发布时按各自上游条款保留许可证全文。

| 路径 | 上游 | 条款 | 改动情况 |
|---|---|---|---|
| `modules/tts_engine/voxcpm/` | [OpenBMB/VoxCPM](https://github.com/OpenBMB/VoxCPM) `src/voxcpm/` | Apache-2.0（全文在该目录 `LICENSE`） | 34 个受控文件与上游快照逐字节一致（去行尾符后 md5 比对 34/34，0 处改动），见该目录 `UPSTREAM-NOTE.md` |
| `modules/tts_engine/openvoice/` | [myshell-ai/OpenVoice](https://github.com/myshell-ai/OpenVoice) V1 | MIT（全文在该目录 `LICENSE`，`Copyright 2024 MyShell.ai`） | 内嵌使用，另附上游 `UPSTREAM-README.md` |

## 二、pip 依赖中需要留意条款的几枚

| 包 | 实测版本 | 条款 | 用法 |
|---|---|---|---|
| `edge-tts` | 7.2.8 | LGPLv3 | 在线配音档调用微软神经音色；作为独立库导入使用，本项目不修改其源码 |
| `kokoro` | 0.9.4 | Apache-2.0 | 本地轻量配音引擎 |
| `torch` | 2.12.0 | BSD-3-Clause | 本地引擎运行时 |

完整依赖清单以 `pyproject.toml` 为准（分 core 与 `edgetts` / `voxcpm` / `codecs` / `dev` 四组 extras）。

## 三、不在仓库内、按名字去找的同级外挂

程序按目录名去找这些邻居，**本仓库不打包、不复制、不重新分发它们的任何文件**，
谁要用谁自行安装并遵循各自上游条款。缺了只影响对应功能腿，不影响在线配音档出声。

| 目录 | 作用 | 本机实测条款 |
|---|---|---|
| `GPT-SoVITS/` | 零样本克隆档（:9880 侧车） | MIT |
| `MinerU-master/` | PDF 解析侧车 | 本机副本内未见 LICENSE 文件，条款以其上游仓库为准 |
| `VoxCPM-main/` | 本地大模型权重来源 | Apache-2.0 |
| `freellmapi-main/` | 大模型通用接口侧车 | MIT |

## 四、模型权重与音色数据

- 模型权重（`data/models/`）体积以 GB 计，不进版本库，首次使用由启动器按需下载，
  遵循各自模型发布方的条款。
- `data/voices/`、`data/books/`、`data/output/`、`data/*.db` 全部是使用者本地生成的数据，
  不在仓库内；本仓库不内置任何人的录音、任何书籍原文或任何成品音频。
- `tools/voice_library/` 下的 `manifest_*.json` / `gates_*.json` / `verdicts_*.json` 是入库判定台账，
  逐条记录音色的来源与许可证据（`real_speech/license_evidence_*.json`），供合规追溯。

## 五、核实用的命令

```bash
# 内嵌 voxcpm 与上游是否仍逐字节一致（换目录名后自行调整）
git ls-files modules/tts_engine/voxcpm | sed 's|modules/tts_engine/voxcpm/||' | while read -r f; do
  a=modules/tts_engine/voxcpm/"$f"; b=../VoxCPM-main/src/voxcpm/"$f"
  [ "$(tr -d '\r' < "$a" | md5sum)" = "$(tr -d '\r' < "$b" | md5sum)" ] || echo "DIFF $f"
done

# pip 依赖条款实测口径
python -X utf8 -c "import importlib.metadata as m;print([v for k,v in m.metadata('edge-tts').items() if k=='Classifier' and 'License' in v])"
```
