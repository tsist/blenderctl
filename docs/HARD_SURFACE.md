# Separate Hard Surface Workbench integration

This Linux integration pairs **blenderctl 0.55.1** with **Hard Surface Workbench
0.2.0**. The plugin has its own source repository and release artifacts. It is not
vendored into blenderctl. Material Workflow remains 0.7.1.

## Explicit local source checkouts

Use Python 3.12+ and independently installed Blender 5.2. Read the plugin's own
runtime qualification and source-protection limits before running native jobs.
Place reviewed source checkouts beside each other, for example:

```text
work/
  blenderctl/
    blenderctl.sh
    tools/blenderctl/cli.py
  hard-surface-workbench/
    blender_manifest.toml
    hardsurface-cli
    blender_worker.py
    hardsurface/
    schemas/
```

After reviewing and verifying the plugin release's checksums, set the absolute
path to its extracted **source** checkout, not the Blender extension ZIP:

```sh
cd /absolute/path/to/work/blenderctl
export BLENDERCTL_HARDSURFACE_ROOT=/absolute/path/to/work/hard-surface-workbench
export BLENDER_PATH=/absolute/path/to/blender
sh ./blenderctl.sh --version
sh ./blenderctl.sh hardsurface describe --section request
sh ./blenderctl.sh hardsurface --help
```

Setting this variable explicitly trusts that checkout's Python code. File layout
and version checks are compatibility checks, not authentication or a security
sandbox. Use a local checkout you control; do not modify it while jobs run. The
integration never downloads code, installs an extension, changes configuration,
reads credentials or silently searches Python/user-site directories for a plugin.
A missing root, symlink component, missing file, mismatched manifest/package
version or invalid required Python source returns a structured JSON error.

## Whole work units and job ownership

Global options precede the command. Supported options are `--blender`,
`--jobs-dir`, `--compact` and `--async`. Hard-surface requests carry their own
budgets: `--timeout`, `--human` and `--transactions-dir` are rejected for this
integration rather than ignored. `--async` remains restricted by the plugin to
`hardsurface run`.

```sh
export BLENDERCTL_JOBS_DIR=/tmp/my-hard-surface-jobs
sh ./blenderctl.sh --compact hardsurface plan \
  --request /absolute/path/to/reviewed-request.json
sh ./blenderctl.sh --async hardsurface run \
  --request /absolute/path/to/reviewed-request.json
sh ./blenderctl.sh job status RETURNED_JOB_ID
sh ./blenderctl.sh job result RETURNED_JOB_ID
```

Use a fresh, dedicated job directory on a filesystem qualified by the plugin.
The example `/tmp` directory is temporary, not a durable archive or evidence of
filesystem suitability. Model/reference approval remains part of the plugin
contract; the examples do not bypass it. A `plan` or `describe` call does not
create a job-store owner marker. Archive accepted results using the plugin's
explicit new-destination command before losing temporary storage.

The last explicit `--jobs-dir` takes precedence over `BLENDERCTL_JOBS_DIR`.
Without either, `hardsurface` retains the plugin's `/tmp/hardsurface-dev-jobs`
default; specify that same path for a later `job` command. Ordinary blenderctl
job commands retain their original default. An existing `owner.json` routes a
job command only when `kind`, version, absolute root and current-user identity
match the hard-surface contract exactly. Directory names and prefixes never
establish ownership. Routing itself does not create or adopt a store.

Relative CLI file paths are interpreted against the caller's working directory
before the child changes into the plugin checkout. Use full spellings of plugin
path options (`--request`, `--cases`, `--file`, `--reference-approval`,
`--destination`) and an absolute Blender executable path. Ambiguous/abbreviated
path options fail closed. Request-internal paths must still obey the plugin's
own absolute-path contract. Symlinks and `..` in guarded paths are rejected.

The child runs the selected host interpreter with bytecode writes, site loading
and implicit current-directory imports disabled. Its Python search path is the
explicit checkout plus interpreter standard-library paths; inherited Python
path/configuration variables are not reused. Required operational variables
such as `BLENDER_PATH` remain available. Child stdout/stderr and its exit status
are preserved. The integration does not retry a failed or uncertain job.

## Source-only checks

```sh
python -B -m unittest discover -s tests -p 'test_hardsurface_dispatch.py' -v
python -B -m unittest discover -s tests -p 'test_*.py' -v
```

Tests generate their own minimal local plugin and metadata, and cover root and
version validation, Python import isolation, globals, relative paths, subprocess
exit status, job markers and legacy command behavior. They require no Blender,
network access, credentials or dependency installation. These checks do not
establish native geometry quality, GUI behavior or Windows plugin support.
