# Linux saved-file rig reads (narrow qualification)
Run `python tests/linux_rig_reads/smoke.py /path/to/blender /tmp/new-private-directory`
with native Blender 5.3.0 and a qualified local filesystem (tested on tmpfs).
This exercises the public CLI, not calls inside a preloaded Blender session.

Qualified inputs are trusted, self-contained synthetic rigs in uncompressed Blender
5.3 files no larger than 64 MiB. Both commands require an expected SHA-256. A
native header/block allowlist rejects linked libraries, images, scripts and unknown
ID types before opening. Blender then checks referenced paths, object/modifier
types, sequencer strips and drivers before producing observations. Only armature,
mesh and empty objects qualify. This scope is intentionally stricter than Windows.
It does not sandbox arbitrary .blend files. Guard and filesystem limitations in
linux_file_guard.py apply. No edits, publication, rendering or resource budgets.
