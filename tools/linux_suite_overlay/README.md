# Linux 5.3 isolated-suite overlay (draft)

This opt-in source snapshot is an **incremental overlay**, not a standalone distribution for a clean checkout. It does not change the default Windows CLI, release manifests or runtime identities.

| Path | Role |
| --- | --- |
| bin/blenderctl, bin/linux-flow | Named public dispatch for an already-provisioned isolated suite |
| source/linux-flow | Bounded background jobs, execution-session keeper and no-replace local publication of a checked candidate |
| source/camera_cli | Non-rendering camera CHECK/APPLY and read-only native/legacy shot inspection |
| source/legacy_cli | Compatibility entry for the older private Linux suite layout |

## Required layout and dependencies

The launchers target an existing isolated Linux workspace with sibling directories named `af-exterior-linux-suite`, `af-exterior-linux-dev`, and `af-exterior-blender53`. These are literal directory names in the qualified launchers. They do not select arbitrary interpreters or worker programs from the user's environment. The overlay mirrors the first directory. The legacy launcher targets the old suite; do not install by overwriting production code.

This snapshot does **not** supply the earlier base workers (including scripts/blenderctl_linux.py, scripts/blenderctl_linux_worker.py and scripts/linux_scene_guards.py), the other domain adapters, or Blender. The production runtime was Linux x64 Blender 5.3.0 build 02f0899d78e3. The named Python entry points use /usr/bin/python3.13. Camera CLI operations depend on the matching camera_windows_static add-on: see [Camera Workflow](https://github.com/tsist/blender-camera-workflow). No add-on ZIP, local jobs, credentials or project files are included. Do not run this directly from this checkout and assume the prerequisites exist.

See [Linux job commands](CLI-LINUX.zh.md) and [camera commands](CAMERA.zh.md). Test in a **new private staging directory** with the matching base, an empty owned 0700 jobs/background directory and disposable self-contained inputs. Require a real prepare/reopen, publish to a new name, verify the SHA, exercise cancellation and test camera CHECK/APPLY before operational use. Preserve backend receipts. Keep the originating terminal alive in process-namespace based execution hosts.

The exact recovered Python/shell sources were checked against the overlay member SHA-256 manifest and syntax checked during PR preparation. This is not a fresh Blender integration test on a clean checkout. Runtime qualification, relocation of dependencies, and automated upstream integration remain separate work.
