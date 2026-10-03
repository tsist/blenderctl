# PBR插件、CLI与MCP执行路由

先遵循[插件+CLI最高指导](../../../blender-work-methodology/references/plugin-cli-work-units.md)。插件封装领域连续操作，CLI组织输入/版本/作业/批量/保存/验证；宿主代理调用原生imagegen。插件不自行假设能访问Codex的原生生图会话。

## 可复用工作单元

材质任务把“图层清单校验→加载图像与色彩空间→建立节点/参数→映射/分区→保存候选→预览/回执”组织为有检查点的插件工作单元，由CLI调度。Material Workflow 0.7.0已真实安装并完成本机验收；当前操作顺序见[插件与CLI材质操作章](plugin-cli-authoring.md)。每次先准备完整输入与验收目标，再按范围选择单目标、批量或有限候选对照。

有适用插件时核对来源/版本/接口、headless支持、授权和真实小样；缺口先评估现有插件适配或自研扩展。API/RNA可创建节点不证明公共CLI白名单允许；安装/启用仍遵循已有授权和项目规则。

## 已有操作入口

本机材质工具已验基线：blenderctl 0.54.1、Material Workflow 0.7.0、Blender 5.2.1 LTS/build 9e2066aef7ef。执行时核对当前Schema/版本；本表是导航而非永久能力承诺。

| 目标 | 入口 | 要点 |
|---|---|---|
| 发现材质接口 | material.describe | 按需读取section；编写请求前核对真实request schema，不搬入无关全文 |
| 单目标材质构建 | material.run | 一个目标的绑定、构建、保存和预览；保留源与依赖SHA |
| 显式批量分配 | material.batch | 多目标明确assignment；逐项报告与失败隔离，不把partial当全部成功 |
| 有限参数/视图对照 | material.study | 固定条件、有界case/view，返回基线差异、语义风险与进度；参数范围见操作章 |
| 保存可复用模板 | material.template-save | managed材质模板和独立bindings；不是任意手工节点图的全保真导出 |
| 几何/UV检查候选 | model.inspect / model.prepare | 求值网格、UV重叠/接缝等按实际返回字段判断 |
| 图像/材质/节点 | node.inspect / node.prepare / query | 当前白名单与真实标识定位；读回实例输入和连接 |
| 冻结目标画面 | render.devices / render.run | 明确设备、相机、场景、色彩与采样；对照计量避免并行GPU争用 |
| 工程与依赖 | project.verify / prepare_copy / dependency.audit | resources实际枚举；open不等于render或所有依赖闭合 |
| 插件/适配器试验缺口 | extension.run或授权的最小bpy | 绑定脚本/输入SHA，明确trust/隔离，独立候选输出；不绕过已知安全拒绝 |
| 批处理与恢复 | pipeline.plan / run / resume | 当前ALLOWED命令，复用绑定正确manifest；one batch仍有检查点和真实终态 |
| 技术交付烘焙/交换 | texture.bake / exchange.*或专用插件 | 只承担已生成图层的技术传递或当次明确要求；记录源/目标/编码，不偷换内容来源 |

当前texture.bake为单对象、单tile、PNG/Non-Color，selected_to_active=False；不是独立高低模投射接口。历史投射实验可查询，但不再作为本生图流程的强制阶段。若需通道打包/重投影/格式交换，查当前接口并实际验证目标读取结果。

CLI使用<仓库根>/blenderctl.ps1，全局参数在子命令前；复杂请求UTF-8 JSON、绝对路径、输入/依赖SHA和独立审计目录。材质作业当前为standalone，不加入pipeline DAG；通用pipeline能力不代表material命令已获DAG支持。核对ok/error与终态后，仅消费逐项验证通过的产物；partial保留成功项，失败项不作为交付，不并发写同一工程。extension输出所在job目录未必有相对textures，必须按回执复制到正式布局再重开验证；Packed另在无外图目录验。

异步提交后用原job的有界wait（0–55秒）读取进度与终态；queued或等待超时不等于失败，也不应重新提交同一制作请求。study恢复绑定前次study报告的file与expected_sha256；不要把batch恢复请求直接套用。缓存/检查点必须核验目录归属、SHA和指纹，变化影响的视图重新渲染；当前case不可变恢复快照和partial隔离按报告读回。

## 实时与离线

实时GUI场景探索用MCP，执行前核对连接、文件、Scene/View Layer、对象/模式、共享数据和未保存状态。历史localhost:9876断连只描述取证当时，不能替代新检查。

MCP的*_for_cli是独立后台入口，不继承blenderctl的SHA/作业/事务契约；临时快照可能清理。入口交接必须持久保存并重新取证。只有UI上下文可用的插件不能声称支持后台CLI；要实现适配或明确MCP交互部分。

当前实际状态由本次输入、Schema和作业结果取得，不继承历史作品验收。公开软件验证与材料视觉验收分别记录。
