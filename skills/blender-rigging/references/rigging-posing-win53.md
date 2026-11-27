# 已有角色的摆姿、动作迁移与窗口接续

用 Windows Blender 5.3.0 的 Rigging 专项开发候选接续已有 C94 控件或普通原生 FK 人形。先定动作、地面、审看片段与时基。把原骨、蒙皮、基础 Action 和原生 Slot 留在可独立重开的工程里。

## 匹配宿主与候选包

先运行 `rigging-workflow phase3 describe` 检查宿主和可用入口。执行 `run` 或 `live-open` 时，用发行方给出的包指纹核对当前 ZIP，并在命令族之前传全局旗标。安装、构建示例以及 JSON 模板见 [CLI 命令与请求](https://github.com/tsist/blenderctl/blob/main/docs/RIGGING_WIN53.md) 和 [绑定候选包说明](https://github.com/tsist/blender-rigging-workflow/blob/main/docs/WIN53_POSING.md)。

```powershell
$ctl = './blenderctl.ps1'
& $ctl rigging-workflow phase3 describe
& $ctl --blender $blender --rigging-package $package --rigging-package-sha256 $packageSha rigging-workflow phase3 run --params $request
```

`run` 接收 UTF-8 JSON，顶层填写 `operation` 与 `parameters`。已有工程还需 `file`、`expected_sha256`、`scene` 及 `view_layer`。创建候选时用一个新的绝对 `.blend` 路径。读取回执中的候选 SHA、原生 Action/Slot 和独立重开结果。

## 先校验素材再迁移动作

1. 用 `retarget-fbx-inspect` 读取已获许可的 FBX，确认原生 Take、Slot、源时基和骨架。对同一源角色，从带蒙皮的参考 FBX 用 `retarget-profile-build` 建档，再用同一 Profile 试跑两段仅动画的片段。选一段行进和一段原地动作，分别检查方向、节拍和位移意图。
2. 从已保存的 Object Mode 工作副本运行 `retarget-body`。显式 Take 或源 Slot 以本次实际导入的名字为准。重名时按回执中的组合选择。分别记下导入源槽和写回的目标槽。
3. 按片段选择身体迁移的根旋转方式。有明确整身翻转时在支持的 C94 目标上验三轴模式。普通原生 FK 用明确的身体骨骼映射，并核对现有控制图能独立回放。检查目标按原帧率保留起止时间。

## 布置动作和检查支撑

- 在原生控制器上布置迈步、接重、伸手三拍。核对脚掌、骨盆、胸廓与手的方向，再用 `three-beats` 提交递增的帧号、明确的控件和通道值。用 `shoulder-pose` 检查肩带跟随与旁路，用 `rom-inspect` 回读髋、肩和胸腰活动提示。把 ROM 与动作、负重和参考坐标一起记录。
- 继续用原 Action 作对照。普通走跑的已有足部标定可在 `gait-contact` 中按问题编号和帧域生成联合候选。局部有意滑步可用切向释放保留设计轨迹。普通原生 FK 的脚跟走 `fk-heel-contact`，核对原骨、权重和 Slot。
- 不同帧率按源秒数对齐。固定机位对照着地、接重与伸手，分别记录骨锚点与实际评估网格的接触结果。标明测点、参考面、帧域、受影响通道，并把人工改键次数与机器用时分别记账。另起进程重开复查绑定与变形，交付可编辑候选和同机位审片。

## 把窗口里的手调交回 CLI

1. 用 `live-open` 打开独立、有明确所有权的 Blender 工作窗口。在窗口里暂停播放，将手调写入正确的原生 Action/Slot。
2. 用 `live-status` 核对场景、帧、模式、Action、Slot 和 FCurve 指纹。用最新 `expected_context` 提交 `live-capture`。修改键值、手柄或工作上下文后重新读取状态。按原请求 ID 用 `live-result` 取回同一回执。
3. 独立重开快照确认手调键，再交下游。用通用 `rig prepare` 续作时，先从窗口或已捕获副本保存 Object Mode 版本并计算新 SHA。续作后重开候选，核对手调范围、续写键和保留的 Action。`live-close` 收尾并保存恢复副本。
