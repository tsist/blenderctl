# Changelog

## CLI 0.55.1 / Hard Surface Workbench integration 0.2.0 — Linux branch

- Dispatch explicit hard-surface work units to a separately reviewed 0.2.0 source checkout selected by `BLENDERCTL_HARDSURFACE_ROOT`; no automatic download, install or in-process plugin import.
- Validate checkout paths, manifest/package versions and exact job-store ownership before dispatch. Preserve legacy commands and isolate child Python import paths.
- Add source-only dispatch regressions and sibling-checkout setup guidance. Material Workflow remains 0.7.1; existing native adapter support is unchanged.

## CLI 0.55.0 / Material Workflow 0.7.1 — Linux branch

- Add fail-closed Linux read guards with kernel lease and namespace evidence; keep Windows deny-write/delete implementation and unsupported transaction boundaries.
- Add deterministic bundled Linux Python discovery, an explicit GUI interpreter override, and portable shell entry point.
- Expose platform limitations in doctor, and record terminal results for rejected pipeline locks.
- Add explicit SHA-verified shared-storage snapshot import and complete successful-job archival tools.
- Keep Blender 5.2.1-specific adapter gates, Windows AppContainer and hard-memory pipeline requirements unchanged. This branch does not claim every Blender adapter is portable.

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
