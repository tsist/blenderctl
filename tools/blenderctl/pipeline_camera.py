"""Saved-scene framing inspection through the registered Camera Workflow core."""

import importlib
from pathlib import Path
import re
import sys

from plugin_registry import load_registry, source_path
from protocol import Failure, ROOT, atomic_json, digest, read_json


PACKAGE = 'camera_windows_static'
SHA256 = re.compile('[0-9a-f]{64}')


def _package():
    try:
        plugin = next(row for row in load_registry(ROOT)['plugins'] if row['domain'] == 'camera')
        package = source_path(ROOT, plugin['addon_dir'])
    except (KeyError, StopIteration, ValueError) as exc:
        raise Failure('UNSUPPORTED', 'Camera Workflow registry source is unavailable') from exc
    if package.name != PACKAGE:
        raise Failure('UNSUPPORTED', 'Camera framing requires the registered Windows adapter')
    return package


def _adapter(package):
    loaded = sys.modules.get(PACKAGE)
    if loaded is not None and Path(loaded.__file__).resolve().parent != package:
        raise Failure('CONFLICT', 'Camera Workflow loaded from a different source')
    if str(package.parent) not in sys.path:
        sys.path.insert(0, str(package.parent))
    return importlib.import_module(f'{PACKAGE}.framing_ops')


def _descriptor(row):
    if not isinstance(row, dict) or set(row) != {'file', 'expected_sha256'}:
        raise Failure('INVALID_REQUEST', 'Camera files require file and expected_sha256')
    filename, sha = row['file'], row['expected_sha256']
    if not isinstance(filename, str) or not Path(filename).is_absolute() or '..' in Path(filename).parts:
        raise Failure('INVALID_REQUEST', 'Camera files require absolute paths')
    if not isinstance(sha, str) or not SHA256.fullmatch(sha):
        raise Failure('INVALID_REQUEST', 'Camera files require a lowercase SHA-256')
    return {'file': filename, 'sha256': sha}


def _native_request(params):
    return {'source': _descriptor({key: params[key] for key in ('file', 'expected_sha256')}),
            'scene': params['scene'], 'specs': params['specs'],
            'resources': [_descriptor(row) for row in params['resources']]}


def normalize(params):
    required = {'file', 'expected_sha256', 'scene', 'specs', 'resources'}
    if (not isinstance(params, dict) or required - params.keys() or
            params.keys() - required - {'implementation_sha256'} or
            not isinstance(params['resources'], list) or len(params['resources']) > 64):
        raise Failure('INVALID_REQUEST', 'Camera inspection requires file, SHA-256, scene, specs and resources')
    package = _package()
    identity = digest(package / 'CODE-MANIFEST.json')
    if params.get('implementation_sha256', identity) != identity:
        raise Failure('CONFLICT', 'Camera Workflow implementation changed')
    try:
        _adapter(package).validate(_native_request(params))
    except (TypeError, ValueError) as exc:
        raise Failure('INVALID_REQUEST', str(exc)) from exc
    return {**params, 'implementation_sha256': identity}


def input_documents(params):
    package = _package()
    manifest_path = package / 'CODE-MANIFEST.json'
    if digest(manifest_path) != params['implementation_sha256']:
        raise Failure('CONFLICT', 'Camera code manifest changed')
    entries = read_json(manifest_path).get('files')
    if not isinstance(entries, dict) or 'framing_ops.py' not in entries:
        raise Failure('CONFLICT', 'Malformed Camera Workflow code manifest')
    documents = [{'file': str(manifest_path), 'expected_sha256': params['implementation_sha256']}]
    for name, sha in entries.items():
        member = package / name
        if Path(name).is_absolute() or '..' in Path(name).parts or not member.resolve().is_relative_to(package):
            raise Failure('CONFLICT', 'Camera code manifest member escapes its package')
        documents.append({'file': str(member), 'expected_sha256': sha})
    return documents + params['resources']


def inspect(params, job):
    import bpy

    params = normalize(params)
    adapter = _adapter(_package())
    try:
        adapter.c.verify_package(params['implementation_sha256'])
        audits = adapter.inspect_saved(bpy, _native_request(params))
    except (TypeError, ValueError) as exc:
        raise Failure('VALIDATION_FAILED', str(exc)) from exc
    report = {'scene': params['scene'], 'audits': audits, 'source_saved': False,
              'source_sha256': params['expected_sha256'], 'implementation_sha256': params['implementation_sha256']}
    output = Path(job) / 'camera-inspection.json'
    atomic_json(output, report)
    return {**report, 'report': str(output)}
