"""Persistent content witnesses and explicit external resources for native VSE.

No lighting-addon dependency. UI/runtime caches are intentionally omitted.
Snapshots with no exclusions are full saved expectations. Operation comparison
may admit new IDs, but always compares every pre-existing record exactly.
"""
from array import array
import hashlib
import json
import math
import os
from pathlib import Path

from .contract import WorkflowError

META = {'_vwe_workflow', '_vwe_timeline', '_vwe_version'}
SESSION = {'rna_type', 'id_data', 'tag', 'is_updated', 'is_updated_data',
           'is_evaluated', 'original', 'users', 'session_uid', 'name_full',
           'select', 'select_left_handle', 'select_right_handle',
           'frame_current', 'frame_subframe', 'animation_data', 'sequence_editor'}
TIMELINE_FIELDS = {'frame_start', 'frame_end'}
RENDER_FIELDS = {'fps', 'fps_base', 'resolution_x', 'resolution_y', 'resolution_percentage'}


def _bpy():
    import bpy
    return bpy


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        allow_nan=False, separators=(',', ':')).encode('utf-8')).hexdigest()


def _absolute(filename, library=None):
    return os.path.normpath(os.path.abspath(_bpy().path.abspath(filename, library=library)))


def _identity(value):
    return {'type': value.bl_rna.identifier, 'name': value.name,
            'library': _absolute(value.library.filepath) if value.library else None}


def _scalar(value):
    bpy = _bpy()
    if isinstance(value, bpy.types.ID):
        return _identity(value)
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else {'nonfinite': str(value)}
    if isinstance(value, set):
        return sorted(value)
    if hasattr(value, 'keys'):
        return {str(k): _scalar(value[k]) for k in value.keys()}
    return [_scalar(v) for v in value]


def _rna(value, depth=2, exclude=()):
    bpy = _bpy()
    result = {}
    ignored = SESSION | set(exclude)
    if isinstance(value, bpy.types.RenderSettings) and value.threads_mode == 'AUTO':
        # Effective CPU count is host/session state in AUTO, not authored data.
        # FIXED mode and its explicit count remain fully protected.
        ignored.add('threads')
    for prop in value.bl_rna.properties:
        name = prop.identifier
        if name in ignored or prop.is_readonly:
            continue
        raw = getattr(value, name)
        if prop.type in ('BOOLEAN', 'INT', 'FLOAT', 'STRING', 'ENUM'):
            result[name] = _scalar(raw)
            if prop.type == 'STRING' and prop.subtype in ('FILE_PATH', 'DIR_PATH') and raw and not raw.startswith('<'):
                owner = getattr(value, 'id_data', None)
                result[name] = _absolute(raw, getattr(owner, 'library', None))
        elif prop.type == 'POINTER' and isinstance(raw, bpy.types.ID):
            result[name] = _identity(raw)
        elif depth and prop.type == 'POINTER' and raw is not None:
            result[name] = _rna(raw, depth - 1)
        elif depth and prop.type == 'COLLECTION':
            result[name] = [_rna(v, depth - 1) for v in raw]
    for name in ('color_ramp', 'mapping', 'texture_mapping', 'color_mapping', 'dof', 'stereo'):
        raw = getattr(value, name, None)
        if depth and raw is not None and name not in result:
            result[name] = _rna(raw, depth - 1)
    return result


def _properties(value):
    return {str(k): _scalar(value[k]) for k in value.keys() if k != '_RNA_UI'}


def _curve(value):
    return {'path': value.data_path, 'index': value.array_index,
            'settings': _rna(value), 'keys': [_rna(k) for k in value.keyframe_points],
            'samples': [_rna(k) for k in value.sampled_points],
            'modifiers': [_rna(m) for m in value.modifiers]}


def _curves(action):
    for layer in action.layers:
        for strip in layer.strips:
            for bag in getattr(strip, 'channelbags', []):
                yield from bag.fcurves


def _action(action, excluded_paths=()):
    def retained(curve):
        return not any(curve.data_path == p or curve.data_path.startswith(p + '.') or
                       curve.data_path.startswith(p + '[') for p in excluded_paths)
    layers = []
    for layer in action.layers:
        strips = []
        for strip in layer.strips:
            bags = [{'slot': bag.slot_handle, 'curves': [_curve(c) for c in bag.fcurves if retained(c)]}
                    for bag in getattr(strip, 'channelbags', [])]
            strips.append({'type': strip.type, 'bags': [b for b in bags if b['curves']]})
        layers.append({'name': layer.name, 'strips': strips})
    return {'slots': [_rna(s) for s in action.slots], 'layers': layers}


def _strip(strip, excluded):
    if strip.name in excluded:
        return None
    result = {'type': strip.type, 'name': strip.name, 'settings': _rna(strip, 3),
              'custom_properties': _properties(strip),
              'frame_final_start': int(strip.frame_final_start),
              'frame_final_end': int(strip.frame_final_end)}
    if strip.type == 'IMAGE':
        result['image_members'] = [_absolute(os.path.join(strip.directory, e.filename)) for e in strip.elements]
    children = getattr(strip, 'strips', None)
    if children is not None:
        result['children'] = [row for child in children if (row := _strip(child, excluded)) is not None]
    for field in ('transform', 'crop'):
        if hasattr(strip, field):
            result[field] = _rna(getattr(strip, field))
    result['modifiers'] = [_rna(m, 3) for m in strip.modifiers]
    if hasattr(strip, 'retiming_keys'):
        result['retiming'] = [_rna(k) for k in strip.retiming_keys]
    return result


def native_strip_record(strip, exclude_metadata=True):
    """Stable strip content including transform/crop/modifiers and children.

    Baseline helpers omit all reserved _vwe_* custom metadata by default to
    avoid hashing the baseline stored inside itself; arbitrary user keys stay.
    """
    result = _strip(strip, ())
    def clean(row):
        if exclude_metadata:
            row['custom_properties'] = {k: v for k, v in row['custom_properties'].items()
                                        if not k.startswith('_vwe_')}
        for child in row.get('children', []):
            clean(child)
    clean(result)
    return json.loads(json.dumps(result, allow_nan=False))


def _record(item, target=None, excluded=(), excluded_paths=()):
    bpy = _bpy()
    row = {**_identity(item), 'fake_user': item.use_fake_user,
           'custom_properties': _properties(item), 'settings': _rna(item)}
    if isinstance(item, bpy.types.Object):
        row['object'] = {'data': _identity(item.data) if item.data else None,
            'parent': _identity(item.parent) if item.parent else None,
            'parent_type': item.parent_type, 'parent_bone': item.parent_bone,
            'matrix_basis': _scalar(item.matrix_basis),
            'matrix_parent_inverse': _scalar(item.matrix_parent_inverse),
            'rotation_mode': item.rotation_mode, 'hide_render': item.hide_render,
            'hide_viewport': item.hide_viewport,
            'modifiers': [_rna(m, 3) for m in item.modifiers],
            'constraints': [_rna(c, 3) for c in item.constraints],
            'groups': [g.name for g in item.vertex_groups],
            'collections': sorted(c.name for c in item.users_collection)}
        if item.pose:
            row['pose'] = [{'name': p.name, 'matrix_basis': _scalar(p.matrix_basis),
                           'constraints': [_rna(c, 3) for c in p.constraints]} for p in item.pose.bones]
    if isinstance(item, bpy.types.Mesh):
        hashes = {}
        def field(name, collection, prop, width, kind):
            values = array(kind, [0]) * (len(collection) * width)
            collection.foreach_get(prop, values)
            hashes[name] = hashlib.sha256(values.tobytes()).hexdigest()
        field('positions', item.vertices, 'co', 3, 'f')
        field('edges', item.edges, 'vertices', 2, 'i')
        field('loops', item.loops, 'vertex_index', 1, 'i')
        for prop in ('loop_start', 'loop_total', 'material_index'):
            field(prop, item.polygons, prop, 1, 'i')
        field('smooth', item.polygons, 'use_smooth', 1, 'b')
        for uv in item.uv_layers:
            field('uv:' + uv.name, uv.data, 'uv', 2, 'f')
        if item.shape_keys:
            for block in item.shape_keys.key_blocks:
                field('shape:' + block.name, block.data, 'co', 3, 'f')
            row['shape_settings'] = [_rna(b) for b in item.shape_keys.key_blocks]
        row['mesh'] = {'hashes': hashes,
            'materials': [_identity(m) if m else None for m in item.materials],
            'weights': [[(g.group, g.weight) for g in v.groups] for v in item.vertices],
            'attributes': [{'name': a.name, 'domain': a.domain, 'type': a.data_type,
                            'sha256': _digest([_rna(v) for v in a.data])} for a in item.attributes]}
    if isinstance(item, bpy.types.Collection):
        row['members'] = sorted(o.name for o in item.objects)
        row['children'] = sorted(c.name for c in item.children)
    if isinstance(item, bpy.types.Text):
        row['text_body'] = item.as_string()
    if isinstance(item, bpy.types.Curve):
        row['splines'] = [{'settings': _rna(s), 'points': [_rna(p) for p in s.points],
                           'bezier_points': [_rna(p) for p in s.bezier_points]} for s in item.splines]
    if isinstance(item, bpy.types.Armature):
        row['bones'] = [{'name': b.name, 'parent': b.parent.name if b.parent else None,
                          'matrix_local': _scalar(b.matrix_local),
                          'head_local': _scalar(b.head_local), 'tail_local': _scalar(b.tail_local),
                          'settings': _rna(b), 'custom_properties': _properties(b)} for b in item.bones]
    packed = getattr(item, 'packed_file', None)
    if packed:
        row['packed_sha256'] = hashlib.sha256(packed.data).hexdigest()
    if isinstance(item, bpy.types.Image) and len(item.packed_files):
        row['packed_images'] = [{'filepath': _absolute(p.filepath, item.library),
                                 'sha256': hashlib.sha256(p.packed_file.data).hexdigest()} for p in item.packed_files]
    tree = item if isinstance(item, bpy.types.NodeTree) else getattr(item, 'node_tree', None)
    if tree:
        row['nodes'] = {'nodes': [{'name': n.name, 'type': n.bl_idname,
            'settings': _rna(n, 3), 'custom_properties': _properties(n),
            'inputs': [{'id': s.identifier, 'value': _scalar(s.default_value) if hasattr(s, 'default_value') else None} for s in n.inputs],
            'outputs': [{'id': s.identifier, 'value': _scalar(s.default_value) if hasattr(s, 'default_value') else None} for s in n.outputs]}
            for n in tree.nodes], 'links': sorted((l.from_node.name, l.from_socket.identifier,
                l.to_node.name, l.to_socket.identifier) for l in tree.links)}
    private_action = target and getattr(getattr(target, 'animation_data', None), 'action', None)
    action_paths = excluded_paths if item == private_action and item.users == 1 else ()
    if isinstance(item, bpy.types.Action):
        if action_paths and not item.use_frame_range:
            # Without an explicit range this is derived from all FCurves,
            # including the authored targets excluded from this witness.
            # Retained curves and explicit frame_start/end remain protected.
            row['settings'].pop('frame_range', None)
        row['action'] = _action(item, action_paths)
    animation = getattr(item, 'animation_data', None)
    if animation:
        action = animation.action
        hidden_action = item == target and action and action.users == 1 and excluded_paths and not any(
            not any(c.data_path == p or c.data_path.startswith(p + '.') or c.data_path.startswith(p + '[')
                    for p in excluded_paths) for c in _curves(action))
        row['animation'] = {'action': _identity(action) if action and not hidden_action else None,
            'slot': animation.action_slot.identifier if animation.action_slot and not hidden_action else None,
            'settings': _rna(animation, 0),
            'drivers': [{**_curve(c), 'driver': _rna(c.driver, 3)} for c in animation.drivers
                        if item != target or not any(c.data_path.startswith(p + '.') for p in excluded_paths)],
            'nla': [_rna(t, 4) for t in animation.nla_tracks]}
        if hidden_action:
            row['animation']['settings'].pop('action', None)
            row['animation']['settings'].pop('action_slot', None)
    if isinstance(item, bpy.types.Scene):
        partial = item == target and bool(excluded)
        row['settings'] = _rna(item, 2, TIMELINE_FIELDS if partial else ())
        if partial:
            for key in META:
                row['custom_properties'].pop(key, None)
        row['scene'] = {'objects': sorted(o.name for o in item.objects),
            'collections': sorted(c.name for c in item.collection.children),
            'world': _identity(item.world) if item.world else None,
            'render': _rna(item.render, 3, RENDER_FIELDS if partial else ()),
            'view': _rna(item.view_settings, 3), 'display': _rna(item.display_settings, 3),
            'view_layers': [_rna(v, 3) for v in item.view_layers]}
        # Remove the generic duplicate render pointer so all exceptions are exact.
        row['settings'].pop('render', None)
        if item.sequence_editor:
            row['scene']['strips'] = [value for s in item.sequence_editor.strips
                                     if (value := _strip(s, excluded if item == target else ())) is not None]
            channels = [{'index': index, **_rna(c)} for index, c in enumerate(item.sequence_editor.channels, 1)]
            # Adding a strip may grow native default channels. Partial operation
            # witnesses protect all user channel settings without treating that
            # harmless allocation as a mutation of unrelated content.
            if partial:
                channels = [c for c in channels if c.get('name') != 'Channel ' + str(c['index'])
                            or c.get('mute', False) or c.get('lock', False)]
            row['scene']['channels'] = channels
        else:
            row['scene']['strips'] = []
            row['scene']['channels'] = []
    return row


def snapshot_protection(scene_name, excluded_names=None, excluded_paths=None):
    """Stable JSON witness. '*' is for a newly created target scene only.

    Explicit names exclude target VSE strips and _vwe_internal descendants,
    their private fcurves, workflow metadata and enumerated timeline settings.
    None/[] exclude nothing and are suitable for full saved expectations.
    """
    bpy = _bpy()
    excluded = set(excluded_names or ())
    whole_scene = '*' in excluded
    if whole_scene and excluded != {'*'}:
        raise WorkflowError('INVALID_REQUEST', "'*' cannot be mixed with strip exclusions")
    target = bpy.data.scenes.get(scene_name)
    if target is None and not whole_scene:
        raise WorkflowError('NOT_FOUND', 'Scene not found: ' + scene_name)
    # Keep the original target paths after a strip is deleted or renamed.
    # Deriving them only from surviving strips would reintroduce its private
    # Action into the after witness and report an unrelated mutation.
    paths = list(excluded_paths or ())
    if target and target.sequence_editor and not whole_scene:
        ids = {s.get('_vwe_clip_id') for s in target.sequence_editor.strips_all if s.name in excluded}
        for strip in target.sequence_editor.strips_all:
            if strip.name in excluded or (strip.get('_vwe_internal') and strip.get('_vwe_internal') in ids):
                excluded.add(strip.name)
                paths.append(strip.path_from_id())
    private_action = getattr(getattr(target, 'animation_data', None), 'action', None)
    omitted_action = None
    if private_action and private_action.users == 1:
        if whole_scene or (paths and all(any(c.data_path == p or c.data_path.startswith(p + '.') or c.data_path.startswith(p + '[')
                    for p in paths) for c in _curves(private_action))):
            omitted_action = private_action
    result = {'version': '1.0', 'target_scene': scene_name, 'records': {},
              'coverage': 'Persistent RNA, raw object transforms, mesh/UV/weights/shape/attributes, nodes, layered actions, NLA/drivers, collections, scenes and VSE; excludes session/UI/runtime caches; not arbitrary add-on external side effects.'}
    seen = set()
    for prop in bpy.data.bl_rna.properties:
        if prop.type != 'COLLECTION':
            continue
        for item in getattr(bpy.data, prop.identifier):
            if not isinstance(item, bpy.types.ID) or item.as_pointer() in seen:
                continue
            seen.add(item.as_pointer())
            if item.bl_rna.identifier in ('Screen', 'WorkSpace', 'WindowManager'):
                continue
            if item == omitted_action or (whole_scene and item == target):
                continue
            if isinstance(item, bpy.types.Image) and item.name in ('Render Result', 'Viewer Node'):
                continue
            key = json.dumps(_identity(item), sort_keys=True, ensure_ascii=False)
            result['records'][key] = _record(item, target, excluded if not whole_scene else (), paths)
    # Fail if any accidental opaque native value escaped the serializer.
    return json.loads(json.dumps(result, allow_nan=False))


def compare_protection(before, after):
    """Operation check permits newly allocated IDs; full expectation uses ==."""
    differences = []
    for key, record in before['records'].items():
        if key not in after['records']:
            differences.append({'record': key, 'reason': 'missing'})
        elif record != after['records'][key]:
            differences.append({'record': key, 'reason': 'changed'})
    return {'status': 'fail' if differences else 'pass', 'differences': differences,
            'before_sha256': _digest(before), 'after_sha256': _digest(after),
            'new_records': sorted(set(after['records']) - set(before['records']))}


def resource_paths():
    """Enumerate real native file references; dynamic unresolved sources fail.

    VSE image members are explicit, not inferred from a filename pattern.
    Packed IDs and internal builtin fonts have no external dependency.
    """
    bpy = _bpy()
    paths = set()
    def add(filename, library=None):
        if filename and not filename.startswith('<'):
            paths.add(_absolute(filename, library))
    def dynamic(message):
        raise WorkflowError('UNSUPPORTED', 'Unresolved dynamic resource: ' + message)
    for library in bpy.data.libraries:
        add(library.filepath, library.parent)
    for image in bpy.data.images:
        if image.source in ('GENERATED', 'VIEWER') or image.packed_file or len(image.packed_files):
            continue
        if image.source == 'SEQUENCE':
            dynamic('Image sequence datablock ' + image.name + ' requires explicit member closure')
        if image.source == 'TILED':
            for tile in image.tiles:
                filename = image.filepath.replace('<UDIM>', str(tile.number))
                if '<UVTILE>' in filename:
                    index = tile.number - 1001
                    filename = filename.replace('<UVTILE>', 'u' + str(index % 10 + 1) + '_v' + str(index // 10 + 1))
                if filename == image.filepath and len(image.tiles) > 1:
                    dynamic('UDIM image lacks tile token: ' + image.name)
                add(filename, image.library)
        else:
            add(image.filepath, image.library)
    for collection in (bpy.data.sounds, bpy.data.fonts):
        for item in collection:
            if not getattr(item, 'packed_file', None):
                add(item.filepath, item.library)
    for clip in bpy.data.movieclips:
        if clip.source == 'SEQUENCE':
            dynamic('MovieClip sequence ' + clip.name)
        add(clip.filepath, clip.library)
    for item in bpy.data.cache_files:
        if item.is_sequence:
            dynamic('CacheFile sequence ' + item.name)
        add(item.filepath, item.library)
    for item in bpy.data.volumes:
        if item.is_sequence:
            dynamic('Volume sequence ' + item.name)
        add(item.filepath, item.library)
    for scene in bpy.data.scenes:
        if scene.sequence_editor:
            for strip in scene.sequence_editor.strips_all:
                if getattr(strip, 'use_proxy', False):
                    proxy = getattr(strip, 'proxy', None)
                    if proxy is None:
                        dynamic('Enabled proxy has no descriptor: ' + strip.name)
                    if not proxy.use_proxy_custom_file:
                        # Generated native caches need their own complete member
                        # manifest; a directory alone does not prove closure.
                        dynamic('Enabled generated proxy needs explicit cache closure: ' + strip.name)
                    filename = proxy.filepath
                    if (not isinstance(filename, str) or not filename or filename.startswith('<')
                            or '<UDIM>' in filename or '<UVTILE>' in filename):
                        dynamic('Enabled custom proxy has unresolved filepath: ' + strip.name)
                    add(filename, scene.library)
                if strip.type == 'IMAGE':
                    for element in strip.elements:
                        add(os.path.join(strip.directory, element.filename), scene.library)
                elif strip.type == 'MOVIE':
                    add(strip.filepath, scene.library)
                elif strip.type == 'SOUND' and strip.sound and not strip.sound.packed_file:
                    add(strip.sound.filepath, strip.sound.library)
    trees = {t.as_pointer(): t for t in bpy.data.node_groups}
    for collection in (bpy.data.materials, bpy.data.worlds, bpy.data.lights, bpy.data.scenes):
        for item in collection:
            tree = getattr(item, 'node_tree', None)
            if tree:
                trees[tree.as_pointer()] = tree
    for tree in trees.values():
        for node in tree.nodes:
            if node.bl_idname == 'ShaderNodeScript' and node.mode == 'EXTERNAL':
                add(node.filepath, tree.library)
            for socket in node.inputs:
                prop = socket.bl_rna.properties.get('default_value')
                if prop and prop.type == 'STRING' and prop.subtype in ('FILE_PATH', 'DIR_PATH'):
                    if socket.is_linked:
                        dynamic('Linked path socket ' + tree.name + '/' + node.name + '/' + socket.identifier)
                    if prop.subtype == 'DIR_PATH' and socket.default_value:
                        dynamic('Directory import socket requires file closure: ' + node.name)
                    add(socket.default_value, tree.library)
            # Import nodes may expose ordinary strings rather than FILE_PATH RNA.
            if 'Import' in node.bl_idname:
                for socket in node.inputs:
                    if socket.name.lower() in ('path', 'file path', 'filename', 'directory'):
                        if socket.is_linked:
                            dynamic('Linked import path ' + node.name)
                        if isinstance(getattr(socket, 'default_value', None), str):
                            if socket.name.lower() == 'directory' and socket.default_value:
                                dynamic('Import directory ' + node.name)
                            add(socket.default_value, tree.library)
    return sorted(paths, key=lambda value: value.casefold())
