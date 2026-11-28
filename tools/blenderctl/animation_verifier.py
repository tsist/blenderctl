"""Independent read-only Blender reopen for a saved animation candidate."""
import hashlib
import json
from pathlib import Path
import sys
import traceback

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from protocol import atomic_json, digest, read_json


def _plain(value):
    return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))


def verify(request_path):
    import bpy
    config = read_json(request_path)
    report = {
        'animation_verification_version': '1.0',
        'status': 'failed',
        'runtime': {'version': bpy.app.version_string,
                    'build': bpy.app.build_hash.decode()},
        'candidate': config.get('candidate'),
        'source': config.get('source'),
        'implementation_sha256': config.get('implementation_sha256'),
        'checks': {'candidate_sha256': 'pending', 'source_sha256': 'pending',
                   'implementation': 'pending', 'runtime': 'pending',
                   'open': 'pending', 'context': 'pending',
                   'resource_closure': 'pending', 'inspect_read_only': 'pending',
                   'record_evidence': 'pending',
                   'animation_fingerprint': 'pending', 'saves': 'none'},
    }
    try:
        from animation_identity import implementation_sha256
        if implementation_sha256() != config['implementation_sha256']:
            raise ValueError('Animation implementation changed')
        report['checks']['implementation'] = 'passed'
        runtime = {'version': bpy.app.version_string,
                   'build': bpy.app.build_hash.decode(),
                   'binary_sha256': digest(bpy.app.binary_path)}
        report['runtime'] = runtime
        if runtime != config['runtime']:
            raise ValueError('Blender runtime changed')
        report['checks']['runtime'] = 'passed'
        candidate = config['candidate']
        if digest(candidate['file']) != candidate['sha256']:
            raise ValueError('Candidate bytes changed')
        report['checks']['candidate_sha256'] = 'passed'
        source = config['source']
        if digest(source['file']) != source['sha256']:
            raise ValueError('Source bytes changed')
        report['checks']['source_sha256'] = 'passed'
        bpy.ops.wm.open_mainfile(filepath=candidate['file'], load_ui=False,
                                 use_scripts=False)
        if tuple(bpy.data.version[:2]) != tuple(bpy.app.version[:2]):
            raise ValueError('Candidate needs a Blender migration policy')
        report['checks']['open'] = 'passed'
        from animation_workflow import (_canonical_sha, _resource_closure,
                                        _validate_context, animation_fingerprint,
                                        clip_record_evidence, state_fingerprint)
        _validate_context(config['request'])
        report['checks']['context'] = 'passed'
        closure = _resource_closure({'resources': config['resources']})
        if _canonical_sha(closure) != config['resource_closure_sha256']:
            raise ValueError('Saved candidate resource closure differs')
        report['resource_closure'] = closure
        report['checks']['resource_closure'] = 'passed'
        from animation_curves_workflow_addon import clip_event
        before = state_fingerprint()
        inspection = _plain(clip_event.inspect(bpy.context, config['request']))
        if not isinstance(inspection, dict):
            raise ValueError('clip_event.inspect result is not an object')
        after = state_fingerprint()
        if after['sha256'] != before['sha256']:
            raise ValueError('clip_event.inspect changed saved actions or records')
        report['inspection'] = inspection
        report['inspection_sha256'] = hashlib.sha256(
            json.dumps(inspection, sort_keys=True, separators=(',', ':'),
                       ensure_ascii=False).encode('utf-8')
        ).hexdigest()
        report['checks']['inspect_read_only'] = 'passed'
        evidence = clip_record_evidence(
            config['request'], config['expected_contact'])
        report['record_evidence'] = evidence
        if _canonical_sha(evidence) != config['record_evidence_sha256']:
            raise ValueError('Saved clip record/actual key evidence differs')
        report['checks']['record_evidence'] = 'passed'
        animation = animation_fingerprint()
        report['animation_fingerprint'] = animation
        if animation != config['animation_fingerprint']:
            raise ValueError('Saved native animation fingerprint differs')
        report['checks']['animation_fingerprint'] = 'passed'
        report['source_sha256_after'] = digest(source['file'])
        if report['source_sha256_after'] != source['sha256']:
            raise ValueError('Source changed during independent verification')
        report.update(status='passed',
                      state='candidate_record_and_animation_match',
                      visual='not_reviewed')
    except Exception as exc:
        report['error'] = str(exc)
        report['traceback'] = traceback.format_exc()
    atomic_json(config['output'], report)
    return 0 if report['status'] == 'passed' else 8


if __name__ == '__main__':
    arguments = sys.argv[sys.argv.index('--') + 1:]
    if len(arguments) != 1:
        raise SystemExit(2)
    raise SystemExit(verify(arguments[0]))
