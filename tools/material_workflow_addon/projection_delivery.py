"""Create an independent, relocatable set of verified projection maps."""
import json
import os
import shutil
import uuid
from pathlib import Path

from .projection_bake import _fail
from .projection_material import read_projection
from .procedural_bake import _sha


def _new_stage(target):
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        _fail('Delivery directory already exists: ' + str(target))
    stage = target.with_name(f'.{target.name}-{uuid.uuid4().hex}.partial')
    stage.mkdir()
    return stage


def _publish(stage, target):
    if target.exists():
        _fail('Delivery directory appeared during copying: ' + str(target))
    os.rename(stage, target)


def deliver_maps(manifests, target_dir):
    """Publish a directory of unchanged maps. Paths in the index are relative."""
    target = Path(target_dir).resolve()
    sources = [Path(path).resolve() for path in manifests]
    if not sources:
        _fail('Select at least one completed bake to deliver')
    stage = _new_stage(target)
    records, targets = [], set()
    for index, manifest in enumerate(sources, 1):
        report, files = read_projection(manifest)
        name = report['target']
        if name in targets:
            _fail('Multiple bakes supplied for target ' + name)
        targets.add(name)
        folder = stage / f'part-{index:02d}'
        folder.mkdir()
        for path in (*files.values(), manifest):
            filename = 'projection.json' if path == manifest else path.name
            shutil.copyfile(path, folder / filename)
        copied_manifest = folder / 'projection.json'
        copied, _ = read_projection(copied_manifest)
        if copied != report:
            _fail('Projection manifest changed during delivery: ' + name)
        records.append({'target': name, 'manifest': copied_manifest.relative_to(stage).as_posix()})
    (stage / 'maps.json').write_text(json.dumps({
        'schema': 'mw-projection-delivery-1.0', 'targets': records,
    }, indent=2, ensure_ascii=False), encoding='utf-8')
    _publish(stage, target)
    return target / 'maps.json'


def deliver_project(context, manifests, target_dir, *, set_relative_paths=None):
    """Save a separate Blend plus maps, without making the copy the live document.

    The optional callback remaps add-on properties while saving and returns
    a function that restores the live state.
    """
    import bpy

    if not bpy.data.filepath:
        _fail('Save the workfile first so relative file references can be resolved')
    if any((bpy.data.libraries, bpy.data.cache_files, bpy.data.movieclips,
            bpy.data.sounds, bpy.data.volumes)):
        _fail('Delivery requires a local project with no linked libraries, media or external simulation caches')
    target = Path(target_dir).resolve()
    stage = _new_stage(target)
    index = deliver_maps(manifests, stage / 'textures')
    delivered = json.loads(index.read_text(encoding='utf-8'))['targets']
    relocated_images, relocated_manifests = {}, {}
    for source, record in zip(manifests, delivered):
        source = Path(source).resolve()
        copied = index.parent / record['manifest']
        relocated_manifests[source] = '//' + copied.relative_to(stage).as_posix()
        _, originals = read_projection(source)
        for path in originals.values():
            relocated_images[path.resolve()] = copied.with_name(path.name)

    originals = {}
    restore_properties = None
    candidate = stage / Path(bpy.data.filepath).name
    try:
        for image in bpy.data.images:
            if not image.users or image.packed_file or image.source in {'GENERATED', 'VIEWER'}:
                continue
            if image.source != 'FILE':
                _fail('Unsupported external image type: ' + image.name)
            source = Path(bpy.path.abspath(image.filepath, library=image.library)).resolve()
            if not source.is_file() or image.is_dirty:
                _fail('Save or pack the changed image before delivery: ' + image.name)
            if source not in relocated_images:
                file_hash = _sha(source)
                assets = stage / 'textures' / 'existing'
                assets.mkdir(exist_ok=True)
                copied = assets / (file_hash[:16] + source.suffix.lower())
                if not copied.exists():
                    shutil.copyfile(source, copied)
                if _sha(copied) != file_hash:
                    _fail('Image changed while copying: ' + image.name)
                relocated_images[source] = copied
            originals[image] = image.filepath
            image.filepath = '//' + relocated_images[source].relative_to(stage).as_posix()
        if set_relative_paths is not None:
            restore_properties = set_relative_paths(relocated_manifests)
        status = bpy.ops.wm.save_as_mainfile(filepath=str(candidate), copy=True, relative_remap=False)
        if status != {'FINISHED'} or not candidate.is_file():
            _fail('Could not write the Blender delivery copy')
    finally:
        for image, filepath in originals.items():
            image.filepath = filepath
        if restore_properties:
            restore_properties()
    _publish(stage, target)
    return target / candidate.name
