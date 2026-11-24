# 建模部门

负责做出比例准确、结构清楚、留好编辑余地的三维白模或程序化几何。

## 从上游收什么

- 前期参考部门给的主效果图、正交视图、尺寸表、部件清单与一致性核对单；返修时收用户认可的上一版工作副本与问题定位。

## 选路与专项技能

按部件特点选最省力又够用的路线，同一件作品里不同部件可以混用：

- **规则硬表面、精确装配与卡线细分**：读 [blender-hard-surface-modeling](https://github.com/tsist/blender-hard-surface-workflow/blob/main/skills/blender-hard-surface-modeling/SKILL.md)。从稀疏四边面控制笼起步，用修改器栈控制倒角、细分与法线，留住可编辑主件。查 CLI 动作运行 `discover --product hard-surface` 或 `discover --product native`。
- **复杂高面数主体、雕塑造型的角色或有机道具**：先读 [tripo-studio-opencli](https://github.com/tsist/tripo-studio-opencli/blob/main/skills/tripo-studio-opencli/SKILL.md) 在网页端生成并导出本地候选，再读 [blender-modeling-workflow](https://github.com/tsist/blender-modeling-workflow/blob/main/skills/blender-modeling-workflow/SKILL.md) 导入 `.blend`、比较候选并按部件决定直接沿用还是重建控制笼。查动作运行 `discover --product tripo-studio` 与 `discover --product modeling`。
- **要反复改数量、路径或散布规则的结构**：读 [blender-geometry-nodes](https://github.com/tsist/blender-geometry-nodes-workflow/blob/main/skills/blender-geometry-nodes/SKILL.md)。把常改的参数和材质槽暴露到修改器面板，下游需要静态网格或模拟缓存时再从主件派生副本。查动作运行 `discover --product geometry`。
- **送审前独立把关**：先跑 `model inspect --qa-profile game`（或 `render`）做几何体检，再读 [blender-modeling-review](https://github.com/tsist/blender-modeling-review/blob/main/SKILL.md) 对照参考单逐项看图检查。查动作运行 `discover --product review`。

## 交给下游什么

- 保存好的可编辑白模工作副本（路径与 SHA）和部件对象名清单。
- 给烘焙或外部引擎用的派生固定网格（需要时单独生成，标明它对应哪个主件）。
- 白模对照图与几何体检结果。

## 什么时候给用户看

- **可复用资产**：这是**审看①（白模）**。摆出同机位的控制笼、细分白模、移动高光图，再演示一次局部改型证明网格好改，请用户确认形体和布线。
- **角色、镜头或场景配角**：自检通过后直接交绑定或 UV 与材质部门。

## 出了问题回哪里修

- 轮廓走样、比例不对、零件穿插或壁厚穿帮：留在本部门改大形和控制笼。
- 多张参考图本身矛盾导致无法闭合：退回前期参考部门统一规格。
