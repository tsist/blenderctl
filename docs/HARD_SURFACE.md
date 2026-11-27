# Hard Surface Workflow 0.3.7 from blenderctl

The public CLI uses the same `surface_sweep` implementation as the Blender panel. This route is qualified on Windows Blender 5.3.0 build `02f0899d78e3`.

## Install the pinned runtime

Build the extension ZIP from the [Hard Surface Workflow v0.3.7 sources](https://github.com/tsist/blender-hard-surface-workflow/releases/tag/v0.3.7) using its separately supplied dependency bundle. From this CLI checkout, run:

```powershell
python scripts/install_hard_surface.py '<path to the locally built 0.3.7 extension ZIP>'
$blender = '<absolute path to Blender 5.3.0 blender.exe>'
$env:BLENDERCTL_PYTHON = Join-Path (Split-Path -Parent $blender) '5.3\python\bin\python.exe'
```

The installer verifies all ZIP members against the [published package inventory](hard-surface-package.json) and writes to `runtime/blender53-production/0.3.7/`. ZIP member SHA-256 values, including dependencies, are fixed individually, so ZIP metadata can vary. A different installed bundle is rejected. The existing four companion domains continue using the separate production bundle documented in [Production operations](PRODUCTION.md).

## Prepare a request

Use `production.run` with `domain: "hard-surface"`, the absolute file path of a saved Blender file, and its lowercase SHA-256. Release `0.3.7` is selected by default for this domain. Use `context` with `scene` and `view_layer` when the intended mounting side is in a particular scene or view layer. Copy this JSON to a new request file and replace the source path, digest, object name and station coordinates with actual values.

```json
{
  "schema_version": "1.0",
  "command": "production.run",
  "params": {
    "file": "<absolute path to source.blend>",
    "expected_sha256": "<64 lowercase hexadecimal digits>",
    "domain": "hard-surface",
    "release": "0.3.7",
    "request": {
      "operation": "preview",
      "object": "ReferenceShell",
      "params": {
        "name": "Swept profile",
        "stations": [
          {"name":"Inlet","position_mm":[-20,0,15],"locked":true,"corner":"STRAIGHT"},
          {"name":"Outlet","position_mm":[20,0,15],"locked":true,"corner":"STRAIGHT"}
        ],
        "ray_direction": [0,0,-1],
        "outward_normal": [0,0,1],
        "max_distance_mm": 30,
        "max_jump_mm": 4,
        "max_normal_angle_deg": 45,
        "profile": {"type":"RECTANGLE","width_mm":4,"height_mm":2},
        "gap_mm": 0.1,
        "min_clearance_mm": 0.05,
        "dimensional_tolerance_mm": 0.1,
        "gap_tolerance_mm": 0.15
      }
    }
  }
}
```

```powershell
./blenderctl.ps1 --blender $blender request ./sweep-preview.json
```

- **Describe.** Set `request` to `{"operation":"describe"}` to read the add-on's parameter catalog through the saved-job route.
- **Preview and execute.** Examine `data.result` for projected hits, section dimensions, clearance, protection and `preview_signature`. Change `request.operation` to `execute` and copy that signature to `request.params.expected_preview_signature`. Updates also use the current managed `expected_signature`.
- **Resume saved controls.** Submit `{"operation":"inspect-sweep","object":"Swept profile","station_positions_mm":{"Editable station":[0,11,15]}}` as the domain `request`. The object may also name a saved station control, and the position override is optional. On success, replace only `params.request` with `data.result.commit_request` and submit again against the same saved source and SHA. The override can move one unlocked station. Recalculate the source SHA and run a new preview whenever the source or intended recipe changes.

The millimetre recipe uses a scene with `1 BU = 1 m`. Keep endpoints and local-edit boundaries locked. Set `ROUND` with `radius_mm` or `MITER` with `miter_limit_deg` as needed. Replace the rectangular profile with `{"type":"PIPE","outer_diameter_mm":6,"wall_mm":0.8,"sides":32}` for an open-bore pipe. Check the mounting side and minimum clearance individually.

The report is `production-report.json` in the job directory. A committed change produces `production-candidate.blend` plus `data.sweep_reopened`, which confirms that the reopened geometry and controls still satisfy the same signed recipe. Preview is read-only and has no candidate. Inspect the actual mesh and fitted interval in a fresh Blender process before further modeling.
