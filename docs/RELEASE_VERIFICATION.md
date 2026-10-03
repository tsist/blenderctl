# Public release verification — 2026-10-04

This page preserves the original Windows release record. See the separate
[Linux branch verification](LINUX_VERIFICATION.md) for CLI 0.55.0 / extension 0.7.1.

Release scope: CLI **0.54.1**, Material Workflow **0.7.0**. Native runtime: **Windows / Blender 5.2.1 LTS / build 9e2066aef7ef / Python 3.13.13**. Tests use generated geometry and numerical textures, with no private models or texture library.

| Check | Result | Evidence / reproducible entry |
| --- | --- | --- |
| Independent finite source review | Pass | Production-only export, runtime dependencies/third-party notices, no historical research/tests or user configuration |
| Public source audit | Pass | `python scripts/check_release.py`; filename exclusions, credential/private-path patterns and Python syntax |
| Host contracts | Pass, 23 tests | `python -m unittest discover -s tests -p "test_*.py" -v`; actual CLI subprocesses and job bridge ownership/recovery cases |
| Material CLI execution | Pass | `tests/run_blender_smoke.py`; original Cube/UV/texture, one 64×64 CPU view, four samples |
| Source preservation | Pass | Source SHA before/after, plus CLI source protection report |
| Independent candidate reopen | Pass | 8 vertices, 6 faces, UV and object-linked managed material nodes verified in a separate Blender process |
| Render decode | Pass | Actual 64×64 PNG and contact sheet loaded by Blender and pixels read |
| Extension registration | Pass | Register/unregister with actual RNA property presence checked during ZIP installation |
| Extension ZIP validation | Pass | `blender --background --factory-startup --command extension validate <zip>` |
| Extension installation | Pass | `tests/extension_install_smoke.py`; isolated config/extension directories, actual extension repository installation/enable/disable; installed files equal ZIP members |
| Quick-start scene and render | Pass | Public `render-studio.json` → `render-cpu.json`, 256×256 CPU PNG, decoded and visually inspected |
| Deterministic ZIP rebuild | Required release gate | `python scripts/build_release.py --output dist`, then the same command with `--verify` |
| GitHub CI | Required release gate | Windows/Ubuntu × Python 3.12/3.13; host contracts, source audit, packaging and byte-identical rebuild; see repository Actions |

The finite source scan is not proof that arbitrary future inputs are safe. The native smoke tests do not establish GUI interaction, GPU/Eevee behavior, animation, batch resume/cancellation, general UV handling, artistic quality or physical material correctness. Historical broader adapter validation is not reproduced wholesale in the public smoke test.

## Reproduce native integration

```powershell
python tests/run_blender_smoke.py --blender <absolute-blender-executable> --output ./test-output/native-new
```

The output directory must be new. Read `summary.json`, `oracle.json`, the actual request/report and captured process logs. A new public implementation hash cannot resume private historical jobs; preserve old environments and make fresh disk snapshots for migration.

For ZIP installation tests, launch a separate factory-startup Blender process with `BLENDER_USER_CONFIG` and `BLENDER_USER_EXTENSIONS` inside a dedicated test root. Pass the ZIP and a new output child directory to `tests/extension_install_smoke.py`. The script refuses non-isolated user paths and never saves preferences.

Released artifact sizes and SHA-256 values are recorded in `release-manifest.json` and `SHA256SUMS.txt` alongside the ZIP downloads. Verify these before installing. Checksums establish downloaded content identity; release archives are not cryptographically signed.
