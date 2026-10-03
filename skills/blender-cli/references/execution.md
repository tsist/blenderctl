# 执行与独立验收

使用PowerShell 7。全局参数放子命令之前，含空格路径整体传参，复杂JSON写文件。

```powershell
$ctl = Join-Path $env:BLENDERCTL_ROOT 'blenderctl.ps1'
$batch = Join-Path $env:BLENDERCTL_ROOT 'runtime\audit\my-blender-task'
$jobs = Join-Path $batch 'jobs'
New-Item -ItemType Directory -Path $batch -Force | Out-Null
$raw = & $ctl --jobs-dir $jobs capabilities
$code = $LASTEXITCODE
$r = $raw | ConvertFrom-Json
if ($code -ne 0 -or !$r.ok) { throw ($raw -join "`n") }
$r.data.execution_contract
```

新批次用独有目录，不覆盖旧请求。JSON用 `ConvertTo-Json -Depth 100 | Set-Content -LiteralPath <文件> -Encoding utf8`。SHA用 `(Get-FileHash -LiteralPath <源> -Algorithm SHA256).Hash.ToLowerInvariant()`。先 `inspect <源> --expected-sha256 <SHA>`，结构详情按需query/scene inspect/rig inspect，不凭文件名猜对象名。

## 空场景到渲染

技能 `assets/studio-pipeline.json` 为三个步骤：scene.prepare创建2米立方体、相机/灯；render.run做256×256 CPU图像；inspect重开候选。无外部资产。复制模板到新批次再修改，不编辑技能原件。

```powershell
& $ctl --jobs-dir $jobs pipeline plan --manifest '<本批模板副本.json>'
# 检查退出码及计划后运行；plan也要求本机Blender存在。
& $ctl --jobs-dir $jobs --timeout 300 pipeline run --manifest '<本批模板副本.json>'
```

结果data.steps每步应succeeded或经验证reused；取studio.data.candidate与candidate_sha256，渲染路径从picture.data.outputs读取，不猜output.png。独立重开检查Cube几何、相机/灯；打开实际PNG检查尺寸、可见性和构图。模板不是完整美术场景或发布器。

实际读取 `result.json` 的完整JSON；不要把终端截断、选列摘要或 `--human`输出当完整返回。必要时按step.child_directory读取子job的result.json。

几何可用 `model inspect <候选> --expected-sha256 <SHA>`查看bounds/顶点/面，再用 `scene inspect`核对变换、单位和对象集合。模板CUBE的size是局部完整边长，不是半径，世界尺寸还受对象变换和scene单位设置影响。按名称query的selector必须包含 `{"type":"Object","name":"Cube","library":null}`三个键；本地对象library不能省略。

## 有源工程

project verify/prepare-copy的源为**位置参数**，--resources必填。仅确认无外部资源时用空数组文件。复制manifest为 `{"version_policy":"SAME_MINOR","path_policy":"REMAP_RELATIVE","unused_ids":"REJECT_LOSS","compress":true}`；读当前Schema核对。

```powershell
& $ctl --jobs-dir $jobs project verify '<源.blend>' --expected-sha256 '<SHA>' --resources '<resources.json>'
& $ctl --jobs-dir $jobs project prepare-copy '<源.blend>' --expected-sha256 '<SHA>' --resources '<resources.json>' --manifest '<copy.json>'
```

dependency plan-relink、project link/override prepare、project override resync使用**--file**（link可省略，override必需）。资源描述符为 `{"file":"绝对路径","expected_sha256":"小写SHA"}`；清单/回执是内联还是描述符由具体Schema决定。

scene/model/node/rig/simulation的prepare也使用 **--file**，不是位置参数。例如毛发：`simulation prepare --file '<源.blend>' --expected-sha256 '<SHA>' --manifest '<hair.json>'`；`hair.density`放在manifest.operations[].op中。

完整请求例：`{"schema_version":"1.0","command":"inspect","params":{"file":"绝对路径.blend","expected_sha256":"小写SHA"}}`，用 `& $ctl --jobs-dir $jobs request '<request.json>'`。schema_version不是CLI版本号。

exchange.analyze/export的driver_profile从当前源公共rig inspect取得，按明确驱动策略审核后使用；不能给新副本套旧SHA。分析通过不等于连续时间/导出/渲染通过。GLB/FBX/USD按适配器、单位、帧率与实际求值帧验收。

保留请求、返回JSON、job路径、成功产物/SHA、实际检验结果和限制。图像交付附图；动画记录帧域/误差/容差；缓存列保留目录。旧报告或测试脚本存在不代表本轮已执行，不把旧job路径充当新结果。
