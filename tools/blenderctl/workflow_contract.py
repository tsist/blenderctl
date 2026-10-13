"""Explicit sequential work units; the legacy pipeline admission set is unchanged."""
import copy,re
from protocol import Failure
from scene_contract import obj,array,enum,validate
import pipeline_contract as pipeline

EXTRA_COMMANDS=('geometry.run','geometry.inspect','material.run')
ALLOWED=(*pipeline.ALLOWED,*EXTRA_COMMANDS)
STEP=copy.deepcopy(pipeline.STEP)
STEP['properties']['command']=enum(*ALLOWED)
POLICY=copy.deepcopy(pipeline.POLICY)
POLICY['properties']['max_workers']={'const':1}
MANIFEST=obj({'workflow_version':{'const':'1.0'},'policy':POLICY,'steps':array(STEP,1,32)})

def normalize(value):
    value=copy.deepcopy(value)
    validate(value,MANIFEST)
    # Share the existing topology/binding/budget validator through its explicit
    # schema argument. The workflow commands are never added to pipeline.ALLOWED.
    value=pipeline.normalize(value,schema=MANIFEST)
    return value

def normalize_request(command,params):
    params=copy.deepcopy(params)
    allowed={'job_id'} if command=='workflow.resume' else {'manifest','reuse_job'}
    if set(params)-allowed or (command=='workflow.resume' and set(params)!={'job_id'}):
        raise Failure('INVALID_REQUEST','Invalid workflow request fields')
    if command!='workflow.resume':
        if 'manifest' not in params:raise Failure('INVALID_REQUEST','Workflow requires manifest')
        params['manifest']=normalize(params['manifest'])
    for field in ('job_id','reuse_job'):
        if field in params and (not isinstance(params[field],str) or not re.fullmatch('[0-9a-f]{32}',params[field])):
            raise Failure('INVALID_REQUEST','Prior workflow job ID must be 32 lowercase hex characters')
    return params

def validate_prior(command,workflow_mode):
    if workflow_mode:
        if command not in ('workflow.run','workflow.resume'):
            raise Failure('INVALID_REQUEST','Workflow recovery source must be a workflow execution job')
    elif not command.startswith('pipeline.') and command!='asset.preview.batch':
        raise Failure('INVALID_REQUEST','Reuse source must be a pipeline/batch job in this jobs directory')

def requires_gpu(command,params):
    if command=='material.run':
        return params['manifest']['preview']['device']['backend']!='CPU'
    if command=='geometry.run':
        preview=params['manifest'].get('preview',{})
        return preview.get('enabled',False) and preview.get('engine','CYCLES')=='BLENDER_EEVEE'
    return False
