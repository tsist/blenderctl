# 历史发行版、审计路径与来源台账

本文件集中存放绑定部门的发行版历史、私有基线模板、历史实证审计目录、Wiki 知识入口与外部参考来源。正文与业务参考始终引导通过 `blenderctl.ps1 discover --product rigging` 实时查询最新入口；需要追溯旧工程、重放历史回执或复查研究原始证据时，从这里查找准确路径。

## 当前宿主与工作流交接文档

- **唯一验收宿主**：Windows Blender `5.3.0`（构建 `02f0899d78e3`），公共入口 `./blenderctl.ps1`。
- **最新能力整合交接**：`<PROJECT_ROOT>/docs/cli/workflows/能力整合交接-20261124.md`
- **角色从绑定交给动画（v003 推荐 / v002 历史 / 0.42.3-rc1 追溯）**：`<PROJECT_ROOT>/docs/cli/workflows/rigging-handoffs.md`
- **Windows Blender 5.3.0 固定版 0.40.1 操练手册**：`<PROJECT_ROOT>/docs/cli/workflows/rigging-0401.md`
- **肩胯定位与修权专项标准与工作卡**：
  - `<PROJECT_ROOT>/docs/rigging/肩胯骨骼定位与权重修复标准.md`
  - `<PROJECT_ROOT>/docs/rigging/肩胯修复工作卡.md`
  - `<PROJECT_ROOT>/docs/rigging/新流程与模板定义-20261106.md`

## 绑定发行版与源码工作目录索引

通过 `blenderctl.ps1 discover --product rigging --edition <发行版> --actions` 可实时查到以下各版动作清单：

| 发行版标识 | 定位与适用场景 | 源码 / 交付路径与主要审计目录 |
|---|---|---|
| `0.42.3-agent-v003` | 当前推荐体素蒙皮候选（单/多域 CUDA、同作业部件转权 `bind_transfer`、选骨局部重算、同骨架迁权与作者返工） | 源码：`<PROJECT_ROOT>/work/rigging-agent-workflow-20261124-v003/`<br>Step 4 速度与等价验收：`work/audit/<batch>/`<br>同作业部件转权验收：`work/audit/<batch>/`<br>Step 5 公共 CLI 验收：`work/audit/<batch>/`<br>Step 7 交接读回：`work/audit/<batch>/` |
| `0.42.3-agent-v002` | 历史冻结候选版（用于重放既有 v002 回执） | 源码：`<PROJECT_ROOT>/work/rigging-agent-workflow-20261123-v002/cli/`<br>七域实跑与选骨对照：`work/audit/<batch>/`<br>多网格选骨：`work/audit/<batch>/`<br>Myona 人工交接交付：`work/audit/<batch>/` |
| `0.40.1` | 固定版完整角色工作台（原骨架维护、四肢 FK/IK、控制面板、面部表情与口型、胸臀弹性与发裙表面动态、独立附属链、MMD 与通用 FK 重定向、定位与镜像） | 工具入口：`tools/rig_0401_workspace/`、`rig_0401_pose/`、`rig_0401_inspect/`、`rig_0401_entry/`、`rig_0401_panel/`<br>附属链验收：`work/audit/<batch>/`<br>Step 3 综合结项：`work/audit/<batch>/`<br>动力学与面板分项验证：`work/audit/<batch>/` 至 `b163/` |
| `c94-0.40.1-derived-skeleton-only-win53-v1` / `c94-0.40.1-derived-rebuild-review-win53-v1` | 0.40.1 在 5.3.0 下的未蒙皮骨架生成与重建迁权审看派生执行版 | 随 `discover --product rigging --edition 0.40.1` 的 `launchers[].implementation_edition` 返回并核对 |
| `0.42.3-rc1` / `0.23.0` | 历史 5.2.1 过渡版与旧角色维护版 | `work/audit/<batch>/`、`b19/`、`b53/` 至 `b83/` |
| `0.40.0-local-r3`（C94 公开基线） | C94 主开发树与公开预发布基线 | 主树：`<PROJECT_ROOT>/work/rigging-workflow-c94/rigging_workflow/`<br>交付 ZIP：`work/audit/<batch>/`（SHA `e2148a2a14ae1e9e4332120fb24f0f105803c8a89078129784cd9416bde7e59c`）<br>公开仓库：`https://github.com/tsist/blender-rigging-workflow`（Tag `v0.40.0-r3`） |

## C94 私有首模板基准

C94 路线的本机私有基准模板文件（保持只读，供 C94 路线角色生成使用）：

- 文件：`work/audit/<batch>/`
- SHA-256：`7b8c1bf154d73868f39285d3d3102ebf029f7493467ad69aeadf665d7c920113`（Object/Data 均为 `千早爱音-素体骨架`）。

## 关键历史专项实证与研究审计目录

- **骨架质量门工具与早期集成**：
  - `<PROJECT_ROOT>/work/rigging-skeleton-quality-20261114-v001/` · `work/audit/<batch>/`
  - `<PROJECT_ROOT>/work/rigging-skeleton-gate-integration-20261115-v001/`
- **独立原生骨架（NATIVE）单域蒙皮恢复与犬样本**：
  - `<PROJECT_ROOT>/work/rigging-native-recovery-20261115-v001/`
  - `work/audit/<batch>/`
  - `work/audit/<batch>/`（游戏原始网格人/猫/犬验证、尾链方向、猫控制中立与 S3 验收）
- **自包含人形肩带 SC/AC/GH、独立肩胛载体与短命配对匹配**：
  - `work/audit/<batch>/`
  - `<PROJECT_ROOT>/work/rigging-scapular-carrier-20261116-v001/CURRENT-CANDIDATE.json`
  - `work/audit/<batch>/`（`pose_match_pair.py` 与 v006/v007 配对证据）
  - `work/audit/<batch>/`
  - `<PROJECT_ROOT>/work/rigging-human-skeleton-20261115-v001/Human_Skeleton_Reviewed.blend`
- **猫四段爪禁伸缩与逐姿态修形（双膝六 Key 与曲线复现）**：
  - `work/audit/<batch>/` · `<PROJECT_ROOT>/work/rigging-cat-skeleton-20261116-v001/Cat_Skeleton_Final_v002.blend`
  - `work/audit/<batch>/`（内点混合采样与单侧修形研究）
  - `work/audit/<batch>/` · `<PROJECT_ROOT>/work/rigging-cat-pose-correctives-20261116-v001/Cat_Dual_Knee_Correctives.blend`
- **显式局部辅助骨与局部拓扑重建对比记录**：
  - `work/audit/<batch>/`
  - `work/audit/<batch>/`
  - `work/audit/<batch>/`（Blender Studio Autumn 对标）
  - `work/audit/<batch>/`（Snow v4 裤装方向 Key 消融）
- **历史模块化开发包（0.42.0 / 0.42.1 / 0.42.2）**：
  - `<PROJECT_ROOT>/work/rigging-modular-20261112-v001/rigging_workflow/`
  - `work/audit/<batch>/`（SHA `b1146f61d96ad974ea1f9c72e6d7fbe714e566ec41e747f5468974590224964b`）

## 共享 Wiki 知识条目索引

- `procedures/blender-skeleton-quality-gate-before-skinning`
- `procedures/blender-local-skin-weight-repair`
- `procedures/blender-shoulder-hip-positioning-and-weight-repair-standard`
- `procedures/exact-local-triangle-self-intersection-for-skinning-diagnosis`
- `procedures/blender-posebone-driver-initialization-before-first-evaluation`
- `procedures/c94-weight-policies-and-protected-local-redistribution`
- `procedures/blender-game-asset-rigging-learning-and-readiness`
- `concepts/blender-lbs-posed-corrective-inverse`
- `concepts/blender-garment-fit-measurement-domain`
- `references/blender-rigify-front-paw-control-semantics`
- `references/blender-studio-autumn-quadruped-rig-benchmark`
- `references/blender-studio-snow-pants-pose-correctives`
- `references/blender-game-asset-rigging-knowledge-map`

## 保留的游戏资产来源台账（JSON 原样保留）

本目录保留 7 份逐条目游戏资产研究来源台账（原始研究报告位于 `work/audit/<batch>/`）：

- [交互道具与可动建筑来源台账](sources-game-interactive-props.json)
- [车辆、载具与机械来源台账](sources-game-vehicles-machinery.json)
- [武器与手持工具来源台账](sources-game-weapons-and-tools.json)
- [植被资产来源台账](sources-game-vegetation.json)
- [柔性非角色资产来源台账](sources-game-flexible-assets.json)
- [可破坏物与装配体来源台账](sources-game-destruction-assemblies.json)
- [游戏运行时消费交叉来源台账](sources-game-runtime-consumers.json)

## 外部官方与开源参考

- **Blender 官方文档**：Armature Modifier、Weight Paint Options、Object Parent / Child Of、B-Bones、Spline IK、IK Solver、Cloth Shape & Collisions、Surface Deform、Apply Transform、glTF 2.0 导出规范，以及官方 `PoseBone.bbone_segment_matrix` 示例。
- **解剖与成熟角色成品**：OpenStax Anatomy & Physiology（胸带与滑膜关节）、UMN Dog & Cat Anatomy Lab Guide；Blender Studio 开放角色 Autumn（《Spring》）、Snow v4、Sprite Fright 制作笔记；Peter Shipkov ShoulderRig、SideFX Houdini Feathers 文档与开源 Rigify 源码。
- **游戏引擎参考**：Unreal Engine（Skeletal Mesh Sockets、Root Motion、Vehicle Art Setup、Cable Components、WPO/Bounds、AnimToTexture VAT、LOD 与 Chaos Destruction）、Unity（Hinge Joint、Optimize Game Object、Skin Weights、OnAnimatorMove、FPS Sample）、Godot（AnimatableBody3D）。
