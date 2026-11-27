# Win53 rigging and posing

`rigging-workflow phase3` is the Windows Blender 5.3.0 development route for body motion, pose candidates and keyed edits in an owned Blender window. It uses build `02f0899d78e3`. Build the separate Rigging Workflow package from its [Win53 source and instructions](https://github.com/tsist/blender-rigging-workflow/blob/main/docs/WIN53_POSING.md).

## Start from a saved scene

```powershell
$ctl = (Resolve-Path .\blenderctl.ps1).Path
& $ctl rigging-workflow phase3 describe

$blender = 'C:\Tools\Blender 5.3\blender.exe'
$package = 'C:\Work\rigging_workflow-win53-posing.zip'
$packageSha = (Get-FileHash -LiteralPath $package -Algorithm SHA256).Hash.ToLowerInvariant()
$runtime = @('--blender', $blender, '--rigging-package', $package, '--rigging-package-sha256', $packageSha)
& $ctl @runtime rigging-workflow phase3 run --params 'C:\Work\request.json'
```

Use a ZIP you trust. The supplied SHA checks that the bytes remain the same throughout the job. Both authoring and independent verification use isolated Blender profiles with offline mode and automatic script execution disabled. Use `--output` for a new absolute `.blend` path in an existing directory. The candidate is published to that path after the separate worker reopens it and verifies the Action and scene. Reopen the result using `data.candidate.file` and `data.candidate.sha256`.

Requests use UTF-8 JSON. `retarget-fbx-inspect` and `retarget-profile-build` take an `operation` and `parameters` with `source_file` and `source_sha256`. All other operations also require top-level `file`, `expected_sha256`, `scene` and `view_layer`. For example, a read-only ROM inspection can use this request with the exact name of your rig.

```json
{
  "operation": "rom-inspect",
  "file": "C:/Work/character.blend",
  "expected_sha256": "<SHA-256 of character.blend>",
  "scene": "Scene",
  "view_layer": "ViewLayer",
  "parameters": {"rig": "CharacterRig", "reference_frame": 1, "frame": 24}
}
```

| Operation | Key parameters and output |
| --- | --- |
| `retarget-fbx-inspect` | Source FBX identity and optional exact `take` and `slot`. Returns the actual Take/Slot, source clock and mapping proposal. |
| `retarget-profile-build` | Source FBX identity and optional confirmed `source_mapping`. Use a skinned reference when building a reusable Profile for animation-only clips. |
| `retarget-body` | `rig`, source FBX, inline `profile` or `profile_file` plus `profile_sha256`, and `target_kind`. `C94` accepts `root_rotation: "HEADING"` or `"FULL"`. `GENERIC_FK` requires `target_mapping` and `acknowledged: true`. |
| `shoulder-pose` | `rig`, base `action`, `slot`, `frame`, `direction` (`FORWARD` or `ABDUCTION`) and `elevation`; optional follow, manual offset and advisory strength. |
| `rom-inspect` | `rig`, `reference_frame` and `frame`; optional advisory `strength`. Returns observations without writing a scene. |
| `three-beats` | `rig`, `action_name`, three ordered `frames`, explicit `controls` and three lists of `poses` channel records. |
| `fk-heel-contact` | `rig`, weighted `mesh` and `calibration` as `{file, expected_sha256}`. Returns a candidate in the original FK Slot. |
| `gait-contact` | `rig`, `action_name`, `motion_usage: "GAIT"`, SHA-bound `calibration` and `detection_report`, exact `issue_id` and consecutive `frames`. `tangent_release` can preserve a deliberate slide inside the support span. |

Take and Slot names refer to the current native import. Blender may rename a source Action or Slot on collision. A failed exact selection returns available Take/Slot pairs. The generated target Slot is reported separately. If the Action starts at one clock and the character uses another, compare events in seconds.

## Capture a keyed edit

Create a request with `file`, `expected_sha256`, `scene`, `view_layer` and `rig`. Start `live-open --params <request> --session-dir <new-directory>` with the same `$runtime` flags. Keep the launching terminal open. Blender opens a new working copy from the specified file. After editing and inserting the required Action keys, use a second terminal for the following steps.

1. Read `session_id` from `session.json` in the new directory. Subsequent commands take `--session-dir` and `--session-id`.
2. Use `live-status` while playback is paused. Check the current Action, Slot, frame and `data.context.expected_context`.
3. Use `live-capture --expected-context <value>` with that value. Pass `--request-id` as 32 lowercase hex digits when you want to name the request. Use `live-result --request-id <id>` to retrieve a pending or completed receipt. A repeat with the same ID and payload returns the original receipt.
4. Read the current status again, then use `live-close --expected-context <value>`. The command writes a recovery snapshot before closing the owned window. The snapshot path and SHA arrive in the receipt.

If Blender is in Pose Mode, preserve the keyed snapshot and make a fresh Object Mode copy for the older `rig prepare` entry. The explicit context check covers the current pose and the authored curves, including Bézier handles. Use fresh observations after editing.
