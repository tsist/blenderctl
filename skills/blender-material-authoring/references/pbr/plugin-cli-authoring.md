# 插件与CLI材质制作

入口：[Blender材质制作](../../SKILL.md)。先遵循[插件与CLI最高指导](../../../blender-work-methodology/references/plugin-cli-work-units.md)，再采用其[次级效率指导](../../../blender-work-methodology/references/efficient-work-units.md)。本文面向实际组装、更新、对照与恢复；按[framework](framework.md)选择constant/image/procedural/hybrid。程序化2.0的完整操作与范例见[程序化PBR](procedural-pbr.md)。

## 1. 已验工作面与按需发现

旧静态图层以CLI0.54.1/Material Workflow0.7.0的公开合同为历史基线；本版配套候选为CLI0.64.0/扩展0.8.0。公开CLI版本与维护者完整工作区版本序列不同，按实际describe/help及Schema核对能力。局部验证范围见仓库`docs/MATERIAL_WORKFLOW.md`；接口发现不等于视觉、安装或发布通过。

先读blender-cli技能。按需用`material describe --operation <操作> --section <相关章>`取得layers/target/preview/manifest，study另有study章；需要完整请求结构时才用`--schema`，两者不同时给。template-save没有分层manifest，查其完整请求。全局选项放子命令前，复杂请求保存为UTF-8 JSON，源/资源为绝对路径。不要复读整包Schema、长面列表与节点树。

| 本轮目标 | 首选入口 | 范围 |
|---|---|---|
| 单对象/槽的程序化或混合PBR | material.procedural-run | 2.0 signals/layers→新可编辑工程→保存重开/预览，可附export |
| 受管程序化材质导出与重建 | material.procedural-export | 逐通道烘焙→磁盘验证→仅图/参数重建→保存重开/预览 |
| 单目标分层组装或明确增量 | material.run | 图层→映射/节点→分配→保存重开→可选多视图/拼图/报告 |
| 多对象、槽位或面区域，独立失败隔离 | material.batch | 显式assignments/dependencies；同worker顺序写，按分配与视图恢复 |
| 指定层的有限数值探索 | material.study | 同一batch基线，少量cases、固定views、语义风险/差异/进度/工程/图像 |
| 保存可迁移受管结构 | material.template-save | 模板与实例bindings分离；不可表达手改须报告，非任意节点树全保真导出 |
| 仅检查已有材质外观 | material.preview | 原隔离预览入口，不替代分层组装 |

材质工作流为standalone作业，不默认进入pipeline DAG。describe是无Blender只读发现；原始契约来自同版本CLI的material_interface.py及describe返回的Schema/SHA，执行选项以实际合同为准。程序化分别发现`material describe --operation procedural-run --schema`、`procedural-export --schema`及同名子命令`--help`；两种操作不继承旧batch/study的resume或候选字段。

## 2. 需求与输入：一次准备可执行清单

制作前确认外观、分层、旧化/干净程度、观察尺度、用途/引擎和验收意见。文件可查的信息自行读取；只询问无法查明且影响正确性的输入，推荐预选不等于已回答。已有明确授权/答案不重复提问。

从受保护源建立工作副本，核对Scene/对象/材质槽/面区域、UV、模式、共享材料/网格/节点组及库引用。法线、轮廓、光顺或UV导致的异常先回模型/映射阶段；不接入默认通用autoUV。源盘快照不能代表GUI未保存状态。

本轮涉及多槽/区域或内腔的器物，在第一张探针前明确目标面域覆盖、轴向、接缝与UV入口；外/内/底域按实际需求划分，其他对象和仅外表面任务按范围裁剪。保留旧UV及非目标数据，当前model.prepare的uv.layer/uv.set操作能承接时直接复用。首轮检查整体与目标缺陷ROI的可观察性；内壁有制作/验收需求时补能看清深处的机位，必要补光显式准备并记录，外部俯视不能替代内腔观察。

按来源列参考、信号依赖和配准关系：image分支先定所需母版/结构再生成相应通道；procedural分支先定坐标/单位、seed和有位置依据的遮罩；hybrid保留特殊图案交接。常量文件确定性生成。一次汇总采用/拒绝版本；只重做受影响分支，已合格输入直接复用。

冻结通道意图清单，明确图像、程序信号、常量和not_applicable原因。通过[图像门卫](../../scripts/image_gate.py)核对解码、≤2048尺寸、编码/色彩语义、字节与SHA；会话预览另≤2MiB，不替换数据原件。按当前Schema投影为：

- 源file/expected_sha256，明确资源需求resources；不默认空资源。
- 唯一稳定assignment/layer ID，target对象/槽/面和UV；不用.001名称或显示位置猜身份。
- 有序layers、channels、mask、values/adjustments，已有模板与独立bindings；拒绝/采用图有明确版本。
- 固定preview视图/ROI、灯光、目标引擎/设备、色彩和质量；明确预算、jobs目录和持久输出。

程序化清单使用2.0的`signals` DAG、层channel到signal ID的引用、层常量与mask ID；保留`height_distance`和`mesh_map_size`。noise/wood的coordinate及seed影响可复现性；edge/cavity需在当前几何/UV上重新生成，不能跨资产复用旧遮罩。声明玻璃Alpha/Transmission/IOR/Coat，所需完整光学输出与重建也要列入验收。

意图清单中的来源/物性备注不是任意可提交字段。实际键、范围、依赖和profile/receipt要求以操作合同为准；保留本次需要的请求与来源SHA。

请求、参数差异及封包优先复用已验证构建器；存在缺口时，可在授权范围复用一次参数化脚本并核对输出合同，避免逐轮重写长请求。复盘建议的构建器/预设不等于已实现能力；没有可用入口时明确缺口并做最小受控操作，不把每次材质制作扩大成工具开发任务。

## 3. 组装、有效旋钮与GUI交接

插件和CLI使用模式对应的受管核心：来源信号→映射/调节→分层→输出→显式分配；核对实例读回、依赖与非目标保护。受管编辑只保留该核心能表达且不重叠的参数手改；节点/图像/指针改动或重叠冲突须报告，不整树覆盖。程序化保留的旧材质/Fake User、共享材质与绑定迁移使用新版本/新material ID或明确隔离，不能通过删元数据绕过保护。

Roughness图存在时，values.roughness常量被连接覆盖；改纹理结果用run/batch支持的adjustments.roughness_scale/bias。Metallic图等也可覆盖相应常量。未提供Normal、关闭图层或Coat为0时相关旋钮可能不起作用；先读基线与提示，不连续提交微调同一无效值。

旧静态图层Normal为OpenGL +Y、TANGENT，使用明确UV；Height走Bump，不自动称作真实几何置换。程序化2.0用总Height生成根级Bump，真实Normal bake包含其效果，重建有该Normal时不再接同Height；Height文件可作为辅助交付保留。Coat共享最终Normal/Bump，独立Coat Normal需另查接口。真实薄膜、体积吸收、多层物理釉及跨引擎表现另验，不把参数混合当物理校准。

显式判断宏观结构Normal是否应跨层共用：同一基体上的连续雕饰可按意图保持一致，覆盖层自身微结构另行组织；独立覆盖或结构变化不能机械共用。核对各层真实连接与覆盖区域，再用固定条件开关对照确认表现；黄铜单例不证明所有材料都应共享，也不证明生成法线校准或真实几何浮雕。

Cycles/Eevee依支持矩阵核对。Eevee用GRAPHICS，不套Cycles设备id/denoise；ShaderToRGB限Eevee，未知节点组合不默默替换。源场景引擎与独立预览引擎分别记录。

GUI面板用显式草稿和回写：切换分配、另存、后台提交不隐式保存未确认单材质编辑。GUI→后台先保护并保存独立持久快照、封包静态依赖，再提交CLI；候选完成后不自动加载覆盖当前场景。MCP用于实时发现/观察时核对会话身份，通过持久快照交换。多个入口不得同时写同一工程或输出。

具体GUI按钮以当前扩展实际界面为准；后台前保存独立持久快照并验证依赖，不自动加载候选覆盖用户场景。

## 4. 旧静态图层的有限对照：study

本节只适用于已验旧batch/study合同。程序化候选由独立manifest/新作业形成；只修改关键参数并保持比较条件，不能声称study已支持2.0信号、图像替换或几何遮罩刷新。

先明确待判断现象与有效参数，再选1–2处代表性ROI、必要柔光/斜光或原色视图；这是节省筛选成本的建议，不代替最终完整视角。准备完整batch基线并点名study.assignment_id/layer_id。cases只覆盖指定层7项数值：

`normal_strength / roughness / metallic / ior / coat_weight / coat_roughness / coat_ior`

未提供字段继承基线，不补新的override默认。study不含roughness_scale/bias、Height控制、纹理替换、层/通道开关、图层结构或映射探索，也不负责建立内腔机位/灯光。可表达的图层/通道修改按当前run/batch字段形成独立候选；特别机位/补光优先适用入口，缺口时在授权的独立副本或仅输出诊断图的临时场景准备。存在贴图覆盖时先明确改常量是否有意义。

同一目标的scale/bias多候选不能声称已能一次study扫描。可用少量独立run保存同基线候选；只有已准备不同对象/槽、独立材质ID和公平视图时才考虑batch。batch拒绝重复object/slot及重叠面分配，不能靠把同一目标重复列三次自动隔离版本；隔离场景需明确建立并验证，不值得时记录当前接口缺口。用户称“更亮”时区分基色/曝光亮度与镜面高光清晰度，先查现有需求；不能直接全部归因roughness。

当前预算：1–8 case、每case最多6 view，case数×view数≤32。case ID按casefold唯一并避开Windows保留名，不生成无界笛卡尔积。color_mode=white只剥点名层BaseColor图并设RGBA(.65,.65,.65,1)，不关闭其他图层或全器去色；需要彻底隔离时建立明确诊断清单。

一次读compact摘要中的基线/差异、无效旋钮提示、语义风险、每case状态/工程/图像及拼图、当前阶段与恢复引用。完整报告/语义分析通过SHA引用按需回读。采样Normal长度/Z/XY偏置、颜色/粗糙度关系等只提示风险，可能漏稀疏缺陷，不能证明物理正确、配准或审美。

进度字段仅在实际存在时返回，早期阶段没有图像不等于失败。警示/失败摘要可能截断，核对omitted计数；缺少决策所需信息时回读完整引用，不假定摘要齐全、不重新提交制作请求来获取更多字段。

实际解码并查看对照图，数值、技术、视觉与用户审美分记。候选由人选定，再用明确run/batch制作完整视角与交付方案；局部筛选图不当完整作品验收。已合格贴图可复用，单纯参数变化不触发全套重生。

诊断按当前现象选择有界full/no-height/层/单通道对照，不默认跑齐所有组合；已有run/batch/study能表达的部分直接复用。公平机位/灯光、受控开关与结果读回须明确准备，不能声称已有通用诊断预设；首批结果足以定位后停止该判断，保留必要复验。

若判断要求多个图层同步改宏观Normal，当前study只覆盖一个点名层，不能当作全部外层开关；使用明确run/batch候选同步对应层并读回，其他通道/映射/观察条件保持。

## 5. 进度、partial、取消与恢复

提交成功/queued只说明已登记。短任务一次结果读取；长任务用原jobs目录和job_id执行job wait --wait-seconds <0–55>，返回终态或当前进度，等待未完成不重新提交。摘要应提供case/assignment/view、阶段、计划/完成/成功计数、最新检查点/图像与下一动作；完成项比例不当剩余秒数。

核对退出码、JSON ok/error和异步终态，并读整体status及每case/assignment/view。ok=true或退出0可能对应partial；独立消费者失败继续可行分支，依赖失败阻断相关分支，CONFLICT/取消/超时按保护停下。保留成功与失败证据，只重试受影响范围。取消只针对本任务作业，核对终态和锁释放。

study恢复传前次**study报告**resume={file,expected_sha256}，不是直接传内部batch报告；batch恢复遵循其自身合同。核对原目录归属、报告SHA、检查点、源/资源/实现/任务/视图指纹。study内上一成功候选可复用未变分配，相同候选可复用已验图像；进行中active_case不可变快照可恢复已完成分配/视图。保留原作业目录与依赖，搬走报告不能冒充可续跑作业。

程序化目前无公开resume字段。读取`procedural-report.json`、progress、日志、已有author/reconstructed及bake report后，按失败阶段从明确已验持久快照新建作业；已有合格author可交给procedural-export，无需重做特殊图案输入。没有完整通过的author不能直接当成功恢复点。不可将旧job重复提交、改旧SHA或构造resume字段当续跑；具体交接见[程序化PBR](procedural-pbr.md)。

程序化2K多通道可能超过默认180秒，按规模显式设置全局timeout；本轮示例采用`--timeout 3600`。长作业可用全局`--async`提交后读取原job进度和终态，均放子命令前；timeout是预算而非速度承诺。超时产物保持partial/失败，先查已有持久输出再决定新的受保护作业。

参数影响的图须重渲，引擎等作用域改变按合同重建。手改冲突、坏SHA或篡改不得通过改receipt/旧SHA、关闭检查或换工具掩盖。实际reused结果与证据才证明复用，请求reuse不证明缓存命中。

## 6. 正式交付与反馈收尾

复制前根据实际引用证据检查live图像、orphan图像与外部依赖，不能到最终图数门卫才发现遗留；当前报告缺分类或旧封包未分类时补取证并标未知项。分类不明不自动拒绝交付，也不能把实际依赖闭包的未知项标成通过：保守保留可取得资源，逐项说明范围；发现缺失的实际依赖只补齐并复验受影响部分。orphan/users=0不自动授权删除，图片数据块的外部文件仍可能是保存依赖；需要fresh重组时列计划并在新候选核对目标/非目标保护，保留原工程和恢复证据。

选定后冻结最终清单与参数，按约定、目标变化及风险覆盖整体、关键局部、必要接缝/内腔和换光，已约定的完整验收范围保持。复制到独立正式目录并验证相对依赖，回接磁盘原图，独立重开与代表图重渲；未完分支保持partial或待修，不能随成功分支冒充全部交付。详见[验收与晋升](acceptance-and-promotion.md)。

交付可编辑工程、外部图层/遮罩、参考/通道与来源清单、各视角原图、受预算保护的拼图/预览、参数/复现请求、报告与恢复入口。程序化另保留仅贴图重建工程，读回导出实际通道、分辨率、位深、色彩/IOR/Height编码和SHA；技术pass之外必须实际查看author/重建的新渲染。Packed、交换、时序和目标引擎测试按用途裁剪，未验项直说。来源/许可绑定具体用途，资产入库转专用技能。

请求最终质量反馈前备齐完整可审查目录、README及验证汇总，包含真实文件/SHA、已验/未验范围和复现/恢复入口；user_feedback待回复，不预填通过。用户回复且产物身份/验证仍匹配、无新缺口时，追加绑定实际SHA的独立侧记及必要当前任务记录；允许确定性补齐目录说明与已验引用。新证据发现依赖/验证缺口时只处理受影响范围；反馈要求作品修改时回对应制作/验收分支并绑定新产物。已获真实反馈不重置待回复，用户审美不由工具成功或代理判断代替。

分账记录提交/状态调用、发现/结果字节、人工判断、生图、组装、保存重开、预览/渲染与失败恢复；比较保持相同质量，不把少调用写成全流程速度倍数。GPU并行争用时不作隔离基准。当前任务保留实际证据，技能只沉淀稳定可执行规则；进入下一材料前确认它自己的需求。

有限实现/运行证据与未验范围见仓库`docs/MATERIAL_WORKFLOW.md`；历史图像案例另见[釉瓷指导](bluewhite-glaze-case.md)。工具技术结果不替代新材料观感与用途判断。
