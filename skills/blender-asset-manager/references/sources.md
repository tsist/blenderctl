# 规范文档与台账入口

本文件列出资产管理规范、CLI 契约、填写模板、现库台账与盘点脚本的精确位置。实际任务以当前磁盘文件、`blenderctl.ps1` 实时返回与资产库最新状态为准。

## 规范正文与填写模板

- **项目资产管理规范**：`<PROJECT_ROOT>/docs/asset-management/STANDARD.md`
- **资产文档总览与变更日志**：`<PROJECT_ROOT>/docs/asset-management/README.md` · `<PROJECT_ROOT>/docs/asset-management/CHANGELOG.md`
- **正式资产卡片模板**：`<PROJECT_ROOT>/docs/asset-management/templates/asset-record.json`
- **物理迁移映射模板**：`<PROJECT_ROOT>/docs/asset-management/templates/migration-map.csv`
- **批次变更与验收模板**：`<PROJECT_ROOT>/docs/asset-management/templates/change-batch.md` · `<PROJECT_ROOT>/docs/asset-management/templates/acceptance.md`

## CLI 工作流文档与 Schema

- **版本发布、浏览器拖入与零件材质取用手册**：`<PROJECT_ROOT>/docs/cli/workflows/asset-versions.md`
- **资产工作流与命令契约**：`<PROJECT_ROOT>/docs/cli/阶段6资产工作流.md` · `<PROJECT_ROOT>/docs/cli/命令契约.md`
- **事务提交、恢复与文件替换**：`<PROJECT_ROOT>/docs/cli/事务与恢复.md` · `<PROJECT_ROOT>/docs/cli/文件替换与混合事务.md`
- **JSON Schema**：
  - `<PROJECT_ROOT>/docs/cli/schemas/asset-record.schema.json`
  - `<PROJECT_ROOT>/docs/cli/schemas/transaction-plan.schema.json`

## 现库台账与盘点脚本

- **当前分类台账入口**：`<PROJECT_ROOT>/runtime/asset-management/current-classification.json`
  - 读取现库历史文件级记录、外部包分组与最新治理指针时，先读这个入口文件，顺着指针查看对应记录。
  - 单件资产的正式卡片按需建在 `<PROJECT_ROOT>/runtime/asset-management/records/<asset_id>.json`。
- **只读全库磁盘盘点脚本**：`tools/blender/audit_assets.py`
  - 需要摸清 `<ASSET_LIBRARY>` 磁盘全貌时运行，会在独立审计目录输出文件列表、Catalog 列表与同名候选清单。
  - 只处理少量明确目标，或需要核对数据块、材质槽与贴图依赖时，直接调 `./blenderctl.ps1` 做定点只读检查。
