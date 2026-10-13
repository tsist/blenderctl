"""Read-only, full-request contract admission; never invokes Blender."""
import copy
from pathlib import Path

import protocol


def _documents(value):
    if isinstance(value, dict):
        if 'file' in value and 'expected_sha256' in value:
            yield {key: value[key] for key in ('file', 'expected_sha256')}
        if 'other' in value and 'other_sha256' in value:
            yield {'file': value['other'], 'expected_sha256': value['other_sha256']}
        for child in value.values():
            yield from _documents(child)
    elif isinstance(value, list):
        for child in value:
            yield from _documents(child)


def _error(code, message, detail):
    return {'code': code, 'message': message, 'detail': detail}


def inspect_requests(requests: list[dict], blender: str | None = None) -> dict:
    """Validate complete submitted scales against the current protocol.

    ``checked`` means static admission only. Missing declared inputs defer
    admission; their paths are never synthesized. Runtime evidence is always
    deferred, even for statically valid requests. ``blender`` is metadata only.
    """
    report = {'preflight_version': '1.0', 'ok': False, 'checked': [],
              'failed': [], 'deferred': [], 'requests': [],
              'scale_contract': {'checked': [], 'failed': [], 'deferred': []},
              'blender': blender, 'runtime_executed': False}
    if not isinstance(requests, list) or not 1 <= len(requests) <= 32:
        report['failed'].append({'index': None, 'error': _error(
            'INVALID_REQUEST', 'requests must contain 1..32 request objects', {})})
        return report
    for index, request in enumerate(requests):
        row = {'index': index, 'command': request.get('command') if isinstance(request, dict) else None}
        try:
            if row['command'] in ('render.run', 'media.prepare', 'media.export'):
                from render_contract import normalize_run, normalize_media, normalize_export
                from scene_contract import validate
                params = request.get('params', {})
                if not isinstance(params, dict):
                    raise protocol.Failure('INVALID_REQUEST', 'params must be an object')
                schema = protocol.resource_schema(row['command'])
                if 'resources' in params and schema is not None:
                    validate(params['resources'], schema)
                normalizer = {'render.run': normalize_run, 'media.prepare': normalize_media,
                              'media.export': normalize_export}[row['command']]
                if row['command'] == 'media.prepare':
                    # The public default still verifies physical paths. This
                    # explicit contract mode checks full scale without IO.
                    normalizer(params.get('manifest'), check_files=False)
                else:
                    normalizer(copy.deepcopy(params.get('manifest')))
                report['scale_contract']['checked'].append({**row, 'phase': 'scale_contract'})
                # An unavailable source cannot mask a conflicting existing
                # resource. Descriptor syntax still comes from the contract.
                from node_contract import FILE
                for document in _documents(params):
                    validate(document, FILE)
                    path = Path(document['file'])
                    if path.is_file() and protocol.digest(path) != document['expected_sha256']:
                        raise protocol.Failure('CONFLICT', 'Declared input SHA256 mismatch: ' + str(path))
            normalized = protocol.validate(copy.deepcopy(request))
            if row['command']=='compositing.encode':
                from compositing_encode_public import producer_input_documents
                producer_closure=producer_input_documents(normalized['params'])
            else:producer_closure=[]
            # Some domain contracts intentionally validate descriptor shape
            # without reading the source. Preflight additionally verifies all
            # declared immutable bytes without writing or resolving a runtime.
            missing = []
            seen = set()
            for document in list(_documents(normalized.get('params', {})))+producer_closure:
                key = (document['file'], document['expected_sha256'])
                if key in seen:
                    continue
                seen.add(key)
                path = Path(document['file'])
                if not path.is_file():
                    missing.append(str(path))
                elif protocol.digest(path) != document['expected_sha256']:
                    raise protocol.Failure('CONFLICT', 'Declared input SHA256 mismatch: ' + str(path))
            report['requests'].append({'index': index, 'request': normalized})
            if missing:
                report['deferred'].append({**row, 'phase': 'static_inputs', 'error': _error(
                    'NOT_FOUND', 'Declared inputs are not available', {'files': missing})})
            else:
                report['checked'].append({**row, 'phase': 'static_contract',
                                          'input_documents': len(seen)})
        except protocol.Failure as exc:
            target = 'deferred' if exc.code == 'NOT_FOUND' else 'failed'
            error_row = {**row, 'phase': 'static_contract', 'error': _error(
                exc.code, str(exc), {'validator': 'protocol.validate', 'complete_scale': True})}
            report[target].append(error_row)
            if row['command'] in ('render.run', 'media.prepare', 'media.export') and not any(
                    r['index'] == index for r in report['scale_contract']['checked']):
                report['scale_contract'][target].append({**error_row, 'phase': 'scale_contract'})
        except (OSError, ValueError, TypeError, KeyError) as exc:
            report['failed'].append({**row, 'phase': 'static_contract', 'error': _error(
                'IO_ERROR' if isinstance(exc, OSError) else 'INVALID_REQUEST', str(exc),
                {'validator': 'protocol.validate', 'exception': type(exc).__name__})})
        report['deferred'].append({**row, 'phase': 'runtime', 'error': _error(
            'NOT_RUN', 'Host encoder runtime verification was not executed' if row['command']=='compositing.encode' else 'Blender runtime verification was not executed',
            {'checks': ['exact OCIO source+view mapping','full HEVC RGB/YUV decode, PTS/CICP/metadata','named-reference3% fidelity','actual memory/disk budgets'] if row['command']=='compositing.encode' else ['API/RNA', 'operator poll/context', 'scene evaluation',
                        'actual memory/disk/GPU budgets', 'visual output']})})
    report['ok'] = not report['failed'] and len(report['checked']) == len(requests)
    return report


def inspect_manifest(manifest, blender=None):
    """Preflight all steps before admission; future file bindings stay deferred."""
    from pipeline_contract import get
    requests = [{'schema_version': protocol.SCHEMA, 'command': step['command'],
                 'params': copy.deepcopy(step['params'])} for step in manifest['steps']]
    report = inspect_requests(requests, blender)
    report['workflow_version'] = manifest['workflow_version']
    report['bindings'] = []
    missing_unbound = []
    for index, step in enumerate(manifest['steps']):
        bound = {(b['target'], b['sha_target']) for b in step['bindings']}
        for binding in step['bindings']:
            report['bindings'].append({'index': index, 'step': step['id'],
                                      **binding, 'status': 'deferred_until_verified_producer_output'})
        for pointer, sha_pointer, document in _document_locations(step['params']):
            if not Path(document['file']).is_file() and (pointer, sha_pointer) not in bound:
                missing_unbound.append({'index': index, 'step': step['id'], 'file': document['file']})
        # Path-only sources have no immutable identity; execution validation
        # handles optional-hash old commands, but unknown files are not admitted.
        for name in ('file', 'other'):
            path = step['params'].get(name)
            sha_name = 'expected_sha256' if name == 'file' else 'other_sha256'
            if isinstance(path, str) and not Path(path).is_file() and ('/'+name, '/'+sha_name) not in bound:
                missing_unbound.append({'index': index, 'step': step['id'], 'file': path})
    if missing_unbound:
        report['failed'].append({'phase': 'static_inputs', 'error': _error(
            'NOT_FOUND', 'Unbound input is missing before workflow admission', {'files': missing_unbound})})
    report['admitted'] = not report['failed']
    report['ok'] = report['admitted']
    report['scope'] = 'Static admission with explicit future bindings; runtime/operator/visual acceptance remains deferred.'
    return report


def _document_locations(value, pointer=''):
    """Bindings cover an exact file/hash field pair, never path aliases."""
    if isinstance(value, dict):
        for field, sha_field in (('file', 'expected_sha256'), ('other', 'other_sha256')):
            if field in value and sha_field in value:
                yield pointer+'/'+field, pointer+'/'+sha_field, {'file':value[field], 'expected_sha256':value[sha_field]}
        for key, child in value.items():
            yield from _document_locations(child, pointer+'/'+str(key))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _document_locations(child, pointer+'/'+str(index))
