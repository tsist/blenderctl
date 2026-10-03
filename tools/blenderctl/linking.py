# SPDX-License-Identifier: GPL-3.0-or-later
"""Explicit library links and native single-library override hierarchies.

All writes are job candidates. Source libraries remain read-only and their
finite typed closure is held with FileGuard throughout mutation and reopening.
"""
import copy
import math
import uuid
from contextlib import ExitStack
from pathlib import Path
import bpy
from protocol import Failure, atomic_json, read_json
from filesystem import FileGuard
from inspection import all_ids, identity, record, path_value
from link_contract import COMMANDS, receipt_contract, pathkey
import typed_dependencies as typed

ADAPTER='LIBRARY_OVERRIDE_5_2_V1'
PROFILE={'closure':{'adapter':'TYPED_FILES_V1','max_depth':8,'max_files':1000}}

def ref(item):
    value=identity(item)
    if 'asset_id' in item:
        raw=item['asset_id']
        try:
            if not isinstance(raw,str) or uuid.UUID(raw).version!=4 or str(uuid.UUID(raw))!=raw.lower():raise ValueError()
        except (ValueError,AttributeError):raise Failure('CONFLICT','Invalid upstream asset_id UUIDv4 on '+item.name)
        value['asset_id']=raw
    return value

def refkey(value):return (value['type'],pathkey(value['library']) if value.get('library') else '',value['name'])

def matches(item,selector):
    value=ref(item)
    return refkey(value)==refkey(selector) and ('asset_id' not in selector or value.get('asset_id')==selector['asset_id'])

def find(selector,overrides=False):
    found=[]
    for item in all_ids():
        target=item.override_library.reference if overrides and item.override_library else item if not overrides else None
        if target and matches(target,selector):found.append(item)
    if len(found)!=1:raise Failure('CONFLICT','Library reference is absent or ambiguous: '+str(selector))
    if getattr(found[0],'is_missing',False):raise Failure('CONFLICT','Missing library data: '+str(selector))
    return found[0]

def open_file(path):
    bpy.ops.wm.open_mainfile(filepath=str(path),load_ui=False,use_scripts=False)
    if tuple(bpy.data.version[:2])!=(5,2):raise Failure('UNSUPPORTED','S05 supports saved Blender 5.2 files only; file subversion is recorded separately')

def activate(context):
    scene=bpy.data.scenes.get(context['scene'])
    layer=scene.view_layers.get(context['view_layer']) if scene else None
    if not scene or scene.library or not layer:raise Failure('CONFLICT','Explicit local scene/view layer does not resolve')
    bpy.context.window.scene=scene;bpy.context.window.view_layer=layer
    layer.update()
    return scene,layer

def state():
    result=[]
    for item in all_ids():
        row=record(item)
        if isinstance(item,bpy.types.Collection):
            row['membership']={'objects':sorted([ref(o) for o in item.objects],key=refkey),'children':sorted([ref(c) for c in item.children],key=refkey),'instance_offset':list(item.instance_offset)}
        if isinstance(item,bpy.types.Scene):row['root_membership']={'objects':sorted([ref(o) for o in item.collection.objects],key=refkey),'children':sorted([ref(c) for c in item.collection.children],key=refkey)}
        if isinstance(item,bpy.types.Object):row['instance']={'type':item.instance_type,'collection':ref(item.instance_collection) if item.instance_collection else None}
        if isinstance(item,bpy.types.Library):row['resolved_path']=path_value(item.filepath)
        if item.override_library:
            ov=item.override_library
            row['override']={'reference':ref(ov.reference) if ov.reference else None,'root':ref(ov.hierarchy_root) if ov.hierarchy_root else None,'system':ov.is_system_override,'properties':sorted([{'path':p.rna_path,'operations':[{'operation':o.operation,'index':o.subitem_local_index,'reference_index':o.subitem_reference_index} for o in p.operations]} for p in ov.properties],key=lambda p:p['path'])}
        result.append(row)
    return sorted(result,key=refkey)

def closure(descriptor,job,label):
    report=typed.audit({**descriptor,'profile':PROFILE},job)
    atomic_json(job/f'project-{label}-closure.json',report)
    if report['closure']!='closed':raise Failure('CONFLICT','Library dependency closure is partial; inspect project-'+label+'-closure.json')
    return report

def lock_documents(stack,documents,held):
    for d in documents:
        key=pathkey(d['file'])
        if key not in held:held[key]=stack.enter_context(FileGuard(d['file']))
        if held[key].sha256()!=d['expected_sha256']:raise Failure('CONFLICT','Upstream input changed: '+d['file'])

def upstream_inventory(closure_report,source=None):
    rows=[]
    for doc in closure_report['visited']:
        if source and pathkey(doc['file'])==pathkey(source):continue
        open_file(doc['file'])
        for item in all_ids():
            if item.library or isinstance(item,(bpy.types.Scene,bpy.types.Library)):continue
            ident=ref(item);ident['library']=str(Path(doc['file']).resolve())
            rows.append({'identity':ident,'content_sha256':typed.fingerprint(record(item))})
    return sorted(rows,key=lambda r:refkey(r['identity']))

def changes(old,new):
    def key(row):
        i=row['identity'];return (i['type'],pathkey(i['library']),i.get('asset_id') or i['name'])
    before={key(r):r for r in old};after={key(r):r for r in new}
    if len(before)!=len(old) or len(after)!=len(new):raise Failure('CONFLICT','Duplicate upstream UUID identity')
    return {'added':[after[k] for k in sorted(after.keys()-before.keys())],
            'removed':[before[k] for k in sorted(before.keys()-after.keys())],
            'changed':[{'before':before[k],'after':after[k]} for k in sorted(before.keys()&after.keys()) if before[k]!=after[k]]}

def check_native():
    missing=[ref(i) for i in all_ids() if getattr(i,'is_missing',False)]
    if missing:raise Failure('CONFLICT','Missing native IDs: '+str(missing))
    for item in all_ids():
        if item.override_library and (not item.override_library.is_in_hierarchy or (item.users==0 and not item.use_fake_user)):
            raise Failure('VALIDATION_FAILED','Unowned or nonhierarchical override ID: '+item.name)
    for lib in bpy.data.libraries:
        if lib.needs_liboverride_resync:raise Failure('VALIDATION_FAILED','Library still needs override resync: '+lib.filepath)
    for obj in bpy.data.objects:
        if any(not math.isfinite(v) for row in obj.matrix_world for v in row):raise Failure('VALIDATION_FAILED','Nonfinite object transform')

def overrides():
    return sorted([{'identity':ref(i),'reference':ref(i.override_library.reference),'root':ref(i.override_library.hierarchy_root),'system':i.override_library.is_system_override} for i in all_ids() if i.override_library and i.override_library.reference and i.override_library.hierarchy_root],key=lambda r:refkey(r['reference']))

def local_content():return [r for r in state() if r['library'] is None and 'override' not in r and r['type']!='Library']

def check_edits(edits,root=None):
    for edit in edits:
        item=find(edit['target'],True)
        if root and item.override_library.hierarchy_root!=root:raise Failure('UNSUPPORTED','Edit target is outside the selected native override hierarchy')
        for field,want in edit['values'].items():
            got=getattr(item,field)
            if isinstance(want,list):
                if len(got)!=len(want) or any(abs(a-b)>max(1e-6,abs(b)*2e-7) for a,b in zip(got,want)):raise Failure('VALIDATION_FAILED','Local override edit was not preserved: '+field)
            elif got!=want:raise Failure('VALIDATION_FAILED','Local override edit was not preserved: '+field)

def preserve_local(before):
    """Native remapping to new overrides is expected; other local edits are not."""
    mapped={refkey(r['reference']):r['identity'] for r in overrides()}
    def remap(value):
        if isinstance(value,list):return [remap(v) for v in value]
        if not isinstance(value,dict):return value
        if {'type','name','library'}<=set(value) and refkey(value) in mapped:
            return {**value,**{k:mapped[refkey(value)][k] for k in ('type','name','library')}}
        return {k:remap(v) for k,v in value.items()}
    after={refkey(r):r for r in state()}
    for row in before:
        if row['library'] or row['type']=='Library':continue
        if remap(row)!=after.get(refkey(row)):raise Failure('VALIDATION_FAILED','Override changed unrelated preexisting local content: '+row['name'])

def local_collections():
    return [s.collection for s in bpy.data.scenes if not s.library]+[c for c in bpy.data.collections if not c.library and not c.override_library]

def root_placements(original):
    collection=isinstance(original,bpy.types.Collection)
    containers=[c for c in local_collections() if original in list(c.children if collection else c.objects)]
    instances=[o for o in bpy.data.objects if not o.library and not o.override_library and o.instance_collection==original] if collection else []
    if not containers and not instances:raise Failure('UNSUPPORTED','Override root needs a direct local scene/collection placement or local instance; link this root first')
    return containers,instances

def replace_placements(original,root,placements):
    # hierarchy_create adds a scene placement but does not remove the linked
    # root. Preserve exactly the old placement multiplicity, avoiding doubles.
    collection=isinstance(original,bpy.types.Collection)
    containers,instances=placements
    for c in local_collections():
        members=c.children if collection else c.objects
        if root in list(members):members.unlink(root)
    for c in containers:
        members=c.children if collection else c.objects
        if original in list(members):members.unlink(original)
        if root not in list(members):members.link(root)
    for obj in instances:obj.instance_collection=root

def finalize(kind,params,job,resources,refs,root=None,edits=None,change=None):
    check_native()
    candidate=job/'project-candidate.blend'
    if candidate.exists():raise Failure('CONFLICT','Candidate already exists')
    # Absolute local references make exact-byte transaction relocation safe.
    bpy.ops.file.make_paths_absolute()
    for item in all_ids():
        if item.override_library:item.override_library.operations_update()
    expected=state();atomic_json(job/'project-expected.json',expected)
    bpy.ops.wm.save_as_mainfile(filepath=str(candidate),copy=True,relative_remap=False,check_existing=False)
    open_file(candidate);activate(params['manifest'].get('context') or params['_context'])
    observed=state();atomic_json(job/'project-observed.json',observed)
    if typed.fingerprint(expected)!=typed.fingerprint(observed):raise Failure('VALIDATION_FAILED','Candidate changed during save/reopen; state evidence retained')
    check_native()
    if edits:check_edits(edits)
    resolved=overrides()
    report={'version':'1.0','adapter':ADAPTER,'kind':kind,'candidate':str(candidate),'candidate_sha256':typed.digest(candidate),'resources':resources,'references':refs,'context':params['manifest'].get('context') or params['_context'],'source':{k:params[k] for k in ('file','expected_sha256') if k in params},'blender_build':bpy.app.build_hash.decode(),'blender_version':list(bpy.app.version),'implementation_sha256':typed.implementation(),'reopen':'pass','source_saved':False,'publication':'working_candidate_only','upstream_changes':change or {'added':[],'removed':[],'changed':[]},'overrides':resolved,'scope':'Blender 5.2.1 build; finite typed closure <=8 levels; one native same-library override hierarchy per operation; Object transform and hide_render edits; cross-library descendants retain their linked identity'}
    report['local_content']=local_content()
    if root:report.update(root=root,edits=edits)
    if kind=='override':
        receipt=job/'project-override-receipt.json';atomic_json(receipt,report)
        report={**report,'receipt':str(receipt),'receipt_sha256':typed.digest(receipt)}
    # A usable publication template carries every upstream SHA into the old
    # recoverable transaction engine. The caller chooses the destination.
    report['transaction_item']={'kind':'blend_exact','mode':'create','file':str(candidate),'dependencies':resources}
    atomic_json(job/('project-link-report.json' if kind=='link' else 'project-override-report.json'),report)
    return report

def link_prepare(params,job,stack,held):
    spec=params['manifest'];lib=spec['library'];lc=closure(lib,job,'library');lock_documents(stack,lc['documents'],held)
    resources={pathkey(d['file']):d for d in lc['documents']};refs=upstream_inventory(lc)
    open_file(lib['file']);selected=[]
    for selector in spec['selections']:
        found=[i for i in all_ids() if not i.library and i.bl_rna.identifier==selector['type'] and i.name==selector['name']]
        if len(found)!=1 or ('asset_id' in selector and found[0].get('asset_id')!=selector['asset_id']):raise Failure('CONFLICT','Source selection does not resolve uniquely: '+str(selector))
        selected.append(found[0])
    # Selecting a collection and one of its objects/children is ambiguous.
    domains=[]
    for item in selected:
        def domain(c):
            result={i.as_pointer() for i in c.all_objects}|{c.as_pointer()}
            for child in c.children:result|=domain(child)
            return result
        domains.append(domain(item) if isinstance(item,bpy.types.Collection) else {item.as_pointer()})
    if any(a&b for n,a in enumerate(domains) for b in domains[n+1:]):raise Failure('CONFLICT','Overlapping selections must be submitted as one root')
    if params.get('file'):
        bc=closure({k:params[k] for k in ('file','expected_sha256')},job,'source');lock_documents(stack,bc['documents'],held)
        resources.update({pathkey(d['file']):d for d in bc['documents'] if pathkey(d['file'])!=pathkey(params['file'])})
        open_file(params['file']);scene,layer=activate(spec['context'])
    else:
        bpy.ops.wm.read_factory_settings(use_empty=True)
        scene=bpy.context.scene;scene.name=spec['context']['scene'];scene.view_layers[0].name=spec['context']['view_layer'];scene,layer=activate(spec['context'])
    before=state();atomic_json(job/'project-before.json',before);old_ids={i.as_pointer() for i in all_ids()}
    for selector in spec['selections']:
        if any(i.bl_rna.identifier==selector['type'] and i.name==selector['name'] and (not i.library or pathkey(path_value(i.library.filepath))==pathkey(lib['file'])) for i in all_ids()):raise Failure('CONFLICT','Selection collides with an existing local or same-library data block')
    if spec.get('instance_name') and bpy.data.objects.get(spec['instance_name']):raise Failure('CONFLICT','Instance name already exists')
    with bpy.data.libraries.load(lib['file'],link=True,relative=False) as (available,target):
        target.objects=[s['name'] for s in spec['selections'] if s['type']=='Object']
        target.collections=[s['name'] for s in spec['selections'] if s['type']=='Collection']
    for selector in spec['selections']:
        item=find({**selector,'library':lib['file']})
        if spec['placement']=='INSTANCE':
            instance=bpy.data.objects.new(spec['instance_name'],None);instance.instance_type='COLLECTION';instance.instance_collection=item;scene.collection.objects.link(instance)
        elif isinstance(item,bpy.types.Collection):scene.collection.children.link(item)
        else:scene.collection.objects.link(item)
    layer.update()
    for i in all_ids():
        if i.as_pointer() not in old_ids and not isinstance(i,bpy.types.Library):
            if i.users==0:raise Failure('VALIDATION_FAILED','New orphan data block: '+str(ref(i)))
            if not i.library and i.name!=spec.get('instance_name'):raise Failure('VALIDATION_FAILED','Link unexpectedly localized data: '+str(ref(i)))
    after={refkey(r):r for r in state()}
    for row in before:
        observed=copy.deepcopy(after[refkey(row)]);expected=copy.deepcopy(row)
        if row['type']=='Scene':observed.pop('root_membership',None);expected.pop('root_membership',None)
        if observed!=expected:raise Failure('VALIDATION_FAILED','Link changed preexisting local data')
    return finalize('link',params,job,list(resources.values()),refs)

def override_prepare(params,job,stack,held):
    spec=params['manifest'];source={k:params[k] for k in ('file','expected_sha256')}
    report=closure(source,job,'source');lock_documents(stack,report['documents'],held)
    resources=[d for d in report['documents'] if pathkey(d['file'])!=pathkey(params['file'])]
    refs=upstream_inventory(report,params['file']);open_file(params['file']);scene,layer=activate(spec['context'])
    before=state();atomic_json(job/'project-before.json',before)
    original=find(spec['root'])
    if not original.library or original.override_library:raise Failure('CONFLICT','Override root must be an ordinary linked data block')
    if any(i.override_library for i in all_ids()):raise Failure('UNSUPPORTED','Create starts from a linked consumer without existing overrides; resync accepts an existing S05 receipt')
    placements=root_placements(original)
    # Pre-resolve each edit before any native mutation. Cross-library children
    # are explicitly outside this native root, and remain linked on disk.
    for edit in spec['edits']:
        target=find(edit['target'])
        if target.library!=original.library:raise Failure('UNSUPPORTED','A native hierarchy does not recursively override foreign libraries; select that library root in a separate consumer')
        if target.animation_data:raise Failure('UNSUPPORTED','Edits to animated transforms need a separate animation override adapter')
        for field in edit['values']:
            if not target.is_property_overridable_library(field):raise Failure('UNSUPPORTED','Property is not library-overridable: '+field)
    root=original.override_hierarchy_create(scene,layer,do_fully_editable=True)
    if not root or not root.override_library:raise Failure('VALIDATION_FAILED','Native override creation returned no hierarchy')
    replace_placements(original,root,placements)
    for edit in spec['edits']:
        target=find(edit['target'],True)
        if target.override_library.hierarchy_root!=root:raise Failure('CONFLICT','Edit reference is outside the created hierarchy')
        if 'rotation_euler' in edit['values'] and target.rotation_mode in ('QUATERNION','AXIS_ANGLE'):raise Failure('UNSUPPORTED','Euler edits require an Euler rotation mode')
        for field,value in edit['values'].items():setattr(target,field,value)
        target.override_library.operations_update()
    layer.update();check_edits(spec['edits'],root);preserve_local(before)
    old_keys={refkey(row) for row in before}
    orphaned=[ref(i) for i in all_ids() if refkey(ref(i)) not in old_keys and i.users==0 and not i.use_fake_user]
    if orphaned:raise Failure('VALIDATION_FAILED','Override created orphan IDs: '+str(orphaned))
    return finalize('override',params,job,resources,refs,spec['root'],spec['edits'])

def override_resync(params,job,stack,held):
    spec=params['manifest'];receipt=receipt_contract(read_json(spec['receipt']['file']))
    if receipt['candidate_sha256']!=params['expected_sha256']:raise Failure('CONFLICT','Receipt is not bound to these exact consumer bytes')
    if receipt['blender_build']!=bpy.app.build_hash.decode() or receipt['implementation_sha256']!=typed.implementation():raise Failure('CONFLICT','Override receipt implementation/build changed')
    known={pathkey(d['file']):d for d in receipt['resources']};expected=dict(known);expected.update({pathkey(d['file']):d for d in spec['updates']})
    lock_documents(stack,list(expected.values()),held)
    audit=closure({k:params[k] for k in ('file','expected_sha256')},job,'resync')
    observed={pathkey(d['file']):d for d in audit['documents'] if pathkey(d['file'])!=pathkey(params['file'])}
    if observed!=expected:raise Failure('CONFLICT','Current closure differs from explicit upstream updates; supply every new or changed file and no unrelated files')
    current_refs=upstream_inventory(audit,params['file']);delta=changes(receipt['references'],current_refs)
    atomic_json(job/'project-upstream-changes.json',delta)
    # Native file loading may auto-resync. Compare to the immutable receipt,
    # never mistake the already-updated in-memory graph for the old baseline.
    for selector in [receipt['root'],*[e['target'] for e in receipt['edits']]]:
        found=[r for r in current_refs if refkey(r['identity'])==refkey(selector) and ('asset_id' not in selector or r['identity'].get('asset_id')==selector['asset_id'])]
        if len(found)!=1:
            raise Failure('CONFLICT','An edited/root reference was renamed or removed upstream; explicit reauthoring is required: '+str(selector))
    open_file(params['file']);scene,layer=activate(receipt['context']);atomic_json(job/'project-before.json',state())
    if local_content()!=receipt['local_content']:raise Failure('CONFLICT','Automatic resync changed unrelated local content; inspect the retained baseline')
    old_ids={refkey(ref(i)) for i in all_ids()}
    root=find(receipt['root'],True)
    if not root.override_library.resync(scene,view_layer=layer,do_hierarchy_enforce=False):raise Failure('VALIDATION_FAILED','Native override resync reported failure')
    layer.update();check_edits(receipt['edits']);check_native()
    if local_content()!=receipt['local_content']:raise Failure('VALIDATION_FAILED','Resync changed unrelated local content')
    if any(refkey(ref(i)) not in old_ids and i.users==0 and not i.use_fake_user for i in all_ids()):raise Failure('VALIDATION_FAILED','Resync created orphan IDs')
    # No unrelated override operations may silently disappear on resync.
    old_refs={refkey(r['reference']) for r in receipt['overrides']}
    new_refs={refkey(r['reference']) for r in overrides()}
    removed={refkey(r['identity']) for r in delta['removed']}
    if old_refs-new_refs-removed:raise Failure('VALIDATION_FAILED','Resync lost an override that still exists upstream')
    return finalize('override',{**params,'_context':receipt['context']},job,list(observed.values()),current_refs,receipt['root'],receipt['edits'],delta)

def run(command,params,job):
    if tuple(bpy.app.version)!=(5,2,1):raise Failure('UNSUPPORTED','S05 native adapter requires Blender 5.2.1; other versions are unverified')
    try:
        with ExitStack() as stack:
            held={}
            if params.get('file'):lock_documents(stack,[{k:params[k] for k in ('file','expected_sha256')}],held)
            result=dict(zip(COMMANDS,(link_prepare,override_prepare,override_resync)))[command](params,job,stack,held)
            for d in result['resources']:
                if held[pathkey(d['file'])].sha256()!=d['expected_sha256']:raise Failure('CONFLICT','Upstream source changed during candidate preparation')
            return result
    except Failure as exc:
        atomic_json(job/'project-override-conflicts.json',{'version':'1.0','command':command,'error':{'code':exc.code,'message':str(exc)},'candidate_accepted':False,'source_saved':False,'upstream_changes':read_json(job/'project-upstream-changes.json') if (job/'project-upstream-changes.json').exists() else None})
        raise
