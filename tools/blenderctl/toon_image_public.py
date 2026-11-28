# SPDX-License-Identifier: GPL-3.0-or-later
"""Public discovery, strict normalization and queued implementation identity."""
from collections.abc import Mapping
import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import re
import sys

from protocol import Failure
from toon_image_contract import ImageContractError, normalize_params, run_params_schema as intersection_params_schema

TOOLS = Path(__file__).parent.parent


def plain(value):
    if isinstance(value, Mapping):
        return {k: plain(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [plain(v) for v in value]
    return value


def canonical(value):
    return (json.dumps(plain(value), ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False) + '\n').encode('utf-8')


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def source_files(profile=None):
    """Exact no-follow CLI root and recursive backend Python inventory."""
    paths = {}
    def walk(root, recurse, depth=0):
        if depth > 8:
            raise Failure('RESOURCE_LIMIT', 'Image code directory depth exceeded')
        if root.lstat().st_file_attributes & 0x400:
            raise Failure('CONFLICT', 'Image code directory is a reparse entry')
        with os.scandir(root) as entries:
            for entry in entries:
                attrs = entry.stat(follow_symlinks=False).st_file_attributes
                if attrs & 0x400:
                    raise Failure('CONFLICT', 'Image code inventory contains reparse entry')
                if attrs & 0x10:
                    if recurse and entry.name != '__pycache__':
                        walk(Path(entry.path), recurse, depth + 1)
                elif entry.name.endswith('.py') or (profile == 'cpu-boundary-image.v1' and entry.name.endswith('.dll')):
                    path = Path(entry.path)
                    paths[path.relative_to(TOOLS).as_posix()] = path
                    if len(paths) > 4096:
                        raise Failure('RESOURCE_LIMIT', 'Image code file count exceeded')
    walk(TOOLS / 'blenderctl', False)
    walk(TOOLS / 'toon_image_backend', True)
    if profile == 'cpu-boundary-image.v1':
        walk(TOOLS / 'toon_image_cpu', True)
    return dict(sorted(paths.items()))


def code_identity(profile=None):
    return dict(schema='toon-image-code.v2' if profile == 'cpu-boundary-image.v1' else 'toon-image-code.v1',
        sources={name: digest(path) for name, path in source_files(profile).items()})


def implementation_sha256(identity=None):
    return hashlib.sha256(canonical(identity if identity is not None else code_identity())).hexdigest()


def launch_identity(blender, profile=None):
    return dict(schema='toon-image-launch.v2' if profile == 'cpu-boundary-image.v1' else 'toon-image-launch.v1', code=code_identity(profile),
        binaries=[dict(file=str(Path(path).resolve()), expected_sha256=digest(path))
                  for path in (sys.executable, blender)],
        binary_scope='selected executable bodies and explicitly listed provider DLLs; OS and installed standard/runtime libraries are environment assumptions' if profile == 'cpu-boundary-image.v1' else 'selected executable bodies; OS, DLL and installed standard/runtime libraries are environment assumptions')


def normalize(params):
    try:
        if type(params) is dict and type(params.get('recipe')) is dict and params['recipe'].get('profile') == 'cpu-boundary-image.v1':
            return plain(cpu_contract().normalize_params(params))
        if type(params) is dict and type(params.get('recipe')) is dict and params['recipe'].get('profile') == 'painted-mesh-image.v1':
            return plain(painted_contract().normalize_params(params))
        if type(params) is dict and type(params.get('recipe')) is dict and params['recipe'].get('profile') == 'selected-mesh-image.v1':
            return plain(selected_contract().normalize_params(params))
        return plain(normalize_params(params))
    except ImageContractError as exc:
        raise Failure(exc.code, exc.path + ': ' + str(exc)) from exc


def selected_contract():
    # Fixed sibling package only; no caller-supplied import name or plugin path.
    if str(TOOLS) not in sys.path:
        sys.path.insert(0, str(TOOLS))
    import toon_selected_image_contract
    return toon_selected_image_contract


def painted_contract():
    if str(TOOLS) not in sys.path:
        sys.path.insert(0,str(TOOLS))
    import toon_painted_image_contract
    return toon_painted_image_contract


def cpu_contract():
    if str(TOOLS) not in sys.path:
        sys.path.insert(0,str(TOOLS))
    import toon_cpu_image_contract
    return toon_cpu_image_contract


def run_params_schema():
    return dict(oneOf=[plain(intersection_params_schema()), plain(selected_contract().run_params_schema()),
        plain(painted_contract().run_params_schema()),plain(cpu_contract().run_params_schema())])


def normalize_inspect(params):
    if type(params) is dict and params.get('profile') == 'cpu-boundary-image.v1':
        if set(params) != {'profile','parent_result','implementation_sha256'}:
            raise Failure('INVALID_REQUEST','CPU inspect requires exact profile, parent_result and implementation_sha256')
        clean=normalize_inspect({key:value for key,value in params.items() if key!='profile'})
        return dict(clean,profile='cpu-boundary-image.v1')
    if type(params) is not dict or set(params) != {'parent_result', 'implementation_sha256'}:
        raise Failure('INVALID_REQUEST', 'Inspect requires parent_result and implementation_sha256')
    descriptor = params['parent_result']
    if type(descriptor) is not dict or set(descriptor) != {'file', 'expected_sha256'}:
        raise Failure('INVALID_REQUEST', 'Exact parent result file/SHA descriptor required')
    for value in (params['implementation_sha256'], descriptor['expected_sha256']):
        if type(value) is not str or re.fullmatch('[0-9a-f]{64}', value) is None:
            raise Failure('INVALID_REQUEST', 'Lowercase SHA256 required')
    value = descriptor['file']
    if type(value) is not str or not 1 <= len(value) <= 1024:
        raise Failure('INVALID_REQUEST', 'Bounded native parent result path required')
    try:
        value.encode('utf-8', errors='strict')
    except UnicodeEncodeError as exc:
        raise Failure('INVALID_REQUEST', 'Strict UTF-8 parent result path required') from exc
    text = value.replace('/', '\\')
    if re.match(r'^[A-Za-z]:\\', text) is None or ':' in text[2:]:
        raise Failure('INVALID_REQUEST', 'Local absolute parent result path without UNC/device/ADS required')
    parts = text[3:].split('\\')
    if not all(part and part not in ('.', '..') and not part.endswith((' ', '.')) and
               not any(ord(c) < 32 or ord(c) == 127 or c in '<>:"|?*' for c in part) for part in parts):
        raise Failure('INVALID_REQUEST', 'Noncanonical parent path component')
    path = PureWindowsPath(text)
    if path.name != 'result.json' or re.fullmatch('[0-9a-f]{32}', path.parent.name) is None:
        raise Failure('INVALID_REQUEST', 'Parent result must be a fixed 32hex job/result.json')
    return dict(parent_result=dict(descriptor), implementation_sha256=params['implementation_sha256'])


def inspect_params_schema():
    digest_schema = dict(type='string', pattern='^[0-9a-f]{64}$')
    legacy = dict(type='object', additionalProperties=False,
        required=['parent_result', 'implementation_sha256'], properties=dict(
            implementation_sha256=digest_schema,
            parent_result=dict(type='object', additionalProperties=False,
                required=['file', 'expected_sha256'], properties=dict(
                    file=dict(type='string', minLength=1, maxLength=1024,
                        description='Local canonical lexical path to original 32hex parent job/result.json; no UNC/device/ADS'),
                    expected_sha256=dict(digest_schema)))),
        description='Caller-pinned original parent; same implementation only. Current source/resources are not read.')
    cpu = dict(legacy,required=['profile','parent_result','implementation_sha256'],
        properties=dict(legacy['properties'],profile=dict(const='cpu-boundary-image.v1')))
    return dict(oneOf=[legacy,cpu])


def prepare_inspect_launch(params, timeout, limits, resource_domain):
    if os.name != 'nt':
        raise Failure('UNSUPPORTED', 'Image inspection requires Windows held-handle reader')
    if limits is not None or resource_domain is not None:
        raise Failure('UNSUPPORTED', 'Image inspection OS resource limits have not been qualified')
    if timeout < 30:
        raise Failure('INVALID_REQUEST', 'Image inspection parent timeout must be at least 30 seconds')
    code = code_identity(params.get('profile'))
    if implementation_sha256(code) != params['implementation_sha256']:
        raise Failure('CONFLICT', 'Inspect implementation differs from requested pin')
    python = Path(sys.executable).resolve()
    return dict(schema='toon-image-inspect-launch.v2' if params.get('profile') == 'cpu-boundary-image.v1' else 'toon-image-inspect-launch.v1', code=code,
        python=dict(file=str(python), expected_sha256=digest(python)),
        binary_scope='selected host Python executable body; no current Blender or original input access')


def prepare_launch(params, blender, timeout, limits, resource_domain):
    if os.name != 'nt':
        raise Failure('UNSUPPORTED', 'First image host requires Windows FileGuard and Job Object')
    if limits is not None or resource_domain is not None:
        raise Failure('UNSUPPORTED', 'Image host OS resource domain/limits have not been qualified')
    if params['recipe']['profile'] == 'cpu-boundary-image.v1':
        if timeout < cpu_contract().minimum_timeout(params):
            raise Failure('INVALID_REQUEST','CPU parent timeout must cover binding + render budgets plus 90s capture/verification reserve; no completion guarantee')
    elif params['recipe']['profile'] == 'painted-mesh-image.v1':
        minimum=painted_contract().minimum_timeout(params)
        if timeout<minimum:
            raise Failure('INVALID_REQUEST','Painted parent timeout must cover its explicit producer budget plus capture/verification reserve; no completion guarantee')
    elif params['recipe']['profile'] == 'selected-mesh-image.v1':
        budgets = params['recipe']['budgets']
        binding = budgets.get('binding', {}).get('max_seconds', 300)
        render = budgets.get('render', {}).get('max_seconds', 300)
        minimum = binding + max(binding, render) + 90
        if timeout < minimum:
            raise Failure('INVALID_REQUEST', 'Selected parent timeout must cover binding max_seconds + max(binding,render max_seconds) +90 scheduling reserve; reserve is not a completion guarantee')
    elif params['recipe']['profile'] == 'intersection-image.v1':
        if params['recipe']['budgets'].get('source'):
            raise Failure('UNSUPPORTED', 'Saved exporter has no source budget override API')
        budget = params['recipe']['budgets'].get('pipeline', {}).get('max_seconds', 3600)
        if timeout < budget + 90:
            raise Failure('INVALID_REQUEST', 'Parent timeout must cover pipeline max_seconds plus 90 seconds for export, verification and cleanup')
    else:
        raise Failure('UNSUPPORTED','Unknown explicit image profile')
    identity = launch_identity(blender,params['recipe']['profile'])
    if implementation_sha256(identity['code']) != params['implementation_sha256']:
        raise Failure('CONFLICT', 'Image implementation differs from requested pin')
    return identity


def describe(operation=None, schema=False):
    if operation not in (None, 'run', 'inspect'):
        raise Failure('INVALID_REQUEST', 'Only image run and inspect are registered')
    identity = code_identity()
    value = dict(namespace='toon-image', operations=['run', 'inspect'],
        implementation_sha256=implementation_sha256(identity),
        status='experimental', source_mode='SAVED_DISK_V1', profile='intersection-image.v1',
        profiles=['intersection-image.v1', 'selected-mesh-image.v1','painted-mesh-image.v1','cpu-boundary-image.v1'],
        profile_implementation_sha256={'cpu-boundary-image.v1':implementation_sha256(code_identity('cpu-boundary-image.v1'))},
        support=dict(one_frame=True, camera='ORTHO', max_dimension=2048,
            source_budget_overrides=False, os_resource_limits=False, parent_terminal_required=True,
            inspect_same_implementation=True, inspect_current_source_check=False),
        not_qualified=['arbitrary render parity', 'commercial equivalence', 'character quality', '48FHD24', 'GUI'],
        invocation='toon-image run|inspect --params <UTF-8 params JSON>; use existing --async and job status/wait/cancel/result',
        timeout='intersection: parent >= pipeline max_seconds (default3600)+90; selected: parent >= binding+max(binding,render)+90 (default690); CPU: parent >= binding+render+90 (default690); scheduling reserve is no completion guarantee; inspect: parent >=30; cooperative checkpoints and owned process cleanup, no hard realtime deadline')
    if schema:
        value['params_schema'] = inspect_params_schema() if operation == 'inspect' else plain(run_params_schema())
    return value
