---
name: blender-physics-workflow
description: Modular Blender 5.2.1 LTS Geometry Nodes physics workflow and isolated CLI automation across rigid_scatter, cloth_rope, and fluid_emission domains.
metadata:
  version: "0.1.0"
---

# Blender Physics Nodes Workflow Skill (Blender 5.2.1 LTS)

本技能面向 **Blender 5.2.1 LTS**（已验证构建 `9e2066aef7ef`），提供交互式三维视口 N 面板工作台（`Physics Nodes`）以及隔离外部批处理 CLI 入口 `<REPO_ROOT>/tools/physics_workflow_cli_v1/cli.py`。

> [!IMPORTANT]
> **并发与安全边界**：物理工作流插件与 CLI 完全独立于 `tools/blenderctl/`、`tools/geometry_nodes_workflow_addon/` 和 `tools/uv_workflow_addon/` 运行；严禁未授权覆盖源 `.blend` 文件或跳过 SHA-256 校验与磁盘缓存失效检查。

---

## 一、核心节点架构：Wrapper + Core + 12 个可编辑子组

1. **Wrapper 层 (`PNW_Wrapper_<obj>_<mod>`)**：直接挂载在修改器上的外层节点组，暴露物理工作台和 CLI 的统一接口。在 Geometry Nodes Editor 中插入的自定义节点（如 `User_Added_Realize`）与新增接口（如 `User Custom Scale`）会在后续 `update` / `parameters` / `bake` 操作中完整保留。
2. **Core 层 (`PNW_Core_<obj>_<mod>`)**：包含 **1 个根级 `GeometryNodeSimulationInput/Output`** + 尾部 `GeometryNodeBake` + 内部 `GeometryNodeRepeatInput/Output`（`Substeps = 1..8`, `dt_sub = Delta Time / Substeps`），确保单 `bake_id` 与磁盘缓存精确 1:1 绑定；并自动完成世界锚点转局部空间（`Anchor - self_info.Location`）、世界地面转局部 `Ground Z` 以及生成网格材质透传（`GeometryNodeSetMaterial`）。
3. **12 个模块化无状态子组 (`PN_*`)**：
   - 力场：`PN_Force_GravityDrag`、`PN_Force_WindTurbulence`
   - 碰撞：`PN_Collide_GroundPlane`、`PN_Collide_MeshSurface`（含距离阈值屏蔽，防有限网格外无限延展）
   - 约束：`PN_Constraint_PinAnchor`、`PN_Constraint_DistanceChain`
   - 步进求解器：`PN_Solver_RigidScatterStep`、`PN_Solver_ClothRopeStep`（基于 `p_pred - pnw_rest_pos` 相对静置位移 PBD 模糊与速度耦合，不收缩不垮塌）、`PN_Solver_FluidParticleStep`
   - 后处理：`PN_Post_InstanceRigid`、`PN_Post_FluidSDFMesh`（`PointsToSDFGrid -> GridToMesh`）、`PN_Post_SmokeVolume`（`PointsToVolume + VolumeToMesh`）

---

## 二、支持的三域、6 套预设与显式桥接

- **合约标识**：`BLENDER_PHYSICS_WORKFLOW_CONTRACT_V1` (`schema_version: 1.0`)
- **三域与预设 (`PRESET_DEFINITIONS`)**：
  1. `rigid_scatter`：`rigid_drop_pack`（多实例散落碰撞与停靠）、`rigid_chain_pendulum`（锚点距离约束链摆）
  2. `cloth_rope`：`cloth_banner_wind`（顶边自动锚定风吹布料）、`rope_catenary_swing`（支持 `CURVE` 自动转管网输出）
  3. `fluid_emission`：`fluid_fountain_sdf`（粒子发射 + 碰撞 + SDF 表面化网格）、`smoke_plume_volume`（浮力 + 风涡旋 + 体积/预览网格）
- **Solver Bridge Modes**：`GN_NATIVE`（默认纯节点核心）、`XPBD_ASSET_BRIDGE`（桥接 Blender 5.2 内置 `Cloth Dynamics (Experimental)`）、`BULLET_BRIDGE`（桥接 `RigidBodyWorld`）、`MANTAFLOW_BRIDGE`（桥接原生 `FLUID` 修改器）。
- **Export Formats**：`NONE`、`STATIC_MESH`、`ALEMBIC`（`.abc` 动画序列）、`OPENVDB`（回执/Volume 适配导出）。

---

## 三、交互式 N-Panel 工作台使用

- **面板位置**：`3D Viewport -> N-Panel -> Physics Nodes`（`VIEW_3D_PT_physics_nodes_workbench`）
- **核心操作算子**：
  - `pnw.load_preset_defaults`：加载预设默认参数
  - `pnw.build_workflow`：构建三层节点树并写入初始状态
  - `pnw.update_parameters`：保留用户节点与外部连线更新工作流/参数，参数哈希变化时自动失效旧缓存
  - `pnw.bake_cache` / `pnw.invalidate_cache`：管理 `DISK` 烘焙（短路径 `< 240` 字符防 Windows `MAX_PATH` 越界）
  - `pnw.inspect_workflow`：统计实体数、位移、接地穿透与缓存一致性

---

## 四、CLI Overlay 自动化调度 (`physics_workflow_cli_v1`)

入口：`<REPO_ROOT>/tools/physics_workflow_cli_v1/cli.py`。运行依赖 `Blender` 可执行文件时，先设置环境变量 `BLENDER_PATH`（或 `BLENDERCTL_BLENDER`）。

### 1. 查询契约与预设
```powershell
python tools/physics_workflow_cli_v1/cli.py describe
```

### 2. 单场景构建、烘焙、冷重开核验与 Alembic 导出 (`run`)
```powershell
$env:BLENDER_PATH = "<path-to-blender-5.2.1>"
python tools/physics_workflow_cli_v1/cli.py run `
  --request "path/to/request.json" `
  --jobs-dir "./jobs/my-job" `
  --timeout-seconds 300
```
最小 `request.json` 示例：
```json
{
  "schema_version": "1.0",
  "operation": "build",
  "preset_id": "fluid_fountain_sdf",
  "target_object": "Fountain_Emitter",
  "collider_object": "Dome_Deflector",
  "input_blend": "./input/scene_in.blend",
  "output_blend": "./output/scene_baked.blend",
  "bake": true,
  "verify_cold_reopen": true,
  "export_format": "ALEMBIC",
  "export_path": "./output/fluid.abc",
  "parameters": {
    "substeps": 3,
    "frame_start": 1,
    "frame_end": 120,
    "emit_rate": 16,
    "emit_speed": 1.85,
    "particle_radius": 0.15,
    "sdf_voxel_size": 0.07,
    "contact_thickness": 0.15,
    "ground_z": 0.0
  }
}
```

### 3. 参数扫描 (`sweep`) 与批处理 (`batch`)
- `sweep`：在基础请求上注入参数变体列表，并列比较位移/速度/稳定性。
- `batch`：顺序/隔离执行多个独立 `.blend` 仿真作业并输出汇总回执。

---

## 五、Blender 5.2.1 关键避坑清单

1. **Modifier 输入赋值**：必须通过 `getattr(mod.properties.inputs, identifier).value = val` 赋值（不可用 `mod[id] = val`），元数据存储在 `obj[f"pnw_state_json__{mod.name}"]`。
2. **`Nodes` 输入插座化属性**：`CollectionInfo["Separate Children"]`、`InstanceOnPoints["Pick Instance"]`、`CurveToMesh["Fill Caps"]`、`PointsToVolume["Resolution Mode"]`、`VolumeToMesh["Resolution Mode"]` 均位于 `node.inputs[...]` 而非节点属性；`GeometryNodeSetShadeSmooth` 首输入端口名为 `"Mesh"`。
3. **Alembic 导出关键字**：Blender 5.2.1 `bpy.ops.wm.alembic_export` **无** `visible_objects_only` 参数，需显式设置 `as_background_job=False` 以保证同步落地。
4. **物理局部坐标与 PBD 防收缩**：`Simulation Zone` 内部运行于物体局部坐标系，锚点与地面需扣除 `Self Object` 位置；布料/绳索 PBD 模糊必须作用于相对静置位移 `(p_pred - pnw_rest_pos)`，不可直接平滑绝对坐标。
