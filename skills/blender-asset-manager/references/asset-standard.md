# Blender 资产管理通用基线 v1.0

此基线供新项目采用；已有项目规范及用户当前明确要求优先。它不替代资产许可证或操作授权。

## 1. 范围与原则

库根由用户在当次任务中明确指定。范围包括模型、材质、纹理、HDRI、灯光、节点组、骨骼、动画、姿态、Kitbash、场景、笔刷与依赖。参考图、音视频、压缩包和其他软件工程作为配套资源记录，不自动标记为 Blender 资产。新增根目录须在当次任务中明确，避免无边界扫描磁盘。

文件清单、资产索引和 Blender Asset Browser 是三种视图：一个 blend 可包含多个资产，一项资产也可依赖多个文件。不要用 blend 数量代替资产数量。

现有资产先编目后治理。新发布副本遵循命名规则；不得为了外观一致就破坏源包结构、旧工程引用或 Catalog 标识。每次只处理可审核的一批。

## 2. 生命周期与目录

| 状态 | 用途 | 规则 |
| --- | --- | --- |
| source | 原始下载、采购或导出包 | 保留原名、原结构、来源与许可；不原地编辑 |
| master | 确认的可编辑主资产 | 指定唯一权威版本；修复前做快照 |
| working | 制作/修复副本 | 可试验，不自动反向覆盖主资产 |
| published | 通过指定用途验收的版本 | 保持版本稳定、依赖完整、检索可用 |
| quarantine | 待修复或来源待核实 | 记录原因；默认先逻辑标记，不搬文件 |
| archived | 历史版本 | 保留来源、引用映射及恢复路径 |

当前目录布局保持原状，以索引中的 lifecycle 字段表达状态。未来新建独立库可采用 `00_Source/10_Master/20_Working/30_Published/90_Quarantine/99_Archive`，但不是对现有目录的迁移命令。

新发布包建议结构：`30_Published/<资产类别>/<asset_id>/v001/`，内含 blend、`textures/`、`previews/`、`licenses/` 和资产记录快照。共享依赖可以集中管理，但必须有显式引用及迁移策略。无需为每个单文件重复复制巨型贴图库。

## 3. 分类与标签

主分类按用途，每项资产一个主要分类，可有多个标签。完整场景中的角色等独立资产可分别建记录。

| 类型代码 | 主分类 | 建议子类 |
| --- | --- | --- |
| MOD | Models | Characters、Clothing、Props、Weapons、Vehicles、Architecture、Furniture、Environment、Kitbash |
| MAT | Materials | Realistic、Stylized、Toon |
| TEX | Textures | Surface、Decal、Mask、Brush |
| HDR | HDRI | Indoor、Outdoor、Studio |
| LGT | Lighting | Studio、Environment、Product |
| GEO | Nodes/Geometry | Modeling、Scatter、Utility |
| SHD | Nodes/Shader | Surface、Utility |
| CMP | Nodes/Compositor | Color、Effects、Utility |
| RIG | Rigs | Character、Mechanical |
| ANI | Animation | Actions、Poses |
| SCN | Scenes | Character、Product、Architecture、Environment |
| BRU | Brushes | Sculpt、Paint |

标签使用 `style:realistic`、`style:toon`、`subject:vehicle`、`source:provider`、`engine:cycles`、`feature:rigged`、`resolution:4k` 等命名空间。引擎和功能标签必须有依据；未知就不添加。显示标签可用中文，但索引规范字段保持稳定。来源、作者、许可证、发布状态不可塞进分类树代替独立字段。

## 4. ID、名称、版本

asset_id 使用首次登记时生成的 UUID v4；生成后持久保存。移动文件、改显示名、更新版本均不换 ID。不同可独立使用的变体可分配新 ID 并填写 parent_asset_id。一个 blend 的多个独立资产分别分配 ID；asset_id 不是 Catalog UUID。

新发布文件格式：`<TYPE>_<Subject>_v<NNN>[_<Variant>][_LOD<N>].blend`，例如 `MOD_SofaChair_v001_Oak_LOD0.blend`、`GEO_ArrayOnCurve_v001.blend`。发布文件主题推荐 ASCII PascalCase，便于跨软件传递；中文显示名和旧文件原名保留。不得使用 Windows 非法字符、保留设备名、尾随空格或点；按大小写不敏感方式查重。

名称中的版本至少三位，v001 开始单调增加；不能只靠“最终”“最新版”区分。正式发布版本不静默覆写；修改资产内容或依赖后发布新版本。修订纯说明也记录变更。文件改名时更新索引中的路径，不改 ID。

LOD0 为本资产最高细节基准，更高数字表示较低细节；不把不同设计版本误标成 LOD。单位、轴向、比例、骨骼约定写入元数据，不从软件默认值猜测。

新纹理建议：`<Subject>_<Map>_<Resolution>[_<UDIM>].<ext>`；Map 采用 BaseColor、Roughness、Metallic、NormalGL、NormalDX、Height、AO、Opacity、Emission。保留已有 UDIM/序列编号和模板关系，不能独立批量改名其中一帧。色彩空间、法线方向按素材实际用途记录。

## 5. 元数据与文件清单

资产记录至少含：schema_version、asset_id、display_name、type、category、version、lifecycle、files、blender.datablocks、catalog、source、dependencies、validation。模板见 [asset-record.json](../assets/templates/asset-record.json)。

每个文件记录相对路径或绝对路径、路径基准、用途、字节数、SHA-256 和修改时间。跨机器资产包优先包内相对路径；机器配置中的库根路径单独管理。哈希是特定文件版本的校验值，不是资产身份。null 表示未采集，空数组表示已确认没有；模板数组占位项填写后方可使用，不要混淆二者。

作者、来源链接、获取日期、许可证名称/凭证路径、允许用途分别记录。来源不明标记 unverified，不推断可商业使用或可再分发。进入 published 必须确认此次 intended_use 被许可；不是要求所有资产都可商业再分发。外部上传另需用户授权。

Blender 保存格式版本、实际验证软件版本分别记录；bpy.data.version 的第三项可能是文件子版本，不要把整个元组误写为正式发行版本。另记所需扩展、最低版本的证据、渲染引擎及硬件限制；未知兼容性留空。

## 6. Catalog 与 Asset Browser

Catalog 文件采用 Blender 标准格式、UTF-8 文本，版本头及 `UUID:catalog/path:simple_name` 结构完整。分类路径和 simple_name 不使用冒号、换行等破坏格式的字符。

已有 UUID 不重建；分类改名只调整路径/显示名并保留 UUID。复制同一 Catalog 定义到不同目录可能合法；应在每个注册库合并范围内检查同一 UUID 是否存在冲突定义，不能把任意跨库重复 UUID 直接当错误。Catalog UUID 与资产索引 asset_id 分离。

修复乱码先保存原始字节、识别逐行编码、核对可读名，再生成严格 UTF-8 副本；不得使用 errors=replace/ignore 解码并覆盖。编码不确定时保留待核对项。

每个已发布数据块须明确映射到适当 Catalog，检测悬空 UUID 和未分类资产。仅放入 blend 不意味着已标记资产；检查实际 asset_data。新增或修复库注册路径后，验证 Asset Browser 刷新、分类可见和所选导入方式；保留原有导入方式，除非任务要求变更。

## 7. 依赖治理

将路径引用区分为图像、链接库、声音、字体、缓存、工作区历史、临时路径、插件资源和其他。不存在路径只是候选问题，先核实它是否影响指定用途。

相对路径以所属 blend/库文件为基准解析，链接库里的资源不能统一相对当前场景解析。UDIM、序列、动态缓存按模式检查。依赖图记录源文件、数据块、原路径、解析后路径、类型、是否打包及验证状态。

优先保留已有效的相对路径；绝对路径转相对或资源打包前评估实际支持、共享依赖成本及许可证。打包不是通用修复：链接库、缓存、视频等需按类型验收。不要删除临时/历史引用前就假定它们无用。

重连资源需核对内容、版本、分辨率/通道和用途；同名不足以确定目标。禁止无清单的全库盘符替换。修复后保存副本，再独立重新打开，检查实际依赖与渲染；记录旧新路径。

## 8. 入库与去重

入库顺序：登记来源 → 只读采集文件与哈希 → 判断资产边界 → 分配或复用 ID → 填写分类/标签 → 工作副本整理 → 检查依赖和预览 → 验收 → 发布并记录版本。未经信任的文件默认禁用自动脚本加载；确需脚本或驱动时核实用途并记录验证条件。

去重顺序：大小筛选 → SHA-256 完整比对 → 资产内容、依赖、授权和被引用情况核对 → 选择权威版本 → 保存映射。字节相同仍可能有不同来源与使用记录；不得丢弃这些记录。

`.blend1/.blend2`、压缩包和解压目录视为备份或来源，不自动删除。无名称冲突不代表内容不同。删除不是默认治理操作；先逻辑归档或制作可恢复方案，遵循用户明确授权。

## 9. 批次变更与回滚

每批指定目标、资产列表、允许操作和停止条件。变更前记录源/配置 SHA-256、字节数与修改时间；记录快照位置和空间。目录结构性变更还需被引用工程清单。快照未就绪不得执行批量改名、移动、覆盖。

迁移映射至少含 asset_id、旧新文件路径、旧新数据块名称、旧新 Catalog UUID、变更前哈希、备份位置、操作和状态。遇到目标已存在、源文件并发变化、编码不明或映射不唯一时停止该项，不能静默覆盖。

先选代表性副本试点，成功后小批推进。回滚按执行逆序：核对目标仍是本批次写入的版本 → 恢复原文件/路径 → 恢复 Catalog 和配置 → 重新打开验证。用户已继续编辑目标时，不覆盖新工作，改为人工合并。批次末复核源文件与记录；宿主有任务记录工具时同步本次实际任务。

## 10. 验收与维护

发布验收记录 pass/fail/not_run/not_applicable，每个 not_applicable 写理由。必须按实际使用方式测试：独立进程打开、Catalog 可见、资产导入、实际依赖、关键材质预览、适用的节点/骨骼/动作。后台打开仅为基础检查，不代表渲染、驱动或插件通过。

预览记录资产版本、视角、引擎、分辨率和生成条件，避免错配缩略图。对象检查尺寸、轴向、变换、材质和法线；角色检查骨骼依赖、姿态/动作和变形；节点检查接口与最小示例；材质检查贴图、引擎和法线约定。无需不相关的大范围测试。

每次入库/修复后做增量巡检；更换磁盘路径、Blender 版本或插件版本后对受影响资产抽检。当前不创建定时任务，也不规定自动清理年限。检查范围和未测项写清，不以未知状态通过发布门槛。

## 11. 规范维护

以当前项目的资产标准为权威；本文件是公开的通用基线。规则变更记录具体版本、影响面与来源。模板只是填写辅助；历史审计不自动成为当前文件已修复或已验收的证据。
