"""Read-only measurements on actual Blender evaluation graphs in a disk worker.

No evaluated mesh is written back. RENDER is captured from RenderEngine.render,
not simulated by substituting viewport modifier settings. No pixels are rendered.
"""
from types import SimpleNamespace
import hashlib
import bpy
import numpy as np
from . import core,metrics,recipes,policies
from .contract import fail,WorkflowError

# Stateful caches, arbitrary geometry node outputs and external generators need
# their own dependency/correspondence contracts before this path can certify them.
MODIFIERS={'ARMATURE','SUBSURF','MIRROR','SOLIDIFY','BEVEL','TRIANGULATE',
           'SMOOTH','LAPLACIANSMOOTH','CORRECTIVE_SMOOTH','SIMPLE_DEFORM',
           'WEIGHTED_NORMAL','NORMAL_EDIT','CAST','LATTICE','SHRINKWRAP','MASK'}

SHADING_NODES={'NodeGroupInput','NodeGroupOutput','NodeReroute','NodeFrame','GeometryNodeGroup',
               'FunctionNodeBooleanMath','FunctionNodeCompare','GeometryNodeInputEdgeSmooth',
               'GeometryNodeInputMeshEdgeAngle','GeometryNodeInputShadeSmooth','GeometryNodeSetShadeSmooth'}

RENDER_SETTINGS=('engine','resolution_x','resolution_y','resolution_percentage','use_border','use_compositing','use_sequencer')


def _controls(scene, window):
    layer=window.view_layer
    return {'frame':scene.frame_current,'subframe':scene.frame_subframe,
            'window_scene':window.scene.name,'window_view_layer':layer.name,
            'render':{k:getattr(scene.render,k) for k in RENDER_SETTINGS},
            'camera':scene.camera.name if scene.camera else None,
            'active_object':layer.objects.active.name if layer.objects.active else None,
            'selected':sorted(o.name for o in layer.objects if o.select_get(view_layer=layer))}


def _shape_state(obj):
    key=obj.data.shape_keys
    if key is None:return None
    return {'name':key.name,'use_relative':key.use_relative,'eval_time':key.eval_time,
            'active_index':obj.active_shape_key_index,'show_only':obj.show_only_shape_key,
            'blocks':[{'name':k.name,'value':k.value,'mute':k.mute,
                       'relative_key':k.relative_key.name if k.relative_key else None,
                       'vertex_group':k.vertex_group} for k in key.key_blocks]}


def _shading_tree(tree, stack=()):
    """Admit the known native Smooth by Angle graph by capabilities, not name."""
    if tree is None or tree.as_pointer() in stack:return False
    for node in tree.nodes:
        if node.bl_idname not in SHADING_NODES:return False
        if node.bl_idname=='GeometryNodeGroup' and not _shading_tree(node.node_tree,(*stack,tree.as_pointer())):return False
    return True


def _surface_snapshot(mesh,matrix,layer,unit):
    """Ordered numeric identity, including unused geometry and every UV loop."""
    def values(items,field,width,dtype):
        out=np.empty(len(items)*width,dtype=dtype);items.foreach_get(field,out)
        return out.reshape(len(items),width) if width>1 else out
    local=values(mesh.vertices,'co',3,np.float32)
    matrix=np.array(matrix,dtype=np.float64)
    world=(local.astype(np.float64)@matrix[:3,:3].T+matrix[:3,3])*unit
    ids,_=core.islands(SimpleNamespace(data=mesh),layer)
    arrays={'local_coordinates':local,'matrix_world':matrix,'world_positions':world,
            'edge_vertices':values(mesh.edges,'vertices',2,np.int32),
            'loop_vertices':values(mesh.loops,'vertex_index',1,np.int32),
            'loop_edges':values(mesh.loops,'edge_index',1,np.int32),
            'polygon_loop_start':values(mesh.polygons,'loop_start',1,np.int32),
            'polygon_loop_total':values(mesh.polygons,'loop_total',1,np.int32),
            'polygon_vertices':np.asarray([i for p in mesh.polygons for i in p.vertices],dtype=np.int32),
            'triangle_vertices':values(mesh.loop_triangles,'vertices',3,np.int32),
            'triangle_loops':values(mesh.loop_triangles,'loops',3,np.int32),
            'triangle_polygons':values(mesh.loop_triangles,'polygon_index',1,np.int32),
            'uv_loops':core.coordinates(mesh,layer),'numeric_island_ids':np.asarray(ids,dtype=np.int64)}
    digest=hashlib.sha256();nonfinite=None
    for name,array in arrays.items():
        if array.dtype.kind=='f':
            invalid=np.flatnonzero(~np.isfinite(array).ravel())
            if len(invalid) and nonfinite is None:nonfinite={'field':name,'index':int(invalid[0]),'reason':'nonfinite'}
            # Signed zero is the same numeric value and must have the same digest.
            array[array==0]=0
        digest.update(name.encode('ascii'));digest.update(str((array.dtype.str,array.shape)).encode('ascii'));digest.update(array.tobytes())
    return {'arrays':arrays,'sha256':digest.hexdigest(),'nonfinite':nonfinite}


def _first_mismatch(base,evaluated):
    if base['nonfinite']:return {'side':'base',**base['nonfinite']}
    if evaluated['nonfinite']:return {'side':'evaluated',**evaluated['nonfinite']}
    for name,a in base['arrays'].items():
        b=evaluated['arrays'][name]
        if a.shape!=b.shape:return {'field':name,'reason':'shape','base':list(a.shape),'evaluated':list(b.shape)}
        differing=np.flatnonzero((a!=b).ravel())
        if len(differing):
            index=int(differing[0])
            return {'field':name,'reason':'value','index':index,'base':a.ravel()[index].item(),'evaluated':b.ravel()[index].item()}
    return None


def _candidate_exact(scene,objects,v):
    """Resolve only explicit reuse; purpose, targets and approval are not inherited."""
    spec=v['surface'];cid=spec['candidate'];record=core.state(scene).get('candidates',{}).get(cid)
    names=[o.name for o in objects]
    if not isinstance(record,dict) or record.get('objects')!=v['objects'] or names!=v['objects']:
        fail('Missing exact surface candidate or complete target identity changed','CONFLICT')
    manifest=record.get('manifest',{});layer=record.get('layer')
    if manifest.get('view_layer')!=v['view_layer'] or manifest.get('scene')!=scene.name:
        fail('Surface candidate scene/view layer changed','CONFLICT')
    if any(manifest.get(k)!=v[k] for k in ('width','height')):
        fail('Surface dimensions differ from candidate dimensions','CONFLICT')
    if any(spec['layers'][o.name]!=layer or layer not in o.data.uv_layers for o in objects):
        fail('Surface must use the exact managed candidate layer for every target','CONFLICT')
    for key in ('meshes','geometry','uv_hash'):
        if not isinstance(record.get(key),dict) or set(record[key])!=set(names):
            fail('Surface candidate lacks complete '+key+' identity','CONFLICT')
    for o in objects:
        if o.data.users!=1 or o.data.name!=record['meshes'][o.name]:
            fail('Surface candidate mesh identity or sharing changed: '+o.name,'CONFLICT')
        try:matches=core.geometry(o)==record['geometry'][o.name] and core._uv_fingerprint(o.data,layer)==record['uv_hash'][o.name]
        except (ValueError,OverflowError):matches=False
        if not matches:fail('Surface candidate geometry, UV or pins changed: '+o.name,'CONFLICT')
    settings=policies.normalize(record.get('settings'),names)
    usage,_,_=policies.resolve(objects,layer,manifest,scene.unit_settings.scale_length,settings)
    if usage['mode']!='EXCLUSIVE' or not usage['reuse_groups']:
        fail('CANDIDATE_EXACT requires explicit EXCLUSIVE candidate settings with nonempty reuse groups','CONFLICT')
    # Bound snapshot once, before frame changes. A deforming/moving base cannot
    # silently become the new identity for a later evaluation sample.
    snapshots={};total=0
    for o in objects:
        o.data.calc_loop_triangles();total+=len(o.data.loop_triangles)
        if total>spec['triangle_limit']:fail('Candidate base exceeds surface triangle_limit','RESOURCE_LIMIT')
        snapshots[o.name]=_surface_snapshot(o.data,o.matrix_world,layer,scene.unit_settings.scale_length)
    return {'candidate':cid,'usage':usage,'snapshots':snapshots}


def _sample(graph,scene,objects,v,mode,progress,exact=None):
    spec=v['surface'];rows=[];counts=[];evaluated=[];total=0
    evidence={o.name:{'object':o.name,'base_sha256':exact['snapshots'][o.name]['sha256'],
                     'evaluated_sha256':None,'match':False,'first_mismatch':None} for o in objects} if exact else {}
    def exact_result():
        return {'reuse_used':False,'candidate':exact['candidate'],'exact_identity':{'comparison':'finite_numeric_values_no_tolerance',
                'objects':list(evidence.values()),'first_mismatch':next(({'object':n,**r['first_mismatch']} for n,r in evidence.items() if r['first_mismatch']),None)}} if exact else {}
    if graph.mode!=mode:fail('Unexpected dependency graph mode: '+graph.mode,'VALIDATION_FAILED')
    try:
        for obj in objects:
            if mode=='VIEWPORT' and not obj.visible_get(view_layer=scene.view_layers[v['view_layer']]):
                fail('Requested target is hidden in the viewport: '+obj.name,'UNSUPPORTED')
            unsupported=[m.type for m in obj.modifiers if (m.show_render if mode=='RENDER' else m.show_viewport)
                         and m.type not in MODIFIERS and not (m.type=='NODES' and _shading_tree(m.node_group))]
            if unsupported:fail('Unsupported surface modifier types: '+obj.name+': '+','.join(unsupported),'UNSUPPORTED')
            if obj.instance_type!='NONE':fail('Surface object instances require an explicit expansion contract','UNSUPPORTED')
            if mode=='RENDER' and obj.hide_render:fail('Requested target is disabled for render: '+obj.name,'UNSUPPORTED')
            ev=obj.evaluated_get(graph)
            if not ev.is_evaluated:fail('Target absent from evaluated graph: '+obj.name,'UNSUPPORTED')
            mesh=ev.to_mesh(preserve_all_data_layers=True,depsgraph=graph);evaluated.append(ev)
            if mesh is None:fail('Target has no evaluated mesh: '+obj.name,'UNSUPPORTED')
            mesh.calc_loop_triangles();total+=len(mesh.loop_triangles)
            counts.append({'object':obj.name,'vertices':len(mesh.vertices),'triangles':len(mesh.loop_triangles),
                           'loops':len(mesh.loops),'layer':spec['layers'][obj.name],
                           'shape_state':_shape_state(ev)})
            if total>spec['triangle_limit']:
                return {'status':'unknown','reason':'triangle_limit','requested_limit':spec['triangle_limit'],
                        'observed_triangles_at_stop':total,'objects_observed':counts,'complete':False,**exact_result()}
            layer=spec['layers'][obj.name]
            if layer not in mesh.uv_layers:
                if exact:
                    evidence[obj.name]['first_mismatch']={'field':'uv_layer','reason':'absent','layer':layer}
                    return {'status':'unknown','reason':'candidate_exact_mismatch','complete':False,'objects':counts,**exact_result()}
                fail('UV layer is absent on evaluated surface: '+obj.name+'/'+layer,'UNSUPPORTED')
            if exact:
                snapshot=_surface_snapshot(mesh,ev.matrix_world,layer,scene.unit_settings.scale_length)
                mismatch=_first_mismatch(exact['snapshots'][obj.name],snapshot)
                evidence[obj.name].update(evaluated_sha256=snapshot['sha256'],match=mismatch is None,first_mismatch=mismatch)
                arrays=snapshot['arrays'];uv=arrays['uv_loops'];island_ids=arrays['numeric_island_ids']
                positions=arrays['world_positions'];triangles=arrays['triangle_vertices'].ravel()
                loops=arrays['triangle_loops'].ravel();faces=arrays['triangle_polygons']
            else:
                uv=core.coordinates(mesh,layer);island_ids,_=core.islands(SimpleNamespace(data=mesh),layer)
                positions=np.empty(len(mesh.vertices)*3,dtype=np.float32);mesh.vertices.foreach_get('co',positions)
                matrix=np.array(ev.matrix_world,dtype=np.float64)
                positions=(positions.reshape(-1,3).astype(np.float64)@matrix[:3,:3].T+matrix[:3,3])*scene.unit_settings.scale_length
                triangles=np.empty(len(mesh.loop_triangles)*3,dtype=np.int32);mesh.loop_triangles.foreach_get('vertices',triangles)
                loops=np.empty(len(mesh.loop_triangles)*3,dtype=np.int32);mesh.loop_triangles.foreach_get('loops',loops)
                faces=np.empty(len(mesh.loop_triangles),dtype=np.int32);mesh.loop_triangles.foreach_get('polygon_index',faces)
            counts[-1]['world_geometry_sha256']=hashlib.sha256(positions.tobytes()+triangles.tobytes()).hexdigest()
            counts[-1]['uv_sha256']=hashlib.sha256(uv.tobytes()).hexdigest()
            rows.append({'name':obj.name,'positions':positions,'triangles':triangles.reshape(-1,3),
                         'uv':uv[loops.reshape(-1,3)],'islands':np.asarray(island_ids,dtype=np.int64)[faces]})
        if exact and any(not row['match'] for row in evidence.values()):
            return {'status':'unknown','reason':'candidate_exact_mismatch','complete':False,'objects':counts,**exact_result()}
        progress('surface_check',frame=scene.frame_current,mode=mode,triangles=total)
        q=metrics.measure(rows,width=v['width'],height=v['height'],pair_budget=v['pair_budget'],
                          usage=exact['usage'] if exact else {'mode':'EXCLUSIVE','reuse_groups':[]},
                          cancel=lambda:progress('surface_check',frame=scene.frame_current,mode=mode,triangles=total))
        q['measurement_surface']='evaluated_mesh';q['evaluation_mode']=mode;q['visual']='not_reviewed'
        q['purpose_checks']={'status':'not_assigned','visual':'not_reviewed'}
        if 'policy' in spec:
            # Caller explicitly supplies a surface policy; no base binding or
            # user acceptance is inherited by this independent measurement.
            recipe={'policy':spec['policy'],'binding':{'surface':'explicit_request'}}
            recipes.evaluate(q,recipe,recipe['binding'])
            q['purpose_checks'].update(measurement_surface='evaluated_mesh',evaluated_surface='sampled',
                binding='explicit_surface_request',reference_semantics='Caller-supplied surface policy, not inherited approval')
        gate=exact_result()
        if exact:gate['reuse_used']=True
        return {'status':q['status'],'complete':q['overlap']['complete'] and q['gap']['complete'],
                'objects':counts,'metrics':q,'reason':None,**gate}
    finally:
        for obj in evaluated:obj.to_mesh_clear()


def inspect(scene,objects,v,progress):
    if not bpy.app.background:
        fail('Multi-frame surface sampling requires a saved-disk background worker; save a working snapshot and use uv.inspect','UNSUPPORTED')
    spec=v['surface'];exact=_candidate_exact(scene,objects,v) if spec['usage']=='CANDIDATE_EXACT' else None
    window=bpy.context.window;original=(scene.frame_current,scene.frame_subframe)
    before=_controls(scene,window)
    context=(window.scene,window.view_layer)
    render=scene.render;settings={k:getattr(render,k) for k in RENDER_SETTINGS}
    camera=scene.camera;owned_camera=None;capture={};results=[];registered=False
    layer_name=v['view_layer']
    class UVWEvaluationEngine(bpy.types.RenderEngine):
        bl_idname='UVW_EVALUATION_CAPTURE'
        bl_label='UV Surface Evaluation Capture'
        bl_use_preview=False
        bl_use_postprocess=False
        def render(self,depsgraph):
            try:capture['sample']=_sample(depsgraph,scene,objects,v,'RENDER',progress,exact)
            except Exception as exc:capture['exception']=exc
    try:
        window.scene=scene;window.view_layer=scene.view_layers[layer_name]
        if 'RENDER' in spec['modes']:
            if getattr(bpy.types,'UVWEvaluationEngine',None):fail('Surface capture is already active','CONFLICT')
            bpy.utils.register_class(UVWEvaluationEngine);registered=True
            if scene.camera is None:
                data=bpy.data.cameras.new('UVW_EvaluationCamera');owned_camera=bpy.data.objects.new('UVW_EvaluationCamera',data)
                scene.collection.objects.link(owned_camera);scene.camera=owned_camera
            render.resolution_x=16;render.resolution_y=16;render.resolution_percentage=100
            render.use_border=False;render.use_compositing=False;render.use_sequencer=False
        for frame in spec['frames']:
            scene.frame_set(frame,subframe=0);window.view_layer.update()
            for mode in spec['modes']:
                progress('surface_evaluate',frame=frame,mode=mode)
                try:
                    if mode=='VIEWPORT':
                        render.engine=settings['engine']
                        sample=_sample(bpy.context.evaluated_depsgraph_get(),scene,objects,v,mode,progress,exact)
                    else:
                        capture.clear();render.engine=UVWEvaluationEngine.bl_idname
                        bpy.ops.render.render(write_still=False,scene=scene.name,layer=layer_name)
                        if 'exception' in capture:raise capture['exception']
                        if 'sample' not in capture:fail('Blender did not supply a RENDER evaluation graph','VALIDATION_FAILED')
                        sample=capture['sample']
                    results.append({'frame':frame,'mode':mode,**sample})
                except WorkflowError as exc:
                    if exc.code!='UNSUPPORTED':raise
                    sample={'frame':frame,'mode':mode,'status':'unknown','reason':'unsupported',
                            'message':str(exc),'complete':False}
                    if exact:
                        sample.update(reuse_used=False,candidate=exact['candidate'],exact_identity={
                            'comparison':'finite_numeric_values_no_tolerance','first_mismatch':{'reason':'unsupported','message':str(exc)},
                            'objects':[{'object':n,'base_sha256':s['sha256'],'evaluated_sha256':None,'match':False,'first_mismatch':None}
                                       for n,s in exact['snapshots'].items()]})
                    results.append(sample)
        status='fail' if any(r['status']=='fail' for r in results) else 'unknown' if any(r['status']!='pass' for r in results) else 'pass'
        result={'surface_version':'1.0','status':status,'request':spec,'samples':results,
                'sample_count':len(results),'usage':spec['usage'],'source_policy':'read_only_saved_disk',
                'evaluation_scope':'BLENDER_EVALUATED_MESH',
                'configured_engine':settings['engine'],'render_capture_engine':UVWEvaluationEngine.bl_idname,
                'configured_renderer_equivalence':'not_verified',
                'quality_acceptance':'requires_surface_purpose_and_visual_review',
                'limits':['Only explicitly sampled frames, not continuous animation',
                          'Full evaluated meshes within the triangle limit; no decimation',
                          'RENDER is a capture-engine dependency graph; shader displacement, adaptive engine subdivision and pixel equivalence are not certified',
                          ('Candidate EXCLUSIVE reuse only after exact finite identity of every target in each sample; no density settings or purpose approval inherited'
                           if exact else 'No inherited base-island reuse/density correspondence or purpose approval'),
                          'No source save, modifier application, key removal or UV write']}
    finally:
        cleanup_errors=[]
        def restore(fn):
            try:fn()
            except Exception as exc:cleanup_errors.append(str(exc))
        for key,value in settings.items():restore(lambda k=key,val=value:setattr(render,k,val))
        restore(lambda:setattr(scene,'camera',camera))
        if owned_camera:
            data=owned_camera.data
            restore(lambda:bpy.data.objects.remove(owned_camera,do_unlink=True))
            restore(lambda:bpy.data.cameras.remove(data))
        if registered:restore(lambda:bpy.utils.unregister_class(UVWEvaluationEngine))
        restore(lambda:scene.frame_set(original[0],subframe=original[1]))
        restore(lambda:setattr(window,'scene',context[0]))
        restore(lambda:setattr(window,'view_layer',context[1]))
        if cleanup_errors:fail('Surface context restoration failed: '+'; '.join(cleanup_errors),'VALIDATION_FAILED')
        after=_controls(scene,window)
        if before!=after:fail('Surface inspection changed scene controls','VALIDATION_FAILED')
    result['controls_restored']=True
    result['controls_before']=before;result['controls_after']=after
    return result
