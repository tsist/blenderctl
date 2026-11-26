# Blender 5.3 production operations

`production.run` uses the same add-on code as the four companion extensions. The qualified host is Windows Blender 5.3.0, build `02f0899d78e3`.

Download `blender53-production-1.0.0.zip` and `SHA256SUMS` from the [blenderctl release](https://github.com/tsist/blenderctl/releases/tag/v0.64.0). Compare the SHA-256 of the ZIP to the published sum, then extract it into the CLI checkout. Its manifest must be at `runtime/blender53-production/1.0.0/manifest.json`. The runtime also verifies every file in the selected add-on before running it. To enable the add-ons in Blender's interface, install their individual archives from the companion repositories.

| Domain | Operations and companion documentation |
| --- | --- |
| `animation` | Correction layers and weights, explicit Action/Slot edits including pose bones, world-space transfer, Euler and quaternion conversion. [Animation Curves Workflow](https://github.com/tsist/blender-animation-curves-workflow) |
| `compositing` | Inspect, insert, reorder, edit, bypass or remove native scene effects. Reuse a local effect or build one from supported nodes with exposed controls. [Compositing Workflow](https://github.com/tsist/blender-compositing-workflow) |
| `lighting` | Solve subject exposure, protected regions, key-to-fill ratio and luminance targets together. New area lights and receiver-link changes require explicit permission in the request. [Lighting Workflow](https://github.com/tsist/blender-lighting-workflow) |
| `video` | Split, remove and insert on a managed VSE timeline by frame, shot-relative position, marker or an earlier result. Choose group or whole-batch rollback. [Video Editing Workflow](https://github.com/tsist/blender-video-editing-workflow) |

Save a UTF-8 request with the absolute path and lowercase SHA-256 of a saved Blender file. For example, inspect the active scene's native effects with the following request.

```json
{
  "schema_version": "1.0",
  "command": "production.run",
  "params": {
    "file": "<absolute path to source.blend>",
    "expected_sha256": "<64 lowercase hexadecimal digits>",
    "domain": "compositing",
    "release": "1.0.0",
    "request": {"operation": "inspect"}
  }
}
```

```powershell
$blender = '<absolute path to Blender 5.3.0 blender.exe>'
$env:BLENDERCTL_PYTHON = Join-Path (Split-Path -Parent $blender) '5.3\python\bin\python.exe'
./blenderctl.ps1 --blender $blender request ./production-request.json
```

Use a regular Python executable for `pipeline run`. The bundled Blender Python above avoids Windows Store app aliases, which the input guards reject.

Supply `context` with `scene` and `view_layer` to select a specific scene or layer. Supply external inputs as `resources` entries with `file` and `expected_sha256`. For MOVIE strips, place the qualified Windows FFprobe executable at `tools/ffmpeg/ffmpeg-9.0.2-essentials_build/bin/ffprobe.exe` under the CLI checkout. Its SHA-256 is `f0d36ecbbdd3bcfac3efa078c96c7271c2e68b3810595552ac3b7f17e9a65c52`. Declare its absolute path and SHA-256 in both `request.media_probe` and `params.resources`. Media must have a compatible constant frame rate, zero start time and square pixels. See the video add-on's batch-edit guide for the exact request fields.

A successful change produces `production-candidate.blend` and `production-report.json` in a new job directory. A read-only inspection returns a report. Review `result` and `reopen` for operation outcomes and the worker's saved-file reopen checks. For an independent check, open the candidate in a fresh process. Source or resource fingerprint failures are reported in the job result before editing. The CLI holds the source and declared inputs read-only. Submit independent operations as separate `production.run` steps in a `pipeline run` manifest. Add dependencies and candidate bindings when steps edit successive versions of the same project.

For a stage following a successful `first` stage, set `depends_on` to `["first"]` and add this entry to `bindings`:

```json
{"step":"first","artifact":"/candidate/file","target":"/file","sha_target":"/expected_sha256"}
```

Pipeline preflight fingerprints the Blender runtime. Allow time for that step on large or slow disks, for example `./blenderctl.ps1 --blender $blender --timeout 600 pipeline run --manifest ./pipeline.json`.
