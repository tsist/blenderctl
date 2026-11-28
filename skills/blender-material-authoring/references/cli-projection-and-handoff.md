# CLI 作业、高低模投射与现场交接

本页汇总材质部门常用 `blenderctl` 命令、高低模投射烘焙流程、现场窗口手调交接以及长作业监控与恢复做法。全局调用规范见 [blender-cli](../../blender-cli/SKILL.md)，回到主入口见 [SKILL.md](../SKILL.md)。

## 1. 先查契约，再选子命令

动手前先查实时能力与 Schema，全局参数（`--jobs-dir`、`--compact`、`--timeout`、`--async`）放在子命令之前，请求文件使用 UTF-8 JSON 与绝对路径：

```powershell
$ctl = './blenderctl.ps1'
& $ctl discover --product material --actions
& $ctl material describe
& $ctl material describe --operation study --section study
& $ctl material describe --operation procedural-run --schema
```

| 制作场景 | 子命令 | 核心输入与产出 |
|---|---|---|
| 程序化/混合 2.0 组装 | `material procedural-run` | `--expected-sha256`、`--resources`、`--manifest`（可选 `--export`）；产出可编辑主工程与预览 |
| 程序化 2.0 烘焙与重建 | `material procedural-export` | `--expected-sha256`、`--resources`、`--context`、`--target`、`--export`（可选 `--channel-preview`）；产出贴图包与纯贴图重建工程 |
| 静态图层单目标组装 | `material run` | `--expected-sha256`、`--resources`、`--manifest`（或 `--template` + `--bindings`） |
| 多对象/槽/分面批量分配 | `material batch` | `--expected-sha256`、`--manifest`、`--resources`（支持 `--resume`、`--resume-sha256`）；独立失败隔离 |
| 静态图层有限参数对照 | `material study` | `--expected-sha256`、`--manifest`、`--resources`、`--study`（支持 `--resume`、`--resume-sha256`） |
| 导出可复用受管模板 | `material template-save` | `--expected-sha256`、`--material-id`、`--template-id`、`--name`、`--version`、`--resources` |
| 已有材质快速预览 | `material preview` | 在隔离预览场景渲出当前材质外观 |

使用 `--compact` 拿到精简摘要、源 SHA256 与报告路径，完整 JSON 照常留在 `--jobs-dir` 作业目录里供按需回读。

## 2. 程序化 2.0 制作与长作业控制

2K 多通道烘焙耗时较长，按规模显式设置 `--timeout`（如 `--timeout 3600`），或加全局 `--async` 提交后台并用原 `--jobs-dir` 与真实 `$jobId` 做有界等待：

```powershell
$batch = 'work/audit/<batch>/'
$jobs  = Join-Path $batch 'jobs'
New-Item -ItemType Directory -Path $jobs -Force | Out-Null

$src = '<PROJECT_ROOT>\work\prop\source.blend'
$sha = (Get-FileHash -LiteralPath $src -Algorithm SHA256).Hash.ToLowerInvariant()

# 1) 构建可编辑主工程并出预览
$out = & $ctl --jobs-dir $jobs --compact material procedural-run $src `
  --expected-sha256 $sha --resources '<PROJECT_ROOT>\work\prop\resources.json' `
  --manifest '<PROJECT_ROOT>\work\prop\procedural.manifest.json'
$res = $out | ConvertFrom-Json

# 2) 看图确认作者效果后，逐通道烘焙并生成纯贴图重建工程
$authorReport = Get-Content (Join-Path $jobs "$($res.job_id)\procedural-report.json") -Raw | ConvertFrom-Json
$authorFile   = $authorReport.author.file
$authorSha    = $authorReport.author.expected_sha256
$authorRes    = Join-Path $jobs "$($res.job_id)\procedural-resources.json"

& $ctl --jobs-dir $jobs --compact --timeout 3600 material procedural-export $authorFile `
  --expected-sha256 $authorSha --resources $authorRes `
  --context '<PROJECT_ROOT>\work\prop\export-context.json' `
  --target  '<PROJECT_ROOT>\work\prop\export-target.json' `
  --export  '<PROJECT_ROOT>\work\prop\export-settings.json'
```

异步提交后用 `& $ctl --jobs-dir $jobs --compact job wait $jobId --wait-seconds 30`（单次最长 55 秒）跟踪阶段进度，运行中沿用同一个 `$jobId` 继续等待。程序化作业失败后，先读 `procedural-report.json`、`procedural-progress.json` 与日志，定位失败环节后从最近已验证的持久快照新建作业。

## 3. 静态图层 `study` 有限参数对照

静态图层需要在固定机位与灯光下比较少量数值候选时，在 `batch` 基线上点名一个 `assignment_id` 与 `layer_id` 跑 `material study`。当前 `study` 覆盖 7 项数值：

`normal_strength`、`roughness`、`metallic`、`ior`、`coat_weight`、`coat_roughness`、`coat_ior`

- **预算控制**：单次 1–8 个候选（`case`）、每候选最多 6 个视图，总图数控制在 32 张以内。
- **白底观察**：`color_mode = white` 会剥除点名层的 BaseColor 贴图并设为中性灰，方便专看高光与法线起伏。
- **粗糙度贴图调整**：目标已有 Roughness 贴图时，使用 `material run` 或 `material batch` 里的 `adjustments.roughness_scale` / `roughness_bias` 调整贴图区间。
- **断点续跑**：`study` 恢复时传入前次 `study` 报告路径与 SHA256（`--resume` / `--resume-sha256`），复用已完成候选与图像。

## 4. 高低模投射烘焙全流程

需要把高模倒角、细节浮雕与结构遮蔽投射到低模，并驱动棱边磨损与凹处积灰时，按五步串联：

1. **生成高低模配对建议**：
   ```powershell
   & $ctl material projection-suggest $src --expected-sha256 $sha `
     --scene Scene --view-layer ViewLayer --frame 1 --output $suggestedBlend
   ```
   先核对配对清单与低模目标 UV；发现漏烘或错配时用 `material projection-edit-pair` 修正配对。
2. **执行 Cycles 投射烘焙**：先用低分辨率试投射检查命中率，调好笼体与射线距离后再正式出 2K 切线法线（OpenGL `+Y`）、凸曲率与局部 AO：
   ```powershell
   & $ctl --jobs-dir $jobs --compact material projection-bake $src `
     --expected-sha256 $sha --context $ctxJson --pairs $pairsJson
   ```
3. **接入材质**：把烘焙好的三张数据图按 `projection.json` 绑定到工作副本的目标材质：
   ```powershell
   & $ctl material projection-attach $src --expected-sha256 $sha `
     --bindings $bindJson --bindings-sha256 $bindSha --output $attachDir
   ```
4. **独立调磨损与积灰**：复用同一批投射贴图，按需开关并调节磨损（`Show Wear` / `Wear`）与积灰参数，同机位对比凸边和凹处，再换光查法线：
   ```powershell
   & $ctl material projection-effects $attachedBlend --expected-sha256 $attachedSha `
     --bindings $effectsJson --bindings-sha256 $effectsSha --output $effectsDir
   ```
5. **打包可重定位交付包**：校验绑定完整性，导出使用相对路径的 `.blend` 与整套贴图，移动到正式目录冷重开并实际渲染复核：
   ```powershell
   & $ctl material projection-deliver $finalBlend --expected-sha256 $finalSha `
     --bindings $deliverJson --bindings-sha256 $deliverSha --output $deliverDir
   ```

## 5. 接手现场窗口手调与草稿读取

用户在打开的 Blender 窗口里调好了材质层、参数或批量分配时，通过现场会话接口安全收口，保持窗口里的未保存进度完好：

1. **检查与提交现场草稿**：在目标窗口挂载会话目录后，先只读检查，再提交成带 SHA256 的持久化工作副本：
   ```powershell
   # 先核对现场单材质或批量草稿
   & $ctl material handoff-live --session-dir $sDir --session-id $sId --operation inspect

   # 提交为持久化工作副本（单材质用 SINGLE，批量用 BATCH）
   & $ctl material handoff-live --session-dir $sDir --session-id $sId `
     --operation submit --mode SINGLE --wait-seconds 30
   ```
   等待超时后复用同一个 `--request-id` 查询结果（`--operation result`）；收尾用 `--operation stop` 卸载队列并保持 Blender 打开。
2. **从已保存文件读草稿与选面**：
   - 读取已保存工程的当前受管材质草稿：`& $ctl material read-current --params $paramsJson --output $draftJson`（需要从旧基线显式升级为 v2 草稿时加 `--upgrade-v2`）。
   - 捕获已保存网格的当前选面用于单个批量分配：`& $ctl material capture-faces --params $paramsJson --output $facesJson`。
   - 遇到历史提交需要找回或重试时，用 `material recover-submission` 或 `material retry-submission`（先跑各自 `--help` 核对参数）。
