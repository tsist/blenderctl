# Blender AI 1.0 统一 GUI 设计系统 (`tools/ui_kit`)

本目录是 Blender AI 1.0 全系 **14 个注册插件**的唯一 UI 设计系统权威源。通过 [sync_ui_kit.py](sync_ui_kit.py) 自动内联分发到各插件内部 `_ui_kit/` 目录，使每个插件既保持统一视觉与交互体验，又 100% 自包含通过官方 `blender --command extension validate`。

## 一、核心设计规范（冻结共识 Q1–Q13）

1. **独立标签拓扑**：1 插件 = 1 独立侧栏短标签，除 `V·剪辑` 驻留 `SEQUENCE_EDITOR` 外，其余 13 个插件全部常驻 `VIEW_3D` 侧栏。
2. **零重码单字母 + 工序直觉主题色**：全部 14 插件配色与代号由 [theme_tokens.py](theme_tokens.py) 唯一定义。
3. **五维视觉联动（零视口遮挡 · 原生侧栏直绘彩色图标）**：
   - **原生右侧竖排侧栏标签就地彩色化 + SVG 图标与激活面板色框（[sidebar_dock.py](sidebar_dock.py)）**：所有插件在 `register()` 中只需调用 `SidebarDock.register_plugin(DOMAIN, _ICONS.icons_dir)`、在 `unregister()` 中调用 `SidebarDock.unregister_plugin(DOMAIN)`。全局单例跨 `.blend` 加载持久运行（锚定于 `sys.modules["__blender_ai_ui_kit_shared_runtime_v1__"]`）：
     - **直接在 Blender 原生 `UI` 区域右侧竖排标签按钮本身上就地绘制**（≤1px 精准对齐 `UI_panel_category_draw_all`，零视口悬浮遮挡、零自定义 Gizmo）：每枚标签展示 **主题色底纹 + 右缘 3.5px 高亮主题色条 + 64px 真彩 SVG 品牌图标（零 `bpy.data.images` 污染）+ 易于阅读的正排竖列中文短名（`灯光` / `建模` / `UV` / `三渲二`）**；
     - 无论当前停留在 Blender 原生 `Item` / `Tool` / `View` 还是其他插件页，所有已装 Blender AI 插件的原生侧栏标签均保持清晰彩色图标识别；
     - 激活态标签亮起主题色底纹并与打开的 `UI` 面板顶部/左侧主题边框无缝贯通咬合，收起侧栏（`N`）后 3D 视口保持 100% 干净零遮挡。
   - **面板头与核心按钮 SVG 图标**：使用 `IconPack(domain)`，从 `_ui_kit/icons/` 加载由 [svg_builder.py](svg_builder.py) 生成的 `32px/64px` 无损 RGBA PNG（在 `bpy.app.background` 无头模式下自动跳过零开销）。
   - **侧栏竖排标签**：`bl_category = get_theme(domain).tab_label`（如 `L·灯光`、`H·硬面`），同时作为点击热区与原生兼容标识。
   - **场景数据块联动染色**：调用 `apply_collection_theme(col, domain)` / `apply_node_theme(node, domain)` / `apply_strip_theme(strip, domain)`。
   - **零闲置遮挡的按需 GPU HUD（[hud_overlay.py](hud_overlay.py)）**：`HUDManager` 内置强制零遮挡守卫——静态/闲置状态胶囊默认关闭，且**严禁在插件自身 N 面板隐藏或切离时于 3D 视口内显示**；仅在后台重型异步任务实际计算期间（`progress != None` 或 `kind == "RUNNING"`）才在右上角临时浮现主题色进度胶囊，任务完成或取消后自动消失。
4. **双模面板骨架**：
   - **重型插件（4 个：`H·硬面` / `R·绑定` / `A·动画` / `T·三渲二`）**：使用 `draw_heavy_workspace_header()` 提供「阶段切页 + 日常/进阶双档」。
   - **紧凑插件（10 个：`M/G/U/S/P/C/L/B/F/V`）**：以标杆插件 [lighting_workflow_addon/ui.py](../lighting_workflow_addon/ui.py) 为模板，零冗余顶栏状态卡，首屏直达核心操作 + Blender 5.3 `section_panel()`（原生 `layout.panel()`）折叠次级参数。
5. **人机交互与防卡死**：
   - 轻中量操作直接在当前场景生效并支持 `Ctrl+Z` 撤销，使用 `ensure_object_mode(context)` 自动代劳编辑态↔物体态切换。
   - 超过 2–3 秒的重型计算通过 `BackgroundJobRunner(domain)` 异步执行，联动 `progress_card()`（原生 `layout.progress()`）与右上角 GPU HUD 胶囊。
   - 切换工程前取消当前 Runner 的工作进程并清除定时器与 HUD。工作进程的标准输出与错误输出写入临时文件，结束后仅保留末尾摘要。

## 二、Blender 5.3 实测避坑铁律

- **只读属性规避**：Blender 5.3 RNA 中 `UILayout.property_split_factor` 为只读，统一经 `prop_column()` 设置 `use_property_split = True` 与 `use_property_decorate = False`，禁止手写 `property_split_factor = ...`。
- **进程级单例必须远离 `driver_namespace`**：Blender 每次 `File -> Open` / `Read blend` 都会清空 `bpy.app.driver_namespace`。所有跨插件共享状态（`SidebarDock`、`HUDManager`）均统一托管在 `sys.modules["__blender_ai_ui_kit_shared_runtime_v1__"]`。
- **Blender 5.3 原生侧栏标签直绘与 GPU 纹理规范**：
  - `gpu.types.GPUTexture((w, h), format="RGBA8", data=buf)` 必须传入 `gpu.types.Buffer("FLOAT", ...)`（归一化 `0.0..1.0`），传入 `UBYTE` 会报错；
  - `SpaceView3D / SpaceSequenceEditor` 的 `UI` 区域 `POST_PIXEL` 回调不受右侧标签区裁剪，可直接以 `UI_UNIT_X = round(20 * ui_scale)` 与 `blf.dimensions(0, cat)` 精确对齐每一枚原生竖排标签，实时可见标签列表通过带缓存的 `active_panel_category` 枚举探测获取；
  - 所有插件不得默认在 3D 视口挂载常驻闲置 HUD（如 `show_viewport_hud` 必须默认 `False`），且 `HUDManager` 仅在异步任务运行中或对应插件面板当前展开激活时绘制。
- **官方扩展安装动态包名**：经 `extension install-file` 安装后，插件位于 `%APPDATA%/Blender Foundation/Blender/5.3/extensions/user_default/<manifest_id>`（模块路径 `bl_ext.user_default.<manifest_id>`）。插件内部必须 100% 使用相对导入；所有子进程 worker 脚本不得硬编码 `from <dev_folder>_addon import ...`，必须按 `Path(__file__).resolve().parent.name` 动态 `importlib.import_module(...)`。
- **Toon 校验清单**：同步 `toon` 插件时，`sync_ui_kit.py --plugin toon` 会自动重算更新 `toon_workflow/bundle.json` SHA-256 文件清单。

## 三、常用构建与部署命令

```powershell
$py = '<PROJECT_ROOT>\runtime\blender\releases\5.3.0\blender-5.3.0-windows-x64\5.3\python\bin\python.exe'

# 1. 重建全部 80 枚 SVG、94 张 PNG 与 14 插件视觉识别看板
& $py -X utf8 '<PROJECT_ROOT>\tools\ui_kit\svg_builder.py'

# 2. 将 _ui_kit 与专属图标内联同步至指定或全部插件
& $py -X utf8 '<PROJECT_ROOT>\tools\ui_kit\sync_ui_kit.py' --plugin lighting
& $py -X utf8 '<PROJECT_ROOT>\tools\ui_kit\sync_ui_kit.py' --all

# 3. 打包、执行 blender --command extension validate 并一键安装启用进本机 Blender 5.3
& '<PROJECT_ROOT>\tools\install_blender53_gui_suite.ps1' -Plugin lighting
& '<PROJECT_ROOT>\tools\install_blender53_gui_suite.ps1' -All
```
