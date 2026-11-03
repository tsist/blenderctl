---
name: blender-rigging
description: Inspect and operate Blender bones, controls, binding weights, expression Shape Keys and hair-bone physics with the rigging development components and a managed Blender CLI. Fixed cyclic poses are inspection aids, not an animation production workflow.
metadata:
  version: "0.1.0-dev1"
---

# Blender rigging

Start with the user's actual rigging outcome and the repository's https://github.com/tsist/blender-rigging-workflow/blob/main/docs/SETUP.md. This is a development baseline: basic feature areas exist, detailed quality and the unified workflow remain unfinished. Do not infer supported input from a similarly named module.

Use the base add-on for its existing panel. Later components require explicit development setup and source/profile/output inputs. Call `hrig_paths.configure()` from the user's checkout before importing them; do not assume a workstation path, character, active object, mode or selection.

Read only the needed initializer and contract. Existing local smoothing, mirror, normalization, influence limits and neighbor-based filling are not proof of whole-character automatic skinning. Character-specific face adapters are not universal face generators. Hair physics belongs to rigging; visible hair-mesh repair and performance-animation authoring are separate.

Use the installed public blenderctl's actual help/schema for managed jobs. A new universal rigging CLI command is not provided. Do not bypass source guards, mutate receipts or claim unsaved GUI state is represented by a saved file.

Inspect actual bone and mesh response. For deformation use a few suitable large-angle cycles such as splits, elbow bends and squats, including intermediate frames. Design fixed expression tests for each character. Observe hair-bone physics and relevant collisions for the actual inputs. Separate successful operation, visible response and detailed quality.

Preserve source and non-target data; use a work copy and meaningful save/reopen/recovery checks. Reuse unaffected evidence. Historical numerical tolerances, test counts and performance values are not universal requirements. If experiments stop explaining the actual problem, change the approach or stop that route.

For ongoing development read https://github.com/tsist/blender-rigging-workflow/blob/main/docs/DEVELOPMENT.md: the next integration task is planned. On formal start, use the available grilling skill to collect requirements before implementation. Future third-party candidates are not already implemented or licensed for redistribution merely because they are listed.
