# 上游出处

本目录不是本项目编写的代码，是 [OpenBMB/VoxCPM](https://github.com/OpenBMB/VoxCPM) 仓库
`src/voxcpm/` 的一整份副本，原样内嵌以便本地档引擎直接 import。

- 许可证：Apache-2.0，全文见本目录 [`LICENSE`](LICENSE)（与上游逐字节一致）。
- 内嵌范围：本目录 34 个受版本控制的源码文件，全部与上游同一版本 `VoxCPM-main`
  快照逐字节一致（按去除行尾符后的 md5 比对，34/34 相同，0 处改动）。
- 模型权重不在本仓库内，运行时由 `VOXCPM_MODEL_PATH` 指定或按启动器下载，
  权重同样适用上游 Apache-2.0 条款。
- 上游 README 声明该模型"code and weights under Apache-2.0, free for commercial use"，
  同时提示生产部署需自行做充分测试与安全评估。

若日后升级本目录，请同步替换 `LICENSE` 并重新做一次逐字节比对，不要只改代码留着旧许可证。
