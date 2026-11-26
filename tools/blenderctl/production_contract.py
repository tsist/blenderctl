"""Blender 5.3 production requests and versioned add-on verification."""

import hashlib
import json
from pathlib import Path
import re

from protocol import Failure, ROOT, read_json


PACKAGES = {
    'animation': 'animation_curves_workflow_addon',
    'compositing': 'compositing_workflow_addon',
    'video': 'video_editing_workflow_addon',
    'lighting': 'lighting_workflow_addon',
}
RELEASES = ROOT / 'runtime/blender53-production'
DEFAULT_RELEASE = '1.0.0'
RELEASE_ID = re.compile(r'[0-9]+\.[0-9]+\.[0-9]+(?:-rc[1-9][0-9]*)?')


def release_id(value):
    if not isinstance(value, str) or not RELEASE_ID.fullmatch(value):
        raise Failure('INVALID_REQUEST', 'Production release must be a version such as 1.0.0')
    return value


def selected_release(value=None):
    return release_id(DEFAULT_RELEASE if value is None else value)


def release_manifest(release):
    release = release_id(release)
    file = RELEASES / release / 'manifest.json'
    if not file.is_file():
        raise Failure('NOT_FOUND', f'Production bundle {release} is missing. See docs/PRODUCTION.md.')
    manifest = read_json(file)
    if (manifest.get('release') != release or manifest.get('schema_version') != '1.0'
            or set(manifest.get('products', {})) != set(PACKAGES)):
        raise Failure('VALIDATION_FAILED', f'Invalid production release manifest: {release}')
    return manifest


def package_root(domain, release=None):
    release = selected_release(release)
    release_manifest(release)
    return RELEASES / release / 'packages' / PACKAGES[domain]


def implementation_files(domain, release=None):
    release = selected_release(release)
    path = package_root(domain, release)
    if not (path / '__init__.py').is_file():
        raise Failure('NOT_FOUND', f'Production add-on source is missing: {path}')
    product = release_manifest(release)['products'][domain]
    names = product.get('files')
    if not isinstance(names, dict) or '__init__.py' not in names:
        raise Failure('VALIDATION_FAILED', f'Invalid file inventory for production release {release}')
    files = []
    for name in sorted(names):
        member = (path / name).resolve()
        if not member.is_relative_to(path.resolve()) or not member.is_file():
            raise Failure('VALIDATION_FAILED', f'Unsafe or missing production release member: {name}')
        files.append(member)
    actual = {
        file.relative_to(path).as_posix() for file in path.rglob('*')
        if file.is_file() and '__pycache__' not in file.parts
    }
    if actual != set(names):
        raise Failure('CONFLICT', f'Production release file inventory changed: {release}/{domain}')
    return files


def implementation_identity(files, domain, release):
    root = package_root(domain, release).resolve()
    hashes = {path.resolve().relative_to(root).as_posix(): sha for path, sha in files.items()}
    if hashes != release_manifest(release)['products'][domain]['files']:
        raise Failure('CONFLICT', f'Production release bytes changed: {release}/{domain}')
    encoded = json.dumps(hashes, sort_keys=True).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def normalize_params(params):
    required = {'file', 'expected_sha256', 'domain', 'request'}
    optional = {'context', 'resources', 'release'}
    if not isinstance(params, dict) or required - params.keys() or params.keys() - required - optional:
        raise Failure('INVALID_REQUEST', 'Production request requires file, expected_sha256, domain and request')
    filename = params['file']
    if not isinstance(filename, str) or not Path(filename).is_absolute() or Path(filename).suffix.lower() != '.blend':
        raise Failure('INVALID_REQUEST', 'Production input must be an absolute .blend path')
    expected = params['expected_sha256']
    if not isinstance(expected, str) or not re.fullmatch('[0-9a-f]{64}', expected):
        raise Failure('INVALID_REQUEST', 'Production input requires a lowercase SHA-256')
    domain = params['domain']
    if not isinstance(domain, str) or domain not in PACKAGES:
        raise Failure('INVALID_REQUEST', 'Domain must be animation, compositing, video or lighting')
    if not isinstance(params['request'], dict):
        raise Failure('INVALID_REQUEST', 'Domain request must be an object')
    if 'release' in params:
        release_id(params['release'])
    if 'context' in params:
        context = params['context']
        if (not isinstance(context, dict) or set(context) != {'scene', 'view_layer'}
                or not all(isinstance(name, str) and name for name in context.values())):
            raise Failure('INVALID_REQUEST', 'Context requires an explicit scene and view_layer')
    source = str(Path(filename).resolve())
    resources = params.get('resources', [])
    if not isinstance(resources, list):
        raise Failure('INVALID_REQUEST', 'Resources must be a list of file and expected_sha256 descriptors')
    declared = []
    paths = {source.casefold()}
    for resource in resources:
        if not isinstance(resource, dict) or set(resource) != {'file', 'expected_sha256'}:
            raise Failure('INVALID_REQUEST', 'A resource requires file and expected_sha256')
        path, expected = resource['file'], resource['expected_sha256']
        if (not isinstance(path, str) or not Path(path).is_absolute()
                or not isinstance(expected, str) or not re.fullmatch('[0-9a-f]{64}', expected)):
            raise Failure('INVALID_REQUEST', 'Resource requires an absolute path and lowercase SHA-256')
        path = str(Path(path).resolve())
        if path.casefold() in paths:
            raise Failure('INVALID_REQUEST', 'Repeated production resource path')
        paths.add(path.casefold())
        declared.append({'file': path, 'expected_sha256': expected})
    return {**params, 'file': source, 'resources': declared}
