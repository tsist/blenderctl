# 分类与命名

本文件补充项目资产规范 `docs/asset-management/STANDARD.md` 的落地做法，帮你在动手写清单或跑发布事务前把资产边界、分类、标签与多层名称一次定清。

## 先划清资产边界

先看交付物以后会被怎么拿去用，再定哪些数据块标为资产：

- **整件模型标一个集合资产**：由外壳、零件、骨架和辅助空物体组成的完整模型，统一标成单个集合资产，拖进新工程时保持完整结构。
- **独立复用件另标独立资产**：同一个 `.blend` 里确实会单独拿去用的零件、材质或节点组，分别标记资产、各自赋 `asset_id`，放在同一发布文件里按同版本发布。
- **配套素材按文件角色登记**：参考图、音视频、压缩包和外部贴图工程记为配套文件，保持清晰的目录与文件清单关系。
- **区分五类对象**：磁盘文件、可复用资产、持久身份 `asset_id`、用途主分类、原生 `Catalog UUID` 各司其职。同一个 `.blend` 可以包含多个资产，一项资产也可以依赖多份贴图或缓存文件。

## 主分类与类型码

按交付后的真实用途选定唯一主分类，风格、题材、引擎与特性用标签补充，来源、作者、许可和生命周期写入各自独立字段。

| 类型码 | 主分类 | 常用子类 |
| --- | --- | --- |
| `MOD` | `Models` | `Characters`、`Clothing`、`Props`、`Weapons`、`Vehicles`、`Architecture`、`Furniture`、`Environment`、`Kitbash` |
| `MAT` | `Materials` | `Realistic`、`Stylized`、`Toon` |
| `TEX` | `Textures` | `Surface`、`Decal`、`Mask`、`Brush` |
| `HDR` | `HDRI` | `Indoor`、`Outdoor`、`Studio` |
| `LGT` | `Lighting` | `Studio`、`Environment`、`Product` |
| `GEO` | `Nodes/Geometry` | `Modeling`、`Scatter`、`Utility` |
| `SHD` | `Nodes/Shader` | `Surface`、`Utility` |
| `CMP` | `Nodes/Compositor` | `Color`、`Effects`、`Utility` |
| `RIG` | `Rigs` | `Character`、`Mechanical` |
| `ANI` | `Animation` | `Actions`、`Poses` |
| `SCN` | `Scenes` | `Character`、`Product`、`Architecture`、`Environment` |
| `BRU` | `Brushes` | `Sculpt`、`Paint` |

- **分类依据按优先级取值**：用户明确指定 → 当前数据块实际用途 → 有出处的历史记录 → 标记为待确认候选。
- **历史分类按需映射**：遇到现库里已有的中文 Catalog 或旧层级，先原样保留；需要并入规范路径时，列出「原分类 → 拟分类 → 依据」映射表，按批次逐步调整。
- **标签写命名空间且有据可查**：采用 `style:realistic`、`style:toon`、`subject:audio`、`engine:cycles`、`feature:rigged`、`resolution:4k` 这类格式。用户当次口头指定的风格标签可直接采纳并记清来源为用户指令；功能与引擎标签等实测确认后再挂。
- **改标签时提交完整清单**：调用 `asset prepare` 的 `edit` 修改标签时，接口采用全量替换。先读出已有标签，算出「保留 + 新增 − 明确删除」后的完整列表再提交，保住原有标签。
- **生命周期使用规范枚举**：在 `source`、`master`、`working`、`published`、`quarantine`、`archived` 六种状态里如实选择；历史自由文本描述先核对真实用途再映射，缺依据时列为待确认。

## 五层名称各守其位

把不同用途的名字分开管理，既方便跨软件流转，又稳住旧工程引用。

| 名称层次 | 做法 |
| --- | --- |
| 源包与已有文件名 | 保持原名，写进 `source.original_name`；收到明确授权并备齐映射表后再做逐项物理迁移。 |
| 中文显示名 | 放进资产记录的 `display_name` 和资产浏览器展示名，方便按中文检索。 |
| 数据块名 | 改名前先查共享引用数、链接覆盖、驱动表达式里的字符串路径，按目标精确修改。 |
| 新发布文件名 | 统一采用 `<TYPE>_<Subject>_v<NNN>[_<Variant>][_LOD<N>].blend`。 |
| Catalog 名称与路径 | 用来表达分类层级，改名或调层级时沿用原 `Catalog UUID`。 |

### 新发布文件名与版本规则

- **格式**：`<TYPE>_<Subject>_v<NNN>[_<Variant>][_LOD<N>].blend`，例如 `MOD_SofaChair_v001_Oak_LOD0.blend`、`GEO_ArrayOnCurve_v001.blend`。
- **混合包前缀**：一份发布文件同时包含整机集合和可独立取用的材质时，文件前缀取主包类型码（如 `MOD_`），索引分片里每条资产各自保留自己的类型码（`MOD` 或 `MAT`）和分类路径。
- **主题词**：推荐使用 ASCII PascalCase，跨操作系统和管线工具传递更稳妥；已有中文名称原样保留在显示名里。
- **版本号与身份**：版本号至少三位，从 `v001` 单调递增。先按 `asset_id` 查已有记录和发布历史，再定下一个版本号。`asset_id` 在首次登记时生成 UUID v4 并持久沿用；可独立演进的新变体分配新 `asset_id`，并用 `parent_asset_id` 指向母本。
- **LOD 约定**：`LOD0` 表示当前资产最高精度，数字越大细节越简；不同造型设计用 `Variant` 字段或独立变体表达。
- **独立贴图命名**：新导出贴图采用 `<Subject>_<Map>_<Resolution>[_<UDIM>].<ext>`，常用贴图类型码为 `BaseColor`、`Roughness`、`Metallic`、`NormalGL`、`NormalDX`、`Height`、`AO`、`Opacity`、`Emission`。带 UDIM 或序列编号的贴图按整组同步更新节点路径，色彩空间与法线方向按素材实际规格登记。

## 使用 `check_names.py` 做名称预检

新发布 `.blend` 候选名定稿后，用随技能提供的只读脚本检查格式与目录冲突。输入一份 UTF-8 JSON：

```json
{
  "directory": "work/audit/<batch>/",
  "names": [
    "MOD_SofaChair_v001_Oak_LOD0.blend",
    "GEO_ArrayOnCurve_v001.blend"
  ]
}
```

运行命令：

```powershell
$ctl = './blenderctl.ps1'
$d = & $ctl discover --product asset-library | ConvertFrom-Json
$py = (Get-ChildItem -Path (Split-Path $d.data.selected_blender.file -Parent) -Recurse -Filter 'python.exe' | Select-Object -First 1).FullName
& $py './.agents/skills\blender-asset-manager\scripts\check_names.py' --input '<候选JSON绝对路径>'
```

脚本会逐项检查：

- `directory` 为绝对路径（允许尚待创建，脚本保持只读，不创建目录也不改写磁盘）。
- 校验 Windows 非法字符、保留设备名、尾随空格或点、路径穿越写法。
- 校验发布命名格式、合法三位类型码、`v001` 起步版本号，以及 ASCII PascalCase 建议提示。
- 按大小写不敏感方式同时查本批内部重名，以及目标目录现有直接子项重名。

读取标准输出的结构化 JSON 和退出码：退出码 `0` 且 `ok: true` 表示名称格式与当前目录重名预检通过；退出码 `2` 表示存在阻止项或输入错误，先修名称再继续。脚本专注检查新发布 `.blend` 文件名，分类语义、历史版本、数据块名、贴图序列与并发变更留给后续索引查询和发布事务核对。

## 规划表与资产卡片交付

提交分类或命名方案时，先产出本批次规划表（JSON 或 Markdown 表），列齐目标文件与数据块身份、已有 `asset_id`、原分类与拟分类、标签增删、命名层次与旧新名称、判定依据和待确认项，并写清当前处于「方案草案 / 台账更新 / 候选验证 / 正式入库」哪一步。

需要物理更名或迁移时，填写项目模板 `docs/asset-management/templates/migration-map.csv`。需要新建正式资产记录时，基于 `docs/asset-management/templates/asset-record.json` 填写：已确认没有的列表填 `[]`，尚未采集的字段填 `null`，如实区分两者。
