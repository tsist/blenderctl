# Changelog

## 0.54.2 — 2026-10-06

- Split Material Workflow and new material companion releases into tsist/blender-material-workflow. Pin extension 0.7.1 as a Git submodule without a second editable core.
- Preserve material commands and recoverable job contracts. Complete source ZIPs contain the pinned dependency; recursive Git checkout is required.
- CLI releases no longer publish extension ZIPs; previous releases and compatibility skill snapshots remain intact.

## Companion skills 0.1.0 — 2026-10-04

- Publish public adaptations of CLI, work methodology, material authoring and asset management skills with references, scripts, templates and agent metadata.
- Replace private paths, fixed tasks, Wiki and asset evidence with explicit root discovery and scoped guidance. Preserve six candidate methods and PBR source/validation rules without inheriting private permissions.
- Add a conflict-safe installer, standalone ZIP, metadata/link checks and actual helper tests. Image decoding uses optional Pillow; no dependencies or host configuration are installed automatically.
- Keep CLI 0.54.1 and extension 0.7.0 runtime code unchanged; original release artifacts remain immutable.

## CLI 0.54.1 / Material Workflow 0.7.0 — 2026-10-04

First public source release of the existing Blender automation and material toolchain.

- Add a portable Windows launcher with `BLENDERCTL_PYTHON`, and CLI runtime discovery via `BLENDER_PATH`, the compatible local portable directory, or `blender` on PATH.
- Add `BLENDERCTL_JOBS_DIR` and `BLENDERCTL_TRANSACTIONS_DIR`; explicit CLI options take precedence.
- Publish the production modules and complete JSON Schema set, bilingual installation guidance, contribution/security policies, GPL-3.0-or-later license and dependency notices.
- Add generated-fixture integration tests, host CI, reproducible source/extension ZIP packaging and SHA-256 checksums.

Material Workflow 0.7.0 retains the existing bounded material study, texture diagnostics, batch preview, template, GUI snapshot and recovery contracts. CLI 0.54.0 was the preceding local version; 0.54.1 changes distribution and runtime discovery, without widening native Blender adapter compatibility.

Historical feature milestones: CLI 0.47 generalized disk-state workflows; 0.51 GUI batch handoff; 0.52 material interface discovery and compact results; 0.53 v2 IOR/coat controls; 0.54 material studies and bounded job waits. Private historical audit data is not part of this release.
