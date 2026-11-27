# blenderctl + Blender workflows

**0.65.0** 接入 Hard Surface Workflow 0.3.7。通过公开请求预检贴面扫掠、提交几何及接手续改已保存的命名站位。[贴面扫掠配置与请求](docs/HARD_SURFACE.md)

**0.64.0** 通过公共 `production.run` 接入 Blender 5.3.0 的动画修正与 Slot 编辑、原生合成效果栈、目标调光和 VSE 批量精剪。[安装和请求说明](docs/PRODUCTION.md)

**0.63.1-r3** 新增 **`blenderctl preview`**：基于 Windows 隔离桌面（`CreateDesktopW`）的零干扰真视口 4 视角快检工具，支持一键聚焦物体/集合（留上下文或 `--isolate` 独显）、线框/实体/实体叠半透明线框/MatCap/法线红蓝/LookDev/Cycles **NVIDIA DLSS Ray Reconstruction**（驱动 ≥ 620，未满足显式回退 GPU OptiX）与 EEVEE Next 渲染预览、同机位四阶段对比板及可选热会话复用。详见 [视口快检说明](docs/VIEWPORT_PREVIEW.md)。

**0.63.1** 已实测 Windows Blender 5.3.0 的运行时选择、5.2 工程另存、合成效果栈与静态材质预览。旧入口仍可选择 5.2.1。专项插件的版本和边界请查各自仓库。[兼容说明](docs/COMPATIBILITY.md) · [骨骼插件说明](docs/RIGGING.md)

**0.63.0 publication:** the qualified video-editing snapshot is now included, with Video Editing Workflow 0.1.1, portable skill and template library 0.1.0. See [video setup and limits](docs/VIDEO-EDITING.md), [current validation](docs/VALIDATION.md) and the [standalone video repository](https://github.com/tsist/blender-video-editing-workflow). Existing material guidance below remains available; other frozen specialty repositories can have different implementation identities despite sharing a CLI version.

面向代理与人工的 Blender 自动化工具：用明确 JSON 清单与 SHA-256 保护源文件，通过独立后台作业产出可编辑候选，配套材质插件共用同一执行核心。

**CLI 0.65.0 · 材质插件 0.7.0 · GPL-3.0-or-later**

另提供[六个配套 Blender 技能](skills/README.md)，包括 CLI 操作、制作方法论、材质制作、资产管理、绑定和剪辑。技能包 **0.9.0** 可独立安装。[安装与验证](docs/SKILLS.md)

## 能力

- `preview`：Windows 隔离桌面真视口 4 视角一键聚焦快检，支持线框、实体、原生半透明线框叠加、MatCap、法线红蓝、LookDev、Cycles DLSS/OptiX 与 EEVEE Next 预览及热会话复用。[使用与示例](docs/VIEWPORT_PREVIEW.md)
- 场景、网格、UV、节点、骨骼动画、模拟、渲染、剪辑、跟踪、格式交换、依赖与资产操作的有界适配器；每个接口都有自己的输入和已验边界。
- `rigging-workflow` 接入独立骨骼仓的 v003 命令，支持新角色蒙皮、按所选骨骼局部重算、已有绑定返修和转权策略。[配置与命令](docs/RIGGING.md)
- 独立作业、源与资源 SHA、取消、检查点、流水线恢复、显式文件事务。
- 材质图层、静态 PBR 通道、遮罩、模板、增量冲突检测、多对象分配、Cycles/Eevee 多视角与拼图。
- `material.study` 在一个有界作业内比较有限参数候选，报告图像语义警示、参数差异与阶段进度。
- Blender 面板编辑草稿、批量分配、保存持久快照并交给 CLI 后台；当前场景不会自动载入后台候选。

## 安装与第一步

从 [GitHub Releases](https://github.com/tsist/blenderctl/releases) 下载源码 ZIP 与插件 ZIP。另行安装 Python **3.12+** 和 Blender。实际运行基线为 **Windows / Blender 5.2.1 LTS / build 9e2066aef7ef / Python 3.13**；部分适配器明确要求该 Blender 版本。

解压源码后在目录内执行：

```powershell
$env:BLENDER_PATH = 'C:\Program Files\Blender Foundation\Blender 5.2\blender.exe'
python tools/blenderctl/cli.py --version
python tools/blenderctl/cli.py --blender $env:BLENDER_PATH --jobs-dir ./runtime/jobs doctor
python tools/blenderctl/cli.py material describe --operation run --schema
```

将示例路径换为实际 Blender。Windows 可使用 `./blenderctl.ps1`；设置 `BLENDERCTL_PYTHON` 可指定 Python。没有 Blender 时仍可读取版本、帮助和材质 Schema。

在 Blender 的 **Preferences → Extensions → Install from Disk** 安装 `material-workflow-0.7.0.zip` 并启用。材质面板在 View3D 侧栏；后台项目目录填写本源码目录，运行时使用实际 Blender 路径。

完整操作参见 [快速开始](docs/GETTING_STARTED.md)、[材质工作流](docs/MATERIAL_WORKFLOW.md)、[命令索引](docs/CLI_REFERENCE.md) 与 [兼容范围](docs/COMPATIBILITY.md)。详细 JSON 合同位于 `docs/cli/schemas/`；全局参数必须放在子命令之前。

## 验证与限制

```powershell
python -m unittest discover -s tests -p "test_*.py" -v
python tests/run_blender_smoke.py --blender $env:BLENDER_PATH --output ./test-output/blender
python scripts/build_release.py --output ./dist
```

集成测试自行生成几何、UV 和纹理，执行 CPU 渲染、源保护、保存重开与插件注册验证。后台执行不能判断 GUI 未保存内容；请先保存独立快照。通用自动 UV、UDIM/动画贴图与自动审美决策尚未提供；Cycles 视口 DLSS 降噪可通过 `blenderctl preview` 在 Windows Blender 5.3 + RTX 显卡 + NVIDIA 驱动 ≥ 620 环境使用（离线终渲 `render run` 使用 OPTIX / OPENIMAGEDENOISE）。含依赖的候选工程必须保留相关作业目录。完整验收与未验范围见 [发布验证记录](docs/RELEASE_VERIFICATION.md)。

仓库不包含 Blender 安装包、私人资产、运行时配置或历史审计目录。贡献与安全反馈见 [CONTRIBUTING](CONTRIBUTING.md)、[SECURITY](SECURITY.md)。
