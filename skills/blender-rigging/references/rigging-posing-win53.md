# 用原控件接动捕、摆姿和续作

用这份流程给已有 C94 原控件和普通原生 FK 人形接身体动作。交出可编辑的原生 Action、可独立重开的工程，以及运动范围与手调记录。先定好角色要做的动作、镜头视角、地面和时基，把原工程留作对照。

## 检查宿主、源文件和当前绑定

```powershell
$ctl = './blenderctl.ps1'
$contract = & $ctl rigging-workflow phase3 describe | ConvertFrom-Json
& $ctl discover --product rigging --edition $contract.data.edition --actions
& $ctl rigging-workflow phase3 run --help
```

读出工作副本的绝对路径、小写 SHA-256、场景、视图层、骨架、当前 Action、Slot、单位和实际 fps。备案原骨 rest、本地轴、权重、驱动和旧 Action。看两个代表姿势，先找关节实际枢轴，再看骨骼显示形状。旧游戏角色的结构差异留在对位报告，姿势改动写进原骨的通道或映射层。

`rigging-workflow phase3 run` 接受 UTF-8 JSON，顶层为 `operation` 和 `parameters`。操作已有工程时，再给 `file`、`expected_sha256`、`scene` 和 `view_layer`。写操作用尚未存在的绝对 `.blend` 路径做 `--output`。回执里的 `data.candidate` 和 `data.independent_reopen` 给出新文件 SHA 与冷开结果。 命令、参数和请求样例（`work/audit/<batch>/`）

## 一次建 Profile，接两段完整 FBX

1. 用 `retarget-fbx-inspect` 检查已获使用授权的带皮参考。提供 `source_file` 与 `source_sha256`，回读实际 Take、原生 Slot、源时基、帧域和根、骨盆、四肢的语义建议。首次配对确认歧义骨名，再用 `retarget-profile-build` 生成 Profile。外部文件应保留原始导出和对应授权信息。
2. 保存建档回执的 `data.profile`。内联传 `profile`，或用 `profile_file` 和文件本身的 `profile_sha256` 引用。分别检查两段无皮动作的 Take、Slot 和完整时域，再复用同一个 Profile。带皮参考与无皮片段的静置骨架身份变化时，复查源语义并让原生核重新校准。
3. 在已保存的 Object Mode 目标副本跑 `retarget-body`。显式指定的 `take`、`slot` 按实际导入名精确匹配。遇到同名导致 Blender 更改导入标识时，按回执列出的实际组合重试。记录导入源槽与目标 Action 槽。
   - C94 选 `target_kind: "C94"`，写到原控制骨。日常转向用 `HEADING`。真实片段需要翻转整个身体时，单段试 `root_rotation: "FULL"`，同时检查根与躯干限位的关系。
   - 普通 FK 选 `target_kind: "GENERIC_FK"`，给出 `target_mapping`、`acknowledged: true`。使用无活动 NLA 混合、无外部控制图的原生骨架。静态对象父级随工程保留，求解回写原骨与原 Slot。
4. 行进片设 `motion: "PRESERVE"`，按两角色体型核对方向、步幅、接触时机和总时长。原地片设 `IN_PLACE`，检查根的水平漂移。无独立根骨时，标明实际承担位移的骨。打开候选，以回执的 Action 与 Slot 回放并另存试改；复核原动作绑定、原 rest、蒙皮和旧动作指纹。

## 按原骨录三拍，校核左肩与多轴 ROM

1. 从角色中立开始，用原生控件摆出「迈步、接重、伸手」三拍。先看脚掌接触与骨盆承重，再看胸廓倾转和手的去向。用 `three-beats` 提交三个递增的 `frames`、明确的 `controls`，以及三组 `poses`。每个通道填 `bone`、`property`、`index`、`value`。在同角度看三张关键姿态，再从侧视和镜头视图播放过渡。
2. 已适配的左 FK 用 `shoulder-pose` 做单帧前举或外展。先试默认跟随，再用 `manual` 压肩或耸肩，用 `follow: 0` 做该姿势的旁路对照。C94 检查肩胛链与上臂协调。游戏 FK 从它的原上臂根读出肩线变化。
3. 用 `rom-inspect` 以直立参考帧比髋与胸腰。先核铰链的正确方向与局部轴，再评估肩、髋与胸腰的多轴活动。记录角色、姿态、载荷、测量轴及提示强度，按镜头所需调节多轴提示。实测角度供角色内比较，换骨图或物种时另校准。

犬的研究先看肩胛相对胸廓在前掌接地至离地间的滑移与回旋。鸟的研究分清膝和跗间，观察蹲伏到伸腿时各自贡献。两者均固定相机尺度，用真实绑定位参考与三个事件位复查。 比较解剖教程与冷开小样（`work/audit/<batch>/`）

## 接触修正先确认动作意图

- **普通走跑**。以加载的同一个 Action/Slot 做检测和地面校准，记录实际接触的足底表面或骨锚点。C94 的 `gait-contact` 使用已校准的脚跟问题，给 `motion_usage: "GAIT"`、检测报告与校准的文件 SHA、唯一问题 ID 和连续帧范围。生成独立联合锁定 Action。在冷开预览中检查足跟穿透、水平漂移及前后半帧的膝、骨盆和胸廓。停止预览后核对基础绑定恢复。
- **普通原生 FK**。以真实髋、膝与踝枢轴校准 `fk-heel-contact` 的目标，读实际带足部权重的脚跟网格。用脚跟支撑候选对照原动作，回写到原 FK 曲线和原槽。确认辅助求解结束后原骨、原蒙皮、旧动作继续可用。
- **舞蹈或有意滑步**。先做检测与人工审看，保留表演的切向运动。已存在走跑修正而某段需放开时，用 `tangent_release` 给起止、混合帧数和已校准支撑范围，另出候选，逐帧复查两端过渡。

同机位、同灯光、同时间坐标并排播放基线和候选。先看接重、停稳与再起步，再看关节过渡和轮廓。输出时写明对照 Action/Slot、支撑测点、时间范围、改了多少个原生通道及作者实际手调次数。混合不同帧率时按秒对齐。 双角色接触与原生审阅实证（`work/audit/<batch>/`）

## 自有 Blender 窗口手调并交回 CLI

1. 当前有人使用的窗口先保住未保存改动。从已保存的工作副本和新会话目录执行 `live-open`，它会建立单独的工作窗口。确认场景、视图层、骨架与源 SHA。
2. 在自有窗口暂停播放，把手调写入原生 Action 的正确 Slot。用 `live-status` 取最新 `expected_context`，并检查帧、模式、Action、Slot 和 FCurve 指纹。关键帧数值、手柄等曲线内容变化都需要重新观察。
3. 用同一会话的新请求 ID 和最新指纹运行 `live-capture`。遇到 `CONFLICT` 刷新状态。有待处理结果时，用 `live-result` 和原请求 ID 取回。准备退出时再次刷新后执行 `live-close`，核对最终恢复快照，再读 `closed.json` 和 `host-ended.json`。
4. 冷开快照核对手调键。现有 `rig prepare` 要求源工程与请求都处于 Object Mode。在自有窗口切换模式再捕获，或把已捕获的 Pose Mode 工程另存为 Object Mode 工作副本。对新的文件计算 SHA，核对帧、Action 和 Slot 后提交续作。冷开最终候选，核验手调范围、后续新增键和其余原 Action。

## 从带皮参考到可审看的两段动作

以 Y Bot 的带皮 T-pose 建档，分别检查完整无皮步行与挥手。用同一份 Profile 在 Myona 和 CesiumMan 各生成两份候选。步行按角色腿长保留行进，挥手保留原地意图。保存逐候选时基、原生 Action/Slot 和冷开回执。动画师在任一原生动作上试改一帧，再让 CLI 从固定快照继续下一帧。交付可编辑候选和同机位视频。 四路完整转换与独立冷开（`work/audit/<batch>/`） · 窗口手调与公共接续（`work/audit/<batch>/`） · 冻结需求与审看标准（`work/audit/<batch>/`）
