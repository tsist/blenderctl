# dev22：有预算的研究证据工作单元

## 能力边界先声明

本候选仅提供通用 HOST 研究证据整理与检查路线，不是完整实验 solver 移植。它不生产 Blender 模型、不运行 LP、不修改阈值、不代替 Blender 原生求值或反射观察，不携带私有案例坐标、模型、任务标识、路径或历史许可。实际支持域与字段必须从匹配候选的 Schema 和 CLI 帮助读取，不把本指导当可执行请求。

合理布线拓扑始终优先；残差变小、面数减少或数据结构合法都不能补偿面形凹折、支撑漂移或编辑通路损坏。

## 一个领域单元，一份预算

由插件承载领域判断，CLI 调度一个完整的 budgeted domain work unit，统一收取并绑定源、线性模型、保护域、覆盖与外部求解记录，由已验证 q 重建候选坐标，输出分层证据和限制。不以多段临时脚本、零散 shell 计算或独立通过的局部报告冒充一个受管单元。

执行前核对实际实现、源与候选的 canonical SHA、对象/坐标/连接身份、冻结技术合同及资源预算。预算要覆盖输入规模与检查成本；超限明确停止，不能切小输入、删不利面或缩 coverage 掩盖。实现尚未提供的计算不能由文档假定存在。

## 本候选真实接口与范围

CLI 为 `hardsurface research-evidence --request <request.json>`；输入版本 `research-evidence/1.0`，报告版本 `research-evidence-report/1.0`，插件协议保持 `0.2.0`。使用 `hardsurface describe --section research-evidence` 读取 `schemas/research-evidence.schema.json`，并核对候选的 `hardsurface/research_evidence.py`、`docs/research-evidence.md` 与公开合成测试；不与其他生产请求 Schema 混用。

输入 source 含 `vertices/faces/vertex_ids`；线性 model 含 `variable_count/basis/inequalities/equalities/bounds`。`source_sha256` 与外部 LP 的 `source_sha256/model_sha256` 使用 canonical SHA。外部 q 通过残差和边界检查后，单元按 source + basis × q 重建候选，沿用 faces 和 vertex_ids；不接受任意独立候选冒充该解的结果。这不证明线性模型等价于实验 Catmull–Clark/junction solver。

当前预算是有界输入加 cooperative wall-time/operation 检查，不是独立进程 watchdog。当前 coverage 仅是候选顶点相对 origin/cell_size/required_bins 的占用检查；不是曲率、连续表面或原有 CC 研究 coverage 算法。

## protected exact identity 与支撑证据

受保护顶点/连接必须能精确对应源与候选，检查完整保护集合和明确不变量；同标签、近邻坐标、总数相同或汇总哈希不能替代实际对应。当前空保护集合记为 `not_run`；不把未声明保护域默认为已保护；身份不一致时拒绝组合证据。

支撑证据应说明支撑集合、实际形状、相邻控制面及变化范围。若接口只核对指定索引的精确坐标与连接，这只是已声明集合的保护检查，不自动证明完整支撑职责、曲率或形体不变。拓扑变化须有明确映射和重新审阅，不能最近点猜测。

## 几何与覆盖关口

- 拓扑：真实连接、面型、边界、方向和域支持单列；图合法不意味着嵌入几何有效。
- 真实几何：同一候选四边面以两条对角分别检查三角退化、定向与凹折风险；固定门限，保留最差局部。有限局部检查不等于整件自交审计。
- 自交、Blender 求值与 native 反射：只有实际相应执行及结果才可通过；本 HOST 整理器没有执行的项目保持 `not_run`。
- 曲率比较：保留原 coverage bins、完整域/排除项、采样与前后身份。缺原 bins 不能宣称曲率比较合格；重新分箱是新证据，不能代填历史缺口。分箱键存在也不自动证明数值可比或连续域覆盖。

## 外部 LP 的有限解释

外部 LP 只接收并核验已有记录；检查记录不等于重新求解或独立证明 solver 数学正确。本单元只验证输入线性模型的 q 残差与边界，并按 basis 显式重建坐标；不验证最优性、不证明模型完整代表设计约束，也不认证外部 infeasibility。本接口绑定 source/model 的 canonical SHA。seed、assignment 与 inner-box 没有独立字段；调用方研究记录须另存其与完整线性模型的对应，缺映射时不得声称本单元已验证这些研究条件。`infeasible` 只描述这一组合，不能推论所有 seed、assignment、inner-box 或设计全局不可行。

若 LP 未产出候选坐标，候选几何为 `not_run`；不得把外部 infeasible 记为 geometry fail，也不得造一个替代候选执行。可独立核验的源/拓扑/身份项目保持各自实际状态。已有候选的失败仅按实际已运行检查记录，不从求解器状态倒推。

## 结果与下一步

分别列出运行成功、输入完整性、拓扑、局部几何、保护域、覆盖、外部 LP、自交、native 反射和产品资格。当前报告即使全部已实施检查通过也仍为 `incomplete`，因为自交未实施；不会输出 `evidence_complete` 或 production pass。拓扑合格 ≠ 几何合格 ≠ native 反射验收 ≠ 成品。

本次工程更新不授权新建模、新 LP 或阈值放宽。下一步若改变几何设计、支撑、seed/assignment/inner-box 或参考，应先显示参考版本、关键尺寸、冲突/待定项及一致性清单，取得所需当前许可和参考冻结，再进入匹配实现的受管工作单元。
