# 领域导航

先定位 BLENDERCTL_ROOT，使用当前帮助/Schema。下表只导航，注册不证明实际验证。

| 目标 | 入口 | 关键边界 |
| --- | --- | --- |
| 工程/快照 | inspect/query/diff、project verify/prepare-copy | 磁盘状态，不观测 GUI 未保存内容 |
| 链接/Override | project link prepare、override prepare/resync | 版本、层级、属性和回执 |
| 依赖/重连 | dependency audit/plan-relink/prepare | 有限闭包，未知保持 partial |
| 场景/网格/UV/节点 | scene/model/node prepare/inspect | 上下文、白名单、原始/求值区别 |
| 材质 | material describe/run/batch/study/template-save | 已有 UV、静态图像、受控节点、standalone |
| 骨架/动作 | rig prepare/inspect/package、animation sample | 源绑定驱动/槽位/采样域，非任意 IK 或合身认证 |
| 模拟/毛发/体积 | simulation prepare/inspect/bake | 限定求解器/帧窗；hair 操作是 manifest 的 op |
| 跟踪 | tracking prepare/inspect/solve | 帧映射/标定，非任意影片米制保证 |
| 渲染/VSE | render devices/run、media prepare/export | 设备/相机/色彩/质量，检测非渲染成功 |
| 格式交换 | exchange analyze/export/import/convert | 单位/轴向/时间与损失，分析非导出 |
| 资产 | asset prepare/index/plan/preview | 项目标准、身份、来源许可与依赖 |
| 脚本/雕刻 | extension review/approve/run、sculpt review/replay | 明确信任/SHA/隔离及前台艺术验收 |

发现脚本：`python <技能目录>/scripts/discover.py --root <仓库根> --command project.override.resync --schema`。零匹配仅表示当前查询无结果，不据此断言能力不存在。

公开文档：[命令索引](https://github.com/tsist/blenderctl/blob/main/docs/CLI_REFERENCE.md)、[兼容范围](https://github.com/tsist/blenderctl/blob/main/docs/COMPATIBILITY.md)、[材质流程](https://github.com/tsist/blenderctl/blob/main/docs/MATERIAL_WORKFLOW.md)。历史本机文档和能力清单不是此技能依赖。
