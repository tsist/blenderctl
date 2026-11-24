# 命令写法、请求 JSON 与建模 UV 扩展

使用 PowerShell 7 调用 `./blenderctl.ps1`。含空格的路径用引号包住，全局开关放在子命令前面。

## 标准起手模板

```powershell
$ctl   = './blenderctl.ps1'
$batch = 'work/audit/<batch>/'
$jobs  = Join-Path $batch 'jobs'
New-Item -ItemType Directory -Path $jobs -Force | Out-Null

$raw  = & $ctl --jobs-dir $jobs capabilities
$code = $LASTEXITCODE
$res  = $raw | ConvertFrom-Json
if ($code -ne 0 -or !$res.ok) { throw ($raw -join "`n") }
```

- **写 UTF-8 JSON 文件**：`$obj | ConvertTo-Json -Depth 100 | Set-Content -LiteralPath $jsonPath -Encoding utf8`。
- **算小写 SHA256**：`(Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash.ToLowerInvariant()`。
- **按名称精确查询数据块**：`query` 的选择器写齐三个键 `{"type": "Object", "name": "Cube", "library": null}`（本地对象的 `library` 填 `null`）。

## 位置参数与 `--file` 速查

| 命令族 | 源 `.blend` 怎么传 | 必填配套参数 |
|---|---|---|
| `inspect`、`query`、`model inspect`、`scene inspect` | 位置参数 `file` | `--expected-sha256` |
| `project verify`、`project prepare-copy` | 位置参数 `file` | `--expected-sha256`、`--resources <json>`（`prepare-copy` 另加 `--manifest <json>`） |
| `scene` / `model` / `node` / `rig` / `simulation` 的 `prepare` | 命名参数 `--file` | `--expected-sha256`、`--manifest <json>` |
| `dependency plan-relink`、`project override prepare` / `resync` | 命名参数 `--file` | 按各自 `--help` 传入资源描述符 `{"file": "绝对路径", "expected_sha256": "小写SHA"}` |
| `request` | 位置参数 `request.json` | JSON 顶层含 `"schema_version": "1.0"`、`"command"`、`"params"` |

工程确实没有任何外部贴图或缓存时，`--resources` 传一个内容为 `[]` 的 JSON 文件；有外部依赖时逐项列齐，保持打包与相对路径重映射完整。

## 建模减面、脏网格重拓扑与 UV 常用操作

这些能力已经集成在 `model prepare` 与 `uv` 子命令里，写 `manifest.json` 前用 `uv describe` 或对应 Schema 核对字段：

- **共面融并减面**：在 `model.prepare` 的 `modifier.add`（`DECIMATE`）里设 `mode="DISSOLVE"` 与 `angle_limit_deg`（可选 `delimit` 按法线、材质、接缝、锐边、UV 保护边界），或用直接网格操作 `mesh.dissolve_limited` 清理布尔后的碎共面。
- **脏网格自动重拓扑回投影**：在 `model.prepare` 的 `mesh.retopology` 里选 `adapter="QUADRIFLOW_SHRINKWRAP_V1"`，适合带修改器、开口边界或锐边的外部生成网格，支持设定目标面数、体素预处理与缩裹回贴，并在报告里列出属性变化与表面贴合误差。闭合干净网格也可继续用 `QUADRIFLOW_CLOSED_V1`。
- **低拉伸展开、显式打缝与光照图 UV2**：`model.prepare` 的 `uv.unwrap` 与 `uv run` 都支持 `method="MINIMUM_STRETCH"`（SLIM）。展开动作保持现有接缝不变；需要先加缝时，用 `uv inspect` 看推荐接缝，再跑 `uv run` 的 `operation="seams"`（按角度或锐边打缝）。做第二套光照图 UV 用 `uv run` 的 `operation="lightmap"`（默认写进 `UVMap_Lightmap` 层，保留主渲染 UV 不动）。

## 空场景流水线试跑

技能自带 `assets/studio-pipeline.json` 三步样例（建 2 米立方体与相机灯光 → 渲 256×256 小图 → 重开检查）。用之前先复制到当前批次目录再改参数：

```powershell
Copy-Item '<本技能目录>\assets\studio-pipeline.json' $batch
& $ctl --jobs-dir $jobs pipeline plan --manifest "$batch\studio-pipeline.json"
& $ctl --jobs-dir $jobs --timeout 300 pipeline run --manifest "$batch\studio-pipeline.json"
```

跑完读作业目录下的完整 `result.json`（子步骤读 `child_directory` 里的 `result.json`），从返回字段取出候选 `.blend` 和输出图片路径核验。导出 `GLB / FBX / USD` 前先从当前副本跑 `rig inspect` 拿到匹配的 `driver_profile` 审核后再传给 `exchange analyze` 与 `export`。
