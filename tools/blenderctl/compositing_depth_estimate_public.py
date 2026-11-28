"""Manifest-rooted local RGB producer. No install/download/scene operation."""
from contextlib import ExitStack
from pathlib import Path
import hashlib
import json
import math
import os
import sys
import time
import uuid

from compositing_depth_estimate_contract import (DepthError, normalize_params, params_schema,
                                                signature, input_descriptors, expected_tensor_shape, SOURCE)
from compositing_depth_contract import normalize_params as normalize_depth, input_descriptors as depth_inputs
from compositing_depth_public import (BoundedInputs, read, row, descriptor, key, publish,
                                      code_signature, runtime_identity)
from compositing_encode import Guard
from compositing_encode_contract import EncodeError
from processes import ProcessTree

PROFILE_SHA = '76b2f1c461291074f571873101a0465cb81ab21f07dafad1622d9bb72d4a0f8d'
MANIFEST_SHA = 'cd58055c3478d19ddda6cf8212f3ed350d1d68b1ae434e7ed0ef8b9c055da71a'
HOST_NAMES = ('cli.py','protocol.py','runner.py','compositing_interface.py','execution_contract.py',
              'compositing_depth_estimate_public.py','compositing_depth_estimate_contract.py',
              'compositing_depth_contract.py','compositing_depth_public.py','compositing_encode.py',
              'compositing_encode_contract.py','processes.py','bootstrap.py')


def need(condition, message, code='CONFLICT'):
    if not condition: raise DepthError(code, message)


def code_rows():
    base = Path(__file__).parent
    rows = [row(base/name, 'host:'+name) for name in HOST_NAMES]
    for role, path in (('depth_estimate_contract', SOURCE), ('depth_contract', SOURCE.with_name('depth_contract.py')),
                       ('depth_estimate_worker', base/'compositing_depth_estimate_worker.py'),
                       ('depth_io', base/'compositing_depth_io.py')):
        rows.append(row(path, role))
    return rows


def launch_identity():
    codes = code_rows()
    return {'profile': 'public-dav2-estimate-v1', 'files': codes, 'sha256': code_signature(codes),
            'runtime_profile_sha256': PROFILE_SHA}


def describe(schema=False, param=None):
    from protocol import VERSION
    if schema: return {'operation': 'depth-estimate', 'schema': params_schema()}
    if param:
        p = params_schema()['properties']; need(param in p, 'Unknown estimate parameter', 'INVALID_REQUEST')
        return {'operation': 'depth-estimate', 'param': param, 'schema': p[param]}
    return {'cli_version': VERSION, 'operation': 'compositing.depth-estimate', 'support_level': 'experimental',
            'model': 'fixed DAV2 Large relative, CC-BY-NC-4.0', 'execution': 'existing isolated CUDA0 FP32 local worker; no fallback',
            'RGB': 'PNG8 RGB/RGBA only, require all opaque Alpha; explicit display sRGB declaration',
            'source_size': '64..2048, native lower_bound518/multiple14 with explicit tensor budget',
            'next': 'returned depth-prepare-params.json -> separate public depth-prepare -> depth-inspect',
            'cold_admission': 'succeeded parent/actual report/raw/recipe authority, no reinference or old guard',
            'profile_sha256': PROFILE_SHA, 'metric_depth': 'not_proven', 'confidence': 'not_proven', 'lighting_quality': 'not_proven',
            'limits': 'explicit pixel/tensor/time only; RAM/VRAM hard limits and DAG not qualified'}


class ModelInputs(BoundedInputs):
    def __init__(self, stack, checkpoint):
        super().__init__(stack, checkpoint)
        self.large_model = None
    def checked_digest(self, path):
        size = Path(path).stat().st_size
        max_size = 1341395338 if key(path) == self.large_model else 536870912
        need(size <= max_size, 'File exceeds model profile byte budget', 'RESOURCE_LIMIT')
        h = hashlib.sha256()
        with Path(path).open('rb') as stream:
            for block in iter(lambda: stream.read(4194304), b''):
                self.checkpoint(); h.update(block)
        return h.hexdigest()


def protect_read(inputs, d): return read(inputs.protect(d))


def profile_admission(params, inputs):
    need(params['runtime_profile']['expected_sha256'] == PROFILE_SHA, 'Unqualified model/runtime profile', 'CONFLICT')
    profile = protect_read(inputs, params['runtime_profile'])
    need(profile.get('schema') == 'dav2-local-qualified-profile-v1' and profile.get('trusted_installed_manifest',{}).get('expected_sha256') == MANIFEST_SHA,
         'Fixed trusted expected manifest absent')
    for name in ('root_payload_review','payload_report','payload_terminal','source_receipt','model_receipt'):
        protect_read(inputs, profile[name])
    for d in profile['receipts'].values(): inputs.protect(d)
    manifest = protect_read(inputs, profile['trusted_installed_manifest'])
    need(manifest.get('schema') == 'dav2-wheel-installed-payload-v2' and len(manifest.get('files',[])) == 16221 and
         len(manifest.get('packages',[])) == 14, 'Prior trusted wheel payload scope differs')
    venv = Path(profile['venv_root'])
    expected = {}
    t = time.monotonic()
    for r in manifest['files']:
        path = Path(r['file'])
        need(path.is_relative_to(venv) and key(path) not in expected and type(r['bytes']) is int and path.stat().st_size == r['bytes'],
             'Expected installed member path/size/uniqueness differs')
        expected[key(path)] = r
        inputs.protect(descriptor(r))
    site = venv/'Lib/site-packages'
    namespaces = {Path(r['file']).relative_to(site).parts[0] for r in manifest['files'] if Path(r['file']).is_relative_to(site)}
    def namespace_inventory():
        actual = set()
        for ns in sorted(namespaces):
            root = site/ns
            paths = [root] if root.is_file() else root.rglob('*')
            for p in paths:
                if p.is_file() and p.suffix.lower() in ('.py','.pyd','.dll'):
                    actual.add(key(p))
        return actual
    qualified_code = {k for k,r in expected.items() if Path(r['file']).suffix.lower() in ('.py','.pyd','.dll')}
    need(namespace_inventory() == qualified_code, 'Extra/missing importable code within approved namespaces', 'CONFLICT')
    source = protect_read(inputs, profile['source_receipt'])
    need(source['commit'] == profile['source_commit'] == 'a561b849ebae10a6f5ef49e26c83cbbcd36c71bf', 'Official fixed source commit differs')
    source_root = Path(profile['source_root']); source_files = []
    for r in source['files']:
        path = source_root/r['path']
        need(path.is_relative_to(source_root), 'Source member leaves official root')
        inputs.protect({'file': str(path), 'expected_sha256': r['sha256']})
        source_files.append(key(path))
    actual_source = {key(p) for p in (source_root/'depth_anything_v2').rglob('*.py')}
    need(actual_source == {x for x in source_files if Path(x).suffix == '.py'}, 'Official source namespace differs', 'CONFLICT')
    inputs.large_model = key(profile['model']['file'])
    need(Path(profile['model']['file']).stat().st_size == profile['model']['bytes'] == 1341395338 and
         profile['model']['expected_sha256'] == 'a7ea19fa0ed99244e67b624c72b8580b7e9553043245905be58796a608eb9345',
         'Fixed published weight identity differs')
    inputs.protect({'file': profile['model']['file'], 'expected_sha256': profile['model']['expected_sha256']})
    return profile, {'files':16221, 'packages':14, 'current_payload_sha_rechecked':True,
                     'wheel_archive_reconstruction':False, 'prior_manifest_sha256':MANIFEST_SHA,
                     'preimport_profile_seconds':time.monotonic()-t}, namespace_inventory, qualified_code


def freeze(codes, target, inputs):
    source = target/'source'; source.mkdir(exist_ok=False); result = []
    for r in codes:
        inputs.protect(descriptor(r))
        path = source/(r['role'].replace(':','-')+('' if r['role'].endswith('.py') else '.py'))
        with path.open('xb') as stream: stream.write(Path(r['file']).read_bytes())
        snap = row(path, r['role']); need(snap['sha256'] == r['sha256'], 'Frozen model source differs')
        inputs.protect(descriptor(snap)); snap['source_file'] = r['file']; result.append(snap)
    return result


def owned_worker(job, binding, checkpoint):
    target = job/'model'; gate = target/'gate'; cache = target/'empty-pycache'
    gate.mkdir(exist_ok=False); cache.mkdir(exist_ok=False)
    python = binding['profile']['python']['file']
    bootstrap = next(r['file'] for r in binding['codes'] if r['role'] == 'host:bootstrap.py')
    worker = next(r['file'] for r in binding['codes'] if r['role'] == 'depth_estimate_worker')
    flags = ['-I','-S','-B','-X','pycache_prefix='+str(cache)]
    command = [python,*flags,bootstrap,str(gate),python,*flags,worker,str(job)]
    lifetime = {'ownership':'new gated owned ProcessTree', 'worker_timeout_seconds':binding['worker_timeout_seconds'],
                'no_site_startup':True, 'cancel_grace_seconds':2, 'partial_eligible':False}
    tree = None; first = None; cancelled = None; secondary_errors = []; started = time.monotonic()
    def secondary(e, stage):
        nonlocal first
        secondary_errors.append({'stage':stage,'type':type(e).__name__,'message':str(e)})
        if first is None: first = e
    try:
        with (target/'stdout.log').open('x',encoding='utf-8') as stdout, (target/'stderr.log').open('x',encoding='utf-8') as stderr:
            tree = ProcessTree(command,job_name='blenderctl-dav2-'+uuid.uuid4().hex,stdin=None,stdout=stdout,stderr=stderr,env=os.environ.copy(),shell=False)
            lifetime['bootstrap_pid'] = tree.process.pid; publish(target/'owned-process.json', lifetime)
            (gate/'worker.gate').touch(exist_ok=False)
            while tree.process.poll() is None:
                if first is None:
                    try:
                        checkpoint(); need(time.monotonic()-started <= binding['worker_timeout_seconds'], 'Owned DAV2 worker timeout', 'TIMEOUT')
                    except Exception as e:
                        first = e; cancelled = time.monotonic(); (job/'model.cancel.request').touch(exist_ok=True)
                elif time.monotonic()-cancelled >= 2:
                    tree.terminate(); lifetime['terminated_owned_tree'] = True; break
                time.sleep(.1)
            lifetime['returncode'] = tree.process.poll()
            if first is None and lifetime['returncode'] in (6,7): first = DepthError('TIMEOUT' if lifetime['returncode']==6 else 'CANCELLED','Owned model child terminal code')
            try: lifetime['usage'] = tree.usage()
            except Exception as e: secondary(e,'usage')
    except Exception as e: secondary(e,'execution/termination')
    finally:
        if tree is not None:
            try:
                if tree.process.poll() is None: tree.terminate()
            except Exception as e: secondary(e,'cleanup termination')
            finally:
                try: tree.close()
                except Exception as e: secondary(e,'JobObject close')
        lifetime['elapsed_seconds'] = time.monotonic()-started; lifetime['secondary_errors'] = secondary_errors
        if first: lifetime['first_error'] = {'code':getattr(first,'code','WORKER_FAILED'),'message':str(first)}
        try: publish(job/'model-lifecycle.json', lifetime)
        except Exception as e:
            secondary(e,'diagnostic publication')
            try: publish(job/'model-lifecycle-secondary.json',lifetime)
            except Exception: pass
    if first: raise first
    path = target/'worker-report.json'; need(path.is_file(),'Model child missing report','WORKER_FAILED')
    report = read(path)
    e = report.get('error',{}); code = e.get('code','WORKER_FAILED')
    if code not in ('VALIDATION_FAILED','CONFLICT','TIMEOUT','CANCELLED','IO_ERROR','UNSUPPORTED','RESOURCE_LIMIT','WORKER_FAILED'): code='WORKER_FAILED'
    need(lifetime['returncode'] == 0 and report.get('status')=='passed',e.get('message','Owned model worker failed'),code)
    need(report['request_signature']==binding['request_signature'],'Worker request signature differs')
    checkpoint(); return report,lifetime


def raw_stage_key(params, identity):
    return signature({'rgb':params['rgb'],'rgb_encoding':params['rgb_encoding'],'alpha_policy':params['alpha_policy'],
                      'domain':params['domain'],'profile':params['runtime_profile'],'expected_tensor':expected_tensor_shape(params['domain']),
                      'model_worker_sha256':next(r['sha256'] for r in identity['files'] if r['role']=='depth_estimate_worker')})


def run(params, job, cancel_parent=None, expected_identity=None):
    params = normalize_params(params); job = Path(job).resolve(); started = time.monotonic()
    try:
        target = job/'model'; need(job.is_dir() and not target.exists(),'Fresh public model job required'); target.mkdir(exist_ok=False)
        launch = read(job/'launch.json'); identity = launch_identity()
        need(identity == (expected_identity or launch.get('estimate_identity')) and
             signature(params) == launch.get('estimate_request_signature'),'Queued model code/request changed')
        def checkpoint():
            if cancel_parent: cancel_parent()
            need(not (job/'cancel.request').exists(),'Model public cancellation','CANCELLED')
        with ExitStack() as stack:
            inputs = ModelInputs(stack, checkpoint)
            for name in ('request.json','launch.json'): inputs.protect(descriptor(row(job/name)))
            for r in identity['files']: inputs.protect(descriptor(r))
            for d in input_descriptors(params): inputs.protect(d)
            runtime = runtime_identity(checkpoint)
            for r in runtime['files']: inputs.protect(descriptor(r))
            import numpy as np
            import compositing_depth_io as io
            rgb, metadata = io.read_rgb(params['rgb'], params['domain'])
            need(rgb.shape[2]==3 or bool((rgb[:,:,3]==255).all()),'Nonopaque Alpha refused','VALIDATION_FAILED')
            decoded = hashlib.sha256(np.ascontiguousarray(rgb[:,:,:3]).tobytes()).hexdigest()
            derived = params['derived']
            if derived['validity']['kind']=='explicit_mask':
                valid,_ = io.read_scalar(derived['validity']['mask'],params['domain'],'validity')
                need(bool(valid.any()),'Empty derived validity','VALIDATION_FAILED')
            for edit in derived['edits']: io.read_scalar(edit['mask'],params['domain'],'edit')
            exclusion = derived['normal'].get('boundary_exclusion')
            if exclusion is not None: io.read_scalar(exclusion,params['domain'],'boundary')
            profile, verification, inventory, expected_code = profile_admission(params, inputs)
            for d in params['resources']: need(inputs.files.get(key(d['file']),{}).get('sha256')==d['expected_sha256'], 'Unused declared model resource')
            codes = freeze(identity['files'],target,inputs)
            inventory_rows = list(inputs.files.values())
            publish(target/'guarded-inputs.json', {'schema':'public-dav2-live-input-inventory-v1','files':inventory_rows,
                                                  'trusted_expected_manifest':profile['trusted_installed_manifest'],'verification':verification,
                                                  'scope':'expected bytes verified and live deny-write/delete held; Python base/OS/driver excluded'})
            binding = {'schema':'public-dav2-worker-binding-v1','params':params,'profile':profile,
                       'request_signature':signature(params),'codes':codes,'expected_tensor_shape':expected_tensor_shape(params['domain']),
                       'decoded_RGB_sha256':decoded,'worker_timeout_seconds':params['budget']['timeout_seconds'],
                       'guarded_input_inventory':row(target/'guarded-inputs.json')}
            inputs.protect(descriptor(binding['guarded_input_inventory']))
            publish(target/'binding.json',binding); inputs.protect(descriptor(row(target/'binding.json')))
            pre_worker_seconds = time.monotonic()-started
            report,lifetime = owned_worker(job,binding,checkpoint)
            for r in (report['raw'],report['display']):
                need(Path(r['file']).parent==target and Path(r['file']).stat().st_size==r['bytes'],'Owned model member differs')
                inputs.protect(descriptor(r))
            # Independent numeric reader in bundled Python; does not import Torch or call old guard.
            raw = io.read_npy(report['raw']['file'],(params['domain']['height'],params['domain']['width']),('float32',))
            need(bool(np.isfinite(raw).all()) and raw.flags.c_contiguous,'Independent model raw numeric readback failed','VALIDATION_FAILED')
            independent = {'min':float(raw.min()),'max':float(raw.max()),'mean':float(raw.mean()),
                           'finite_count':int(np.isfinite(raw).sum()),'negative_count':int((raw<0).sum())}
            for k,v in independent.items(): need(report['statistics'][k]==v,'Independent raw statistics differ')
            # Decode the diagnostic too, preserving its display-only role.
            io.read_png(report['display']['file'],params['domain'],1)
            checkpoint(); need(inventory()==expected_code,'Approved namespace changed during inference')
            post = time.monotonic(); inputs.recheck(); post_seconds=time.monotonic()-post
            frozen = {'schema':'public-dav2-model-report-v1','status':'passed','parent_job_id':job.name,
                      'request_signature':signature(params),'request':{'params':params},
                      'parent_documents':{n:row(job/n) for n in ('request.json','launch.json')},
                      'producer_identity':identity,'frozen_sources':codes,'runtime_profile':params['runtime_profile'],
                      'guarded_input_inventory':row(target/'guarded-inputs.json'),'worker_report':row(target/'worker-report.json'),
                      'raw':report['raw'],'display':report['display'],'RGB':params['rgb'],'RGB_decode_metadata':metadata,
                      'domain':params['domain'],'preprocess':report['preprocess'],'runtime':report['runtime'],
                      'statistics':report['statistics'],'stage_key':raw_stage_key(params,identity),
                      'source_provenance':params['input_provenance'],'profile_verification':verification,
                      'timing_seconds':{'pre_worker':pre_worker_seconds,'owned_worker':lifetime['elapsed_seconds'],
                                        'post_guard':post_seconds,'host_total_before_publication':time.monotonic()-started,
                                        'worker_stages':report['timing_seconds']},
                      'declared_near_order':profile['near_order'],'actual_per_image_geometry_quality':'not_proven',
                      'source_RGB_is_albedo':False,'finite_is_confidence':False,'metric_depth_proven':False,
                      'calibration_proven':False,'consumer_eligible':False,'public_parent_success_required':True}
            publish(job/'model-report.json',frozen)
            producer = {'file':str(job/'model-report.json'),'expected_sha256':row(job/'model-report.json')['sha256']}
            prepare = {'depth_prepare_version':'1.0','rgb':params['rgb'],'rgb_encoding':params['rgb_encoding'],
                       'depth':{'file':report['raw']['file'],'expected_sha256':report['raw']['sha256'],'format':'NPY','channel':None},
                       'domain':params['domain'],'semantics':{'measure':'relative_score','near_order':'larger_nearer','units':'unitless'},
                       'provenance':{'kind':'verified_public_dav2','producer':producer},**derived,'resources':[]}
            # Direct descriptors include only this recipe inputs; model authority is expanded read-only later.
            def collect(v):
                if type(v) is dict:
                    if 'file' in v and 'expected_sha256' in v: return [descriptor(v)]
                    return [r for k,x in v.items() if k!='resources' for r in collect(x)]
                if type(v) is list: return [r for x in v for r in collect(x)]
                return []
            prepare['resources'] = list({key(r['file']):r for r in collect(prepare)}.values())
            prepare = normalize_depth(prepare,'depth-prepare')
            publish(job/'depth-prepare-params.json',prepare)
            checkpoint()
            return {'status':'passed','operation':'depth-estimate','request_signature':signature(params),'producer_identity':identity,
                    'model_report':row(job/'model-report.json'),'raw':report['raw'],'prepare_params':row(job/'depth-prepare-params.json'),
                    'stage_key':frozen['stage_key'],'preprocess':frozen['preprocess'],'timing_seconds':frozen['timing_seconds'],
                    'consumer_eligible':False,'qualification':'actual numeric RGB inference; geometry/lighting not proven'}
    except EncodeError as e:
        error = DepthError(e.code,str(e)); failure(job,error); raise error from e
    except Exception as e:
        # Code-loader aliases have distinct exception classes. Keep the public
        # code while raising the bridge class expected by the runner.
        code = getattr(e,'code','IO_ERROR' if isinstance(e,OSError) else 'WORKER_FAILED')
        if code=='INPUT_CHANGED': code='CONFLICT'
        elif code not in ('INVALID_REQUEST','NOT_FOUND','CONFLICT','WORKER_FAILED','TIMEOUT','CANCELLED','VALIDATION_FAILED','IO_ERROR','UNSUPPORTED','RESOURCE_LIMIT'):
            code='VALIDATION_FAILED' if hasattr(e,'code') else 'WORKER_FAILED'
        converted = DepthError(code,str(e)); failure(job,converted)
        raise converted from e


def failure(job, error):
    if job.is_dir() and not (job/'model-report.json').exists():
        try: publish(job/'model-report.json',{'schema':'public-dav2-model-report-v1','status':'failed','consumer_eligible':False,
                                            'error':{'code':getattr(error,'code','WORKER_FAILED'),'message':str(error)},'partial_eligible':False})
        except Exception: pass
