---
name: blender-work-methodology
description: 把一份 Blender 委托拆成部门工序，排定谁交什么给谁、什么时候停下来给用户看、每一步怎么验收。接到资产、角色动画、镜头、成片或返修这类需要多个环节配合的委托时，先读这里，再按部门卡进入专项技能。
metadata:
  version: "1.0.0"
---

# Blender 制片方法

这份技能管部门之间的事：一份委托要经过哪些部门，每个部门从上游拿什么、交给下游什么，什么时候请用户审看。部门之内怎么做，读该部门的专项技能。所有对已保存 .blend 的操作都通过 blenderctl 完成，调用方法见 [blender-cli](../blender-cli/SKILL.md)。

## 接到委托先做三件事

1. **用一句话写出交付结果。** 这句话要能当场检验，比如「一个能拖进新工程、零件可改的监听控制器资产」。
2. **问清用途。** 谁用、在哪用、要保留什么可编辑、用户会从哪个角度判断好坏。尺寸、帧率、引擎能从文件和镜头表读到的就自己读，只问会改变路线、又查不到的事。
3. **从成品倒推工序。** 对照下面的部门表和「作品串联」，排出要经过的部门和先后顺序。

## 十个部门

| 部门 | 管什么 | 专项技能 | 部门卡 |
|---|---|---|---|
| 前期参考 | 参考图、尺寸、结构研究、找教程 | blender-modeling-reference · blender-tutorial-scout · simready-hard-surface-research | [reference](references/departments/reference.md) |
| 建模 | 白模、硬表面、外部生成、几何节点 | blender-hard-surface-modeling · blender-modeling-workflow · tripo-studio-opencli · blender-geometry-nodes · blender-modeling-review | [modeling](references/departments/modeling.md) |
| UV 与材质 | 展 UV、材质节点、贴图、烘焙 | blender-uv · blender-material-authoring | [surfacing](references/departments/surfacing.md) |
| 绑定 | 骨架、蒙皮、控制器、表情 | blender-rigging | [rigging](references/departments/rigging.md) |
| 动画与模拟 | 关键帧、事件节拍、物理、次级动态 | blender-animation-workflow · blender-physics-workflow | [animation](references/departments/animation.md) |
| 镜头与灯光 | 镜头表、相机、灯组 | blender-camera-workflow · blender-lighting | [camera-lighting](references/departments/camera-lighting.md) |
| 渲染与风格化 | 三渲二、描边、批渲染 | blender-toon-workflow · blender-render-batch | [render](references/departments/render.md) |
| 合成 | 通道合成、局部修正、输出 | blender-compositing | [compositing](references/departments/compositing.md) |
| 剪辑 | VSE 剪辑、换片、字幕声音、成片 | blender-video-editing | [editing](references/departments/editing.md) |
| 资产入库 | 命名、Catalog、发布、出新版 | blender-asset-manager | [asset-library](references/departments/asset-library.md) |

## 作品串联

下面四条是最常见的委托。加粗的是默认的用户审看点。

### 一件可复用资产

前期参考 → 建模 → **审看①白模** → UV 与材质 → **审看②最终材质** → 资产入库

白模审过再展 UV，形体一改 UV 就得重展。材质在中性灯光下做、下审，成片灯光另做。用户认可材质后直接入库，在新工程里拖进来验收。

### 一个会动的角色

前期参考 → 建模（或外部生成）→ 绑定出骨架 → **审看①骨架** → 蒙皮、控制器、表情 → 动画 → **审看②动作预览**

骨架在角色要做的姿态下审，通过后再蒙皮，骨架一动权重就作废。材质可以和绑定同时做。

### 一个产品镜头

资产就位 → 镜头与灯光定镜头表 → 动画或模拟 → **审看①分镜预演** → 灯光与材质精修 → 渲染 → 合成 → **审看②最终帧**

分镜预演用实体模式播放，看机位、构图和节奏。预演定了再投入正式渲染。

### 多镜成片

每个镜头按产品镜头走到预演 → 剪辑出 **审看①粗剪** → 各部门按粗剪意见修 → 正式渲染、合成 → 原位换片 → **审看②精剪** → 导出成片

镜头表是全片的时间主档。剪辑要改切点，先改镜头表，再让相机、动画、剪辑各出候选。

### 返修

从用户认可的版本和当前工作副本出发，沿对象、镜头、缓存的依赖，找到第一次出错的部门，在那里修。修好后把受影响的下游重新跑一遍，用同机位、同条件的前后对比给用户看。

## 审看点与商业质量标尺

每次任务按用途归入四档标尺：**L0 草稿探索**、**L1 镜头自用（≥65 分）**、**L2 Blender 商业标准级（默认交付与入库放行线，≥80 分且各梯度 ≥70%）**、**L3 旗舰主角级（≥90 分且核心梯度 ≥85%）**。

全部任务统一用同一套「红线一票否决 + 五梯度加权 100 分成绩单」衡量，按从高到低顺序卡关，前一级不过当场打回：

1. **G1 工程生死门（15 分）**：冷重开、零丢贴图与本机绝对路径、零负缩放、数据贴图 `Non-Color`、切刀关渲染可见、修改器视口与渲染级别一致。
2. **G2 结构、拓扑与大纲规范（25 分）**：1 米单位、正面朝 `-Y`、落地原点与缩放 `1,1,1`、单一根集合、清零 `Cube.001` 默认名；模型看四边面控制笼与极点，绑定/镜头/合成看骨骼、节点与时间线组织规范。
3. **G3 造型、构图与参考还原（25 分）**：多视角轮廓、物理比例或镜头构图与事先定下的一致性核对单逐项吻合。
4. **G4 光影法线与运动连续（20 分）**：5.x 法线栈与条形高光顺滑无夹捏，角色弯折、机构行程或帧间画面连续稳定。
5. **G5 UV、PBR 与交付品相（15 分）**：SubD 锁角、UV 边距与纹素密度、反照率物理区间、Cycles 与 EEVEE Next 光追双引擎及纯贴图重建一致（只交白模或前中期阶段时，按适用梯度满分折算百分制）。

模型与资产直接跑 [blender-modeling-review](https://github.com/tsist/blender-modeling-review/blob/main/SKILL.md) 生成 `quality-scorecard.json`；绑定、动画、镜头、合成、剪辑按 [各领域验收要点](references/domain-acceptance.md) 出一份同结构成绩单。**L2 与 L3 任务在送用户审看和最终交付前，先派一名未参与制作的独立只读子代理盲审打分，达到放行线再呈交用户。**

| 作品 | 审看① | 审看② |
|---|---|---|
| 资产 | 白模：同机位的控制笼、细分白模、移动高光，加一次局部改型 | 最终材质：节点版、纯贴图重建版与 EEVEE Next 光追对照，含整体、近景与棋盘格 |
| 角色 | 骨架（蒙皮前）：和角色同角度叠图，摆出要做的姿态 | 动作预览：可播放的片段 |
| 镜头 | 分镜预演：实体模式连续播放 | 最终帧：正式渲染加合成 |
| 成片 | 粗剪 | 精剪 |

其余部门自检达标后直接交给下游。委托里另有约定时按约定来。每次请用户看，附上本轮成绩单摘要、改了什么、想请用户拍板什么。

## 交接带四样东西

每次把活交给下一个部门，都带上：

1. **保存好的工作副本**：路径和 SHA。
2. **名字**：对象、镜头、资产或 Action 的确切名字，下游靠它找东西。
3. **检查结果**：这一步查了什么、结果怎样、还有什么没查。
4. **退路**：原作业号或上一版文件，出了问题能回去。

下游接手先核对名字和版本对得上，再开始干活。

## 推进节奏

- 先做能决定成败的小样，再铺开。
- 陌生领域先从 tutorial-library 和 Wiki 找 1–3 个好案例，照做一遍，再按差距调整。
- 一轮是一次修改加一次同条件观察。看得出改善才算进展。连续两轮、三轮没进展怎么办，按 AGENTS.md 的止损规则走。
- 失败时留着产物和回执，先判断是输入、版本、依赖还是实现出了问题。只重跑受影响的步骤。
- 正式写入前，确认目标文件还是你接手时那一版。用户可能在这期间改过。

## 看图与传图

传进会话的图，最长边 2048 像素以内，每张 2 MiB 以内。日常各阶段快检（白模布线、随机分件色、MatCap 曲面、法线红蓝翻面、LookDev 材质叠半透明线框、Cycles DLSS/OptiX 或 EEVEE Next 快速渲染预览）统一优先用 `./blenderctl.ps1 preview <file.blend>` 一键直出（已默认限宽 ≤ 2048px 且隔离桌面零干扰，多轮迭代可开 `preview session` 复用）；正式成片大原图留着原件，另做预览图传进会话。照片和渲染用 JPEG，透明图、线稿、文字用 PNG。

## 例子：一盏桌面台灯

用户说：「做一盏工业风台灯，放进资产库，以后场景里能用。」

1. 交付结果写成：「一盏能从 User Library 拖进新工程、灯臂可调、零件可改的台灯资产。」
2. 前期参考：用 blender-modeling-reference 做主效果图和正交三视图，标出底座直径、灯臂长度。
3. 建模：灯座、灯臂、灯罩都是规则硬表面，走原生建模和硬表面专项。灯臂关节留好枢轴，方便以后摆姿势。
4. **审看①**：给用户看同机位的控制笼、细分白模和移动高光，现场演示调一次灯臂角度。用户提出灯罩再大一圈，改完再看一次。
5. UV 与材质：灯罩内侧要烘焙磨损，用独占 UV；四颗相同的螺丝共用一套 UV。材质做哑光烤漆和拉丝金属。
6. **审看②**：节点版和纯贴图重建版并排，给整体、灯罩近景和换一盏灯光的效果。
7. 资产入库：入 User Library 的 `Models/Props/Lighting`，在新工程拖进来，确认是轻量集合实例，转本地后零件、材质、修改器都能改。

交付时附：资产在库里的位置、两次审看的对照图、检查清单的结果。

## 参考

- `部门卡`
- [各领域验收要点](references/domain-acceptance.md)
- [技能写作规范](references/skill-writing-guide.md)
- [已有制作方法](references/method-index.md)
- [现场窗口与 Blender 数据要点](references/mcp-and-blender-semantics.md)
- [批量执行、封装时机与恢复](references/efficient-work-units.md)
- [知识检索、沉淀与开源复用](references/knowledge-and-oss.md)
- [实践记录与方法沉淀](references/practice-record.md)
- [来源与证据索引](references/sources.md)

维护：唯一维护源是 `./.agents/skills/blender-work-methodology/`，改完运行同步脚本更新各镜像。
