---
name: blender-material-authoring
description: 在定稿白模与合格 UV 上制定参考标准、规划材质分层、调用原生生图或程序化信号组装着色节点、执行高低模投射烘焙，并导出贴图完成 Cycles、EEVEE Next 与纯贴图重建三方对照。进入材质制作、贴图烘焙、材质返修或接手现场手调材质时使用。
metadata:
  version: "1.0.0"
---

# Blender 材质与贴图制作

本技能属于 [UV 与材质部门](../blender-work-methodology/references/departments/surfacing.md)，负责把定稿白模做出经得起近景和换光检验的材质与可移植贴图。开工前先确认白模与 UV 已定稿，形体一旦改动，UV 与烘焙贴图都要重来。实际读写 `.blend` 与跑后台作业时，配合 [blender-cli](../blender-cli/SKILL.md) 执行。

## 第一步：查接口、核输入、定参考

1. **先查真实命令与参数**。动手前查清当前工作区的可用动作与请求结构，省去猜参数的返工。
   ```powershell
   $ctl = './blenderctl.ps1'
   & $ctl discover --product material --actions
   & $ctl material describe
   & $ctl material describe --operation procedural-run --schema
   ```
2. **核对工程输入与几何起点**。在独立工作副本上先用 `inspect` 读出对象名、材质槽、UV 层名、单位尺度、面朝向和共享数据块。遇到翻法线、坏面、拉伸或接缝露在正面，先回建模或 UV 部门修好，把材质建在光顺的几何与干净的 UV 上。
3. **找齐参考并冻结质量简报**。每个新材质先收集并亲眼看过主参考和关键局部，从材料身份、宏观组织、中尺度结构、微表面与反射、物理尺度、表面连续性、烘焙后保真七个维度写清通过标准。在任务目录复制 [material-quality-brief.template.json](assets/material-quality-brief.template.json) 填好简报，把风格与完成度落成可观察的对照点。细节见 [参考定标与原生生图](references/reference-and-images.md)。

## 第二步：按表面需求选择材质来源

一个资产的不同部件或不同通道，可以按需求组合下面四种来源，把算力和精力花在视觉最关键的地方：

| 来源 | 适用表面 | 做法要点 |
|---|---|---|
| 常量（`constant`） | 纯色、均匀粗糙度、开关型金属度或固定折射率 | 直接填明确数值与单位；需要文件时确定性生成，留清说明 |
| 图像（`image`） | 特定纹样、文字标识、人物浮雕、复杂磨损遮罩或已有扫描贴图 | 优先调用宿主代理原生生图制作无光照二维母版，再派生各通道；已有合格图直接复用 |
| 程序化（`procedural`） | 木纹、漆面、金属拉丝、边缘磨损、凹处积灰、局部锈蚀 | 用真实物理尺度、固定坐标轴与 `seed` 驱动信号，用几何位置和朝向定位旧化 |
| 混合（`hybrid`） | 程序基材叠加图像标识，或图像纹样叠加程序旧化 | 逐通道写清来源、坐标配准与覆盖顺序，让每层各司其职 |

所有图像先过 [image_gate.py](scripts/image_gate.py) 核对解码、宽高与体积：生产原图最长边控制在 2048 像素以内，预览图单张控制在 2 MiB 以内，数据贴图和有损预览分开存放。

## 第三步：按物理规范搭分层与通道

按「基材 → 覆盖层 → 旧化层」的顺序搭材质，把中性展示灯光固定下来，将材质自身表现与镜头打光分开调。

- **BaseColor（反照率）**：非金属漫反射颜色落在 `30–240 sRGB` 之间，画面干净无烘焙阴影和高光；金属区域用 BaseColor 表达导体反射色，与非金属漆层、锈层明确分区。色彩空间设为 `sRGB`。
- **Roughness 与 Metallic**：统一设为 `Non-Color`。粗糙度按真实表面微结构独立绘制或驱动，区分抛光、缎面与哑光层次；金属度清晰区分裸金属（1）与绝缘覆盖层（0）。
- **Normal 与 Height**：法线统一采用切线空间 OpenGL `+Y`（`Non-Color`），经 `Normal Map` 节点接入；可靠切线法线优先通过真实烘焙产出。法线贴图已包含总起伏时，重建材质直接连 `Normal Map`，把 `Height` 保留为有符号浮点 EXR 辅助交付。
- **特殊光学通道**：透明玻璃保持 `Alpha = 1`、用 `Transmission`、`IOR` 和 `Roughness` 控制透射与磨砂，并核实壁厚、口沿与内外法线；半透明剪裁才使用 `Alpha`。

完整通道定义、跨层共享起伏与内腔处理见 [PBR 通道契约与分层规范](references/pbr-channels-and-layering.md)；程序化信号组合与 2.0 清单写法见 [程序化与混合 PBR 制作](references/procedural-pbr.md)。

## 第四步：按场景选用 CLI 入口执行

所有修改走独立 `--jobs-dir` 批次目录，全局参数（`--jobs-dir`、`--compact`、`--timeout`、`--async`）放在子命令前面。加上 `--compact` 可直接拿到精简摘要、源 SHA 和报告路径。

- **程序化与混合 2.0 制作**：用 `material procedural-run` 构建可编辑主工程并出预览；作者外观确认后，用 `material procedural-export` 逐通道烘焙贴图并自动在干净工程里重建纯贴图版本。
- **静态图层组装与微调**：单目标分层组装用 `material run`；多对象、多材质槽或分面域批量分配用 `material batch`；在固定机位和灯光下做少量数值对照用 `material study`；沉淀可复用受管结构用 `material template-save`。
- **高低模投射烘焙**：先用 `material projection-suggest` 按命名生成配对建议，确认笼体与射线距离后用 `material projection-bake` 产出切线法线、凸曲率与局部 AO；再用 `material projection-attach` 接入材质、`material projection-effects` 调磨损与积灰开关，最后用 `material projection-deliver` 打包可重定位工程与贴图。
- **接手现场窗口手调**：用户在打开的 Blender 窗口里调好材质后，用 `material handoff-live --operation inspect` 检查草稿，再 `--operation submit --mode SINGLE`（或 `BATCH`）收进带 SHA 的工作副本；从已保存工程读取受管草稿用 `material read-current`，抓取批量选面用 `material capture-faces`。

各子命令必填参数、长作业 `job wait` 与断点续跑细则见 [CLI 作业、高低模投射与现场交接](references/cli-projection-and-handoff.md)。

## 第五步：由大到小看图迭代与三方对照

1. **先看小图定大局，再看局部抠细节**。先渲 `256–384` 全帧缩略图对照冻结参考，确认材料身份、色块比例和旧化分布成立；再渲原尺寸近景，核对纹理方向、颗粒粗细、接缝、端面、倒角和玻璃内腔。每次只改一个关键假设，保留同机位前后图对照。
2. **用通道隔离快速归因**。画面不对时，分别看仅 BaseColor、白底仅法线、常色平面仅粗糙度，或开关单个旧化层，找出根因再动参数。
3. **跑通三方对照再放行**。把最终贴图放到正式目录，用外部贴图在干净工程里重建材质并独立冷重开。在相同机位、相同中性灯光和换光条件下，并排对比三组画面：**Cycles 可编辑节点版**、**Cycles 纯贴图重建版**与 **EEVEE Next 渲染版**。确认 Cycles 下可编辑版与纯贴图版主特征一致，如实记录 EEVEE Next 在透射、厚度或高频滤波上的差异。

验收维度与按用途（静帧/游戏/影视/动画）裁剪清单见 [视觉迭代、三方对照与交付清单](references/acceptance-and-delivery.md)。

## 交给下游

向用户提交「审看②（最终材质）」，或交给绑定、镜头灯光与资产入库部门时，备齐以下四项：

1. **可编辑主工程副本**（路径与 SHA256），保留完整可编辑节点树与 UV 层。
2. **导出贴图包与纯贴图重建验证工程**（路径与 SHA256），全部使用相对路径并在正式目录冷重开复渲通过。
3. **材质与通道说明**，列出对象、材质槽、UV 层名、每张贴图的通道角色、位深、色彩空间（`sRGB` / `Non-Color`）及常量参数。
4. **同机位对照图与审阅记录**，包含可编辑版与纯贴图重建版在整体、近景和换光下的并排图，以及 EEVEE Next 对照观察与剩余待办。

资产定稿需要规范命名、编目和入库时，转交 [blender-asset-manager](../blender-asset-manager/SKILL.md)。

## 例子：给工业夹座做高低模烘焙与旧化材质

接到委托：「建模部门交来定稿低模 `work/clamp/clamp_low.blend` 和配对高模 `clamp_high`，需要做哑光深灰喷漆带轻度棱边磨损和凹处积灰，按可复用资产交付。」

1. **查接口与建批次**：建目录 `work/audit/<batch>/`，算出源文件 SHA256，跑 `inspect` 核对对象名 `Clamp_Low`、`Clamp_High` 和 `UVMap` 均无重叠。
2. **找参考并定简报**：选取真实喷漆铸铁夹具整体与棱边近景图，填写 `material-quality-brief.json`，设定喷漆 BaseColor 落在 `55–75 sRGB`、粗糙度 `0.38–0.48`，磨损集中在受力棱边。
3. **高低模投射烘焙**：先跑 `material projection-suggest` 确认 `Clamp_High -> Clamp_Low` 配对，再用 `material projection-bake` 低分辨率试烘检查命中率，微调笼体距离后正式产出 2K 切线法线（OpenGL `+Y`）、凸曲率与局部 AO。
4. **接图与调旧化**：用 `material projection-attach` 把三张数据图接到喷漆基材，再用 `material projection-effects` 分别开关磨损与积灰层，在中性柔光与斜光下看 384 缩略图和原尺寸棱边近景，把磨损收敛到贴近参考的幅度。
5. **打包重开与三方对照**：跑 `material projection-deliver` 输出可重定位工程与贴图包，在正式目录冷重开并分别渲出 Cycles 可编辑版、Cycles 纯贴图重建版与 EEVEE Next 对照图。确认高光连续、无穿笼、无色彩空间错配后，把两份工程路径、贴图清单和并排对照图交用户完成审看②，通过后转资产入库部门。

## 参考

- [参考定标与原生生图](references/reference-and-images.md)
- [PBR 通道契约与分层规范](references/pbr-channels-and-layering.md)
- [程序化与混合 PBR 制作](references/procedural-pbr.md)
- [CLI 作业、高低模投射与现场交接](references/cli-projection-and-handoff.md)
- [视觉迭代、三方对照与交付清单](references/acceptance-and-delivery.md)
- [知识来源、Wiki 导航与历史实证](references/sources.md)
- 质量简报模板：[material-quality-brief.template.json](assets/material-quality-brief.template.json) · 图像门卫脚本：[image_gate.py](scripts/image_gate.py)
