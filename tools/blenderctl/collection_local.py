"""Realize one static collection instance into private editable Blender data."""

from hashlib import sha256
from pathlib import Path

import bpy
from mathutils import Matrix

from inspection import identity
from protocol import Failure


_MODIFIERS = {
    'ARRAY', 'BEVEL', 'BOOLEAN', 'DECIMATE', 'EDGE_SPLIT', 'MIRROR',
    'NORMAL_EDIT', 'SMOOTH', 'SOLIDIFY', 'SUBSURF', 'TRIANGULATE',
    'WEIGHTED_NORMAL', 'WELD',
}


def _require(condition, code, message):
    if not condition:
        raise Failure(code, message)


def guard_library(operation, protections):
    file = operation.get('expected_library_file')
    expected = operation.get('expected_library_sha256')
    _require((file is None) == (expected is None), 'INVALID_REQUEST',
             'An external linked library requires both an absolute path and its SHA-256')
    if file is None:
        return None
    from filesystem import FileGuard

    path = Path(file)
    _require(path.is_absolute() and path.suffix.lower() == '.blend', 'INVALID_REQUEST',
             'expected_library_file must be an absolute .blend path')
    guard = protections.enter_context(FileGuard(path))
    _require(guard.sha256() == expected, 'CONFLICT', f'Instance library SHA differs: {path}')
    return {'file': str(guard.path.resolve()), 'sha256': expected, 'mode': 'external_link'}


def _library_reference(backing, verified):
    library = backing.library
    if library is None:
        _require(verified is None, 'CONFLICT', 'The instance backing collection is local')
        return None
    path = Path(bpy.path.abspath(library.filepath)).resolve()
    if getattr(backing, 'is_linked_packed', False) and getattr(library, 'is_archive', False):
        _require(verified is None, 'INVALID_REQUEST', 'Packed linked IDs are bound by the receiving project SHA, not a disk library hash')
        return {'origin_path': str(path), 'mode': 'embedded_archive', 'content_identity': 'receiving_project_sha256'}
    if library.packed_file is not None:
        _require(verified is None, 'INVALID_REQUEST', 'Packed libraries are bound by the receiving project SHA, not a disk library hash')
        return {'origin_path': str(path), 'mode': 'embedded_library',
                'embedded_sha256': sha256(library.packed_file.data).hexdigest()}
    _require(verified is not None, 'INVALID_REQUEST', 'An external linked library requires expected_library_file and expected_library_sha256')
    _require(Path(verified['file']) == path and not backing.is_missing, 'CONFLICT',
             f'Actual instance library differs from the verified file: {path}')
    return verified


def _check_custom_references(data):
    pending = list(data.values())
    while pending:
        value = pending.pop()
        _require(not isinstance(value, bpy.types.ID), 'UNSUPPORTED',
                 f'Custom property contains an ID reference: {getattr(data, "name", type(data).__name__)}')
        if hasattr(value, 'values'):
            pending.extend(value.values())
        elif isinstance(value, (tuple, list)):
            pending.extend(value)


def _editable_copy(data, backing):
    if data.library is not None:
        _require(data.library == backing.library and not data.is_missing and
                 (not getattr(backing, 'is_linked_packed', False) or getattr(data, 'is_linked_packed', False)),
                 'UNSUPPORTED', f'Asset needs data outside the verified source: {data.name}')
    _check_custom_references(data)
    copy = data.copy()
    _require(not copy.library and not copy.override_library, 'VALIDATION_FAILED',
             f'Blender did not create an independent local copy of {data.name}')
    if copy.asset_data is not None:
        copy.asset_clear()
    if 'asset_id' in copy:
        del copy['asset_id']
    database = {
        'Object': bpy.data.objects, 'Mesh': bpy.data.meshes, 'Material': bpy.data.materials,
        'ShaderNodeTree': bpy.data.node_groups, 'Image': bpy.data.images,
    }[copy.bl_rna.identifier]
    names = {item.name for item in database if item != copy}
    if copy.name in names:
        # Local and linked Blender IDs may share a name, but scene edit requests use names.
        index = 0
        while True:
            suffix = '.Local' if index == 0 else f'.Local.{index:03d}'
            prefix = data.name.encode('utf-8')[:63-len(suffix)].decode('utf-8', errors='ignore')
            name = prefix + suffix
            if name not in names:
                break
            index += 1
        copy.name = name
        _require(copy.name == name, 'VALIDATION_FAILED', f'Blender changed the new local name: {name}')
    return copy


def _copy_modifier_targets(source, target, objects):
    for original, copied in zip(source.modifiers, target.modifiers, strict=True):
        _require(original.type in _MODIFIERS, 'UNSUPPORTED',
                 f'Unsupported modifier {source.name}/{original.name} ({original.type})')
        for prop in original.bl_rna.properties:
            if prop.type != 'POINTER':
                continue
            value = getattr(original, prop.identifier)
            if isinstance(value, bpy.types.ID):
                _require(isinstance(value, bpy.types.Object) and value in objects and not prop.is_readonly,
                         'UNSUPPORTED', f'Modifier has an external or unsupported target: {source.name}/{original.name}')
                setattr(copied, prop.identifier, objects[value])


def _copy_surfaces(objects, backing):
    materials, groups, images = {}, {}, {}

    def copy_graph(graph):
        _require(not graph.animation_data, 'UNSUPPORTED', f'Animated shader graph: {graph.name}')
        _check_custom_references(graph)
        for node in graph.nodes:
            _check_custom_references(node)
            _require(not any(isinstance(getattr(socket, 'default_value', None), bpy.types.ID) for socket in node.inputs),
                     'UNSUPPORTED', f'Shader socket contains an ID reference: {graph.name}/{node.name}')
            for prop in node.bl_rna.properties:
                if prop.type != 'POINTER':
                    continue
                value = getattr(node, prop.identifier)
                if not isinstance(value, bpy.types.ID):
                    continue
                if isinstance(value, bpy.types.Image):
                    if value not in images:
                        _require(value.source == 'FILE' and bool(value.packed_files), 'UNSUPPORTED',
                                 f'Shader requires a packed file image: {value.name}')
                        image = _editable_copy(value, backing)
                        _require(_packed_shas(image) == _packed_shas(value), 'VALIDATION_FAILED',
                                 f'Blender did not preserve packed image data: {value.name}')
                        images[value] = image
                    replacement = images[value]
                elif isinstance(value, bpy.types.ShaderNodeTree):
                    if value not in groups:
                        groups[value] = _editable_copy(value, backing)
                        copy_graph(groups[value])
                    replacement = groups[value]
                else:
                    _require(isinstance(value, bpy.types.Object) and value in objects, 'UNSUPPORTED',
                             f'Shader has an external or unsupported target: {graph.name}/{node.name}')
                    replacement = objects[value]
                _require(not prop.is_readonly, 'UNSUPPORTED', f'Read-only shader reference: {graph.name}/{node.name}')
                setattr(node, prop.identifier, replacement)

    def copy_material(original):
        if original is None:
            return None
        if original not in materials:
            _require(not original.animation_data, 'UNSUPPORTED', f'Animated material: {original.name}')
            material = _editable_copy(original, backing)
            if material.node_tree:
                copy_graph(material.node_tree)
            materials[original] = material
        return materials[original]

    for source, target in objects.items():
        if source.type != 'MESH':
            continue
        # A material can remain referenced by the mesh beneath an Object slot.
        for index, original in enumerate(source.data.materials):
            target.data.materials[index] = copy_material(original)
        for slot, output in zip(source.material_slots, target.material_slots, strict=True):
            if slot.link == 'OBJECT':
                output.material = copy_material(slot.material)
    return {'materials': [item.name for item in materials.values()],
            'shader_groups': [item.name for item in groups.values()],
            'packed_images': {item.name: _packed_shas(item) for item in images.values()}}


def _packed_shas(image):
    return [sha256(member.packed_file.data).hexdigest() for member in image.packed_files]


def _close(before, after):
    return all(abs(a - b) <= 1e-5 + 1e-6 * max(abs(a), abs(b))
               for row_a, row_b in zip(before, after, strict=True)
               for a, b in zip(row_a, row_b, strict=True))


def _instance_matrices(anchor):
    graph = bpy.context.evaluated_depsgraph_get()
    return {row.object.original: row.matrix_world.copy() for row in graph.object_instances
            if row.is_instance and row.parent is not None and row.parent.original == anchor}


def _unreferenced_links(backing):
    library = backing.library
    if library is None:
        return []
    usage = bpy.data.user_map()
    dependents = {owner: set() for owner in usage if owner.library == library}
    for item, users in usage.items():
        if item.library == library:
            for owner in users & dependents.keys():
                dependents[owner].add(item)
    closure = set()
    pending = [backing]
    while pending:
        item = pending.pop()
        if item not in closure:
            closure.add(item)
            pending.extend(dependents.get(item, ()))
    while True:
        drop = {item for item in closure if usage.get(item, set()) <= closure}
        if drop == closure:
            break
        closure = drop
    if set(dependents) <= closure and not any(item.parent == library for item in bpy.data.libraries):
        closure.add(library)
        logical = getattr(library, 'archive_parent_library', None)
        if logical and not any(item.library == logical for item in usage) and not any(
                item != library and (item.parent == logical or getattr(item, 'archive_parent_library', None) == logical)
                for item in bpy.data.libraries):
            closure.add(logical)
    return sorted((identity(item) for item in closure), key=lambda row: (row['type'], row['name']))


def allow_unused_link_pruning(expected, observed, localizations):
    """Blender omits unreferenced linked IDs when saving a new Main."""
    allowed = {(row['type'], row['name'], row['library']) for result in localizations
               for row in result.pop('unused_links_before_save')}
    live_collections = {(row['name'], row['library']) for row in observed['collections']}
    for item in expected['objects']:
        if ('Object', item['name'], item['library']) in allowed:
            item['collections'] = [name for name in item['collections']
                                   if (name, item['library']) in live_collections
                                   or ('Collection', name, item['library']) not in allowed]
    for table, kind in (('datablocks', None), ('objects', 'Object'), ('collections', 'Collection')):
        def key(row):
            return kind or row['type'], row['name'], row['library']

        survivors = {key(row) for row in observed[table]}
        expected[table] = [row for row in expected[table] if key(row) in survivors or key(row) not in allowed]


def _require_independent_destination(destination):
    ancestors = {destination}
    while True:
        added = {collection for collection in bpy.data.collections if collection not in ancestors
                 and any(child in ancestors for child in collection.children)}
        if not added:
            break
        ancestors.update(added)
    if any(obj.instance_collection in ancestors for obj in bpy.data.objects if obj.instance_type == 'COLLECTION'):
        raise Failure('UNSUPPORTED', 'Destination collection or ancestor is itself used as an instance')


def realize(anchor, operation, verified_library):
    """Keep the local Empty as a transform parent and replace its instance only."""
    scene, layer = bpy.context.scene, bpy.context.view_layer
    backing = anchor.instance_collection
    _require(anchor.type == 'EMPTY' and anchor.instance_type == 'COLLECTION' and backing is not None,
             'CONFLICT', f'Object is not a collection instance: {anchor.name}')
    _require(backing.name == operation['expected_collection'], 'CONFLICT',
             f'Backing collection differs from expected_collection: {backing.name}')
    _require(len(anchor.users_collection) == 1 and anchor.users_scene == (scene,)
             and layer.objects.get(anchor.name) == anchor and anchor.visible_get(view_layer=layer)
             and not anchor.hide_render and not anchor.hide_viewport,
             'UNSUPPORTED', 'The instance must be visible in the requested view layer and bound to one local scene/collection')
    destination = anchor.users_collection[0]
    _require(not destination.library and not destination.override_library, 'UNSUPPORTED', 'Instance collection owner is linked')
    _require_independent_destination(destination)
    _require(not backing.override_library and not backing.children and not backing.hide_viewport and not backing.hide_render,
             'UNSUPPORTED', 'Realization requires a visible, single-level, non-override backing collection')
    library = _library_reference(backing, verified_library)
    parts = set(backing.objects)
    _require(bool(parts) and all(source.type in {'MESH', 'EMPTY'} for source in parts), 'UNSUPPORTED',
             'Backing collection must contain only mesh objects and ordinary Empty parents')
    for source in parts:
        _require(source.instance_type == 'NONE' and not source.override_library and not source.animation_data
                 and not source.constraints and not source.rigid_body and not source.rigid_body_constraint
                 and (source.parent is None or source.parent in parts and source.parent_type == 'OBJECT'),
                 'UNSUPPORTED', f'Unsupported dynamic, nested or externally parented object: {source.name}')
        if source.data is not None:
            _require(not source.data.override_library and not source.data.animation_data and not source.data.shape_keys,
                     'UNSUPPORTED', f'Unsupported dynamic or override mesh: {source.data.name}')

    before = _instance_matrices(anchor)
    _require(all(source in before for source in parts if source.type == 'MESH'), 'UNSUPPORTED',
             'Every mesh must evaluate in the selected collection instance')
    objects = {}
    for source in sorted(parts, key=lambda item: item.name):
        target = _editable_copy(source, backing)
        if source.data is not None:
            target.data = _editable_copy(source.data, backing)
        objects[source] = target
    surfaces = _copy_surfaces(objects, backing)
    root_offset = Matrix.Translation(-backing.instance_offset)
    for source, target in objects.items():
        target.parent = objects[source.parent] if source.parent else anchor
        target.matrix_parent_inverse = source.matrix_parent_inverse if source.parent else root_offset
        _copy_modifier_targets(source, target, objects)
        destination.objects.link(target)
    anchor.instance_type = 'NONE'
    anchor.instance_collection = None
    layer.update()
    _require(all(_close(matrix, objects[source].matrix_world) for source, matrix in before.items() if source in objects),
             'VALIDATION_FAILED', 'Realized object did not retain its evaluated instance transform')
    result = {'anchor': anchor.name, 'backing_collection': backing.name, 'backing_library': library,
              'unused_links_before_save': _unreferenced_links(backing),
              'parts': {source.name: {'object': target.name, 'mesh': target.data.name if target.data else None,
                                      'parent': target.parent.name}
                        for source, target in objects.items()}, **surfaces}
    verify_reopened(result)
    return result


def verify_reopened(result):
    anchor = bpy.data.objects.get(result['anchor'])
    _require(anchor is not None and anchor.instance_type == 'NONE' and anchor.instance_collection is None,
             'VALIDATION_FAILED', 'Original Empty remained a collection instance')
    owned = set()
    for row in result['parts'].values():
        item = bpy.data.objects.get(row['object'])
        _require(item is not None and not item.library and not item.override_library and item.parent is not None
                 and item.parent.name == row['parent'] and (row['mesh'] is None and item.data is None or item.data is not None
                 and item.data.name == row['mesh'] and not item.data.library and item.data.users == 1),
                 'VALIDATION_FAILED', f'Missing private local object or mesh after realization: {row["object"]}')
        owned.add(item)
        if item.data is not None:
            owned.add(item.data)
    for names, database in ((result['materials'], bpy.data.materials), (result['shader_groups'], bpy.data.node_groups)):
        for name in names:
            item = database.get(name)
            _require(item is not None and not item.library, 'VALIDATION_FAILED', f'Missing local shader data: {name}')
            owned.add(item)
    for name, expected in result['packed_images'].items():
        image = bpy.data.images.get(name)
        _require(image is not None and not image.library and _packed_shas(image) == expected,
                 'VALIDATION_FAILED', f'Missing or changed local packed image: {name}')
        owned.add(image)
    allowed = owned | {anchor}
    for dependency, users in bpy.data.user_map().items():
        _require(dependency in allowed or not users & owned, 'VALIDATION_FAILED',
                 f'Realized asset still refers to external datablock: {dependency.name}')
