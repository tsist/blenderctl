# Compatibility and support boundaries

The tested Windows x64 builds are **Blender 5.2.1 LTS (9e2066aef7ef)** and **Blender 5.3.0 (02f0899d78e3)**. Use host Python 3.12+. The source distribution requires a separately installed Blender.

Set the optional `BLENDERCTL_BLENDER_ROOT` absolute path to the directory containing the versioned Windows portable distributions. It defaults to `runtime/blender/releases` beside the source checkout. Layout example: `5.3.0/blender-5.3.0-windows-x64/blender.exe`.

```powershell
.\blenderctl.ps1 --blender-version 5.3.0 doctor
.\blenderctl.ps1 --blender-version 5.2.1 doctor
```

Both builds were exercised for background startup and the CPU material smoke test. The 5.3 qualification covers reopening project copies from 5.2 and managed static compositor stacks. The generic render guard checks every effect and rejects unmanaged output nodes. Separate video, simulation and rigging adapter qualifications are maintained with their workflows. Use `--blender` for an explicit executable path.

Several native drivers, links/overrides, retopology and format-time adapters explicitly restrict Blender versions. The extension minimum of Blender 5.2.0 is an installation declaration, not a statement that every Blender 5.2 build was exercised. Check the actual adapter and runtime result.

Windows Job Objects, shared-delete file reads and AppContainer APIs have Windows semantics. POSIX process code exists, but Linux/macOS full runtime behavior is not qualified by the Windows integration run. GitHub Linux jobs exercise host contracts and packaging only.

Cycles/Eevee render paths and static managed PBR inputs have bounded node/engine contracts. GPU enumeration is not proof of successful GPU rendering. The public release smoke test uses CPU; no new GPU, Eevee, animation, batch recovery, GUI interaction, artistic or physical material certification is claimed.

Working candidates, dependency SHA verification, atomic JSON records and guarded transactions do not guarantee simultaneous multi-file visibility, power-loss consistency, hard disk/VRAM quotas or safety of arbitrary trusted Python. No cloud service or network share compatibility is implied.

Private historical validation informed the adapters' original restrictions. Its audit directories and user assets are not distributed. Public validation is independently reproducible with generated fixtures; see [release verification](RELEASE_VERIFICATION.md).
