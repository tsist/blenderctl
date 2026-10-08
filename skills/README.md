# Blender agent skills / Blender 配套技能

Five companion skills, distributed under **GPL-3.0-or-later**. Development bundle version: **0.3.0-dev.21-r1**. The original four skills remain at **0.2.0**, targeting **blenderctl 0.54.1** and **Material Workflow 0.7.0**. The new hard-surface guidance is **0.1.0-dev.21-r1** for the separately installed Hard Surface Workbench dev21 candidate; it is not bundled with or enabled by the material extension.

| Skill | Use |
| --- | --- |
| `blender-cli` | Saved-file CLI operations, discovery, rendering, job/transaction protection and recovery |
| `blender-work-methodology` | Plugin/CLI work units, reference/assembly decisions, progress, preview and acceptance |
| `blender-material-authoring` | References, PBR layers/channels, image budgets, managed material operations and validation |
| `blender-asset-manager` | Asset identity, classification, new publish naming, Catalog, dependency and migration records |

| `blender-hard-surface-authoring` | Sparse editable hard-surface planning, reference gates, managed edits, subdivision and surface-evidence limits |

## Install in Codex

The [stable 0.2.0 skills release](https://github.com/tsist/blenderctl/releases/tag/skills-v0.2.0) contains the original four skills only. This development branch adds the fifth skill; use the branch checkout to inspect it. No new stable release is implied. Keep all manifest-listed skill folders as siblings because they link to each other. They use the standard `SKILL.md`, `agents/openai.yaml`, `references/`, `scripts/` and `assets/` structure; other agent hosts must support or adapt that structure themselves.

From the repository root:

```powershell
# List the manifest-listed skills without changing anything.
python scripts/install_skills.py --list
# Use an explicit installation destination; existing skills are never replaced.
python scripts/install_skills.py --destination '<CODEX_HOME>\skills'
```

When `CODEX_HOME` is unset, Codex's personal skill location is `~/.codex/skills`. Replace the example with the actual absolute destination. The same installer is at the root of the skill ZIP. You can also copy the manifest-listed folders manually, preserving their sibling layout. If a same-named skill already exists, compare versions and choose your migration explicitly; the installer refuses before copying any skill. Restart the agent session if needed for its host's discovery mechanism.

From an extracted skill ZIP root, use `python install_skills.py --destination '<actual-skill-directory>'`. Installation copies the manifest-listed sibling directories only; it never replaces existing skills or automatically configures the CLI/runtime.

Configure the separate software checkout and runtime:

```powershell
$env:BLENDERCTL_ROOT = '<absolute-unpacked-blenderctl-source-root>'
$env:BLENDER_PATH = '<absolute-blender-executable>'
python '<installed-skills>\blender-cli\scripts\discover.py' --root $env:BLENDERCTL_ROOT --command material.run
```

The skill ZIP contains guidance, scripts and templates; install the CLI/extension separately. It does not download Blender, configure MCP, install an image provider or modify global preferences. Root discovery can use the repository layout, explicit `--root`, or `BLENDERCTL_ROOT` when installed elsewhere.

## Optional image gate

The CLI discovery and name-check helpers use the Python standard library. Material image decoding requires **Pillow** in the interpreter you choose. If you want to install it in your own virtual environment, use `python -m pip install -r skills/requirements-images.txt` from the checkout. Skill execution itself never installs packages. An existing Pillow-capable interpreter can run `image_gate.py` directly.

Live Blender MCP, native image generation, optional fallback services and memory/Wiki are host capabilities, not bundled dependencies. Missing memory/Wiki does not block work or imply prior task state. Third-party image services require the current user's authorization/configuration; this public package inherits no private service permission.

## Hard-surface development guidance

The new [hard-surface skill](blender-hard-surface-authoring/SKILL.md) separates reference approval, actual L0 inspection, editability, evaluated geometry, native normals/highlights and delivery qualification. Read its [provenance and current limitations](blender-hard-surface-authoring/references/provenance.md) before execution. Dev19 has two independent parameter-edit native chains; dev20 has limited surface-export/normal-binding technical evidence but insufficient broad-surface highlight coverage; dev21 reflected-anchor lighting is HOST-tested and **native not run**. The package contains no private model, reference image, case parameters or prior user authorization.

## 中文说明

0.2.0修订方法论、材质与CLI指导：按需回读合同和证据，前移区域/UV与关键区域可观察性检查，按依赖准备生图与有界诊断，复制前核对依赖，最终质量反馈前备齐交付说明。构建器、通用诊断预设和全链路遥测仍是待开发候选，不宣称本次指导修订已测得提速。资产管理指导内容不变，仅随技能包更新版本标识；CLI与插件未升级。

当前开发包五个技能同级组织：原四项CLI操作、制作方法论、材质制作、资产管理保持0.2.0内容；新增硬表面指导0.1.0-dev.21-r1。插件另行安装；本次公开源码不表示已安装或稳定发布。`BLENDERCTL_ROOT` 指向另行解压的公开源码，`BLENDER_PATH` 指向实际 Blender。辅助脚本不启动 Blender；只有本次实际制作/验收才走已授权 CLI。资产根由用户当次明确，随包资产标准是新项目可采用的基线，已有项目规则优先。

原个人技能和工作区规则保持完整。公开版移除本机盘符/用户名、私人 Wiki、固定任务编号、原资产及用户对话；六个领域方法仍是 candidate，历史教训不冒充公开可复现或跨模型验证。本包保留原生生图优先的材质路线，用户当前明确要求优先。

结构/链接、安装冲突保护、实际辅助脚本和 CI 的验证见 [skills verification](https://github.com/tsist/blenderctl/blob/main/docs/SKILLS.md)。软件已验 Windows/Blender 5.2.1 基线不因发布指导文件而扩大。
