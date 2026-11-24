# 程序化与混合 PBR 制作

本页说明如何用基础程序信号组合成可信表面、编写 2.0 `manifest`，以及通过 `procedural-run` 和 `procedural-export` 完成烘焙与纯贴图重建。回到主入口见 [SKILL.md](../SKILL.md)。

## 1. 先拆表面结构，再选信号基元

先把目标表面拆成「基材 -> 覆盖层 -> 变化发生的位置 -> 中尺度结构 -> 微表面」，再挑对应基元组合，让每个信号承担单一清晰职责：

| 基元或操作 | 职责 | 易混提醒 |
|---|---|---|
| `noise` | 基频（`scale`）、细节层数（`detail`）、幅度谱（`roughness`）、频率比（`lacunarity`）、4D 种子（`seed`） | `noise` 的 `roughness` 控制频率衰减，与材质粗糙度分开理解 |
| `wood`（Wave） | 主纹理方向、带/环切片、相位偏移与波形（`SIN / SAW / TRI`） | 波传播方向与可见长纹方向不同；避免做成等距高对比条纹 |
| `voronoi` | 孔隙、颗粒（`F1`）与胞元边界（`DISTANCE_TO_EDGE`），返回 `VALUE` | 胞元边界是纹理胞元，与模型几何棱边不同 |
| `coordinate` | 坐标空间（`OBJECT / UV`）、三轴缩放、旋转与平移 | `OBJECT` 是对象局部坐标；`POINT` 模式下放大倍率会让花纹变密 |
| `warp` | 在采样前扭曲输入向量，让相关结构自然协同弯曲 | 作用在坐标空间 `f(p + h(p))`，把弯曲与颜色叠加分开 |
| `remap` / `ramp` | 把 `0..1` 信号映射到真实物性区间或窄过渡阈值 / 颜色梯度 | 每个物性通道各自映射真实物理范围，避免直连原始 `0..1` |
| `edge` / `cavity` | 基于目标几何 Bevel 法线差（`edge`）与本地 AO 反相（`cavity`）生成辅助遮罩 | 属于当前几何与尺度下的近似场；模型或 UV 变动后重新生成 |
| `height_band` / `normal_direction` | 按高度范围或法线朝向定位受潮、积灰、顶面磨损区域 | 局部 Z 轴与世界重力方向分开核对，按实际模型朝向设阈值 |

## 2. 统一坐标尺度与两个组合起点

动手搭信号前，先读对象的局部包围盒、世界变换与场景单位，确保同一作品里各部件的物理纹理尺度统一。同一块木料的顶面、侧面和端面共享同一个 3D 坐标体积场，先做横向 `warp`，再驱动年轮、长纤维与导管，让端面自然露出年轮切面。

- **缎面暖木材起点**：`coordinate` -> 慢速横向 `warp`（低沿轴频率保留长弧） -> `wood` 环形切面经窄 `remap` 提取晚材，与拉长 `noise` 纤维按小权重混合 -> 高频细长 `voronoi F1` 提取早材导管 -> 分别驱动 BaseColor、Roughness（约 `0.32–0.43`）、少量 Coat 与微米级 Height（`height_distance = 0.001`）。
- **涂漆钢与局部锈蚀起点**：按「区域场 `G`（下缘 `height_band`、受力 `edge`、凹处 `cavity`） × 分布场 `D`（宏观低频 `noise` + 碎边中频 `noise`）」经窄软阈值生成掉漆与锈蚀遮罩 -> 层序按裸钢（`metallic = 1`） -> 漆面（`metallic = 0`，`roughness` 约 `0.30–0.39`） -> 氧化物（`metallic = 0`，`roughness` 约 `0.68–0.90`） -> 少量凹处积灰组织，并保留旧化主开关方便对照干净基材。

工程内置的配方辅助脚本 `tools/material_workflow_addon/procedural_examples.py` 提供 `make_recipe`（支持 `painted_metal`、`wood`、`glass_clear`、`glass_tinted`、`glass_frosted` 及 `grain_axis`、`metal_region`、`timber_scale` 参数），可生成合法 2.0 起始 `manifest`，再按实际模型尺寸与参考微调。

## 3. 2.0 Manifest 结构与表面厚度

外层 CLI 请求版本为 `1.0`，程序化内部清单使用 `schema_version: "2.0"` 与 `material.template: "procedural_pbr_v1"`。所有信号声明稳定 `id`、`type`、`parameters` 与 `inputs`（无上游依赖写 `inputs: {}`），并确保 UV 位于单个 `0..1` 象限且无正面积重叠。

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
  "layers": [{
    "id": "substrate", "name": "Metal substrate", "enabled": true,
    "opacity": 1, "mask": null,
    "channels": {"base_color": "metal_color", "roughness": "micro_roughness"},
    "values": {"metallic": 1, "height": 0, "alpha": 1, "transmission": 0,
      "ior": 1.5, "coat_weight": 0, "coat_roughness": 0.03}
  }],
  "height_distance": 0.001,
  "mesh_map_size": 512,
  "preview": {
    "engine": "CYCLES", "device": {"backend": "CPU"},
    "width": 384, "height": 384, "samples": 16, "denoise": false,
    "color": {"view": "AgX", "exposure": 0, "gamma": 1}, "margin": 1.15,
    "views": [
      {"id": "soft", "azimuth": 45, "elevation": 20, "lighting": "soft"},
      {"id": "raking", "azimuth": -45, "elevation": 10, "lighting": "raking"}
    ]
  }
}
```

- **可选表面厚度（玻璃与薄壳）**：在 manifest 顶层可写 `"surface": {"thickness_mode": "SLAB", "thickness": 0.015}`（模式可选 `SLAB` 或 `SPHERE`，厚度取对象空间真实壁厚标量；省略 `thickness` 保持 `AUTO`）。厚壁玻璃在 EEVEE Next 下发黑时，先核对真实壁厚、光线追踪设置与 `SLAB` 近似，再与 Cycles 同机位对照。
- **集中控件与重开读回**：程序化受管材质把集中参数放在 `MWP Controls` 组实例，根级 `Principled BSDF` 与总 Height Bump 位于材质本身，导出端由 `mwp_channel` Reroute 标识。保留这些受管标识，改参后读回实例值核对。

## 4. 逐通道导出与纯贴图重建

作者外观在预览里达标后，用 `material procedural-export` 导出贴图并自动生成纯贴图重建工程。当前支持的导出通道名包括：`base_color`、`roughness`、`metallic`、`normal`、`height`、`alpha`、`transmission`、`ior`、`coat_weight`、`coat_roughness`。需要 AO 时走高低模投射入口 `material projection-bake`。

- **BaseColor**：从最终颜色信号经受控 Emit 通道捕获，以 `sRGB` 保存，纯金属与非金属均准确还原、无灯光污染。
- **标量通道与 IOR**：设为 `Non-Color` 导出 8/16 位 PNG；`IOR` 按 `(IOR - 1) / 3` 编码保存，重建时按 `3 * x + 1` 解码还原。
- **Height 与 Normal**：`height` 输出有符号 32 位浮点 EXR，保留零位、符号与物理幅度；`normal` 走 Cycles 真实切线空间 OpenGL `+Y` 烘焙，已包含总 Height Bump。纯贴图重建工程直接连 `Normal Map`，避免再次叠加同一 Height。

## 5. 三个高频诊断点

1. **有年轮节点却看不出端面弧纹**：先查年轮与长纤维的 `Mix` 权重，权重过小会让端面年轮被淹没。拍一张端面正视与顶面同框对照，再调混合权重。
2. **露钢层与锈层同时混成灰斑**：先让掉漆遮罩内的裸钢完整替代漆层，再用独立的氧化分布场叠加锈层，避免把同一噪声反复相乘把有效遮罩压成全零。
3. **朝上法线场覆盖不到水平凸筋**：`max(Normal.Z, 0)` 只选中朝上的肩面，水平向外的凸脊值为零。需要凸脊接触磨损时改用 `edge` 或对应方向场，并在目标区域查看最终遮罩图确认非零有效。
