"""Host binding for the single pure compositing contract and immutable inputs."""
import copy,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from protocol import Failure,read_json
from compositing_identity import implementation_sha256,runtime_documents

def _call(fn,*args,**kwargs):
    from compositing_workflow_addon.contract import WorkflowError
    try:return fn(*args,**kwargs)
    except WorkflowError as e:
        failure=Failure(e.code,str(e));failure.details=e.details
        raise failure from e

def _descriptor(value):
    if not isinstance(value,dict) or set(value)!={'file','expected_sha256'}:
        raise Failure('INVALID_REQUEST','Expected file/expected_sha256 descriptor')
    import re
    if not isinstance(value['file'],str) or not Path(value['file']).is_absolute() or not re.fullmatch('[0-9a-f]{64}',str(value['expected_sha256'])):
        raise Failure('INVALID_REQUEST','Descriptor needs absolute path and lower-case SHA256')
    return {**value,'file':str(Path(value['file']).resolve())}

def _identity(value):
    if not isinstance(value,dict):raise Failure('INVALID_REQUEST','Compositing params must be object')
    out=copy.deepcopy(value);supplied=out.pop('implementation_sha256',None);current=implementation_sha256()
    if supplied is not None and supplied!=current:raise Failure('CONFLICT','Compositing implementation changed')
    return out,current

def _read_descriptor(desc):
    from filesystem import FileGuard
    with FileGuard(desc['file']) as guard:
        if guard.sha256()!=desc['expected_sha256']:raise Failure('CONFLICT','Compositing descriptor changed')
        return read_json(desc['file'])

def media_sources(manifest):
    source=manifest.get('source',{})
    if source.get('kind') in ('image','image_sequence','movie'):yield source
    for node in manifest.get('graph',{}).get('nodes',[]):
        binding=node.get('binding',{})
        if binding.get('kind') in ('image','image_sequence','movie'):yield binding

def image_sources(manifest):
    # Historical callers specifically request static Image resources.
    for source in media_sources(manifest):
        if source['kind']=='image':yield source

def media_documents(manifest):
    for source in media_sources(manifest):
        if source['kind']=='image_sequence':
            for row in source['frames']:
                yield _descriptor({'file':row['file'],'expected_sha256':row['expected_sha256']})
        else:
            yield _descriptor({'file':source['file'],'expected_sha256':source['expected_sha256']})
        if source['kind']=='movie':
            for row in source['decoder_runtime'].values():yield _descriptor(row)
    for row in manifest.get('tracking_imports',[]):
        yield _descriptor({'file':row['file'],'expected_sha256':row['expected_sha256']})

def normalize_params(value):
    from compositing_workflow_addon import contract as c
    out,current=_identity(value);resume=out.pop('resume',None)
    out=_call(c.normalize_run_params,out)
    out['file']=str(Path(out['file']).resolve())
    out['resources']=[_descriptor(r) for r in out['resources']]
    for source in media_sources(out['manifest']):
        members=source['frames'] if source['kind']=='image_sequence' else [source]
        for row in members:
            row['file']=_descriptor({'file':row['file'],'expected_sha256':row['expected_sha256']})['file']
        if source['kind']=='movie':
            source['decoder_runtime']={key:_descriptor(row) for key,row in source['decoder_runtime'].items()}
    for row in out['manifest'].get('tracking_imports',[]):
        row['file']=_descriptor({'file':row['file'],'expected_sha256':row['expected_sha256']})['file']
    video=out.get('preview',{}).get('video',{})
    for key,row in video.get('runtime',{}).items():
        video['runtime'][key]=_descriptor(row)
    out['implementation_sha256']=current
    if resume is not None:out['resume']=_descriptor(resume)
    return out

def normalize_inspect_params(value):
    from compositing_workflow_addon import contract as c
    out,current=_identity(value);out=_call(c.normalize_inspect_params,out)
    out['file']=str(Path(out['file']).resolve());out['resources']=[_descriptor(r) for r in out['resources']]
    out['implementation_sha256']=current
    return out

def normalize_command(command,value):
    if command=='compositing.run':return normalize_params(value)
    if command=='compositing.inspect':return normalize_inspect_params(value)
    if command=='compositing.template-run':
        from compositing_template_run_contract import normalize_host_params
        return normalize_host_params(value)
    if command in ('compositing.template-save','compositing.batch','compositing.study'):
        from compositing_batch_contract import normalize_command as normalize_m5
        return normalize_m5(command,value)
    raise Failure('INVALID_REQUEST','Unknown compositing command')

def anchor_documents(params,command=None):
    if command=='compositing.template-run':
        from compositing_template_run_contract import anchor_documents as template_anchors
        return template_anchors(params,command)
    if command in ('compositing.template-save','compositing.batch','compositing.study'):
        return [params[name] for name in ('producer','resume','template') if name in params]
    return [params['resume']] if 'resume' in params else []

def input_documents(params,command=None):
    if command=='compositing.template-run':
        from compositing_template_run_contract import input_documents as template_inputs
        return template_inputs(params,command)
    if command in ('compositing.template-save','compositing.batch','compositing.study'):
        from compositing_batch_contract import input_documents as parent_inputs
        return parent_inputs(params,command)
    documents=list(params.get('resources',[]))+runtime_documents()
    documents.extend(media_documents(params.get('manifest',{})))
    documents.extend(_descriptor(row) for row in params.get('preview',{}).get('video',{}).get('runtime',{}).values())
    if 'resume' in params:
        old=_read_descriptor(params['resume'])
        if not isinstance(old,dict) or not old.get('candidate') or not old.get('saved_expectation'):
            raise Failure('CONFLICT','Resume lacks saved candidate and verification expectation')
        row=old['candidate'];documents.append(_descriptor({'file':row['file'],'expected_sha256':row.get('sha256',row.get('expected_sha256'))}))
        for row in old.get('outputs',[])+old.get('baselines',[]):
            if 'file' in row and ('sha256' in row or 'expected_sha256' in row):
                documents.append(_descriptor({'file':row['file'],'expected_sha256':row.get('sha256',row.get('expected_sha256'))}))
        # Immutable staged ancestors are candidate dependencies, not temporary
        # files. Resume keeps their original paths and exact candidate bytes.
        for row in old.get('staged_resources',[]):
            documents.append(_descriptor({'file':row['file'],'expected_sha256':row.get('sha256',row.get('expected_sha256'))}))
        from contextlib import ExitStack
        from compositing_media import guarded_movie_documents
        from compositing_movie import validate_resolved_movie
        from compositing_workflow_addon.contract import media_sources as keyed_media_sources
        from compositing_movie import value_sha
        declared_media=dict(keyed_media_sources(params['manifest']))
        for binding_key,entry in (old.get('media_receipt') or {}).get('resolved_media',{}).items():
            if entry.get('kind')!='decoded_movie':continue
            source=declared_media.get(binding_key)
            if (not source or source['kind']!='movie' or source['file']!=entry['original_movie']['file'] or
                source['expected_sha256']!=entry['original_movie']['sha256'] or value_sha(source)!=entry['declaration_sha256']):
                raise Failure('CONFLICT','Resume Movie binding key/declaration differs from its original receipt')
            with ExitStack() as stack:
                checked={}
                closure=guarded_movie_documents(entry,stack,checked)
                validate_resolved_movie(source,entry)
                documents.extend(_descriptor({'file':r['file'],'expected_sha256':r.get('sha256',r.get('expected_sha256'))}) for r in closure)
    merged={}
    for row in documents:
        row=_descriptor(row);key=row['file'].casefold()
        if key in merged and merged[key]['expected_sha256']!=row['expected_sha256']:
            raise Failure('CONFLICT','Conflicting declared input SHA: '+row['file'])
        merged[key]=row
    return list(merged.values())

def schemas():
    from compositing_workflow_addon import contract as c
    from compositing_batch_contract import schemas as parent_schemas
    from compositing_template_run_contract import schemas as template_schemas
    return {'run':c.RUN_SCHEMA,'inspect':c.INSPECT_SCHEMA,'manifest':c.MANIFEST_SCHEMA,'preview':c.PREVIEW_SCHEMA,**parent_schemas(),**template_schemas()}
