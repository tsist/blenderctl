# SPDX-License-Identifier: GPL-3.0-or-later
"""Trusted saved-file intake, extracted from the accepted image exporter.

Only normalized internal parameters enter this helper. It neither selects an
exporter nor grants implementation authorization or transfers source leases.
"""
from contextlib import ExitStack, contextmanager
from collections.abc import Mapping
from dataclasses import dataclass
import json
import ntpath
from pathlib import Path, PureWindowsPath
import re
import bpy
from filesystem import FileGuard
from protocol import Failure
from typed_dependencies import audit as audit_dependencies

def _need(ok, message, code='UNSUPPORTED'):
    if not ok:
        raise Failure(code, message)


def _local_path(value):
    _need(type(value) is str, 'Native local path required', 'INVALID_REQUEST')
    value.encode('utf-8', errors='strict')
    text=value.replace('/', '\\');pure=PureWindowsPath(text)
    _need(re.fullmatch(r'[A-Za-z]:', pure.drive) is not None and pure.root=='\\',
          'Only local absolute drive paths are admitted', 'INVALID_REQUEST')
    for part in pure.parts[1:]:
        _need(part not in ('.','..') and not part.endswith((' ','.')) and
              not any(ord(c)<32 or c in '<>:"|?*' for c in part),
              'Unsafe Windows component or stream path', 'INVALID_REQUEST')
    # normpath is lexical only; no resolve or source replacement.
    return Path(ntpath.normpath(text))


def _key(path):
    return ntpath.normcase(str(path))


def _info(guard):
    native=guard._information()  # Existing same-handle API, recorded dependency.
    return dict(**guard.identity(), bytes=(native.size_high<<32)|native.size_low,
                links=native.links, attributes=native.attributes)


def _write_json(path,value):
    data=(json.dumps(value,ensure_ascii=False,sort_keys=True,indent=2,allow_nan=False)+'\n').encode('utf-8')
    with path.open('xb') as stream:
        stream.write(data)
    with FileGuard(path) as guard:
        return dict(file=str(path),expected_sha256=guard.sha256(),bytes=_info(guard)['bytes'])


def _plain(value):
    if isinstance(value,Mapping):
        return {key:_plain(item) for key,item in value.items()}
    if type(value) is tuple:
        return [_plain(item) for item in value]
    return value


def _tree_is_viewport(tree,seen):
    if tree is None or tree.as_pointer() in seen:
        return False
    seen.add(tree.as_pointer())
    for node in tree.nodes:
        if node.bl_idname=='GeometryNodeIsViewport' or _tree_is_viewport(getattr(node,'node_tree',None),seen):
            return True
    return False


def _capture_context(recipe):
    scene=bpy.data.scenes.get(recipe['scene'])
    _need(scene is not None,'Missing scene', 'NOT_FOUND')
    layer=scene.view_layers.get(recipe['view_layer'])
    _need(layer is not None,'Missing view layer','NOT_FOUND')
    bpy.context.window.scene=scene
    bpy.context.window.view_layer=layer
    scene.frame_set(recipe['frame']);layer.update()
    _need(bpy.context.scene==scene and bpy.context.view_layer==layer,'Context activation mismatch','CONFLICT')
    width=int(scene.render.resolution_x*scene.render.resolution_percentage/100)
    height=int(scene.render.resolution_y*scene.render.resolution_percentage/100)
    _need(0<width<=2048 and 0<height<=2048,'Dimensions must be positive with maximum edge 2048')
    _need(not scene.render.use_simplify,'Render Simplify is outside saved export profile')
    camera=scene.objects.get(recipe['camera_name'])
    _need(camera is not None and camera.type=='CAMERA' and camera.data.type=='ORTHO','Explicit ORTHO camera required')
    _need(layer.objects.get(camera.name) is not None,'Camera excluded from view layer')
    render_visible=set()
    def visit(collection,blocked=False):
        blocked=blocked or collection.exclude or collection.hide_viewport or collection.collection.hide_viewport or collection.collection.hide_render or collection.holdout or collection.indirect_only
        if not blocked:
            render_visible.update(o.name for o in collection.collection.objects)
        for child in collection.children:
            visit(child,blocked)
    visit(layer.layer_collection)
    selected=[];modifiers=[]
    for name in recipe['occluder_object_names']:
        obj=scene.objects.get(name)
        _need(obj is not None,'Missing named source: '+name,'NOT_FOUND')
        _need(obj.type=='MESH' and layer.objects.get(name) is not None and name in render_visible and
              not obj.hide_render and not obj.hide_viewport and not obj.hide_get(view_layer=layer) and obj.visible_get(view_layer=layer),
              'Source must be mesh and visible in the explicit view layer/render: '+name)
        _need(obj.instance_type=='NONE','Instancing source is outside single-object capture: '+name)
        for mod in obj.modifiers:
            _need(mod.show_viewport==mod.show_render,'Viewport/render modifier flags differ: '+name+'/'+mod.name)
            if mod.type=='SUBSURF' and (mod.show_viewport or mod.show_render):
                _need(mod.levels==mod.render_levels,'Subdivision viewport/render levels differ: '+name)
            if mod.type=='NODES' and (mod.show_viewport or mod.show_render):
                _need(not _tree_is_viewport(mod.node_group,set()),'Geometry Is Viewport node is outside parity profile: '+name)
            modifiers.append(dict(object=name,name=mod.name,type=mod.type,
                show_viewport=mod.show_viewport,show_render=mod.show_render,
                arbitrary_render_equivalence='not_verified'))
        selected.append(obj)
    _need(set(recipe['member_object_names'])<=set(o.name for o in selected),'Members not in captured source universe','CONFLICT')
    graph=bpy.context.evaluated_depsgraph_get();targets={o.as_pointer() for o in selected}
    for instance in graph.object_instances:
        if not instance.is_instance:
            continue
        obj=getattr(instance.object,'original',instance.object)
        parent=getattr(instance,'parent',None)
        parent=getattr(parent,'original',parent)
        _need(not (obj and obj.as_pointer() in targets or parent and parent.as_pointer() in targets),
              'Selected source has unrepresented depsgraph instance')
    return scene,dict(scene=scene.name,view_layer=layer.name,frame=scene.frame_current,
        camera_name=camera.name,camera_type=camera.data.type,width=width,height=height,
        graph_mode=graph.mode,modifiers=modifiers,
        render_parity='limited_known_checks_passed',
        render_equivalence_unverified=['arbitrary modifier/driver/engine behavior','arbitrary render depsgraph equivalence'],
        object_names=[o.name for o in selected],member_object_names=list(recipe['member_object_names']))


@dataclass(frozen=True, slots=True)
class SavedSourceSession:
    root: Path
    scene: object
    context: dict
    source: dict
    resources: list
    dependency_audit: dict
    recheck_inputs: object


@contextmanager
def guarded_saved_source(p, output_root, checkpoint):
    """Hold source/resource/ancestor guards throughout the caller's capture.

    p is trusted normalized data with file/expected_sha256/resources/recipe.
    Existing audit/open/context order is retained. No post-yield work is added;
    the caller explicitly requests the historical final input recheck point.
    """
    source=_local_path(p['file']);root=_local_path(output_root)
    documents=[dict(file=str(source),expected_sha256=p['expected_sha256'])]
    documents += [dict(file=str(_local_path(row['file'])),expected_sha256=row['expected_sha256']) for row in p['resources']]
    _need(len({_key(row['file']) for row in documents})==len(documents),'Duplicate canonical input paths','CONFLICT')
    with ExitStack() as stack:
        dirs={};guards=[];identities=set();before=[]
        def guard_dirs(path):
            for ancestor in reversed(path.parents):
                key=_key(ancestor)
                if key not in dirs:
                    guard=stack.enter_context(FileGuard(ancestor,directory=True))
                    _need(bool(_info(guard)['attributes']&0x10),'Ancestor is not a directory','CONFLICT')
                    dirs[key]=guard
        for row in documents:
            checkpoint();path=Path(row['file']);guard_dirs(path)
            guard=stack.enter_context(FileGuard(path));info=_info(guard)
            identity=(info['volume'],info['file_index'])
            _need(info['links']==1 and identity not in identities,'Input hardlink or file identity alias','CONFLICT')
            digest=guard.sha256(checkpoint)
            _need(digest==row['expected_sha256'],'Caller input SHA mismatch: '+str(path),'CONFLICT')
            identities.add(identity);guards.append(guard);before.append(dict(**row,**info))
        guard_dirs(root);checkpoint();root.mkdir(exist_ok=False)
        root_guard=stack.enter_context(FileGuard(root,directory=True));dirs[_key(root)]=root_guard
        profile=dict(closure=dict(adapter='TYPED_FILES_V1',max_files=101,max_depth=8),
            timeline=dict(scene=p['recipe']['scene'],start=p['recipe']['frame'],end=p['recipe']['frame'],step=1))
        audit=audit_dependencies(dict(file=str(source),expected_sha256=p['expected_sha256'],profile=profile),root)
        audit_descriptor=_write_json(root/'resources.json',audit)
        checkpoint();_need(audit['closure']=='closed' and not audit['issues'],'Typed dependency closure is not closed')
        rejected={'point_cache','fluid_cache','rigidbody_cache','geometry_nodes_cache','geometry_nodes_bake','cache_file','volume','declared_cache','declared_plugin'}
        _need(all(row['kind'] not in rejected and row.get('requirement')!='unknown' and
                  row.get('source')!='SEQUENCE' and not row.get('frame_mapping') for row in audit['resources']),
              'Cache, unknown or dynamic dependency is outside static saved intake')
        actual={_key(_local_path(row['file'])):row['expected_sha256'] for row in audit['documents']}
        explicit={_key(row['file']):row['expected_sha256'] for row in documents}
        _need(len(actual)==len(audit['documents']) and actual==explicit,
              'Typed closure differs from source plus explicitly guarded resources','CONFLICT')
        # Audit may leave a library open. Reopen the protected original explicitly.
        bpy.ops.wm.open_mainfile(filepath=str(source),load_ui=False,use_scripts=False)
        checkpoint();scene,capture=_capture_context(p['recipe']);checkpoint()
        active=True
        def recheck_inputs():
            _need(active,'Saved source session is no longer held','CONFLICT')
            after=[]
            for row,guard in zip(before,guards):
                checkpoint();info=_info(guard);digest=guard.sha256(checkpoint)
                _need(info=={k:row[k] for k in info} and digest==row['expected_sha256'],'Guarded source/resource changed','CONFLICT')
                after.append(dict(file=row['file'],expected_sha256=digest,**info))
            return after
        session=SavedSourceSession(root,scene,capture,before[0],before[1:],audit_descriptor,recheck_inputs)
        try:
            yield session
        finally:
            active=False
