"""UV saved-disk protection over authored data, without evaluating other scenes.

An inactive Scene's Object dimensions/matrix_local/matrix_world are lazy caches
in Blender 5.2. Their post-load defaults need not match the previous process.
Their setters modify the persistent TRS/basis/parent-inverse representation,
which remains protected. Actual target world geometry is separately checked by
the UV core and, when requested, real evaluation-graph surface inspection.
"""
import hashlib


def scene_fingerprint(exclude_objects=(), persistence_exceptions=()):
    import bpy
    from geometry_nodes_workflow_addon import inspection as shared
    core=shared.core
    excluded=set(exclude_objects)
    objects=[]
    for obj in sorted(bpy.data.objects,key=lambda o:o.name):
        if obj.name in excluded:continue
        properties=shared._rna_state(obj,persistence_exceptions)
        for key in ('dimensions','matrix_local','matrix_world'):properties.pop(key,None)
        modifiers=[]
        for modifier in obj.modifiers:
            row=shared._rna_state(modifier,persistence_exceptions)
            if modifier.type=='NODES' and modifier.node_group:
                row['graph_sha256']=core.tree_fingerprint(modifier.node_group)
                row['parameters']=core.parameter_values(modifier)
            modifiers.append(row)
        objects.append({'object':obj.name,'properties':properties,
            'data':shared._data_state(obj.data,persistence_exceptions),'modifiers':modifiers,
            'constraints':[shared._rna_state(c,persistence_exceptions) for c in obj.constraints],
            'custom_properties':{k:core.plain(v) for k,v in obj.items() if k!='_RNA_UI'}})
    materials=[{'name':m.name,'properties':shared._rna_state(m,persistence_exceptions),
        'graph':core.tree_fingerprint(m.node_tree) if m.node_tree else None} for m in sorted(bpy.data.materials,key=lambda m:m.name)]
    worlds=[{'name':w.name,'properties':shared._rna_state(w,persistence_exceptions),
        'graph':core.tree_fingerprint(w.node_tree) if w.node_tree else None} for w in sorted(bpy.data.worlds,key=lambda w:w.name)]
    collections=[{'name':c.name,'objects':sorted(o.name for o in c.objects if o.name not in excluded),
        'children':sorted(child.name for child in c.children)} for c in sorted(bpy.data.collections,key=lambda c:c.name)]
    value={'object_transform_basis':'AUTHORED_PERSISTENT_V1','objects':objects,
        'materials':materials,'worlds':worlds,'collections':collections}
    return hashlib.sha256(core._dump(value).encode()).hexdigest()
