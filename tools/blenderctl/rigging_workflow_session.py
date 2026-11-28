"""Pure host facade for the product's explicit local main-thread transport."""
import importlib
import json
import math
from pathlib import Path
import sys
import time
import types
import uuid
from protocol import Failure
from rigging_workflow_contract import PRODUCT


def _client():
    namespace='_blenderctl_rigging_transport'
    folder=PRODUCT/'core/transport'
    if not (folder/'client.py').is_file():
        raise Failure('UNSUPPORTED','Product live client has not been integrated')
    if namespace not in sys.modules:
        package=types.ModuleType(namespace)
        package.__path__=[str(folder)]
        sys.modules[namespace]=package
    return importlib.import_module(namespace+'.client')


def dispatch(params):
    fields={'session','action','request','job_id','request_id','wait_seconds'}
    if not isinstance(params,dict) or set(params)-fields:
        raise Failure('INVALID_REQUEST','Unknown rigging session parameters')
    descriptor=params.get('session')
    action=params.get('action')
    if not isinstance(descriptor,str) or not Path(descriptor).is_absolute() or not isinstance(action,str) or action not in {'submit','status','result','cancel'}:
        raise Failure('INVALID_REQUEST','Explicit session descriptor and typed action required')
    seconds=params.get('wait_seconds',0)
    if type(seconds) not in (int,float) or not math.isfinite(seconds) or not 0<=seconds<=55:
        raise Failure('INVALID_REQUEST','wait_seconds must be0..55')
    client=_client()
    known_job_id=None
    submission=None
    try:
        if action=='submit':
            operation=params.get('request')
            if not isinstance(operation,dict) or 'job_id' in params:
                raise Failure('INVALID_REQUEST','Submit needs a typed request and no existing job_id')
            operation=dict(operation)
            params_id_given='request_id' in params
            operation_id_given='request_id' in operation
            rid=params.get('request_id')
            operation_rid=operation.get('request_id')
            if params_id_given and (not isinstance(rid,str) or not client.SESSION_ID_RE.fullmatch(rid)):
                raise Failure('INVALID_REQUEST','request_id must be 32 lowercase hex')
            if operation_id_given and (not isinstance(operation_rid,str) or not client.SESSION_ID_RE.fullmatch(operation_rid)):
                raise Failure('INVALID_REQUEST','request_id must be 32 lowercase hex')
            if params_id_given and operation_id_given and rid!=operation_rid:
                raise Failure('INVALID_REQUEST','Transport and operation request IDs differ')
            if not params_id_given:
                rid=operation_rid if operation_id_given else uuid.uuid4().hex
            operation.update(request_id=rid,actor=operation.get('actor','ai'))
            # submit may publish the request before its observation fails.
            known_job_id=rid
            submission=client.submit(descriptor,operation,request_id=rid)
            job_id=submission['job_id']
        else:
            if 'request' in params or 'request_id' in params or not isinstance(params.get('job_id'),str):
                raise Failure('INVALID_REQUEST','Status/result/cancel need exactly one existing job_id')
            job_id=params['job_id']
            if not client.SESSION_ID_RE.fullmatch(job_id):
                raise Failure('INVALID_REQUEST','job_id must be 32 lowercase hex')
            known_job_id=job_id
            if action=='cancel':
                cancellation=client.cancel(descriptor,job_id)
                return {'ok':True,'command':'rigging-workflow.session','action':action,'data':cancellation}
        deadline=time.monotonic()+seconds
        final=None
        while True:
            final=client.result(descriptor,job_id)
            if final is not None or time.monotonic()>=deadline:
                break
            time.sleep(min(0.1,max(0,deadline-time.monotonic())))
        state=client.status(descriptor,job_id)
        terminal_status=state.get('status')
        ok=terminal_status not in {'failed','stale'}
        response={'ok':ok,'command':'rigging-workflow.session','action':action,'job_id':job_id,
                  'data':{'status':state,'result':final,'submission':submission}}
        if not ok:
            response['error']={'code':'CONFLICT' if terminal_status=='stale' else 'WORKER_FAILED',
                               'message':'Live operation '+str(terminal_status)+'; inspect its recorded result'}
        return response
    except Failure:
        raise
    except OSError as exc:
        if known_job_id is None:
            raise Failure('INVALID_REQUEST',str(exc)) from exc
        # The request may have executed. Keep its identity for observation;
        # never retry a potentially published submit or disclose credentials.
        return {'ok':False,'command':'rigging-workflow.session','action':action,
                'job_id':known_job_id,
                'error':{'code':'IO_ERROR','message':
                    'Live submission/observation state is unknown after an I/O error; '
                    'query result for the original job_id. Do not resubmit or infer worker failure.'},
                'data':{'status':None,'result':None,'submission':submission,
                        'resume':{'action':'result','job_id':known_job_id}}}
    except (ValueError,TypeError,KeyError,RuntimeError) as exc:
        # Product transport errors are data-only diagnostics. Never print the
        # descriptor/capability contents or retry an uncertain write.
        raise Failure('INVALID_REQUEST',str(exc)) from exc
