# 前期参考部门

负责把一句模糊的需求变成尺寸明确、视角统一、工艺有据可查的制作依据。动刀建模前先把这一步做扎实，后面才少返工。

## 从上游收什么

- 用户的委托目标、使用场景与参考线索（草图、照片、文字描述或现有工程）。

## 选路与专项技能

- **做全套参考图包**：读 [blender-modeling-reference](https://github.com/tsist/blender-modeling-reference/blob/main/SKILL.md)。先定一张主效果图锁定风格和大形，再补正交三视图、尺寸表和局部放大图。用原生生图或按需用 ComfyUI 出图，传进会话的图片控制在最长边 2048 像素以内。
- **遇到陌生工艺或质量标准拿不准**：先查项目 `tutorial-library/` 和共享 Wiki，找一到三个成熟案例看懂标准做法。库里缺中文教程时，读 [blender-tutorial-scout](https://github.com/tsist/blender-tutorial-scout/blob/main/SKILL.md) 定向检索补充。
- **做工业机械、夹爪或仓储设备需要真实结构参考**：读 [simready-hard-surface-research](https://github.com/tsist/simready-hard-surface-research/blob/main/SKILL.md)，挑选当前任务需要的对象提取分件关系和布线参考。
- **查 CLI 入口**：运行 `blenderctl.ps1 discover --product research` 查看参考与研究相关入口。

## 交给下游什么

- 主效果图与正交制作视图。
- 关键总尺寸、部件拆分清单与装配连接说明。
- 一份一致性核对单：列出标志性轮廓、比例和配色特征，写明在哪个视角检查。多张参考图互相打架的地方，在单子里写清按哪张为准，再交给建模部门。

## 什么时候给用户看

风格开放或从零原创时，主效果图定稿后给用户看一眼确认方向；用户已经给了明确参考图时，整理好尺寸和拆件单直接交建模部门。

## 出了问题回哪里修

- 正视图和透视图结构对不上：回到本部门重出视图或统一规格说明，再动模型。
- 制作中途发现某个机械连接悬空或干涉：回到本部门补一张局部结构参考，明确搭接关系后再改网格。
