---
name: blender-asset-manager
description: 管理 Blender 资产分类、命名、标签、Catalog 与索引；用于整理、入库、命名规划及分类迁移，不用于普通建模渲染。
metadata:
  version: "0.1.0"
---

# Blender 资产管理

建立可检索、可追溯的分类与名称，产出索引、逐项命名/Catalog 映射或已验证副本。保护源包、身份及引用。

先读当前项目已有资产标准；新库可采用随包[通用基线](references/asset-standard.md)。已有规则与用户当前要求优先；示例标准不授予操作许可。空白模板在 assets/templates，需填实才能使用。

库根/工程/批次目录由用户当次明确，限定检查，不默认扫描磁盘。项目有当前分类指针时回读目标记录；没有就限定盘点，不虚构历史索引。证据结构见[证据导航](references/project-evidence.md)。记忆/Wiki 是可选宿主能力，缺失时正常推进。

分类命名读[分类与名称](references/classification-and-naming.md)；修改 Catalog、共享索引或物理路径读[Catalog 与执行](references/catalog-and-execution.md)。分类建议不需启动 Blender。

## 身份与范围

- 文件数不等于资产数，一个 blend 可包含多个独立资产。
- 复用 asset_id；无 ID 时以文件/类型/数据块/library 定位，首次登记才生成持久 UUID，不重复发号。
- 用途决定分类，来源/作者/许可/生命周期独立字段，包编号不当资产 ID。
- Catalog UUID、资产 ID、内容 SHA、路径与作业 ID 互不替代；同路径不同 UUID 或跨库同 UUID 不自动冲突。
- 文件级分类、按 ID 的管理记录和 CLI 发布索引各有结构；null 是未采集，空数组是确认无项。

## 形成方案

依据优先：用户确认、当前数据块用途、有来源记录、待确认候选。文件名可支持候选，不能证明作者、许可或功能。建议列身份、原/拟分类、标签、依据、未决项。

新发布副本按采用标准命名；源包及已有资产保留原名。更名/迁移填 assets/templates/migration-map.csv，含旧新路径/数据块/Catalog、前 SHA、备份及状态。显示名和逻辑分类调整不自动授权物理迁移。

`python <技能目录>/scripts/check_names.py --input <候选JSON绝对路径>` 只读检查随包基线的新发布名；项目规则不同先确认，不能批量修旧名。预检不分配 ID、不更名、不证明分类/许可/发布质量。

## 执行与验收

blend 操作用[Blender CLI](../blender-cli/SKILL.md)和当前合同；MCP 可用时核对会话，跨入口保护 GUI 未保存状态。批次证据放授权工程内、资产库外的新目录。

常规读取、草案、新副本与已授权可逆修改直接推进；目标冲突、删除、范围扩张或外部发布按本次授权处理，历史授权不外推。同名/同大小/同字节/.blend1 不授权删除。

草案验身份/字段/依据/歧义；实际写入另验非目标保护、依赖、独立重开及用途所需导入/预览。交付映射、文件、pass/fail/not_run/not_applicable 和理由，命名规划通过不当全库合格。
