"""No-bpy parent acceptance of a source-bound, independently reopened candidate."""
from pathlib import Path
import os
import math

from filesystem import FileGuard
from protocol import Failure, read_json
from toon_workflow_addon import contract, state_contract


def accept(job, request, data, stack, checkpoint):
    job = Path(job).resolve()
    checked = {}
    def document(row):
        if not isinstance(row, dict) or not {'file', 'sha256', 'bytes'}.issubset(row):
            raise Failure('VALIDATION_FAILED', 'Toon output descriptor is incomplete')
        binding = {name: row[name] for name in ('file', 'sha256', 'bytes')}
        path = Path(os.path.abspath(row['file']))
        if not path.is_absolute() or not path.is_relative_to(job) or path == job:
            raise Failure('VALIDATION_FAILED', 'Toon output escapes its owned job')
        for parent in [path, *path.parents]:
            if parent == job: break
            if parent.is_symlink() or (parent.stat().st_file_attributes & 0x400):
                raise Failure('CONFLICT', 'Toon output uses a reparse path')
        if path.resolve() != path or not path.is_file():
            raise Failure('VALIDATION_FAILED', 'Toon output is not a regular owned file')
        key = str(path).casefold()
        if key in checked:
            if checked[key] != binding: raise Failure('CONFLICT', 'Conflicting Toon output descriptors')
            return path
        guard = stack.enter_context(FileGuard(path))
        if guard.sha256(checkpoint) != row['sha256'] or path.stat().st_size != row['bytes']:
            raise Failure('CONFLICT', 'Toon output bytes changed before acceptance')
        checked[key] = binding
        return path
    if not isinstance(data, dict) or data.get('status') != 'passed':
        raise Failure('VALIDATION_FAILED', 'Toon worker did not return a passed result')
    rows = data.get('outputs')
    if not isinstance(rows, list) or not rows: raise Failure('VALIDATION_FAILED', 'Toon output inventory is absent')
    for row in rows: document(row)
    report_path = document(data['report'])
    if report_path != job / 'toon-report.json': raise Failure('VALIDATION_FAILED', 'Toon report filename differs')
    report = read_json(report_path); params = request['params']
    if report.get('operation') != request['command'].split('.')[1]:
        raise Failure('VALIDATION_FAILED', 'Toon report operation differs from requested command')
    expected_source = {'file': params['file'], 'sha256': params['expected_sha256']}
    if report.get('status') != 'passed' or report.get('source') != expected_source or report.get('implementation_sha256') != params['implementation_sha256']:
        raise Failure('VALIDATION_FAILED', 'Toon report source/implementation/status differs')
    if request['command'] == 'toon.inspect':
        if report.get('source_saved') is not False: raise Failure('VALIDATION_FAILED', 'Inspect source-save policy differs')
        inventory = {str(Path(row['file'])).casefold() for row in rows}
        if not set(checked).issubset(inventory): raise Failure('VALIDATION_FAILED', 'Inspect report is outside its output inventory')
        checkpoint()
        return data
    manifest = params['manifest']
    if report.get('toon_report_version') != '1.1' or report.get('manifest') != manifest or report.get('finalization', {}).get('status') != 'passed':
        raise Failure('VALIDATION_FAILED', 'Toon run manifest/finalization differs')
    candidate = document(report['candidate'])
    if candidate != job / 'toon-candidate.blend' or data.get('candidate') != str(candidate) or data.get('candidate_sha256') != report['candidate']['sha256']:
        raise Failure('VALIDATION_FAILED', 'Candidate output binding differs')
    state_path = document(report['state']); state = read_json(state_path)
    if state_path != job / 'toon-state.json' or data.get('state') != report['state'] or state.get('candidate') != report['candidate'] or state.get('source') != expected_source:
        raise Failure('VALIDATION_FAILED', 'Persistent Toon state binding differs')
    if state.get('embedded_state', {}).get('manifest') != manifest or state.get('expectation') != report.get('expectation'):
        raise Failure('VALIDATION_FAILED', 'Persistent Toon manifest/original expectation differs')
    if state.get('schema') != state_contract.state_schema(manifest):
        raise Failure('VALIDATION_FAILED', 'Persistent Toon state schema is unsupported')
    embedded = state['embedded_state']
    try:
        state_contract.validate_embedded_state(embedded, manifest)
    except contract.WorkflowError as exc:
        raise Failure('VALIDATION_FAILED', 'Persistent Toon frame state is invalid: ' + str(exc)) from exc
    if report.get('frames') != embedded['frames']:
        raise Failure('VALIDATION_FAILED', 'Reported frame summaries differ from persistent state')
    provenance = report.get('chain_provenance')
    if (type(provenance) is not list or any(type(row) is not dict or type(row.get('frame')) is not int for row in provenance)
            or [row['frame'] for row in provenance] != [row['frame'] for row in embedded['frames']]):
        raise Failure('VALIDATION_FAILED', 'Full chain provenance frame domain differs')
    for row, frame in zip(provenance, embedded['frames']):
        fingerprint = frame['chain_fingerprint']
        path = document(row)
        if (path != job / 'toon-chain-provenance' / ('frame-%04d.json' % frame['frame']) or
                row.get('schema') != fingerprint['schema'] or row.get('sha256') != fingerprint['sha256']):
            raise Failure('VALIDATION_FAILED', 'Full chain provenance output/fingerprint binding differs')
    expectation_path = document(report['expectation'])
    if expectation_path != job / 'toon-expectation.json': raise Failure('VALIDATION_FAILED', 'Original expectation path differs')
    verification = report.get('verification', {})
    if verification.get('status') != 'passed': raise Failure('VALIDATION_FAILED', 'Independent verification is absent')
    reopen_path = document(verification['reopen']); reopened = read_json(reopen_path)
    content_path = document(verification['content']); content = read_json(content_path)
    if (content_path != job / 'toon-content-reopen.json' or reopen_path != job / 'toon-reopen.json'
            or reopened.get('reopen_schema') != ('toon-reopen-combined-v2' if contract.is_placed(manifest) else 'toon-reopen-combined-v1')
            or reopened.get('content_receipt') != verification['content']):
        raise Failure('VALIDATION_FAILED', 'Independent content/image receipt binding differs')
    from toon_verifier import validate_content_receipt
    document(report['plan'])
    if Path(report['plan']['file']) != job / 'toon-plan.json':
        raise Failure('VALIDATION_FAILED', 'Original Toon plan path differs')
    try:
        validation_request = {
            'candidate': report['candidate'], 'state': report['state'], 'plan': report['plan'],
            'expectation': report['expectation'], 'images': report.get('images', []),
            'chain_provenance': provenance, 'implementation_sha256': params['implementation_sha256'],
            'execution_context': report.get('execution_context')}
        if contract.is_placed(manifest):
            validate_content_receipt(content, validation_request, manifest, embedded['frames'], embedded.get('shading_animation'), embedded['materials'])
        else:
            validate_content_receipt(content, validation_request, manifest)
    except (RuntimeError, KeyError, TypeError, ValueError) as exc:
        raise Failure('VALIDATION_FAILED', 'Independent content receipt is invalid: ' + str(exc)) from exc
    added = {'content_receipt', 'image_thread_observation'}
    if set(reopened) != set(content) | added:
        raise Failure('VALIDATION_FAILED', 'Independent combined receipt fields differ')
    for key, value in content.items():
        if key not in {'status', 'reopen_schema', 'image_checks'} and reopened.get(key) != value:
            raise Failure('VALIDATION_FAILED', 'Independent combined receipt changed content field: ' + key)
    image_context = reopened.get('image_thread_observation')
    if (type(image_context) is not dict or type(image_context.get('cpu_threads')) is not int
            or image_context['cpu_threads'] != report['execution_context']['cpu_threads']):
        raise Failure('VALIDATION_FAILED', 'Independent image process CPU context differs')
    if reopened.get('status') != 'passed' or reopened.get('candidate') != report['candidate'] or reopened.get('implementation_sha256') != params['implementation_sha256']:
        raise Failure('VALIDATION_FAILED', 'Independent reopen receipt does not bind this candidate')
    if reopened.get('source_protection', {}).get('status') != 'passed' or reopened.get('geometry') != 'passed' or reopened.get('materials') != 'passed' or reopened.get('render_settings', {}).get('status') != 'passed':
        raise Failure('VALIDATION_FAILED', 'Independent content/settings checks are incomplete')
    if (reopened.get('state_schema') != state_contract.state_schema(manifest) or reopened.get('chain_provenance') != provenance
            or reopened.get('chain_provenance_validation') != 'full_canonical_fingerprint'):
        raise Failure('VALIDATION_FAILED', 'Independent full chain provenance binding differs')
    required_frames = list(range(manifest['time']['start'], manifest['time']['end'] + 1))
    if sorted(row['frame'] for row in reopened.get('visibility', [])) != required_frames:
        raise Failure('VALIDATION_FAILED', 'Independent replay does not cover the full integer frame domain')
    if any(row.get('status') != 'passed' for row in reopened['visibility']):
        raise Failure('VALIDATION_FAILED', 'Independent visibility replay contains a failed frame')
    images = report.get('images', [])
    if [row['frame'] for row in images] != (required_frames if manifest['render']['enabled'] else []):
        raise Failure('VALIDATION_FAILED', 'Rendered frame inventory differs')
    for row in images:
        path = document(row)
        if path != job / 'toon-frames' / ('frame-%04d.png' % row['frame']):
            raise Failure('VALIDATION_FAILED', 'A rendered frame points to a different frame file')
        actual = row.get('decoded', {}).get('images', [])
        if len(actual) != 1 or [actual[0]['width'], actual[0]['height']] != manifest['render']['resolution'] or row.get('settings', {}).get('status') != 'passed':
            raise Failure('VALIDATION_FAILED', 'Decoded image/settings receipt differs')
    image_checks = reopened.get('image_checks', [])
    if len(image_checks) != (min(3, len(required_frames)) if manifest['render']['enabled'] else 0) or any(row.get('status') != 'passed' for row in image_checks):
        raise Failure('VALIDATION_FAILED', 'Independent rendered replay sample checks are incomplete')
    expected_replay = list(dict.fromkeys([required_frames[-1], required_frames[0],
                                         (required_frames[0] + required_frames[-1]) // 2])) if images else []
    if [row.get('frame') for row in image_checks] != expected_replay:
        raise Failure('VALIDATION_FAILED', 'Independent rendered replay frame identities differ')
    by_frame = {row['frame']: row for row in images}
    for row in image_checks:
        expected_reference = {k: by_frame[row['frame']][k] for k in ('file', 'sha256', 'bytes')}
        if row.get('reference') != expected_reference:
            raise Failure('VALIDATION_FAILED', 'Independent replay reference binding differs')
        document(row['reference'])
        cold_path = document(row['cold'])
        if cold_path != job / 'toon-cold-frames' / ('frame-%04d.png' % row['frame']):
            raise Failure('VALIDATION_FAILED', 'Independent replay output frame binding differs')
        tolerance = 2 / 255 if manifest['engine'] == 'BLENDER_EEVEE' else 1 / 255
        delta = row.get('max_decoded_delta')
        if type(delta) not in (int, float) or not math.isfinite(delta) or delta < 0 or delta > tolerance or row.get('tolerance') != tolerance:
            raise Failure('VALIDATION_FAILED', 'Independent replay measurement exceeds the fixed engine tolerance')
    # Every accepted diagnostic is bound by the returned inventory. A report
    # cannot introduce an unlisted output under a valid outer descriptor.
    inventory = {str(Path(row['file'])).casefold() for row in rows}
    if not set(checked).issubset(inventory):
        raise Failure('VALIDATION_FAILED', 'Toon receipt references an unlisted owned output')
    checkpoint()
    return {**data, 'candidate_accepted': True, 'parent_output_validation': 'passed'}
