---
name: blender-cli
description: 使用 blenderctl 创建、检查、修改 Blender 工作副本，执行渲染、动画、模拟、格式交换与可恢复流水线；用于实际操作已保存工程并验证产物，不用于实时 GUI 操控或纯知识解释。
metadata:
  version: "0.2.0"
---

# Blender CLI

用公开 blenderctl 完成目标，以实际产物验收。配套基线为 CLI **0.54.1**、Material Workflow **0.7.0**，原生运行已验 Windows / Blender **5.2.1 LTS**；注册存在不代表功能、视觉或跨平台通过。

## 定位工具

仓库根采用用户指定目录或 `BLENDERCTL_ROOT`，不假定个人磁盘布局；须含 `tools/blenderctl/cli.py` 与 `docs/cli/schemas/`。Python 3.12+；Blender 由 `--blender`、`BLENDER_PATH`、兼容 portable 目录或 PATH 选择。缺运行时如实报告，不擅自下载、安装或替换环境。

```powershell
$root = $env:BLENDERCTL_ROOT
$ctl = Join-Path $root 'blenderctl.ps1'
& $ctl --version
python '<技能目录>\scripts\discover.py' --root $root --command material.run
& $ctl material describe --operation study --section study
```

发现脚本读取公开合同，不启动 Blender或认证 GUI 状态；安装到其他目录后仍用显式 root/环境变量。按需查[领域导航](references/domains.md)、当前帮助与 Schema，不重复灌入全量节点树或默认字段。

先从当前索引、领域describe或命令help的真实返回确认接口，再定位真实Schema路径；不猜命令、文件名或报告字段层级。同任务记录已读接口、版本、Schema路径/SHA和适用输入，未变化时保留摘要与引用；版本、输入或外部文件变化后复读受影响章节。`--compact`仅按实际支持的材质接口使用，不假定extension.run、project.verify等支持。

## 材质工作单元

单目标 `material.run`，明确多对象/槽/面清单 `material.batch`，有限数值对照 `material.study`，结构导出 `material.template-save`。先 `material describe` 获取相关章节或完整请求；`--section` 与 `--schema` 不同时给。

study 点名 assignment/layer，最多8候选、每候选6视图、总32视图。参数仅含 normal_strength、roughness、metallic、ior、coat_weight、coat_roughness、coat_ior；White 只隔离点名层颜色。先读差异/语义风险/无效旋钮和进度，再看图人工判断。贴图覆盖 roughness 常量时，scale/bias 用 run/batch。

材质执行/读取优先 `--compact`，完整结果仍在磁盘，摘要含 file/SHA/bytes 引用。显示失败不重新制作。长任务用同一 jobs 根的 `job wait <id> --wait-seconds 30`（0–55秒），仍运行时读真实阶段/恢复引用，不重复提交。

## 执行与验收

先核对 Scene/View Layer、对象/槽/面、模式、共享数据、依赖及实际名称。CLI 的 `SAVED_DISK_V1` 看不到未保存 GUI 编辑，跨入口用独立持久快照。

输入绝对路径、真实 SHA 和完整资源清单，不默认空资源。修改产生新候选，源保持不变。新批次指定独立 `--jobs-dir`，事务另指定并复用 `--transactions-dir`；全局选项在子命令之前，复杂请求用 UTF-8 JSON。

参数化复用实际存在的请求生成器/报告适配器，按当前Schema验证输出；缺失构建器是待开发能力，不编造命令或强制每例开发。UV修改优先当前model.prepare合同的uv.layer/uv.set，不为普通UV操作自写通用RNA签名。资源live/orphan分类须有引用证据并标未知项，孤立不等于可删除；封包保留相对依赖与复制关系，按实际修改影响面核对非目标数据。

完整JSON留盘，交互返回实际报告路径、SHA及本次决策必要字段，按真实字段层级投影；不隐藏错误、异步未完状态或保护信息。

同时检查退出码、JSON ok/error、整体及逐项状态、异步终态。提交成功/文件存在不等于验收；只消费已验证产物。独立重开核对目标及非目标，实际解码查看图像，动画/模拟检查关键和必要中间状态，交换用目标读取器验。技术、视觉及用户反馈分开记录。

具体执行见[执行与验收](references/execution.md)。仅把当前 ALLOWED 命令放入 DAG；材质、事务、宿主管理及审阅入口独立。恢复/取消见[恢复与发布](references/recovery.md)：绑定源、资源、实现、任务和视图身份，不改旧 SHA；取消只作用于本任务作业。

计时区分总墙钟、顶层作业和内部阶段；嵌套case/view已包含父级时不重复累加。工具秒数不能解释全部会话时间，缺遥测区间标未测，不归因模型、等待或GPU。历史耗时复盘仅提出待匹配验证的优化候选，原证未公开，不代表已实现提速；见[效率章节](../blender-work-methodology/references/efficient-work-units.md)。

交付保留 blend、纹理、frames/VDB/usdc、缓存、回执及上游作业。先检查消费者再清理，缺全局反向消费者索引时注明影响面。资产命名/入库转[资产管理](../blender-asset-manager/SKILL.md)。无隔离不冒称隔离，未验平台/断电一致性/硬配额不得宣称支持，安全拒绝不能换入口绕过。
