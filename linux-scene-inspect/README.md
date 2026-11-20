# Testable Linux R3 scene inspection overlay

For the exact standalone artifact on `dist/linux-x64-blender-5.3-r3`, commit `7d7951a7b92e14861a275381cbeaef9844199342`. This change does not replace the original distribution, affect Windows, or add a job/publish route. The existing installer, runtime hash pin, private-copy, initialization and worker guards remain in place. A local, trusted, self-contained project is required. This is not a sandbox for untrusted files.

From the repository root, use a new extraction directory and a **new absolute private install prefix**. Put the verified official Blender 5.3 Linux x64 archive at `$ARCHIVE` and set `$PREFIX`:

```sh
R3=blenderctl-linux-x64-5.3.0-standalone-reconstruction-r3.zip
printf '7fc26b97df0b50901b5714074017747196096c3b469456464736477c19319b3d  %s\n' "$R3" | sha256sum -c - || exit 1
WORK=$(mktemp -d)
unzip -q "$R3" -d "$WORK" || exit 1
patch --batch --forward --fuzz=0 -p1 -d "$WORK/blenderctl-linux" < linux-scene-inspect/r3.patch || exit 1
/usr/bin/python3.13 -I -B "$WORK/blenderctl-linux/install.py" --prefix "$PREFIX" --blender-archive "$ARCHIVE" || exit 1
/usr/bin/python3.13 -I -B linux-scene-inspect/test_inspect.py "$PREFIX/bin/blenderctl" "$WORK/blenderctl-linux/smoke-scene.json"
```

Inspect with `"$PREFIX/bin/blenderctl" scene inspect --file /absolute/local/source.blend --expected-sha256 "$SHA256"`. Standard output contains the existing JSON envelope with a scene report in `data`. The worker opens the private copy once with scripts disabled and reuses R3's scene snapshot implementation. It does not apply manifests, create a candidate or render. The original source and working copy are rehashed before success. Job logs, the copied input, and report JSON remain under the private job directory (as in R3). The report covers declared scene fields; it is not a complete record of plugin or render state. No signature or new release artifact is claimed.
