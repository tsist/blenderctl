# 动画与模拟部门

负责做出节奏舒服、接触扎实的动作表演，以及服从表演节奏的布料、毛发、刚体等次级物理动态。

## 从上游收什么

- 绑定部门交来的可操作角色或机构工程（路径与 SHA）、控制器说明与静置姿态。
- 镜头与灯光部门给的镜头表（帧率、帧域、机位与切点），或音乐节拍信息。

## 选路与专项技能

主表演归动画，物理跟着表演走：

- **关键帧表演、双人互动与音乐卡点**：读 [blender-animation-workflow](https://github.com/tsist/blender-animation-curves-workflow/blob/main/skills/blender-animation-workflow/SKILL.md)。先定关键姿态、落地支撑和接触帧，再顺曲线；多人对戏或配合灯光闪动时用事件计划锁定相对时差；有现成动捕或片段时按骨架映射导入再修脚底滑动。查 CLI 动作运行 `discover --product animation`。
- **刚体散落、流体颗粒、衣物绳索与角色发裙次级动态**：独立旗帜、披布、绳索、刚体散落与粒子表面重建读 [blender-physics-workflow](https://github.com/tsist/blender-physics-workflow/blob/main/skills/blender-physics-workflow/SKILL.md)（几何节点时序模拟兼看 [blender-geometry-nodes](https://github.com/tsist/blender-geometry-nodes-workflow/blob/main/skills/blender-geometry-nodes/SKILL.md)）；带骨架变形角色的胸臀弹性与发裙表面动态优先走 [blender-rigging](../../../blender-rigging/SKILL.md) 的 `elastic_action.BAKE` / `surface_dynamic_action.BAKE`（若改交物理工作台，先将角色碰撞体烘焙为关键帧网格再接入）。统一先锁死主动作与碰撞体范围，跑小帧段小样确认受力和碰撞，通过后烘焙完整磁盘缓存并独立重开核对。查动作运行 `discover --product physics`。

## 交给下游什么

已有 C94 或普通原生 FK 角色时，先按原控件布置关键姿态与接触。区分普通走跑和有意滑步，再用同机位并排评估脚跟、骨盆与胸廓的接重。把手调写入原生 Action/Slot，再从捕获副本续作。 [原控件摆姿与窗口接续](../../../blender-rigging/references/rigging-posing-win53.md)

- 保存好的动画工程副本（路径与 SHA），标明角色名、Action 名、Action Slot、帧率与起止帧。
- 事件时间表（关键接触帧、节拍点）与已烘焙的物理缓存目录及回执。
- 一段可播放的预览视频。

## 什么时候给用户看

- **单角色动作委托**：这是角色的**审看②（动作预览）**。导出一段能直接播放的动作预览请用户确认节奏、重心和表情。
- **镜头或成片委托**：动作与相机合在一起进入**分镜预演**或**粗剪**给用户看。

## 出了问题回哪里修

- 动作飘、节奏拖沓、脚底滑步或手指穿插：留在动画环节修关键帧与支撑曲线。
- 布料炸开、穿模或缓存重开后跳帧：留在模拟环节调碰撞体厚度、子步数并重烘受影响区间。
- 正常姿态下关节变形破面或控制器锁死：退回绑定部门修权重或约束。
