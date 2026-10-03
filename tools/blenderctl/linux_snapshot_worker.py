# SPDX-License-Identifier: GPL-3.0-or-later
"""Blender-only static-resource rebaser for the explicit Linux snapshot importer.

Never open or save the original .blend. Original paths only identify declared
resource copies; they are resolved lexically against the ORIGINAL source base.
"""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bpy
from filesystem import FileGuard
from protocol import Failure, digest, read_json

RESOURCE_KEY = 'blenderctl_image_manifest'


def reference_path(raw, source):
    if (not isinstance(raw, str) or not raw or '\x00' in raw or '\\' in raw
            or any(ord(c) < 32 for c in raw) or any(t in raw.upper() for t in ('<UDIM>', '<UVTILE>'))):
        raise Failure('UNSUPPORTED', 'Malformed, dynamic, or non-Linux external resource path')
    if raw.startswith('//'):
        # Blender-relative // is NOT a POSIX network path. Never call resolve()
        # here: the original namespace is intentionally outside our protection.
        value = os.path.normpath(os.path.join(str(Path(source).parent), raw[2:]))
    elif raw.startswith('/'):
        if raw != os.path.normpath(raw):
            raise Failure('UNSUPPORTED', 'Aliased absolute resource path')
        value = raw
    else:
        raise Failure('UNSUPPORTED', 'Resource path must be absolute or Blender-relative //')
    return value


def all_ids():
    seen = set()
    for prop in bpy.data.bl_rna.properties:
        if prop.type != 'COLLECTION':
            continue
        for item in getattr(bpy.data, prop.identifier, ()):
            if isinstance(item, bpy.types.ID) and item.as_pointer() not in seen:
                seen.add(item.as_pointer()); yield item
                tree = getattr(item, 'node_tree', None)
                if tree is not None and tree.as_pointer() not in seen:
                    seen.add(tree.as_pointer()); yield tree


def reject_dynamic_dependencies():
    if bpy.data.libraries:
        raise Failure('UNSUPPORTED', 'Linked libraries cannot be imported as a static snapshot')
    if any(len(getattr(bpy.data, name, ())) for name in ('sounds', 'movieclips', 'cache_files', 'volumes')):
        raise Failure('UNSUPPORTED', 'Media/cache/volume dependencies require another adapter')
    for item in all_ids():
        if item.library or item.override_library:
            raise Failure('UNSUPPORTED', 'Linked or overridden datablocks require another adapter')
        animation = getattr(item, 'animation_data', None)
        if animation and animation.drivers:
            raise Failure('UNSUPPORTED', 'Drivers are not static snapshot dependencies')
        if isinstance(item, bpy.types.NodeTree):
            for node in item.nodes:
                if (node.bl_idname in ('ShaderNodeScript', 'GeometryNodeBake', 'GeometryNodeSimulationInput', 'GeometryNodeSimulationOutput')
                        or any(word in node.bl_idname for word in ('Import', 'FileOutput'))):
                    raise Failure('UNSUPPORTED', 'External/script/cache node requires another adapter: ' + node.bl_idname)
    for scene in bpy.data.scenes:
        editor = scene.sequence_editor
        strips = getattr(editor, 'strips', getattr(editor, 'sequences', ())) if editor else ()
        if scene.rigidbody_world or len(strips):
            raise Failure('UNSUPPORTED', 'Physics/sequencer dependencies are outside static snapshots')
    for obj in bpy.data.objects:
        if obj.particle_systems:
            raise Failure('UNSUPPORTED', 'Particle dependencies are outside static snapshots')
        for modifier in obj.modifiers:
            if modifier.type in ('CLOTH', 'SOFT_BODY', 'FLUID', 'MESH_CACHE', 'MESH_SEQUENCE_CACHE', 'DYNAMIC_PAINT', 'NODES'):
                raise Failure('UNSUPPORTED', 'Dynamic/cache/geometry-node modifier requires another adapter: ' + modifier.type)
    for text in bpy.data.texts:
        if text.filepath or text.use_module:
            raise Failure('UNSUPPORTED', 'External or registered text scripts are outside static snapshots')


def static_references(source, resources):
    result = []
    for image in bpy.data.images:
        if image.type in ('RENDER_RESULT', 'COMPOSITING'):
            continue
        if image.source not in ('FILE', 'GENERATED'):
            raise Failure('UNSUPPORTED', 'Only static FILE or GENERATED images are supported')
        if image.source == 'GENERATED' or image.packed_file:
            continue
        path = reference_path(image.filepath, source)
        if path not in resources:
            raise Failure('INVALID_REQUEST', 'Undeclared external image: ' + path)
        if image.get(RESOURCE_KEY):
            try:
                members = json.loads(image[RESOURCE_KEY])
                if (not isinstance(members, list) or len(members) != 1 or members[0].get('number') != 0
                        or reference_path(members[0]['file'], source) != path
                        or members[0].get('expected_sha256') != resources[path]['expected_sha256']):
                    raise ValueError('stored member differs')
            except (ValueError, TypeError, KeyError) as exc:
                raise Failure('CONFLICT', 'Stored static image descriptor differs') from exc
        result.append((image, path, 'Image'))
    for font in bpy.data.fonts:
        if font.filepath == '<builtin>' or font.packed_file:
            continue
        path = reference_path(font.filepath, source)
        if path not in resources:
            raise Failure('INVALID_REQUEST', 'Undeclared external font: ' + path)
        result.append((font, path, 'Font'))
    # Blender's own external-path inventory catches additional datablock kinds.
    # Nothing besides explicitly mapped static image/font paths may escape.
    allowed = {path for _, path, _ in result}
    for raw in bpy.utils.blend_paths(absolute=False, packed=False, local=True):
        if raw in ('', '<builtin>'):
            continue
        if reference_path(raw, source) not in allowed:
            raise Failure('UNSUPPORTED', 'Unhandled external dependency: ' + raw)
    return result


def run(plan):
    copied, snapshot = plan['source_copy'], Path(plan['snapshot'])
    source, resources = plan['original_source'], plan['resources']
    if snapshot.exists() or snapshot == Path(copied['file']) or snapshot == Path(source):
        raise Failure('CONFLICT', 'Snapshot output must be separate and new')
    with ExitStack() as stack:
        for row in [copied, *resources.values()]:
            guard = stack.enter_context(FileGuard(row['file']))
            if guard.sha256() != row['expected_sha256']:
                raise Failure('CONFLICT', 'Staged input does not match declared hash')
        bpy.ops.wm.open_mainfile(filepath=copied['file'], load_ui=False, use_scripts=False)
        if tuple(bpy.data.version[:2]) != tuple(bpy.app.version[:2]):
            raise Failure('UNSUPPORTED', 'Source and worker require matching Blender major/minor versions')
        reject_dynamic_dependencies()
        references = static_references(source, resources)
        rebound = []
        for item, original, kind in references:
            target = resources[original]
            path = Path(target['file'])
            if not path.is_relative_to(snapshot.parent):
                raise Failure('CONFLICT', 'Staged resource lies outside snapshot directory')
            relative = '//' + os.path.relpath(path, snapshot.parent)
            item.filepath = relative
            if kind == 'Image' and RESOURCE_KEY in item:
                item[RESOURCE_KEY] = json.dumps([{'file': str(path), 'expected_sha256': target['expected_sha256'], 'number': 0}])
            # Retain otherwise unused source resources across save/reopen.
            if item.users == 0:
                item.use_fake_user = True
            rebound.append({'kind': kind, 'name': item.name, 'original': original,
                            'snapshot_path': relative, 'file': str(path), 'expected_sha256': target['expected_sha256']})
        # Copy=False updates Main's base ONLY to a separate, never-existing file.
        # Relative paths already use that base, so disable automatic remapping.
        bpy.context.preferences.filepaths.save_version = 0
        result = bpy.ops.wm.save_as_mainfile(filepath=str(snapshot), copy=False, relative_remap=False, check_existing=False)
        if 'FINISHED' not in result:
            raise Failure('WORKER_FAILED', 'Blender did not save snapshot')
        snapshot_guard = stack.enter_context(FileGuard(snapshot))
        snapshot_sha256 = snapshot_guard.sha256()
        bpy.ops.wm.open_mainfile(filepath=str(snapshot), load_ui=False, use_scripts=False)
        reject_dynamic_dependencies()
        new_mapping = {r['file']: r for r in resources.values()}
        observed = static_references(str(snapshot), new_mapping)
        wanted = {(r['kind'], r['name'], r['file']) for r in rebound}
        actual = {(kind, item.name, path) for item, path, kind in observed}
        if actual != wanted:
            raise Failure('VALIDATION_FAILED', 'Snapshot dependency set changed after reopen')
        for item, path, _ in observed:
            if not item.filepath.startswith('//') or digest(path) != new_mapping[path]['expected_sha256']:
                raise Failure('CONFLICT', 'Snapshot references are not verified relative dependencies')
        if snapshot_guard.sha256() != snapshot_sha256:
            raise Failure('CONFLICT', 'Snapshot changed during verification')
        for row in [copied, *resources.values()]:
            if digest(row['file']) != row['expected_sha256']:
                raise Failure('CONFLICT', 'Staged input changed during rebasing')
    return {'ok': True, 'rebound': rebound, 'snapshot_sha256': snapshot_sha256,
            'blender': bpy.app.version_string, 'saved_reopen_verified': True}


def main():
    plan_file = Path(sys.argv[sys.argv.index('--') + 1])
    report_file = plan_file.parent / 'worker-report.json'
    try:
        report = run(read_json(plan_file))
    except Exception as exc:
        report = {'ok': False, 'error': {'code': exc.code if isinstance(exc, Failure) else 'WORKER_FAILED', 'message': str(exc)}}
    with report_file.open('x', encoding='utf-8') as stream:
        json.dump(report, stream, indent=2, allow_nan=False)
    if report['ok'] is not True:
        raise RuntimeError(report['error']['message'])


if __name__ == '__main__':
    main()
