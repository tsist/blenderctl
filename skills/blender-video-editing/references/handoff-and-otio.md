# 合成显式媒体交接与纯剪切 OTIO 往返

剪辑部门负责全片镜头节奏、入出点与声画编排；复杂画面效果交给 [合成部门](https://github.com/tsist/blender-compositing-workflow/blob/main/skills/blender-compositing/SKILL.md)，外部非编软件的纯剪切交换走 Blender 内置 OTIO 通道。两条路线都以清晰的清单和独立重开核对收口。

## 1. 与合成部门的显式媒体往返

剪辑与合成各自维护独立的可编辑 `.blend` 工程，通过序列媒体与交接清单往返，避免跨工程节点直接耦合。

### 送出清单写清五件事

1. **镜头与资源身份**：写明 `shot_id`、候选版本、来源工程路径与 SHA256；把送交序列的每一张图像文件都列进 `resources` 清单。
2. **五套帧号各自标明**：分开记录源帧、剪辑场景帧、交接序列索引、合成场景帧和回流场景帧，保持有理帧率一致。所有区间用左闭右开 `[start, end_exclusive)`，明确标出有效区间与前后手柄帧（handles）。
3. **色彩与透明通道规格**：写清分辨率、色彩空间、工作空间、`view_transform`、位深（8-bit PNG 或 32-bit 线性 EXR）以及 Alpha 关联方式（`STRAIGHT` 或 `PREMUL`）。需要高动态范围线性母版时直接走线性 EXR 路线。
4. **保留完整 handles**：合成部门渲染时覆盖完整 handles 区间，输出到新目录，原片与旧候选保持原样。
5. **声音归属明确**：除非本次交接专门要求处理声音，否则声音保留在剪辑主时间线里按原节奏播放。

### 回流接入与核对

收到合成成品后，按四步回流到剪辑时间线：

1. 核对合成回执与全部回流序列帧的 SHA256。
2. 把完整带 handles 的新序列挂进时间线，显式设置 `source_in` 指向有效区间起点（例如前后各留 2 帧 handles 时设为 `source_in = 2`），从有效帧切入，让后续微调仍有余量。
3. 另起进程冷重开回流后的剪辑工程，确认帧域、`source_in` 与资源闭包一致。
4. 抽看同源代表帧的「源 → 合成 → 回流」三图对照，检查曝光、构图与边缘；带透明通道的画面同步检查 Alpha 数值，再回播上下游切点。

## 2. 纯剪切 OTIO 时间线往返

需要和外部剪辑软件交换时间线结构时，使用公共 CLI 封装的 Blender 5.3 内置 OpenTimelineIO 命令。该通道面向**纯剪切**的受管 `MOVIE` 与 `SOUND` 时间线；字幕、转场特效与变速在 VSE 主工程里继续维护。

### 导出 OTIO

先用 `video describe --operation otio_export --schema` 确认字段，再运行：

```powershell
$ctl = './blenderctl.ps1'

& $ctl --jobs-dir $jobs --compact video otio export $blend `
  --expected-sha256 $sha `
  --resources $resJson `
  --scene VideoCut `
  --workflow-id cut01
```

导出成功后从回执 `artifacts` 取出 `.otio` 文件路径与它的 SHA256，连同媒体清单一起交接。

### 导入 OTIO 到新受管场景

收回外部调整好的纯剪切 `.otio` 文件后，先算文件 SHA256，再导入到**尚未存在的新场景名**中生成新工作副本：

```powershell
$otioSha = (Get-FileHash -LiteralPath $otioPath -Algorithm SHA256).Hash.ToLowerInvariant()

& $ctl --jobs-dir $jobs --compact video otio import $blend `
  --expected-sha256 $sha `
  --resources $resJson `
  --scene VideoCutFromOTIO `
  --workflow-id cut01_otio `
  --otio $otioPath `
  --otio-sha256 $otioSha
```

导入完成后，按常规流程对新生成的候选 `.blend` 跑一次 `video inspect` 核对条带起止帧与源入点，再对照镜头表补齐字幕、转场或混音参数，最后导出审片视频确认切点准确。
