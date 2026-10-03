---
name: blender-material-authoring
description: 制作、修改并验证 Blender 材质、着色节点和贴图，组织参考、表面分层、映射、色彩与交付；PBR 是本技能章节，不替代建模、灯光构图或资产编目。
metadata:
  version: "0.1.0"
---

# Blender 材质制作

先明确用途/引擎、对象/区域、实际尺寸、观察条件、编辑性与交付。几何法线、坏面、轮廓、厚度或 UV 引起的问题回建模/映射阶段。

## 制作来源

沿用原工作规范的原生生图优先路线：所需空间通道/复杂遮罩首先用当前宿主的原生生图工具。只有实际缺失、429 或确认服务故障等受阻时，才在当前授权及配置内用可用替代服务；质量不足、批量或速度不是切换理由。本规范不继承维护者的第三方服务授权。

Blender 制作遮罩的例外限简单渐变或明确几何/数学输入的计算域；复杂裂纹/风化即使能用 Noise/Voronoi 拼成也沿用生图。无空间变化物性可明确常量；合同要求图像时按约定制作。完整定义见[PBR 规范](references/pbr/framework.md)。用户本次指定其他来源时记录并遵从。

## 插件与 CLI

先按[最高指导](../blender-work-methodology/references/plugin-cli-work-units.md)评估连续工作单元，再准备完整输入与有限候选。宿主负责生图，插件消费图层/映射/节点，CLI 管身份/作业/证据；插件不假设访问原生生图会话。

读[操作章](references/pbr/plugin-cli-authoring.md)选 run/batch/study/template-save，检查旋钮是否生效；看诊断/差异/阶段/图像后人工决定，局部筛选不代正式验收。范围见[工具路由](references/pbr/tool-routing.md)。

PBR先读[规范](references/pbr/framework.md)，按需转[参考/图像](references/pbr/reference-and-images.md)、[通道合同](references/pbr/channel-contract.md)。小调参复用合格图层，不重新制作整套；风格化按目标，PBR数值不是非PBR配方。

参考冻结 → 几何/UV → 图层/常量 → 插件组装 → 缩略图大特征 → 有限局部对照 → 换光/通道隔离 → 正式目录重开重渲 → 反馈/交付。失败回对应来源阶段。

[图像门卫](scripts/image_gate.py)只读解码/尺寸/字节/SHA：生成最长边≤2048，会话预览≤2MiB，原件/数据图/有损预览分开。需要可选 Pillow，缺失报告依赖，不自动安装。漂亮图不证明校准、配准或权利。

## 验收与证据

技术、视觉、性能、时序、依赖复现、权利与用户反馈分别记录。没有反馈不写成用户认可。许可绑定素材/用途，入库转[资产管理](../blender-asset-manager/SKILL.md)。

[赤陶经验](references/pbr/terracotta-case.md)、[釉瓷经验](references/pbr/bluewhite-glaze-case.md)为去标识历史指导，原件/私人对话未公开，参数非新材料默认。证据见[公开导航](references/pbr/wiki-and-evidence.md)。宿主记忆/Wiki 是可选能力，缺失时正常完成，不声称已读写。
