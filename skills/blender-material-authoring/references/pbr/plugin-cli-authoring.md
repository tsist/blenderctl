# 插件与CLI材质制作

入口：[Blender材质制作](../../SKILL.md)。先遵循[插件与CLI最高指导](../../../blender-work-methodology/references/plugin-cli-work-units.md)，再采用其[次级效率指导](../../../blender-work-methodology/references/efficient-work-units.md)。本文面向实际组装、更新、对照与恢复；生图来源完整约束在[framework](framework.md)，不因工具方便改用程序纹理。

## 1. 已验工作面与按需发现

2026-10-04本机已验CLI0.54.1、Material Workflow0.7.0、Blender5.2.1 LTS。执行前按影响核对实际版本与安装身份；当前范围包含静态分层PBR、受管编辑、显式批量、模板/bindings、Cycles/Eevee受控预览、GUI持久快照和有限study。可见GUI像素交互、自动通用UV、UDIM/动画贴图、DLSS5、完整NPR风格模板及通用物理校准未由本批证明。

先读blender-cli技能。按需用`material describe --operation <操作> --section <相关章>`取得layers/target/preview/manifest，study另有study章；需要完整请求结构时才用`--schema`，两者不同时给。template-save没有分层manifest，查其完整请求。全局选项放子命令前，复杂请求保存为UTF-8 JSON，源/资源为绝对路径。不要复读整包Schema、长面列表与节点树。

| 本轮目标 | 首选入口 | 范围 |
|---|---|---|
| 单目标分层组装或明确增量 | material.run | 图层→映射/节点→分配→保存重开→可选多视图/拼图/报告 |
| 多对象、槽位或面区域，独立失败隔离 | material.batch | 显式assignments/dependencies；同worker顺序写，按分配与视图恢复 |
| 指定层的有限数值探索 | material.study | 同一batch基线，少量cases、固定views、语义风险/差异/进度/工程/图像 |
| 保存可迁移受管结构 | material.template-save | 模板与实例bindings分离；不可表达手改须报告，非任意节点树全保真导出 |
| 仅检查已有材质外观 | material.preview | 原隔离预览入口，不替代分层组装 |

材质工作流为standalone作业，不默认进入pipeline DAG。describe是无Blender只读发现；原始契约来自material_interface.py（历史工程证据未随包发布）及其返回的Schema/SHA，执行选项以实际合同为准。

## 2. 需求与输入：一次准备可执行清单

制作前确认外观、分层、旧化/干净程度、观察尺度、用途/引擎和验收意见。文件可查的信息自行读取；对当前材料分别确认需求，推荐预选不等于已回答。已有明确授权/答案不重复提问。

从受保护源建立工作副本，核对Scene/对象/材质槽/面区域、UV、模式、共享材料/网格/节点组及库引用。法线、轮廓、光顺或UV导致的异常先回模型/映射阶段；不接入默认通用autoUV。源盘快照不能代表GUI未保存状态。

冻结参考与通道意图清单，分别原生生成所需空间通道/复杂遮罩；明确常量与not_applicable原因。通过[图像门卫](../../scripts/image_gate.py)核对解码、≤2048尺寸、编码/色彩语义、字节与SHA；会话预览另≤2MiB，不替换数据原件。按当前Schema投影为：

- 源file/expected_sha256，明确资源需求resources；不默认空资源。
- 唯一稳定assignment/layer ID，target对象/槽/面和UV；不用.001名称或显示位置猜身份。
- 有序layers、channels、mask、values/adjustments，已有模板与独立bindings；拒绝/采用图有明确版本。
- 固定preview视图/ROI、灯光、目标引擎/设备、色彩和质量；明确预算、jobs目录和持久输出。

意图清单中的来源/物性备注不是任意可提交字段。实际键、范围、依赖和profile/receipt要求以操作合同为准；保留本次需要的请求与来源SHA。

## 3. 组装、有效旋钮与GUI交接

插件和CLI调用同一受管核心：图像解码/角色→映射与调节→分层混合→着色输出→显式分配；核对实例读回、依赖与非目标保护。受管编辑可保留不重叠手改；无法表达或重叠冲突须报告，不能整树覆盖。

Roughness图存在时，values.roughness常量被连接覆盖；改纹理结果用run/batch支持的adjustments.roughness_scale/bias。Metallic图等也可覆盖相应常量。未提供Normal、关闭图层或Coat为0时相关旋钮可能不起作用；先读基线与提示，不连续提交微调同一无效值。

Normal当前为OpenGL +Y、TANGENT，使用明确UV。Height在当前层实现中走Bump，不自动称作真实几何置换；同一结构Normal与Height不满强重复叠加。Coat启用时共享最终Normal/Bump，尚无独立Coat Normal控制。真实薄膜/折射、多层物理釉及跨引擎表现另按用途验证，不将MixShader或受控参数等同物理校准。

Cycles/Eevee依支持矩阵核对。Eevee用GRAPHICS，不套Cycles设备id/denoise；ShaderToRGB限Eevee，未知节点组合不默默替换。源场景引擎与独立预览引擎分别记录。

GUI面板用显式草稿和回写：切换分配、另存、后台提交不隐式保存未确认单材质编辑。GUI→后台先保护并保存独立持久快照、封包静态依赖，再提交CLI；候选完成后不自动加载覆盖当前场景。MCP用于实时发现/观察时核对会话身份，通过持久快照交换。多个入口不得同时写同一工程或输出。

具体按钮和交接见[公开 GUI/快照说明](https://github.com/tsist/blenderctl/blob/main/docs/MATERIAL_WORKFLOW.md)：先确认单材质草稿回写，机位/预览分别提交，在 Object Mode 保存独立快照并提交后台。未保存像素、链接库、序列/UDIM及缓存等不支持的依赖先解决；仅save_as不替代草稿回写。安装身份和可见交互仍需现场核对，历史阶段证据不充当当前验证。

## 4. 一次有限对照：study

先明确待判断现象与有效参数，再选1–2处代表性ROI、必要柔光/斜光或原色视图；这是节省筛选成本的建议，不代替最终完整视角。准备完整batch基线并点名study.assignment_id/layer_id。cases只覆盖指定层7项数值：

`normal_strength / roughness / metallic / ior / coat_weight / coat_roughness / coat_ior`

未提供字段继承基线，不补新的override默认。study不含roughness_scale/bias、纹理替换、图层结构或映射探索；这些变化用run/batch。存在贴图覆盖时先明确改常量是否有意义。

同一目标的scale/bias多候选不能声称已能一次study扫描。可用少量独立run保存同基线候选；只有已准备不同对象/槽、独立材质ID和公平视图时才考虑batch。batch拒绝重复object/slot及重叠面分配，不能靠把同一目标重复列三次自动隔离版本；隔离场景需明确建立并验证，不值得时记录当前接口缺口。用户称“更亮”时区分基色/曝光亮度与镜面高光清晰度，先查现有需求；不能直接全部归因roughness。

当前预算：1–8 case、每case最多6 view，case数×view数≤32。case ID按casefold唯一并避开Windows保留名，不生成无界笛卡尔积。color_mode=white只剥点名层BaseColor图并设RGBA(.65,.65,.65,1)，不关闭其他图层或全器去色；需要彻底隔离时建立明确诊断清单。

一次读compact摘要中的基线/差异、无效旋钮提示、语义风险、每case状态/工程/图像及拼图、当前阶段与恢复引用。完整报告/语义分析通过SHA引用按需回读。采样Normal长度/Z/XY偏置、颜色/粗糙度关系等只提示风险，可能漏稀疏缺陷，不能证明物理正确、配准或审美。

进度字段仅在实际存在时返回，早期阶段没有图像不等于失败。警示/失败摘要可能截断，核对omitted计数；缺少决策所需信息时回读完整引用，不假定摘要齐全、不重新提交制作请求来获取更多字段。

实际解码并查看对照图，数值、技术、视觉与用户审美分记。候选由人选定，再用明确run/batch制作完整视角与交付方案；局部筛选图不当完整作品验收。已合格贴图可复用，单纯参数变化不触发全套重生。

## 5. 进度、partial、取消与恢复

提交成功/queued只说明已登记。短任务一次结果读取；长任务用原jobs目录和job_id执行job wait --wait-seconds <0–55>，返回终态或当前进度，等待未完成不重新提交。摘要应提供case/assignment/view、阶段、计划/完成/成功计数、最新检查点/图像与下一动作；完成项比例不当剩余秒数。

核对退出码、JSON ok/error和异步终态，并读整体status及每case/assignment/view。ok=true或退出0可能对应partial；独立消费者失败继续可行分支，依赖失败阻断相关分支，CONFLICT/取消/超时按保护停下。保留成功与失败证据，只重试受影响范围。取消只针对本任务作业，核对终态和锁释放。

study恢复传前次**study报告**resume={file,expected_sha256}，不是直接传内部batch报告；batch恢复遵循其自身合同。核对原目录归属、报告SHA、检查点、源/资源/实现/任务/视图指纹。study内上一成功候选可复用未变分配，相同候选可复用已验图像；进行中active_case不可变快照可恢复已完成分配/视图。保留原作业目录与依赖，搬走报告不能冒充可续跑作业。

参数影响的图须重渲，引擎等作用域改变按合同重建。手改冲突、坏SHA或篡改不得通过改receipt/旧SHA、关闭检查或换工具掩盖。实际reused结果与证据才证明复用，请求reuse不证明缓存命中。

## 6. 正式交付与下一例

选定后冻结最终清单与参数，完整视角覆盖整体、关键局部、接缝/内腔及换光。复制到独立正式目录并验证相对依赖，回接磁盘原图，独立重开与代表图重渲；未完分支保持partial或待修，不能随成功分支冒充全部交付。详见[验收与晋升](acceptance-and-promotion.md)。

交付可编辑工程、外部图层/遮罩、参考/通道与来源清单、各视角原图、受预算保护的拼图/预览、参数/复现请求、报告与必要恢复入口。Packed、交换、时序和目标引擎测试按用途裁剪，未验项直说。来源/许可绑定具体用途，资产入库转专用技能。

分账记录提交/状态调用、发现/结果字节、人工判断、生图、组装、保存重开、预览/渲染与失败恢复；比较保持相同质量，不把少调用写成全流程速度倍数。GPU并行争用时不作隔离基准。宿主有相应能力时将实际状态/稳定经验分别回写，技能只沉淀可执行规则；进入下一材料前确认它自己的需求。

已验来源：操作与进度说明（历史工程证据未随包发布）、安装与运行总验收（历史工程证据未随包发布）、study合同（历史工程证据未随包发布）、[青花实证](bluewhite-glaze-case.md)。新材料或新能力的美术/物性结论仍需实战。
