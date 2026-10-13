"""Host-safe lighting inputs, template anchors and dependency expansion."""
import copy
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from protocol import Failure,read_json,digest
from lighting_identity import implementation_sha256,runtime_documents
from lighting_workflow_addon import contract as c
TEMPLATE_SAVE_SCHEMA=c.obj({'file':c.NAME,'expected_sha256':c.SHA,'resources':c.arr(c.FILE,0,256),'scene':c.NAME,'rig_id':c.ID,'template_id':c.ID,'revision':{'type':'integer','minimum':1,'maximum':1000000}},('file','expected_sha256','resources','scene','rig_id','template_id','revision'))

def _call(fn,*args,**kwargs):
    try:return fn(*args,**kwargs)
    except c.WorkflowError as e:raise Failure(e.code,str(e)) from e

def _descriptor(value):
    _call(c._validate,value,c.FILE)
    out=copy.deepcopy(value);out['file']=_call(c._absolute,out['file']);return out

def _identity(value):
    if not isinstance(value,dict):raise Failure('INVALID_REQUEST','params must be object')
    out=copy.deepcopy(value);supplied=out.pop('implementation_sha256',None);current=implementation_sha256()
    if supplied is not None and supplied!=current:raise Failure('CONFLICT','Lighting implementation changed')
    return out,current

def _template(desc):
    desc=_descriptor(desc)
    from lighting_workflow_addon.templates import normalize_template
    return _call(normalize_template,_read_descriptor(desc))

def _read_descriptor(desc):
    """Enumeration reads use the same deny-write handle as their SHA check."""
    from filesystem import FileGuard
    with FileGuard(desc['file']) as guard:
        if guard.sha256()!=desc['expected_sha256']:raise Failure('CONFLICT','Lighting descriptor SHA mismatch')
        return read_json(desc['file'])

def _merge_resources(*groups):
    merged={}
    for rows in groups:
        for row in rows:
            row=_descriptor(row);key=row['file'].casefold()
            if key in merged and merged[key]['expected_sha256']!=row['expected_sha256']:raise Failure('CONFLICT','Conflicting resource hashes: '+row['file'])
            merged[key]=row
    return list(merged.values())

def normalize_params(value):
    v,current=_identity(value);resume=v.pop('resume',None)
    if 'template' in v:
        if 'manifest' in v:raise Failure('INVALID_REQUEST','Choose manifest or template, not both')
        allowed={'file','expected_sha256','resources','template','target','preview'}
        if set(v)-allowed or not {'file','expected_sha256','resources','template','target'}<=set(v):raise Failure('INVALID_REQUEST','Template run fields are invalid')
        from lighting_workflow_addon.templates import normalize_target
        v['template']=_descriptor(v['template']);body=_template(v['template']);v['target']=_call(normalize_target,v['target'])
        v['resources']=_merge_resources(v['resources'],body['resources'])
        base={k:v[k] for k in ('file','expected_sha256','resources')};base['scene']=v['target']['scene'];base=_call(c.normalize_inspect_params,base)
        v.update({k:base[k] for k in ('file','expected_sha256','resources')})
        if 'preview' in v:v['preview']=_call(c.normalize_preview,v['preview'])
    else:
        if 'target' in v:raise Failure('INVALID_REQUEST','target requires template')
        v=_call(c.normalize_params,v)
    v['implementation_sha256']=current
    if resume is not None:v['resume']=_descriptor(resume)
    return v

def normalize_inspect_params(value):
    v,current=_identity(value);v=_call(c.normalize_inspect_params,v);v['implementation_sha256']=current;return v

def normalize_template_params(value):
    v,current=_identity(value);v=_call(c._inputs,v,TEMPLATE_SAVE_SCHEMA);v['implementation_sha256']=current;return v

def normalize_edit_params(value):
    v,current=_identity(value)
    if set(v)!={'template','revision','edits'} or not isinstance(v.get('edits'),dict) or set(v['edits'])!={'content'}:raise Failure('INVALID_REQUEST','Edit requires template, revision and edits.content')
    v['template']=_descriptor(v['template']);old=_template(v['template']);revision=v['revision']
    if isinstance(revision,bool) or not isinstance(revision,int) or not old['revision']<revision<=1000000:raise Failure('INVALID_REQUEST','Template revision must strictly increase')
    from lighting_workflow_addon.templates import normalize_template
    body=copy.deepcopy(v['edits']['content'])
    if not isinstance(body,dict) or body.get('template_id')!=old['template_id']:raise Failure('INVALID_REQUEST','Edit must retain template_id')
    body['revision']=revision;body['parent_sha256']=v['template']['expected_sha256'];v['edits']={'content':_call(normalize_template,body)};v['implementation_sha256']=current;return v

def normalize_batch_params(value):
    v,current=_identity(value)
    if set(v)-{'version','items','resume'} or v.get('version')!='1.0' or not isinstance(v.get('items'),list) or not 1<=len(v['items'])<=8:raise Failure('INVALID_REQUEST','Batch requires version 1.0 and 1..8 items')
    seen=set();total=0
    for row in v['items']:
        if not isinstance(row,dict) or set(row)!={'id','params'}:raise Failure('INVALID_REQUEST','Batch item requires id and params')
        _call(c._validate,row['id'],c.ID)
        if row['id'] in seen:raise Failure('INVALID_REQUEST','Duplicate batch item id')
        seen.add(row['id']);row['params']=normalize_params(row['params'])
        if 'resume' in row['params']:raise Failure('INVALID_REQUEST','Use batch resume, not item resume')
        p=row['params'].get('preview',{});total+=len(p.get('engines',[]))*len(p.get('frames',[1]))*len(p.get('cameras',['active']))
    if total>24:raise Failure('RESOURCE_LIMIT','Batch exceeds 24 preview outputs')
    if 'resume' in v:v['resume']=_descriptor(v['resume'])
    v['implementation_sha256']=current;return v

def normalize_command(command,params):
    routes={'lighting.run':normalize_params,'lighting.inspect':normalize_inspect_params,'lighting.template-save':normalize_template_params,'lighting.template-edit':normalize_edit_params,'lighting.batch':normalize_batch_params}
    if command not in routes:raise Failure('INVALID_REQUEST','Unknown lighting command')
    return routes[command](params)

def anchor_documents(params,command=None):
    docs=[]
    for key in ('template','resume'):
        if key in params:docs.append(_descriptor(params[key]))
    for row in params.get('items',[]):docs.extend(anchor_documents(row['params'],'lighting.run'))
    return docs

def pending_report_descriptor(parent,row):
    """Recover only the owned child path already declared before its worker ran."""
    _call(c._validate,row.get('id'),c.ID)
    root=Path(parent['file']).resolve().parent
    expected=root/'items'/row['id']/'lighting-report.json'
    supplied=row.get('pending_report_path')
    if not isinstance(supplied,str) or not Path(supplied).is_absolute() or str(Path(supplied).resolve()).casefold()!=str(expected.resolve()).casefold():
        raise Failure('CONFLICT','Pending child report differs from its derived owning path')
    if not expected.is_file():
        raise Failure('CONFLICT','Pending child report was never materialized; start a new batch from source')
    from filesystem import FileGuard
    with FileGuard(expected) as guard:
        sha=guard.sha256();value=read_json(expected)
    if not isinstance(value,dict) or value.get('lighting_report_version')!='1.0' or 'items' in value:
        raise Failure('INVALID_REQUEST','Pending report is not a single lighting checkpoint')
    return {'file':str(expected),'expected_sha256':sha}

def _resume_docs(report,*,item=False,parent=None):
    if not isinstance(report,dict):raise Failure('INVALID_REQUEST','Resume report must be object')
    if item and 'items' in report:raise Failure('INVALID_REQUEST','Batch item report must be a single lighting report')
    if not isinstance(report.get('previews',[]),list) or len(report.get('previews',[]))>24:
        raise Failure('INVALID_REQUEST','Resume preview records exceed bounds')
    if 'items' in report and (not isinstance(report['items'],list) or len(report['items'])>8):
        raise Failure('INVALID_REQUEST','Resume item records exceed bounds')
    docs=[]
    if report.get('candidate'):
        r=report['candidate'];docs.append({'file':r['file'],'expected_sha256':r['sha256']})
    for r in report.get('previews',[]):docs.append({'file':r['file'],'expected_sha256':r['sha256']})
    for row in report.get('items',[]):
        if row.get('report'):
            desc=row['report'];docs.append({'file':desc['file'],'expected_sha256':desc['sha256']})
            docs.extend(_resume_docs(_read_descriptor({'file':desc['file'],'expected_sha256':desc['sha256']}),item=True))
        elif row.get('pending_report_path'):
            if parent is None:raise Failure('INVALID_REQUEST','Pending child report lacks its parent anchor')
            desc=pending_report_descriptor(parent,row);docs.append(desc)
            docs.extend(_resume_docs(_read_descriptor(desc),item=True))
    return docs

def input_documents(params,command=None):
    docs=runtime_documents()+list(params.get('resources',[]))
    if 'file' in params:docs.append({'file':params['file'],'expected_sha256':params['expected_sha256']})
    if params.get('template'):docs.append(params['template']);docs.extend(_template(params['template'])['resources'])
    if params.get('edits'):docs.extend(params['edits']['content']['resources'])
    for row in params.get('items',[]):docs.extend(input_documents(row['params'],'lighting.run'))
    if params.get('resume'):
        desc=params['resume']
        docs.append(desc);old=_read_descriptor(desc)
        if 'items' not in old and not old.get('candidate'):raise Failure('CONFLICT','Resume report has no saved candidate')
        docs.extend(_resume_docs(old,parent=desc))
    return _merge_resources(docs)

def schemas():
    from lighting_workflow_addon.templates import TEMPLATE_SCHEMA,TARGET_SCHEMA
    common={'file':c.NAME,'expected_sha256':c.SHA,'resources':c.arr(c.FILE,0,256),
        'preview':c.PREVIEW_SCHEMA,'implementation_sha256':c.SHA,'resume':c.FILE}
    run=c.obj({**common,'manifest':c.MANIFEST_SCHEMA,'template':c.FILE,'target':TARGET_SCHEMA},('file','expected_sha256','resources'))
    run['oneOf']=[{'required':['manifest'],'not':{'anyOf':[{'required':['template']},{'required':['target']}]}},
                  {'required':['template','target'],'not':{'required':['manifest']}}]
    inspect=copy.deepcopy(c.INSPECT_PARAMS_SCHEMA);inspect['properties']['implementation_sha256']=c.SHA
    save=copy.deepcopy(TEMPLATE_SAVE_SCHEMA);save['properties']['implementation_sha256']=c.SHA
    edit=c.obj({'template':c.FILE,'revision':{'type':'integer','minimum':2,'maximum':1000000},
        'edits':c.obj({'content':TEMPLATE_SCHEMA},('content',)),'implementation_sha256':c.SHA},('template','revision','edits'))
    batch=c.obj({'version':{'const':'1.0'},'items':c.arr(c.obj({'id':c.ID,'params':run},('id','params')),1,8),
        'resume':c.FILE,'implementation_sha256':c.SHA},('version','items'))
    return {'run':run,'inspect':inspect,'manifest':c.MANIFEST_SCHEMA,'template':TEMPLATE_SCHEMA,
        'target':TARGET_SCHEMA,'template-save':save,'template-edit':edit,'batch':batch}
