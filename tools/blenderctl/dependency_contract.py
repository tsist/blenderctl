# SPDX-License-Identifier: GPL-3.0-or-later
"""Versioned typed dependency and same-content relink contracts (no bpy)."""
from pathlib import Path
from protocol import Failure, read_json
from node_contract import FILE, array, obj, validate

HASH={'type':'string','pattern':'^[0-9a-f]{64}$'}
TEXT={'type':'string','minLength':1}
CLOSURE=obj({'adapter':{'const':'TYPED_FILES_V1'},'max_depth':{'type':'integer','minimum':1,'maximum':16},'max_files':{'type':'integer','minimum':1,'maximum':10000},'simulation_receipt':FILE},['adapter'])
MEMBER=obj({'from':TEXT,'to':FILE},['from','to'])
MAPPING=obj({'resource_id':HASH,'members':array(MEMBER,1,1000)},['resource_id','members'])
RELINK=obj({'version':{'const':'1.0'},'baseline':FILE,'mappings':array(MAPPING,1,100)},['version','baseline','mappings'])

def normalize_closure(value):
    validate(value,CLOSURE)
    return {'max_depth':8,'max_files':1000,**value}

def normalize(params,command):
    from protocol import validate as request_validate
    base=request_validate({'schema_version':'1.0','command':'query','params':{k:params[k] for k in ('file','expected_sha256') if k in params}})['params']
    if 'expected_sha256' not in base:raise Failure('INVALID_REQUEST','Dependency relink requires source SHA256')
    if command=='dependency.plan-relink':
        validate(params.get('manifest'),RELINK)
        for mapping in params['manifest']['mappings']:
            for member in mapping['members']:
                if not Path(member['from']).is_absolute():raise Failure('INVALID_REQUEST','Relink from path must be absolute')
        return {**base,'manifest':params['manifest']}
    validate(params.get('plan'),FILE)
    return {**base,'plan':params['plan']}

def input_documents(params,command):
    """Descriptors are subsequently hash-locked by the supervisor."""
    closure=params.get('profile',{}).get('closure',{})
    documents=[]
    if closure.get('simulation_receipt'):
        descriptor=closure['simulation_receipt'];documents.append(descriptor)
        from simulation_contract import receipt_contract
        receipt=read_json(descriptor['file']);receipt_contract(receipt)
        documents.extend(receipt['files']);documents.extend(receipt.get('resources',[]))
    if command=='dependency.plan-relink':
        spec=params['manifest'];documents.append(spec['baseline'])
        baseline=read_json(spec['baseline']['file']);removed={str(Path(m['from']).resolve()).casefold() for r in spec['mappings'] for m in r['members']}
        documents.extend(f for f in baseline.get('documents',[]) if str(Path(f['file']).resolve()).casefold() not in removed)
        documents.extend(m['to'] for r in spec['mappings'] for m in r['members'])
    elif command=='dependency.prepare':
        documents.append(params['plan']);plan=read_json(params['plan']['file'])
        documents.extend(plan.get('inputs',[]));documents.append(plan['baseline'])
    return documents
