"""Replay edits to one named Material Workflow projection pair."""
import math
from pathlib import Path
from struct import pack, unpack

from material_draft_handoff import name
from material_projection_handoff import require


PAIR_NUMBERS = ('extrusion', 'distance', 'ao_distance', 'curvature_radius')
MAX_FLOAT32 = float.fromhex('0x1.fffffep+127')


def pair_state(pair, scene):
    import bpy
    inputs = {key: getattr(pair, key) for key in PAIR_NUMBERS}
    size = scene.mwp_projection_size
    inputs.update(resolution=size, samples=scene.mwp_projection_samples, margin=min(12, max(2, size // 128)))
    return {'pair': {'target': pair.target.name if pair.target else None,
                     'sources': [row.object.name if row.object else None for row in pair.sources],
                     'uv_layer': pair.uv_layer, 'cage': pair.cage.name if pair.cage else None, 'settings': inputs},
            'confirmed': pair.confirmed, 'hit_rate': pair.hit_rate,
            'manifest': str(Path(bpy.path.abspath(pair.manifest)).resolve()) if pair.manifest else None}


def normalize_changes(changes):
    require(isinstance(changes, dict) and changes and set(changes) <= {'target', 'sources', 'uv_layer', 'cage', 'settings'},
            'INVALID_REQUEST', 'Changes require at least one of target, sources, uv_layer, cage or settings')
    for field in ('target', 'cage'):
        if field in changes:
            require(changes[field] is None or name(changes[field]), 'INVALID_REQUEST', f'{field} must be a mesh name or null')
    if 'uv_layer' in changes:
        require(changes['uv_layer'] == '' or name(changes['uv_layer']), 'INVALID_REQUEST',
                'uv_layer must be a UV layer name or an empty string')
    if 'sources' in changes:
        rows = changes['sources']
        require(isinstance(rows, list) and rows, 'INVALID_REQUEST', 'sources must contain source row edits')
        indices = set()
        for row in rows:
            require(isinstance(row, dict) and set(row) == {'index', 'expected', 'object'} and
                    type(row['index']) is int and row['index'] >= 0 and
                    all(row[key] is None or name(row[key]) for key in ('expected', 'object')),
                    'INVALID_REQUEST', 'Each source edit requires a nonnegative index, expected mesh and replacement mesh (names or null)')
            require(row['index'] not in indices, 'INVALID_REQUEST', 'A source row may only be edited once')
            indices.add(row['index'])
    if 'settings' in changes:
        settings = changes['settings']
        require(isinstance(settings, dict) and settings and set(settings) <= set(PAIR_NUMBERS),
                'INVALID_REQUEST', 'settings must contain per-pair extrusion, distance, ao_distance or curvature_radius')
        for field, value in settings.items():
            require(type(value) in (int, float) and 0 <= value <= MAX_FLOAT32 and math.isfinite(value),
                    'INVALID_REQUEST', f'{field} must be a finite nonnegative Blender float')
            settings[field] = unpack('<f', pack('<f', value))[0]
    return changes


def edit_args(args, changes=None):
    operation, target, sources, index = args.operation, args.target, args.high or [], args.source_index
    require(target is None or name(target), 'INVALID_REQUEST', 'Expected a mesh name in --target')
    require(all(name(item) for item in sources) and len(set(sources)) == len(sources),
            'INVALID_REQUEST', 'High mesh names must be non-empty and unique')
    require(index is None or (operation == 'remove-source' and index >= 0),
            'INVALID_REQUEST', '--source-index is a nonnegative row for remove-source')
    if operation == 'update':
        require(not sources and changes is not None, 'INVALID_REQUEST', 'Update requires --changes and does not take --high')
        require(args.pair_index is None or args.pair_index >= 0, 'INVALID_REQUEST', '--pair-index must be nonnegative')
        require(target is not None or args.pair_index is not None, 'INVALID_REQUEST',
                'Use --target for a named pair or --pair-index for a pair with no target')
        return {'operation': operation, 'target': target, 'pair_index': args.pair_index,
                'changes': normalize_changes(changes)}
    require(args.changes is None and args.pair_index is None, 'INVALID_REQUEST', '--changes and --pair-index require update')
    if operation == 'add':
        require(not sources or target, 'INVALID_REQUEST', 'Use --target with named high meshes, or omit both to replay the saved selection')
    else:
        require(target, 'INVALID_REQUEST', 'Name the existing pair with --target')
        if operation == 'remove-source':
            require(len(sources) <= 1 and (index is not None or sources), 'INVALID_REQUEST',
                    'Use --high or --source-index to identify the source row')
        elif operation == 'add-source':
            require(len(sources) <= 1, 'INVALID_REQUEST', 'Add one high mesh or an empty source row per action')
        else:
            require(not sources, 'INVALID_REQUEST', 'This operation does not take --high')
    return {'operation': operation, 'target': target, 'sources': sources, 'source_index': index}


def update_pair(scene, pair, prior, changes, mesh):
    stored = prior['pair']
    target = changes.get('target', stored['target'])
    cage = changes.get('cage', stored['cage'])
    uv_layer = changes.get('uv_layer', stored['uv_layer'])
    sources = list(stored['sources'])
    replacements = {}
    for row in changes.get('sources', []):
        index = row['index']
        require(index < len(sources) and sources[index] == row['expected'],
                'CONFLICT', f'Source row or its expected object differs at index {index}')
        if sources[index] != row['object']:
            sources[index] = row['object']
            replacements[index] = mesh(row['object']) if row['object'] is not None else None

    retargeted = target != stored['target']
    cage_changed = cage != stored['cage']
    uv_changed = uv_layer != stored['uv_layer']
    target_object = pair.target
    if retargeted:
        target_object = mesh(target) if target is not None else None
    if retargeted and target is not None:
        require(not any(other.target == target_object for other in scene.mwp_projection_pairs),
                'CONFLICT', f'Low mesh already has a pair: {target}')
    if uv_layer and 'uv_layer' in changes:
        require(target_object and uv_layer in target_object.data.uv_layers,
                'INVALID_REQUEST', 'Requested UV layer is absent on the resulting low mesh')
    cage_object = pair.cage
    if cage_changed:
        cage_object = mesh(cage) if cage is not None else None
    identity_changed = retargeted or cage_changed or bool(replacements)
    if identity_changed:
        objects = [item for item in [target, *sources, cage] if item is not None]
        require(len(objects) == len(set(objects)), 'CONFLICT', 'Low mesh, high sources and cage must be distinct')

    settings = {field: value for field, value in changes.get('settings', {}).items() if stored['settings'][field] != value}
    if retargeted:
        pair.target = target_object
    if cage_changed:
        pair.cage = cage_object
    if uv_changed:
        pair.uv_layer = uv_layer
    for index, replacement in replacements.items():
        pair.sources[index].object = replacement
    for field, value in settings.items():
        setattr(pair, field, value)
    binding_changed = identity_changed or uv_changed
    if binding_changed or settings:
        pair.manifest, pair.hit_rate = '', 0.0
    observed = pair_state(pair, scene)
    expected = {'target': target, 'sources': sources, 'cage': cage, 'uv_layer': uv_layer,
                'settings': {**stored['settings'], **settings}}
    require(observed['pair'] == expected and observed['confirmed'] == (prior['confirmed'] and not binding_changed),
            'VALIDATION_FAILED', 'Saved projection values or native confirmation state differ from the requested changes')


def apply_edit(bpy, scene, layer, edit, source, context):
    from material_projection import _object
    from material_projection_contract import normalize_params
    from material_workflow_addon.projection_ui import _pair

    def mesh(name):
        obj = _object(name, layer)
        require(obj.type == 'MESH', 'INVALID_REQUEST', f'Expected a mesh in this view layer: {name}')
        return obj

    def run(operator, **kwargs):
        status = operator(**kwargs)
        require(status == {'FINISHED'}, 'VALIDATION_FAILED', scene.mwp_projection_status or str(status))

    ops = bpy.ops.material_workflow
    operation, target_name, names = edit['operation'], edit['target'], edit.get('sources', [])
    if operation == 'add':
        if target_name is None:
            target = bpy.context.active_object
            require(target and target.type == 'MESH', 'CONFLICT', 'Saved selection needs an active low mesh')
            selected = [obj for obj in bpy.context.selected_objects if obj != target and obj.type == 'MESH']
        else:
            target = mesh(target_name)
            selected = [mesh(item) for item in names]
        require(target not in selected, 'INVALID_REQUEST', 'A low mesh cannot be its own high source')
        require(not any(p.target == target for p in scene.mwp_projection_pairs), 'CONFLICT', 'Low mesh already has a pair')
        with bpy.context.temp_override(active_object=target, object=target, selected_objects=[target, *selected]):
            run(ops.projection_add)
        pair = _pair(scene)
        require(pair.target == target and [row.object for row in pair.sources] == selected and not pair.confirmed,
                'VALIDATION_FAILED', 'Panel did not capture the requested low and high meshes')
        return target.name, None

    pair_index = edit.get('pair_index')
    if pair_index is None:
        matches = [i for i, pair in enumerate(scene.mwp_projection_pairs) if pair.target and pair.target.name == target_name]
        require(matches, 'NOT_FOUND', 'No saved projection pair for ' + target_name)
        require(len(matches) == 1, 'CONFLICT', 'Multiple pairs match ' + target_name)
        pair_index = matches[0]
    else:
        require(pair_index < len(scene.mwp_projection_pairs), 'CONFLICT', 'Saved projection pair index is out of range')
        current_target = scene.mwp_projection_pairs[pair_index].target
        require((current_target.name if current_target else None) == target_name,
                'CONFLICT', 'Saved projection pair index or its expected mesh differs')
    scene.mwp_projection_index = pair_index
    pair = _pair(scene)
    prior = pair_state(pair, scene)
    if operation == 'update':
        update_pair(scene, pair, prior, edit['changes'], mesh)
        return target_name, prior
    if operation == 'remove':
        run(ops.projection_remove)
        return target_name, prior
    if operation in ('add-source', 'remove-source'):
        if operation == 'add-source':
            obj = mesh(names[0]) if names else None
            if obj:
                require(obj not in (pair.target, pair.cage) and all(row.object != obj for row in pair.sources),
                        'CONFLICT', 'High source already occurs in this pair or matches its low mesh or cage')
            run(ops.projection_source, index=-1)
            if obj:
                pair.sources[-1].object = obj
        else:
            before = prior['pair']['sources']
            index = edit['source_index']
            if index is None:
                require(names[0] in before, 'NOT_FOUND', 'High source not found in this pair')
                require(before.count(names[0]) == 1, 'CONFLICT', 'Ambiguous source name; supply --source-index')
                index = before.index(names[0])
            require(index < len(before) and (not names or before[index] == names[0]),
                    'CONFLICT', 'Source row or its expected object differs')
            run(ops.projection_source, index=index)
        require(not pair.confirmed, 'VALIDATION_FAILED', 'Panel did not invalidate the changed pair')
        pair.manifest, pair.hit_rate = '', 0.0
    elif operation == 'confirm':
        target = mesh(target_name)
        require(pair.uv_layer in target.data.uv_layers, 'INVALID_REQUEST', 'Pair UV layer is absent on the low mesh')
        for row in pair.sources:
            require(row.object is not None, 'INVALID_REQUEST', 'Assign or remove empty high source rows first')
            mesh(row.object.name)
        if pair.cage:
            mesh(pair.cage.name)
        normalize_params({'file': source['file'], 'expected_sha256': source['expected_sha256'],
                          'context': context, 'pairs': [prior['pair']]})
        pair.confirmed = True
    else:
        pair.confirmed = False
    return target_name, prior
