# Validation scope

## 0.64.0 procedural material qualification

The release adds `material.procedural-run` and `material.procedural-export` on the public 0.63 baseline. It does not incorporate later unrelated CLI features. The Material Workflow extension is version 0.8.0.

Native qualification used Blender 5.2.1 on Windows, CLI 0.64.0 and the public request routes. Two additional cases passed:

| Case | Preview | Export | Result |
| --- | --- | --- | --- |
| Wooden tabletop | Cycles CPU, two 128×128 studio views, 16 samples | Nine 2048×2048, 16-bit maps, 8 bake samples | Passed |
| Tinted glass vessel | Eevee GRAPHICS, two 128×128 studio views, 16 samples | Nine 128×128, 16-bit maps, 8 bake samples | Passed |

Both cases passed author and reconstructed save/reopen, rendered image decoding, disk-only reconstruction, non-target preservation during authoring/bake/rebuild, and unchanged source/resource SHA checks. The author and reconstructed contact sheets were visually inspected. These are technical checks at the stated resolutions and settings; they do not certify final artistic quality, every example, or arbitrary material graphs. Detailed native evidence is retained in a local audit and is not distributed with this source package. Earlier 64-pixel public run/export smoke also passed.

Host verification passed 33 CLI/packaging tests (one optional Pillow test skipped), 16 procedural route tests, and 35 video template compiler tests. The template failures caused by the old runtime pin were resolved by updating only its versioned `RUNTIME_SHA256`; the pin calculation, compiler algorithms, presets and catalog were preserved.

Final visual review and any independent review are tracked separately from these technical results. No external-engine reimport, cross-platform Blender run, large-scene stress test or universal shader-conversion compatibility is claimed. See [procedural materials](PROCEDURAL_MATERIALS.md) for the supported graph and export boundaries.

## Optional Thickness candidate regression

The optional `surface` addition remains part of the unpublished CLI 0.64.0 / extension 0.8.0 candidate. On Windows with Blender 5.2.1 LTS, both public source checkouts passed 35 fixture checks and eight further checks in a separate Blender process reopening the saved fixture; all four Blender processes returned 0. These checks cover the five public recipes without `surface`, pre-surface snapshot shape/persistence, SPHERE/SLAB, AUTO versus connected explicit 0 and positive thickness, mode/link edit protection, unknown dynamic Thickness rejection, and actual 32×32 backend bake/rebuild round trips for the three thickness states. Each bake completed nine required channels with verified disk decoding. The small backend fixtures do not change the public export size bounds.

A separate public CLI cup round trip passed at 128×128 with nine required channel maps, saved author/reconstructed files and Thickness preservation. This is bounded technical evidence; small samples do not establish the visual behavior of arbitrary glass, lossless refraction equivalence or publication. The later assisted file-stage review and extracted build-a checks are recorded separately below.

The portable scripts construct their own fixtures and refuse existing output directories. In this CLI checkout, run the build and independent reopen in separate background processes, replacing each output placeholder with an absolute path outside the checkout and using an existing Blender executable:

```text
blender --background --factory-startup --python-exit-code 1 --python tools/blenderctl/tests/material_procedural_optics.py -- --output <ABSOLUTE_NEW_BUILD_OUTPUT>
blender --background --factory-startup --python-exit-code 1 --python tools/blenderctl/tests/material_procedural_optics.py -- --output <ABSOLUTE_NEW_REOPEN_OUTPUT> --verify-fixtures <ABSOLUTE_BUILD_OUTPUT>
```

In the standalone Material Workflow checkout, its runner executes `tests/procedural_optics.py` and the independent reopen:

```text
python -B tests/procedural_runner.py --cases optics --blender <ABSOLUTE_BLENDER_EXECUTABLE> --output <ABSOLUTE_NEW_OUTPUT>
```

These targeted runs supplement the historical results above; they are not a new run of the entire suite. Host CI does not launch Blender for these fixtures.

## Completed bounded file-stage review — 2026-10-25

Fourteen material targets across the wood table, oil drum and three glass variants completed 126 required 2K channel maps, independent full-pixel disk checks and disk-only reconstruction. The maintainer actually reviewed 30 final author/reconstructed images across Cycles, Eevee and relighting, plus 12 local supplemental images: 42 images in total, including the oil drum's actual upper shoulder and lid edge. Private source assets and case audits are not distributed. Public scripts such as `tools/blenderctl/tests/material_procedural_optics.py` and the standalone extension's `tests/procedural_runner.py` construct their own reproducible fixtures.

DeepSeek completed real file-based creation/correction, 2K export, reconstruction and finite final-image review. The maintainer intervened in wood mixing, oil material layers and glass-thickness diagnosis; this was assisted reuse, not an unassisted blind test or a fully unattended workflow. Each recorded final view was supplied through a real JPEG image-tool input. Completion of these stages does not convert conditional visual findings into unconditional approval.

Retained limits include softer 2K pores/fine lines and approximate Eevee glass transmission, cavity depth and frosted microdetail. Wood end-grain identity and top/end orientation were supported by the supplemental views, while the end-face reconstruction remained conditional because fine pores and sharp lines softened. The oil upper-shoulder pair remained conditional: its dark structural band can resemble continuous wear and one rust area is broad; the actual lid-edge pair passed within that view. Glass and all other pair judgments remain limited to the supplied views; no arbitrary-prop or final aesthetic certification is claimed.

## Extracted build-a qualification — 2026-10-25

The tested build-a source and extension ZIPs were extracted into isolated test directories and passed 15 stages. Host checks covered 73 cases, with two optional Pillow-dependent cases skipped. Fresh standalone procedural cases were contract, core, bake and optics, including an independent-process optics reopen. Examples, mesh_maps, regressions, signals and ui were not freshly rerun in this build-a qualification.

The extracted CLI completed a public 128×128, nine-channel `surface` round trip; the independent full-pixel saved-delivery check reported maximum error 0. The newly packaged 25-member material extension matched its ZIP/source members, installed in an isolated environment, exercised legacy load/apply and the new surface path, saved its material and passed an independent reopen. No user preferences were saved. These are code/packaging checks; their own visual status was not run and is separate from the 42-image case review above.

This qualifies the tested build-a bytes and stated code paths. These final documentation updates require new archives and final byte/member and relevant verification checks; no later archive is claimed qualified here. CLI 0.64.0 / extension 0.8.0 / skills 0.3.0 remain candidates without public release assets.

## Identities and packaging

`PUBLICATION.json` records an explicit distribution inventory and a versioned runtime identity. The release runtime covers root Python modules in the CLI, Material Workflow addon and Video Editing Workflow addon, plus both addon manifests. Its exact count and identity are reported by `scripts/verify_release.py` and recorded in `PUBLICATION.json`.

The video template compiler keeps its original, narrower pin scope: CLI root Python modules plus the video addon root Python modules and manifest. Its current pin is recorded in `templates/video/template_library.py`; it intentionally differs from the release runtime identity because it excludes the material addon. Neither identity is a hash of the entire distribution.

All source ZIP members must match the staged Git tree and inventory. The existing video addon, video skill, template and all-skills archive outputs are retained.

The public template compiler differs from its local source only in root resolution (`BLENDERCTL_ROOT` or the source root). Media/proxy helpers use the same relocation convention; the SRT helper takes `--root`. Public docs exclude private project state. The four preset JSON files and catalog retain their original bytes and hashes.

Reproducible packaging uses ZIP_STORED with fixed member timestamps and permissions. Every build reads the archive back and compares each member. `verify_release.py` checks explicit hashes, runtime identity, syntax, skill links and targeted privacy patterns. These finite scans do not certify behavior or guarantee absence of every possible secret.

## Historical video qualification

The 0.63.0-r1 distribution had a 189-file CLI/video runtime identity of `4a6e44b18e1187baf1569e0f93f3eda4ae2ffa170a3d73b8c7083c0ca8f38bcb`. Its native release smoke used an extracted source ZIP: one serial create/output followed by cold inspection, eight 160×90 SDR main frames and eight per-shot frames, exact decoded pixels at the cut, and unchanged source SHA. The historical result summary in `RELEASE-VALIDATION.json` describes that earlier release. The optional native smoke requires an existing Pillow installation and Blender; CI runs host checks only.

Earlier local validation included seven small public template cases, separate movie/portrait input cases and decoded image/audio checks. Those historical checks are not a new full native video regression of this archive. This publication does not requalify every material, geometry, lighting, compositing or CLI adapter. Matching version numbers alone do not guarantee matching implementation identities between independently frozen specialty releases.

No 4K/HDR stress test, HDR display review, arbitrary foreign VSE, advanced audio, complex acting or long-form production qualification is claimed. Do not treat registration, static checks or a generated technical clip as artistic acceptance.

Publication revision r1 resolves Windows 8.3 TEMP aliases in the pure test fixture before comparing resource descriptors. The compiler already normalizes these paths; no production behavior or native qualification changed.
