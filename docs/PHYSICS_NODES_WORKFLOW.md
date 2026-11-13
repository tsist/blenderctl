# Physics Nodes Workflow Companion CLI & Add-on

Companion standalone repository: [tsist/blender-physics-workflow](https://github.com/tsist/blender-physics-workflow) (GPL-3.0-or-later, v0.1.0).

This integration adds the isolated Geometry Nodes physics workbench add-on (`tools/physics_nodes_workflow_addon/`), the companion CLI (`tools/physics_workflow_cli_v1/`), unit tests (`tests/test_physics_workflow_contract.py`), and the portable skill (`skills/blender-physics-workflow/`) without altering the core `tools/blenderctl/` runtime identity.

## Supported Scope (`BLENDER_PHYSICS_WORKFLOW_CONTRACT_V1`)

- **Baseline**: Windows x64, **Blender 5.2.1 LTS** (`build 9e2066aef7ef`), Python 3.12+.
- **Domains & Presets**:
  - `rigid_scatter`: `rigid_drop_pack`, `rigid_chain_pendulum`
  - `cloth_rope`: `cloth_banner_wind`, `rope_catenary_swing`
  - `fluid_emission`: `fluid_fountain_sdf`, `smoke_plume_volume`
- **Operations**: `build`, `update`, `parameters`, `bake`, `invalidate_cache`, `inspect`, `adopt`, `export`
- **Bridge Modes**: `GN_NATIVE` (default), `BULLET_BRIDGE`, `XPBD_ASSET_BRIDGE`, `MANTAFLOW_BRIDGE`
- **Export Formats**: `NONE`, `STATIC_MESH`, `ALEMBIC`, `OPENVDB`

## Usage

```powershell
# Host-only contract discovery (does not launch Blender)
python -B tools/physics_workflow_cli_v1/cli.py describe

# Run unit tests
python -B -m unittest tests/test_physics_workflow_contract.py -v

# Execute a bounded physics simulation job with Blender 5.2.1
$env:BLENDER_PATH = "<path-to-blender-5.2.1>"
python -B tools/physics_workflow_cli_v1/cli.py run `
  --request path/to/request.json `
  --jobs-dir ./runtime/physics-jobs `
  --timeout-seconds 300
```

CLI subcommands: `describe`, `run`, `sweep`, `batch`.

## Verification & Explicit Limits

- Verified locally on Windows + Blender 5.2.1 LTS across three `GN_NATIVE` production acceptance scenes (`rigid_chain_pendulum`, `cloth_banner_wind`, `fluid_fountain_sdf`) including `DISK` cache baking, cold `.blend` reopen hash consistency, EEVEE multi-frame renders, and synchronous Alembic `.abc` export (`fluid_fountain_sdf`).
- `GN_NATIVE` solvers are modular Geometry Nodes approximations (`PN_*` subgroups + `GeometryNodeSimulationZone` + `PointsToSDFGrid`/`GridToMesh`), not bit-exact replacements for native Bullet, XPBD C++, or Mantaflow FLIP internals.
- Host-only Linux/Windows GitHub Actions CI verifies syntax, contract tests, `cli.py describe`, and deterministic archive builds; it does not run live Blender simulations.
