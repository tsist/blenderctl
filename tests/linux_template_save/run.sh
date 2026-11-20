#!/bin/sh
set -eu
umask 077
root=$(realpath "$1")
blender=$(realpath "$2")
here=$(dirname "$(realpath "$0")")
out=$(mktemp -d /tmp/material-case.XXXXXX)
mkdir -p "$out/config" "$out/resources"
export BLENDER_USER_CONFIG="$out/config" BLENDER_USER_RESOURCES="$out/resources"
"$blender" -b -t 1 --factory-startup --disable-autoexec --offline-mode --python-exit-code 5 -P "$here/fixture.py" -- "$root" "$out"
python3 "$root/tools/blenderctl/cli.py" --blender "$blender" --jobs-dir "$out/jobs" material template-save "$out/managed.blend" --expected-sha256 "$(sha256sum "$out/managed.blend" | cut -d' ' -f1)" --material-id Studio --template-id StudioExport --name 'Studio Export' --version 1.0.0 --resources "$out/resources.json" > "$out/result.json"
python3 - "$out" <<'PY'
import sys,pathlib,json,hashlib
p=pathlib.Path(sys.argv[1]); r=json.loads((p/'result.json').read_text())
assert r['ok'] and r['data']['status']=='pass'
d=r['data']; assert d['source_saved'] is False and d['source_unchanged'] and all(v=='pass' for v in d['checks'].values())
for k in ['template','bindings']: assert hashlib.sha256(pathlib.Path(d[k]).read_bytes()).hexdigest()==d[k+'_sha256']
assert hashlib.sha256((p/'managed.blend').read_bytes()).hexdigest()==json.loads((p/'request.json').read_text())['params']['expected_sha256']==d['source_sha256']
print('Verified:',p/'result.json')
PY
