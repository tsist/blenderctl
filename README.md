# blenderctl

## Material Workflow 独立仓库 / separate repository

Material Workflow 插件与配套技能现由 [tsist/blender-material-workflow](https://github.com/tsist/blender-material-workflow) 独立维护和发布。CLI 保留 `material.*` 调度接口，通过固定提交的 Git 子模块引用核心。

Clone with `git clone --recurse-submodules https://github.com/tsist/blenderctl.git`. Existing clones: after updating, run `git submodule update --init --recursive`. Use the release asset `blenderctl-0.54.2-source.zip` for a complete unpacked backend; GitHub automatic source archives omit submodules.

Extension ZIPs and new material companion skill bundles are published in the [separate release](https://github.com/tsist/blender-material-workflow/releases/tag/v0.7.1). This repository retains its previous four-skill snapshot for compatibility. Historical release assets are unchanged.


Recoverable Blender automation for agents and people: explicit JSON inputs, SHA-256 protected source files, isolated background jobs, editable candidates, and a matching layered-material Blender extension.

**CLI 0.54.2 · Material Workflow 0.7.1 · GPL-3.0-or-later**

**[中文使用说明](README.zh-CN.md)** · [Getting started](docs/GETTING_STARTED.md) · [Material workflow](docs/MATERIAL_WORKFLOW.md) · [CLI reference](docs/CLI_REFERENCE.md) · [Compatibility](docs/COMPATIBILITY.md)

[Companion agent skills](skills/README.md): CLI operation, work methodology, material authoring and asset management. The separate **0.1.0** bundle includes installation guidance and helper scripts; see [skills verification](docs/SKILLS.md).

## What you get

- CLI adapters for scene construction, mesh/UV/node work, materials, rigs/animation, simulation, rendering, media, tracking, format exchange, dependencies, and asset workflows. Each adapter has a bounded contract; this is not every Blender operation.
- Immutable job requests, explicit source/resource hashes, cancellation, recoverable pipelines, and separately reviewed file transactions.
- Material Workflow: static PBR layers, managed node graphs, templates, explicit object/slot/face assignments, Cycles/Eevee diagnostic views, contact sheets, batch checkpoints, and bounded parameter studies.
- A Blender sidebar for material drafts, batch assignments, and persistent snapshots handed to the same CLI backend.
- Public JSON Schemas, generated test fixtures, source archives, a separately released extension ZIP, and release checksums.

## Start

Install Python **3.12+** and Blender separately. The runtime baseline is **Windows, Blender 5.2.1 LTS, build `9e2066aef7ef`, Python 3.13**. Some native adapters deliberately reject other Blender versions. Host-only CI on Linux does not establish Linux Blender compatibility.

Download the source ZIP from [Releases](https://github.com/tsist/blenderctl/releases), or clone this repository. From its root:

```powershell
$env:BLENDER_PATH = 'C:\Program Files\Blender Foundation\Blender 5.2\blender.exe'
python tools/blenderctl/cli.py --version
python tools/blenderctl/cli.py --jobs-dir ./runtime/jobs doctor
python tools/blenderctl/cli.py material describe --operation study --section study
```

The path is an example: set it to your actual executable. All global options, including `--blender`, `--jobs-dir`, `--timeout`, `--async` and `--compact`, go **before** the subcommand. Use `blenderctl.ps1` on Windows if preferred; `BLENDERCTL_PYTHON` selects an explicit Python executable. See the [tested scene/render walkthrough](docs/GETTING_STARTED.md).

Download `material-workflow-0.7.1.zip` from the independent Material Workflow release and install it using Blender **Preferences → Extensions → Install from Disk**. Enable **Material Workflow**. Its backend project path is this repository; Blender and Python paths must point to your installed runtime. See [plugin installation and workflow](docs/MATERIAL_WORKFLOW.md).

## Verify and build

```powershell
python -m unittest discover -s tests -p "test_*.py" -v
python tests/run_blender_smoke.py --blender $env:BLENDER_PATH --output ./test-output/blender
python scripts/build_release.py --output ./dist
```

The integration test creates its own cube, UVs and texture, renders with CPU, checks source preservation, reopens the candidate, and tests extension registration. It does not touch an open Blender session. CI verifies host contracts and reproducible packaging. [Release verification](docs/RELEASE_VERIFICATION.md) separates those results from the local Blender run and historical adapter validation.

## Boundaries

CLI inputs are **saved disk snapshots**. Background jobs cannot detect unsaved GUI edits. Material workflows require existing UVs and static image files; automatic general-purpose UV repair, UDIM/animated textures, automatic aesthetic selection and DLSS are not included. Script execution requires explicit trust; the Windows AppContainer route is experimental and has no fallback. Preserve successful job directories when candidates refer to their textures, caches or receipts.

Blender, third-party binaries, private assets and user configuration are not distributed. See [third-party notices](THIRD_PARTY_NOTICES.md), [security policy](SECURITY.md), and [contribution guide](CONTRIBUTING.md).
