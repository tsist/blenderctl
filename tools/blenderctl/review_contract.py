# SPDX-License-Identifier: GPL-3.0-or-later
"""Explicit local review records: content bindings, never an OS security boundary."""
import hashlib,json,os
from pathlib import Path
from scene_contract import obj,array,NAME,validate
from node_contract import FILE
from exchange_contract import SCRIPT,descriptor,normalize as script_normalize
from protocol import Failure,ROOT,VERSION
TEXT={'type':'string','minLength':1,'maxLength':4096}
SHA={'type':'string','pattern':'^[0-9a-f]{64}$'}
ACK='reviewed_exact_request_accept_unsandboxed_local_execution'
ACK_ISOLATED='reviewed_exact_request_accept_windows_appcontainer_execution'
LIMITS={'os_sandbox':False,'identity_authenticated':False,'signed':False,'declared_inputs_complete':False,'external_send_authorized':False,'installation_authorized':False,'artistic_quality_approved':False}
DECLARATIONS=obj({'source':TEXT,'read_purpose':TEXT,'write_purpose':TEXT,'network_purpose':TEXT,'process_purpose':TEXT})
REVIEW_SPEC=obj({'execution':SCRIPT,'resources':array(FILE,0,100),'declarations':DECLARATIONS})
ENV=obj({'cli_version':TEXT,'implementation_sha256':SHA,'blender_sha256':SHA})
REVIEW=obj({'version':{'const':'1.0'},'kind':{'const':'script_review'},'spec':REVIEW_SPEC,'environment':ENV,'source_utf8':{'type':'string','maxLength':1048576},'imports':array(TEXT,0,10000),'binding_sha256':SHA,'limitations':{'const':LIMITS}})
RECEIPT=obj({'version':{'const':'1.0'},'kind':{'const':'script_trust_receipt'},'review':FILE,'binding_sha256':SHA,'reviewer':NAME,'acknowledgment':{'enum':[ACK,ACK_ISOLATED]},'created_at':{'type':'number'},'reuse':{'const':'exact_request_only'},'limitations':{'const':LIMITS}})
APPROVE=obj({'review':FILE,'reviewer':NAME,'acknowledgment':{'enum':[ACK,ACK_ISOLATED]}})
SCULPT=obj({'before':FILE,'after':FILE,'object':NAME,'artistic_intent':TEXT})
AUTHORIZATION={'oneOf':[obj({'mode':{'const':'trust_literal'},'limitations':{'const':LIMITS}}),obj({'mode':{'const':'review_receipt'},'receipt':FILE,'review':FILE,'binding_sha256':SHA,'reviewer':NAME,'reuse':{'const':'exact_request_only'},'limitations':{'const':LIMITS}})]}
CONTRACTS={'extension.review':obj({'manifest':REVIEW_SPEC}),'extension.approve':obj({'manifest':APPROVE}),'sculpt.review':obj({'manifest':SCULPT})}

def canonical(value):
    try:
        raw=json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode('utf-8')
        if len(raw)>4*1024*1024:raise ValueError('Review JSON exceeds 4 MiB')
        return raw
    except (ValueError,TypeError,RecursionError) as ex:raise Failure('INVALID_REQUEST','Invalid bounded review JSON: '+str(ex)) from ex

def key(value):return hashlib.sha256(canonical(value)).hexdigest()

def normalized_execution(execution,resources):
    script_normalize(execution,'run')
    def norm(d):
        descriptor(d)
        return {'file':os.path.normcase(str(Path(d['file']).resolve())),'expected_sha256':d['expected_sha256']}
    inputs=[norm(d) for d in resources]
    if len({d['file'] for d in inputs})!=len(inputs):raise Failure('INVALID_REQUEST','Duplicate reviewed input path')
    return {'execution':{**execution,'script':norm(execution['script'])},'resources':sorted(inputs,key=lambda d:d['file'])}

def binding(spec,environment):return key({**normalized_execution(spec['execution'],spec['resources']),'declarations':spec['declarations'],'environment':environment})

def normalize(params,command):
    validate(params,CONTRACTS[command]);canonical(params)
    m=params['manifest']
    if command=='extension.review':normalized_execution(m['execution'],m['resources'])
    elif command=='extension.approve':descriptor(m['review'])
    else:
        for d in (m['before'],m['after']):
            descriptor(d)
            if Path(d['file']).suffix.lower()!='.blend':raise Failure('INVALID_REQUEST','Sculpt review requires blend files')
    return params
