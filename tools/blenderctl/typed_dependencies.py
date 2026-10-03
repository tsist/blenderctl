# SPDX-License-Identifier: GPL-3.0-or-later
"""Finite native file closure. Availability is separate from domain validity."""
import copy
import hashlib
import json
from contextlib import ExitStack
from pathlib import Path
import bpy
from protocol import Failure, atomic_json, read_json
from filesystem import FileGuard
from inspection import path_value, all_ids, identity
from dependency_contract import normalize_closure

REGISTRY=[
 {'kind':'image','adapter':'IMAGE_FILES_V1','coverage':'FILE, packed, declared UDIM tiles, image-user sequence frames'},
 {'kind':'image_sequence_binding','adapter':'IMAGE_SEQUENCE_V1','coverage':'explicit image-user frame mapping'},
 {'kind':'library','adapter':'LIBRARY_FILES_V1','coverage':'recursive native library paths; visited set and cycle detection'},
 {'kind':'font','adapter':'FONT_FILE_V1','coverage':'builtin, packed or single file'},
 {'kind':'sound','adapter':'MEDIA_FILE_V1','coverage':'packed or complete audio container'},
 {'kind':'movieclip','adapter':'MEDIA_FILE_V1','coverage':'complete movie container; sequence requires explicit members'},
 {'kind':'vse_image','adapter':'VSE_ELEMENTS_V1','coverage':'all declared strip elements; conservative superset of timeline'},
 {'kind':'vse_movie','adapter':'MEDIA_FILE_V1','coverage':'complete movie container'},
 {'kind':'vse_sound','adapter':'MEDIA_FILE_V1','coverage':'complete audio container'},
 {'kind':'point_cache','adapter':'SIMULATION_RECEIPT_V1','coverage':'verified native simulation receipt only'},
 {'kind':'fluid_cache','adapter':'SIMULATION_RECEIPT_V1','coverage':'verified native simulation receipt only'},
 {'kind':'rigidbody_cache','adapter':'SIMULATION_RECEIPT_V1','coverage':'verified native simulation receipt only'},
 {'kind':'geometry_nodes_cache','adapter':'GEOMETRY_ZONE_V1','coverage':'S03 receipt, exact members and reverse-frame native replay'},
 {'kind':'geometry_nodes_bake','adapter':'GEOMETRY_ZONE_V1','coverage':'S03 receipt only'},
 {'kind':'declared_plugin','adapter':'custom-property-files-v1','coverage':'explicit hashed member contract; plugin execution not certified'},
 {'kind':'declared_cache','adapter':'custom-property-files-v1','coverage':'explicit hashed member and frame contract; solver validity not certified'},
 {'kind':'cache_file','adapter':None,'coverage':'unknown native cache format; retained as partial, never inferred closed'},
 {'kind':'volume','adapter':None,'coverage':'volume format and frame adapter pending'},
]
ADAPTERS={r['kind']:r['adapter'] for r in REGISTRY}
CACHE_KINDS={'point_cache','fluid_cache','rigidbody_cache','geometry_nodes_cache','geometry_nodes_bake'}

def canon(path):return str(Path(path).resolve())
def pathkey(path):return canon(path).casefold()
def fingerprint(value):return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=True,allow_nan=False).encode()).hexdigest()
def digest(path):
    with FileGuard(path) as g:return g.sha256()
def implementation():return fingerprint({p.name:digest(p) for p in sorted(Path(__file__).parent.glob('*.py'))})

def validate_report(report,source=None):
    if not isinstance(report,dict) or report.get('version')!='1.0' or report.get('adapter')!='TYPED_FILES_V1':raise Failure('INVALID_REQUEST','Expected typed dependency baseline')
    if report.get('implementation_sha256')!=implementation() or report.get('blender_build')!=bpy.app.build_hash.decode():raise Failure('CONFLICT','Dependency baseline implementation/build changed')
    if source and (pathkey(report['source']['file'])!=pathkey(source['file']) or report['source']['expected_sha256']!=source['expected_sha256']):raise Failure('CONFLICT','Dependency baseline source changed')
    if report.get('closure')!='closed':raise Failure('CONFLICT','Relink baseline must be a previously closed file inventory')

def detect_cycles(edges):
    graph={}
    for edge in edges:graph.setdefault(edge['from'],set()).add(edge['to'])
    done=set();active=[];cycles=[]
    def walk(node):
        if node in active:
            cycles.append(active[active.index(node):]+[node]);return
        if node in done:return
        active.append(node)
        for child in sorted(graph.get(node,())):walk(child)
        active.pop();done.add(node)
    for node in sorted(graph):walk(node)
    return cycles

def run(params,job):
    report=audit(params,job)
    atomic_json(job/'dependency-closure.json',report)
    return report

def audit(params,job):
    profile=copy.deepcopy(params.get('profile',{}));config=normalize_closure(profile.get('closure',{}));root=canon(params['file'])
    profile['closure']=config
    frames=[]
    if profile.get('timeline'):
        t=profile['timeline'];frames=list(range(t['start'],t['end']+1,t.get('step',1)))
    documents={};resources=[];edges=[];issues=[];visited={};total_bytes=0
    cache_receipt=None;cache_runtime=None
    with ExitStack() as guards:
        def document(path,expected=None):
            nonlocal total_bytes
            p=canon(path);k=pathkey(p)
            if k not in documents:
                if len(documents)>=config['max_files']:raise Failure('UNSUPPORTED','Typed dependency file budget exceeded')
                guard=guards.enter_context(FileGuard(p));sha=guard.sha256();size=Path(p).stat().st_size
                total_bytes+=size
                if total_bytes>4*1024**3:raise Failure('UNSUPPORTED','Typed dependency byte budget exceeds 4 GiB')
                documents[k]={'file':p,'expected_sha256':sha}
            result=documents[k]
            if expected is not None and result['expected_sha256']!=expected:raise Failure('CONFLICT','Dependency SHA changed: '+p)
            return result
        source=document(root,params.get('expected_sha256'))
        if config.get('simulation_receipt'):
            descriptor=config['simulation_receipt'];document(descriptor['file'],descriptor['expected_sha256']);cache_receipt=read_json(descriptor['file'])
            from simulation_contract import receipt_contract
            receipt_contract(cache_receipt)
            if cache_receipt.get('candidate_sha256')!=source['expected_sha256']:raise Failure('CONFLICT','Simulation receipt belongs to another source')
            request=cache_receipt['request']
            if request.get('adapter')=='RIG_CLOTH_V1':raise Failure('UNSUPPORTED','Rig cloth dependency replay requires a driver-profile adapter')
            if not frames or min(frames)<request['frame_start'] or max(frames)>request['frame_end'] or profile['timeline']['scene']!=request['scene']:raise Failure('CONFLICT','Dependency timeline lies outside cache receipt')
            for f in cache_receipt['files']+cache_receipt.get('resources',[]):document(f['file'],f['expected_sha256'])
            import simulation
            replay_dir=job/'dependency-cache-replay';replay_dir.mkdir()
            replay={'file':root,'expected_sha256':source['expected_sha256'],'manifest':{**request,'mode':'reuse','receipt':descriptor}}
            if cache_receipt.get('adapter')=='GEOMETRY_ZONE_V1':replay['resources']=cache_receipt.get('resources',[])
            cache_runtime=simulation.bake(replay,replay_dir)
            if cache_receipt.get('adapter')!='GEOMETRY_ZONE_V1':
                # Legacy receipts compare three solver samples. Also read every
                # requested integer frame; file closure must not infer playback
                # solely from directory names or a baked flag.
                atomic_json(replay_dir/'dependency-frame-samples.json',simulation.sample(list(reversed(frames))))
        pending=[(root,0)]
        while pending:
            file,depth=pending.pop(0);file=canon(file);filekey=pathkey(file)
            if filekey in visited:continue
            if depth>config['max_depth']:
                issues.append({'code':'DEPTH_LIMIT','file':file});continue
            if not Path(file).is_file():
                issues.append({'code':'LIBRARY_MISSING','file':file});continue
            document(file);visited[filekey]={'file':file,'depth':depth,'requirement':'provenance','purpose':'native owner file; bytes remain required closure inputs'}
            bpy.ops.wm.open_mainfile(filepath=file,load_ui=False,use_scripts=False)
            visited[filekey]['saved_version']=list(bpy.data.version)
            if tuple(bpy.data.version[:2])!=tuple(bpy.app.version[:2]):issues.append({'code':'VERSION_UNVERIFIED','file':file,'saved_version':list(bpy.data.version)})
            local_profile={k:v for k,v in profile.items() if k!='closure'};local_profile['hash_resources']=False
            if filekey!=pathkey(root):
                local_profile.pop('resources',None)
                if frames and bpy.data.scenes:local_profile['timeline']={**local_profile['timeline'],'scene':bpy.data.scenes[0].name}
                elif frames:local_profile.pop('timeline',None)
            from inspection import snapshot
            old=snapshot(file,local_profile,dependency_only=True)
            # Socket-computed filenames (for example geometry import nodes) need
            # not appear in blend_paths. Only registered node dependency behavior
            # may participate in a closed report; unfamiliar nodes stay unknown.
            from node_contract import NODE_TYPES
            from node_zones import TYPES as ZONE_TYPES
            seen_trees=set()
            for owner in all_ids():
                if owner.library:continue
                tree=owner if isinstance(owner,bpy.types.NodeTree) else getattr(owner,'node_tree',None)
                if not tree or tree.as_pointer() in seen_trees:continue
                seen_trees.add(tree.as_pointer());allowed=set(NODE_TYPES.get(tree.bl_idname,()))|ZONE_TYPES
                if tree.bl_idname=='ShaderNodeTree':allowed.add('ShaderNodeOutputLight')
                for node in tree.nodes:
                    if node.bl_idname not in allowed:issues.append({'code':'NODE_DEPENDENCY_UNKNOWN','file':file,'owner':identity(owner),'node':node.name,'type':node.bl_idname})
            # Libraries are read from each native Main independently. Blender may
            # flatten parent pointers on save; never reconstruct paths from names.
            for lib in bpy.data.libraries:
                if lib.is_library_indirect:continue
                if getattr(lib,'packed_file',None):continue
                target=canon(path_value(lib.filepath));edges.append({'from':file,'to':target,'raw_path':lib.filepath})
                pending.append((target,depth+1))
                stable={'owner_file':file,'owner':identity(lib),'kind':'library','binding':{}}
                exists=Path(target).is_file()
                resources.append({**stable,'resource_id':fingerprint(stable),'adapter':'LIBRARY_FILES_V1','requirement':'required','raw_path':lib.filepath,'resolved_path':target,'source':None,'status':'exists' if exists else 'missing','members':[{**document(target),'bytes':Path(target).stat().st_size}] if exists else [],'complete':exists,'evidence':'native_library_reference','reachability':'scene_or_asset'})
            expanded={json.dumps((d.get('binding') or {}).get('image'),sort_keys=True) for d in old['items'] if d['kind']=='image_sequence_binding'}
            known_paths=set()
            for dep in old['items']:
                if dep['owner'].get('library'):continue
                if dep['kind']=='library':continue
                binding=dep.get('binding') or {}
                if dep.get('source')=='SEQUENCE' and not binding.get('node') and json.dumps(dep['owner'],sort_keys=True) in expanded:continue
                stable={'owner_file':file,'owner':dep['owner'],'kind':dep['kind'],'binding':binding}
                row={**stable,'resource_id':fingerprint(stable),'adapter':ADAPTERS.get(dep['kind']),'requirement':'optional' if dep.get('reachability')=='retained_unreferenced' else 'required','raw_path':dep.get('raw_path',''),'resolved_path':dep.get('resolved_path'),'source':dep.get('source'),'status':dep['status'],'members':[],'complete':False,'evidence':dep.get('completeness','single_container'),'reachability':dep.get('reachability')}
                row['frame_mapping']=dep.get('frame_mapping',[])
                for p in [dep.get('resolved_path'),*dep.get('expected_files',[])]:
                    if p:known_paths.add(pathkey(p))
                kind=dep['kind'];status=dep['status']
                if status in ('packed','builtin','generated') or dep.get('source')=='GENERATED':
                    row['complete']=True;row['evidence']='embedded_in_owner_file'
                elif kind in CACHE_KINDS:
                    if cache_receipt and filekey==pathkey(root):
                        directory=dep.get('resolved_path');members=[f for f in cache_receipt['files'] if directory and Path(f['file']).resolve().is_relative_to(Path(directory).resolve())]
                        row['members']=[{**document(f['file'],f['expected_sha256']),'bytes':Path(f['file']).stat().st_size} for f in members]
                        row['complete']=bool(members);row['evidence']='native_receipt_reverse_frame_replay' if members else 'cache_not_bound_to_receipt'
                        if kind in ('point_cache','rigidbody_cache') and binding.get('is_baked') and not binding.get('external') and cache_receipt.get('simulation_cache_version')=='1.0':
                            row['complete']=True;row['evidence']='embedded_baked_cache_native_receipt_replay'
                    else:row['evidence']='verified_cache_receipt_required'
                elif row['adapter']:
                    paths=dep.get('expected_files') or ([dep['resolved_path']] if status=='exists' else [])
                    excluded={pathkey(p) for p in dep.get('packed_members',[])}
                    expected_hashes={pathkey(p['path']):p['sha256'] for p in dep.get('fingerprints',[]) if 'path' in p and 'sha256' in p}
                    for p in paths:
                        if pathkey(p) in excluded:continue
                        if Path(p).is_file():row['members'].append({**document(p,expected_hashes.get(pathkey(p))),'bytes':Path(p).stat().st_size})
                    complete=bool(paths) and len(row['members'])==len({pathkey(p) for p in paths}-excluded) and status not in ('missing','incomplete','pattern_unresolved')
                    if dep.get('source')=='SEQUENCE':complete &= bool(frames) and dep.get('completeness')=='explicit_render_time_mapping'
                    if dep.get('source')=='TILED':complete &= bool(dep.get('expected_files'))
                    if kind.startswith('declared_'):
                        # The explicit adapter declares files, not arbitrary plugin behavior.
                        complete &= not dep.get('missing_frames') and not dep.get('hash_mismatches')
                    row['complete']=bool(complete)
                else:row['requirement']='unknown';row['evidence']='adapter_unavailable'
                if not row['complete']:issues.append({'code':'RESOURCE_UNRESOLVED','resource_id':row['resource_id'],'file':file,'kind':kind,'evidence':row['evidence']})
                resources.append(row)
            # Native registered file paths provide a second inventory. An absent
            # adapter cannot disappear merely because the old auditor omitted it.
            for raw in bpy.utils.blend_paths(absolute=True,packed=False,local=True):
                if not raw:continue
                p=canon(raw)
                if pathkey(p) in known_paths or any(pathkey(e['to'])==pathkey(p) and e['from']==file for e in edges):continue
                if pathkey(p)==filekey:continue
                issues.append({'code':'NATIVE_PATH_UNKNOWN','file':file,'path':p})
            missing_ids=[identity(i) for i in all_ids() if getattr(i,'is_missing',False)]
            if missing_ids:issues.append({'code':'MISSING_LINKED_IDS','file':file,'ids':missing_ids})
        cycles=detect_cycles(edges)
        if cycles:issues.append({'code':'LIBRARY_CYCLE','cycles':cycles})
        else:
            graph={}
            for edge in edges:graph.setdefault(edge['from'],set()).add(edge['to'])
            deepest={};todo=[(root,0)]
            while todo:
                node,depth=todo.pop()
                if depth<=deepest.get(node,-1):continue
                deepest[node]=depth
                if depth>config['max_depth']:
                    issues.append({'code':'DEPTH_LIMIT','file':node,'depth':depth});continue
                todo.extend((child,depth+1) for child in graph.get(node,()))
        unique={row['resource_id']:row for row in resources}
        report={'version':'1.0','adapter':'TYPED_FILES_V1','source':source,'profile':profile,'blender_build':bpy.app.build_hash.decode(),'implementation_sha256':implementation(),'scope':'all retained native registered file dependencies plus explicit plugin manifests; finite declared tiles and timeline; availability is not render/animation/licensing certification','registry':REGISTRY,'documents':sorted(documents.values(),key=lambda x:pathkey(x['file'])),'resources':list(unique.values()),'libraries':edges,'visited':list(visited.values()),'cycles':cycles,'issues':issues,'closure':'partial' if issues else 'closed','file_count':len(documents),'bytes':total_bytes,'cache_replay':'pass' if cache_runtime else 'not_run','source_saved':False}
        return report
