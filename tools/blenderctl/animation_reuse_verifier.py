"""Independent read-only Blender reopen for a saved animation-reuse candidate.

This process never loads the add-on's clip_asset entry and never saves.  It
checks the pinned destination/library/resources/runtime, opens the candidate,
and reads real native Scene, targeted pre-existing source state, resources,
role metadata and saved Action state.
"""
from pathlib import Path
import sys
import traceback

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from protocol import atomic_json, digest, read_json

def verify(request_path):
    import bpy
    config = read_json(request_path)
    report = {
        'animation_reuse_verification_version': '1.0',
        'status': 'failed',
        'runtime': {'version': bpy.app.version_string,
                    'build': bpy.app.build_hash.decode()},
        'candidate': config.get('candidate'),
        'source': config.get('source'),
        'library': config.get('library'),
        'implementation_sha256': config.get('implementation_sha256'),
        'checks': {
            'source_sha256': 'pending', 'library_sha256': 'pending',
            'resources': 'pending', 'implementation': 'pending',
            'runtime': 'pending', 'candidate_sha256': 'pending',
            'open': 'pending', 'scene_settings': 'pending',
            'source_preserved': 'pending', 'resource_closure': 'pending',
            'role_witness': 'pending', 'animation': 'pending',
            'saves': 'none', 'render': 'not_run',
        },
    }
    try:
        from animation_identity import implementation_sha256
        if implementation_sha256() != config['implementation_sha256']:
            raise ValueError('Animation reuse implementation changed')
        report['checks']['implementation'] = 'passed'
        from animation_reuse import (_resource_closure, _resource_witness,
                                     _refresh_evaluation, _runtime_descriptor,
                                     _scene_settings, _sha_value,
                                     animation_fingerprint,
                                     compare_source_witness, source_witness,
                                     role_witness)
        runtime = _runtime_descriptor()
        report['runtime'] = runtime
        if runtime != config['runtime']:
            raise ValueError('Blender runtime changed')
        report['checks']['runtime'] = 'passed'
        source = config['source']
        report['source_sha256_before'] = digest(source['file'])
        if report['source_sha256_before'] != source['sha256']:
            raise ValueError('Destination source bytes changed')
        report['checks']['source_sha256'] = 'passed'
        library = config['library']
        report['library_sha256_before'] = digest(library['file'])
        if report['library_sha256_before'] != library['expected_sha256']:
            raise ValueError('Frozen clip library bytes changed')
        report['checks']['library_sha256'] = 'passed'
        resource_hashes = {}
        for row in config['resources']:
            actual = digest(row['file'])
            resource_hashes[row['file']] = actual
            if actual != row['expected_sha256']:
                raise ValueError('Animation resource bytes changed: ' + row['file'])
        report['resource_sha256'] = resource_hashes
        report['checks']['resources'] = 'passed'
        candidate = config['candidate']
        report['candidate_sha256_before'] = digest(candidate['file'])
        if report['candidate_sha256_before'] != candidate['sha256']:
            raise ValueError('Candidate bytes changed before reopen')
        report['checks']['candidate_sha256'] = 'passed'
        bpy.ops.wm.open_mainfile(filepath=candidate['file'], load_ui=False,
                                 use_scripts=False)
        if tuple(bpy.data.version[:2]) != tuple(bpy.app.version[:2]):
            raise ValueError('Candidate needs a Blender migration policy')
        report['checks']['open'] = 'passed'
        scene = bpy.data.scenes.get(config['scene_name'])
        if scene is None:
            raise ValueError('Saved destination Scene is missing: '
                             + str(config['scene_name']))
        if bpy.context.window is not None:
            bpy.context.window.scene = scene
            if len(scene.view_layers):
                bpy.context.window.view_layer = scene.view_layers[0]
        if bpy.context.scene != scene:
            raise ValueError('Saved active Scene is not the expected Scene')
        _refresh_evaluation(scene)
        settings = _scene_settings(scene)
        report['scene_settings'] = settings
        if _sha_value(settings, 'candidate Scene settings') != _sha_value(
                config['scene_settings'], 'expected Scene settings'):
            raise ValueError('Saved candidate changed Scene settings')
        report['checks']['scene_settings'] = 'passed'
        closure = _resource_closure({'resources': config['resources']})
        report['resource_closure'] = closure
        resource_witness = _resource_witness(closure)
        if _sha_value(resource_witness, 'candidate resources') != _sha_value(
                config['resource_witness'], 'expected resources'):
            raise ValueError('Saved candidate resource closure differs')
        report['resource_witness'] = resource_witness
        report['checks']['resource_closure'] = 'passed'
        current = source_witness()
        report['source_witness_scope'] = config['source_witness_scope']
        report['source_witness_sha256'] = _sha_value(
            current, 'candidate source witness')
        errors = compare_source_witness(current, config['source_witness'])
        if errors:
            raise ValueError('Saved candidate changed pre-existing source state: '
                             + ', '.join(errors[:16]))
        report['checks']['source_preserved'] = 'passed'
        if _sha_value(config['native_witness'], 'expected native witness') != \
                config['native_witness_sha256']:
            raise ValueError('Expected native witness hash and body differ')
        animation = animation_fingerprint()
        report['animation_fingerprint'] = animation
        if _sha_value(animation, 'candidate animation fingerprint') != _sha_value(
                config['animation_fingerprint'], 'expected animation fingerprint'):
            raise ValueError('Saved native animation fingerprint differs')
        native = role_witness(config['apply_summary'],
                              config['source_witness'], scene)
        report['native_witness'] = native
        report['native_witness_sha256'] = _sha_value(
            native, 'candidate native witness')
        if report['native_witness_sha256'] != config['native_witness_sha256']:
            raise ValueError('Saved native roles/actions/metadata witness differs')
        report['checks']['role_witness'] = 'passed'
        report['checks']['animation'] = 'passed'
        report['candidate_sha256_after'] = digest(candidate['file'])
        report['source_sha256_after'] = digest(source['file'])
        report['library_sha256_after'] = digest(library['file'])
        if report['candidate_sha256_after'] != candidate['sha256']:
            raise ValueError('Candidate bytes changed during verification')
        if report['source_sha256_after'] != source['sha256']:
            raise ValueError('Destination source changed during verification')
        if report['library_sha256_after'] != library['expected_sha256']:
            raise ValueError('Frozen clip library changed during verification')
        report.update(status='passed',
                      state='candidate_native_clip_and_source_state_match',
                      visual='not_reviewed',
                      acceptance=('technical_read_only; deformed-mesh '
                                  'comparison and visual recording remain external'))
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
