---
name: blender-rigging
description: 设计、制作与修复 Blender 角色、生物及可动游戏资产的骨架、蒙皮权重、面部表情、次级动态与动作重定向。也适用于已有角色的身体 FBX、摆姿、脚底接触和现场手调接续。
metadata:
  version: "1.0.0"
---

# 角色、生物与可动资产绑定

本技能归属 [绑定部门](../blender-work-methodology/references/departments/rigging.md)，负责把定稿模型做成动画师顺手好调的运动与变形系统。交出的成果包括可编辑绑定工程、控制器清单、复位姿态方法与动作范围验证。动手前先用 `blenderctl.ps1 discover --product rigging` 查询当前宿主支持的发行版与动作入口，按返回的参数和工作副本流程推进。

```powershell
$ctl = './blenderctl.ps1'

# 查询绑定产品支持的发行版与入口
& $ctl discover --product rigging

# 查看当前推荐候选或固定版的具体动作
$edition = '<发行版>'
& $ctl discover --product rigging --edition $edition --actions
```

## 第一步：摸清运动职责，选最轻的绑定路线

先读上游交来的模型工作副本、对象名清单和委托动作范围，算出源文件 SHA-256 留作基线。把每个活动部件的职责拆成四句话：允许怎么动、挂在谁身上、由哪一层变形、交给谁消费。

- **简单刚性运动**：台灯摆臂、旋钮旋转、门盖开合这类整件绕轴或沿轨运动的道具，直接设准物体原点、父子层级或基础变换约束即可交动画，省去整套骨架维护。
- **角色全身与生物四肢**：先查清物种的真实解剖关系，再选模板或模块化骨架。遇到陌生生物、特殊机构或未知目标引擎规则，先做只读拆解和资料复现，把关节中心与运动规律理顺再建骨。
- **已有外部绑定或旧角色返修**：先只读识别原骨架、控制器、形态键、Action 和驱动依赖。用户要求保原架构就走原骨架维护路线；需要换成工作台体系就在独立副本上重建并核对迁权映射。

详细原理与跨物种方案见 [变形原理、解剖与身体方案](references/anatomy-and-body-plans.md)，交互道具、载具、武器、植被、柔性物与运行时消费见 [游戏可动资产与运行时消费](references/game-assets-and-runtime.md)。

## 第二步：先做未蒙皮骨架，过双审再求权（审看①）

新角色或结构大改的绑定，把“做骨架”和“刷权重”严格分成两段。先交出未蒙皮的待审骨架，把关节位置、骨轴朝向和控制器运动全部验实，再进入蒙皮求权；骨架改动会直接推翻整身权重，把关口前移最省返工。

1. **按真实解剖安放关节**：人形分清胸锁（SC）、肩锁（AC）与盂肱（GH），让锁骨连向肩峰、肱骨从肩峰下方伸向肘部；犬猫等四足按肌肉悬吊肩胛定位，分清后肢跗关节与膝关节。数字控骨按用途简化，并写明每根骨的枢轴与职责。
2. **定好局部轴与弯曲平面**：Blender 骨长轴固定是局部 Y（head 指向 tail），Roll 决定主弯曲平面。摆完静置姿态后核查弯曲方向、预弯与缩放继承；对称部件可用定位会话的 `MIRROR_L` / `MIRROR_R` 镜像修正端点后重新生成。
3. **出全向分区叠图**：按躯干、头颈、左右肢与附属分区赋稳定颜色并附图例。静置姿态输出 9 宫格或 16 宫格（正、背、左右、顶、底加斜向），密集关节补近景；同时保留实面叠骨图与独立骨骼轴向图。
4. **验无皮肤运动与切换**：不挂皮肤，逐项测 FK 屈伸轴转、肩带骨盆联动、IK 弯曲平面与近伸展、手脚支撑、中间帧、左右对称与回静置姿态，并记录 FK/IK 保姿态切换残差与无伸缩骨长。
5. **走主审、独审与用户审看①**：主制作与独立审查看齐全部多向图与运动记录，签认绑定当前骨架、参照与运动范围的通过回执，再摆代表姿态请用户确认。

具体验收清单、四足禁伸缩核查和短命配对匹配方法见 [骨架质检、无皮肤运动与配对匹配](references/skeleton-gate-and-matching.md)。

## 第三步：体素蒙皮、部件转权与局部修权

骨架通过审看①并拿到匹配回执后，再在工作副本上求权和修权。新角色默认走体素蒙皮公共入口，按部件性质分流：身体走单域或多域体素求解，贴体衣物从已应用身体做同作业转权，硬饰品走刚性单骨绑定。

```powershell
# 1. 检查整角色输入、复算域与同作业转权范围、求权试片与冷开核对
& $ctl rigging-workflow candidate-entry inspect  --request $req --run-dir $runDir
& $ctl rigging-workflow candidate-entry scope    --request $req --run-dir $runDir
& $ctl rigging-workflow candidate-entry audition --request $req --run-dir $runDir
& $ctl rigging-workflow candidate-entry result   --request $req --run-dir $runDir

# 2. 审片通过后产出交付副本（可开启 isolate_bound 聚焦骨架与已绑部件）
& $ctl rigging-workflow candidate-entry finish   --request $req --run-dir $runDir
```

- **同作业部件转权与多域缓存复用**：在 `candidate-entry scope` 的 `transfer` 块里写明目标部件、刚应用好的供体部件与最大采样米距，写前自动拦冲突与超距；重开后继续同一角色时，在请求里传入上次的 `worker_jobs_dir`，未变域直接复用缓存。
- **肩胯肘膝局部修权与选骨重算**：先做分层诊断，区分原模形体、骨架轨迹、权重支持还是法线问题。确属局部权重支持不足时，用 `candidate-selected-skin inspect` 预检受影响点与 `coincident_boundary_pairs`（把接缝两侧拆点成对纳入，避免弯折开缝），再用 `execute` 在新副本上只重算所选骨骼，原样保留其余骨组权重。同骨架供体迁权走 `candidate-skinning-policy qualify|plan|preview|apply`；作者返工或旧绑重建走 `candidate-repair inspect|review-handoff|apply`。
- **统一视口看图标准**：检查皮肤变形时，保持几何表面完全不透明、关闭表面透显（X-Ray），叠加完整网格线框（中性色、只给关注区域标色、线框透明度约 0.6–0.7），开启屏幕空间曲率（Curvature），并配同机位无叠线实面图对照。左右成对部位同步抽查中立、中间、峰值与反侧。
- **睫毛与嘴唇默认交人工精修**：自动初绑中如发现睫毛或嘴唇出现粘连、微穿插或局部尖褶，记录对象名、动作帧和局部对照图，标明待人工修复，继续完成身体、其他部件与冷开验收。

权重诊断表、表面回传、辅助骨与拓扑重建边界见 [体素蒙皮、局部修权与变形诊断](references/voxel-skinning-and-local-repair.md)。

## 第四步：表情控制、衣物附件与次级弹性

身体与主服装蒙皮稳固后，再叠表情、饰品和次级动态，保证每一层都在上一层的稳定基线上工作。

- **面部表情与控制板**：先确认头部基础权重，再用骨骼混合设计标准表情并整组采样为形态键；跨皮肤与附件同步同一表情时，走多网格形态键映射保持中立一致。交付前整理控制板行名、分区与显隐，按最新布局指纹保存。深屈曲需要补形时，先做单侧单态目标曲面并验证原生逆解与中间帧，再合并多锚点驱动曲线。详见 [面部表情、姿态修形与控制板](references/face-correctives-and-controls.md)。
- **服装附件与次级动态**：硬扣件、耳挂、头盔走单骨刚性跟随或挂点切换；裙片与挂饰按需建独立附属链（固定版 `c94_workflow.bind_accessory_copies`）。制作胸臀弹性与发裙摆动时，先锁定身体主动作并烘焙胸臀弹性（`elastic_action.BAKE`）到独立 NLA 轨，再在保存副本上配置固定点顶点组与碰撞集合烘焙发裙表面动态（`surface_dynamic_action.BAKE`），并用 `controls.mix` 调叠加强度。详见 [服装、附属物与次级动态](references/garments-accessories-and-dynamics.md)。

## 第五步：动作重定向、现场接续与交给下游

- **已有 C94 与普通原生 FK 人形**。接身体 FBX、摆三拍、校核肩带或修脚跟时，先用 `rigging-workflow phase3 describe` 确认 Win53 专项开发候选。按带 SHA 的目标副本生成独立 Action，记录源 Take、目标 Slot 及两端时基。在自有工作窗口写入手调键，再捕获并独立重开。 [已有角色的摆姿、动作迁移与窗口接续](references/rigging-posing-win53.md)
- **接外部动作与修脚底接触**：导入 MMD（PMX/VMD）或通用 FK（BVH/FBX）动捕时，通过 `discover --product rigging` 查清重定向队列与转换入口，按源帧率与目标帧率对齐身体和表情通道，记录 Action 与 Slot。出现脚滑或穿地时，走后台足底检测并生成交接单，送到现场审阅确认或撤回。用户在打开窗口里做了手调时，先走 `rigging-workflow session-live` 收拢状态再存副本。详见 [动作重定向与工作台 CLI 操练](references/retargeting-and-cli-operations.md)。
- **独立冷开并交接动画部门**：最终交付副本另起进程冷开复验，核对源 SHA、部件拓扑与 UV 完整、全部变形骨权非零覆盖、代表姿态无爆点且能干净回静置姿态。交付清单写齐以下四项：
  1. 保存好的绑定工程副本路径与 SHA-256。
  2. 控制器清单、常用控制板布局，以及一键回静置姿态的步骤。
  3. 动作范围测试样例（含 FK/IK 切换、代表帧或测试 Action/Slot）、表情映射与次级弹性轨道说明。
  4. 已知极限姿态变形边界说明，以及需人工精修的极个别帧位（如睫毛、嘴唇细微交叠）。

## 例子：人形角色从拟合骨架到体素蒙皮与动画交接

接到委托：「给 `work/char/myona/model-whitebox.blend` 做人形全身绑定，覆盖日常站走、抬臂和下蹲，身体配贴体袜子与刚性耳挂，交动画部门做表演。」

1. **查入口与基线**：运行 `blenderctl.ps1 discover --product rigging` 确认当前推荐候选版与固定版入口；建批次目录 `work/audit/<batch>/`，算出源工程 SHA-256 并只读检查部件名（身体、右袜、左耳挂）。
2. **定位骨架并过审看①**：按人体 SC/AC/GH 与四肢真实枢轴拟合参考骨，镜像校正左右足跟端点后生成未蒙皮骨架。导出分区配色 16 宫格叠图与无皮肤抬臂、下蹲、FK/IK 切换记录；主审、独审和用户依次签认骨架回执。
3. **跑体素蒙皮与同作业转权**：在 `candidate-entry scope` 里配置身体多域体素求权、左耳挂刚性绑至头骨，并在 `transfer` 块里把右袜设为从刚应用的身体近面转权；运行 `audition` 生成审片副本。
4. **看图复查与局修**：打开不透明实面加完整线框和曲率对照图，核对右袜在大开腿与深蹲下紧贴右腿、零跨侧拉扯，耳挂随转头刚性跟随；若肩部接缝有微拆点，用 `candidate-selected-skin inspect` 查出成对边界点后只重算上臂骨。发现睫毛和嘴唇在极值帧有轻微交叠，登记为待人工精修项。
5. **冷开收尾交动画**：运行 `candidate-entry finish`（开启 `isolate_bound`）并用 `result` 在新进程冷开核验全部部件、动作态与源 SHA；把交付副本、SHA、控制器说明、复位方法和人工交接帧表交给动画部门。

## 参考

- [变形原理、解剖与身体方案](references/anatomy-and-body-plans.md)
- [骨架质检、无皮肤运动与配对匹配](references/skeleton-gate-and-matching.md)
- [体素蒙皮、局部修权与变形诊断](references/voxel-skinning-and-local-repair.md)
- [面部表情、姿态修形与控制板](references/face-correctives-and-controls.md)
- [服装、附属物与次级动态](references/garments-accessories-and-dynamics.md)
- [动作重定向与工作台 CLI 操练](references/retargeting-and-cli-operations.md)
- [游戏可动资产与运行时消费](references/game-assets-and-runtime.md)
- [历史发行版、审计路径与来源台账](references/sources.md)
