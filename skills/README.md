# Blender agent skills / Blender 配套技能

Six companion skills for **blenderctl 0.63.1** and **Material Workflow 0.7.0**, distributed under **GPL-3.0-or-later**. The current source bundle is **0.5.0**.

| Skill | Use |
| --- | --- |
| `blender-video-editing` | Whitebox/AI/CG editing, shot candidates, audio/subtitles, manual continuation and SDR templates |
| `blender-rigging` | Character rigging and the v003 Windows Blender 5.3.0 command-line preview |
| `blender-cli` | Saved-file CLI operations, discovery, rendering, job/transaction protection and recovery |
| `blender-work-methodology` | Plugin/CLI work units, reference/assembly decisions, progress, preview and acceptance |
| `blender-material-authoring` | References, PBR layers/channels, image budgets, managed material operations and validation |
| `blender-asset-manager` | Asset identity, classification, new publish naming, Catalog, dependency and migration records |

## Install in Codex

Install from the current repository checkout. Keep all six skill folders as siblings because they link to each other. They use the standard `SKILL.md`, `agents/openai.yaml`, `references/`, `scripts/` and `assets/` structure; other agent hosts must support or adapt that structure themselves.

From the repository root:

```powershell
# List the six skills without changing anything.
python scripts/install_skills.py --list
# Use an explicit installation destination; existing skills are never replaced.
python scripts/install_skills.py --destination '<CODEX_HOME>\skills'
```

When `CODEX_HOME` is unset, Codex's personal skill location is `~/.codex/skills`. Replace the example with the actual absolute destination. The same installer is at the root of the skill ZIP. You can also copy the six folders manually, preserving their sibling layout. If a same-named skill already exists, compare versions and choose your migration explicitly; the installer refuses before copying any skill. Restart the agent session if needed for its host's discovery mechanism.

From an extracted skill ZIP root, use `python install_skills.py --destination '<actual-skill-directory>'`. Installation copies the six sibling directories only; it never replaces existing skills or automatically configures the CLI/runtime.

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

0.5.0 技能包包含 CLI 操作、制作方法论、材质制作、资产管理、视频剪辑和角色绑定。六个技能保持同级安装。新选骨重算和转权使用 5.3.0 的独立骨骼候选源码。将 `BLENDERCTL_RIGGING_ROOT` 指向骨骼仓库根，并按[公开 CLI 接入说明](https://github.com/tsist/blenderctl/blob/main/docs/RIGGING.md)查询命令。资产取用在目标 Blender 版本实际拖入和保存，再独立重开检查。

辅助脚本只读取公开合同。软件与技能可分别安装，同一作业的恢复沿用原发行和源指纹。结构、链接及安装验证见 [skills verification](https://github.com/tsist/blenderctl/blob/main/docs/SKILLS.md)。
