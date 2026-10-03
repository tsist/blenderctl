# SPDX-License-Identifier: GPL-3.0-or-later
"""Byte-identical native rig package, source-bound receipts and reopen witnesses."""
import shutil,time
from pathlib import Path
import bpy,numpy as np
from protocol import Failure,atomic_json,digest
import drivers,mesh_clip,projects
from rig_package_contract import normalize

def inventory():
    # Byte identity is the exhaustive preservation proof. These counts describe
    # native editable content without pretending the inventory is a serializer.
    ids=list(drivers.owners());rigs=[];meshes=[]
    for o in bpy.data.objects:
        if o.type=='ARMATURE':
            rigs.append({'name':o.name,'bones':len(o.data.bones),'bbones':sum(b.bbone_segments>1 for b in o.data.bones),'constraints':sum(len(p.constraints) for p in o.pose.bones)})
        if o.type=='MESH':
            meshes.append({'name':o.name,'vertices':len(o.data.vertices),'groups':len(o.vertex_groups),'shape_keys':len(o.data.shape_keys.key_blocks) if o.data.shape_keys else 0,'bindings':[{'name':m.name,'rig':m.object.name if m.object else None} for m in o.modifiers if m.type=='ARMATURE']})
    return {'rigs':rigs,'meshes':meshes,'actions':[{'name':a.name,'slots':[s.identifier for s in a.slots]} for a in bpy.data.actions],'drivers':sum(len(o.animation_data.drivers) for o,_ in ids if getattr(o,'animation_data',None)),'objects':len(bpy.data.objects),'collections':len(bpy.data.collections),'embedded_cloth':[{'object':o.name,'modifier':m.name,'baked':m.point_cache.is_baked} for o in bpy.data.objects for m in o.modifiers if m.type=='CLOTH']}

def preflight(params,spec,job):
    bpy.ops.wm.open_mainfile(filepath=params['file'],load_ui=False,use_scripts=False)
    if tuple(bpy.data.version[:2])!=tuple(bpy.app.version[:2]):raise Failure('UNSUPPORTED','Native rig package requires matching Blender major/minor')
    if bpy.data.libraries or bpy.data.cache_files or bpy.data.volumes or projects.file_paths():raise Failure('UNSUPPORTED','Native rig package requires self-contained local data and packed resources')
    if len(bpy.data.scenes)!=1 or not bpy.data.armatures:raise Failure('UNSUPPORTED','Native rig package requires one scene containing native armatures')
    if any(o.particle_systems or any(m.type in ('FLUID','SOFT_BODY') for m in o.modifiers) for o in bpy.data.objects):raise Failure('UNSUPPORTED','Native rig package supports embedded RIG_CLOTH_V1 caches only')
    for o in bpy.data.objects:
        for m in o.modifiers:
            if m.type=='CLOTH' and (m.point_cache.use_external or m.point_cache.use_disk_cache or not m.point_cache.is_baked):raise Failure('UNSUPPORTED','Native rig package requires baked embedded cloth')
    clip={**spec,'frame_start':spec['frames'][0],'frame_end':spec['frames'][-1]}
    mesh_clip.prepare_source(params,clip,job)
    if bpy.context.scene.render.fps_base!=1 or bpy.context.scene.unit_settings.scale_length!=1:raise Failure('UNSUPPORTED','Native rig package witnesses require integer fps and meter scale one')
    return inventory()

def package(params,job):
    started=time.monotonic();spec=normalize(params['manifest']);source=Path(params['file']);expected=params['expected_sha256'];profile=params['driver_profile']
    if digest(source)!=expected:raise Failure('CONFLICT','Native rig source SHA mismatch')
    before=preflight(params,spec,job);atomic_json(job/'rig-package-inventory.json',before)
    references=[];count=0
    for index,t in enumerate(spec['frames']):
        rows=mesh_clip.capture(spec['objects'],t,profile);count+=sum(len(r['vertices']) for r in rows)
        if count>16000000:raise Failure('UNSUPPORTED','Native rig witnesses exceed 16 million vertex samples')
        # Keep invariant face/UV tables only once across witness frames.
        if references:
            for first,row in zip(references[0],rows):
                for key in ('faces','loose_edges'):
                    if first[key]==row[key]:row[key]=first[key]
                if set(first['uv'])==set(row['uv']) and all(np.array_equal(first['uv'][k],row['uv'][k]) for k in row['uv']):row['uv']=first['uv']
        for row in rows:row.pop('triangle_indices',None)
        references.append(rows);atomic_json(job/'rig-package-progress.json',{'phase':'source_witness','index':index})
    # Copy only after admission and source observation; never serialize away
    # unsupported native semantics or invalidate embedded cache/profile hashes.
    candidate=job/'rig-native.blend'
    with source.open('rb') as src,candidate.open('xb') as dst:shutil.copyfileobj(src,dst,1024*1024)
    if digest(candidate)!=expected:raise Failure('VALIDATION_FAILED','Native rig byte copy differs')
    output_params={**params,'file':str(candidate)};after=preflight(output_params,spec,job)
    if after!=before:raise Failure('VALIDATION_FAILED','Native rig reopen inventory differs')
    checks=[]
    for t,reference in reversed(list(zip(spec['frames'],references))):
        observed=mesh_clip.capture(spec['objects'],t,profile)
        for a,b in zip(reference,observed):
            topology=len(a['vertices'])==len(b['vertices']) and a['faces']==b['faces'] and a['loose_edges']==b['loose_edges'];error=mesh_clip.maximum(a['vertices'],b['vertices']) if topology else None
            uv=set(a['uv'])==set(b['uv']) and all(np.array_equal(a['uv'][k],b['uv'][k]) for k in a['uv'])
            checks.append({'object':a['name'],'requested_frame':t,'actual_frame':float(bpy.context.scene.frame_current_final),'vertices':len(a['vertices']),'topology_ok':topology,'uv_ok':uv,'position_error':error,'ok':topology and uv and error==0})
    atomic_json(job/'rig-package-checks.json',checks)
    if not all(c['ok'] for c in checks):raise Failure('VALIDATION_FAILED','Native rig reopen witnesses differ')
    atomic_json(job/'rig-driver-profile.json',profile);receipt=None
    if 'simulation_receipt' in params:
        source_receipt=Path(params['simulation_receipt']['file']);target=job/'rig-simulation-receipt.json'
        with source_receipt.open('rb') as src,target.open('xb') as dst:shutil.copyfileobj(src,dst)
        if digest(target)!=params['simulation_receipt']['expected_sha256']:raise Failure('CONFLICT','Native rig receipt changed')
        receipt={'file':str(target),'expected_sha256':digest(target)}
    report={'rig_package_version':'1.0','adapter':spec['adapter'],'settings':spec,'candidate':str(candidate),'candidate_sha256':expected,'source_sha256':expected,'source_saved':False,'byte_identical':True,'self_contained':True,'reopen':'pass','inventory':before,'checks':checks,'driver_profile':profile,'simulation_receipt':receipt,'seconds':time.monotonic()-started,'scope':'Whole native Blender project preserved byte for byte, including unselected data, control/constraint/driver/skin/shape/action semantics. Witnesses cover requested mesh frames only. No render, fitting, collision or arbitrary editing guarantee; edits invalidate source-bound profiles and simulation receipts.'}
    atomic_json(job/'rig-package-report.json',report);return report
