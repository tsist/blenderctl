# Video editing distribution

Use Python 3.12+ and an existing Blender 5.2 installation. Native qualification is limited to Windows / Blender 5.2.1 LTS build `9e2066aef7ef`. Host CI on another OS does not qualify Blender there. No binaries, fonts, user footage or production projects are bundled.

Set `BLENDERCTL_ROOT` to this source checkout when installing a skill or template archive separately. `BLENDERCTL_PYTHON` selects the Python used by `blenderctl.ps1`; `--blender` selects Blender; `BLENDER_PATH` below is only a shell variable passed explicitly. A template compiler checks the full runtime identity, not just the CLI version string.

```powershell
$env:BLENDERCTL_ROOT = (Get-Location).Path
python -B tools/blenderctl/cli.py --version
python -B tools/blenderctl/cli.py video describe
python -B templates/video/cli.py list
python -B templates/video/cli.py schema --kind input
```

The source ZIP is self-contained at the source level: retain the `tools` hierarchy and the schemas. Other workflow adapters need their bundled support modules even if only video is used. Use `scripts/build_release.py` to make this distribution; the generic `project.package` command packages project candidates and is not this workflow release builder.

## Small reproducible example

Run from the source root with a real executable path. The example creates an empty new source and eight 160×90 SDR color frames; no fonts, sound or downloads. Use new output locations and keep the jobs with the candidate.

```powershell
$env:BLENDER_PATH = 'C:\Program Files\Blender Foundation\Blender 5.2\blender.exe'
$exampleDir = [System.IO.Path]::GetFullPath('../video-example-output')
& $env:BLENDER_PATH --background --factory-startup --disable-autoexec --threads 1 --python examples/create_fixture.py -- ../video-example-output/seed.blend
python -B examples/make_input.py --source ../video-example-output/seed.blend --output ../video-example-output/input.json
$inputSha = (Get-FileHash -LiteralPath ../video-example-output/input.json -Algorithm SHA256).Hash.ToLowerInvariant()
python -B templates/video/cli.py compile --template cg-clean --input (Join-Path $exampleDir 'input.json') --expected-input-sha $inputSha --output-dir (Join-Path $exampleDir 'compiled')
python -B tools/blenderctl/cli.py --blender $env:BLENDER_PATH --jobs-dir ../video-example-output/jobs --timeout 120 --compact request ../video-example-output/compiled/request.json
```

Check native exit code, JSON `ok`, job terminal state, report and output bytes. Cold inspection is a separate `video.inspect` request bound to the candidate SHA, complete resources, workflow ID and Scene. `tests/video_smoke.py --blender EXE --output NEW_SHORT_PATH` automates this example plus all-frame pixel checking with an already installed Pillow library.

## Blender extension

Download `video_editing_workflow_addon-0.1.1.zip`, install through Blender's **Install from Disk**, and enable Video Editing Workflow. This is a small shared VSE core and inspection/editing UI; the CLI works with source snapshots in the package without requiring global addon installation. An enabled addon does not prove a production case passed. Do not mix a different addon core into this SHA-bound source tree.

## Agent skill

Copy `skills/blender-video-editing` (or that folder from the skill ZIP) to your agent's skill directory. Keep all references and scripts together. Set `BLENDERCTL_ROOT` to the matching source root before using its helpers. The standalone skill ZIP does not bundle the full CLI. The skill plans editing, selects candidates, executes within an approved plan, and asks for artistic review of outputs. It does not submit to video-generation services.

## Templates

See [template library](../templates/video/README.md). Four presets support whitebox boards, AI final cuts, CG presentation and readable subtitles; JSON custom presets and explicit batch compilation (1–16 members) are included. Version 0.1.0 supports SEQUENCE/SDR/hard cuts only. Compile produces ordinary `video.run` requests and receipts; it does not run Blender. Consume only completed receipts with no FAILED marker. A valid compile is not proof of media decoding, source dependency closure or artistic quality. Use an explicit licensed font for every text strip; exported SRT includes titles and shot labels.

## Protection and boundaries

Inputs are saved disk snapshots. Work on new candidates, preserve manual/locked/unknown strips, and keep both versions on conflict. Source/resource SHA checks do not observe unsaved GUI state. Do not clear caches or dependencies still referenced by delivered candidates.

Base video workflow supports bounded retiming/freeze/replacement, candidates, original-speed dialogue/music/SFX, volume/fades, subtitle/title editing, basic transforms/color and explicit outputs. Complex effects belong to the compositing workflow. HLG, arbitrary imported VSE continuation, audio time-stretch, nonzero pan and HDR display viewing are not supported. Technical PQ/10-bit paths have limited prior evidence; 4K HDR and long combined loads are not qualified. This template library deliberately rejects HDR. Complex performance and real long-form production remain future validation goals.

The compiler refuses output/custom-library locations inside the protected source root. Place project outputs in a sibling or other explicitly selected external directory. FFmpeg/ffprobe for MP4 or preparation helpers must be explicitly bound by their real executable paths and SHA-256; PNG-only examples do not need them. Some legacy native report fields still show `addon_version: 0.1-dev`; the authoritative addon version is the shipped `blender_manifest.toml` (0.1.1).
