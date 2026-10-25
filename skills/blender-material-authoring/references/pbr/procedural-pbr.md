# 程序化与混合PBR：2.0制作、导出与重建

入口：[材质技能](../../SKILL.md)；来源选择见[framework](framework.md)，公共入口见[tool-routing](tool-routing.md)。制作方法先读[基础信号与分层](procedural-signals.md)。本章描述Material Workflow0.8.0 / CLI0.64.0公开开发候选，不将接口发现、局部夹具或示例配方当作所有道具的视觉验收。

## 适用范围与交付

首期为已有清晰UV的静态写实道具：金属/漆面/木材及透明、着色、磨砂玻璃，可有克制污渍、划痕和有位置依据的旧化。油桶、书桌、玻璃器皿是验收目标，不是已通过的通用成品预设。NPR、液体、复杂焦散、体积吸收和自动UV不在本章首期范围。

交付同时保留可编辑author与所需PBR图，以及仅用磁盘图和声明参数的reconstructed工程。默认2K，可按用途调整；2K不保证每个UV小岛的细节都可读。Cycles为质量基准，Eevee另验透射/厚度/排序差异；Blender内重建不等于其他游戏引擎已验。

## 先检查输入，再设计信号

核对明确Scene/View Layer/frame、对象/材质槽、已有UV、面域、尺寸/单位、matrix_world、法线与共享关系。普通静态Bevel已有小样支持；其他修改器、库链接、约束、动画或实例按真实入口限制判断，不静默Apply、换源或改UV来通过。原材质、非目标槽、共享网格/组/图像受到保护。

目标UV须在一个0..1 tile，正面积重叠会拒绝，不能把有意镜像当默认例外。几何辅助图作用于整个目标对象，目前还要求全对象UV无正面积重叠；最终逐材质输出隔离不代表辅助图也允许跨槽覆盖。缺UV、拉伸、坏面、轮廓或光顺异常回建模/UV流程。玻璃尤其检查内外表面、口沿、壁厚和法线；外部俯视不能替代内腔观察。

清单至少记录：目标区域和层顺序、constant/image/procedural/hybrid来源、每个通道的signal/常量、遮罩成因、坐标/尺度/主方向、seed、图像路径/SHA/色彩空间、Height距离、玻璃光学值、预览条件和交付通道。备注保存在制作清单，不把未定义字段塞入严格manifest。

## 2.0信号与层

外层CLI请求版本是1.0，内部manifest为`schema_version: "2.0"`、`material.template: "procedural_pbr_v1"`；不要与旧pbr_layers_v1/v2混用。

- `signals`是有类型的无环依赖图。每项都有稳定`id/type/parameters/inputs`；无依赖也写`inputs: {}`。公共类型为constant、image、coordinate、warp、noise、wood、voronoi、edge、cavity、height_band、normal_direction、math、remap、ramp、mix。VALUE/COLOR/VECTOR引用严格匹配；coordinate/warp输出VECTOR，voronoi/remap输出VALUE。Gabor不在已支持接口中。
- `layers`按底到顶合成参数通道；基底必须enabled、opacity=1、mask=null。每层`channels`将通道名映射到signal ID；未接signal的通道用`values`常量。mask也是VALUE信号ID，层opacity与mask组合。不是任意MixShader/物理多层BSDF的全保真替代。
- 物性通道为base_color、roughness、metallic、height、alpha、transmission、ior、coat_weight、coat_roughness。颜色为RGBA，其他为标量；IOR为1..4，Height保留符号，最终物理幅度由`height_distance`控制。同一遮罩可关联掉漆后的颜色/金属度/粗糙度/高度，但这些物性不能简单共用一个数值。
- 当前根级Principled与总Height Bump位于材质本身，集中参数在`MWP Controls`组实例；最终信号由带`mwp_channel`的Reroute标识。读实例值和连接，不只看组接口默认值。不要手动删这些标识来绕过受管状态保护。

### 坐标、方向与seed

`coordinate.space`当前为OBJECT或UV。OBJECT是对象局部坐标，不是毫米，也不是Generated包围盒归一化；记录对象尺寸及非均匀缩放，按实际纹理尺度设置coordinate.scale。UV显式使用target.uv_layer。Mapping的POINT模式放大坐标会让纹理变小；移动或缩放后检查纹理是否仍贴附、尺度是否符合目标。

noise使用4D Noise，seed映射W；wood使用Wave的带/环及Phase Offset。固定seed和输入可复现图案，不能保证不同模型上的观感相同。木纹需解释桌面、腿、端面的局部方向；示例配方假设局部包围盒接近-1..1、中心在原点，不能未经核对直接套到任意尺寸/原点的模型。

coordinate.parameters.coordinate保存space/scale/rotation/translation。warp需要inputs.vector，参数scale/detail/roughness/lacunarity/seed/sample_scale/strength；noise/wood/voronoi/height_band可接inputs.vector，之后仍应用自身Mapping。voronoi参数为scale/randomness/feature(F1或DISTANCE_TO_EDGE)/coordinate，没有seed。remap的inputs.value需VALUE，参数from_min/from_max/to_min/to_max/clamp/interpolation（LINEAR/SMOOTHSTEP/SMOOTHERSTEP）。wood新增detail_scale和profile（SIN/SAW/TRI）；noise支持lacunarity。

Mapping Scale控件名S/id/coordinate/scale，纹理Scale是S/id/scale。所有shader参数规范化到float32；无法存储或存后坍缩的区间建图前拒绝。旧未发布碰撞图只能保留诊断，以新ID重建。

### 几何辅助遮罩

edge是Cycles Bevel着色法线与Geometry法线的`1-dot`经gain/clamp得到的响应；cavity为目标本地AO的反相。它们是特定渲染/距离下的近似，不能命名成“真实曲率”或“真实磨损历史”。当前cavity只支持only_local=true，避免邻近道具阴影固化成永久污渍。width/distance按实际空间尺度解释。

辅助图按当前几何、UV、法线、变换和参数生成，报告输入指纹；无跨调用缓存承诺。几何/UV/尺度改变后重新生成，不能把上一个资产的辅助PNG直接迁移。程序Noise只作破碎、粗细和局部差异；用高度、朝向、凹处或边缘依据定位旧化，保留旧化强度为0的干净基材对照。

## 最小manifest

将下列JSON保存为`procedural.manifest.json`。示例只要求已有Scene/ViewLayer/Cube/UVMap及非重叠UV；按实际源改名称和区域，不会创建模型。它是金属色与轻微粗糙变化的技术起点，不是油桶/书桌/玻璃的成品配方。

```json
{
  "schema_version": "2.0",
  "context": {"scene": "Scene", "view_layer": "ViewLayer", "frame": 1},
  "target": {"object": "Cube", "material_slot": 0, "uv_layer": "UVMap"},
  "material": {"id": "MetalDemo", "name": "Metal Demo", "template": "procedural_pbr_v1"},
  "signals": [
    {"id": "metal_color", "type": "constant", "parameters": {"value": [0.2, 0.23, 0.25, 1]}, "inputs": {}},
    {"id": "micro_roughness", "type": "noise", "parameters": {
      "scale": 60, "detail": 2, "roughness": 0.5, "distortion": 0, "seed": 7,
      "coordinate": {"space": "OBJECT", "scale": [1, 1, 1], "rotation": [0, 0, 0], "translation": [0, 0, 0]}
    }, "inputs": {}}
  ],
  "layers": [{"id": "substrate", "name": "Metal substrate", "enabled": true,
    "opacity": 1, "mask": null,
    "channels": {"base_color": "metal_color", "roughness": "micro_roughness"},
    "values": {"metallic": 1, "height": 0, "alpha": 1, "transmission": 0,
      "ior": 1.5, "coat_weight": 0, "coat_roughness": 0.03}
  }],
  "height_distance": 0.001,
  "mesh_map_size": 512,
  "preview": {"engine": "CYCLES", "device": {"backend": "CPU"},
    "width": 384, "height": 384, "samples": 16, "denoise": false,
    "color": {"view": "AgX", "exposure": 0, "gamma": 1}, "margin": 1.15,
    "views": [
      {"id": "soft", "azimuth": 45, "elevation": 20, "lighting": "soft"},
      {"id": "raking", "azimuth": -45, "elevation": 10, "lighting": "raking"}
    ]
  }
}
```

扩展源码根的`procedural_examples.py`（CLI源码根为`tools/material_workflow_addon/procedural_examples.py`）提供`make_recipe`，families为painted_metal、wood、glass_clear、glass_tinted、glass_frosted。它只产出配方并验证合同，不寻找对象、不修模型。新增grain_axis、metal_region、timber_scale控制木轴/旧化区域/局部三轴倍率。先按实际单位和区域适配，再看渲染；模板值不保证任意模型外观。

## 实际CLI发现与两命令范例

使用已配置的`BLENDERCTL_ROOT`定位当前工程；不复制其他机器的私有绝对路径。Windows示例使用PowerShell语法，实际操作先读配套[Blender CLI](../../../blender-cli/SKILL.md)。

```powershell
$ctlRoot = (Resolve-Path -LiteralPath $env:BLENDERCTL_ROOT).Path
$ctl = Join-Path $ctlRoot 'blenderctl.ps1'
& $ctl --version
& $ctl material describe --operation procedural-run --schema
& $ctl material describe --operation procedural-export --schema
& $ctl material procedural-run --help
& $ctl material procedural-export --help
```

以下工作目录事先包含`source.blend`及上面的`procedural.manifest.json`。确认源和其余非目标内容无外部文件时，`resources.json`才可为`[]`；有外图/链接等输入时按实际闭包列file/expected_sha256。图像signal另需与resources匹配的SHA和sRGB/Non-Color声明。

```powershell
$work = Join-Path $ctlRoot 'work\material-demo'
$source = Join-Path $work 'source.blend'
$manifestFile = Join-Path $work 'procedural.manifest.json'
$resourcesFile = Join-Path $work 'resources.json'
$jobs = Join-Path $work 'jobs'
$sourceSha = (Get-FileHash -LiteralPath $source -Algorithm SHA256).Hash.ToLowerInvariant()
# 第一条制作命令：构建、保存重开并渲染可编辑author。
$runOutput = & $ctl --jobs-dir $jobs --compact material procedural-run $source --expected-sha256 $sourceSha --resources $resourcesFile --manifest $manifestFile
if ($LASTEXITCODE -ne 0) { throw 'Read the returned job report and logs before retrying' }
$runResult = $runOutput | ConvertFrom-Json
if (-not $runResult.ok) { throw 'Inspect the returned operation error' }
```

读取返回的job_id与实际报告引用；完整报告在对应job目录`procedural-report.json`，资源在`procedural-resources.json`。确认report.status、author.file/expected_sha256与checks，并实际查看author新渲染。局部失败或观感未达目标先修对应信号/层/机位，另建候选；不能直接跳过看图进入最终导出。

第二条制作命令从已验author导出。`$jobId`取真实返回，不使用占位值发请求；下列导出清单包含全部当前通道，按交付可裁剪。例中2K是目标设置，需读回实际输出；较小预览/夹具通过不证明2K已验。

```powershell
$jobId = $runResult.job_id
$authorJob = Join-Path $jobs $jobId
$report = Get-Content -LiteralPath (Join-Path $authorJob 'procedural-report.json') -Raw | ConvertFrom-Json
if ($report.status -ne 'pass') { throw 'Author job has not passed its technical checks' }
$author = $report.author.file
$authorSha = $report.author.expected_sha256
$manifest = Get-Content -LiteralPath $manifestFile -Raw | ConvertFrom-Json
$contextFile = Join-Path $work 'export-context.json'
$targetFile = Join-Path $work 'export-target.json'
$exportFile = Join-Path $work 'export-settings.json'
# 新文件仅首次创建；已有文件先审核，不静默覆盖。
New-Item -ItemType File -Path $contextFile -Value ($manifest.context | ConvertTo-Json -Depth 8) -ErrorAction Stop | Out-Null
New-Item -ItemType File -Path $targetFile -Value ($manifest.target | ConvertTo-Json -Depth 8) -ErrorAction Stop | Out-Null
$settings = @{channels=@('base_color','roughness','metallic','normal','height','alpha','transmission','ior','coat_weight','coat_roughness');size=2048;margin=16;samples=16;bit_depth='16'}
New-Item -ItemType File -Path $exportFile -Value ($settings | ConvertTo-Json -Depth 8) -ErrorAction Stop | Out-Null
$authorResources = Join-Path $authorJob 'procedural-resources.json'
& $ctl --jobs-dir $jobs --compact --timeout 3600 material procedural-export $author --expected-sha256 $authorSha --resources $authorResources --context $contextFile --target $targetFile --export $exportFile
if ($LASTEXITCODE -ne 0) { throw 'Read the export job report and retained partial diagnostics' }
```

2K多通道烘焙可能超过默认180秒，不能假定默认时限够。上例显式使用全局`--timeout 3600`；该值是本次长作业预算，不是完成时间保证。procedural-run附export时同样按规模设置timeout。需要后台运行时，将全局`--async`放在子命令前，保存真实job_id，用原jobs目录的有界job wait读取阶段/终态，不因queued或有界等待返回running而重复提交。

新上下文从真实返回或已读报告设置jobId，不猜ID、不调用交互等待。CLI只允许写自己job目录；不自行构造覆盖正式文件的output参数。也可在procedural-run追加`--export <settings.json>`，但仍需查看其author和reconstructed画面后验收。

## 逐通道导出与光学数据

当前export通道名为base_color、roughness、metallic、normal、height、alpha、transmission、ior、coat_weight、coat_roughness；没有AO导出键。遗漏的非恒定关键通道会被补入，须读报告requested_channels与required_added_channels，不假定请求的四图就是实际完整交付。无图的常量必须有明确参数；不能靠重建器默认值猜源光学属性。

- BaseColor从最终颜色信号通过受控Emission/Emit捕获，sRGB保存；不能用Diffuse Color通用提取纯金属，也不能用Combined把灯光烘入颜色。
- 标量数据为Non-Color；PNG读回实际8/16位、尺寸和SHA。IOR以`(IOR-1)/3`编码，重建用`3*x+1`解码。未知编码或缺失/改变文件必须失败。
- Height为有符号float EXR，当前实际输出32位，独立于PNG bit_depth设置；记录零位、距离、强度与符号，不截断成0..1灰图。
- Normal走真实Cycles Normal bake，TANGENT、OpenGL/+Y和相同UV；包含总Height Bump。重建连接Normal Map，不能再叠同Height。Height可保留作辅助交付，不代表本次有真实几何置换。
- 每图保存后销毁旧buffer、重新载入磁盘像素并检查。Blender5.2.1的sRGB bake float buffer与重载PNG16 float buffer处于不同编码阶段，比较必须统一到线性信号域，不能为了通过而放宽到肉眼明显的误差。

玻璃常用Alpha=1、Transmission>0及有依据的IOR；Roughness负责清晰/磨砂，不降低Alpha冒充透射。着色BaseColor是本期表面着色近似；真实随光程变化的吸收不在无体积首期。验证中近景内腔、口沿和壁厚；Eevee Thickness/透射排序是近似，逐引擎观察并列损失。

## 看图、迭代与恢复交接

每轮先读回参数和阶段/输出，再检查图像最长边与≤2MiB预算，实际查看新图。先判断材料身份、尺度和旧化组织，再看接缝、凹处、局部特写和玻璃内腔。改一个关键因素并保留对照；提高采样不能修复UV、方向或满面噪声。最终从磁盘图重建、保存重开，在相同镜头/灯光及必要换光下对照author/reconstructed；报告`requires_visual_review`不是视觉通过。

procedure没有公开resume字段。失败后读job状态、`procedural-progress.json`、`procedural-report.json`、日志、bake的partial/report、已有持久文件及SHA。保留partial目录，不把半套通道称完整包，不改旧SHA或删保护重试。已有通过的author可新建procedural-export作业；不完整author/改变资源须先补齐并验证，手改/共享冲突用明确新版本或隔离处理。几何辅助图不承诺缓存复用。

交给DeepSeek或新上下文时提供：用户目标/特殊图案待交接项、源与资源身份、当前manifest和必要Schema、版本、真实job/report路径、最后已验author、最新图与具体未决现象、可执行下一步和停止条件。外部执行者先读实际技能与工具Schema，不假装有生图能力。必须读新渲染→迭代→烘焙→rebuild检查；view_image或等价图像观察不可用时，保留视觉未验并交回判断，不能把数字或completed状态当已看图。

## 有界证据与公开候选状态

维护者在Windows / Blender5.2.1 LTS工作区完成木桌5、油桶6、厚壁玻璃3部件的2K必要通道共126图，以及整物体作者/贴图重建×Cycles/Eevee×整体/近景/换光共36图。该限定实做支持对应模型和条件下的迁移判断；原始私有工程与审计不随包发布，不宣称公开可复现结果。Cycles宏观结构保留，高频细节受2K/UV限制；Eevee玻璃差异、木材端面与卷边滤波差异须分别记录，不能称跨引擎等价。

公开配套为扩展0.8.0、CLI0.64.0、技能包0.3.0候选，材质指导源版本0.6.0。CLI公开版与维护者完整工作区版本序列不同，每次按describe/help和Schema发现。公开候选代码已通过解压后运行及隔离扩展安装；DeepSeek按文件阶段完成14部件/126张2K与重建，主线程实际检查42张最终图。含主线程诊断纠偏，不称无协助盲测；2K高频损失、Eevee玻璃差异及未验跨机/外引擎保留。公开验证导航见仓库`docs/MATERIAL_WORKFLOW.md`；具体制作仍以当前作业回执、实际读回和新渲染为准。

## 可选表面厚度与分阶段复用（2026-10-24候选）

Blender5.2的Material Output Thickness未连是AUTO估厚，连接0才关闭厚度近似。当前2.0候选可写 `"surface":{"thickness_mode":"SLAB","thickness":0.13}`；0.13只是语法示例，制作时从对象空间真实壁厚/形状核对，不能当统一值。省略thickness保留AUTO；省略surface保持旧行为。模式可选SPHERE/SLAB，显式厚度为0..1000有限float32标量，集中控件`Surface/thickness`可编辑并进入模板。用正式manifest创建/更新，手工改链接或重写mwp_state不属于合法更新。

导出报告的`thickness={mode,space,value}`须保留AUTO与EXPLICIT（包括0）区别，重建恢复模式与连接。动态/未知厚度明确拒绝；厚度参数不等于独立贴图、不替代网格厚壁、Alpha或体积吸收。厚壁杯Eevee发黑时，先核实真实厚度/坐标单位、raytracing和近似模式，再同条件对照；SLAB在本组三杯实验中改善透射，仍弱于Cycles的口沿/折射，不能推广为所有玻璃默认值。

主线程44项后端检查、公共CLI128²杯子往返、两仓各35项公开夹具检查及8项独立重开均已通过。DeepSeek随后用正式surface完成三杯27张2K/16bit及磁盘图重建；独立全像素检查与厚度保持通过，Cycles/Eevee成对图已实际查看。Eevee仍弱于Cycles的内腔与多界面折射，不能称跨引擎等效。完整证据与版本边界保存在Wiki `procedures/blender-procedural-pbr-authoring-and-bake-validation.md`；公开小夹具可由仓库optics测试复现。

DeepSeek遇到长图像上下文WriteTimeout时，采用可保存的文件阶段：制作/迭代检查点 → 导出重建 → 少量成对看图。当前已验证每个新观察阶段1–4张JPEG；这是本机已验运行方式，不是服务商硬性图片上限。保留task/job/源SHA/参数/图像SHA，阶段内实际看图且完成即返回；后续阶段明确延续哪个文件，不携带长图像历史。主线程协助选择或修参数时如实记录，不能称无协助盲测通过。

