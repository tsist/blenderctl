# 公开来源与当前能力边界

本技能是与 Hard Surface Workbench dev22 同步的公开开发版指导，版本 `0.1.0-dev.22-r1`；它不安装插件，不携带可执行生产请求，也不授予过去任务的许可。

- 软件来源：[Hard Surface Workbench](https://github.com/tsist/blender-hard-surface-workbench)
- 技能来源：[blenderctl companion skills](https://github.com/tsist/blenderctl)
- 插件协议版本保持 `0.2.0`；协议版本不是开发候选的唯一身份。
- 候选标识 `research-evidence-host-dev.22-r1`。dev22 新增通用 HOST research-evidence 工作单元；不是完整实验 solver 移植，不改变历史 native 或产品资格。
- 固定 dev22 公开源码提交：[797d8fcd6504b6809a11b6103b16e8a385046162](https://github.com/tsist/blender-hard-surface-workbench/tree/797d8fcd6504b6809a11b6103b16e8a385046162)，对应 Git tree `219fb3ec11505e979d3a1ae9ddbd817cfa270694`。
- [同提交开发计划](https://github.com/tsist/blender-hard-surface-workbench/blob/797d8fcd6504b6809a11b6103b16e8a385046162/docs/development-plan.md)说明当前开发范围与未完成关口；新设计仍需当前参考冻结与许可。
- 历史 dev21 源码为 [1bee43d7c6acf6b7cf0c08f6eb71d589580aed94](https://github.com/tsist/blender-hard-surface-workbench/tree/1bee43d7c6acf6b7cf0c08f6eb71d589580aed94)，对应 Git tree `6aecd3c203a31e872d047ee0e40cc833a4d8d9da`；仅用于历史追溯。
- 匹配 dev22 源码的本地完整 HOST 回归发现 1525 项，其中 1523 通过、2 项因 slvs 未配置而跳过。34 项新增模块检查属于该回归的子集；22 项独立反例另计，不与完整回归混加。远端 CI 在本次发布核验时仍为 pending，不能据本地结果宣称远端 CI 通过。技能结构检查和 HOST 回归均不包含 Blender 原生运行、反射验收或用户成品接受。

## 证据解释

原生技术、HOST、图像查看、覆盖充分性、表面品质及用户接受分别记录。已报告的 dev19 两个独立参数编辑原生整链和 dev20 表面导出/法线绑定，是限定历史技术证据；原始私有工程与案例回执不公开随包提供，因此不是本仓库可直接复现的案例集。

dev20 实际观察的广阔平面高光覆盖不足，整体形体/高光未接受；发布技能不改变这一结论。dev21 reflected-anchor 灯架是 opt-in HOST 实现，未原生运行，反射预测不能当成实际亮像素覆盖或商业交付资格。

## 排除范围

不发布私有工作区路径、工程/素材/参考图、个人资料、生产参数、历史批准、私有研究卡 ID 或原始任务回执。源码仓库、公开 HOST 测试与本指导只能证明其实际覆盖范围；游戏派生、任意网格修复、连续编辑组合和用途资格仍须独立实现与验证。
