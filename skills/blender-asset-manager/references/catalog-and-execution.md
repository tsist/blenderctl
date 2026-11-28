# Catalog、发布事务与版本升级

当任务涉及读写 Catalog、修改 `.blend` 资产标记、执行正式入库、从注册库取用资产或发新版时，按本文件步骤配合 [blender-cli](../../blender-cli/SKILL.md) 执行。全部作业目录 `jobs` 和事务目录 `transactions` 建在注册库之外（如 `work/audit/<batch>/>/`）。

## 核对注册库与 Catalog

先读 Blender 当前保存的注册库配置，确认库根路径与 Catalog 合并范围。同一台机器可以登记多个独立库根，每个库各自维护一份 Catalog 合并视图。

```powershell
$ctl = './blenderctl.ps1'

# 查看当前配置下的注册库与指定分类路径
& $ctl asset libraries --name 'User Library' --catalog-path 'Models/Props/Audio Equipment'
```

使用隔离 Blender 用户配置目录时，追加 `--profile <用户资源目录绝对路径>`。处理 Catalog 文件时遵守四条准则：

- **保住原始 UUID**：修改分类路径或显示名时沿用原 `Catalog UUID`；需要拆分或合并分类时，同时列齐每个受影响数据块 `asset_data.catalog_id` 的新旧映射、消费工程影响与回滚步骤。
- **尊重合法的同路径或多库复用**：同一合并范围内核对同一 UUID 是否出现冲突定义；不同 UUID 共用同一展示路径，或不同库复用同一 Catalog 定义，均按实际合并范围如实判定。
- **严格 UTF-8 维护**：Catalog 文本保持标准版本头与 `UUID:catalog/path:simple_name` 行格式，保留原有注释、空行与行尾。遇到历史乱码时，先备份原始字节，逐行识别原编码并核对可读名称，再写出严格 UTF-8 副本；编码拿不准的行留待人工确认。
- **窄范围修补 `simple_name`**：修补损坏的 `simple_name` 时，走 `project plan-files` 的 `catalog_repair` 窄修复路径，保留原 UUID、分类路径和有效显示名。

## 发布事务四步链

全程沿用公共 CLI 的 `asset` 和 `transaction` 命令族，每一步从上一步返回 JSON 的 `artifacts` 里实取候选路径、SHA-256 和 `asset_id`。

| 步骤 | 命令 | 作用与要点 |
| --- | --- | --- |
| 1. 准备候选 | `asset prepare <源.blend> --expected-sha256 <SHA> --manifest <manifest.json>` | 在工作副本里执行 `edit`、`extract` 或 `unmark`。只改分类、标签、显示名等元数据时用 `edit`；需要拆出独立发布文件时用 `extract`；撤除旧版浏览器标记时用 `unmark`。已有 `asset_id` 时用 `selector.asset_id` 精确匹配，只写本次要改的字段，保留原描述与作者信息。 |
| 2. 生成分片 | `asset index <候选.blend> --expected-sha256 <SHA> --manifest <index.json>` | 为单个候选文件生成完整资产索引分片，覆盖该文件里的全部活动资产；彻底撤标的独立旧版产出 `assets: []`。 |
| 3. 规划事务 | `asset plan --manifest <plan.json>` | 冻结库内现有索引、历史与 Catalog 文件哈希（首次建库缺失文档填 `null`），合并单文件分片，校验冲突与发布凭据，返回 `transaction_id`。 |
| 4. 提交与核对 | `transaction apply <transaction_id>` 与 `transaction status <transaction_id>` | 复核计划后用同一套 `--jobs-dir` 和 `--transactions-dir` 提交入库，检查终态与锁释放。 |

### 首次发布与同名冲突处理

- **首次发布**：在 `asset plan` 清单对应 `updates[]` 项里写 `"expected_output_sha256": null`，要求目标文件空闲。遇到库里已有同名文件时，计划会返回冲突状态；先查清原文件身份，为新资产换一个空闲版本路径，再同步更新 `asset index` 和 `asset plan`。
- **授权替换既有文件**：确需接管已有路径时，先核实原文件并取得明确授权，用 `Get-FileHash` 读取当前小写 SHA-256 填入 `expected_output_sha256`。文件在规划后若被改动，提交阶段会自动拦截并保留现场。
- **中断恢复与回滚**：提交中途断开时，先运行 `transaction status <transaction_id>` 查清状态，再跑 `transaction recover <transaction_id>` 续完同一事务。确需回滚时，先核对库内目标文件仍是本批次写入的版本，再按 `transaction rollback <transaction_id>` 逆序恢复，保护用户在发布之后做的新编辑。

### 打包贴图与独立贴图同步交付

发布主工程把渲染依赖贴图打包进 `.blend`，让新工程脱开外部贴图目录也能正常出图；同时在发布包的 `textures/` 目录放一份同版本独立贴图文件，并在资产记录与文件清单里写清各自路径、哈希和用途。链接库、模拟缓存与大体积视频按各自类型逐项核验路径解析，在独立进程重开后再签认。

## 从注册库取用与验收

### 1. 查找资产并取得 `placement`

```powershell
# 按发布 ID 或分类查受管集合资产
& $ctl asset find-collections --name 'User Library' --catalog-path 'Models/Props/Audio Equipment' --asset-id '<asset_id>'

# 按类型查零件或材质（Collection / Object / Material）
& $ctl asset find-assets --type Material --name 'User Library' --query 'Monitor Dial'

# 直接读取资产浏览器里的手工标记，并接回匹配的受管发布版
& $ctl asset find-collections --name 'User Library' --native --resolve-published --blend 'MOD_MonitorDial_v001.blend' --query 'Monitor Dial'
```

原生扫描返回 `scan.complete` 和 `scan.issues`，大型资产库用 `--blend` 限定单个或多个相对路径做快速定点排查。全零 UUID 对应资产浏览器里的未归类资产（`catalog_path` 为 `null`）。

### 2. 放置整件集合、独立零件或材质

从查询结果取出 `items[0].placement`，连同接收工程路径和实测 SHA-256 写入请求 JSON，输出到库外的新副本：

| 取用目标 | 命令 | `target` 字段 |
| --- | --- | --- |
| 摆放整件集合实例 | `asset place-collection --params <req.json> --output <out.blend>` | `scene`、`collection_path`（空数组表示场景根）、`instance_name`、`location` |
| 单独取用零件 | `asset use-asset --params <req.json> --output <out.blend>` | `scene`、`collection_path`、`object_name`、`location`（需要联动骨架或约束链的零件优先按整件集合取用） |
| 赋给对象指定材质槽 | `asset use-asset --params <req.json> --output <out.blend>` | `scene`、`object`、从零开始的 `slot`、`expected_material`（空槽或末尾新增槽填 `null`） |

重复放置同一受管版本时，CLI 会复用接收工程里已有的本地集合树与手调结果；若接收工程已绑定同 `asset_id` 的另一版本，命令会保留旧工程并返回冲突提示。

### 3. 轻量实例转本地可编辑与依赖复查

1. **轻量实例冷重开**：用独立进程打开接收工程，运行 `project verify <receiver.blend> --expected-sha256 <SHA> --resources <resources.json>` 核对 `external_paths`，确认外部未声明依赖为零。
2. **实例转本地零件**：对保存好的接收工程跑 `scene prepare --file <receiver.blend> --expected-sha256 <SHA> --manifest <local-manifest.json>`，在清单里调用 `object.make_collection_local` 点名实例对象与原集合。命令返回原零件到本地对象的新旧名称映射，随后按映射名对单个零件做变换或修改器测试，保存后再冷重开确认零件层级、修改器、UV、打包贴图及同场其他实例均完好。
3. **实例审查出图**：需要为已放置的轻量集合生成审查图时，用 `discover --product asset-library` 查 `asset preview prepare` 参数，在清单里把 `target` 指向实例 Empty 或所在集合，设置 `fixture=NONE` 和 `embed=false`，在新作业目录出图而不改动接收工程。

## 同事务发布新版并保留旧路径

同一件资产升级到新版本时，按四步在同一个事务里完成平滑切换：

1. **准备新版**：沿用原来的 `asset_id` 和 `Catalog UUID`，用 `asset prepare` 从新主工程生成带新版路径的候选（例如 `MOD_MonitorDial_v002.blend`），再跑 `asset index` 产出新版分片。
2. **撤下旧版显示标记**：对原版本文件跑 `asset prepare` 的 `unmark` 定位同一 `asset_id`，只撤下该资产的浏览器显示标记，原文件路径和可链接数据块保持完好；再跑 `asset index` 生成旧版分片（独立旧版文件为 `assets: []`，多资产旧文件继续保留其他活动资产条目）。
3. **同事务合并提交**：在 `asset plan` 里同时提交新版发布项（附候选哈希与验收凭据）和旧版撤标项，复核后执行 `transaction apply` 并查 `transaction status`。
4. **新旧双工程复验**：新建干净工程刷新资产浏览器，确认分类下只显示新版且拖入重开正常；同时打开旧版接收工程，确认它仍按原版本路径正常解析和使用。

需要从历史记录找回某一旧版原始文件时，在库里的 `asset-history.json` 按 `asset_id` 和版本号找到记录，核对 `preserved_file` 的实测 SHA-256 与 `preserved_sha256` 一致后，复制到库外的新恢复路径并冷重开验收。

## 来源许可与验收记录

- **如实保留来源与许可层级**：保留 `license_basis=user_declared` 与 `license_evidence_status=not_independently_verified` 等原始标记；用户口头声明如实记录，单个外购组件的来源限定在该组件本身。进入 `published` 时确认本次 `intended_use` 在允许范围内。
- **验收表四态填清**：验收清单统一使用 `pass`、`fail`、`not_run`、`not_applicable` 四种状态，填 `not_applicable` 时写清理由。每项验收绑定实际候选哈希、`asset_id` 与验证用途，并覆盖独立进程冷重开、Catalog 可见、拖入摆放、外链核对和关键材质或机构实测。
