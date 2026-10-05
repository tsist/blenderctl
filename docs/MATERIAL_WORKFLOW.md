# Material Workflow integration

The extension and current material companion skill releases moved to [blender-material-workflow](https://github.com/tsist/blender-material-workflow). Read its [current workflow](https://github.com/tsist/blender-material-workflow/blob/main/docs/MATERIAL_WORKFLOW.md). This CLI keeps backend commands and consumes its pinned extension submodule.

Clone/update with recursive submodules, or unpack this repository's complete source release asset. Automatic GitHub source archives omit the dependency. For historical 0.7.0 behavior, see the previous Git tag.

# Backend contract (compatible with 0.7.1)

The extension and CLI share a managed material core. It assembles static layered PBR materials from explicitly identified objects, slots, UV layers and resources. It preserves unrelated edits and refuses overlapping or structural conflicts.

## Install the extension

Download `material-workflow-0.7.1.zip` from the separate Material Workflow Releases. In Blender use **Preferences → Extensions → Install from Disk**, select the ZIP, and enable Material Workflow. Open the View3D sidebar with **N** and select **Material Workflow**. Blender 5.2.0 is the manifest minimum; the actual verified runtime is 5.2.1 LTS. The ZIP is a Blender extension with its manifest at the archive root.

For background handoff, set the backend project directory to the unpacked CLI source root. The Windows backend discovers the selected Blender installation's bundled Python. If automatic discovery is ambiguous, the Python bridge API accepts an explicit Python binary; visible panel behavior on other operating systems remains unverified. The plugin does not bundle or download Blender or the CLI.

## Drafts and snapshots

Select the target mesh with valid UVs. Layer editing operates on a draft; apply explicitly. The bottom layer remains enabled, opaque and without a mask. Template saving uses applied managed state, not an unapplied draft. Shared meshes/materials and animated/linked managed structures have deliberate restrictions.

Batch assignments identify object, material slot, optional face indices and dependency task IDs. Explicitly commit edits to assignment targets and view settings. Loading an assignment into the single-material editor does not silently write subsequent edits back; use its explicit draft writeback action.

Before a GUI background submission, choose a new persistent snapshot directory. The handoff captures unsaved scene edits and supported static dependencies in a separate snapshot; the current source is not saved or replaced. Read the handoff receipt and refresh to inspect completion. Retry the original snapshot only after confirming ownership/acceptance; new scene edits need a new snapshot. Keep the complete handoff and job roots.

## CLI discovery and execution

```powershell
python tools/blenderctl/cli.py material describe
python tools/blenderctl/cli.py material describe --operation run --schema
python tools/blenderctl/cli.py material describe --operation study --section study
python tools/blenderctl/cli.py --jobs-dir ./runtime/material-jobs --compact request <absolute-request.json>
```

A request has `schema_version: "1.0"`, a dotted command and `params`. Material manifests use schema 1.0 / `pbr_layers_v1`, or 1.1 / `pbr_layers_v2`. The public request schemas distinguish:

| Command | Workflow |
| --- | --- |
| `material.run` | Assemble/update one material, save and reopen, render selected views and a contact sheet |
| `material.batch` | Explicit object/slot/face assignments, independent failure isolation, checkpoints and verified resume |
| `material.study` | At most 8 parameter candidates, 6 views each and 32 total views; input semantic warnings, effective parameter differences and comparison sheets |
| `material.template-save` | Export managed material template, separate bindings and content identities |

Run the [generated fixture test](../tests/run_blender_smoke.py) to obtain a complete original request, texture, candidate, image and report under a new local output directory. No proprietary texture or `.blend` is needed. The fixture is a numerical smoke test rather than a photorealism example.

v2 supports 1–8 layers; static base color, roughness, metalness, normal, height/Bump, opacity, emission and masks. `ior`, `coat_weight`, `coat_roughness`, `coat_ior` are constants; an enabled coat shares that layer's final normal/bump. Texture-connected channels may override fallback constants; diagnostics explain unused values. White isolation changes the named layer's color, preserving other layers.

Cycles supports CPU or exact detected CUDA/OPTIX device IDs. Eevee uses `BLENDER_EEVEE` with `{"backend":"GRAPHICS"}`; denoise and GPU IDs are rejected. Controlled static node groups support a bounded node matrix; Shader to RGB is Eevee-only. Previews use isolated scenes and preserve the source render engine.

## Limits and retention

Existing valid UVs and static FILE images are required. General automatic UV, UDIM, sequences, animated textures, arbitrary external node libraries, volume/true displacement and automatic visual selection are not included. Preserved non-target materials also need supported preview nodes. Rendering success is not artistic approval.

`--compact` retains full disk reports while returning small summaries with file/SHA/byte references. Recover only against unchanged source, implementation, task scope and verified checkpoint dependencies. A new public distribution has a different implementation hash from a private build; do not transplant old recovery records. Save independent snapshots instead.
