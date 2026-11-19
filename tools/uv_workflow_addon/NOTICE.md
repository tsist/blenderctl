# UV Workflow 来源与依赖

本文件说明 UV Workflow 插件包的来源、运行时依赖和许可边界。插件包只包含本项目编写的 Python 生产模块、`blender_manifest.toml`、完整 GPLv3 `LICENSE` 与本说明，不包含 Blender、Python、NumPy、CLI、模型、贴图、字体、审计记录、作业目录或凭据。

## 代码来源

- 插件生产模块位于 `tools/uv_workflow_addon/`，由本项目维护；本次有限源码复核未发现从 xatlas、OptCuts、UVgami、Blender 自带 UV 求解器源码或其他第三方项目复制并改写的实现。
- 模块使用 Python 标准库、Blender 自带的 `bpy`、`bmesh`、`mathutils`，以及 Blender 发行版随附的 NumPy。没有 vendor 第三方求解器、二进制、wheel 或网络服务客户端。
- 插件不包含 blenderctl CLI。CLI 是独立完整项目，必须以匹配的实现身份单独提供；插件 ZIP 内的 UV 模块与 CLI 的 `tools/uv_workflow_addon/` 必须来自同一源码版本。
- 有限源码复核不是对所有未来贡献的法律判断；新增代码必须继续记录实际来源和许可，不能用 SPDX 标识替代完整许可证文本。

## 运行时依赖

| 组件 | 用途 | 是否随插件 ZIP 分发 | 许可/来源 |
| --- | --- | --- | --- |
| Blender 5.2.1 LTS（`bpy`、`bmesh`、`mathutils`） | Blender 扩展宿主与原生 UV 求值 | 否 | Blender 发行版的 GPL 与组件声明；blender.org |
| CPython 3.13 | Blender 内置 Python 与宿主脚本 | 否 | Python Software Foundation License；python.org |
| NumPy | UV 度量、数组与数值计算 | 否；由 Blender 发行版提供 | BSD-3-Clause；numpy.org |
| blenderctl | 独立 CLI、作业、资源锁定、独立重开验证 | 否 | GPL-3.0-or-later；项目现有完整 CLI |
| Blender glTF / FBX / OBJ 交换模块 | 下游交换 | 否；从实际 Blender 安装导入 | 以实际 Blender 发行版的组件许可为准 |

## 许可

插件源码与配套技能按 **GPL-3.0-or-later** 分发。完整 GPLv3 文本见同包根目录 `LICENSE`；`blender_manifest.toml` 中的 `SPDX:GPL-3.0-or-later` 只是机器可读元数据，不替代 `LICENSE`。Blender、Python、NumPy 和用户资产仍受各自许可与授权约束，本插件许可不授予这些内容的再分发权。
