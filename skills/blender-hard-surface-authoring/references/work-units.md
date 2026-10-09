# 插件工作单元、真实接口与能力状态

## 先识别证据层

本技能与稀疏构造器一起开发，以下是本草案快照的状态；执行前重新核对交付清单、实际 `describe`、测试回执及文件身份。

| 能力 | 已有证据范围 | 不能推导的结论 |
|---|---|---|
| 特征 DAG、源保护、语义绑定、检查点 | 可复用底座；具体运行能力以相同实现的回执核实 | 不自动证明新补片、编辑或视觉质量 |
| `sparse_control_cage` 完整体母版与检查 | 有限定单孔板完整体全四边源笼、保存重开和实际 L0 检查历史证据 | 不证明任意导入网格识别、完整表面或用途资格 |
| 固定轴对齐布局、中点角部循环、有界插线 | 有限定构造和编辑的实际连接、区域、循环、极点证据 | 不扩大为任意布局、无限插线、独立滑动、第二孔或槽 |
| dev18 `edit-review` 整链 | 有一次南侧插线的前诊断、编辑、保存重开、后诊断、双向对照原生记录 | 不证明其他编辑整链或视觉资格 |
| dev19 参数编辑整链 | 孔径、孔位两个独立整链有原生通过记录 | 不证明其他参数域、连续组合或厚度/外缘圆角整链；视觉与用途资格未授予 |
| dev20 稀疏表面导出与 L2 观察 | 限定案例导出、源保护、原生法线绑定及独立技术审计通过 | 实际图中广阔平面高光覆盖不足，整体形体/高光未接受 |
| dev21 reflected-anchor 观察灯架 | opt-in HOST 实现并测试；未原生运行 | 反射预测与 HOST 成功不证明实际像素覆盖或表面通过 |
| dev22 通用研究证据工作单元 | HOST 证据收取、身份绑定及有限检查；具体字段与实测状态须核对匹配实现 | 不运行 LP，不生成新模型，不包含完整实验 solver；不授予 native 反射或成品资格 |
| 游戏派生、任意网格修复 | 不包含完整链路 | 不能调用拟议命令或继承母版资格 |

上述原生结果是有限历史证据摘要，私有模型、参数和回执不随本包公开，不能视为公开可复现测试。当前源码身份和公开检查范围见[发布来源与状态](provenance.md)。

“源码已存在”“宿主测试通过”“原生技术通过”“实际视觉通过”“用户已接受”分别报告。收到更新回执后只提升它覆盖的列；源码的旧状态文字不覆盖更晚的真实回执，更晚回执也不改写历史文件。

## 当前源码候选的接口

使用任务选定的插件根与 CLI 包装器，不假定安装路径。下列命令名来自当前源码候选，必须先确认对应版本已包含实现且相关宿主准入已通过：

- `hardsurface describe --section request`：构造请求 Schema
- `hardsurface describe --section quad.panel`：当前单板操作 Schema
- `hardsurface describe --section mesh.inspect`：新只读源笼检查 Schema
- `hardsurface plan --request <request.json>`：按实际参考绑定方式预检/dry-run
- `hardsurface run --request <request.json>`：完整构造单元，参考批准参数从当前帮助取得
- `hardsurface mesh inspect --request <inspect.json>`：读取已保存实际 L0，候选入口不替换旧 `hardsurface inspect --file ... --expected-sha256 ...` 的不同职责
- `hardsurface topology --request <topology.json>` 与 `hardsurface subdivision diagnose --request <diagnose.json>`：既有拓扑/求值诊断，各用自己的 Schema

本候选未安装为全局原生 CLI。全局选项、Blender 路径、jobs 根和资源限制按现场 CLI 技能与实际帮助组织。新接口 discovery 失败、模块缺失、Schema/实现不一致时停在集成缺口，不回退到旧构造器或以旧测试报新接口通过。

## 首次原生试跑准入

新实现不需要先有一份原生成功回执才可首次验证，否则无法开始；但正常候选试跑前，应让本次将调用的入口、Schema、支持域拒绝、源保护和相应纯宿主构造/检查测试通过。与本工作单元无关的未测功能仍记未测，不作为无限前置条件。已知宿主失败、缺模块或字段不匹配不能带病进入正常候选作业。只能在 Blender 判断的项目可在当前实际权限允许的有界诊断单元中首测，并保持该项 `not_run` 直到取得实际结果；诊断试跑本身不授予模型资格。

## G1 专用的源笼构造边界

当前新增字段的结构位置以生成 Schema 为准：

- 采用步骤程序时，`params.design.state.features[].program.kind` 为 `steps`，对应 `program.steps[]` 的 `op` 为 `quad.panel`，该步骤的 `topology_strategy` 取 `sparse_control_cage`
- 同一 `quad.panel` 步骤的 `sparse_cage` 承载新低笼技术参数；现行步骤合同没有包裹这些字段的 `parameters` 对象。不能混用旧 `subdivision_cage`
- 运行请求 `params.quality.stage` 显式取 `source_cage`
- `sparse_cage.preview_levels` 为 0；关闭 `params.wire.enabled`，省略 `params.preview`

当前 source-stage 验收要求构造器对象保持单位变换（位置零、旋转零、缩放一）；设计中心平移属于参数，不用额外对象变换代替。独立 inspector 可以读取其他实际变换，但其读取结果不继承此构造验收。

该阶段只允许新稀疏源笼的 L0 构造/检查，不执行依赖 G1 审阅的 SubD 拟合、渲染线框或 beauty 预览；不能请求用它满足求值尺寸或视觉参考验收。技术参数的 Schema 数值范围不是几何支持域，构造器的实际 guard 与原生结果仍需核验。

完整单元应写出新候选、实际数据源笼检查与独立重开结果。预期阶段回执 `domain_outcome` 为 `partial`；`qualification_scope` 中 `source_structure` 可以在真实检查后为 `pass`，`evaluated_shape`、`surface_observation` 和 `production_qualification` 保持 `not_run`。不要把 partial 翻译成失败重试，也不要把 source_structure pass 翻译成用户 G1 接受。缺任一必需输出或整体失败时，部分文件只能作诊断。按实际检查范围解读 `source_structure`，不能仅由此推断完整 G1。当前完整构造单元在 `quad-quality` 证据中对实际 control mesh 执行必需自交审计，内附只读检查可省略重复审计；必须同时核验这两份同身份证据。若 mandatory audit 缺失/未测，G1 不能通过。

### 布局字段：显式新路线与旧请求兼容

同一 `quad.panel` 步骤可显式设置 `sparse_cage.layout`：`schema` 为 `fixed-frame-axis-aligned/1.1`，`feature_frame_mm` 为 `[xmin, ymin, xmax, ymax]`，`corner_guard_mm` 为正数。它是冻结的技术布局，不改名义设计尺寸或 v2 语义图。具体坐标约定、Schema 与几何 guard 以当前实现核实；字段合法不等于孔编辑在有效域内。

推荐新低笼请求显式携带该布局，并在基线与独立孔编辑中复用同一特征框；选框与局部面角检查见[低笼规划](topology-planning.md)。省略 `layout` 时保留旧布局的兼容行为，不自动迁移，也不把旧行为解释成新轴对齐布局。更改布局是新技术候选，不借旧版 source-stage 批准启动新版本原生执行；本次实现与范围须对应当前项目的准确批准。

旧路线请求默认 `full` 行为保留；新稀疏路线的 `full` 阶段当前明确以 `SPARSE_FULL_QUALIFICATION_PENDING` 拒绝，这不撤回已经验证的独立只读诊断与 dev18 插线对照链；未实现的完整资格/表面环节须另行适配与验证，不能把 `full` 拒绝误读为全部求值能力未实现。不能为了跑通旧请求悄悄改成 source_cage；也不能将 source_cage 解释为放宽公差或完整资格的捷径。本阶段通过后展示真实 L0 并等待用户结构审阅；获准后仍须先核实后续阶段已实现，不能直接套旧高密构造器的 full 管线。

## 当前一次有界插线的受管编辑

沿用 `hardsurface run`，没有独立的 `edit.run` 命令。当前真实 Schema 在 `params.design.mode=patch_if_revision` 下提供 `patches[]` 条目：`op=insert_sparse_strip`、`feature_id`、`step_id`、`expected_feature_sha256`、`corridor=east|south`、`fraction`（0.2–0.8）。还需外层已保存设计的 `expected_revision` 与 `state_sha256`；具体身份从当前文件/设计取得，不填写占位 SHA 执行。

兼容线性插线只接受从无插线基线增加一次；显式 `axis_plane_v1` 路径可按文末说明保留已有声明前缀、逐次追加，累计上限由当前 Schema 与几何 guard 共同限定。不同时改尺寸，不隐式从旧构造器迁移。准备完整体和全部端口/实体映射，拓扑 epoch 精确加一，校验后以新副本提交并保留源。当前已取得限定东/南插线原生证据，但不是所有 Schema 组合均已原生测试。完整流程与失败批次中的独立保全结果必须分开。后续输入仍需实际图和当前身份下的结构复审，不能沿用历史批次资格。

当前初始技术点序族仅实现孔 16 / 外端口 32，插线可改变端口数；这是候选支持域，不是唯一正确采样策略。Schema 允许的更宽数字范围不代表构造器已实现。`hole_planar_support` 提供有限有/无对照，插线数、位置比例和其他技术参数都需经过实际 guard。滑动不是这个插入 patch 的独立承诺，新增孔槽仍不支持。

## 只读源笼检查的请求范围

`mesh.inspect` 的当前请求使用 `params.source` 文件描述符（`file`、`expected_sha256`、`bytes`）；`feature_ids` 与 `object_ids` 只能选一组。`views` 可按实际支持的 `whole/top/bottom/holewall/outer_roundover/side` 区域组织，输出实际数据和对应 SVG，标签预算只是画面显示预算，不应截断原始 ID 数据。当前 native 入口要求受管对象与结构注册绑定；它不是任意导入网格的通用识别工具。

`params.self_intersections` 默认 false，省略意味着未执行该审计，不能报告通过。完整 source_cage 构造的 `quad-quality` 会另行携带 mandatory 自交审计，不能只读 inspector 的汇总就结束 G1。若单独检查用于 G1 且没有同候选的其他可靠结果，显式请求并核查实际返回状态；质量预条件失败或审计未完成仍须保持未通过/未测。无需为了源笼检查开启 Cycles。

## 单元输入与回执

执行前核对源/参考/实现身份、当前权限、支持域、预算和保存候选位置。先 plan，再按批准范围执行；不把方案中的 `topology.plan`、`construct.run`、`edit.run`、`qualify.evaluate` 或 `game.*` 当成已注册命令。

构造/编辑回执应回答：实际做了什么，哪些目标/非目标已检验，候选在哪里，哪些接口/证据未测，在哪一关停止。活动作业沿同一 job ID/jobs 根跟踪，状态未终止不重复提交。保持成功/失败/未测范围和源保护证据，不用文件存在或进程退出零替代整体完成。

本技能不复制整份可执行请求或自建第二套 Schema。执行时读取当前真实 Schema；需要复用示例时，仅纳入已验证的中性最小成功输入与代表性拒绝输入，不携带生产尺寸、私有路径或批准凭据。

新 `1.1` 调度将东侧外沿跨度均衡，旧 `1.0` 与未声明 layout 的行为保持明确兼容，不静默迁移。固定框计划会运行全件 HOST 多边形与自交预测硬门槛；new_scene 源笼请求在入队或启动进程前拒绝失败。源工程编辑需要先取得实际受保护的已保存状态，再在构造前使用同一计划门槛。失败回执不等于候选完成。

可选 `sparse_cage.insertion_policy: axis_plane_v1` 启用有界轴向插线；必须同时指定固定框布局。`insertions` 至多两项，已存在的声明只能作为完整前缀保留，每次追加一项 east/south 与 fraction0.2–0.8。fraction基准是冻结原始条带的公共轴区间，不是每边同百分比。端口按实际事务从32到34再到36，新增面数由完整追踪决定。原有线性插线仍为独立兼容路径。

## 稀疏求值诊断：已有有限案例

新稀疏profile通过候选随附的 `hardsurface-cli` 调用既有 `hardsurface subdivision diagnose` 完整只读单元。请求固定源文件和对象、完整实际修改器设置，取四个层级、导出几何并关闭渲染。它不打开构造器的full资格分支，也不编辑或另存源模型。

能力与确切字段以冻结实现Schema为准。母版及3份插线后的既有实际数据优先复用（核对身份）；只有缺少当前问题所需证据才新运行，不默认对整个编辑矩阵重复渲染。普通位置/边/面导出、语义传递、真实法线和正式形体验收是不同产物，分别核验。

## 显式中点角部构造

`quad.panel` 的可选 `sparse_cage.corner_columns` 只接受 `{"schema":"corner-columns-midpoint/1.0"}`，要求显式稀疏路线、固定框架、轴向插线策略和受支持的截面分段。两条结构循环在构造基线中生成，再冻结轴向参考与重放用户插线；它们不消耗用户插线次数。旧声明不自动迁移。仍使用插件现有完整构造、检查、保存、重开与编辑作业，不把 HOST 提案对象直接当成可重放原生资产。

检查新循环实际闭合性、完整端口传播、稳定父元素身份、尺寸编辑的依赖范围，以及交叉插线对结构循环的延续。孔参数变化不能使无关外部 ID 随整体哈希变化。旧成功记录不替新构造版本完成这些验证。

## 编辑与细分对照工作单元

当前开发增量将既有受管编辑与前后只读诊断组合为一个可恢复单元。先读取实际 `hardsurface describe --section edit-review` 与 `hardsurface edit-review --help`；入口、字段和原生资格以本次冻结源码及回执核对。准备方法、结果解释与中断处理见[编辑与细分对照](edit-subdivision-review.md)。dev18 的一次南侧插线整链已有原生证据；形体审阅仍未完成。HOST 测试数量与历史数值回归不代表新原生模型数量。后续新操作或实现仍须独立核验。

## 观察接口的历史边界与当前进展

旧表面导出器绑定 `source_bound_qualification_v1` 与层级 `[0,2,3]`，不能直接冒充稀疏路线。稀疏保存栈的 SUBSURF `levels/render_levels=0`；按原保存栈观察会得到 L0，不能标为正式 L2。dev20 已有只读临时 L2 设置、实际 L2/L3 表面与原生法线绑定的有限技术记录；未适配入口仍不能直接沿用，不得为观察改写母版。技术进展不代表形体/高光已接受。

观察中只有局部角部条带被照亮、大片平面缺少足够高光覆盖时，记录观察不足；不要把“未看见缺陷”写成表面通过。HOST 反射诊断可检验朝相机灯带是否错过平面的镜面反射方向，但它不是包括遮挡、粗糙度和逐像素积分的完整渲染模型；射线命中率不是亮像素比例。

dev21 增加 opt-in reflected-anchor 灯架的 HOST 实现和测试。它未原生运行，不默认启用，也不改变 dev20 的观察不足结论。执行前读取当前 Schema，保持源保护、版本绑定和独立输出；有实际原生图后才能评价区域覆盖和表面。

## dev22 研究证据入口

入口为 `hardsurface research-evidence --request <request.json>`；`hardsurface describe --section research-evidence` 提供 `schemas/research-evidence.schema.json`；输入 `research-evidence/1.0`，报告 `research-evidence-report/1.0`，协议仍为 `0.2.0`。准备调用前读取[研究证据关口](research-evidence.md)，核对当前实现与公开合成测试。单元接收 inline source、线性 model、保护顶点 ID、coverage 与外部 LP 记录，以 canonical SHA 绑定源和模型；通过残差/边界检查的 q 用于显式重建 source + basis × q 候选坐标，不接受任意独立候选。

外部 LP 仅验证，不求解、不证明最优性或 infeasibility；没有有效候选 q 时 geometry 为 `not_run`。当前 coverage 仅候选顶点分箱占用，不是曲率或原 CC 研究覆盖。自交与 native 反射未执行，明确为 `not_run`；即使已实施检查全部通过，整体也仅为 `incomplete`，不会输出 `evidence_complete` 或 production pass。seed/assignment/inner-box 未设独立字段，不能声称已验证这些研究语义。

此单元不替代已保存 Blender 文件的原生身份、自交审计、求值或高光，也不授权新建模、新 LP 或设计变化。首次 CLI 集成需通过适用 HOST 回归；原生生产仍遵循 G0–G5。
