# blenderctl 与 Material Workflow

## Material Workflow 独立仓库 / separate repository

Material Workflow 插件与配套技能现由 [tsist/blender-material-workflow](https://github.com/tsist/blender-material-workflow) 独立维护和发布。CLI 保留 `material.*` 调度接口，通过固定提交的 Git 子模块引用核心。

Clone with `git clone --recurse-submodules https://github.com/tsist/blenderctl.git`. Existing clones: after updating, run `git submodule update --init --recursive`. Use the release asset `blenderctl-0.54.2-source.zip` for a complete unpacked backend; GitHub automatic source archives omit submodules.

Extension ZIPs and new material companion skill bundles are published in the [separate release](https://github.com/tsist/blender-material-workflow/releases/tag/v0.7.1). This repository retains its previous four-skill snapshot for compatibility. Historical release assets are unchanged.


面向代理与人工的 Blender 自动化工具：用明确 JSON 清单与 SHA-256 保护源文件，通过独立后台作业产出可编辑候选，配套材质插件共用同一执行核心。

**CLI 0.54.2 · 材质插件 0.7.1 · GPL-3.0-or-later**

另提供[四个配套 Blender 技能](skills/README.md)：CLI 操作、制作方法论、材质制作、资产管理。技能包 **0.1.0** 可独立下载安装，见[安装与验证](docs/SKILLS.md)。

## 能力

- 场景、网格、UV、节点、骨骼动画、模拟、渲染、剪辑、跟踪、格式交换、依赖与资产操作的有界适配器；每个接口都有自己的输入和已验边界。
- 独立作业、源与资源 SHA、取消、检查点、流水线恢复、显式文件事务。
- 材质图层、静态 PBR 通道、遮罩、模板、增量冲突检测、多对象分配、Cycles/Eevee 多视角与拼图。
- `material.study` 在一个有界作业内比较有限参数候选，报告图像语义警示、参数差异与阶段进度。
- Blender 面板编辑草稿、批量分配、保存持久快照并交给 CLI 后台；当前场景不会自动载入后台候选。

## 安装与第一步

从 [blenderctl Releases](https://github.com/tsist/blenderctl/releases) 下载完整 CLI 源码 ZIP；插件 ZIP 与新配套技能包从 [Material Workflow Releases](https://github.com/tsist/blender-material-workflow/releases) 下载。另行安装 Python **3.12+** 和 Blender。实际运行基线为 **Windows / Blender 5.2.1 LTS / build 9e2066aef7ef / Python 3.13**；部分适配器明确要求该 Blender 版本。

解压源码后在目录内执行：

```powershell
$env:BLENDER_PATH = 'C:\Program Files\Blender Foundation\Blender 5.2\blender.exe'
python tools/blenderctl/cli.py --version
python tools/blenderctl/cli.py --jobs-dir ./runtime/jobs doctor
python tools/blenderctl/cli.py material describe --operation run --schema
```

将示例路径换为实际 Blender。Windows 可使用 `./blenderctl.ps1`；设置 `BLENDERCTL_PYTHON` 可指定 Python。没有 Blender 时仍可读取版本、帮助和材质 Schema。

在 Blender 的 **Preferences → Extensions → Install from Disk** 安装 `material-workflow-0.7.1.zip` 并启用。材质面板在 View3D 侧栏；后台项目目录填写本源码目录，运行时使用实际 Blender 路径。

完整操作参见 [快速开始](docs/GETTING_STARTED.md)、[材质工作流](docs/MATERIAL_WORKFLOW.md)、[命令索引](docs/CLI_REFERENCE.md) 与 [兼容范围](docs/COMPATIBILITY.md)。详细 JSON 合同位于 `docs/cli/schemas/`；全局参数必须放在子命令之前。

## 验证与限制

```powershell
python -m unittest discover -s tests -p "test_*.py" -v
python tests/run_blender_smoke.py --blender $env:BLENDER_PATH --output ./test-output/blender
python scripts/build_release.py --output ./dist
```

集成测试自行生成几何、UV 和纹理，执行 CPU 渲染、源保护、保存重开与插件注册验证。后台执行不能判断 GUI 未保存内容；请先保存独立快照。通用自动 UV、UDIM/动画贴图、自动审美决策及 DLSS 尚未提供。含依赖的候选工程必须保留相关作业目录。完整验收与未验范围见 [发布验证记录](docs/RELEASE_VERIFICATION.md)。

仓库不包含 Blender 安装包、私人资产、运行时配置或历史审计目录。贡献与安全反馈见 [CONTRIBUTING](CONTRIBUTING.md)、[SECURITY](SECURITY.md)。
