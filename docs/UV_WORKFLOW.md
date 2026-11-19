# UV Workflow

This branch adds the saved-file UV commands to the public Blender CLI and includes the same UV runtime modules as [UV Workflow 0.8.9](https://github.com/tsist/blender-uv-workflow/tree/feat/uv-workflow-0.8.9). Use `uv describe` for the actual plugin version, request contract and implementation SHA-256 of your checkout.

```powershell
python tools/blenderctl/cli.py uv describe
python tools/blenderctl/cli.py --blender <absolute-binary> --jobs-dir <absolute-jobs-dir> uv inspect --params <inspect.json>
python tools/blenderctl/cli.py --blender <absolute-binary> --jobs-dir <absolute-jobs-dir> uv run --params <edit.json>
python tools/blenderctl/cli.py --blender <absolute-binary> --jobs-dir <absolute-jobs-dir> uv grid-transfer --params <transfer.json>
```

The commands consume saved BLEND data. Use a Blender runtime matching the source file's major/minor version. Pass absolute file paths and SHA-256 values for the inputs and declared resources. A request that specifies the implementation SHA-256 must match this checkout's discovery result. A successful edit writes a job-local candidate, preserves the original render UV and independently reopens the candidate. Review its visual and intended-use results before adoption.

## Merge micro-islands

In an `edit` request select `action.kind = "merge_micro_islands"`. Specify the named candidate and an `ISLANDS` selection that covers every face of one static triangulated mesh. The candidate must be an unadopted, exclusive 2048² layout with no texture consumer. The default area threshold is 64 px². Pins, native seams, locked faces, explicit cut/join constraints and reuse or tiled layouts are refused. Read the merge count before packing separately from islands that pass the area threshold after packing changes their size.

## Integer-grid normal transfer

Supply a source and destination BLEND, source scene, paired objects and UV layers, one patch assignment per saved triangle, and the integer texel translation for each patch. The supported input is a packed `Non-Color`, `CHANNEL_PACKED` RGBA8 tangent-space normal PNG. The adapter checks saved geometry, transforms, corner normals, tangent basis, and the base-level bilinear footprint. It refuses noninteger translations, missing coverage, conflicting destination writes and out-of-bounds samples.

Use the new job's `grid-transfer/normal.png` and `grid-transfer/receipt.json` after success. Bind the verified target UV explicitly in the consuming material. Mipmaps, cross-tile sampling, texture compression and other image semantics need their own verification.
