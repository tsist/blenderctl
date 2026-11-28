# 公共 CLI、现场窗口接手、人工往返与长作业恢复

所有正式读写、剪辑修改、现场接手和长作业恢复都经由 `./blenderctl.ps1` 完成。先跑 `discover` 与 `describe` 拿准确参数，再落盘请求 JSON。

## 1. 发现入口与查看 Schema

全局参数（如 `--jobs-dir`、`--timeout`、`--compact`、`--video-release`）统一放在 `video` 子命令前面。

```powershell
$ctl = './blenderctl.ps1'

# 查看当前剪辑产品目录与入口
& $ctl discover --product video

# 查看顶层契约（不启动 Blender，不建作业）
& $ctl video describe

# 按需查看各分节 Schema
& $ctl video describe --operation manifest --schema
& $ctl video describe --operation clip --schema
& $ctl video describe --operation output --schema
& $ctl video describe --operation ripple --schema
& $ctl video describe --operation otio_export --schema
& $ctl video describe --operation otio_import --schema
```

可用子命令一览：

- `video describe`：查当前契约、支持范围与分节 Schema。
- `video run`：执行 `create` 或 `patch` 清单，生成新的工作副本与可选输出。
- `video inspect`：只读检查已保存工程中的受管场景、条带、锁定状态与指纹。
- `video ripple`：对短 SDR 受管时间线的指定条带右边界（`--edge END`）做波纹改时。
- `video otio export` / `video otio import`：导出或导入纯剪切 MOVIE/SOUND 的 OTIO 时间线。
- `video inspect-live`：读取当前打开的 Blender 窗口里尚未保存的剪辑现场状态。
- `video job status|result|wait|cancel|cleanup-isolation`：查询、分段等待或取消当前分发的剪辑作业。

## 2. 最小建片请求与提交流程

每次任务在 `work/audit/<batch>/>/jobs` 建独立作业目录。所有时间区间使用左闭右开 `[start, end_exclusive)`，帧率使用有理数 `[numerator, denominator]`。

下面演示在已确认零外部引用的空源工程里创建新场景 `VideoCut`（`create` 需要尚不存在的场景名），实际制作时把 `file`、`expected_sha256` 和 `resources` 替换为现场实测值：

```json
{
  "schema_version": "1.0",
  "command": "video.run",
  "params": {
    "file": "<PROJECT_ROOT>\\work\\demo\\source.blend",
    "expected_sha256": "<现场计算的64位小写SHA256>",
    "resources": [],
    "manifest": {
      "version": "1.0",
      "action": "create",
      "workflow_id": "cut01",
      "scene": "VideoCut",
      "timeline": {"fps": [24, 1], "width": 1920, "height": 1080, "frame_start": 1, "frame_end_exclusive": 49},
      "clips": [
        {"id": "shot001", "shot_id": "shot001", "kind": "COLOR", "channel": 1, "start": 1, "duration": 24, "color": [0.15, 0.3, 0.6, 1]},
        {"id": "shot002", "shot_id": "shot002", "kind": "COLOR", "channel": 1, "start": 25, "duration": 24, "color": [0.6, 0.2, 0.1, 1]}
      ]
    }
  }
}
```

把请求存为 UTF-8 JSON 后提交，同时检查退出码、返回 JSON 的 `ok` 与权威作业终态：

```powershell
$batch = 'work/audit/<batch>/'
$jobs  = Join-Path $batch 'jobs'
New-Item -ItemType Directory -Path $jobs -Force | Out-Null

& $ctl --jobs-dir $jobs --timeout 180 --compact request (Join-Path $batch 'request.json')
```

也可以直接用参数形式调用 `video run`、`video inspect` 或 `video ripple`：

```powershell
# 只读检查已保存工程
& $ctl --jobs-dir $jobs --compact video inspect $blend `
  --expected-sha256 $sha --resources $resJson --scene VideoCut --workflow-id cut01

# 波纹调整单镜右边界时长
& $ctl --jobs-dir $jobs --compact video ripple $blend `
  --expected-sha256 $sha --resources $resJson --scene VideoCut `
  --workflow-id cut01 --clip shot002 --duration 36 --edge END
```

凡是源 `.blend` 或清单里用到的外部文件（图像序列每一帧、视频、音频、字体、代理、工具可执行文件），都把绝对路径和小写 SHA256 显式写进 `resources` 闭包，产物路径一律以回执的 `artifacts` 为准。

## 3. 接手打开的 Blender 窗口（`video inspect-live`）

用户在 Blender 窗口里手动调整了时间线且尚未存盘时，先用现场检查入口把未保存的手改读出来：

1. 在目标 Blender 窗口中调用挂载接口 `attach(NEW_ABSOLUTE_DIRECTORY)`，取得会话目录 `SESSION_DIR`、`SESSION_ID`、场景名 `SCENE`（以及受管 `WORKFLOW_ID`）。
2. 在终端执行现场只读检查：
   ```powershell
   & $ctl video inspect-live --session-dir $sDir --session-id $sId --scene $scene --workflow-id $wfId --wait-seconds 30
   ```
   若一次等待仍在执行，带上同一个 `--request-id` UUID 重复调用即可取回挂起的检查结果。
3. 确认手改内容后，让用户在窗口中另存为一份带版本名的新快照，再对这份磁盘快照计算 SHA256，转入下面的定向 `patch` 流程。

## 4. 人工往返、锁条带与冲突处理

接手手改过的受管工程快照时，按「算新 SHA → `video inspect` → 定向 `patch` → 冷重开核对」四步推进：

- **只动目标字段**：`patch` 清单保留原 `version`、`workflow_id` 和 `scene`，在 `edits` 里按顺序列出 `add`、`update`、`remove` 或 `select_candidate`。`update.changes` 只写实际要改的键，未提及的字段、非目标条带和未知条带原样保留。
- **尊重锁定与原生手改**：锁定检查同时覆盖条带本身及来源/目的轨道。涉及 `remove`、换源或改 `fit` 重建时，系统会核对原生指纹，保护用户手改过的裁切（crop）、自定义属性、混合模式和曲线；遇到冲突直接报错并保留双方快照，由用户决定取舍。
- **同步联动声音与字幕**：增删镜头、改时长或换顺序后，显式更新对应的声音与字幕条带，随后复查有效帧域与输出预算。
- **外来工程只做只读查看**：非本工作流创建的任意外来 VSE 工程先只读查看并评估重建方案，受管 `patch` 聚焦在本工作流创建并标记所有权的场景上。

## 5. 长作业控制、取消与恢复

- **异步提交与分段等待**：耗时渲染或长片导出在全局参数里加上 `--async`，拿到 `$jobId` 后用 `video job wait` 分段等结果（单次最长 55 秒），还在跑就用同一个 `$jobId` 继续等：
  ```powershell
  & $ctl --jobs-dir $jobs --compact video job wait $jobId --wait-seconds 30
  & $ctl --jobs-dir $jobs --compact video job status $jobId
  & $ctl --jobs-dir $jobs --compact video job result $jobId
  ```
- **取消以权威终态为准**：需要中止本批次长作业时运行 `video job cancel $jobId`，以 `video job status` / `video job result` 返回的 `cancelled` 为最终状态，忽略被中断作业残留的旧进度条文本。
- **从旧报告安全恢复**：新作业里传 `--resume <旧video-report.json> --resume-sha256 <SHA>`（或在请求 JSON 里写 `resume: {file, expected_sha256}`），系统会核对原请求、源工程、资源闭包与保存前期望，在新作业目录里完整重做输出，原候选与旧回执保持原样。若期间改动了工程或资源 SHA，直接新建请求从最新源重跑。
- **读取或恢复 0.1.4 历史作业**：沿用原宿主与原 `--jobs-dir`，在 `video` 子命令前加上 `--video-release 0.1.4`：
  ```powershell
  & $ctl --jobs-dir $oldJobs --video-release 0.1.4 --compact video job result $oldJobId
  ```
- **保留恢复依赖**：准备继续恢复某条作业链时，保留原报告引用的上游候选、资源文件和旧输出缓存；确定彻底放弃该链路后再清理大缓存。
