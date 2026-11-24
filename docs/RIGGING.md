# Rigging Workflow

The `rigging-workflow` command group runs the standalone v003 character binding commands from a local checkout of [Blender Rigging Workflow](https://github.com/tsist/blender-rigging-workflow).

## Setup

Use a checkout containing `cli-v003/` and set `BLENDERCTL_RIGGING_ROOT` to its absolute repository path. Set `--rigging-root` after `rigging-workflow` to choose a different checkout for one call. The selected checkout supplies the Python code that will run.

```powershell
$env:BLENDERCTL_RIGGING_ROOT = 'D:\Projects\blender-rigging-workflow'
.\blenderctl.ps1 rigging-workflow discover
.\blenderctl.ps1 --blender 'D:\Tools\Blender\blender.exe' rigging-workflow candidate-entry inspect `
  --request 'D:\Jobs\binding\request.json' --run-dir 'D:\Jobs\binding\inspect'
```

v003 uses Windows Blender 5.3.0 build `02f0899d78e3`. Pass `--blender` before the command group or select the portable installation with `--blender-version 5.3.0` and `BLENDERCTL_BLENDER_ROOT`. With neither flag, the group looks for the same portable 5.3.0 installation. Discovery and repair cancellation run without Blender.

For direct Python invocation, replace `.\blenderctl.ps1` with `python tools/rigging_workflow_cli.py` and keep the remaining arguments.

`discover` reports the v003 edition, commands, modes, and which scripts exist in the configured checkout. Use `--help` after a command to see its arguments.

| Command | Modes |
| --- | --- |
| `candidate-entry` | `inspect`, `scope`, `result`, `audition`, `finish` |
| `candidate-selected-skin` | `inspect`, `execute` |
| `candidate-repair` | `inspect`, `review-handoff`, `apply`, `cancel` |
| `candidate-skinning-policy` | `qualify`, `plan`, `preview`, `apply` |

Use absolute paths and a new run directory with an existing parent. To cancel a repair, pass its existing run directory and omit the request. The standalone commands own their run directories, evidence, JSON output and exit codes. For request preparation, source review, recovery, and local CUDA kernel setup, see the [standalone command guide](https://github.com/tsist/blender-rigging-workflow/tree/main/cli-v003).

The core `rig prepare`, `rig inspect`, and `rig package` commands continue to use the built-in blenderctl contracts. The separate repository also documents its Blender add-on and companion skill.
