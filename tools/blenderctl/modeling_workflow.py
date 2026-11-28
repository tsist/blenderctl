"""Public Blender worker adapter: native candidate plus independent reader."""
from functools import partial
import os
from pathlib import Path
import subprocess

import bpy
from protocol import Failure, atomic_json, digest, read_json
from modeling_workflow_contract import normalize_params, read_studio_export


def import_studio_fbx(path, *, binding):
    from exchange import import_file, snapshot
    import_file(path, 'FBX', studio_static=True)
    scene = bpy.context.scene
    if (bpy.data.actions or bpy.data.libraries or any(
            obj.type not in ('MESH', 'EMPTY') or obj.animation_data or obj.vertex_groups
            or (obj.type == 'MESH' and obj.data.shape_keys) for obj in scene.objects)):
        raise Failure('UNSUPPORTED', 'Studio static FBX requires local, unrigged and unanimated mesh objects')
    bpy.context.view_layer.update()
    snapshot(scene.frame_current)
    return binding


def run(params, job):
    from modeling_workflow_addon import core, contract
    params = normalize_params(params)
    job = Path(job)
    manifest = params['manifest']
    if manifest['operation'] != 'import_local':
        raise Failure('INVALID_REQUEST', 'Cloud modeling operations belong to the host job route')
    source = manifest['input']
    if digest(source['file']) != source['expected_sha256']:
        raise Failure('CONFLICT', 'Modeling local source SHA changed')
    report = {'status': 'running', 'operation': 'import_local', 'input': source,
              'implementation_sha256': params['implementation_sha256']}
    try:
        native_import = None
        studio_binding = None
        if 'studio_export' in params:
            studio_binding = read_studio_export(params['studio_export'], source)
            if Path(source['file']).suffix.lower() == '.fbx':
                native_import = partial(import_studio_fbx, binding=studio_binding)
        report = core.create_candidate(manifest, str(job), native_import=native_import, studio_binding=studio_binding)
        report['implementation_sha256'] = params['implementation_sha256']
        report['operation'] = 'import_local'
        atomic_json(job / 'modeling-report.json', report)
        candidate = {'file': report['candidate'], 'sha256': report['candidate_sha256']}
        if Path(candidate['file']).resolve().parent != job.resolve() or digest(candidate['file']) != candidate['sha256']:
            raise Failure('CONFLICT', 'Candidate is not an unchanged owned job file')
        cfg = {'candidate': candidate, 'content': report['content'], 'previews': report.get('previews', []),
               'implementation_sha256': params['implementation_sha256'], 'output': str(job / 'modeling-reopen.json')}
        if 'studio_fbx' in report:
            cfg.update(studio_fbx=report['studio_fbx'], native_normals=report['native_normals'])
        atomic_json(job / 'modeling-verification-request.json', cfg)
        args = [bpy.app.binary_path, '--background', '--factory-startup', '--disable-autoexec',
                '--offline-mode', '--threads', '2', '--python-exit-code', '23', '--python',
                str(Path(__file__).with_name('modeling_verifier.py')), '--', str(job / 'modeling-verification-request.json')]
        with (job / 'modeling-reopen.log').open('wb') as log:
            proc = subprocess.run(args, stdout=log, stderr=subprocess.STDOUT, timeout=120,
                                  creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        observed = read_json(cfg['output']) if Path(cfg['output']).is_file() else {'status': 'failed', 'error': 'Missing reader report'}
        if proc.returncode or observed['status'] != 'passed':
            raise Failure('VALIDATION_FAILED', 'Fresh-process modeling reopen failed: ' + str(observed.get('error')))
        if digest(source['file']) != source['expected_sha256'] or digest(candidate['file']) != candidate['sha256']:
            raise Failure('CONFLICT', 'Modeling input or candidate changed during verification')
        report.update(status='passed', fresh_process_reopen='pass', verification={k: observed[k] for k in ('status', 'process', 'blender', 'build')}, source_unchanged=True)
        atomic_json(job / 'modeling-report.json', report)
        return report
    except contract.ContractError as exc:
        report.update(status='failed', error={'code': 'INVALID_REQUEST', 'message': str(exc)})
        atomic_json(job / 'modeling-report.json', report)
        raise Failure('INVALID_REQUEST', str(exc)) from exc
    except ValueError as exc:
        report.update(status='failed', error={'code': 'VALIDATION_FAILED', 'message': str(exc)})
        atomic_json(job / 'modeling-report.json', report)
        raise Failure('VALIDATION_FAILED', str(exc)) from exc
    except Exception as exc:
        report.update(status='failed', error={'code': getattr(exc, 'code', 'WORKER_FAILED'), 'message': str(exc)})
        atomic_json(job / 'modeling-report.json', report)
        raise
