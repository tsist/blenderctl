# 动作重定向与工作台 CLI 操练

本篇汇总绑定部门日常通过 `blenderctl.ps1` 调度的实操入口，覆盖入口发现、参考定位镜像、已有骨架维护、旧绑重建、动作重定向与足底接触修正。具体发行版号与最新参数，动手前先用 `discover --product rigging` 实时查询。

## 第一步：用 discover 拿准入口与参数

```powershell
$ctl = './blenderctl.ps1'

# 查看当前支持的绑定发行版与概览
& $ctl discover --product rigging

# 查看推荐候选版或固定版的完整动作清单
& $ctl discover --product rigging --edition <发行版> --actions

# 精确查询单个原生动作的启动器与请求字段
$found = & $ctl discover --product rigging --edition <发行版> --action <动作ID> | ConvertFrom-Json
$entry = $found.data.actions.items[0].launchers[0]
```

拿到 `$entry` 后，先跑 `$entry.argv` 看清参数契约，再顺着 `$entry.command_argv` 拼入实际请求 JSON 与全新的 `--run-dir`。所有请求都用绝对路径和源工程 SHA-256 绑定输入，每次执行先读返回目录的 `result.json`，排错时再读同目录 `blender.log`。

## 日常核心公共入口速查

| 制作任务 | 公共命令前缀 | 常用子命令与关键用途 |
|---|---|---|
| 整角色检查、多域体素蒙皮、同作业转权与交付 | `rigging-workflow candidate-entry` | `inspect` \| `scope` \| `audition` \| `finish` \| `result` |
| 选骨局部重算与拆点接缝核验 | `rigging-workflow candidate-selected-skin` | `inspect` \| `execute`（支持单件与 `targets` 多网格批量） |
| 同骨架供体迁权（`RIGID` / `TRANSFER` / `LOCAL`） | `rigging-workflow candidate-skinning-policy` | `qualify` \| `plan` \| `preview` \| `apply` |
| 作者返工、兼容重建与原骨架逐点修权 | `rigging-workflow candidate-repair` | `inspect` \| `review-handoff` \| `apply` \| `cancel` |
| 接手现场打开窗口（播放、毛发开关、聚焦源对象） | `rigging-workflow session-live` | `inspect` \| `focus-source` \| `play-pause` \| `stop` \| `transport-enable` \| `transport-disable` \| `result` |
| 角色定位、参考生成、姿态切换、表情、动力学与重定向 | 通过 `discover --product rigging --action <动作ID>` 查取 | 按返回的固定版面板会话或专用脚本参数执行 |

## 参考骨定位、镜像与骨架生成

1. **捕获输入与定位拟合**：用 `capture_character_inputs` 登记生产网格，用 `sync_markers`、`lock_markers`、`mirror_markers` 与 `restore_markers` 整理身体、手部与面部标记；面部标尺先以 `inspect:true` 核对范围与点数，再带 `expected_preview_sha256` 采用。按 `BODY`/`HANDS`/`FACE` 估计后，从 `fit_reference` 拿回待审参考骨架。
2. **定位工作区检查与贴面**：用 `inspection_workspace` 与 `inspection_focus` 聚焦局部视图检查关节；需要贴合表面时用 `inspection_edit`（`REMEMBER`）和 `inspection_nearest`；对比前后骨位用 `inspection_compare`。退出后自动还原原工作区、视图层与选区。
3. **镜像修正单侧关节**：外形左右对称且单侧关节需返修时，在 `--background-positioning` 会话中调用 `inspection_edit.MIRROR_L`（左到右）或 `MIRROR_R`（右到左）。从 `status.positioning_drag` 取 `owner`、`rig`、`target_mesh` 填入 `parameters.targets`，以 `骨名/H` 或 `骨名/T` 指定来源侧端点并带上 `expected_context`。保存快照并另起进程叠图复查，随后用修正后的参考重做模块配置再生成新骨架。
4. **生成未蒙皮待审骨架**：配置好模块库后，通过 `prepare_module_configuration`、`generate_rig` 或 `prepare_character_entry`（设 `save_review_snapshot=true`）产出独立的未蒙皮待审骨架工程；在待审骨架上核对保姿态 FK/IK 切换时，给 `operation=switch` 传 `measurement=bone-matrices` 直接按骨段矩阵核验，随后走骨架双审与用户审看①。

## 已有角色接手：原骨架维护 vs 兼容重建

- **保留原骨架独立维护**：通过 `native_maintenance_action` 在 `--background-maintenance` 会话中作业：
  - `CLONE`：先传 `inspect:true` 读取网格顺序与外部引用，再复制出绑定指向新副本的独立维护工程。
  - `SELECT_PACK` / `SELECT_CONTROL` / `READ_BONE` / `READ_VERTICES`：只读核对骨架、控骨局部端点与 Roll，或读取原始顶点权重。
  - `REPAIR.CONSTRAINT` / `REPAIR.BONE` / `REPAIR.WEIGHT`：先用 `inspect:true` 或 `review-scope` 冻结计划与双审范围，再生成独立候选。
  - `READ` / `ADD_CONTROL` / `ADD_PROPERTY` / `ADD_KEY` / `APPLY_LAYOUT` / `KEY_CONTROLS`：维护自定义控制布局并给可见控骨写键（原 Action 自动留检查点）。
- **旧作者返工与兼容重建**：
  - 评估参考改动影响：在 `--background-author-rework` 会话里调用 `author_rework.CAPTURE` 与 `PREPARE`，读回 `observation_after.impact` 查看骨位、形变语义和控制组受影响范围，完成后用 `CANCEL` 结束临时计划。
  - 生成可核查的重建候选与迁权计划：通过 `rebuild_bound_copy` 执行 `operation=prepare-rebuild`（旧骨架使用不同配置库时显式传入 `source_configuration`），得到保留原权重的新候选及 `artifacts.inspect_request`；接收方调用 `cli.rigging.rebuild.inspect` 在新进程逐点复核每个部件的旧骨到新骨映射与组锁，再另行审核骨架与实际写权。

## 外部动作重定向：MMD 与通用 FK（BVH / FBX）

### 1. 导入与转换 MMD（PMX / VMD）

- 在场景内直接转换走 `retarget_mmd_scene.CONVERT`，批量后台处理走 `retarget_mmd_background.ENQUEUE` → `REFRESH` → `USE_SELECTED` / 收集独立候选。
- 明确传入源片段、身体与表情 Action/Slot、源帧率与目标工程，按帧率比例对齐身体与指定表情通道；关闭预览时恢复原基础动作。
- 复用既有 MMD 相机库时，按源库文件 SHA 执行 `c94_camera.migrate_legacy_profile` 与 `append_profile`，切换查看后用 `restore_active_camera` 还原原活动相机与时间线。

### 2. 通用 FK 与 BVH / FBX 动捕队列

1. 在 `--background-retarget` 会话打开工作副本，用 `retarget_import_bvh` 等入口载入动捕源文件（带绝对路径与 SHA），在 `retarget-body` 选定源、目标与动作来源。
2. 先对外部带骨架角色运行 `retarget_generic_target.INSPECT` 检查目标控制图与约束依赖；确认映射骨名后发送 `{"acknowledged":true}`，再跑 `retarget_general_job_prepare` 的 `POLICY_FROM_GENERIC` 和 `VALIDATE_POLICY` 获取原生签名策略。
3. 在 `<session-dir>/retarget-jobs` 配置队列目录，用 `retarget_queue.ADD` 录入源 Action、Slot 与帧率（可按需修改起止帧与输出名），提交 `retarget_general_job_enqueue.ENQUEUE_GENERIC` 并记下作业 ID。
4. 用 `retarget_background` 的 `REFRESH`、`SELECT`、`COLLECT_SELECTED` 和 `PREVIEW` 收取并预览；需要把转头幅度分担到颈部时，用 `retarget_headneck_preview` 对照首中尾帧。结束预览先运行 `retarget_action.STOP` 恢复原绑定，再 `RELEASE` 并保存冷开复验。
5. 已有校准 Profile 的短片段转换走 `profile_convert`、`profile_remember`、`profile_stop`，一并交付可复用的校准 JSON、新 Action 与 Slot。

## 足底接触检测与现场审阅交接

出现脚滑、悬空或穿地时，按「后台检测 → 交接审阅 → 确认或撤回」处理：

1. **生成足底报告与人工提示**：用 `contact_review_hint`（`RECORD` / `VALIDATE`）留存动画师观察到的接触帧段；需要整段重检时在 `--background-detection` 会话调用 `contact_detection_background`（`ENQUEUE` → `REFRESH` → `COLLECT`），在报告里 `SELECT_ISSUE` 并 `TRANSFER` 传给修正面板。
2. **现场窗口审阅与撤回**：在目标 Blender 窗口打开交接单指定的 `review.blend`，附着现场接触会话后，通过 CLI 依次调用 `contact-live-status`、`contact-live-load`、`contact-live-preview`（对照 `review.frames` 帧段）。确认送审后跑 `contact-live-present` 唤起原生确认对话框由审阅者拍板；若需撤回已应用修正，刷新状态拿到最新 `expected_context` 后跑 `contact-live-withdraw`，核对原 Action 与 Slot 恢复，最后 `contact-live-release` 释放现场计划。

## 随文件便携脚本与冷开收尾

- 交付给未安装制作插件的下游环境时，确认工作副本内嵌日常脚本（`portable.embed`）为手动运行且字体采用相对目标 `.blend` 目录的显式路径；发裙动态片段使用原生 driver frame 以支持安全 Append 到新场景。
- 最终交付前，对保存好的 `.blend` 另起后台进程冷开检查：核实源 SHA、拓扑与 UV 完整、变形骨权与 Action/Slot 完好、控制板布局可读且外部路径清零。
