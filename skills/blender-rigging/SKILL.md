---
name: blender-rigging
description: 使用 Blender 角色工作台和受管 CLI 制作及维修骨架、蒙皮、控制、面部与辅助动态。适用于有明确输入和动作验收目标的绑定及重定向任务。
metadata:
  version: "0.42.3-agent-v003"
---

# Blender 角色绑定

从角色用途和实际动作出发，检查骨架、皮肤、表情、辅助动态及已有动作。先确认已保存的工程、目标对象、真实版本和输出位置。固定版 0.40.1 的面板、重定向与安装使用独立仓库的[完整绑定技能](https://github.com/tsist/blender-rigging-workflow/blob/main/skills/blender-rigging/SKILL.md)与[设置说明](https://github.com/tsist/blender-rigging-workflow/blob/main/docs/SETUP.md)。

## Windows Blender 5.3.0 的 v003 命令行预览

按[公开 CLI 接入说明](https://github.com/tsist/blenderctl/blob/main/docs/RIGGING.md)将 `BLENDERCTL_RIGGING_ROOT` 指向骨骼仓库的检出根。用 `rigging-workflow discover` 查当前命令，以各子命令帮助和实际请求字段为准。旧作业从原版源码、宿主及已保存的回执恢复。

- 骨架审核通过后，新角色使用 `candidate-entry` 作体素初绑、同作业部件转权和保存交付。几何上有歧义的部位留待处理，继续独立且通过检查的域。
- 用 `candidate-selected-skin inspect` 预览选骨局部重算的点域。显式指定网格和骨骼，需要时调整范围。`execute` 只写指定网格内选区中的选中骨骼组。保留其他骨骼的权重原值，允许每点保存权重总和超过 100%。检查接缝两端并在冷开工程中重放相应动作。
- 作者返工、模块重建及逐点维修用 `candidate-repair`。同骨架的迁权用 `candidate-skinning-policy` 检查供体、审阅范围并采用。沿用源指纹、角色和范围审核，只提交经确认的候选。

睫毛和嘴唇粘连、穿插或有尖褶时，记录对象、帧、可见位置和工作副本，交人工修复。继续验收其他部位的相关压力动作和过渡姿态。保存可编辑工程并独立重开，核对原输入及非目标权重，实际检查漏权、明显串权、粘连、塌陷与复位。
