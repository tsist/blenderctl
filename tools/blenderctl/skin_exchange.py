# SPDX-License-Identifier: GPL-3.0-or-later
"""Per-mesh checked multi-cage B-Bone exchange using the native segment oracle."""
import time
import bpy
from protocol import Failure,atomic_json,digest
import exchange,rig_exchange


def export_multi(params,spec,job,started,hashes):
    selected=[bpy.data.objects[n] for n in spec['objects']]
    rigs=[o for o in selected if o.type=='ARMATURE'];meshes=[o for o in selected if o.type=='MESH']
    if len(rigs)!=1 or not 1<=len(meshes)<=16 or len(selected)!=len(meshes)+1:
        raise Failure('UNSUPPORTED','Multi cage selects one source rig and 1..16 meshes')
    if sum(len(o.data.vertices) for o in meshes)>100000:
        raise Failure('UNSUPPORTED','Multi cage exceeds 100000 source vertices')
    data=[]
    for index,mesh in enumerate(meshes):
        folder=job/('mesh-%02d'%index);folder.mkdir()
        item=rig_exchange.capture({**spec,'objects':[mesh.name,rigs[0].name]},params['driver_profile'],folder)
        item['folder']=folder;item['output_name']='CageBody_%02d'%index;data.append(item)
    if sum(len(d['joints']) for d in data)>2048:
        raise Failure('UNSUPPORTED','Multi cage exceeds 2048 total affine joints / 4096 export bones')
    bpy.ops.wm.read_factory_settings(use_empty=True)
    for index,item in enumerate(data):rig_exchange.build(item,item['folder'],reset=False,suffix='_%02d'%index,stable_euler=spec['format']=='FBX')
    bpy.context.scene.frame_set(spec['frame_start'])
    bpy.context.preferences.filepaths.save_version=0
    bpy.ops.wm.save_as_mainfile(filepath=str(job/'cage-transfer.blend'),relative_remap=False,check_existing=False)
    path=job/('export'+exchange.SUFFIX[spec['format']]);exchange.export_file(path,spec)
    bpy.ops.wm.read_factory_settings(use_empty=True);bpy.context.scene.render.fps=data[0]['fps']
    exchange.import_file(path,spec['format'],'ANIMATION')

    def verify(reopening=False):
        if len([o for o in bpy.context.scene.objects if o.type=='MESH'])!=len(data):
            raise Failure('VALIDATION_FAILED','Multi cage mesh count changed')
        rows=[];checks=[]
        for item in data:
            r=item['report'];name=item['output_name']
            node=bpy.data.objects.get(name)
            if node and node.type=='EMPTY':
                mesh=bpy.data.objects.get(name.replace('CageBody_','CageMesh_'))
                if not mesh or mesh.type!='MESH' or mesh.parent!=node.parent:
                    raise Failure('VALIDATION_FAILED','Imported skin node does not have its expected mesh sibling')
                name=mesh.name
            mesh=bpy.data.objects.get(name)
            if not mesh or not any(m.type=='ARMATURE' and m.object and m.object.name==item['output_name'].replace('CageBody_','CageRig_') for m in mesh.modifiers):
                raise Failure('VALIDATION_FAILED','Imported mesh is bound to the wrong per-mesh skeleton')
            skeletal=rig_exchange.structure(r['export_bones'],len(item['samples']),name)
            observations=[exchange.snapshot(ref['frame'],objects=[name]) for ref in item['expected']]
            local_checks=[exchange.compare_geometry(a,b) for a,b in zip(item['expected'],observations)]
            atomic_json(item['folder']/('reopen-checks.json' if reopening else 'import-checks.json'),local_checks)
            if not all(c['ok'] for c in local_checks):
                raise Failure('VALIDATION_FAILED','Imported mesh differs from native cage: '+r['source_mesh'])
            checks.extend(local_checks)
            rows.append({'source':r['source_mesh'],'output':name,'source_shape_keys':r['source_shape_keys'],
                         'source_vertices':r['cage_vertices'],'independent_lbs_max_error':r['independent_lbs_max_error'],
                         'map':str(item['folder']/'cage-transfer-map.json'),'omitted_post_modifiers':r['omitted_post_modifiers'],**skeletal})
        return rows,checks
    rows,checks=verify();candidate=exchange.save_candidate(job)
    bpy.ops.wm.open_mainfile(filepath=str(candidate),load_ui=False,use_scripts=False);verify(True)
    report={'exchange_report_version':'1.0','operation':'export','format':spec['format'],'mode':'ANIMATION','settings':spec,
            'checks':checks,'outputs':[{'file':str(path),'sha256':digest(path),'bytes':path.stat().st_size}],
            'candidate':str(candidate),'candidate_sha256':digest(candidate),'resource_hashes':hashes,'seconds':time.monotonic()-started,
            'source_saved':False,'reopen':'pass','material_profile':[],
            'losses':['Selected base cages only; listed post modifiers and materials omitted in exchange copy.',
                      'Each mesh gets its own ordinary affine-factor skeleton; native controllers and editable shape semantics stay in source.',
                      'Morph targets sample native pre-armature shape deformation; only requested integer frames are verified.',
                      'FBX uses equivalent signed-axis SVD factors away from Euler poles and scoped 5.2.1 importer sibling traversal correction; bundled files unchanged.' if spec['format']=='FBX' else 'GLB skin and morph animation use a single scene clip.'],
            'rig_transfer':{'adapter':'BBONE_MULTI_CAGE_V1','frames':list(range(spec['frame_start'],spec['frame_end']+1)),'meshes':rows}}
    atomic_json(job/'exchange-report.json',report);return report
