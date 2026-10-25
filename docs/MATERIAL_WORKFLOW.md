# Material Workflow 0.8.0 candidate

The extension and CLI share a managed material core. It assembles static layered PBR materials from explicitly identified objects, slots, UV layers and resources. It preserves unrelated edits and refuses overlapping or structural conflicts.

## Install the extension

This local0.8.0 candidate has no release asset promised here. Review and validate the source/package before installing it. In Blender use **Preferences → Extensions → Install from Disk**, select the ZIP, and enable Material Workflow. Open the View3D sidebar with **N** and select **Material Workflow**. Blender 5.2.0 is the manifest minimum; the actual verified runtime is 5.2.1 LTS. The ZIP is a Blender extension with its manifest at the archive root.

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
python tools/blenderctl/cli.py --blender $env:BLENDER_PATH --jobs-dir ./runtime/material-jobs --compact request <absolute-request.json>
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

Existing valid UVs are required; static FILE images are required for image inputs. General automatic UV, UDIM, sequences, animated textures, arbitrary external node libraries, volume/true displacement and automatic visual selection are not included. Preserved non-target materials also need supported preview nodes. Rendering success is not artistic approval.

`--compact` retains full disk reports while returning small summaries with file/SHA/byte references. Recover only against unchanged source, implementation, task scope and verified checkpoint dependencies. A new public distribution has a different implementation hash from a private build; do not transplant old recovery records. Save independent snapshots instead.

## Procedural/hybrid candidate: CLI0.64.0 / extension0.8.0

Manifest2.0 / `procedural_pbr_v1` is a separate contract beside legacy1.0/1.1 image-layer manifests. Fifteen typed signals are constant, image, coordinate, warp, noise, wood, voronoi, edge, cavity, height_band, normal_direction, math, remap, ramp and mix. Coordinate/warp produce VECTOR; strict VALUE/COLOR/VECTOR references and float32 storage checks apply. Mapping scale differs from texture scale; per-signal Mapping still follows an explicit vector input.

`make_recipe` offers painted_metal, wood and clear/tinted/frosted glass starting points, not finished arbitrary-prop presets. Wood takes keyword-only `grain_axis` and positive finite `timber_scale=(1,1,1)` in local axes before shared axis rotation. Callers choose sampling units; transforms/physical units are not inferred. Metal `metal_region` is lower/edges/seam, explicitly caller-selected. Zero weathering preserves a clean control. See the [full signal method](../skills/blender-material-authoring/references/pbr/procedural-signals.md) and [portable manifest/CLI examples](../skills/blender-material-authoring/references/pbr/procedural-pbr.md).

Discover actual request schemas with `material describe --operation procedural-run --schema` and `material describe --operation procedural-export --schema`. `material.procedural-run` builds/saves/reopens/renders a new editable author, optionally exports; `material.procedural-export` uses an existing managed author. The outer request stays1.0. Source/resources use explicit file/SHA, context/target/UV/slot bindings. Inspect new author renders and iterate before final export. Use explicit long-job timeout and global async when needed, preserve actual job IDs, and read terminal reports; no public procedural resume field exists. Retain partial diagnostics, and export a verified author in a new job instead of pretending partial is complete.

Layers compose base_color, roughness, metallic, signed height, alpha, transmission, ior and coat properties. Export supports base_color/roughness/metallic/normal/height/alpha/transmission/ior/coat_weight/coat_roughness; required nonconstant channels may be added and constants must be declared. BaseColor is sRGB; data is Non-Color; IOR encodes(ior-1)/3; Height is signed32-bit EXR. Normal is real Cycles tangent/+Y baking including total Height Bump; disk-map reconstruction must not add that Bump again. No AO export key or volume absorption is claimed.

Protect shared data, non-target slots/materials/UVs, source identity and managed graph edits. Target UVs must lie in one0..1 tile without positive-area overlap; geometry helpers currently require this across the whole target object. Geometry changes require new helper maps. Scene units/local coordinates, normals, thickness, modifiers and region assumptions must be checked. The procedural GUI's live controls read back actual values; changing new-preset widgets does not silently reset existing author parameters. Old static draft/template/snapshot operations retain their own contracts.

Maintainer-workspace evidence covers14 parts,126 necessary2K maps and36 whole-object author/reconstruction × Cycles/Eevee × overall/closeup/relighting images, limited to those models and conditions. Private assets/audits are not distributed. Cycles macrostructure retention does not prove lossless fine detail or Eevee glass equivalence. **Public candidate revalidation and independent DeepSeek reuse remain in progress.** Keep technical, visual, installation and release conclusions separate; source presence and completed jobs do not prefill acceptance. Default2K remains a target setting, not an arbitrary UV/island quality guarantee.
