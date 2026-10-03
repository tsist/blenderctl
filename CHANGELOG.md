# Changelog

## CLI 0.54.1 / Material Workflow 0.7.0 — 2026-10-04

First public source release of the existing Blender automation and material toolchain.

- Add a portable Windows launcher with `BLENDERCTL_PYTHON`, and CLI runtime discovery via `BLENDER_PATH`, the compatible local portable directory, or `blender` on PATH.
- Add `BLENDERCTL_JOBS_DIR` and `BLENDERCTL_TRANSACTIONS_DIR`; explicit CLI options take precedence.
- Publish the production modules and complete JSON Schema set, bilingual installation guidance, contribution/security policies, GPL-3.0-or-later license and dependency notices.
- Add generated-fixture integration tests, host CI, reproducible source/extension ZIP packaging and SHA-256 checksums.

Material Workflow 0.7.0 retains the existing bounded material study, texture diagnostics, batch preview, template, GUI snapshot and recovery contracts. CLI 0.54.0 was the preceding local version; 0.54.1 changes distribution and runtime discovery, without widening native Blender adapter compatibility.

Historical feature milestones: CLI 0.47 generalized disk-state workflows; 0.51 GUI batch handoff; 0.52 material interface discovery and compact results; 0.53 v2 IOR/coat controls; 0.54 material studies and bounded job waits. Private historical audit data is not part of this release.
