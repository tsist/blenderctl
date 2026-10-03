# Blender agent skills / Blender 配套技能

Four companion skills for **blenderctl 0.54.1** and **Material Workflow 0.7.0**, distributed under **GPL-3.0-or-later**. Public bundle version: **0.1.0**.

| Skill | Use |
| --- | --- |
| `blender-cli` | Saved-file CLI operations, discovery, rendering, job/transaction protection and recovery |
| `blender-work-methodology` | Plugin/CLI work units, reference/assembly decisions, progress, preview and acceptance |
| `blender-material-authoring` | References, PBR layers/channels, image budgets, managed material operations and validation |
| `blender-asset-manager` | Asset identity, classification, new publish naming, Catalog, dependency and migration records |

## Install in Codex

Download `blender-skills-0.1.0.zip` from the [skills release](https://github.com/tsist/blenderctl/releases/tag/skills-v0.1.0), or use the repository. Keep all four skill folders as siblings because they link to each other. They use the standard `SKILL.md`, `agents/openai.yaml`, `references/`, `scripts/` and `assets/` structure; other agent hosts must support or adapt that structure themselves.

From the repository root:

```powershell
# List the four skills without changing anything.
python scripts/install_skills.py --list
# Use an explicit installation destination; existing skills are never replaced.
python scripts/install_skills.py --destination '<CODEX_HOME>\skills'
```

When `CODEX_HOME` is unset, Codex's personal skill location is `~/.codex/skills`. Replace the example with the actual absolute destination. The same installer is at the root of the skill ZIP. You can also copy the four folders manually, preserving their sibling layout. If a same-named skill already exists, compare versions and choose your migration explicitly; the installer refuses before copying any skill. Restart the agent session if needed for its host's discovery mechanism.

From an extracted skill ZIP root, use `python install_skills.py --destination '<actual-skill-directory>'`. Installation copies the four sibling directories only; it never replaces existing skills or automatically configures the CLI/runtime.

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

## 中文说明

四个技能保持同级安装：CLI操作、制作方法论、材质制作、资产管理。`BLENDERCTL_ROOT` 指向另行解压的公开源码，`BLENDER_PATH` 指向实际 Blender。辅助脚本不启动 Blender；只有本次实际制作/验收才走已授权 CLI。资产根由用户当次明确，随包资产标准是新项目可采用的基线，已有项目规则优先。

原个人技能和工作区规则保持完整。公开版移除本机盘符/用户名、私人 Wiki、固定任务编号、原资产及用户对话；六个领域方法仍是 candidate，历史教训不冒充公开可复现或跨模型验证。本包保留原生生图优先的材质路线，用户当前明确要求优先。

结构/链接、安装冲突保护、实际辅助脚本和 CI 的验证见 [skills verification](https://github.com/tsist/blenderctl/blob/main/docs/SKILLS.md)。软件已验 Windows/Blender 5.2.1 基线不因发布指导文件而扩大。
