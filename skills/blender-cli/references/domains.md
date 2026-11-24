# 子命令与专项分派速查表

`blenderctl.ps1` 一头连着核心引擎命令，一头连着各自研插件的专项入口与现场窗口交接。拿不准用哪个时，先跑 `blenderctl.ps1 discover --product <产品ID> --actions` 查完整动作列表。

## 工程基础与通用检查

| 用途 | 命令入口 | 要点 |
|---|---|---|
| 查环境与能力清单 | `--version`、`doctor`、`capabilities`、`discover` | `--version`、`--help`、`discover` 同步秒回且不建作业；`doctor` 和 `capabilities` 会建作业，记得带上 `--jobs-dir` |
| 只读查看 `.blend` 与对比差异 | `inspect`、`query`、`diff`、`validate`、`dependency` | 摸清数据块、选择器属性、两版文件差异与外部资源重连计划 |
| 工程依赖核验、复制与链接覆盖 | `project verify`、`prepare-copy`、`plan-copy`、`link`、`override` | 核对外部资源闭包，生成相对路径重映射副本，管理库链接与 Library Override |
| 场景组装与通用几何体检 | `scene`、`model`（含 `model inspect --qa-profile`、`model panel-controls`） | 建相机灯光集合、网格与修改器栈操作、游戏/渲染/打印体检、硬表面面板选区交接 |
| 格式导入导出与传统烘焙 | `exchange`（`analyze / export / import / convert`）、`texture`、`sculpt` | GLB / FBX / USD 交换与误差分析、CPU 贴图烘焙、受控雕刻回放 |
| 多步流水线、事务与作业控制 | `pipeline`、`workflow`、`transaction`、`job`（`status / result / wait / cancel`） | 有界 DAG 与顺序工作单元编排、原子文件发布事务、长作业等待与取消 |

## 自研插件专项与现场交接入口

| 领域（`discover --product`） | 常用 CLI 命令族 | 现场窗口交接与特色子命令 |
|---|---|---|
| 生成与建模采用（`modeling`） | `modeling`（默认工作区版含 Studio FBX；加全局开关 `--modeling-release 0.1.14` 走独立 GLB 版） | `modeling review-candidate`、`modeling adopt-candidate` |
| UV 展开与迁移（`uv`） | `uv`（`describe / inspect / run / batch`） | `uv capture-selection`、`capture-constraints`、`transfer-prepare / execute`、`checker-preview`、`read-surface-report` |
| 材质与高低模投射（`material`） | `material`（`describe / run / batch / study / template-save / procedural-export / projection-bake`） | `material handoff-live`、`read-current`、`capture-faces`、`projection-suggest / attach / effects / deliver` |
| 原生资产治理（`asset-library`） | `asset`（`libraries / prepare / index / plan / preview`）、`identity` | `asset find-collections`、`find-assets`、`place-collection`、`use-asset` |
| 几何节点（`geometry`） | `geometry`（`describe / inspect / run / session`） | `geometry session-host`、`geometry session-live` |
| 角色绑定（`rigging`） | `rig`、`rigging-workflow` | `session-live`、`rc-generation / modular / local-skin / weight-repair / face / pose / control / contact / dynamics / accessory / retarget-queue`、`candidate-entry / selected-skin / repair` |
| 动画曲线与事件（`animation`） | `animation`（`inspect / run / sample / channel-inspect / channel-edit / clip-apply`） | `animation fk-character`、`pose-edit`、`event-store`、`project-events`、`panel-events`、`support`、`record-live` |
| 物理模拟（`physics`） | `physics`（`run / sweep / batch / resume / status / cancel`）、`simulation` | `physics pin-selection`、`physics panel-copy` |
| 镜头表与相机（`camera`） | `camera shot-table`、`camera copy`、`tracking` | `camera session-live`、`camera framing-check` |
| 灯光（`lighting`） | `lighting`（`run / inspect / batch`） | `lighting rig-handoff` |
| 三渲二与描边（`toon`） | `toon-workflow`（`describe` 及各模块动作）、`toon`、`toon-image` | `surface-slot`、`finish-slot`、`component-slot`、`sdf-face / hair`、`cel-recipe`、`shot-look`、`viewport-lines / session`、`vector-output`、`omni-preview` |
| 后期合成（`compositing`） | `compositing`、`node` | `compositing session-live`、`compositing preview-copy` |
| 批量渲染（`render-batch`） | `render`（`devices / run / batch`；可选 `--render-batch-release 0.3.0` 或 `0.3.1-dev`） | `render batch-panel`、`render batch-artifact`、`request` 下的 `render.batch.*` |
| 视频剪辑（`video`） | `video`（默认 `0.1.5`，旧作业恢复可加 `--video-release 0.1.4`）、`media` | `video inspect-live`、`video job status / result / wait / cancel` |

## 离线查核心 Schema 脚本

想在不启动 Blender 的情况下直查看底层点号命令对应的 JSON Schema 路径和分支，可运行本技能自带的只读脚本：

```powershell
$py = '<PROJECT_ROOT>/runtime\blender\releases\5.3.0\blender-5.3.0-windows-x64\5.3\python\bin\python.exe'
& $py '<本技能目录>\scripts\discover.py' --root '<PROJECT_ROOT>' --query 'project.link'
& $py '<本技能目录>\scripts\discover.py' --root '<PROJECT_ROOT>' --command project.override.resync --schema
```
