"""Nonblocking local session client. Stdlib/pure contracts only, no Torch/bpy."""
import copy,hashlib,json,os,re,time,uuid
from pathlib import Path
from compositing_relight_session_contract import normalized_parameters,signature,IDENTITY_FIELDS
from compositing_depth_contract import loads_strict
import compositing_relight_local_edits as edits

def read(path):
    path=Path(path)
    if path.stat().st_size>16777216:raise ValueError('Bounded JSON exceeded')
    # Windows replace/read handles can briefly deny sharing. Bounded local
    # retry (under40ms), not an unbounded UI wait or silent stale fallback.
    for attempt in range(4):
        try:return loads_strict(path.read_bytes())
        except PermissionError:
            if attempt==3:raise
            time.sleep(.005)

def sha(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda:f.read(1048576),b''):h.update(block)
    return h.hexdigest()

def publish(path,value):
    path=Path(path);temp=path.with_name('.'+uuid.uuid4().hex+'.partial')
    with temp.open('x',encoding='utf-8') as f:json.dump(value,f,ensure_ascii=False,allow_nan=False);f.flush();os.fsync(f.fileno())
    os.rename(temp,path)

class SessionClient:
    def __init__(self,ready_file):
        self.path=Path(ready_file).resolve();self.job=self.path.parent
        if self.path.name!='session-ready.json' or not re.fullmatch('[0-9a-f]{32}',self.job.name):raise ValueError('Public owned ready path required')
        self._ready=read(self.path)
        if self._ready.get('schema')!='relight-session-ready-v1' or self._ready.get('session_id')!=self.job.name:raise ValueError('Wrong session ready')
        request=read(self.job/'request.json');launch=read(self.job/'launch.json')
        if request.get('command')!='compositing.depth-session' or launch.get('session_request_signature')!=signature(request['params']) or self._ready.get('request_signature')!=signature(request['params']):raise ValueError('Queued session authority differs')
        if self._ready.get('producer_identity')!=launch.get('session_identity'):raise ValueError('Session producer identity differs')
        self.ready_sha=sha(self.path);self.inbox=self.job/'session/inbox';self.root=self.job/'session/gpu/revisions'
        self.revision=max([0]+[int(p.stem) for p in self.inbox.glob('*.json') if re.fullmatch('[0-9]{9,10}',p.stem)])
        self._desired=None;self.last_loaded=None;self.last_reason='none';self.submitted=[]
        self._live()
    @property
    def ready(self):return copy.deepcopy(self._ready)
    @property
    def desired(self):return copy.deepcopy(self._desired)
    def _live(self):
        if sha(self.path)!=self.ready_sha:raise ValueError('Ready changed')
        s=read(self.job/'status.json')
        if s.get('job_id')!=self.job.name or s.get('state')!='running':raise RuntimeError('Session is not running')
        if time.time()-s.get('heartbeat_at',0)>10:raise RuntimeError('Session heartbeat stale')
        return s
    def submit(self,config,kind='preview',reason='edit',local_edits=None):
        self._live();value={'config':config,'kind':kind}
        if local_edits is not None:
            if self._ready.get('local_edits_version')!=edits.VERSION:raise ValueError('Session does not support local edits')
            value['local_edits']=local_edits
        p=normalized_parameters(value,self._ready['initial_config']);self.revision+=1
        message={'schema':'relight-session-message-v1',**{k:self._ready[k] for k in IDENTITY_FIELDS},
                 'revision':self.revision,'parameters':p,'parameters_sha256':signature(p),'reason':reason}
        if type(reason) is not str or not 0<len(reason)<=64:raise ValueError('Invalid reason')
        path=self.inbox/(str(self.revision).zfill(9)+'.json');publish(path,message)
        self._desired=copy.deepcopy(message);result={'revision':self.revision,'parameters_sha256':message['parameters_sha256'],'file':str(path),'submitted_perf':time.perf_counter()}
        self.submitted.append(result);return result
    def poll(self):
        self._live();path=self.job/'session/latest.json'
        if self._desired is None:return None
        rejected=self.job/'session'/('rejected-revision-'+str(self._desired['revision'])+'.json')
        if rejected.is_file():
            failure=read(rejected)
            if failure.get('parameters_sha256')!=self._desired['parameters_sha256']:raise ValueError('Rejected revision identity differs')
            self.last_reason='rejected';raise ValueError(failure.get('message','Local edit rejected'))
        if not path.is_file():return None
        manifest=read(path);request=manifest.get('request',{})
        if manifest.get('schema')!='relight-session-published-v1' or manifest.get('status')!='passed':self.last_reason='invalid_manifest';return None
        expected={k:self._desired[k] for k in (*IDENTITY_FIELDS,'revision','parameters_sha256')}
        if any(type(request.get(k)) is not type(v) or request.get(k)!=v for k,v in expected.items()):self.last_reason='stale_or_alien';return None
        if type(request.get('request_id')) is not str or not re.fullmatch('[0-9a-f]{32}',request['request_id']):raise ValueError('Invalid dispatch identity')
        if signature(request.get('parameters'))!=self._desired['parameters_sha256'] or request['parameters']!=self._desired['parameters']:raise ValueError('Complete parameter body differs from immutable request')
        if type(manifest.get('revision')) is not int or manifest['revision']!=request['revision'] or manifest.get('parameters_sha256')!=request['parameters_sha256']:raise ValueError('Manifest revision/SHA differs')
        if manifest.get('session_must_be_running') is not True or manifest.get('parent_success_required') is not False:raise ValueError('Invalid live preview authority')
        if self._ready.get('local_edits_version')==edits.VERSION:
            expected_data=edits.derived_id(self._ready['cache_id'],self._desired['parameters'].get('local_edits',edits.empty()))
            if manifest.get('derived_cache_id')!=expected_data:raise ValueError('Derived data identity differs')
        expected_domain=self._ready['domain'] if request['parameters']['kind']=='preview' else self._ready['source_domain']
        if manifest.get('domain')!=expected_domain:raise ValueError('Manifest domain differs')
        folder=self.root/(str(request['revision']).zfill(9)+'-'+request['request_id'])
        receipt=read(folder/'host-verified.json')
        if receipt.get('publication_decision')!='published' or {k:v for k,v in receipt.items() if k!='publication_decision'}!=manifest:raise ValueError('Missing exact immutable Host publication receipt')
        if self.last_loaded==request.get('request_id'):return None
        for name in ('output','display'):
            d=manifest[name];file=Path(d['file']).resolve();suffix='output.npy' if name=='output' else 'display.png'
            folder=self.root/(str(request['revision']).zfill(9)+'-'+request['request_id'])
            if file!=folder/suffix or not file.is_relative_to(self.root) or type(d['bytes']) is not int or file.stat().st_size!=d['bytes'] or sha(file)!=d['sha256']:raise ValueError('Published member identity changed')
        capability=self._ready.get('final_output_contract')
        if capability is not None and capability!='linear-straight-exr-v1':raise ValueError('Unknown final output contract')
        if capability=='linear-straight-exr-v1' and request['parameters']['kind']=='final':
            semantics={'color_space':'scene_linear_Rec709','alpha_mode':'STRAIGHT','precision':'FLOAT32',
                       'compression':'ZIP','display_transform':'NONE','rgb_clamped':False,'source_role':'output'}
            if signature(manifest.get('linear_output_semantics'))!=signature(semantics) or manifest.get('linear_output_producer')!='HOST_FINAL_ENCODER':raise ValueError('Final EXR semantics/producer differs')
            members=manifest.get('members');d=manifest.get('linear_output')
            if type(members) is not list or type(d) is not dict or d.get('role')!='linear_straight_exr':raise ValueError('Final EXR descriptor missing')
            matching=[m for m in members if type(m) is dict and m.get('role')=='linear_straight_exr']
            if len(matching)!=1 or signature(matching[0])!=signature(d):raise ValueError('Final EXR member binding differs')
            expected_shape=[expected_domain['height'],expected_domain['width'],4]
            if signature(d.get('shape'))!=signature(expected_shape) or d.get('dtype')!='float32':raise ValueError('Final EXR shape/precision differs')
            file=Path(d['file']).resolve()
            if file!=folder/'output-linear-straight.exr' or not file.is_relative_to(self.root) or type(d.get('bytes')) is not int or file.stat().st_size!=d['bytes'] or sha(file)!=d['sha256']:raise ValueError('Published EXR identity changed')
        self.last_loaded=request['request_id'];self.last_reason='published';return manifest
    def stop(self):
        self._live();path=self.job/'session/stop.json'
        if not path.exists():publish(path,{'schema':'relight-session-stop-v1',**{k:self._ready[k] for k in IDENTITY_FIELDS}})
