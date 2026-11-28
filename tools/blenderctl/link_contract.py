"""S05 bounded library workflows, shared by host validation and schema export."""
from pathlib import Path
from protocol import Failure, read_json
from scene_contract import obj, array, enum, integer, number, NAME, NULL_NAME, BOOL, VEC, validate
from node_contract import FILE

COMMANDS=('project.link.prepare','project.override.prepare','project.override.resync')
TEXT={'type':'string','minLength':1}
UUID={'type':'string','pattern':'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'}
SELECT=obj({'type':enum('Object','Collection'),'name':NAME,'asset_id':UUID},['type','name'])
REFERENCE=obj({**SELECT['properties'],'library':TEXT},['type','name','library'])
CONTEXT=obj({'scene':NAME,'view_layer':NAME})
TRANSFORMS = {'location': VEC, 'rotation_euler': VEC, 'scale': VEC,
              'rotation_quaternion': array(number(-1e6, 1e6), 4, 4),
              'rotation_axis_angle': array(number(-1e6, 1e6), 4, 4)}
POSE = obj({'name': NAME, 'values': obj(TRANSFORMS, [])})
SLOT = obj({'slot': integer(0, 65535), 'material': NULL_NAME})
VALUES = obj({**TRANSFORMS, 'delta_location': VEC, 'delta_scale': VEC,
              'hide_render': BOOL, 'hide_viewport': BOOL, 'show_in_front': BOOL,
              'color': array(number(0, 1), 4, 4),
              'pose_bones': array(POSE, 1, 4096), 'material_slots': array(SLOT, 1, 4096)}, [])
ADAPTERS = enum('LIBRARY_OVERRIDE_5_2_V1', 'LIBRARY_OVERRIDE_5_3_V1')
EDIT=obj({'target':REFERENCE,'values':VALUES})
BASE={'version':{'const':'1.0'},'adapter':ADAPTERS,'context':CONTEXT}
LINK=obj({**BASE,'library':FILE,'selections':array(SELECT,1,32),'placement':enum('ATTACH','INSTANCE'),'instance_name':NAME},[*BASE,'library','selections','placement'])
OVERRIDE=obj({**BASE,'root':REFERENCE,'edits':array(EDIT,1,128)})
RESYNC=obj({'version':{'const':'1.0'},'adapter':ADAPTERS,'receipt':FILE,'updates':array(FILE,0,1000)})
MANIFESTS=dict(zip(COMMANDS,(LINK,OVERRIDE,RESYNC)))

# Receipt shapes are shared by the runtime reader and published JSON Schema.
# Nullable fields use oneOf because the small standard-library validator uses
# single type names. Semantic path/identity uniqueness is checked below.
HASH=FILE['properties']['expected_sha256']
ABSOLUTE={'type':'string','minLength':1,'pattern':r'^(?:[A-Za-z]:[\\/]|\\\\[^\\/]+[\\/][^\\/]+[\\/]|/)'}
DOCUMENT=obj({'file':ABSOLUTE,'expected_sha256':HASH})
DOCUMENTS=array(DOCUMENT,1,1000)
IDENTITY=obj({'type':TEXT,'name':TEXT,'library':{'oneOf':[ABSOLUTE,{'type':'null'}]},'asset_id':UUID},['type','name','library'])
INVENTORY=obj({'identity':IDENTITY,'content_sha256':HASH})
def many(item):return {'type':'array','items':item}
CHANGES=obj({'added':many(INVENTORY),'removed':many(INVENTORY),'changed':many(obj({'before':INVENTORY,'after':INVENTORY}))})
OVERRIDE_RECORD=obj({'identity':IDENTITY,'reference':IDENTITY,'root':IDENTITY,'system':BOOL})
RECEIPT_REFERENCE=obj({**REFERENCE['properties'],'library':ABSOLUTE},['type','name','library'])
RECEIPT_EDIT=obj({'target':obj({**RECEIPT_REFERENCE['properties'],'type':{'const':'Object'}},['type','name','library']),'values':{**VALUES,'minProperties':1}})
REPORT_COMMON={
    'version':{'const':'1.0'},'adapter':ADAPTERS,
    'candidate':ABSOLUTE,'candidate_sha256':HASH,'resources':DOCUMENTS,
    'references':many(INVENTORY),'context':CONTEXT,'source':{'oneOf':[obj({}),DOCUMENT]},
    'blender_build':TEXT,'blender_version':array(integer(0, 1000), 3, 3),
    'implementation_sha256':HASH,'reopen':{'const':'pass'},
    'source_saved':{'type':'boolean','const':False},'publication':{'const':'working_candidate_only'},
    'upstream_changes':CHANGES,'overrides':many(OVERRIDE_RECORD),'scope':TEXT,
    'local_content':many({'type':'object'}),
}
TRANSACTION_TEMPLATE=obj({'kind':{'const':'blend_exact'},'mode':{'const':'create'},'file':ABSOLUTE,'dependencies':DOCUMENTS})
LINK_REPORT=obj({**REPORT_COMMON,'kind':{'const':'link'},'transaction_item':TRANSACTION_TEMPLATE})
RECEIPT=obj({**REPORT_COMMON,'kind':{'const':'override'},'source':DOCUMENT,'root':RECEIPT_REFERENCE,'edits':array(RECEIPT_EDIT,1,128),'overrides':{**many(OVERRIDE_RECORD),'minItems':1}})
OVERRIDE_REPORT=obj({**RECEIPT['properties'],'receipt':ABSOLUTE,'receipt_sha256':HASH,'transaction_item':TRANSACTION_TEMPLATE})

def pathkey(path):return str(Path(path).resolve()).casefold()

def validate_edit(values):
    bones = values.get('pose_bones', [])
    slots = values.get('material_slots', [])
    if (len({entry['name'] for entry in bones}) != len(bones)
        or len({entry['slot'] for entry in slots}) != len(slots)
        or any(not entry['values'] for entry in bones)):
        raise Failure('INVALID_REQUEST', 'Pose-bone and material-slot edits must be bounded, unique and nonempty')
    for row in [values, *(entry['values'] for entry in bones)]:
        for key in ('scale', 'delta_scale'):
            if any(abs(value) < 1e-8 for value in row.get(key, [])):
                raise Failure('INVALID_REQUEST', 'Singular scale is unsupported')


def normalize(params,command):
    from protocol import validate as validate_request
    validate(params.get('manifest'),MANIFESTS[command])
    result={'manifest':params['manifest']}
    if 'file' in params:
        if 'expected_sha256' not in params:raise Failure('INVALID_REQUEST','Library candidates require input SHA256')
        result.update(validate_request({'schema_version':'1.0','command':'query','params':{k:params[k] for k in ('file','expected_sha256')}})['params'])
    elif command!='project.link.prepare' or 'expected_sha256' in params:
        raise Failure('INVALID_REQUEST','Override requires a hashed input blend')
    spec=result['manifest']
    if command==COMMANDS[0]:
        if spec['placement']=='INSTANCE' and (len(spec['selections'])!=1 or spec['selections'][0]['type']!='Collection' or 'instance_name' not in spec):raise Failure('INVALID_REQUEST','INSTANCE requires one Collection and an explicit instance_name')
        if spec['placement']=='ATTACH' and 'instance_name' in spec:raise Failure('INVALID_REQUEST','instance_name belongs to INSTANCE placement')
        keys=[(s['type'],s['name']) for s in spec['selections']]
        if len(set(keys))!=len(keys):raise Failure('INVALID_REQUEST','Duplicate library selection')
    if command==COMMANDS[1]:
        seen=set()
        for e in spec['edits']:
            t=e['target'];key=(t['type'],t['name'],pathkey(t['library']))
            if t['type']!='Object' or key in seen or not e['values']:raise Failure('INVALID_REQUEST','Edits require unique Object targets and nonempty values')
            seen.add(key)
            validate_edit(e['values'])
    for ref in ([spec['root']]+[e['target'] for e in spec['edits']] if command==COMMANDS[1] else []):
        if not Path(ref['library']).is_absolute():raise Failure('INVALID_REQUEST','Library identity needs an absolute owner path')
    docs=([spec['library']] if command==COMMANDS[0] else [spec['receipt'],*spec['updates']] if command==COMMANDS[2] else [])
    for d in docs:
        if not Path(d['file']).is_absolute():raise Failure('INVALID_REQUEST','Input descriptors require absolute paths')
    if command==COMMANDS[2] and len({pathkey(d['file']) for d in spec['updates']})!=len(spec['updates']):raise Failure('INVALID_REQUEST','Duplicate update path')
    return result

def receipt_contract(value):
    validate(value,RECEIPT)
    if len({pathkey(d['file']) for d in value['resources']})!=len(value['resources']):raise Failure('INVALID_REQUEST','Duplicate override receipt resource')
    seen=set()
    for edit in value['edits']:
        target=edit['target'];key=(target['type'],target['name'],pathkey(target['library']))
        if not edit['values'] or key in seen:raise Failure('INVALID_REQUEST','Override receipt edits require unique targets and nonempty values')
        validate_edit(edit['values'])
        seen.add(key)
    for document in [value['source'],*value['resources']]:
        if not Path(document['file']).is_absolute():raise Failure('INVALID_REQUEST','Override receipt needs absolute document paths')
    return value

def input_documents(params,command):
    spec=params['manifest']
    if command==COMMANDS[0]:return [spec['library']]
    if command==COMMANDS[1]:return []
    receipt=receipt_contract(read_json(spec['receipt']['file']))
    updates={pathkey(d['file']):d for d in spec['updates']}
    docs={pathkey(d['file']):d for d in receipt['resources']};docs.update(updates)
    return [spec['receipt'],*docs.values()]
