# CLI reference

The authoritative inputs are [public schemas](cli/schemas/) and the exact subcommand's `--help`. `material describe` offers focused discovery without running Blender. Request envelopes use `schema_version: "1.0"`, `command` and `params`.

| Group | Scope |
| --- | --- |
| `doctor`, `capabilities` | Runtime, registered operations and orchestration dispositions |
| `inspect`, `query`, `diff`, `validate`, `dependency` | Disk structure, comparisons, dependency identities and typed relinking |
| `scene`, `model`, `node` | Explicit scene/geometry/UV/node candidates and diagnostics |
| `material`, `texture` | Layered material workflows, isolated previews and texture baking |
| `rig`, `animation` | Local skeletons, weights, retargeting, sampled actions and packaging |
| `simulation` | Bounded physics, hair, volume and cache adapters |
| `tracking` | Clip preparation, bounded native tracking and camera reconstruction |
| `render`, `media` | Devices, frame rendering, VSE and media export |
| `exchange` | Formats, analysis, export/import/conversion and native time/cache adapters |
| `project`, `asset`, `identity` | Copies, links/overrides, packages, metadata and explicit publication plans |
| `extension`, `sculpt` | Exact script trust/review, OS isolation and native sculpt replay boundaries |
| `pipeline` | Bounded DAG planning/execution and verified new resume attempts |
| `job`, `transaction`, `resource`, `cache` | Owned processes, explicit file transactions, resource domains and cache plans |

Some domain adapters are selected through manifest operations rather than separate top-level commands. Registration does not establish functional or artistic verification. `capabilities.data.execution_contract` distinguishes allowed DAG entries from standalone workflows, transactions and review checkpoints.

```powershell
python tools/blenderctl/cli.py --help
python tools/blenderctl/cli.py model prepare --help
python tools/blenderctl/cli.py material batch --help
python tools/blenderctl/cli.py --blender <absolute-binary> --jobs-dir <absolute-root> --timeout 180 request <absolute-request.json>
```

Unless requesting help/version, stdout is a JSON envelope with `ok`, `command`, `job_id`, `data` or `error`, and artifacts. Check both process exit code and `ok`. Async acceptance is not a final result. Terminal job results distinguish completed success from failure, timeout and cancellation.

| Exit code | Meaning |
| --- | --- |
| 2 | Invalid request |
| 3 | Missing input |
| 4 | Conflict |
| 5 | Worker failure |
| 6 | Timeout |
| 7 | Cancelled |
| 8 | Verification failed |
| 9 | I/O error |
| 10 | Unsupported |
| 11 | Resource limit |

Saved disk inputs are `SAVED_DISK_V1`; unsaved GUI state is not observed. Explicit profiles, receipts and resource identities bind specific contents. The existence of a file, device or registered operator cannot stand in for a complete domain result.
