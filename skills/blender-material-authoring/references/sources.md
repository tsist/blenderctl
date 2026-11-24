# 知识来源、Wiki 导航与历史实证索引

本页集中记录共享 Wiki 词条入口、官方文档参考以及本技能沉淀所依据的历史案例与验收审计路径。日常制作规范见 [SKILL.md](../SKILL.md) 与同级业务参考。

## 1. 共享 Wiki 知识导航

通过 `wiki_query` 或 `wiki_get` 按需回读对应相对路径：

| 主题 | Wiki 相对路径 |
|---|---|
| 材质来源按需路由决策 | `decisions/2026-10-23-blender-material-source-routing-20261023.md` |
| 程序化材质研究与信号组合 | `concepts/blender-procedural-signal-composition.md`、`references/blender-procedural-material-research-20261023.md` |
| 程序化制作、烘焙与重建流程 | `procedures/blender-procedural-pbr-authoring-and-bake-validation.md`、`procedures/blender-procedural-noise-layered-prop-workflow.md` |
| 材质工作流公共工具与交接 | `procedures/blender-material-workflow.md` |
| 参考规划、几何与 UV 起点 | `procedures/blender-pbr-reference-planning.md`、`procedures/blender-pbr-geometry-uv.md` |
| 通道色彩、着色与历史烘焙 | `concepts/blender-pbr-maps-color.md`、`procedures/blender-pbr-shaders.md`、`procedures/blender-pbr-baking.md` |
| 陶瓷石材与金属涂层材料笔记 | `concepts/blender-pbr-ceramic-stone.md`、`concepts/blender-pbr-metals-coatings.md` |
| 验收、来源授权与按用途核验 | `concepts/blender-pbr-acceptance.md`、`references/blender-pbr-sources-license.md`、`procedures/blender-pbr-game-acceptance.md`、`procedures/blender-pbr-film-acceptance.md`、`procedures/blender-pbr-animation-acceptance.md` |
| 花瓶系列历史案例实证 | `procedures/blender-terracotta-first-piece-pbr.md`、`procedures/blender-oxidized-brass-vase-normals-and-uv.md`、`procedures/blender-vase-materials-final-closeout.md` |

## 2. 项目文档与工具契约源码

- 部门卡：[UV 与材质部门](../../blender-work-methodology/references/departments/surfacing.md)
- UV 与材质交接说明：`<PROJECT_ROOT>/docs/cli/workflows/uv-material-handoffs.md`
- Material Workflow 插件说明：`tools/material_workflow_addon/README.md`
- 公共 CLI 材质契约：`tools/blenderctl/material_interface.py`、`material_study_contract.py`、`material_procedural_contract.py`、`material_projection_contract.py`
- 程序化配方生成器：`tools/material_workflow_addon/procedural_examples.py`

## 3. 历史案例与实证审计路径

以下路径保留历次实做参数、用户验收反馈与自动化测试记录，供回溯查证：

- **赤陶花瓶（静态图像图层，2026-10-02）**：`<PROJECT_ROOT>/work/pbr-vase-lab/terracotta-image-layers-v001/README.md`；审计目录 `work/audit/<batch>/` 与 `terracotta-final-tune-20261002-v001/`。验证了 256/384 缩略图先判大形、按数据用途独立审查通道及口沿内腔 UV 校正。
- **青花瓷三表面（亮釉/缎面/哑光，2026-10-03）**：`<PROJECT_ROOT>/work/pbr-vase-lab/bluewhite-glaze-v001/README.md`；审计目录 `work/audit/<batch>/`。验证了 `Coat Normal` 显式连接、白底法线开关对照与颜料/粗糙度分离审查。
- **旧裂纹釉陶（覆盖与台阶起伏，2026-10-04）**：`<PROJECT_ROOT>/work/pbr-vase-lab/old-crackle-glaze-v001/deliverables-v003/README.md`；审计目录 `work/audit/<batch>/`。验证了底胎与釉层分离色域，以及多 BSDF 混合时共享统一 Height 场保持断口连续。
- **锈蚀黄铜花瓶（人物法线与内壁接缝，2026-10-05）**：`<PROJECT_ROOT>/work/pbr-vase-lab/oxidized-brass-v001/deliverables-v002/README.md`；审计目录 `work/audit/<batch>/`。验证了三外层共享宏观 Normal、内外底三面域划分与腔内单通道消融定位接缝。
- **花瓶三材质综合复核（2026-10-06）**：`work/audit/<batch>/`。
- **程序化 2.0 烘焙、木桌/锈桶/厚壁玻璃 14 部件与 SLAB 表面厚度实证（2026-10-23 – 2026-10-24）**：`work/audit/<batch>/`（含烘焙夹具、保存重开检查、31 项 mesh-map 检查与 Cycles/EEVEE 整物体双引擎重建对照）。
- **高低模投射烘焙与 15 mm 夹座交付实测（2026-11-19）**：`<PROJECT_ROOT>/docs/proposals/high-low-bake-texture-delivery-wm-20261119.md`；面板动作公共对应审计 `work/audit/<batch>/`。

## 4. 外部技术文档参考

- Blender 官方着色节点手册：[Noise](https://docs.blender.org/manual/en/latest/render/shader_nodes/textures/noise.html)、[Wave](https://docs.blender.org/manual/en/latest/render/shader_nodes/textures/wave.html)、[Voronoi](https://docs.blender.org/manual/en/latest/render/shader_nodes/textures/voronoi.html)
- Poly Haven 纹理质量标准：[Technical Standards - Textures](https://docs.polyhaven.com/en/technical-standards/textures)
- 域扭曲与程序木纹参考：[Inigo Quilez - Domain Warping](https://iquilezles.org/articles/warp/)、[Decscroll 木纹勘误](http://www.decscroll.com/?p=223)
