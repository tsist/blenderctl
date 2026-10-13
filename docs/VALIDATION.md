# Validation scope

The distribution preserves the 189-file video runtime identity:
`4a6e44b18e1187baf1569e0f93f3eda4ae2ffa170a3d73b8c7083c0ca8f38bcb`.

`PUBLICATION.json` records the full explicit release inventory including supporting addons, schemas, helper adaptations and documents. The video runtime identity covers a smaller set; it is not a hash of the complete distribution. All source ZIP members must match the staged Git tree and inventory.

The public template compiler differs from its local source only in root resolution (`BLENDERCTL_ROOT` or the source root). Media/proxy helpers use the same relocation convention; the SRT helper takes `--root`. Public docs exclude private project state. The four preset JSON files and catalog retain their original bytes and hashes.

Reproducible packaging uses ZIP_STORED with fixed member timestamps and permissions. Every build reads the archive back and compares each member. `verify_release.py` checks explicit hashes, runtime identity, syntax, skill links and targeted privacy patterns. These finite scans do not certify behavior or guarantee absence of every possible secret.

Host tests exercise actual CLI subprocesses, template compiler constraints and skill packaging. Native release smoke uses an extracted source ZIP: one serial create/output followed by cold inspection, eight 160×90 SDR main frames and eight per-shot frames, exact decoded pixels at the cut, and unchanged source SHA. The published result summary is in `RELEASE-VALIDATION.json`. The optional native smoke requires an existing Pillow installation and Blender; CI runs host checks only.

Earlier local validation included 35 compiler tests, seven small public template cases, separate movie/portrait input cases and decoded image/audio checks. Those historical checks are not a new full regression of this archive. This publication does not requalify every material, geometry, lighting, compositing or CLI adapter. Matching version numbers alone do not guarantee matching implementation identities between independently frozen specialty releases.

No 4K/HDR stress test, HDR display review, arbitrary foreign VSE, advanced audio, complex acting or long-form production qualification is claimed. Do not treat registration, static checks or a generated technical clip as artistic acceptance.

Publication revision r1 resolves Windows 8.3 TEMP aliases in the pure test fixture before comparing resource descriptors. The compiler already normalizes these paths; no production behavior or native qualification changed.
