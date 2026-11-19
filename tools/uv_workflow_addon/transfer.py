"""Read-only correspondence, followed by a protected new-candidate transaction."""
import copy
import json
import math
import numpy as np
from .contract import fail


def source_identity(obj, layer):
    from . import core
    if layer not in obj.data.uv_layers:
        fail('Source UV layer missing: ' + obj.name + '/' + layer, 'NOT_FOUND')
    return {'geometry': core.geometry(obj), 'mesh': obj.data.name,
            'uv_hash': core._uv_fingerprint(obj.data, layer)}


def target_identity(obj):
    from . import core
    return {'geometry': core.geometry(obj), 'mesh': obj.data.name,
            'protection': core._hash(core.protection([obj])),
            'active_uv': obj.data.uv_layers.active.name if obj.data.uv_layers.active else None}


def identities(objects):
    return {o.name: {'target': target_identity(o),
                     'sources': {u.name: source_identity(o, u.name) for u in o.data.uv_layers}}
            for o in objects}


def make_request(scene, view_layer, source, source_uv, targets, candidate, mode='INDEXED',
                 width=2048, height=2048, max_distance_m=.001, normal_degrees=5,
                 uv_tolerance_px=.01, pair_budget=2000000):
    from .contract import normalize
    if mode not in ('INDEXED', 'PROJECTED'):
        fail('Explicit correspondence must be supplied as a complete manifest')
    rows = {}
    for obj in targets:
        row = {'source_object': source.name, 'source_uv': source_uv,
               'source_binding': source_identity(source, source_uv),
               'target_binding': target_identity(obj), 'mode': mode}
        if mode == 'PROJECTED':
            row.update(regions=[{'source_faces': list(range(len(source.data.polygons))),
                                  'target_faces': list(range(len(obj.data.polygons)))}],
                       max_distance_m=max_distance_m, normal_degrees=normal_degrees,
                       uv_tolerance_px=uv_tolerance_px)
        rows[obj.name] = row
    return normalize({'version': '1.0', 'operation': 'transfer', 'scene': scene.name,
                      'view_layer': view_layer.name if hasattr(view_layer, 'name') else view_layer,
                      'objects': [o.name for o in targets], 'candidate': candidate,
                      'width': width, 'height': height, 'pair_budget': pair_budget,
                      'transfer': rows, 'transfer_unit_scale': scene.unit_settings.scale_length})


def _mesh(obj, unit):
    m = obj.data
    m.calc_loop_triangles()
    # Blender stores local coordinates and transforms in float32. Multiplying
    # them through mathutils rounds the world coordinates a second time, so
    # matching boundaries on parallel surfaces can acquire artificial gaps.
    local = np.asarray([tuple(p.co) for p in m.vertices], dtype=np.float64)
    matrix = np.asarray(obj.matrix_world, dtype=np.float64)
    world = (local @ matrix[:3, :3].T + matrix[:3, 3]) * unit
    return {'name': obj.name, 'positions': world.tolist(),
            'faces': [list(f.vertices) for f in m.polygons],
            'face_loops': [list(f.loop_indices) for f in m.polygons],
            'triangles': [{'face': t.polygon_index, 'loops': list(t.loops)} for t in m.loop_triangles]}


def _reject(code, message, details):
    fail(message + '; transfer_diagnostic=' + json.dumps(details, ensure_ascii=False, allow_nan=False), code)


def execute(scene, objects, state, v, unit, progress):
    from . import core, transfer_math
    from .consumers import inspect_objects
    from .editing import _pin_write
    if unit != v['transfer_unit_scale'] or not math.isfinite(unit) or unit <= 0:
        fail('Transfer scene unit scale changed; recapture the correspondence', 'CONFLICT')
    if v['candidate'] in state['candidates']:
        fail('Candidate already exists', 'CONFLICT')
    layer = 'UVW_' + v['candidate']
    sources = {}
    vl = scene.view_layers[v['view_layer']]
    for row in v['transfer'].values():
        source = vl.objects.get(row['source_object'])
        if not source or source.type != 'MESH':
            fail('Transfer source mesh missing: ' + row['source_object'], 'NOT_FOUND')
        if source.mode != 'OBJECT' or source.library or source.data.library or source.override_library:
            fail('Transfer sources require local Object Mode base meshes', 'UNSUPPORTED')
        if source_identity(source, row['source_uv']) != row['source_binding']:
            fail('Transfer source geometry, UV or pin identity changed: ' + source.name, 'CONFLICT')
        sources[source.name] = source
    for obj in objects:
        if obj.data.users != 1:
            fail('Shared target mesh requires an isolated working copy: ' + obj.name, 'UNSUPPORTED')
        if target_identity(obj) != v['transfer'][obj.name]['target_binding']:
            fail('Transfer target geometry, old UV, pins or render identity changed: ' + obj.name, 'CONFLICT')
        if layer in obj.data.uv_layers or len(obj.data.uv_layers) >= 8:
            fail('Transfer candidate layer collision or UV layer limit: ' + obj.name, 'CONFLICT')
    all_objects = list(sources.values()) + objects
    consumers = inspect_objects(all_objects)
    for obj in objects:
        row = v['transfer'][obj.name]
        source = sources[row['source_object']]
        for name, uv in [(source.name, row['source_uv']),
                         (obj.name, v['constraints'].get(obj.name, {}).get('source_uv') or row['target_binding']['active_uv'])]:
            result = consumers[name]
            if result.get('unresolved_udim') or uv in result['protected_udim_layers']:
                fail('Existing or unresolved UDIM layer is protected during transfer: ' + name, 'UNSUPPORTED')
    core._check_geometry(all_objects, unit)
    before = core.protection(all_objects)
    source_active = {o.name: o.data.uv_layers.active.name if o.data.uv_layers.active else None for o in sources.values()}
    active = {o.name: o.data.uv_layers.active.name if o.data.uv_layers.active else None for o in objects}
    cs = [core._constraints(o, v) for o in objects]
    meshes = {o.name: _mesh(o, unit) for o in all_objects}
    arrays, reports = {}, {}
    remaining = v['pair_budget']
    for obj in objects:
        progress('transfer_correspondence', object=obj.name)
        row = v['transfer'][obj.name]
        source = sources[row['source_object']]
        a, b, uv = meshes[source.name], meshes[obj.name], core.coordinates(source.data, row['source_uv'])
        try:
            if row['mode'] == 'INDEXED':
                mapping = transfer_math.indexed_map(a, b)
                array, report = transfer_math.topology(a, b, uv, mapping)
            elif row['mode'] == 'EXPLICIT':
                array, report = transfer_math.topology(a, b, uv, row['corner_map'], row['winding'],
                    vertex_domain=row.get('vertex_domain', 'ALL'))
            else:
                if remaining <= 0:
                    _reject('RESOURCE_LIMIT', 'Transfer correspondence budget exhausted', {'target': obj.name})
                array, report = transfer_math.surface(a, b, uv, row['regions'], v['width'], v['height'],
                    row['max_distance_m'], row['normal_degrees'], row['uv_tolerance_px'], remaining,
                    cancel=lambda: progress('transfer_correspondence', object=obj.name))
                remaining -= report['pair_checks']['consumed']
        except transfer_math.TransferError as exc:
            _reject(exc.code, str(exc), {'target': obj.name, 'source': source.name, 'mode': row['mode'],
                                         'details': getattr(exc, 'details', None)})
        array = np.asarray(array, dtype=np.float32)
        if array.shape != (len(obj.data.loops), 2) or not np.isfinite(array).all():
            fail('Transfer returned invalid target corner data', 'VALIDATION_FAILED')
        arrays[obj.name], reports[obj.name] = array, report
    # Everything above is read-only. Commit all targets together or restore the
    # new layers and Scene record; source UVs and target originals never change.
    previous = scene.get(core.STATE)
    created = []
    try:
        for obj, constraint in zip(objects, cs):
            uv = obj.data.uv_layers.new(name=layer, do_init=False)
            created.append(obj)
            if uv.name != layer:
                fail('Transfer candidate name changed', 'CONFLICT')
            core._write_uv(obj.data, layer, arrays[obj.name])
            _pin_write(obj.data, layer, sorted(map(int, constraint['fixed'])))
            if active[obj.name]:
                obj.data.uv_layers.active = obj.data.uv_layers[active[obj.name]]
        q = core._quality(objects, layer, v, cs, unit, progress)
        if q['constraints']['status'] != 'pass':
            _reject('CONFLICT', 'Transfer violates target hard constraints', q['constraints'])
        if core.protection(list(sources.values())) != {n: before[n] for n in sources} or core.protection(objects, [layer]) != {o.name: before[o.name] for o in objects}:
            fail('Transfer changed source or protected target data', 'VALIDATION_FAILED')
        if source_active != {o.name: o.data.uv_layers.active.name if o.data.uv_layers.active else None for o in sources.values()}:
            fail('Transfer changed the source active UV layer', 'VALIDATION_FAILED')
        if any(active[o.name] and o.data.uv_layers.active.name != active[o.name] for o in objects):
            fail('Transfer changed the target active UV layer', 'VALIDATION_FAILED')
        readonly = {'objects': sorted(sources), 'protection': {n: before[n] for n in sources}, 'active': source_active}
        record = {'objects': v['objects'], 'layer': layer, 'method': 'TRANSFER', 'metrics': q,
                  'geometry': {o.name: core.geometry(o) for o in objects},
                  'uv_hash': {o.name: core._uv_fingerprint(o.data, layer) for o in objects},
                  'meshes': {o.name: o.data.name for o in objects}, 'constraints': cs, 'manifest': v,
                  'previous_active': active, 'seam_plan': q['seams'],
                  'transfer': {'sources': copy.deepcopy(v['transfer']), 'reports': reports,
                               'surface': 'BASE_MESH', 'source_policy': 'not_inherited'},
                  'status': 'needs_review' if q['status'] == 'pass' else q['status']}
        state['candidates'][v['candidate']] = record
        core._store(scene, state)
        return {'status': 'candidate_transferred', 'candidate': v['candidate'], 'record': record,
                'read_only_sources': readonly,
                'quality_acceptance': 'requires_target_purpose_and_visual_review'}
    except Exception:
        for obj in reversed(created):
            if layer in obj.data.uv_layers:
                obj.data.uv_layers.remove(obj.data.uv_layers[layer])
            if active[obj.name] and active[obj.name] in obj.data.uv_layers:
                obj.data.uv_layers.active = obj.data.uv_layers[active[obj.name]]
        if previous is None:
            if core.STATE in scene:
                del scene[core.STATE]
        else:
            scene[core.STATE] = previous
        raise
