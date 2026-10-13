"""Workflow host adapter; reuse the existing supervisor, never nest workers."""
from workflow_contract import normalize

def execute(job,parent_checkpoint=None):
    import time
    from pipeline import execute as execute_pipeline
    from workflow_evidence import delivery_index
    from protocol import atomic_json, read_json, CODES, Failure
    from timing import Trace
    trace=Trace(job/'timing.json',metadata={'scope':'host workflow execution; excludes client and delivery-index finalization','job_id':job.name})
    started=time.monotonic();launch=read_json(job/'launch.json')
    def checkpoint():
        if parent_checkpoint:parent_checkpoint()
        if (job/'cancel.request').exists():raise Failure('CANCELLED','Workflow cancelled during delivery finalization')
        if time.monotonic()-started>launch['timeout_seconds']:raise Failure('TIMEOUT','Workflow exceeded timeout during delivery finalization')
        state=read_json(job/'status.json')
        if time.time()-state['heartbeat_at']>=1:
            state['heartbeat_at']=time.time();atomic_json(job/'status.json',state)
    try:
        with trace.span('workflow_execution') as span:
            result, code = execute_pipeline(job,parent_checkpoint,manifest_normalizer=normalize,workflow_mode=True,defer_publication=True)
        if result.get('error'):
            span['status']='failed';span['error']=result['error']
        trace.finish(Exception(str(result['error'])) if result.get('error') else None)
    except BaseException as exc:
        trace.finish(exc)
        raise
    result['artifacts']['timing']=str(job/'timing.json')
    try:
        index = delivery_index(job, result, checkpoint)
        result['artifacts']['delivery_index'] = index['file']
        result['artifacts']['delivery_index_sha256'] = index['expected_sha256']
        result['artifacts']['delivery_readme'] = str(job / 'DELIVERY.md')
    except (Failure, OSError) as exc:
        code = CODES[exc.code] if isinstance(exc, Failure) else CODES['IO_ERROR']
        result.update(ok=False, data=None, error={'code':exc.code if isinstance(exc,Failure) else 'IO_ERROR',
                                               'message':'Delivery index failed; complete execution report is retained: '+str(exc)})
    state=read_json(job/'status.json')
    state.update(state='cancelled' if code==7 else 'timed_out' if code==6 else 'failed' if code else 'succeeded',
                 phase='complete',exit_code=code,finished_at=time.time(),heartbeat_at=time.time(),elapsed_seconds=round(time.monotonic()-started,3))
    checkpoint_cancelled=(job/'cancel.request').exists()
    if checkpoint_cancelled and code==0:
        code=CODES['CANCELLED'];result.update(ok=False,data=None,error={'code':'CANCELLED','message':'Workflow cancelled before final publication'})
        state.update(state='cancelled',exit_code=code)
    atomic_json(job / 'result.json', result)
    atomic_json(job / 'status.json', state)
    return result, code
