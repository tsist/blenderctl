# Linux branch verification — 2026-10-03 UTC

Components: CLI **0.55.0**, Material Workflow **0.7.1**. Base: main commit
`14dd74767874f13d02ff15c030b07f0cc1d2daa4`; original software release remains
`v0.54.1` at `6e9fa62d26ac4b9e9b71e9b6d3d7dcc253a2791a`.

Native target: **Linux x86-64, Blender 5.2.2 LTS, build d13f752e3b9c**, bundled
Python 3.13.13. Host tests used Python 3.12.14. The installed launcher also verified
CLI startup under host Python 3.13.5. CPU rendering only; original generated
geometry, checker textures and a system DejaVu font were used, with no user assets.

## Executed checks

| Check | Result and scope |
| --- | --- |
| Original release baseline | 33 host tests passed; original material job failed with the expected Windows-only FileGuard refusal |
| Final host/native suite | **119 tests passed, zero skipped**, with `BLENDERCTL_TEST_BLENDER` set to the actual 5.2.2 executable |
| Linux guarded reads | 37 dedicated tests on actual tmpfs: writer/mmap conflict, blocking/nonblocking opens, nested readers/threads/processes, construction race, replacement-and-restore, ancestor moves, symlinks, overflow/lost evidence, fork and descriptor/signal cleanup |
| Real unsupported filesystem | Overlay input refused with `UNSUPPORTED`; no fallback guard |
| Native conflict acceptance | A nonblocking write-open during a real material job was rejected with EAGAIN; job ended `CONFLICT`, `data: null`, source SHA unchanged |
| Interpreter/UI contracts | 28 new tests; original 18 bridge tests preserved; new tests also exercised with bundled Python 3.13 |
| Snapshot workflow | 16 tests including native absolute/relative texture and external-font rebase, source version mismatch, undeclared/dynamic input refusal, cumulative copy/growth budgets |
| Material smoke | Generated source → CPU 64×64 view/contact sheet → independent reopen; geometry, UV, material nodes and image pixels verified; source hash unchanged |
| Scene/render walkthrough | Public scene manifest → 256×256 CPU PNG; decoded and visually inspected |
| Shared-storage roundtrip | Shared overlay source → protected tmpfs snapshot → successful material job → complete 24-file archive; every copied hash verified; archived candidate reopened with relative images/font and all PNGs decoded |
| Extension ZIP | Blender manifest validation and isolated install/enable/disable; installed members equal ZIP bytes |
| Persistent extension | Saved preference enablement loaded in a fresh Blender process; extension 0.7.1, CLI 0.55.0, bundled Python and snapshot defaults verified |
| Actual installed operators | `material_workflow.handoff` and refresh operators submitted a generated snapshot, reached succeeded/pass and preserved the original source |
| Packaging | Public source audit, skill metadata/link audit, Python compileall, diff whitespace check, deterministic build/rebuild and ZIP inventory coverage |
| Independent review | Separate source/security review; no remaining blocker after resource-copy budget and cleanup fixes |

The installed-operator and restart tests used real Blender in background mode.
Visible panel clicks were **not** tested. GPU/Eevee, animation, every adapter, batch
recovery/cancellation and artistic/physical material quality were not qualified.
Native Windows Blender was not rerun here; Windows implementations and their existing
tests were preserved. GitHub Actions is the separate Windows/Ubuntu host CI gate.

## Reproduce

```sh
export BLENDER_PATH=/absolute/path/to/blender-5.2.2/blender
BLENDERCTL_TEST_BLENDER="$BLENDER_PATH" python -m unittest discover -s tests -p 'test_*.py' -v
python tests/run_blender_smoke.py --blender "$BLENDER_PATH" --output /tmp/new-linux-native-smoke
python scripts/check_release.py
python scripts/check_skills.py
python scripts/build_release.py --output dist
python scripts/build_release.py --output dist --verify
```

Read [Linux boundaries and import/export instructions](LINUX.md). On other hosts,
real-kernel tests may explicitly skip if the required primitives/filesystem are
unavailable; that is not qualification. Ext-family, XFS, Btrfs and aarch64 are
implementation allowlist targets, but this run exercised x86-64 tmpfs only.

Archival preserves native relative resources, but historical absolute paths in reports
or custom image metadata can require an explicit rebase for future CLI operations.
The exporter does not silently alter the candidate's bytes. Temporary storage must
never be the only retained project/result copy. Existing version locks and unsupported
Windows transaction/pipeline/AppContainer boundaries remain in force.
