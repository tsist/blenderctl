# 公开来源与当前能力边界

本技能是与 Hard Surface Workbench dev21 同步的公开开发版指导，版本 `0.1.0-dev.21-r1`；它不安装插件，不携带可执行生产请求，也不授予过去任务的许可。

- 软件来源：[Hard Surface Workbench](https://github.com/tsist/blender-hard-surface-workbench)
- 技能来源：[blenderctl companion skills](https://github.com/tsist/blenderctl)
- 插件协议版本保持 `0.2.0`；协议版本不是开发候选的唯一身份。
- 候选标识 `reflection-anchor-diagnostic-dev.21-r1`；执行前必须核对实际源码、当前 Schema 和相应回执，不仅比较版本字符串。
- 固定公开源码提交：[1bee43d7c6acf6b7cf0c08f6eb71d589580aed94](https://github.com/tsist/blender-hard-surface-workbench/tree/1bee43d7c6acf6b7cf0c08f6eb71d589580aed94)，对应 Git tree `6aecd3c203a31e872d047ee0e40cc833a4d8d9da`。
- [同提交开发计划](https://github.com/tsist/blender-hard-surface-workbench/blob/1bee43d7c6acf6b7cf0c08f6eb71d589580aed94/docs/development-plan.md)记录 C1 暂停而非完成；新实用案例重新确认参考。
- 运行时和测试源码与前一提交保持一致，已完成的本地完整 HOST 回归为 1489 通过、2 个可选跳过；当前提交仅为 CI 补齐固定版本测试依赖。这是代码检查，不包含 dev21 原生运行或视觉验收。CI 以该提交的实际运行结果为准，不由本技能预先宣称通过。

## 证据解释

原生技术、HOST、图像查看、覆盖充分性、表面品质及用户接受分别记录。已报告的 dev19 两个独立参数编辑原生整链和 dev20 表面导出/法线绑定，是限定历史技术证据；原始私有工程与案例回执不公开随包提供，因此不是本仓库可直接复现的案例集。

dev20 实际观察的广阔平面高光覆盖不足，整体形体/高光未接受；发布技能不改变这一结论。dev21 reflected-anchor 灯架是 opt-in HOST 实现，未原生运行，反射预测不能当成实际亮像素覆盖或商业交付资格。

## 排除范围

不发布私有工作区路径、工程/素材/参考图、个人资料、生产参数、历史批准、私有研究卡 ID 或原始任务回执。源码仓库、公开 HOST 测试与本指导只能证明其实际覆盖范围；游戏派生、任意网格修复、连续编辑组合和用途资格仍须独立实现与验证。
