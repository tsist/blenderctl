# blenderctl + Blender workflows

**0.64.0** adds the `production.run` request for four Blender 5.3.0 companion add-ons. It covers targeted animation edits, native compositor effects, multi-light goals, and transactional VSE recuts. [Production setup and requests](docs/PRODUCTION.md)

**0.63.1-r3** adds `blenderctl preview`: zero-disruption Windows isolated-desktop real-viewport 4-view fast review (`wireframe`/`solid`/`solid_wire`/`matcap`/`face_orientation`/`material`/`rendered` with Blender 5.3 Cycles **NVIDIA DLSS Ray Reconstruction** or explicit OptiX fallback, EEVEE Next, one-command focus/`--isolate`, same-view `--compare-modes` boards, and optional hot sessions). See [viewport preview guide](docs/VIEWPORT_PREVIEW.md).

**0.63.1** adds Windows Blender 5.3.0 selection, 5.2-to-5.3 project copies and multi-effect compositor checks. See the [compatibility matrix](docs/COMPATIBILITY.md) and [independent rigging setup](docs/RIGGING.md).

**0.63.0 publication:** the qualified video-editing snapshot is now included, with Video Editing Workflow 0.1.1, portable skill and template library 0.1.0. See [video setup and limits](docs/VIDEO-EDITING.md), [current validation](docs/VALIDATION.md) and the [standalone video repository](https://github.com/tsist/blender-video-editing-workflow). Existing material guidance below remains available; other frozen specialty repositories can have different implementation identities despite sharing a CLI version.

Recoverable Blender automation for agents and people: explicit JSON inputs, SHA-256 protected source files, isolated background jobs, editable candidates, and a matching layered-material Blender extension.

**CLI 0.64.0 · Material Workflow 0.7.0 snapshot · GPL-3.0-or-later**

**[中文使用说明](README.zh-CN.md)** · [Getting started](docs/GETTING_STARTED.md) · [Material workflow](docs/MATERIAL_WORKFLOW.md) · [CLI reference](docs/CLI_REFERENCE.md) · [Compatibility](docs/COMPATIBILITY.md)

[Companion agent skills](skills/README.md) cover CLI operation, work methodology, rigging, video editing, material authoring and asset management. The separate **0.8.0** bundle includes installation guidance and helper scripts. [Skills verification](docs/SKILLS.md)

## What you get

- `preview`: isolated-desktop Windows Blender 5.3 real-viewport 4-view fast review with object/collection focusing, semi-transparent wireframe overlay on any mode, MatCap/face-orientation diagnostics, same-view 4-stage comparison boards, optional hot sessions, and Cycles GPU **DLSS Ray Reconstruction** (driver ≥ 620, explicit OptiX fallback) + EEVEE Next previews. [Guide and examples](docs/VIEWPORT_PREVIEW.md)
- CLI adapters for scene construction, mesh/UV/node work, materials, rigs/animation, simulation, rendering, media, tracking, format exchange, dependencies, and asset workflows. Each adapter has a bounded contract; this is not every Blender operation.
- `rigging-workflow` calls the standalone v003 commands for character binding, selected-bone weight recalculation, rig repair and weight transfer policy. [Setup and commands](docs/RIGGING.md)
- Immutable job requests, explicit source/resource hashes, cancellation, recoverable pipelines, and separately reviewed file transactions.
- Material Workflow: static PBR layers, managed node graphs, templates, explicit object/slot/face assignments, Cycles/Eevee diagnostic views, contact sheets, batch checkpoints, and bounded parameter studies.
- A Blender sidebar for material drafts, batch assignments, and persistent snapshots handed to the same CLI backend.
- Public JSON Schemas, generated test fixtures, source archives, an installable extension ZIP, and release checksums.

## Start

Install Python **3.12+** and Blender separately. The runtime baseline is **Windows, Blender 5.2.1 LTS, build `9e2066aef7ef`, Python 3.13**. Some native adapters deliberately reject other Blender versions. Host-only CI on Linux does not establish Linux Blender compatibility.

Download the source ZIP from [Releases](https://github.com/tsist/blenderctl/releases), or clone this repository. From its root:

```powershell
$env:BLENDER_PATH = 'C:\Program Files\Blender Foundation\Blender 5.2\blender.exe'
python tools/blenderctl/cli.py --version
python tools/blenderctl/cli.py --blender $env:BLENDER_PATH --jobs-dir ./runtime/jobs doctor
python tools/blenderctl/cli.py material describe --operation study --section study
```

The path is an example: set it to your actual executable. All global options, including `--blender`, `--jobs-dir`, `--timeout`, `--async` and `--compact`, go **before** the subcommand. Use `blenderctl.ps1` on Windows if preferred; `BLENDERCTL_PYTHON` selects an explicit Python executable. See the [tested scene/render walkthrough](docs/GETTING_STARTED.md).

The historical material extension remains in the [v0.54.1 release](https://github.com/tsist/blenderctl/releases/tag/v0.54.1); download `material-workflow-0.7.0.zip` there separately and install it using Blender **Preferences → Extensions → Install from Disk**. Enable **Material Workflow**. Its backend project path is this repository; Blender and Python paths must point to your installed runtime. See [plugin installation and workflow](docs/MATERIAL_WORKFLOW.md).

## Verify and build

```powershell
python -m unittest discover -s tests -p "test_*.py" -v
python tests/run_blender_smoke.py --blender $env:BLENDER_PATH --output ./test-output/blender
python scripts/build_release.py --output ./dist
```

The integration test creates its own cube, UVs and texture, renders with CPU, checks source preservation, reopens the candidate, and tests extension registration. It does not touch an open Blender session. CI verifies host contracts and reproducible packaging. [Release verification](docs/RELEASE_VERIFICATION.md) separates those results from the local Blender run and historical adapter validation.

## Boundaries

CLI inputs are **saved disk snapshots**. Background jobs cannot detect unsaved GUI edits. Material workflows require existing UVs and static image files; automatic general-purpose UV repair, UDIM/animated textures, and automatic aesthetic selection are not included. Cycles DLSS viewport denoising is available via `blenderctl preview` on Windows Blender 5.3 with an RTX GPU and NVIDIA driver ≥ 620 (final offline `render run` uses OPTIX or OPENIMAGEDENOISE). Script execution requires explicit trust; the Windows AppContainer route is experimental and has no fallback. Preserve successful job directories when candidates refer to their textures, caches or receipts.

Blender, third-party binaries, private assets and user configuration are not distributed. See [third-party notices](THIRD_PARTY_NOTICES.md), [security policy](SECURITY.md), and [contribution guide](CONTRIBUTING.md).
