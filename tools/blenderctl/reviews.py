# SPDX-License-Identifier: GPL-3.0-or-later
"""Static script review and local, self-asserted trust receipts. Never imports reviewed code."""
import ast,hashlib,time
from pathlib import Path
from contextlib import ExitStack
from filesystem import FileGuard
from protocol import ROOT,VERSION,Failure,atomic_json,read_json
from review_contract import (REVIEW,RECEIPT,LIMITS,ACK,ACK_ISOLATED,validate,normalize,descriptor,key,binding,normalized_execution,canonical)

def protect(d,stack,checkpoint=lambda:None,max_bytes=256*1024*1024):
    descriptor(d)
    guard=stack.enter_context(FileGuard(d['file']))
    if Path(d['file']).stat().st_size>max_bytes:raise Failure('UNSUPPORTED','Review input exceeds bounded size')
    actual=guard.sha256(checkpoint)
    if actual!=d['expected_sha256']:raise Failure('CONFLICT','Reviewed file content changed: '+d['file'])
    return guard

def document(d,schema,stack,checkpoint):
    protect(d,stack,checkpoint,4*1024*1024)
    value=read_json(d['file']);validate(value,schema);canonical(value)
    return value

def environment(blender,stack,checkpoint):
    hashes={}
    for p in sorted((ROOT/'tools/blenderctl').glob('*.py')):
        g=stack.enter_context(FileGuard(p));hashes[p.name]=g.sha256(checkpoint)
    g=stack.enter_context(FileGuard(blender))
    return {'cli_version':VERSION,'implementation_sha256':key(hashes),'blender_sha256':g.sha256(checkpoint)}

def check_review(d,blender,stack,checkpoint):
    r=document(d,REVIEW,stack,checkpoint)
    spec=r['spec'];normalized_execution(spec['execution'],spec['resources'])
    if binding(spec,r['environment'])!=r['binding_sha256']:raise Failure('CONFLICT','Review binding inconsistent')
    if key(environment(blender,stack,checkpoint))!=key(r['environment']):raise Failure('CONFLICT','Reviewed implementation or Blender executable changed')
    script=protect(spec['execution']['script'],stack,checkpoint,1024*1024)
    raw=b''.join(script.chunks())
    try:source=raw.decode('utf-8-sig')
    except UnicodeError as ex:raise Failure('INVALID_REQUEST','Review requires UTF-8 Python source') from ex
    if source!=r['source_utf8']:raise Failure('CONFLICT','Review source snapshot differs from script')
    for d in spec['resources']:protect(d,stack,checkpoint)
    return r

def verify_run(params,blender,stack,checkpoint=lambda:None):
    if 'review_receipt' not in params:return {'mode':'trust_literal','limitations':LIMITS}
    receipt=document(params['review_receipt'],RECEIPT,stack,checkpoint)
    review=check_review(receipt['review'],blender,stack,checkpoint)
    expected_ack=ACK_ISOLATED if 'isolation' in review['spec']['execution'] else ACK
    if receipt['acknowledgment']!=expected_ack:raise Failure('CONFLICT','Receipt acknowledgment differs from isolation profile')
    if receipt['binding_sha256']!=review['binding_sha256']:raise Failure('CONFLICT','Receipt binding differs from review')
    expected=normalized_execution(review['spec']['execution'],review['spec']['resources'])
    actual=normalized_execution(params['manifest'],params.get('resources',[]))
    if key(expected)!=key(actual):raise Failure('CONFLICT','Execution or declared input set differs from reviewed request')
    return {'mode':'review_receipt','receipt':params['review_receipt'],'review':receipt['review'],'binding_sha256':receipt['binding_sha256'],'reviewer':receipt['reviewer'],'reuse':receipt['reuse'],'limitations':LIMITS}

def run(request,launch,job,checkpoint):
    command=request['command'];spec=normalize(request['params'],command)['manifest']
    with ExitStack() as stack:
        if command=='extension.review':
            if Path(spec['execution']['script']['file']).suffix.lower()!='.py':raise Failure('INVALID_REQUEST','Review requires .py source')
            env=environment(launch['blender'],stack,checkpoint)
            g=protect(spec['execution']['script'],stack,checkpoint,1024*1024)
            raw=b''.join(g.chunks(checkpoint))
            try:source=raw.decode('utf-8-sig');tree=ast.parse(source)
            except (UnicodeError,SyntaxError,ValueError,RecursionError) as ex:raise Failure('INVALID_REQUEST','Cannot statically parse source: '+str(ex)) from ex
            for d in spec['resources']:protect(d,stack,checkpoint)
            imports=sorted({a.name for n in ast.walk(tree) if isinstance(n,ast.Import) for a in n.names}|{n.module or '.' for n in ast.walk(tree) if isinstance(n,ast.ImportFrom)})
            r={'version':'1.0','kind':'script_review','spec':spec,'environment':env,'source_utf8':source,'imports':imports,'binding_sha256':binding(spec,env),'limitations':LIMITS}
            validate(r,REVIEW);canonical(r);name='extension-review.json'
        else:
            review=check_review(spec['review'],launch['blender'],stack,checkpoint)
            expected_ack=ACK_ISOLATED if 'isolation' in review['spec']['execution'] else ACK
            if spec['acknowledgment']!=expected_ack:raise Failure('INVALID_REQUEST','Acknowledgment differs from isolation profile')
            r={'version':'1.0','kind':'script_trust_receipt','review':spec['review'],'binding_sha256':review['binding_sha256'],'reviewer':spec['reviewer'],'acknowledgment':expected_ack,'created_at':time.time(),'reuse':'exact_request_only','limitations':LIMITS}
            validate(r,RECEIPT);name='extension-approval-receipt.json'
        atomic_json(job/name,r)
        g=stack.enter_context(FileGuard(job/name))
        return {'document':{'file':str(job/name),'expected_sha256':g.sha256(checkpoint)},'binding_sha256':r['binding_sha256'],'limitations':LIMITS}
