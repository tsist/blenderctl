"""Strict Host binding for the one shared video contract and immutable inputs."""
import copy
import hashlib
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from protocol import Failure, read_json
from video_identity import implementation_sha256, runtime_documents
from video_editing_workflow_addon import contract as c


def _call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except c.WorkflowError as exc:
        code = 'RESOURCE_LIMIT' if exc.code == 'OUTPUT_BUDGET_EXCEEDED' else exc.code
        raise Failure(code, str(exc)) from exc


def descriptor(value):
    if not isinstance(value, dict) or set(value) != {'file', 'expected_sha256'}:
        raise Failure('INVALID_REQUEST', 'Expected exact file/expected_sha256 descriptor')
    if not isinstance(value['file'], str) or not Path(value['file']).is_absolute():
        raise Failure('INVALID_REQUEST', 'Input paths must be absolute')
    if not isinstance(value['expected_sha256'], str) or not re.fullmatch('[0-9a-f]{64}', value['expected_sha256']):
        raise Failure('INVALID_REQUEST', 'SHA256 must be 64 lower-case hex characters')
    return {**value, 'file': str(Path(value['file']).resolve())}


def _resources(rows, *, limit=100000, reject_duplicates=False):
    if not isinstance(rows, list) or len(rows) > limit:
        raise Failure('INVALID_REQUEST', 'resources requires 0..100000 explicit descriptors')
    result = {}
    for row in rows:
        row = descriptor(row)
        key = row['file'].casefold()
        if reject_duplicates and key in result:
            raise Failure('INVALID_REQUEST', 'Duplicate resource path: ' + row['file'])
        if key in result and result[key]['expected_sha256'] != row['expected_sha256']:
            raise Failure('CONFLICT', 'Conflicting resource hashes: ' + row['file'])
        result[key] = row
    return list(result.values())


def _base(value, required, optional):
    if not isinstance(value, dict) or not required <= set(value) or set(value) - required - optional:
        raise Failure('INVALID_REQUEST', 'Video params contain missing or unknown fields')
    out = copy.deepcopy(value)
    source = descriptor({key: out[key] for key in ('file', 'expected_sha256')})
    out.update(source)
    out['resources'] = _resources(out['resources'], reject_duplicates=True)
    current = implementation_sha256()
    supplied = out.get('implementation_sha256')
    if supplied is not None and supplied != current:
        raise Failure('CONFLICT', 'Video implementation changed')
    out['implementation_sha256'] = current
    return out


def normalize_params(value):
    out = _base(value, {'file', 'expected_sha256', 'resources', 'manifest'},
                {'output', 'resume', 'implementation_sha256', 'media_probe'})
    out = _call(c.normalize_params, out)
    if out.get('output', {}).get('color_mode') == 'HDR10_PQ':
        policy = out['output']['hdr']
        if policy['mapping'] != 'STANDARD_100' or policy['alpha'] != 'REQUIRE_OPAQUE':
            raise Failure('UNSUPPORTED', 'Only explicit Standard100/opaque technical HDR policy is qualified')
    if 'resume' in out:
        out['resume'] = descriptor(out['resume'])
    return out


def normalize_inspect_params(value):
    out = _base(value, {'file', 'expected_sha256', 'resources', 'scene'},
                {'workflow_id', 'implementation_sha256'})
    return _call(c.normalize_inspect_params, out)


def normalize_command(command, params):
    if command == 'video.run':
        return normalize_params(params)
    if command == 'video.inspect':
        return normalize_inspect_params(params)
    raise Failure('INVALID_REQUEST', 'Unknown video command')


def signature(params):
    body = {key: params[key] for key in ('file', 'expected_sha256', 'resources', 'manifest', 'output', 'implementation_sha256', 'media_probe') if key in params}
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _read_descriptor(desc):
    from filesystem import FileGuard
    with FileGuard(desc['file']) as guard:
        if guard.sha256() != desc['expected_sha256']:
            raise Failure('CONFLICT', 'Video anchored descriptor changed')
        return read_json(desc['file'])


def anchor_documents(params, command=None):
    return [descriptor(params['resume'])] if params.get('resume') else []


def _declared(value):
    if isinstance(value, dict):
        if 'file' in value and 'expected_sha256' in value:
            yield descriptor({key: value[key] for key in ('file', 'expected_sha256')})
        for key, child in value.items():
            if key not in ('file', 'expected_sha256'):
                yield from _declared(child)
    elif isinstance(value, list):
        for child in value:
            yield from _declared(child)


def input_documents(params, command=None):
    rows = runtime_documents() + list(_declared(params))
    if params.get('resume'):
        old = _read_descriptor(params['resume'])
        if not isinstance(old, dict) or old.get('video_report_version') != '1.0' or not old.get('candidate') or not old.get('saved_expectation'):
            raise Failure('CONFLICT', 'Resume report lacks a saved video checkpoint')
        if old.get('signature') != signature(params) or old.get('implementation_sha256') != params['implementation_sha256']:
            raise Failure('CONFLICT', 'Resume request or implementation differs')
        candidate = old['candidate']
        rows.append(descriptor({'file': candidate['file'], 'expected_sha256': candidate['sha256']}))
        # Saved output bytes are protected too; no partial output is accepted or overwritten.
        for output in old.get('output_files', []):
            rows.append(descriptor({'file': output['file'], 'expected_sha256': output['sha256']}))
    return _resources(rows, limit=300000)


def schemas():
    return copy.deepcopy({'run': c.RUN_PARAMS_SCHEMA, 'inspect': c.INSPECT_PARAMS_SCHEMA,
                          'manifest': c.MANIFEST_SCHEMA, 'output': c.OUTPUT_SCHEMA, 'clip': c.CLIP_SCHEMA})
