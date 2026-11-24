# 契约文档、本地 Wiki 与历史证据索引

本页集中收纳剪辑部门的外部文档入口、运行时契约指针、本地知识库条目与阶段性历史验收归档，方便按需追溯。实际作业参数始终以实时执行 `blenderctl.ps1 discover --product video` 与 `video describe` 的输出为准。

## 1. 运行时与公共契约文档

- **公共工作流手册**：`<PROJECT_ROOT>/docs/cli/workflows/physics-and-editing.md`（公共 CLI 宿主入口、当前 0.1.5 分发与历史 0.1.4 分发恢复说明）
- **本地交付总览**：`<PROJECT_ROOT>/docs/video-editing/LOCAL-DELIVERY-20261013.md`
- **模板库交付与安装说明**：
  - `<PROJECT_ROOT>/docs/video-editing/TEMPLATE-LIBRARY-20261013.md`
  - `<PROJECT_ROOT>/runtime/video-editing/template-library/0.1.0/README.md`
- **5.3.0 手调往返与当前分发范围记录**：
  - `work/audit/<batch>/`
  - `work/audit/<batch>/`

## 2. 本地 Wiki 知识库条目

遇到细分边界或需核对底层实测数据时，通过 `wiki_get` 回读对应原始条目，核清适用前提与版本范围：

- `references/blender-video-editing-workflow-research.md`：剪辑工作流前期调研背景
- `procedures/blender-vse-native-work-units.md`：Blender VSE 原生条带、帧区间、变速与缓存失效工作单元实测
- `procedures/blender-vse-packed-font-capacity-and-visible-subtitle-verification.md`：打包字体 64 槽容量约束与烧录字幕显现核验
- `procedures/blender-vse-linear-exr-handoff-and-downscale.md`：线性 EXR 交接与 `transform.filter=BOX` 面积平均缩小实证
- `procedures/blender-vse-hdr-storage-and-replay.md`：HDR10/PQ 临时缓存、磁盘预算与重放核对说明
- `procedures/blender-vse-template-library.md`：通用模板库编译与配方说明
- `procedures/h3-vhs-ffv1-color-and-timing-derivation.md`：特定 FFV1 素材色彩矩阵与时序派生记录

## 3. 历史阶段证据与范围边界归档

以下审计目录保留历史阶段的完整测试回执与范围界定，供回溯或核查旧作业使用：

- **需求与接口冻结**：
  - `work/audit/<batch>/`
  - `work/audit/<batch>/`
  - `work/audit/<batch>/`
- **A 阶段技术资格（基础剪辑、变速、代理、叠化、300 镜结构与规模）**：
  - `work/audit/<batch>/`（含 12 秒有声白膜 `audio-case/REPORT.md`、代理中断恢复 `proxy-recovery/RESULT-FINAL.json`、合成媒体交接 `compositing-handoff/`、300 镜 7200 帧低清输出 `scale-output-v002/RESULT.json`）
- **B 阶段实片与音字修复（真实 AI 短片、5 分钟 SDR、8 秒原创 4K SDR / 1080 HDR）**：
  - `work/audit/<batch>/`（含 48kHz 连续解码整数采样混音 `audio-fix/`、57 条字幕全显修复 `subtitle-repair/` 与 8 秒原创 CG 4K SDR / 1080 HDR `four-k-hdr-v001/full-delivery-v001/VERIFICATION.json`）
- **C 阶段受控长片与本机负载边界**：
  - `work/audit/<batch>/`（30 分钟 / 1080p SDR / 300 逻辑镜 / 43,200 帧受控技术规模与音画同步验证）
  - `work/audit/<batch>/`（长时 4K HDR 本机负载边界记录与低负载维护收尾；HLG、任意外来 VSE 工程接续、音频变速/非零声像及 HDR 设备观看已按用户决定移出当前范围，复杂长片演出待对应素材到位后单独验收）
