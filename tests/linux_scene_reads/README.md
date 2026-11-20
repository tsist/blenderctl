# Shared Linux CLI: static scene inspection

The source CLI now supports `scene inspect` for trusted, self-contained,
uncompressed Blender 5.3.0 files up to 64 MiB on qualified local filesystems.
Pass the expected SHA-256. The supervisor holds its Linux read guard through
result acceptance; the worker checks the block directory before opening and
uses no scripts. The qualified profile includes static local mesh, empty,
camera and light objects, collection instances and explicit inspection context.
It refuses linked libraries, external data, unknown IDs, animation, modifiers,
constraints and other non-static behavior. It neither saves nor renders.
This does not sandbox untrusted .blend files and does not change Windows or the
separately distributed R3 overlay.

Run with native Linux Blender 5.3.0:

```sh
python tests/linux_scene_reads/smoke.py /qualified/path/blender /tmp/new-private-directory
```

The test checks the saved and explicitly requested scene context, object
transform, camera and collection instance; verifies unchanged source bytes;
and checks refusal of missing source/SHA, wrong SHA, symlink, a linked file,
external image, unsupported modifier and open writer.
