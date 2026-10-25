# Procedural PBR materials — CLI 0.64.0

`material.procedural-run` creates a protected editable author material from a schema-2.0 manifest. Optional export settings bake its declared surface signals and build a new target material from the final disk maps and explicit parameters. `material.procedural-export` performs that export on an existing managed procedural material.

This backend is separate from legacy image-layer v1/v2 BSDF assembly. A single-Principled surface-channel recipe can have explicit export semantics; arbitrary shader stacks, coat optics or effects cannot automatically become an equivalent set of ordinary PBR textures.

## Discover before submitting

```powershell
python tools/blenderctl/cli.py --version
python tools/blenderctl/cli.py material describe --operation procedural-run --schema
python tools/blenderctl/cli.py material describe --operation procedural-run --section manifest
python tools/blenderctl/cli.py material describe --operation procedural-run --section signals
python tools/blenderctl/cli.py material describe --operation procedural-export --section export
```

These are host-only queries and create no Blender job. Static contracts are in `docs/cli/schemas/material-procedural-*.schema.json`. The shared addon contract owns source/signal/export semantics; GUI and CLI use that same contract.

The optional `surface` property is included in the manifest schema. In this frozen CLI 0.64.0 candidate, read it through `--section manifest` or `--schema`; `--section surface` is not accepted by the CLI parser.

## Inputs and sources

Use a saved local `.blend`, its SHA-256, explicit Scene/View Layer/frame, one local mesh object/material slot and an existing named UV map. Declare every external resource by absolute file path and SHA; `resources: []` is valid only when there are no external inputs. Source geometry, topology, UVs and non-target material data are protected. Geometry problems return to the modeling workflow.

Choose constants, registered procedural signals, declared static images or a mixture for the material requirement. A manifest cannot contain arbitrary executable Python. Signal references form an acyclic graph with explicit scalar/color roles, coordinate space, direction, scale and seed. Noise changes distribution and detail; it does not by itself explain where edges wear or dirt accumulates. Wood grain, part direction/end grain, metal/paint/rust separation and geometry-dependent masks need their own visual checks.

Authoring is bounded to simple static, single-object/single-tile props. Automatic UV/semantic partitioning, animated texture/UDIM workflows, NPR style templates, volumes, liquids and complex caustics are outside this increment.

## Run and export

```powershell
$inputBlend = (Resolve-Path -LiteralPath './input.blend').Path
$sourceSha = (Get-FileHash -LiteralPath $inputBlend -Algorithm SHA256).Hash.ToLowerInvariant()
python tools/blenderctl/cli.py --blender $env:BLENDER_PATH --jobs-dir ./runtime/material-jobs --timeout 3600 --compact --async material procedural-run $inputBlend --expected-sha256 $sourceSha --resources ./resources.json --manifest ./material.json --export ./export.json
```

Replace input paths with the actual absolute source/resource paths required by the contracts. Global options precede `material`. `--export` is optional on `procedural-run`; the JSON request envelope also allows `export: null`. It is required for the standalone export operation:

```powershell
$authorBlend = (Resolve-Path -LiteralPath './author.blend').Path
$authorSha = (Get-FileHash -LiteralPath $authorBlend -Algorithm SHA256).Hash.ToLowerInvariant()
python tools/blenderctl/cli.py --blender $env:BLENDER_PATH --jobs-dir ./runtime/material-jobs --timeout 3600 --compact --async material procedural-export $authorBlend --expected-sha256 $authorSha --resources ./resources.json --context ./context.json --target ./target.json --export ./export.json
```

An export settings example:

```json
{"channels":["base_color","roughness","metallic","normal"],"size":2048,"margin":16,"samples":16,"bit_depth":"16"}
```

Size defaults to 2048 and is currently bounded to 64–2048. Additional required nonconstant channels may be included to avoid dropping a signal. BaseColor is an sRGB color map; roughness/metallic/normal and other data channels use their declared data encoding. IOR and signed height require the report's decode/unit metadata. The normal map uses the named UV, tangent OpenGL +Y convention and records height inclusion; do not add the same bump twice.

All output paths belong to the owned job directory. The public contract accepts no arbitrary overwrite destination. Keep `author.blend`, `reconstructed.blend`, channel files, source/resource manifests, report and preview dependencies together. Reconstruction isolates the target material graph from authoring procedures; preserved non-target/author data can still be present in the saved file.

Read the returned job ID once, then use bounded `--compact job wait <id> --wait-seconds 30` with the same jobs directory. Inspect `ok`, error, terminal state and the full report reference. Partial output or an existing file is not success; do not resubmit a running job just to ask for progress.

## Glass and engines

The existing CLI 0.64.0 / extension 0.8.0 candidate supports an optional top-level manifest property:

```json
{"surface":{"thickness_mode":"SLAB","thickness":0.13}}
```

`thickness_mode` is required when `surface` is present and accepts `SPHERE` or `SLAB`. Optional `thickness` is an object-space scalar in the inclusive range 0–1000, represented as finite float32; unrepresentable values are rejected. The example value is syntax only: choose the mode and value from the object's actual geometry, local scale and intended approximation.

Omitting `surface` preserves existing recipes and their managed snapshot behavior. Omitting `thickness` leaves Material Output Thickness unlinked for AUTO estimation. A connected explicit value of 0 is different: it disables the thickness approximation. Explicit values use the editable concentrated control `Surface/thickness` and are read back into the manifest and saved templates. Apply changes through the managed recipe; manual mode or link edits are rejected as managed conflicts.

The bake report records `thickness={mode,space,value}`, with `AUTO` / `OBJECT` / null or `EXPLICIT` / `OBJECT` / the declared scalar, including 0. Disk reconstruction preserves that distinction, the link and material thickness mode. Old reports without the thickness field retain unlinked AUTO reconstruction. Unknown or dynamic Thickness sources are rejected rather than exported as a guessed scalar. Thickness is a declared parameter, not a separate baked image channel.

Alpha is not Transmission. Glass needs the appropriate Transmission, IOR, roughness, wall thickness and inward/outward normals; attach these conventions to exported maps. Surface tint is distinct from volume absorption, which is outside this increment. Cycles is the visual baseline; Eevee thickness/refraction and layer behavior have separate limits. The owned Eevee preview studio enables ray tracing explicitly and records its settings; it does not change the user's source scene settings.

The reconstruction is reopened and rendered in Blender. No Unity/Unreal import, universal BSDF equivalence, calibrated physical glass or final artistic acceptance is implied by a technical pass. See [validation scope](VALIDATION.md).
