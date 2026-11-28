"""Public saved-file and live contracts for the integrated rigging product."""
from pathlib import Path
import copy
import hashlib
import importlib.util
import json
import re
import os
from protocol import Failure, ROOT, digest, envelope

LOCATION = ROOT/'runtime/rigging-workflow-product.json'
SOURCE = ROOT/'tools/addons/rigging_workflow'


def product_location():
    explicit=os.environ.get('RIGGING_WORKFLOW_PRODUCT')
    if explicit:
        path=Path(explicit)
        if not path.is_absolute():
            raise Failure('INVALID_REQUEST','RIGGING_WORKFLOW_PRODUCT must be absolute')
    else:
        if not LOCATION.is_file() or LOCATION.stat().st_size>16384:
            raise Failure('NOT_FOUND','Explicit rigging product locator is missing or too large')
        value=json.loads(LOCATION.read_text(encoding='utf-8-sig'))
        if not isinstance(value,dict) or value.get('schema')!='1.0' or not isinstance(value.get('product_root'),str):
            raise Failure('INVALID_REQUEST','Invalid rigging product locator')
        path=Path(value['product_root'])
        if not path.is_absolute():
            path=ROOT/path
    path=path.resolve()
    if not all((path/name).is_file() for name in ('__init__.py','contracts.py','core/service.py')):
        raise Failure('NOT_FOUND','Selected location is not a rigging product package')
    return path


if os.environ.get('RIGGING_WORKFLOW_PRODUCT'):
    PRODUCT = product_location()
else:
    try:
        PRODUCT = product_location()
    except Failure as error:
        if error.code != 'NOT_FOUND':
            raise
        PRODUCT = None


def runtime_documents():
    if PRODUCT is None:
        raise Failure('NOT_FOUND', 'The legacy disk/live plan service requires a valid RIGGING_WORKFLOW_PRODUCT')
    files=[p for p in sorted(PRODUCT.rglob('*.py'))
            if not any(x in {'sources','tests','debug','build-debug','__pycache__'} for x in p.relative_to(PRODUCT).parts)
            and not p.name.startswith(('debug_','test_'))]
    files += [Path(__file__),Path(__file__).with_name('rigging_workflow_disk.py'),Path(__file__).with_name('rigging_workflow_session.py')]
    if not os.environ.get('RIGGING_WORKFLOW_PRODUCT'):
        files.append(LOCATION)
    return [{'file':str(p),'expected_sha256':digest(p)} for p in files]


def implementation_sha256():
    return hashlib.sha256(json.dumps([(str(Path(r['file']).relative_to(PRODUCT)) if Path(r['file']).is_relative_to(PRODUCT) else 'host/'+Path(r['file']).name,r['expected_sha256'])
                       for r in runtime_documents()],separators=(',',':')).encode()).hexdigest()


def descriptor(value):
    if not isinstance(value,dict) or set(value)!={'file','expected_sha256'} or not isinstance(value['file'],str) or not Path(value['file']).is_absolute() or not re.fullmatch('[0-9a-f]{64}',str(value['expected_sha256'])):
        raise Failure('INVALID_REQUEST','Expected absolute file/SHA descriptor')
    return dict(value)


def _binding_path(value):
    if not isinstance(value,list) or not 1<=len(value)<=16 or any(
            not (isinstance(item,str) and 1<=len(item)<=256 or type(item) is int and 0<=item<=65535)
            for item in value):
        raise Failure('INVALID_REQUEST','Result binding paths require1..16 nonempty keys or nonnegative indices')
    return tuple(value)


def _path_value(root,path,label):
    value=root
    for part in path:
        if isinstance(value,dict) and isinstance(part,str) and part in value:
            value=value[part]
        elif isinstance(value,list) and type(part) is int and 0<=part<len(value):
            value=value[part]
        else:
            raise Failure('INVALID_REQUEST',label+' path does not exist')
    return value


def _binding_target(parameters,path):
    parent=_path_value(parameters,path[:-1],'Binding target parent')
    key=path[-1]
    # A binding supplies an omitted field; it must never override an explicit
    # literal or an earlier binding, including a containing subtree.
    if not isinstance(parent,dict) or not isinstance(key,str) or key in parent:
        raise Failure('INVALID_REQUEST','Result binding target must be a new dictionary field')
    return parent,key


def validate_result_bindings(step,previous_ids):
    bindings=step.get('result_bindings',[])
    if not isinstance(bindings,list) or len(bindings)>32:
        raise Failure('INVALID_REQUEST','A step accepts at most32 result bindings')
    targets=[]
    for binding in bindings:
        if not isinstance(binding,dict) or set(binding)!={'step','result_path','parameter_path'} or not isinstance(binding['step'],str) or binding['step'] not in previous_ids:
            raise Failure('INVALID_REQUEST','Result bindings require an earlier explicit step and two paths')
        _binding_path(binding['result_path'])
        target=_binding_path(binding['parameter_path'])
        if any(target[:len(other)]==other or other[:len(target)]==target for other in targets):
            raise Failure('INVALID_REQUEST','Result binding targets overlap')
        if step['operation'] in {'character.create_work_copy','character.diagnose'} and target[0] in {'source_objects','active_body'} or 'candidate_from_step' in step and target[0]=='candidate_id':
            raise Failure('INVALID_REQUEST','Result binding conflicts with a dedicated manifest binding')
        _binding_target(step.get('parameters',{}),target)
        targets.append(target)


def resolve_parameters(step,results):
    """Copy JSON result fields only; no expressions, live references or writes."""
    validate_result_bindings(step,set(results))
    parameters=copy.deepcopy(step.get('parameters',{}))
    for binding in step.get('result_bindings',[]):
        value=_path_value(results[binding['step']],binding['result_path'],'Binding result')
        parent,key=_binding_target(parameters,binding['parameter_path'])
        parent[key]=copy.deepcopy(value)
    try:
        size=len(json.dumps(parameters,ensure_ascii=False,allow_nan=False).encode('utf-8'))
    except (ValueError,TypeError) as exc:
        raise Failure('INVALID_REQUEST','Bound parameters require finite JSON data') from exc
    if size>262144:
        raise Failure('RESOURCE_LIMIT','Bound parameters exceed256KiB')
    return parameters


def normalize_params(value):
    allowed={'file','expected_sha256','resources','implementation_sha256','manifest'}
    if not isinstance(value,dict) or set(value)-allowed or not {'file','expected_sha256','resources','manifest'}<=set(value):
        raise Failure('INVALID_REQUEST','Rigging disk run requires file/SHA/resources/manifest')
    descriptor({k:value[k] for k in ('file','expected_sha256')})
    resources=value['resources']
    if not isinstance(resources,list) or len(resources)>512:
        raise Failure('INVALID_REQUEST','resources must be an explicit list of at most512 descriptors')
    resources=[descriptor(r) for r in resources]
    manifest=value['manifest']
    if not isinstance(manifest,dict) or set(manifest)-{'schema','scene','view_layer','character_id','base_revision','source_objects','active_body','steps'} or manifest.get('schema')!='1.0':
        raise Failure('INVALID_REQUEST','Unknown disk manifest fields/schema')
    for key in ('scene','view_layer'):
        if not isinstance(manifest.get(key),str) or not 1<=len(manifest[key])<=256:
            raise Failure('INVALID_REQUEST','Explicit scene/view_layer required')
    steps=manifest.get('steps')
    if not isinstance(steps,list) or not 1<=len(steps)<=32:
        raise Failure('INVALID_REQUEST','Disk plan requires1..32 explicit steps')
    ids=set()
    spec=importlib.util.spec_from_file_location('_rw_disk_operation_contract',PRODUCT/'contracts.py')
    contract=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(contract)
    allowed_ops=set(contract.PARAMETERS)-{'describe','operation.status','operation.snapshot','operation.worker_launch','operation.worker_status','operation.worker_result','operation.worker_cancel','operation.worker_receive','character.save_copy','workspace.focus','workspace.select_bones'}
    for step in steps:
        if not isinstance(step,dict) or set(step)-{'id','operation','parameters','character_from_step','candidate_from_step','result_bindings','export_skin_packet','export_hair_packet','export_hair_author','export_rebuild_packet'} or not {'id','operation'}<=set(step):
            raise Failure('INVALID_REQUEST','Unknown plan step fields')
        if not isinstance(step['id'],str) or not re.fullmatch('[a-zA-Z][a-zA-Z0-9_-]{0,63}',step['id']) or step['id'] in ids:
            raise Failure('INVALID_REQUEST','Distinct step IDs required')
        if not isinstance(step['operation'],str) or step['operation'] not in allowed_ops or not isinstance(step.get('parameters',{}),dict):
            raise Failure('INVALID_REQUEST','Unknown disk operation')
        if 'export_skin_packet' in step and (type(step['export_skin_packet']) is not bool or step['operation']!='skin.prepare'):
            raise Failure('INVALID_REQUEST','export_skin_packet is a boolean opt-in for skin.prepare only')
        if 'export_hair_packet' in step and (type(step['export_hair_packet']) is not bool or step['operation']!='hair.preview'):
            raise Failure('INVALID_REQUEST','export_hair_packet is a boolean opt-in for hair.preview only')
        if 'export_hair_author' in step and (type(step['export_hair_author']) is not bool or step['operation']!='hair.run'):
            raise Failure('INVALID_REQUEST','export_hair_author is a boolean opt-in for hair.run only')
        if 'export_rebuild_packet' in step and (type(step['export_rebuild_packet']) is not bool or step['operation']!='skeleton.rebuild.prepare'):
            raise Failure('INVALID_REQUEST','export_rebuild_packet is a boolean opt-in for skeleton.rebuild.prepare only')
        if step.get('export_hair_author'):
            settings=step.get('parameters',{}).get('settings',{})
            action=settings.get('action') if isinstance(settings,dict) else None
            keys={'action','hair_id','path'} if action=='cache_import' else {'action','hair_id'}
            if action not in {'manual_capture','cache_import'} or set(settings)!=keys:
                raise Failure('INVALID_REQUEST','Hair author packet requires explicit Capture or frozen cache verification')
        if step.get('export_hair_packet'):
            settings=step.get('parameters',{}).get('settings',{})
            if (not isinstance(settings,dict) or settings.get('action')!='bake'
                    or not {'hair_id','start','end'}<=set(settings)
                    or set(settings)-{'action','hair_id','start','end','preroll','substeps','cache_file'}):
                raise Failure('INVALID_REQUEST','Hair packet requires explicit Base Bake hair_id/start/end')
        for binding in ('character_from_step','candidate_from_step'):
            if binding in step and (not isinstance(step[binding],str) or step[binding] not in ids):
                raise Failure('INVALID_REQUEST','Binding must reference an earlier explicit step')
        validate_result_bindings(step,ids)
        ids.add(step['id'])
    if steps[0]['operation'] in {'character.create_work_copy','character.diagnose'}:
        names=manifest.get('source_objects')
        if not isinstance(names,list) or not names or len(names)>128 or any(not isinstance(n,str) or not n for n in names) or len(set(names))!=len(names) or manifest.get('active_body') not in names:
            raise Failure('INVALID_REQUEST','Creation needs source_objects and active_body')
    elif not isinstance(manifest.get('character_id'),str) or not re.fullmatch('[0-9a-f]{32}',manifest['character_id']) or type(manifest.get('base_revision')) is not int:
        raise Failure('INVALID_REQUEST','Existing character needs explicit character_id/base_revision')
    current=implementation_sha256()
    if value.get('implementation_sha256',current)!=current:
        raise Failure('CONFLICT','Rigging implementation changed')
    # The existing strict JSON reader rejects duplicate keys; bound and finite.
    try:
        size=len(json.dumps(manifest,allow_nan=False).encode())
    except (ValueError,TypeError) as exc:
        raise Failure('INVALID_REQUEST','Manifest requires finite JSON data') from exc
    if size>262144:
        raise Failure('RESOURCE_LIMIT','Disk plan exceeds256KiB')
    return {**value,'resources':resources,'manifest':manifest,'implementation_sha256':current}


def input_documents(params):
    return list(params['resources'])+runtime_documents()


def describe():
    if PRODUCT is None:
        manifest = SOURCE/'exchange-runtime-manifest.json'
        if not manifest.is_file() or not (SOURCE/'modular_rig.py').is_file():
            raise Failure('NOT_FOUND', 'The rigging workflow source package is missing')
        return envelope('rigging-workflow.describe', data={
            'edition':'1.0.0-dev', 'domain':'rigging-workflow',
            'product_location':str(SOURCE), 'implementation_sha256':digest(manifest),
            'compatibility':{'command':'rigging-workflow phase3 describe',
                             'legacy_plan_service':'RIGGING_WORKFLOW_PRODUCT'},
            'modular':{'kinds':['HUMAN','QUADRUPED'],
                       'source_module':str(SOURCE/'modular_rig.py'),
                       'semantic_map_module':str(SOURCE/'semantic_rig_map.py')},
        })
    return {'schema':'1.0','domain':'rigging-workflow','stage':'M1 integration development',
        'product_location':str(PRODUCT),'location_source':'RIGGING_WORKFLOW_PRODUCT' if os.environ.get('RIGGING_WORKFLOW_PRODUCT') else str(LOCATION),
        'implementation_sha256':implementation_sha256(),
        'disk':{'command':'rigging-workflow.run','input':'SAVED_DISK_V1 with exact SHA',
                'output':'new job-owned .blend candidate, never overwrite input',
                'manifest_fields':['schema','scene','view_layer','character_id/base_revision or source_objects/active_body','steps'],
                'bindings':['character_from_step','candidate_from_step','result_bindings'],
                'result_binding':{'step':'earlier explicit step ID','result_path':['source_signature'],
                                  'parameter_path':['settings','expected_source_signature'],
                                  'target':'omitted field in an explicit parent dictionary; no literal overwrite'},
                'operation_limit':32,'cancel':'standard owned job cancel terminates disk worker; no partial output acceptance'},
        'live':{'command':'rigging-workflow.session','transport':'explicit local session descriptor, typed main-thread dispatch',
                'actions':['submit','status','result','cancel'],'write_preconditions':['character_id','expected_revision','expected_signature'],
                'queued_cancellation':True,'running_cancellation':'not interruptible; return actual state'},
        'scope':'Registered Core/Skeleton/Skin/Face/Hair operations of the selected product only; not full product acceptance'}
