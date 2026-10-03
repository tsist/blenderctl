# SPDX-License-Identifier: GPL-3.0-or-later
"""Explicit full evaluated surface clips, time-rescaled integer export and midpoint gates."""
import math,time
from contextlib import ExitStack
import bpy,numpy as np
from protocol import Failure,atomic_json,digest,read_json
import exchange,scenes,nodes,drivers,rig_exchange


def activate(spec):
    scene=scenes.find(bpy.data.scenes,spec['scene']);layer=scenes.find(scene.view_layers,spec['view_layer'])
    bpy.context.window.scene=scene;bpy.context.window.view_layer=layer
    return scene


def prepare_source(params,spec,job):
    import simulation
    if any(s.rigidbody_world for s in bpy.data.scenes) or any(m.type in ('CLOTH','SOFT_BODY','FLUID') for o in bpy.data.objects for m in o.modifiers):
        descriptor=params.get('simulation_receipt')
        if not descriptor:raise Failure('INVALID_REQUEST','Evaluated physics clip requires RIG_CLOTH_V1 receipt')
        receipt=read_json(descriptor['file']);request=receipt['request']
        if request.get('adapter')!='RIG_CLOTH_V1' or request['scene']!=spec['scene'] or request['view_layer']!=spec['view_layer'] or request['frame_start']>spec['frame_start'] or request['frame_end']<spec['frame_end']:
            raise Failure('CONFLICT','Mesh clip range/context outside RIG_CLOTH_V1 receipt')
        simulation.bake({'file':params['file'],'manifest':{**request,'mode':'reuse','receipt':descriptor},'driver_profile':params['driver_profile']},job)
    else:
        if params.get('simulation_receipt'):raise Failure('INVALID_REQUEST','Simulation receipt on non-physics source')
        exchange.safe_source(params['driver_profile'],params['file'],job)
    return activate(spec)


def capture(names,frame,profile,limit=200000):
    base=math.floor(frame);bpy.context.scene.frame_set(base,subframe=frame-base)
    bpy.context.view_layer.update();drivers.evaluated(profile)
    graph=bpy.context.evaluated_depsgraph_get();rows=[];count=0;loops=0;triangle_count=0
    for name in names:
        obj=scenes.find(bpy.data.objects,name)
        if obj.type!='MESH' or obj.name not in bpy.context.view_layer.objects or obj.library or obj.override_library:
            raise Failure('UNSUPPORTED','Mesh clip needs local active-layer meshes')
        if not obj.visible_get():raise Failure('UNSUPPORTED','Mesh clip objects must remain visible')
        ev=obj.evaluated_get(graph);mesh=ev.to_mesh(preserve_all_data_layers=True,depsgraph=graph)
        try:
            count+=len(mesh.vertices)
            if count>limit:raise Failure('UNSUPPORTED','Mesh clip exceeds evaluated vertex limit: '+str(limit))
            loops+=len(mesh.loops)
            if loops>2000000 or len(mesh.uv_layers)>8:raise Failure('UNSUPPORTED','Mesh clip exceeds 2000000 loops or 8 UV layers')
            points=np.empty(len(mesh.vertices)*3,dtype=np.float32);mesh.vertices.foreach_get('co',points);points=points.reshape((-1,3))
            matrix=np.array(ev.matrix_world,dtype=np.float64)
            points=(points@matrix[:3,:3].T+matrix[:3,3]).astype(np.float32)
            if not len(points) or not np.isfinite(points).all():raise Failure('VALIDATION_FAILED','Empty or non-finite mesh clip surface')
            mesh.calc_loop_triangles();triangles=np.array([tuple(t.vertices) for t in mesh.loop_triangles],dtype=np.int32)
            triangle_count+=len(triangles)
            if triangle_count>1000000:raise Failure('UNSUPPORTED','Mesh clip exceeds 1000000 triangles')
            if not len(triangles):raise Failure('UNSUPPORTED','Mesh clip requires surface triangles')
            faces=[list(p.vertices) for p in mesh.polygons];loose_edges=[list(e.vertices) for e in mesh.edges if e.is_loose]
            uv={}
            for layer in mesh.uv_layers:
                values=np.empty(len(layer.data)*2,dtype=np.float32);layer.data.foreach_get('uv',values);uv[layer.name]=values.reshape((-1,2))
            edges=points[triangles[:,1]]-points[triangles[:,0]];other=points[triangles[:,2]]-points[triangles[:,0]]
            area=float(np.linalg.norm(np.cross(edges,other),axis=1).sum(dtype=np.float64)/2)
            rows.append({'name':name,'frame':frame,'vertices':points,'faces':faces,'loose_edges':loose_edges,'uv':uv,'triangle_indices':triangles,
                'triangles':len(triangles),'area':area,'bounds':[points.min(axis=0).tolist(),points.max(axis=0).tolist()]})
        finally:ev.to_mesh_clear()
    return rows


def topology_match(a,b):
    if len(a['vertices'])!=len(b['vertices']) or a['faces']!=b['faces'] or a['loose_edges']!=b['loose_edges'] or set(a['uv'])!=set(b['uv']):return False
    return all(np.array_equal(a['uv'][k],b['uv'][k]) for k in a['uv'])


def maximum(a,b):return float(np.max(np.linalg.norm(a-b,axis=1)))


def progress(job,phase,**details):atomic_json(job/'exchange-clip-progress.json',{'phase':phase,**details})


def export_clip(params,spec,job,started):
    profile=params['driver_profile'];substeps=spec['sampling']['substeps'];tolerance=spec['sampling']['max_position_error']
    start,end=spec['frame_start'],spec['frame_end'];keys=[start+i/substeps for i in range((end-start)*substeps+1)]
    times=[start+i/(2*substeps) for i in range((end-start)*2*substeps+1)]
    with ExitStack() as stack:
        hashes=nodes.resource_guards(params,stack);scene=prepare_source(params,spec,job)
        fps=scene.render.fps;fps_base=scene.render.fps_base
        if fps_base!=1 or scene.unit_settings.scale_length!=1 or fps*substeps>240:
            raise Failure('UNSUPPORTED','Mesh clip requires meter scale one, integer fps and output fps <=240')
        first=capture(spec['objects'],times[0],profile)
        count=sum(len(row['vertices']) for row in first);observations=count*len(times)
        if observations>spec['sampling']['max_vertex_samples']:
            raise Failure('UNSUPPORTED','Declared vertex sample budget exceeded before capture')
        refs=[first];mods={n:[{'name':m.name,'type':m.type,'viewport':m.show_viewport,'render':m.show_render} for m in bpy.data.objects[n].modifiers] for n in spec['objects']}
        progress(job,'capture',vertex_samples=observations,times=times)
        for frame in times[1:]:
            rows=capture(spec['objects'],frame,profile)
            if any(not topology_match(a,b) for a,b in zip(first,rows)):raise Failure('UNSUPPORTED','Evaluated mesh topology/UV changed across clip')
            # Topology is invariant; retain it once, not once per time sample.
            for a,b in zip(first,rows):
                b['faces']=a['faces'];b['loose_edges']=a['loose_edges'];b['uv']=a['uv'];b.pop('triangle_indices')
            refs.append(rows)
        for i,row in enumerate(first):
            np.savez_compressed(job/('exchange-clip-source-%02d.npz'%i),frames=np.array(times),vertices=np.stack([sample[i]['vertices'] for sample in refs]),triangles=row['triangle_indices'])
        interpolation=[]
        for i,name in enumerate(spec['objects']):
            worst=max((maximum(refs[k][i]['vertices'],(refs[k-1][i]['vertices']+refs[k+1][i]['vertices'])/2) for k in range(1,len(times)-1,2)),default=0.)
            interpolation.append({'source':name,'midpoint_indexed_error':worst,'tolerance':tolerance,'ok':worst<=tolerance})
        atomic_json(job/'exchange-clip-interpolation.json',interpolation)
        if not all(row['ok'] for row in interpolation):raise Failure('VALIDATION_FAILED','Native midpoint curvature exceeds declared linear clip budget; increase explicit substeps or review source')
        # A simulation receipt proves integers only. Verify this clip's fractional
        # observations in reverse after an independent source reopen as well.
        bpy.ops.wm.open_mainfile(filepath=params['file'],load_ui=False,use_scripts=False);prepare_source(params,spec,job)
        replay=[];progress(job,'source_reopen_reverse')
        for frame,expected in reversed(list(zip(times,refs))):
            observed=capture(spec['objects'],frame,profile)
            if any(not topology_match(a,b) for a,b in zip(expected,observed)):raise Failure('VALIDATION_FAILED','Source replay topology changed')
            errors=[maximum(a['vertices'],b['vertices']) for a,b in zip(expected,observed)]
            replay.append({'frame':frame,'errors':errors})
            if max(errors)>min(tolerance,1e-6):raise Failure('VALIDATION_FAILED','Source fractional cache/evaluation is not repeatable after reopen')
        atomic_json(job/'exchange-clip-source-replay.json',replay)
        bpy.ops.wm.read_factory_settings(use_empty=True);scene=bpy.context.scene;scene.name='EvaluatedMeshClip'
        scene.render.fps=fps*substeps;scene.frame_start=start*substeps;scene.frame_end=end*substeps
        output_names=[]
        for i,reference in enumerate(first):
            name='Clip_%02d'%i;output_names.append(name);mesh=bpy.data.meshes.new('ClipMesh_%02d'%i)
            mesh.from_pydata(reference['vertices'].tolist(),reference['loose_edges'],reference['faces']);mesh.update()
            obj=bpy.data.objects.new(name,mesh);scene.collection.objects.link(obj);obj.select_set(True)
            for layer,uv in reference['uv'].items():mesh.uv_layers.new(name=layer).data.foreach_set('uv',uv.ravel())
            obj.shape_key_add(name='Basis')
            if len(keys)>1:
                bag=rig_exchange.action(obj.data.shape_keys,'ClipMorph_%02d'%i,'KEY')
                for j in range(1,len(keys)):
                    key=obj.shape_key_add(name='Sample_%03d'%j);key.data.foreach_set('co',refs[2*j][i]['vertices'].ravel())
                    rig_exchange.curve(bag,key.path_from_id('value'),0,[k*substeps for k in keys],[1. if k==j else 0. for k in range(len(keys))])
        output_fps=fps*substeps

        def verify(stage,indexed=False):
            if len([o for o in bpy.context.scene.objects if o.type=='MESH'])!=len(output_names):raise Failure('VALIDATION_FAILED','Mesh clip output count changed')
            if abs(bpy.context.scene.render.fps/bpy.context.scene.render.fps_base-output_fps)>1e-5:raise Failure('VALIDATION_FAILED','Mesh clip output fps changed')
            checks=[]
            for ti in reversed(range(len(times))) if stage=='reopen' else range(len(times)):
                observed=capture(output_names,times[ti]*substeps,None,limit=500000)
                for i,(expected,actual) in enumerate(zip(refs[ti],observed)):
                    check=exchange.compare_geometry(expected,actual)
                    check.update(source=spec['objects'][i],output=output_names[i],source_frame=times[ti],output_frame=times[ti]*substeps,declared_position_tolerance=tolerance)
                    check['ok']=bool(check['ok'] and check['bidirectional_vertex_distance']<=tolerance)
                    if indexed:
                        check['indexed_vertex_error']=maximum(expected['vertices'],actual['vertices']);check['ok'] &= check['indexed_vertex_error']<=tolerance
                    checks.append(check)
            atomic_json(job/('exchange-clip-'+stage+'-checks.json'),checks)
            if not all(row['ok'] for row in checks):raise Failure('VALIDATION_FAILED','Evaluated mesh clip '+stage+' surface exceeds declared budget')
            return checks
        progress(job,'native_copy');verify('native',True)
        scene.frame_set(start*substeps);bpy.context.preferences.filepaths.save_version=0
        bpy.ops.wm.save_as_mainfile(filepath=str(job/'exchange-clip-native.blend'),relative_remap=False,check_existing=False)
        path=job/('export'+exchange.SUFFIX[spec['format']]);exchange.export_file(path,{**spec,'frame_start':start*substeps,'frame_end':end*substeps})
        progress(job,'import');bpy.ops.wm.read_factory_settings(use_empty=True);bpy.context.scene.render.fps=output_fps
        exchange.import_file(path,spec['format'],'ANIMATION');checks=verify('import')
        candidate=exchange.save_candidate(job);bpy.ops.wm.open_mainfile(filepath=str(candidate),load_ui=False,use_scripts=False);verify('reopen')
        report={'exchange_report_version':'1.0','operation':'export','format':spec['format'],'mode':'ANIMATION','settings':spec,'checks':checks,
            'outputs':[{'file':str(path),'sha256':digest(path),'bytes':path.stat().st_size}],'candidate':str(candidate),'candidate_sha256':digest(candidate),'resource_hashes':hashes,'seconds':time.monotonic()-started,'source_saved':False,'reopen':'pass','material_profile':[],
            'losses':['Full viewport-evaluated world surface baked to morph animation; native controllers, editable skin and source shape semantics remain in source.',
                'Materials omitted explicitly. Fixed source topology/UV only; exchange shading and UV equivalence not proven. Every key interval midpoint verified; not a continuous-time or artistic clothing-fit guarantee.',
                'Output frame and fps both multiplied by substeps; duration and seconds preserved. Use reported output_fps when importing GLB.'],
            'geometry_transfer':{'adapter':spec['geometry_adapter'],'source_fps':fps,'output_fps':output_fps,'source_frames':times,'output_frames':[f*substeps for f in times],'key_frames':keys,'vertex_samples':observations,
                'source_replay_max_error':max(max(r['errors']) for r in replay),'interpolation':interpolation,'modifiers_evaluated':mods,
                'meshes':[{'source':n,'output':output_names[i],'vertices':len(first[i]['vertices'])} for i,n in enumerate(spec['objects'])]}}
        atomic_json(job/'exchange-report.json',report);progress(job,'verified');return report
