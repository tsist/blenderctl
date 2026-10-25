---
name: blender-material-authoring
description: 制作、修改并验证 Blender 材质、着色节点和贴图，按需求组织常量、图像、程序信号与分层PBR；不替代建模、灯光构图或资产编目。
metadata:
  version: "0.3.0"
---

# Blender 材质制作

## 每次开工先找参考并冻结质量标准

所有来源（含程序化、常量和DeepSeek执行）先读[参考与质量门槛](references/pbr/reference-quality-gate.md)：每个新任务主动寻找/筛选并实际看过适用主参考和必要局部，再冻结带版本的质量brief。参考可以来自真实工艺照片、材质/纹理网站、优秀CG或用户图；用于风格、材料层次与完成度，不要求木纹/瑕疵位置逐处一致。

按七个质量维度逐项对照参考与当前新渲染，关键项有差距或未观察就不通过；不以模板名、图数、数值误差或“2K限制”放宽目标。小调参沿用本任务已核参考，目标变化保留旧版并更新依据。无网络时由主代理提供冻结参考包，无看图能力时交接并保持视觉未完成。

先明确用途/引擎、对象/区域、实际尺寸、观察条件、可编辑性与交付。几何法线、坏面、轮廓、厚度或UV引起的问题回建模/映射阶段。来源按需求选择，不把生图作为所有PBR任务的前置。

## 来源与入口

| 来源 | 适用情形 | 关键检查 |
|---|---|---|
| constant | 无空间变化的物性、纯色、开关 | 明确值/单位；需要图片时确定性导出 |
| image | 指定图案、文字、语义浮雕、照片或合格贴图 | 来源/权利、配准、色彩与数据语义；需要生图时优先宿主原生工具 |
| procedural | 可由坐标、参数和几何依据表达的基材/旧化 | 固定尺度、方向、seed；实际看渲染，噪声不自动等于合理风化 |
| hybrid | 特殊图案与程序基材/遮罩组合 | 明确通道来源、区域、覆盖和烘焙边界 |

先读[PBR规范](references/pbr/framework.md)。程序化/混合2.0先读[基础信号与分层实做](references/pbr/procedural-signals.md)，再读[程序化PBR](references/pbr/procedural-pbr.md)，图像分支按需读[参考/图像](references/pbr/reference-and-images.md)，通道读取见[通道合同](references/pbr/channel-contract.md)。特殊图案无法可靠完成时交接图案输入，先完成其他已授权部分，不用普通噪声替代目标。没有生图工具不阻断适合程序化或常量的目标。

## 插件与CLI

先按[工作单元指导](../blender-work-methodology/references/plugin-cli-work-units.md)准备完整输入、连续操作和有限候选；插件构建材质，CLI管理身份/作业/保存和证据，宿主在需要时负责生图，插件不假设访问其会话。

配套公开候选为CLI0.64.0、Material Workflow0.8.0；技能包0.3.0包含材质指导0.7.0的参考质量增补，版本含义不同。执行前用BLENDERCTL_ROOT定位CLI并通过describe/help发现能力，不能仅凭版本或节点注册宣称可用。旧静态图层的run/batch/study/template-save与2.0的procedural-run/procedural-export是不同合同；见[操作章](references/pbr/plugin-cli-authoring.md)和[路由](references/pbr/tool-routing.md)。

## 执行与验收

需求/参考 → 区域/UV/共享检查 → 来源与通道清单 → 信号/图层/参数 → 读新渲染并迭代 → 所需通道烘焙 → 仅磁盘图和声明参数重建 → 保存重开、换光/局部对照 → 封包和反馈。可编辑author与reconstructed分别保存；内腔任务检查深处、口沿、内外法线和玻璃壁厚。

可靠切线Normal采用真实Tangent/+Y烘焙，不能因紫蓝外观就宣称校准。已含Height的Normal重建时不再重复Bump。玻璃分别保留Alpha/Transmission/IOR与粗糙度，不降低Alpha冒充透射。首期写实PBR，NPR、液体、复杂焦散、体积与自动UV另行研究验证。

DeepSeek或其他无生图执行者必须实际读新图→迭代→烘焙→rebuild检查。view_image或等价观察不可用时交接路径、SHA和待判断点，视觉项保持未完成，不把数值/工具completed当成已看图。

[图像门卫](scripts/image_gate.py)检查解码/尺寸/字节/SHA：生成最长边≤2048，会话预览≤2MiB，原件/数据图/有损预览分开。Pillow为可选依赖，缺失报告，不自动安装。

## 证据与维护

技术、视觉、性能、时序、依赖复现、权利和用户反馈分列。没有反馈不写用户认可；来源许可绑定素材与用途，入库转[资产管理](../blender-asset-manager/SKILL.md)。历史案例中的统一生图前置已由按需路由更新，既有合格图层仍可复用，案例参数不是新材质默认值。

本包不含维护者私有工程、对话、任务态或审计。有限验证及未验范围见仓库`docs/MATERIAL_WORKFLOW.md`；[赤陶](references/pbr/terracotta-case.md)与[釉瓷](references/pbr/bluewhite-glaze-case.md)是去标识历史指导，不是公开可复现实验。维护者工作区14部件/126张2K通道与36张双引擎换光图仅为限定证据；私有工件不随包发布。公开候选代码已完成解压、隔离安装与运行回归；DeepSeek已按文件阶段完成14部件/126张2K与重建，主线程检查42张最终图并保留差异。过程包含主线程诊断纠偏，不称无协助盲测，也不等于已发布Release或任意模型通过。以当前报告和实际新图为准。
