"""M5 prototype: pure bounded orchestration contract; no member files read here."""
import copy,math,re
from pathlib import Path
from protocol import Failure
from compositing_identity import implementation_sha256,runtime_documents
from compositing_workflow_contract import normalize_params as normalize_single
from compositing_workflow_addon import contract as c

MAX_ITEMS=16;MAX_CASES=8
RESERVED={'con','prn','aux','nul',*(f'com{i}' for i in range(1,10)),*(f'lpt{i}' for i in range(1,10))}
ID=c.ID;FILE=c.FILE;FRAME=c.LEGACY_MANIFEST_SCHEMA['properties']['frame']
ITEM_SCHEMA=c.obj({'id':ID,'params':c.RUN_SCHEMA,'template':FILE},('id','params'))
BATCH_MANIFEST_SCHEMA=c.obj({'batch_version':{'const':'1.0'},'items':{'type':'array','minItems':1,'maxItems':MAX_ITEMS,'items':ITEM_SCHEMA}},('batch_version','items'))
BATCH_PARAMS_SCHEMA=c.obj({'manifest':BATCH_MANIFEST_SCHEMA,'common_inputs':{'type':'array','maxItems':1024,'items':FILE},'resume':FILE,'implementation_sha256':c.SHA},('manifest',))
OVERRIDE_SCHEMA=c.obj({'node':ID,'domain':{'enum':['inputs','properties','component']},'identifier':c.NAME,'value':{}},('node','domain','identifier','value'))
CASE_SCHEMA=c.obj({'id':ID,'overrides':{'type':'array','minItems':1,'maxItems':16,'items':OVERRIDE_SCHEMA}},('id','overrides'))
STUDY_PARAMS_SCHEMA=c.obj({'base':c.RUN_SCHEMA,'cases':{'type':'array','minItems':1,'maxItems':MAX_CASES,'items':CASE_SCHEMA},'template':FILE,'resume':FILE,'implementation_sha256':c.SHA},('base','cases'))
TEMPLATE_SAVE_SCHEMA=c.obj({'producer':FILE,'template_id':ID,'implementation_sha256':c.SHA},('producer','template_id'))
FORBIDDEN_PROPERTIES={'name','label','location','width','height','parent','node_tree','scene','image','layer','filepath','frame_offset','frame_start','frame_duration','use_auto_refresh','data_type','factor_mode'}

def fail(message,code='INVALID_REQUEST',details=None):
    error=Failure(code,message);error.details=details;raise error

def checked(value,schema):
    try:c.validate(value,schema);c._json(value)
    except c.WorkflowError as error:fail(str(error),error.code,error.details)
    return copy.deepcopy(value)

def descriptor(row):
    out=checked(row,FILE)
    if not Path(out['file']).is_absolute():fail('Descriptor path must be absolute')
    out['file']=str(Path(out['file']).resolve());return out

def safe_ids(ids):
    keys=[id.casefold() for id in ids]
    if len(set(keys))!=len(keys) or set(keys)&RESERVED:fail('IDs must be casefold unique and not Windows reserved')

def pinned_identity(out):
    current=implementation_sha256()
    if out.get('implementation_sha256',current)!=current:fail('Implementation SHA changed','CONFLICT')
    out['implementation_sha256']=current;return out

def normalize_params(value):
    out=pinned_identity(checked(value,BATCH_PARAMS_SCHEMA));safe_ids([i['id'] for i in out['manifest']['items']])
    for item in out['manifest']['items']:
        item['params']=normalize_single(item['params'])
        if 'template' in item:item['template']=descriptor(item['template'])
    out['common_inputs']=[descriptor(r) for r in out.get('common_inputs',[])]
    if 'resume' in out:out['resume']=descriptor(out['resume'])
    return out

def input_documents(params,command=None):
    """Parent locks ONLY common implementation/explicit shared/resume anchors."""
    rows=runtime_documents()+list(params.get('common_inputs',[]))
    if 'resume' in params:rows.append(params['resume'])
    if command=='compositing.template-save' and 'producer' in params:rows.append(params['producer'])
    if command=='compositing.study' and 'template' in params:rows.append(params['template'])
    merged={}
    for row in rows:
        row=descriptor(row);key=row['file'].casefold()
        if key in merged and merged[key]['expected_sha256']!=row['expected_sha256']:fail('Conflicting parent input SHA','CONFLICT')
        merged[key]=row
    return list(merged.values())

def static_study(base):
    manifest=base['manifest'];preview=base.get('preview',{})
    if manifest['operation']!='create':fail('Study requires a full create recipe')
    if manifest.get('animation') or manifest.get('animation_patches') or manifest.get('tracking_imports'):fail('Study is static: animation/tracking replacement is not supported','UNSUPPORTED')
    if 'timeline' in preview or preview.get('video',{}).get('enabled') or len(preview.get('frames',[manifest.get('frame',1)]))!=1:fail('Study requires one static preview frame','UNSUPPORTED')
    media=[manifest.get('source',{})]+[n.get('binding',{}) for n in manifest.get('graph',{}).get('nodes',[])]
    if any(m.get('kind') in ('image_sequence','movie') for m in media):fail('Study temporal inputs are not supported','UNSUPPORTED')

def same_type(before,after):
    if isinstance(before,bool):return isinstance(after,bool)
    if isinstance(before,(int,float)) and not isinstance(before,bool):return isinstance(after,(int,float)) and not isinstance(after,bool) and math.isfinite(after)
    if isinstance(before,str):return isinstance(after,str)
    if isinstance(before,list):return isinstance(after,list) and len(before)==len(after) and all(same_type(a,b) for a,b in zip(before,after))
    return False

def apply_overrides(base,overrides):
    out=copy.deepcopy(base);manifest=out['manifest'];changes=[];seen=set()
    for override in overrides:
        override=checked(override,OVERRIDE_SCHEMA);node=override['node'];domain=override['domain'];key=override['identifier'];value=override['value']
        target=(node,domain,key)
        if target in seen:fail('Duplicate study target')
        seen.add(target)
        if domain=='component':
            component=next((n for n in manifest.get('components',[]) if n['id']==node),None)
            if component is None or key not in component['params']:fail('Study component field must already be explicitly declared')
            schema=c.component_param_schema(component['kind'],manifest['workflow_version'])['properties'].get(key)
            if schema is None:fail('Unknown component parameter')
            checked(value,schema);container=component['params']
        else:
            graph_node=next((n for n in manifest.get('graph',{}).get('nodes',[]) if n['id']==node),None)
            if graph_node is None or key not in graph_node.get(domain,{}):fail('Study field must already be explicitly declared on an existing node')
            if domain=='properties' and key in FORBIDDEN_PROPERTIES:fail('Study structural/binding/UI property forbidden','UNSUPPORTED')
            if graph_node['type'] in ('CompositorNodeImage','CompositorNodeRLayers'):fail('Study source node interpretation/binding forbidden','UNSUPPORTED')
            container=graph_node[domain]
        before=container[key]
        if not same_type(before,value):fail('Study value type/dimension differs from explicit baseline')
        container[key]=copy.deepcopy(value);changes.append({'node':node,'domain':domain,'identifier':key,'before':copy.deepcopy(before),'after':copy.deepcopy(value)})
    return normalize_single(out),changes

def normalize_study_params(value):
    out=pinned_identity(checked(value,STUDY_PARAMS_SCHEMA));out['base']=normalize_single(out['base']);static_study(out['base']);safe_ids([r['id'] for r in out['cases']])
    if any(r['id'].casefold()=='baseline' for r in out['cases']):fail('baseline ID reserved for study reference')
    for case in out['cases']:apply_overrides(out['base'],case['overrides'])
    if 'resume' in out:out['resume']=descriptor(out['resume'])
    if 'template' in out:out['template']=descriptor(out['template'])
    return out

def expand_study(params):
    out=normalize_study_params(params);items=[{'id':'baseline','params':copy.deepcopy(out['base'])}];diffs={'baseline':[]}
    for case in out['cases']:
        member,changes=apply_overrides(out['base'],case['overrides']);items.append({'id':case['id'],'params':member});diffs[case['id']]=changes
    batch={'manifest':{'batch_version':'1.0','items':items},'implementation_sha256':out['implementation_sha256']}
    if 'template' in out:
        for item in items:item['template']=out['template']
    return batch,diffs

def normalize_template_params(value):
    out=pinned_identity(checked(value,TEMPLATE_SAVE_SCHEMA));out['producer']=descriptor(out['producer']);return out

def normalize_command(command,value):
    fn={'compositing.batch':normalize_params,'compositing.study':normalize_study_params,'compositing.template-save':normalize_template_params}.get(command)
    if fn is None:fail('Unknown M5 prototype operation')
    return fn(value)

def schemas():return {'batch':BATCH_PARAMS_SCHEMA,'study':STUDY_PARAMS_SCHEMA,'template-save':TEMPLATE_SAVE_SCHEMA}
