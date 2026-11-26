---
name: blender-cli
description: 通过项目公共入口 blenderctl 查找动作、读取工程、在工作副本上执行修改、等待长作业、接手现场窗口并做独立重开与发布事务。只要实际读写 .blend、调度自研插件后台动作或核验交付产物就用它。
metadata:
  version: "1.1.0"
---

# Blender CLI 调用手册

本仓的 `production.run` 适用于 Blender 5.3.0 `02f0899d78e3` 的动画曲线、场景合成、灯光和 VSE。先按公开发行说明核验并解包 `blender53-production-1.0.0.zip`，再用 `--blender <实际程序> request <请求.json>` 提交。请求指定 `domain`、`release`、源文件和 SHA-256。独立步骤可交 `pipeline run` 并行。先读 `production-report.json` 的操作和保存后重开结果，再领用 `production-candidate.blend` 作独立核对。[四域请求说明](https://github.com/tsist/blenderctl/blob/main/docs/PRODUCTION.md)

本技能管怎么调 `./blenderctl.ps1`。项目当前唯一支持与验收的宿主是 Windows Blender `5.3.0`，启动器默认使用工作区内置的这套环境。具体某一门工艺怎么做，读对应部门的专项技能；跨部门顺序见 [blender-work-methodology](../blender-work-methodology/SKILL.md)。

## 第一步：用 discover 找到准确入口

动手前先查公共目录，拿到经过核验的命令前缀和所需字段，省去猜子命令和参数名的麻烦。

```powershell
$ctl = './blenderctl.ps1'

# 按作品目标或专项产品浏览
& $ctl discover --goal asset
& $ctl discover --product material --actions

# 按动作 ID 精确取出启动器
$d = & $ctl discover --action asset.find_by_type | ConvertFrom-Json
$item = $d.data.actions.items[0]
```

拿到 `$item` 后按下面顺序用：

1. **先跑帮助看参数**：取出 `$item.launchers[0].argv` 直接执行一次，看清最新参数说明；同一动作有多个版本时加 `--product` 和 `--edition` 挑定。要是 `launchers` 为空，读 `launcher_reason` 看它属于纯界面按钮还是需要现场窗口条件。
2. **顺着 `command_argv` 拼输入**：把真实文件路径和业务参数接在 `command_argv` 后面。返回里带有 `request_fields` 就填进请求 JSON；带有 `session_open_fields` 就在开现场会话时带上。
3. **专项细则用 `describe` 查**：像 `material`、`uv`、`geometry`、`toon-workflow` 都自带 `describe` 子命令（如 `& $ctl material describe --operation study --section study`），不启动 Blender 就能读到参数分节说明。

## 第二步：建批次目录，先读后改

每次任务在 `work/audit/<batch>//` 下建独立的 `jobs` 和 `transactions` 目录，全局参数统一放在子命令前面。

```powershell
$batch = 'work/audit/<batch>/'
$jobs  = Join-Path $batch 'jobs'
$tx    = Join-Path $batch 'transactions'
New-Item -ItemType Directory -Path $jobs, $tx -Force | Out-Null

$src = '<PROJECT_ROOT>/work\lamp\source.blend'
$sha = (Get-FileHash -LiteralPath $src -Algorithm SHA256).Hash.ToLowerInvariant()

# 先只读摸清对象名、材质槽与外部引用
& $ctl --jobs-dir $jobs inspect $src --expected-sha256 $sha
& $ctl --jobs-dir $jobs scene inspect $src --expected-sha256 $sha
```

先读结构报告再写修改请求，对象名、集合名、UV 层名和帧域全都从报告里实取。所有编辑命令都在新作业目录里生成工作候选（如 `model-candidate.blend`），原文件保持完好留作对照。

## 第三步：提交请求与等待长作业

- **短参数直接走命令行，复杂参数写 UTF-8 JSON**：写好 `{"schema_version": "1.0", "command": "领域.动作", "params": {...}}` 后运行 `& $ctl --jobs-dir $jobs request $reqFile`。命令行用空格和连字符（`project prepare-copy`），JSON 请求里用点号和下划线（`project.prepare_copy`）。
- **用 `--compact` 省上下文**：跑 `material`、`geometry` 和 `model inspect` 时加上 `--compact`，终端直接拿精简摘要、源 SHA 和报告路径，完整 JSON 照常留在作业目录供随时按需细查。
- **长作业用 `job wait` 等结果**：提交耗时渲染、烘焙或参数对比后，运行 `& $ctl --jobs-dir $jobs --compact job wait $jobId --wait-seconds 30`（单次最长 55 秒），一次拿回终态或当前做到第几项进度；还在跑就顺着同一个 `$jobId` 接着等。

## 第四步：接手打开的 Blender 窗口

后台命令读的是磁盘快照；用户在打开的 Blender 5.3.0 窗口里做了手调时，先走对应插件的现场交接入口把改动收进工作副本：

- **材质手调**：目标窗口挂好会话后，运行 `& $ctl material handoff-live --session-dir $sDir --session-id $sId --operation inspect` 核对，再用 `--operation submit --mode SINGLE`（或 `BATCH`）生成带 SHA 的工作副本继续往下做。
- **相机、几何节点、绑定、合成与剪辑**：分别走 `camera session-live`、`geometry session-live`、`rigging-workflow session-live`、`compositing session-live` 和 `video inspect-live`（先运行各自 `--help` 看会话目录参数）。

## 第五步：一键真视口快检、几何体检与冷重开

每次跑完同时看 `$LASTEXITCODE` 和返回 JSON 里的 `ok`。`ok` 为 `true` 时，顺着 `artifacts` 给出的路径取候选文件接着用；`ok` 为 `false` 时先读 `error` 和作业目录日志定位原因。

1. **一键聚焦与真视口 4 视角快检（推荐优先）**：任何建模、绑定、UV、材质、布光迭代后，直接用隔离桌面真视口秒出预览图（0 弹窗/0 抢焦点，纯只读不改源 `.blend`，自动控图 ≤ 2048px）：
   ```powershell
   # 默认 Quad 四视角（顶/前-Y/右+X 正交 + 前3/4 透视）实体+45%半透明线框
   & $ctl --jobs-dir $jobs --compact preview $candidate --output "$batch\quad_wire.png"

   # 聚焦单个零件/集合：默认保留上下文查穿插；加 --isolate 独显目标但保留灯光世界
   & $ctl --jobs-dir $jobs --compact preview $candidate --objects "Body,Handle" --isolate --mode matcap

   # 同机位四阶段联检板（大形 / 半透明线框 / LookDev / Cycles DLSS 或 OptiX 回退）
   & $ctl --jobs-dir $jobs --compact preview $candidate --compare-modes solid,solid_wire,material,rendered --view front_3q

   # 其他专业检查模式与热会话复用
   # --mode: wireframe(--xray) | solid(--solid-color random) | solid_wire(--wire-opacity 0.45) | matcap | face_orientation | material(--wire-overlay 0.35) | rendered(--render-engine cycles|eevee --denoiser auto|dlss|optix --strict-denoiser)
   # 多轮迭代免冷启动：preview session start -> preview $file --session <id> [--framing-from prior.json] -> preview session stop <id>
   ```
2. **通用几何体检**：在白模里程碑、外部生成导入初筛或正式交付前，对保存好的 `.blend` 跑一次只读体检：
   ```powershell
   & $ctl --jobs-dir $jobs --compact model inspect $candidate --expected-sha256 $cSha --qa-profile game
   ```
   按用途选 `game`、`render`、`mobile_strict` 或 `print`。报告把负缩放、法线粗糙度贴图误设 `sRGB`、使用中的空材质槽、丢贴图归进 `hard_blockers`（先修清再往下交），把共面闪烁、浮空件、对称 UV 重叠、多边面归进 `review_signals` 供结合画面判断。属于无争议的纯数据贴图色彩空间或未用空材质槽，可在 `model prepare` 里加 `{"op": "qa.fix_safe", "profile": "game", "dry_run": false}` 在新副本上一键修好。
3. **另起进程冷重开并看真图**：交付前对最终候选再跑一次 `inspect` 或 `project verify`（核对完整外部资源清单，包括贴图、VDB、USD 与模拟缓存），动画抽查关键帧数值，视觉任务用 `preview` 或正式渲染核对代表画面。

## 第六步：中断续跑与正式发布

- **续跑**：`pipeline` 或 `workflow` 中断后，用原 `--jobs-dir` 跑 `pipeline resume <job_id>` 或 `workflow resume` 重评未完步骤；单步命令修好输入后从源副本重新提交。
- **发布到正式位置或资产库**：工作候选验收通过后，写新文件用 `project plan-copy`，覆盖已授权目标或发布进 `<ASSET_LIBRARY>` 走 `asset plan` 加 `transaction apply`（全程保持同一个 `--transactions-dir`）。细节见 [恢复与发布](references/recovery.md)。

## 例子：给一件道具做交付前体检并修好贴图色彩空间

接到任务：「检查 `work/prop/dial.blend` 能不能按游戏资产交付，修好无争议的小毛病。」

1. 建批次目录 `work/audit/<batch>/`，算出 `dial.blend` 的小写 SHA256。
2. 查帮助确认参数：`& $ctl model inspect --help`。
3. 跑体检：`& $ctl --jobs-dir $jobs --compact model inspect $src --expected-sha256 $sha --qa-profile game`。
4. 看返回：`ok` 为 `true`，`qa-report.json` 提示法线贴图误标成了 `sRGB` 且多出两个未使用的空材质槽，其余拓扑与 UV 正常。
5. 写一份 `manifest.json` 放入 `{"operations": [{"op": "qa.fix_safe", "profile": "game", "dry_run": false}]}`，运行 `& $ctl --jobs-dir $jobs model prepare --file $src --expected-sha256 $sha --manifest $manifest`。
6. 从返回 `artifacts` 取出新生成的 `model-candidate.blend` 和它的新 SHA，另跑一次 `model inspect --qa-profile game` 冷重开复查，`hard_blockers` 清零，连同候选路径和体检报告交给资产入库部门。

## 参考

- [命令写法、请求 JSON 与建模 UV 扩展](references/execution.md)
- [流水线、错误码、脚本回执与事务发布](references/recovery.md)
- [子命令与专项分派速查表](references/domains.md)
- [契约文档与 Schema 索引](references/sources.md)
- 离线契约速查脚本：`scripts/discover.py` · 最小流水线样例：`assets/studio-pipeline.json`

维护：唯一维护源是 `./.agents/skills/blender-cli/`，改完运行同步脚本更新各镜像。
