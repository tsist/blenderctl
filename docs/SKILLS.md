# Companion Blender skills 0.1.0

[Installation and contents](../skills/README.md). Four sibling skill folders are distributed separately from the CLI and Blender extension. The source package also contains them under `skills/`.

The public adaptations preserve the existing plugin/CLI work-unit priority, technical/visual acceptance separation, six candidate methods, PBR channel/source rules and asset identity/UUID/dependency protection. They remove private machine locations, fixed task IDs, Wiki roots, current project progress, asset provenance judgments and user conversations. Historical examples are explicitly limited guidance; original case artifacts are not published.

## Verification

| Check | Scope |
| --- | --- |
| `python scripts/check_skills.py` | Four consistent metadata entries, agent invocation metadata, complete local reference/resource graph and no private task/path references |
| `python -m unittest discover -s tests -p "test_skill_tools.py" -v` | Actual CLI source discovery including installation elsewhere, public material schemas, invalid-input handling, read-only naming/collision checks, image help without Pillow |
| Same tests with a Pillow-capable Python | Original PNG decoding, size/byte budget rejection and input protection; optional test is skipped if Pillow is absent |
| `python -m unittest discover -s tests -p "test_skill_install.py" -v` | Four-sibling installation, byte preservation, existing-skill refusal before any copy, read-only listing |
| Standalone ZIP rehearsal | Extract bundle, run its root installer into an owned temporary directory, read installed cross-skill links and run discovery with explicit CLI root |
| Byte-identical build | `python scripts/build_release.py --skills-only --output <new-dir>`, then the same command with `--verify` |
| Repository CI | Windows/Ubuntu × Python 3.12/3.13: host/helper tests, public audit, skill metadata/resource graph and repeated ZIP build |

Local helper validation uses existing interpreters, including a Pillow-capable runtime; no package installation is performed. Stdlib-only CI explicitly skips Pillow decoding. All other skill/helper checks are required. The metadata checker checks this package's documented simple YAML subset and local graph; it is not a certification of every agent host.

These checks validate packaging and helper behavior, not full artistic decisions, all mechanical methods, image-provider availability or cross-platform Blender runtime. Existing software runtime verification remains separately scoped in [release verification](RELEASE_VERIFICATION.md). CLI and extension production modules are unchanged by this companion release.

## Maintenance

Public sources live in `skills/`. Manifest version identifies the companion distribution independently of CLI/extension versions. Keep the four skill folders at the same level, preserve automatic invocation, update supporting resources and rerun the local graph check after edits. Project rules and the user's actual authorization take precedence; installed copies cannot inherit the original maintainer's private approvals.

Image decoding has optional Pillow dependencies listed in `skills/requirements-images.txt`. MCP, native image generation, fallback providers and memory/Wiki remain optional host capabilities. Use explicit `BLENDERCTL_ROOT` when the installed skills are outside the software checkout. The installer requires an explicit destination and never replaces a same-named skill.
