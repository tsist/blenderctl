# 已有制作方法索引

这些是从真实项目实战里攒下来的候选经验。遇到相似题型时翻开对应卡片照着避坑；各卡片对应的原始审计记录见 [sources.md](sources.md)。

## 候选方法表

| 方法卡片 | 解决什么问题 | 什么时候用 | 状态 |
|---|---|---|---|
| [drawing-spec](methods/drawing-spec.md) | 多张参考图和尺寸表冲突时怎么定稿 | 概念图驱动的静态建筑外观（另见 [建筑硬表面基础](architecture-basics.md)） | candidate |
| [reference-fidelity](methods/reference-fidelity.md) | 怎么保证做出来的画面真像原参考图 | 用户给了明确效果图、要求高度还原的任务 | candidate |
| [part-reference-chain](methods/part-reference-chain.md) | 多零件载具和机械怎么逐级出参考并边做边拼 | 独立零件多、后续要装配或绑定的硬表面载具与器械 | candidate |
| [motion-sweep-check](methods/motion-sweep-check.md) | 运动件怎么查碰撞、包裹和装配贴合 | 带转动或滑动机构的刚体装配体 | candidate |
| [complex-surface](methods/complex-surface.md) | 厚壳细分件怎么去高光波纹并兼顾后续 UV | 带厚度、有弧度过渡的细分壳体 | candidate |
| [mechanical-interface-detailing](methods/mechanical-interface-detailing.md) | 已经在动的机构上怎么安全地继续加细节 | 多连杆、链条、软管和紧密配合的机械设备 | candidate |
| [white-surface-refinement](methods/white-surface-refinement.md) | 白模结构过了但近景嵌板或提手根部还有碎面怎么查 | 参考驱动的硬表面白模局部精修 | candidate |

## 做新项目时值得顺手观察的问题

| 方向 | 留心观察什么 |
|---|---|
| 建模与改型 | 大形在哪个节点拍板最省返工，改局部时怎么确认没带歪旁边的面？ |
| 灯光与材质 | 怎么把材质自身颜色粗糙度和镜头打光拆开看，用哪张图拍板最准？ |
| 绑定与动画 | 哪几个极限姿态最早把权重毛病暴出来，怎么少刷冤枉权重？ |
| 模拟与几何节点 | 用几帧小样就能试准受力和碰撞，什么时候再开全帧段烘焙？ |
| 返修与格式导出 | 怎么框住改动影响面，在接收软件里重开第一眼查哪几项？ |
