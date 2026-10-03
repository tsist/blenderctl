# SPDX-License-Identifier: GPL-3.0-or-later
"""Bounded DAG and resource declarations; candidate production only, no publication."""
import copy,os,re
from pathlib import Path
from protocol import Failure
from scene_contract import obj,array,enum,integer,number,validate
ALLOWED=('query','diff','dependency.audit','validate','identity.prepare','asset.prepare','asset.index','scene.prepare','scene.inspect','model.prepare','model.inspect','node.prepare','node.inspect','material.preview','texture.bake','rig.prepare','rig.inspect','animation.sample','simulation.prepare','simulation.inspect','simulation.bake','render.run','media.prepare','media.export','exchange.export','exchange.import','exchange.convert','extension.prepare','extension.run')
ALLOWED=(*ALLOWED,'asset.preview.prepare','dependency.plan-relink','dependency.prepare','sculpt.replay','rig.package')
ALLOWED=(*ALLOWED,'tracking.prepare','tracking.inspect','tracking.solve')
ALLOWED=(*ALLOWED,'inspect','project.verify','project.prepare_copy','project.link.prepare','project.override.prepare','project.override.resync','exchange.analyze')
ID={'type':'string','pattern':'^[A-Za-z][A-Za-z0-9_-]{0,47}$'}
POINTER={'type':'string','minLength':2,'maxLength':256,'pattern':'^/[^~]+$'}
LIMITS=obj({'cpu_threads':integer(1,8),'memory_mb':integer(256,32768),'disk_mb':integer(1,32768),'gpu_mb':integer(0,32768)})
POLICY=obj({'max_workers':integer(1,4),'cpu_threads':integer(1,32),'memory_mb':integer(256,65536),'gpu_mb':integer(0,32768),'disk_mb':integer(1,65536),'min_free_mb':integer(0,1048576),'failure':enum('continue','fail_fast')})
BINDING=obj({'target':POINTER,'sha_target':POINTER,'step':ID,'artifact':POINTER})
POLICY['properties']['resource_domain']={'type':'string','minLength':1}
STEP=obj({'id':ID,'command':enum(*ALLOWED),'params':{'type':'object'},'depends_on':{**array(ID,0,32),'uniqueItems':True},'bindings':array(BINDING,0,100),'limits':LIMITS,'timeout_seconds':number(.05,86400),'reuse':{'type':'boolean'}})
MANIFEST=obj({'pipeline_version':{'const':'1.0'},'policy':POLICY,'steps':array(STEP,1,32)})
SUCCESS=('succeeded','reused');TERMINAL=(*SUCCESS,'failed','blocked','cancelled','timed_out')

def parts(pointer):
    p=pointer.split('/')[1:]
    if not p or any(not k or k in ('.','..') for k in p):raise Failure('INVALID_REQUEST','Invalid binding pointer')
    return p

def get(v,pointer):
    try:
        for k in parts(pointer):v=v[int(k)] if isinstance(v,list) else v[k]
        return v
    except (KeyError,ValueError,IndexError,TypeError) as ex:raise Failure('INVALID_REQUEST','Binding pointer does not exist: '+pointer) from ex

def put(v,pointer,value):
    p=parts(pointer)
    try:
        for k in p[:-1]:v=v[int(k)] if isinstance(v,list) else v[k]
        if isinstance(v,list):v[int(p[-1])]=value
        elif isinstance(v,dict):v[p[-1]]=value
        else:raise TypeError()
    except (KeyError,ValueError,IndexError,TypeError) as ex:raise Failure('INVALID_REQUEST','Invalid binding target: '+pointer) from ex

def normalize(v):
    v=copy.deepcopy(v);validate(v,MANIFEST);steps=v['steps'];ids=[s['id'] for s in steps];policy=v['policy']
    if 'resource_domain' in policy:
        from resource_leases import domain_path
        policy['resource_domain']=str(domain_path(policy['resource_domain']))
    if len(set(ids))!=len(ids):raise Failure('INVALID_REQUEST','Duplicate pipeline step ID')
    for s in steps:
        if s['id'] in s['depends_on'] or set(s['depends_on'])-set(ids):raise Failure('INVALID_REQUEST','Self/unknown dependency')
        targets=[]
        for b in s['bindings']:
            if b['step'] not in s['depends_on']:raise Failure('INVALID_REQUEST','Binding source must be an explicit direct dependency')
            if parts(b['target'])[-1] not in ('file','other') or parts(b['sha_target'])[-1] not in ('expected_sha256','other_sha256'):raise Failure('INVALID_REQUEST','Bindings target file/hash fields only')
            parts(b['artifact']);targets.extend([b['target'],b['sha_target']])
        if len(set(targets))!=len(targets):raise Failure('INVALID_REQUEST','Duplicate binding targets')
        for key in ('cpu_threads','memory_mb','gpu_mb'):
            if s['limits'][key]>policy[key]:raise Failure('INVALID_REQUEST','Step cannot fit pipeline '+key+' budget')
        if s['limits']['disk_mb']>policy['disk_mb']:raise Failure('INVALID_REQUEST','Step disk budget exceeds pipeline budget')
    done=set()
    while len(done)<len(steps):
        ready=[s['id'] for s in steps if s['id'] not in done and set(s['depends_on'])<=done]
        if not ready:raise Failure('INVALID_REQUEST','Pipeline dependency cycle')
        done.update(ready)
    return v

def normalize_request(command,p):
    if command=='pipeline.resume':
        if set(p)!={'job_id'}:raise Failure('INVALID_REQUEST','Resume requires job_id')
    else:
        if 'manifest' not in p:raise Failure('INVALID_REQUEST','Pipeline requires manifest')
        p['manifest']=normalize(p['manifest'])
    for key in ('job_id','reuse_job'):
        if key in p and (not isinstance(p[key],str) or not re.fullmatch('[0-9a-f]{32}',p[key])):raise Failure('INVALID_REQUEST','Prior pipeline job ID must be 32 lowercase hex characters')
    return p

def disk_usage(root):
    if os.name=='nt':
        from directory_handles import disk_usage as handle_usage
        return handle_usage(root)
    total=0
    for directory,dirs,files in os.walk(root,followlinks=False):
        dirs[:]=[d for d in dirs if not Path(directory,d).is_symlink() and not Path(directory,d).is_junction()]
        for name in files:
            p=Path(directory,name)
            try:
                if not p.is_symlink():total+=p.stat().st_size
            except FileNotFoundError:pass
    return total
