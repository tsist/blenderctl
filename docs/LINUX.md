# Linux installation and support boundary

This Linux branch pairs **CLI 0.55.1** with **Material Workflow 0.7.1**.
Python 3.12+ and an independently installed Blender are required. No pip package,
network service, credentials, MCP server or kernel configuration changes are needed.
The original Windows 5.2.1 release remains available under `v0.54.1`.

## Install and configure

Keep the full source tree, including `tools/` and `docs/cli/schemas/`. Use the real
Blender executable, not a wrapper that overwrites `BLENDER_USER_*`: the CLI supplies
isolated per-job Blender configuration. All global flags precede subcommands.

```sh
export BLENDER_PATH=/absolute/path/to/blender
export BLENDERCTL_ROOT=/absolute/path/to/blenderctl
export BLENDERCTL_JOBS_DIR=/qualified/local/filesystem/blenderctl-jobs
sh "$BLENDERCTL_ROOT/blenderctl.sh" --version
sh "$BLENDERCTL_ROOT/blenderctl.sh" doctor
```

`BLENDERCTL_PYTHON` can select the shell launcher's host interpreter. Blender's
extension independently discovers one bundled `python3`, `python3.x` or Windows
`python.exe` in the chosen installation. It never picks a Python from PATH, and
ambiguous or escaping candidates are rejected. The sidebar also offers an explicit
Python interpreter field.

Build the source and extension ZIPs with `python scripts/build_release.py --output
dist`, then repeat with `--verify`. Install `material-workflow-0.7.1.zip` through
Preferences → Extensions → Install from Disk. Set the backend source directory.
The optional `BLENDERCTL_ROOT` and `BLENDERCTL_SNAPSHOT_ROOT` launch environment
variables provide defaults for new scenes; they do not create or save a scene.
The paired source tree is still required for background work.

## Protected inputs are a filesystem capability

Linux has no direct equivalent of the Windows open-handle deny-write/deny-delete
contract. This branch uses a **different, explicitly bounded** protected-read
contract, with no advisory-lock-only or hash-only fallback:

- The normalized real target is opened through a no-symlink, held-directory path walk
- A kernel read lease rejects a file already open for writing or writable mapping,
  and delays conflicting write-open attempts
- Inotify evidence and held identities detect file/ancestor namespace changes,
  replacement-and-restore, lease breaks and loss/overflow of monitoring evidence
- Guard checks occur during supervision and before accepting results; an observed
  conflict fails the job, and failed candidates must not be used
- Unsupported filesystems, namespaces or kernel capabilities fail closed

Kernel leases do not prevent renaming/unlinking. Such namespace changes are
detected and invalidate acceptance rather than being claimed impossible. This is
not a security sandbox against a malicious privileged process, kernel, device or
mount-namespace administrator. File hashes identify content, not safety.

Existing public request normalizers may resolve caller-supplied symlink aliases
before the guard runs. Protection covers that concrete normalized target, which is
also the path actually consumed; it does not monitor an alias no longer used by the
job. Calling FileGuard directly with a symlink component is rejected. Do not read
this as a promise that every user-facing spelling of a path is preserved/monitored.

Overlayfs and network filesystems are not qualified protected input roots. A
directory name alone proves nothing: each real held descriptor is checked, and
tests exercise the actual lease and event behavior. A suitable local tmpfs can be
used for snapshots, but **tmpfs and `/tmp` are not persistent storage**. Do not keep
the only copy of a project or completed results there. Space is bounded by that
filesystem; the tool does not resize it, mount another filesystem or change security
settings. A capability check may still refuse an apparently suitable path.

## Import an existing shared-storage material request

The import helper requires an explicitly new destination under `/tmp`; the actual
filesystem must still pass its capability probe. It accepts an existing `material.run` JSON request containing
an exact source SHA-256 and complete static resource descriptors. It does not infer
or download missing dependencies. It writes a **new** snapshot directory and never
edits the original project. Relative references are interpreted against the original
project location; declared static images/fonts are rebased to the snapshot. Linked
libraries, dynamic media/cache inputs and unsupported state are rejected.

```sh
python scripts/linux_snapshot.py stage \
  --request /shared/project/material-request.json \
  --destination /tmp/new-snapshot \
  --blender "$BLENDER_PATH"
```

Read the returned JSON and use its returned request path rather than guessing it:

```sh
python tools/blenderctl/cli.py --blender "$BLENDER_PATH" \
  --jobs-dir /qualified/local/filesystem/jobs request /returned/request.json
```

The original shared-storage files are not given deny-write protection during import.
The importer verifies copied bytes against the explicitly declared hashes and checks
for observed changes. Only the resulting snapshot is eligible for protected CLI
execution. Snapshot creation changes source paths and may reserialize the `.blend`;
its new SHA is recorded separately from the original. A failure leaves clearly
unaccepted partial files for inspection and does not overwrite/retry a destination.

## Archive completed material results

After the authoritative job result reports success, copy the **whole job**, retaining
its candidate, textures, reports and relative directory layout:

```sh
python scripts/linux_snapshot.py export \
  --job /qualified/local/filesystem/jobs/returned-job-id \
  --destination /shared/results/new-archive
```

The exporter requires a terminal successful `material.run` result, protects all source
job files while copying, checks hashes and writes a relative archive manifest. The
original evidence reports retain their original absolute paths; the archive manifest
locates the archived candidate. Reopen that candidate and verify its dependencies
before discarding temporary files. Export never deletes the source job.

Some existing Blender custom properties, such as `blenderctl_image_manifest`, may
retain absolute paths from the producing job. Native relative texture/font paths
remain portable, but a later CLI validation/authoring step may require an explicit
metadata rebase and a new request. The byte-identical exporter intentionally does
not silently rewrite the candidate or its historical evidence.

## Deliberately unsupported

- Windows-style file publication/rollback transactions, directory guards and PathLocks
- Pipeline orchestration that depends on those locks and hard Windows Job memory limits
- Windows AppContainer script isolation; no unconfined fallback
- Blender adapters still gated to exact 5.2.1, including native simple drivers,
  links/overrides, Quadriflow and the scoped GUI sculpt replay
- General linked/dynamic project migration in the snapshot importer

CPU saved-file tests do not establish GPU/Eevee, animation, arbitrary add-ons, artistic
quality, all adapter support or complete GUI interaction. See the [branch verification
record](LINUX_VERIFICATION.md) for the exact executed coverage. Use fresh snapshots after implementation
changes; old recovery records are intentionally tied to their original implementation.

## Reproduce checks

```sh
python -m unittest discover -s tests -p 'test_*.py' -v
BLENDERCTL_TEST_BLENDER="$BLENDER_PATH" python -m unittest discover \
  -s tests -p 'test_*.py' -v
python tests/run_blender_smoke.py --blender "$BLENDER_PATH" \
  --output /qualified/local/filesystem/new-native-smoke
python scripts/check_release.py
python scripts/check_skills.py
```

Host tests include Windows-compatible bridge regressions, Linux guard races and
negative capability tests. Native tests must use the actual target Blender and
qualified fixture directories. Windows branches of the source are preserved; Linux
test success alone is not a claim that native Windows Blender was rerun.
