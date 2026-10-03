# SPDX-License-Identifier: GPL-3.0-or-later
"""Opt-in material-only edits with deformation invariants and frame checks."""
import bpy, math, copy
from inspection import mesh_content, record, value, properties
from protocol import Failure
import modeling, scenes


def editable(op):
    obj = scenes.find(bpy.data.objects, op['object'], True)
    if obj.type != 'MESH':
        raise Failure('INVALID_REQUEST', 'Material deformation guard requires a mesh')
    data = obj.data
    if data.library or data.override_library or data.animation_data:
        raise Failure('UNSUPPORTED', 'Material deformation guard requires local mesh data without mesh animation')
    users = [o for o in bpy.data.objects if o.data == data]
    policy = op['data_scope']
    if any(s.link != 'DATA' for o in users for s in o.material_slots):
        raise Failure('UNSUPPORTED', 'Material deformation guard requires DATA-linked slots for all mesh users')
    # A fake user keeps data alive; it is not another consumer needing isolation.
    if data.users - int(data.use_fake_user) > 1:
        if policy == 'reject_shared':
            raise Failure('CONFLICT', 'Shared mesh requires single_user or shared policy')
        if policy == 'single_user':
            if any(x.asset_data or x.get('asset_id') for x in (data, data.shape_keys) if x):
                raise Failure('UNSUPPORTED', 'Cannot duplicate mesh or shape-key asset identity')
            name = obj.name + '.Material'
            scenes.fresh(bpy.data.meshes, name)
            obj.data = scenes.named(data.copy(), name)
        elif any(o.library or o.override_library for o in users):
            raise Failure('UNSUPPORTED', 'Shared material edit has linked/override object users')
    modeling.bounded(obj.data)
    return obj


def geometry(mesh):
    modeling.bounded(mesh)
    result = mesh_content(mesh)
    if result['non_finite_values']:
        raise Failure('VALIDATION_FAILED', 'Material deformation guard found non-finite geometry, UV, shape keys or weights')
    result.pop('materials')
    result['hashes'].pop('material_index')
    result['attributes'] = [a for a in result['attributes'] if a['name'] != 'material_index']
    result['diagnostics'].pop('invalid_material_indices')
    return result


def snapshot(frames, evaluated_uv_tolerance=0):
    """All mesh users, not only selected targets; sampled geometry is world-space."""
    objects = sorted(bpy.data.objects, key=lambda o:o.name)
    meshes = [o for o in objects if o.type == 'MESH']
    # Drivers can read material slots or datablock names; no such dependency is certified.
    from inspection import all_ids
    for item in all_ids():
        ad = getattr(item, 'animation_data', None)
        if ad and ad.drivers:
            raise Failure('UNSUPPORTED', 'Material deformation guard does not support drivers')
    base = {}
    for obj in meshes:
        data = obj.data
        r = record(obj)
        r['object'].pop('data')  # single_user intentionally changes the mesh identity.
        keys = data.shape_keys
        shape = None
        if keys:
            if keys.library or keys.override_library:
                raise Failure('UNSUPPORTED', 'Linked/override shape keys are unsupported')
            shape = {'relative':keys.use_relative, 'eval_time':keys.eval_time,
                     'custom':record(keys)['custom_properties'], 'animation':record(keys).get('animation'),
                     'blocks':[{'name':k.name, 'value':k.value, 'mute':k.mute, 'relative_key':k.relative_key.name,
                                'slider_min':k.slider_min, 'slider_max':k.slider_max, 'vertex_group':k.vertex_group,
                                'interpolation':k.interpolation, 'frame':k.frame} for k in keys.key_blocks]}
        base[obj.name] = {'geometry':geometry(data),'object':r,'shape_keys':shape,
                          'groups':[{'name':g.name,'index':g.index,'lock_weight':g.lock_weight} for g in obj.vertex_groups]}
    # Armature rest/pose/configuration and action contents are protected, too.
    rig = []
    for obj in objects:
        if obj.type == 'ARMATURE':
            rig.append({'object':record(obj), 'data':record(obj.data),
                        'bones':[{'name':b.name,'parent':b.parent.name if b.parent else None,
                                  'matrix':value(b.matrix_local),'head':value(b.head_local),'tail':value(b.tail_local),
                                  'properties':properties(b)} for b in obj.data.bones]})
    actions = [record(a) for a in sorted(bpy.data.actions,key=lambda a:a.name)]
    scene = bpy.context.scene; original_frame = scene.frame_current; subframe = scene.frame_subframe
    samples = {}
    try:
        for frame in frames:
            scene.frame_set(frame); scenes.update(); graph = bpy.context.evaluated_depsgraph_get()
            rows = {}
            for obj in meshes:
                if obj.name not in bpy.context.view_layer.objects:
                    continue
                evaluated = obj.evaluated_get(graph)
                mesh = evaluated.to_mesh(preserve_all_data_layers=True, depsgraph=graph)
                try:
                    sampled=geometry(mesh)
                    uv=None
                    if evaluated_uv_tolerance:
                        # Base UVs above remain exact hashes. Only evaluated UVs get
                        # explicit numeric comparison, never topology or skin weights.
                        names={layer.name for layer in mesh.uv_layers}
                        uv={layer.name:[list(p.uv) for p in layer.data] for layer in mesh.uv_layers}
                        for name in names:sampled['hashes'].pop('uv:'+name)
                        sampled['attributes']=[a for a in sampled['attributes'] if a['name'] not in names]
                    rows[obj.name] = {'geometry':sampled, 'world':value(evaluated.matrix_world),'evaluated_uv':uv}
                finally:
                    evaluated.to_mesh_clear()
            samples[str(frame)] = rows
    finally:
        scene.frame_set(original_frame, subframe=subframe); scenes.update()
    result={'meshes':base,'rigs':rig,'actions':actions,'samples':samples}
    def finite(v):
        if isinstance(v,dict):return 'non_finite' not in v and all(finite(x) for x in v.values())
        if isinstance(v,(list,tuple)):return all(finite(x) for x in v)
        return math.isfinite(v) if type(v) in (int,float) else True
    if not finite(result):raise Failure('VALIDATION_FAILED','Material deformation guard found non-finite transform or animation data')
    return result


def verify(expected, observed, evaluated_uv_tolerance=0):
    left=copy.deepcopy(expected);right=copy.deepcopy(observed);maximum=0
    if evaluated_uv_tolerance:
        if left['samples'].keys()!=right['samples'].keys():raise Failure('VALIDATION_FAILED','Deformation sample frame set changed')
        for frame,rows in left['samples'].items():
            if rows.keys()!=right['samples'][frame].keys():raise Failure('VALIDATION_FAILED','Deformation sampled object set changed')
            for name,row in rows.items():
                a=row.pop('evaluated_uv');b=right['samples'][frame][name].pop('evaluated_uv')
                if a.keys()!=b.keys():raise Failure('VALIDATION_FAILED','Evaluated UV layer set changed')
                for layer,values in a.items():
                    if len(values)!=len(b[layer]):raise Failure('VALIDATION_FAILED','Evaluated UV loop count changed')
                    for u,v in zip(values,b[layer]):
                        for x,y in zip(u,v):
                            error=abs(x-y)
                            if not math.isfinite(error) or error>evaluated_uv_tolerance:
                                raise Failure('VALIDATION_FAILED','Evaluated UV error exceeds explicit tolerance')
                            maximum=max(maximum,error)
    mismatch = scenes.compare(left, right)
    if mismatch:
        raise Failure('VALIDATION_FAILED', 'Material edit changed deformation state: ' + mismatch)
    return maximum
