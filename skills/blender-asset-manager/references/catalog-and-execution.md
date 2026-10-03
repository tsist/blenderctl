# Catalog 与执行

仅在修改 Catalog、blend、物理路径或共享索引时读取。采用当前项目规则，随包基线见[资产标准](asset-standard.md)第6—10节；CLI 以公开仓库 Schema/帮助为准。[命令索引](https://github.com/tsist/blenderctl/blob/main/docs/CLI_REFERENCE.md)只作导航，旧示例不是免检参数。

## Catalog 身份与范围

- 先确认目标注册库根及其 Catalog 合并范围；使用当前配置/只读观察，不把旧报告里的注册位置当现状。整个 `<本次资产库根>` 不等于一个注册库。
- 严格解析 UTF-8、VERSION 和 `UUID:catalog/path:simple_name`。空白、注释和原始字节应保留以供对比与恢复；不能 `errors=replace/ignore` 后覆盖源文件。
- 按同一库合并范围核对同 UUID 是否存在不同定义。不同 UUID 的相同路径是合法情形；不同库重复定义也可能合法，不能按路径去重或全库统一重建 UUID。
- 改路径/显示名沿用 UUID。拆分、合并分类若确有需要，列出每个受影响 asset_data.catalog_id 的新旧映射、消费者与回滚策略，不仅修改文本文件。
- 当前 Catalog 与规范主分类可以通过映射协调，不强制一次性改为英文路径。仅改逻辑记录不应假装已改 Asset Browser。
- 损坏 simple_name 的窄修复可使用 `project plan-files` 的 `catalog_repair`：保留 UUID、路径、有效显示名；普通改类/改名用正常 Catalog 候选和明确映射，不能假借修复扩大范围。
- 验证悬空 UUID、未分类资产、实际 asset_data；需要确认 UI 行为时再观察 Asset Browser 刷新、分类可见及原导入方式。后台打开成功不代表这些 UI 检查通过。

## 复用现有 CLI，不自造发布器

执行前读取同级[Blender CLI 技能](../../blender-cli/SKILL.md)，核对实际入口、原 SHA、工作目录与资源。全部作业/事务目录置于注册库之外。

| 步骤 | 当前入口与边界 |
| --- | --- |
| 目标识别 | inspect/query 与资产相关 Schema；已有UUID精确定位，无ID用type/name/library。链接/override或不支持数据块先确认适配器边界。 |
| 候选编辑/提取 | `asset prepare` → 新候选；edit/extract等operation在manifest内，不是臆造 `asset rename` 命令。 |
| 单文件资产索引 | `asset index`：完整覆盖候选中所有活动资产，仅是一个文件分片，不直接覆盖全库索引。 |
| 共享库协调 | `asset plan`：冻结现有index/history/catalog哈希，合并完整文件分片，保留未涉及资产和历史字节。返回计划，不直接写库。 |
| 提交/恢复 | 授权明确后 `transaction apply/recover/rollback`，始终使用同一transactions目录与返回ID。 |
| 身份/依赖验收 | validate及相关profile；按目标独立重开、导入、预览或功能验证。索引结构检查不是全库逐文件验收。 |

从 CLI 返回的 `artifacts` 和结果提取候选路径/哈希/资产 ID，不猜作业文件名。MCP 可编辑授权的当前场景，但必须明确保存的持久候选、实际数据块与依赖，再按需要接入索引/事务；其临时 `_for_cli` 快照不作为唯一产物。

只改分类/标签/名称等元数据时使用 `edit`，即使源文件含多个资产也不因此切换为 `extract`。只有目标确实需要独立提取的工作副本时才用 `extract`，并明确是否保留依赖资产标记。已有asset_id时优先使用 `selector.asset_id` 精确匹配；仅提交明确要改的字段，未要求改description/author时不要为了描述本次工作而覆写它们。

## 批次与保留项

1. 使用现有批次/迁移/验收模板，填写目标、允许操作、输入/目标身份、SHA、备份、停止条件。执行之前检查目标冲突、并发变化和备份可恢复性。
2. 物理更名或移动会改变相对路径解析与旧工程引用；列出被引用工程，处理所属库的相对依赖、UDIM、序列、缓存和打包资源，先在代表性副本验证。无法查明动态或库外引用时如实限定覆盖范围。
3. 多资产文件只迁出部分资产时，旧候选及旧文件索引分片保留其余资产。asset_id 不随位置改变。
   同一活动资产迁到新文件或升版到新路径时，同批提供旧位置退标记候选和新位置候选，不能只添加第二份相同活动ID；旧文件中其他资产仍完整保留。
4. 保留来源包、备份、共享资源及回滚历史；同名、同大小或相同 SHA 都不自动授权删除。`quarantine` 的默认含义是逻辑状态；CLI同名operation的退标记行为须核对后选择。
5. 实际提交检查终态和锁释放。跨文件提交不是数据库式原子可见，事务完成后再供库消费者使用。回滚前确认目标仍为本批写入，不能覆盖用户后续编辑。

## 来源和发布

保留 `license_basis=user_declared` 与 `license_evidence_status=not_independently_verified` 等原始来源标记。用户自述可以记录，不能改写为独立核验；某组件外部来源不自动扩展到整个角色或包。

进入 published 按标准与 CLI门禁提供此次 intended_use、对应许可依据和绑定实际候选/资产ID/用途的验收证据。不要伪造凭证或用其他资产回执替代；分类工作本身不要求完成发布许可审查。检查结果使用 pass/fail/not_run/not_applicable，后者写原因。
