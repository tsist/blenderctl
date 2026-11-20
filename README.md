# Blenderctl Linux x64 handoff — reconstruction R3

Download **[blenderctl-linux-x64-5.3.0-standalone-reconstruction-r3.zip](./blenderctl-linux-x64-5.3.0-standalone-reconstruction-r3.zip)** using GitHub's **Download raw file** button, and verify it with [SHA256SUMS](./SHA256SUMS). This is a binary ZIP stored here as an installable artifact, not a source snapshot or the automatic ZIP from PR #4. The three missing Linux backend scripts are in `blenderctl-linux/base/`. They were reimplemented; they are not a byte-identical copy of the historical missing scripts.

Requires Linux x86-64, `/usr/bin/python3.13`, tar/xz, and the [official Blender 5.3.0 Linux x64 archive](https://download.blender.org/release/Blender5.3/blender-5.3.0-linux-x64.tar.xz). Blender is intentionally separate. The included README covers checksums, a fresh install prefix, and smoke examples for Blender build `02f0899d78e3`.

Supported here: probe, scene/model prepare, local background jobs, and new-file-only publish. Camera and other domain add-ons, rendering, and external providers are not part of this package. This artifact branch leaves the existing source PR and release inventory untouched.
