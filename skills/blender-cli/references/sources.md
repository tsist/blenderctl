# 契约文档与 Schema 索引

本文件集中列出 `docs/cli/` 下的参考合同、工作流手册与 JSON Schema 位置，需要核对某个底层字段定义时按路径直读。

## 总纲与工程管理

- **快速开始与命令总契约**：`<PROJECT_ROOT>/docs/cli/安装与快速开始.md` · `<PROJECT_ROOT>/docs/cli/命令契约.md`
- **通用化开发清单与请求总 Schema**：`<PROJECT_ROOT>/docs/cli/通用化开发清单.json` · `<PROJECT_ROOT>/docs/cli/schemas/request.schema.json`
- **能力整合交接总览**：`<PROJECT_ROOT>/docs/cli/workflows/能力整合交接-20261124.md`
- **工程开存、依赖重连、链接覆盖与文件事务**：
  - `<PROJECT_ROOT>/docs/cli/S11工程开存与启动.md`
  - `<PROJECT_ROOT>/docs/cli/S04类型化依赖与安全重连.md`
  - `<PROJECT_ROOT>/docs/cli/S05链接与覆盖工作流.md`
  - `<PROJECT_ROOT>/docs/cli/事务与恢复.md`
  - `<PROJECT_ROOT>/docs/cli/通用工作单元与效率优化-20261007.md`
  - `<PROJECT_ROOT>/docs/cli/脚本OS隔离.md` · `<PROJECT_ROOT>/docs/cli/隔离枚举与恢复.md`

## 分领域合同与工作流手册

- **资产治理**：`<PROJECT_ROOT>/docs/asset-management/STANDARD.md` · `<PROJECT_ROOT>/docs/cli/阶段6资产工作流.md` · `<PROJECT_ROOT>/docs/cli/workflows/asset-versions.md`
- **场景、建模与硬表面面板**：`<PROJECT_ROOT>/docs/cli/阶段7场景工作流.md` · `<PROJECT_ROOT>/docs/cli/阶段8建模工作流.md` · `<PROJECT_ROOT>/docs/cli/扩散建模工作流.md` · `<PROJECT_ROOT>/docs/cli/workflows/session-and-preview.md`
  - Schema：`<PROJECT_ROOT>/docs/cli/schemas/model-manifest.schema.json` · `<PROJECT_ROOT>/docs/cli/schemas/geometry-operation-report.schema.json`
- **UV、材质与几何节点**：`<PROJECT_ROOT>/docs/cli/阶段9节点工作流.md` · `<PROJECT_ROOT>/docs/cli/workflows/uv-material-handoffs.md` · `<PROJECT_ROOT>/docs/cli/材质工具批量诊断与进度-20261004.md` · `<PROJECT_ROOT>/docs/cli/几何节点工作流.md`
- **绑定、角色动画与模拟缓存**：`<PROJECT_ROOT>/docs/cli/骨骼内置工作流.md` · `<PROJECT_ROOT>/docs/cli/workflows/rigging-handoffs.md` · `<PROJECT_ROOT>/docs/cli/workflows/rigging-0401.md` · `<PROJECT_ROOT>/docs/cli/阶段10角色动画工作流.md` · `<PROJECT_ROOT>/docs/cli/阶段11模拟缓存工作流.md` · `<PROJECT_ROOT>/docs/cli/workflows/physics-and-editing.md`
- **镜头、灯光、三渲二、合成、渲染与剪辑**：
  - `<PROJECT_ROOT>/docs/cli/workflows/shot-timing.md` · `<PROJECT_ROOT>/docs/cli/跟踪输入标定与流水线.md`
  - `<PROJECT_ROOT>/docs/cli/打光插件CLI-20261009.md`
  - `<PROJECT_ROOT>/docs/cli/workflows/toon-surface-slots.md`
  - `<PROJECT_ROOT>/docs/cli/合成工作流.md` · `<PROJECT_ROOT>/docs/cli/workflows/relight-session-control.md`
  - `<PROJECT_ROOT>/docs/cli/阶段12渲染媒体工作流.md` · `<PROJECT_ROOT>/docs/cli/阶段13格式交换工作流.md` · `<PROJECT_ROOT>/docs/cli/阶段14流水线工作流.md`
