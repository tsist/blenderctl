"""Closed, no-bpy host contract for portable Toon template operations.
SPDX-License-Identifier: GPL-3.0-or-later
"""
from copy import deepcopy
from pathlib import Path
import sys
from toon_identity import ROOT, implementation_sha256, runtime_documents
sys.path.insert(0,str(ROOT/'tools'))
from toon_workflow_addon import contract, templates
from protocol import Failure

COMMANDS=('toon.template-save','toon.template-edit','toon.template-apply')
obj=contract._object
NAME={'type':'string','minLength':1,'maxLength':128,'pattern':r'^[^\u0000-\u001f\u007f-\u009f]+$'}
TAG={**NAME,'maxLength':64}
TAGS={'type':'array','minItems':0,'maxItems':16,'uniqueItems':True,'items':TAG}
NATIVE_SETTINGS=deepcopy(contract.SHADING_SCHEMA)
NATIVE_SETTINGS['properties']['mode']={'const':'native_cel'}
SETTINGS={'oneOf':[NATIVE_SETTINGS,deepcopy(contract.EEVEE_CEL3_SHADING_SCHEMA),deepcopy(contract.EEVEE_CEL3_REFLECT_SHADING_SCHEMA)]}
ENTRY=obj({'template_id':contract.UUID_SCHEMA,'name':NAME,'tags':TAGS,
           'engine':{'enum':['BLENDER_EEVEE','CYCLES']},'settings':SETTINGS})
DOCUMENT_SCHEMA=obj({'schema_version':{'const':'toon-template-library-v1'},'library_id':contract.UUID_SCHEMA,
    'revision':{'type':'integer','minimum':1},'name':NAME,
    'templates':{'type':'array','minItems':1,'maxItems':64,'items':ENTRY}})
SELECTION=obj({'material_id':contract.UUID_SCHEMA,'template_id':contract.UUID_SCHEMA,'name':NAME,'tags':TAGS})
EDIT=obj({'template_id':contract.UUID_SCHEMA,'settings':SETTINGS})
BINDING=obj({'library_template_id':contract.UUID_SCHEMA,'target_template_id':contract.UUID_SCHEMA})
SAVE_SCHEMA=obj({'file':contract.PATH_SCHEMA,'expected_sha256':contract.SHA_SCHEMA,
    'resources':{'type':'array','maxItems':100,'items':contract.FILE_SCHEMA},
    'implementation_sha256':contract.SHA_SCHEMA,'state_source':{'const':contract.STATE_SOURCE},
    'scope_id':contract.UUID_SCHEMA,'library_id':contract.UUID_SCHEMA,'name':NAME,
    'revision':{'type':'integer','minimum':1},
    'selections':{'type':'array','minItems':1,'maxItems':64,'items':SELECTION}})
EDIT_SCHEMA=obj({'template':contract.FILE_SCHEMA,'implementation_sha256':contract.SHA_SCHEMA,
    'expected_revision':{'type':'integer','minimum':1},
    'edits':{'type':'array','minItems':1,'maxItems':64,'items':EDIT}})
APPLY_SCHEMA=obj({'template':contract.FILE_SCHEMA,'request':contract.FILE_SCHEMA,
    'implementation_sha256':contract.SHA_SCHEMA,
    'bindings':{'type':'array','minItems':1,'maxItems':16,'items':BINDING}})
SCHEMAS=dict(zip(COMMANDS,(SAVE_SCHEMA,EDIT_SCHEMA,APPLY_SCHEMA)))

def normalize(command,value):
    if command not in SCHEMAS:raise Failure('UNSUPPORTED','Unknown Toon template operation')
    try:
        if command=='toon.template-save' and isinstance(value,dict):
            if 'name' in value:templates._name(value['name'],'$.params.name',128)
            if isinstance(value.get('selections'),list):
                for i,row in enumerate(value['selections']):
                    if not isinstance(row,dict):continue
                    if 'name' in row:templates._name(row['name'],'$.params.selections[%d].name'%i,128)
                    if 'tags' in row:templates._tags(row['tags'],'$.params.selections[%d].tags'%i)
        contract.validate(value,SCHEMAS[command],'$.params');result=deepcopy(value)
        if result['implementation_sha256']!=implementation_sha256():
            raise Failure('CONFLICT','Toon implementation changed; preserve the old request pin')
        paths=[]
        if command=='toon.template-save':
            result['file']=str(Path(result['file']).resolve())
            if Path(result['file']).suffix.lower()!='.blend':raise Failure('INVALID_REQUEST','Saved .blend source required')
            paths.append(result['file'])
            for row in result['resources']:row['file']=str(Path(row['file']).resolve());paths.append(row['file'])
            if len({x['material_id'] for x in result['selections']})!=len(result['selections']):
                raise Failure('INVALID_REQUEST','Duplicate material selector')
            ids=[result['library_id']]+[x['template_id'] for x in result['selections']]
            if len(set(ids))!=len(ids):raise Failure('INVALID_REQUEST','Library/template UUID collision')
        else:
            for key in ('template','request'):
                if key not in result:continue
                row=result[key];row['file']=str(Path(row['file']).resolve());paths.append(row['file'])
        if len(set(x.casefold() for x in paths))!=len(paths):raise Failure('INVALID_REQUEST','Duplicate template input paths')
        return result
    except contract.WorkflowError as exc:
        raise Failure(exc.code if exc.code in ('INVALID_REQUEST','CONFLICT','UNSUPPORTED') else 'INVALID_REQUEST',exc.path+': '+str(exc)) from exc

def anchors(params,command):
    return [params[k] for k in ('template','request') if k in params]

def input_documents(params,command):
    documents=runtime_documents()+list(params.get('resources',[]))+anchors(params,command)
    if 'file' in params:documents.append({'file':params['file'],'expected_sha256':params['expected_sha256']})
    result={}
    for row in documents:
        key=str(Path(row['file']).resolve()).casefold()
        if key in result and result[key]['expected_sha256']!=row['expected_sha256']:raise Failure('CONFLICT','Conflicting template resource SHA')
        result[key]=deepcopy(row)
    return list(result.values())

def describe(operation=None,schema=False):
    chosen=COMMANDS if operation is None else ('toon.'+operation,)
    if operation=='template':
        return {'contract_version':'1.0','operations':[],'template_document_schema':deepcopy(DOCUMENT_SCHEMA),
                'limits':['Portable static parameter content only; no scene bindings, executable code or arbitrary wrapper graph.']}
    if any(name not in SCHEMAS for name in chosen):raise Failure('UNSUPPORTED','Unknown Toon template describe operation')
    return {'contract_version':'1.0','operations':[{'command':name,'execution':'standalone_blender_worker',
        'required_params':list(SCHEMAS[name]['required']),**({'params_schema':deepcopy(SCHEMAS[name])} if schema else {})} for name in chosen],
        'template_document_version':'toon-template-library-v1','bpy_imported':False,
        'limits':['template-apply produces a checked run request, not an applied Blender candidate.','Source/state pins never migrate implicitly.']}
