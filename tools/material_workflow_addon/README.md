# Material Workflow 0.7.0

Blender extension for explicit static layered PBR materials, templates, managed incremental edits, batch assignments and persistent GUI snapshots handed to blenderctl.

Install the release ZIP with Preferences → Extensions → Install from Disk. Enable the extension and open View3D → Sidebar → Material Workflow. Verified baseline: Windows / Blender 5.2.1 LTS; manifest minimum 5.2.0. The ZIP contains the extension and GPL license; Blender and CLI are installed separately.

For background handoff, the project root must be an unpacked blenderctl source checkout containing tools/blenderctl and docs/cli/schemas. Editing uses explicit draft commits. Snapshot submission creates a separate persistent copy and never silently replaces the open scene. Preserve job and handoff files for recovery.

Existing UVs and static image files are required. General automatic UV, UDIM/animation, automatic aesthetic selection and DLSS are not included. Cycles/Eevee have separate device/node contracts. Rendering success does not establish artistic acceptance.

Full installation, CLI schemas and workflow documentation: https://github.com/tsist/blenderctl/blob/main/docs/MATERIAL_WORKFLOW.md

SPDX-License-Identifier: GPL-3.0-or-later. See LICENSE.
