---
name: blender-asset-manager
description: 按项目规范完成 Blender 资产分类、命名校验、Catalog 映射、事务发布、新工程拖入验收与版本升级。接到审看通过的模型或材质要正式入库、整理资产目录、从注册库取用集合零件材质或发新版时用它。
metadata:
  version: "1.0.0"
---

# Blender 资产入库与管理

本技能负责把用户审看通过的模型、材质或节点组按规范命名、归类、打包并发布进正式 Blender 资产库，同时负责从注册库取用资产与发新版，属于 [资产入库部门](../blender-work-methodology/references/departments/asset-library.md)。实际读写 `.blend` 与提交后台事务配合 [blender-cli](../blender-cli/SKILL.md)。

## 怎么做

### 第一步：核对注册库与现有分类

动手前先查当前 CLI 的资产入口，再核对目标库在 Blender 偏好设置里的注册状态和现有 Catalog。注册名与 Catalog UUID 全从当前配置实取，确保入库位置与资产浏览器完全一致。

```powershell
$ctl = './blenderctl.ps1'

# 查资产入库与取用的当前入口
& $ctl discover --product asset-library

# 核对目标库注册状态，并解析现有 Catalog 路径
& $ctl asset libraries --name 'User Library' --catalog-path 'Models/Props/Audio Equipment'
```

目标库已经登记时，直接沿用查到的库根与 Catalog UUID；缺少子分类时，留在后面的正式事务里追加新条目。目标库尚未登记时，先把已验候选原样留好，告诉用户在 Blender 的 **编辑 → 偏好设置 → 文件路径 → 资产库** 里登记并保存后再入库。

### 第二步：定分类、起名字并跑名称预检

先划清资产边界。整件模型标为一个集合资产，拖进新工程一摆就齐；确实会被单独拿去复用的零件或材质，在同文件里另标独立资产并各自分配 `asset_id`。

按用途定主分类和三位类型码，新发布文件按 `<TYPE>_<Subject>_v<NNN>[_<Variant>][_LOD<N>].blend` 起名（如 `MOD_MonitorDial_v001.blend`、`MOD_SofaChair_v001_Oak_LOD0.blend`）。主题优先用 ASCII PascalCase 方便跨软件流转，中文显示名放进资产记录和浏览器展示名，原始包文件名留作来源字段。

候选取名定好后，先写一份 UTF-8 JSON（包含目标绝对目录 `directory` 与候选文件名列表 `names`），跑随包校验脚本查 Windows 非法字符、格式规范和目录大小写重名：

```powershell
$d = & $ctl discover --product asset-library | ConvertFrom-Json
$py = (Get-ChildItem -Path (Split-Path $d.data.selected_blender.file -Parent) -Recurse -Filter 'python.exe' | Select-Object -First 1).FullName
& $py './.agents/skills\blender-asset-manager\scripts\check_names.py' --input '<候选JSON绝对路径>'
```

返回 `ok: true` 且退出码为 `0` 后进入下一步。分类编码、标签规则、多层次命名与贴图命名细则见 [分类与命名](references/classification-and-naming.md)。

### 第三步：备齐打包工程与独立贴图

保留获用户认可的可编辑主工程，确保零件层级、修改器栈、UV 与节点材质完好。把材质用到的贴图打包进要发布的 `.blend`，这样资产拖进任意新工程都能立刻显示正确画面；同时在发布包的 `textures/` 目录另交一份独立贴图文件，方便后续贴图精修或跨软件取用。

同步核对来源、作者、许可依据和本次允许用途，把已确认的事实写进资产记录，待核实项如实留待核对。

### 第四步：按发布事务链正式写入库

在注册库外部建独立批次目录 `work/audit/<batch>/>/`，分设 `jobs` 和 `transactions` 两个目录，源工程保持完好留作对照。全程固定同一套目录，按下面顺序走完四步：

1. **生成发布候选（`asset prepare`）**：基于源工程与实测 SHA-256 生成工作副本，写入集合资产与独立复用件的标记、Catalog UUID、标签和显示名。
2. **生成单文件分片（`asset index`）**：为发布候选产出覆盖该文件全部活动资产的索引分片，保证多资产文件里每条记录都完整登记。
3. **冻结基线并规划事务（`asset plan`）**：冻结当前库索引、历史与 Catalog 文件的哈希，合并单文件分片并校验发布凭据。首次发布时把目标项的 `expected_output_sha256` 设为 `null`，要求目标路径空闲；确需替换已授权旧文件时，填入实测文件哈希。
4. **提交并核对终态（`transaction apply`）**：人工或程序复核计划无误后执行 `& $ctl --jobs-dir $jobs --transactions-dir $tx transaction apply <transaction_id>`，再跑 `transaction status <transaction_id>` 确认事务完成且锁已释放。

请求清单写法、冲突处理与中断恢复见 [Catalog、发布事务与版本升级](references/catalog-and-execution.md)。

### 第五步：新工程真拖入与冷重开验收

入库完成后，新建一份干净工程验证真实使用体验。优先在资产浏览器切到目标库和 Catalog，选列表视图并将导入设置设为 `Append` 与 `Instance Collections → When Appending`，把资产拖进 3D 视口保存；需要脚本化复现或批量验收时，先查入口再落位：

```powershell
# 查集合资产并取出 placement
& $ctl asset find-collections --name 'User Library' --catalog-path 'Models/Props/Audio Equipment' --asset-id '<asset_id>'

# 把集合放到新工程指定集合里（另存为库外接收副本）
& $ctl asset place-collection --params '<place-request.json>' --output '<receiver.blend>'
```

要验证独立零件或材质时，用 `asset find-assets --type Object`（或 `--type Material`）查出 `placement`，交给 `asset use-asset` 放进目标集合或指定材质槽。

接着对接收工程做两道必查检验：

1. **轻量实例冷重开与依赖检验**：另起进程冷重开接收工程，跑 `project verify` 确认外部未声明依赖为零，并确认默认放置形态为轻量的集合实例。
2. **转本地可编辑检验**：对接收工程跑一次 `scene prepare`（操作 `object.make_collection_local`），把集合实例转为本地可编辑零件；保存后再次冷重开，核对零件层级、修改器、UV、打包贴图和局部变换都完好可改。

### 第六步：沿用身份发新版，保留旧路径稳住老工程

同一件资产需要升级时，沿用原来的 `asset_id` 和 `Catalog UUID`，给新版起下一个版本号（如从 `v001` 升到 `v002`）并写到新文件路径。

在同一个发布事务里同时提交两份候选：新版文件带着完整资产标记进入活动索引；旧版文件用 `asset prepare` 的 `unmark` 撤下该资产的浏览器显示标记，同时把旧 `.blend` 文件路径和可链接数据块原样留在库里。事务提交后，刷新资产浏览器只会看到最新版，老工程原来链接的旧版本路径依然稳稳打开，升不升级留给使用者自己拍板。需要找回旧版原始文件时，按 `asset-history.json` 核对 `preserved_sha256` 后复制到库外恢复路径。

## 交给下游

向用户或下游镜头部门交付以下四项产物：

- 正式资产库里的发布路径、`asset_id`、版本号与 Catalog 路径。
- 在干净新工程里拖入并完成冷重开、转本地可编辑验收的接收样例工程与预览图。
- 随发布包附带的独立贴图文件夹。
- 完整的资产卡片记录与本次事务回执路径。

验收发现问题按归属分流返修：

- 资产浏览器搜不到、分类放错、缩略图发黑或命名冲突：留在本部门修 Catalog UUID、资产标记与预览图。
- 拖进新工程后尺寸异常、原点偏移或掉贴图：退回建模部门或 UV 与材质部门修单位变换、原点与贴图打包，再发新版入库。

## 例子：把审看通过的监听旋钮发布进正式库并升级到新版

接到委托：「材质审看通过了，把 `work/monitor-dial/master.blend` 里的整套监听旋钮和旋钮金属材质发布进 `User Library` 的 `Models/Props/Audio Equipment`，验收拖入可用。」

1. **查库与规划**：跑 `asset libraries --name 'User Library' --catalog-path 'Models/Props/Audio Equipment'` 确认库已注册并拿到现有 Catalog UUID。主件集合归入 `MOD` / `Models/Props/Audio Equipment`，常用金属材质另标独立材质资产归入 `MAT` / `Materials/Realistic`，新文件名定为 `MOD_MonitorDial_v001.blend`。
2. **跑名称预检**：把目录和 `MOD_MonitorDial_v001.blend` 写入 JSON，运行 `check_names.py` 返回 `ok: true`。核对主工程已打包全部贴图，并把独立贴图拷贝进发布包的 `textures/`。
3. **跑发布事务**：在 `work/audit/<batch>/` 和 `transactions` 下依次运行 `asset prepare`、`asset index`、`asset plan`（设置 `expected_output_sha256: null`），复核计划后执行 `transaction apply`，用 `transaction status` 确认入库完成。
4. **新工程真拖入与冷重开**：新建空工程，从 `User Library` 的 `Models/Props/Audio Equipment` 拖入 `Monitor Dial`（列表视图、`Append`、开启 `Instance Collections`），保存后冷重开并跑 `project verify` 确认外部未声明依赖为零；再对接收工程跑 `scene prepare` 的 `object.make_collection_local` 转本地，单转旋钮 `15°` 后冷重开，确认修改器、UV、打包贴图和相邻部件都完好。
5. **汇报交付**：把库内路径、`asset_id`、版本号 `v001`、Catalog 路径、接收样例和拖入预览图交给用户收尾。
6. **后续升级 `v002`**：旋钮刻度优化后，沿用同一个 `asset_id` 和 Catalog UUID 生成 `MOD_MonitorDial_v002.blend`，在同一事务里对 `MOD_MonitorDial_v001.blend` 执行 `unmark` 撤下浏览器标记但保留旧文件，提交后确认浏览器显示 `v002` 且老工程链接 `v001` 仍能正常重开。

## 参考

- [分类与命名](references/classification-and-naming.md)
- [Catalog、发布事务与版本升级](references/catalog-and-execution.md)
- [规范文档与台账入口](references/sources.md)
- 新发布名称预检脚本：`scripts/check_names.py`
