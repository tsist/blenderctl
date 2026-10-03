# Installation and first render

Install Python 3.12+ and Blender separately. No pip package or network service is required for the host CLI. Download and unpack the source release, retaining the complete `tools/` and `docs/cli/schemas/` directories. Installing the material extension is optional for non-material CLI commands; material workers import the core bundled in this source tree.

## Runtime configuration

| Setting | Meaning |
| --- | --- |
| `--blender <absolute executable>` | Explicit Blender; highest precedence |
| `BLENDER_PATH` | Blender default; otherwise compatible project portable runtime, then `blender` on PATH |
| `BLENDERCTL_PYTHON` | Python executable used by the Windows PowerShell launcher |
| `--jobs-dir` / `BLENDERCTL_JOBS_DIR` | Job root; otherwise `runtime/blenderctl/jobs` inside the checkout |
| `--transactions-dir` / `BLENDERCTL_TRANSACTIONS_DIR` | Transaction root; otherwise `runtime/blenderctl/transactions` |

On Windows, the launcher tries explicit Python, compatible local portable Python, then `python3`/`python`. You can always call `python tools/blenderctl/cli.py` directly. The host Python and Blender's worker Python are separate processes. The actual baseline is Windows/Blender 5.2.1; changing an executable does not establish compatibility.

```powershell
$env:BLENDER_PATH = 'C:\Program Files\Blender Foundation\Blender 5.2\blender.exe'
python tools/blenderctl/cli.py --version
python tools/blenderctl/cli.py --jobs-dir ./runtime/demo-jobs doctor
```

`doctor` creates an owned job. Check its exit code and JSON `ok`; device enumeration establishes detection, not rendering success. `--version`, `--help` and `material describe` do not start Blender.

## Scene and CPU render

Run from the source root. These examples create original geometry with no asset downloads:

```powershell
$sceneText = python tools/blenderctl/cli.py --jobs-dir ./runtime/demo-jobs scene prepare --manifest ./docs/cli/examples/render-studio.json
if ($LASTEXITCODE -ne 0) { throw $sceneText }
$scene = ($sceneText | ConvertFrom-Json).data
$renderText = python tools/blenderctl/cli.py --jobs-dir ./runtime/demo-jobs render run $scene.candidate --expected-sha256 $scene.candidate_sha256 --manifest ./docs/cli/examples/render-cpu.json
if ($LASTEXITCODE -ne 0) { throw $renderText }
($renderText | ConvertFrom-Json).data.outputs
```

The scene is an editable candidate saved inside its job. The rendering job produces 256×256 PNG on CPU. Keep both job directories until you have reviewed and retained all needed files. Use the returned file paths, not guessed output names.

## Jobs and failures

Append `--help` to the exact subcommand. Global flags precede the subcommand. `--async` returns acceptance, then use the actual returned job ID:

```powershell
python tools/blenderctl/cli.py --jobs-dir ./runtime/demo-jobs job status <job_id>
python tools/blenderctl/cli.py --jobs-dir ./runtime/demo-jobs --compact job wait <job_id> --wait-seconds 30
python tools/blenderctl/cli.py --jobs-dir ./runtime/demo-jobs job result <job_id>
```

Reuse the original job/transaction roots for later queries and recovery. `job cancel` requests cancellation; verify the terminal state. Failed jobs can contain incomplete outputs that are not qualified candidates.

Inputs requiring mutation use absolute file paths, expected SHA-256, explicit context and resource lists. Do not replace an incomplete resource list with an empty array. Candidate creation and transaction publication are separate operations. See [CLI reference](CLI_REFERENCE.md) and the corresponding schemas.
