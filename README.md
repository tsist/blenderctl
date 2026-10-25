# blenderctl + Blender workflows

**0.64.0 adds procedural PBR authoring and export** on the public 0.63 baseline. Agents can build editable registered signal graphs, render protected candidates, bake channel maps and reopen a reconstruction made from disk textures. See [procedural materials](docs/PROCEDURAL_MATERIALS.md) and [validation scope](docs/VALIDATION.md).

The existing Video Editing Workflow 0.1.1, source/extension/skill/template packaging and video algorithms are retained. See [video setup and limits](docs/VIDEO-EDITING.md) and the [standalone video repository](https://github.com/tsist/blender-video-editing-workflow). This release does not import later unrelated local CLI or Toon changes.

Recoverable Blender automation for agents and people: explicit JSON inputs, SHA-256 protected source files, isolated background jobs, editable candidates, and a matching layered-material Blender extension.

**CLI 0.64.0 · Material Workflow 0.8.0 · GPL-3.0-or-later**

These versions remain candidates; no new public release assets are announced here. Bounded file-stage production and review are complete: 14 material targets, 126 required 2K maps with independent full-pixel disk checks/reconstruction, and 42 actually viewed final images (30 dual-engine/relighting plus 12 local supplements). DeepSeek performed creation, correction, export, reconstruction and finite image review with maintainer assistance in wood mixing, oil layers and glass-thickness diagnosis; this was not an unassisted blind test or fully unattended workflow. Softer 2K microdetail, Eevee glass approximations and conditional pair findings remain.

Extracted build-a passed 15 technical stages: 73 host cases with two optional Pillow skips, selected standalone procedural cases and independent reopen, a public 128×128/nine-map surface round trip with full-pixel maximum error 0, and isolated installation/legacy/surface/reopen checks of the 25-member material extension. Final document changes require repacking and final verification; later archives are not prequalified. Private cases are not bundled; repository tests create their own fixtures. See [scope and reproducible commands](docs/VALIDATION.md).

**[中文使用说明](README.zh-CN.md)** · [Getting started](docs/GETTING_STARTED.md) · [Material workflow](docs/MATERIAL_WORKFLOW.md) · [CLI reference](docs/CLI_REFERENCE.md) · [Compatibility](docs/COMPATIBILITY.md)

[Companion agent skills](skills/README.md): CLI operation, work methodology, material authoring and asset management. The bundle version is recorded in `skills/manifest.json`; see [skills verification](docs/SKILLS.md).

## What you get

- CLI adapters for scene construction, mesh/UV/node work, materials, rigs/animation, simulation, rendering, media, tracking, format exchange, dependencies, and asset workflows. Each adapter has a bounded contract; this is not every Blender operation.
- Immutable job requests, explicit source/resource hashes, cancellation, recoverable pipelines, and separately reviewed file transactions.
- Material Workflow: static PBR layers, managed node graphs, templates, explicit object/slot/face assignments, Cycles/Eevee diagnostic views, contact sheets, batch checkpoints, and bounded parameter studies.
- `material.procedural-run` and `material.procedural-export`: protected schema-2.0 procedural sources, editable author files, PBR channel baking, disk-only target-material reconstruction and matched diagnostic views. Arbitrary legacy BSDF stacks are not promised equivalent PBR exports.
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

The Material Workflow extension is maintained in its [separate repository](https://github.com/tsist/blender-material-workflow). Install its matching archive through Blender **Preferences → Extensions → Install from Disk** and point its backend project path to this source tree. The historical 0.7.0 extension remains in the [v0.54.1 release](https://github.com/tsist/blenderctl/releases/tag/v0.54.1). Blender and Python paths must point to your installed runtime. See [plugin workflow](docs/MATERIAL_WORKFLOW.md) and [new procedural operations](docs/PROCEDURAL_MATERIALS.md).

## Verify and build

```powershell
python -m unittest discover -s tests -p "test_*.py" -v
python tests/run_blender_smoke.py --blender $env:BLENDER_PATH --output ./test-output/blender
python scripts/build_release.py --output ./dist
```

The integration test creates its own cube, UVs and texture, renders with CPU, checks source preservation, reopens the candidate, and tests extension registration. It does not touch an open Blender session. CI verifies host contracts and reproducible packaging. [Release verification](docs/RELEASE_VERIFICATION.md) separates those results from the local Blender run and historical adapter validation.

## Boundaries

CLI inputs are **saved disk snapshots**. Background jobs cannot detect unsaved GUI edits. Procedural materials require existing UVs and support constants, registered procedures and declared static image resources. Automatic general-purpose UV repair, UDIM/animated textures, automatic aesthetic selection and DLSS are not included. Script execution requires explicit trust; the Windows AppContainer route is experimental and has no fallback. Preserve successful job directories when candidates refer to their textures, caches or receipts.

Blender, third-party binaries, private assets and user configuration are not distributed. See [third-party notices](THIRD_PARTY_NOTICES.md), [security policy](SECURITY.md), and [contribution guide](CONTRIBUTING.md).
