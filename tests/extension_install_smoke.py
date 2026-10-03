# SPDX-License-Identifier: GPL-3.0-or-later
"""Blender worker: install a release ZIP into a new, isolated local repository.

Set BLENDER_USER_CONFIG and BLENDER_USER_EXTENSIONS to an owned test root before
launch. Invoke with -- ZIP ABSOLUTE_NEW_OUTPUT. Never save user preferences.
"""
import hashlib
import json
from pathlib import Path
import sys
import zipfile
import bpy

package_text, output_text = sys.argv[sys.argv.index('--') + 1:][:2]
package = Path(package_text).resolve(strict=True)
output = Path(output_text).resolve()
output.mkdir(parents=True, exist_ok=False)
for kind in ('CONFIG', 'EXTENSIONS'):
    location = Path(bpy.utils.user_resource(kind)).resolve()
    if not location.is_relative_to(output.parent):
        raise RuntimeError('Test requires isolated BLENDER_USER_' + kind)
repository = output / 'repository'
repository.mkdir()
repo = bpy.context.preferences.extensions.repos.new(name='Release QA', module='release_qa',
                                                     custom_directory=str(repository))
assert Path(repo.directory).resolve() == repository
result = bpy.ops.extensions.package_install_files(filepath=str(package), repo=repo.module,
                                                   enable_on_install=True)
assert result == {'FINISHED'}, result
module = 'bl_ext.release_qa.material_workflow_addon'
assert module in bpy.context.preferences.addons
assert hasattr(bpy.types.Scene, 'mw_handoff_receipt')
installed = repository / 'material_workflow_addon'
with zipfile.ZipFile(package) as archive:
    for name in archive.namelist():
        actual = installed / name
        assert actual.is_file(), name
        assert actual.read_bytes() == archive.read(name), name
bpy.ops.preferences.addon_disable(module=module)
assert module not in bpy.context.preferences.addons
assert not hasattr(bpy.types.Scene, 'mw_handoff_receipt')
report = {'ok': True, 'blender': bpy.app.version_string, 'installed_module': module,
          'zip_sha256': hashlib.sha256(package.read_bytes()).hexdigest(),
          'files_identical': True, 'enabled': True, 'disabled': True,
          'preferences_saved': False, 'scope': 'Background extension installation; visible GUI not exercised.'}
(output / 'result.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
print(json.dumps(report))
