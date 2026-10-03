#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Stage declared bytes into a Linux snapshot; archive a successful material job.

Shared-storage imports are exact-byte copies, NOT deny-write guards on originals.
Only the resulting snapshot is eligible for the normal CLI's strict Linux guards.
No existing destination is overwritten, and failed partial directories are retained.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack, contextmanager
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools' / 'blenderctl'))
from protocol import Failure, CODES, digest
from material_interface_contract import normalize_public_run_params
from material_workflow_addon.contract import WorkflowError, image_documents

MAX_JSON = 8 * 1024 * 1024
MAX_FILE = 4 * 1024**3
MAX_TOTAL = 16 * 1024**3


def absolute_path(value):
    """Accept one unambiguous Linux spelling; never resolve through symlinks."""
    try:
        value = os.fspath(value)
    except TypeError as exc:
        raise Failure('INVALID_REQUEST', 'A Linux path string is required') from exc
    if (not isinstance(value, str) or not value.startswith('/') or value.startswith('//')
            or '\\' in value or '\x00' in value or len(value) > 4096
            or any(ord(c) < 32 for c in value) or value != os.path.normpath(value)
            or any(t in value for t in ('<UDIM>', '<UVTILE>'))):
        raise Failure('INVALID_REQUEST', 'A canonical absolute Linux path is required: ' + repr(value))
    return Path(value)


@contextmanager
def nofollow_open(path, *, directory=False):
    """Walk using pinned directory FDs; reject links at EVERY path component."""
    path = absolute_path(path)
    opened = []
    try:
        fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        opened.append(fd)
        for number, part in enumerate(path.parts[1:]):
            last = number == len(path.parts) - 2
            flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
            if not last or directory:
                flags |= os.O_DIRECTORY
            fd = os.open(part, flags, dir_fd=fd)
            opened.append(fd)
        info = os.fstat(fd)
        if directory:
            if not stat.S_ISDIR(info.st_mode):
                raise Failure('INVALID_REQUEST', 'Directory required: ' + str(path))
        elif not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise Failure('CONFLICT', 'Only unaliased regular files are accepted: ' + str(path))
        yield fd
    except OSError as exc:
        raise Failure('IO_ERROR', 'No-follow open failed for ' + str(path) + ': ' + str(exc)) from exc
    finally:
        for fd in reversed(opened):
            os.close(fd)


def signature(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_nlink)


def parse_json_bytes(raw):
    def pairs(items):
        out = {}
        for key, value in items:
            if key in out:
                raise Failure('INVALID_REQUEST', 'Duplicate JSON key: ' + key)
            out[key] = value
        return out
    try:
        return json.loads(raw.decode('utf-8-sig'), object_pairs_hook=pairs,
                          parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
    except (UnicodeError, ValueError) as exc:
        raise Failure('INVALID_REQUEST', 'Invalid JSON: ' + str(exc)) from exc


def read_bytes(path, limit=MAX_JSON):
    with nofollow_open(path) as fd:
        before = os.fstat(fd)
        if before.st_size > limit:
            raise Failure('RESOURCE_LIMIT', 'File exceeds size limit: ' + str(path))
        raw = bytearray()
        while block := os.read(fd, min(1024 * 1024, limit + 1 - len(raw))):
            raw.extend(block)
            if len(raw) > limit:
                raise Failure('RESOURCE_LIMIT', 'File grew beyond size limit')
        if signature(before) != signature(os.fstat(fd)):
            raise Failure('CONFLICT', 'File changed while reading: ' + str(path))
        return bytes(raw)


def write_new(path, data):
    path = absolute_path(path)
    with nofollow_open(path.parent, directory=True) as parent:
        fd = os.open(path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                     0o600, dir_fd=parent)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())


def write_json_new(path, value):
    write_new(path, (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode())


def new_directory(path):
    path = absolute_path(path)
    with nofollow_open(path.parent, directory=True) as parent:
        try:
            os.mkdir(path.name, mode=0o700, dir_fd=parent)
        except FileExistsError as exc:
            raise Failure('CONFLICT', 'Destination must be a new directory: ' + str(path)) from exc
    return path


def descriptor_path(item):
    if not isinstance(item, dict) or not {'file', 'expected_sha256'} <= item.keys():
        raise Failure('INVALID_REQUEST', 'Explicit file/SHA256 descriptor required')
    if not isinstance(item['expected_sha256'], str) or not re.fullmatch('[0-9a-f]{64}', item['expected_sha256']):
        raise Failure('INVALID_REQUEST', 'Expected SHA256 must be 64 lowercase hex characters')
    return absolute_path(item['file'])


def copy_declared(item, destination, identities=None, *, max_bytes=MAX_FILE):
    """Copy precisely the declared byte version without claiming source protection."""
    source = descriptor_path(item)
    with nofollow_open(source) as fd:
        before = os.fstat(fd)
        identity = (before.st_dev, before.st_ino)
        if identities is not None:
            if identity in identities:
                raise Failure('CONFLICT', 'Aliased source/resource declarations are forbidden')
            identities.add(identity)
        if before.st_size > max_bytes:
            raise Failure('RESOURCE_LIMIT', 'Input exceeds bounded import size limit')
        h = hashlib.sha256()
        with nofollow_open(Path(destination).parent, directory=True) as parent:
            target = os.open(Path(destination).name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=parent)
            with os.fdopen(target, 'wb') as stream:
                count = 0
                while block := os.read(fd, 1024 * 1024):
                    count += len(block)
                    if count > max_bytes:
                        raise Failure('RESOURCE_LIMIT', 'Input grew beyond bounded import size limit')
                    h.update(block)
                    stream.write(block)
                stream.flush()
                os.fsync(stream.fileno())
        if signature(before) != signature(os.fstat(fd)):
            raise Failure('CONFLICT', 'Source changed during import: ' + str(source))
        if h.hexdigest() != item['expected_sha256'] or digest(destination) != h.hexdigest():
            raise Failure('CONFLICT', 'Declared SHA256 does not match copied bytes: ' + str(source))
        return {'file': str(destination), 'expected_sha256': h.hexdigest(), 'bytes': count}


def check_file_spellings(value):
    if isinstance(value, dict):
        if 'file' in value:
            absolute_path(value['file'])
        for child in value.values():
            check_file_spellings(child)
    elif isinstance(value, list):
        for child in value:
            check_file_spellings(child)


def rewrite_descriptors(value, mapping):
    if isinstance(value, dict):
        if 'file' in value and 'expected_sha256' in value:
            original = str(descriptor_path(value))
            if original not in mapping or mapping[original]['expected_sha256'] != value['expected_sha256']:
                raise Failure('INVALID_REQUEST', 'Undeclared or conflicting resource: ' + original)
            value['file'] = mapping[original]['file']
        for child in value.values():
            rewrite_descriptors(child, mapping)
    elif isinstance(value, list):
        for child in value:
            rewrite_descriptors(child, mapping)


def material_request(raw):
    if (not isinstance(raw, dict) or set(raw) != {'schema_version', 'command', 'params'}
            or raw['schema_version'] != '1.0' or raw['command'] != 'material.run'):
        raise Failure('INVALID_REQUEST', 'A full material.run 1.0 public request envelope is required')
    try:
        params = normalize_public_run_params(raw['params'])
        check_file_spellings(params)
        return params
    except WorkflowError as exc:
        raise Failure(exc.code, str(exc)) from exc


def stage(request_path, destination, blender, *, timeout=180):
    if not sys.platform.startswith('linux'):
        raise Failure('UNSUPPORTED', 'Snapshot staging is Linux-only')
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not 0 < timeout <= 86400:
        raise Failure('INVALID_REQUEST', 'Timeout must be within (0, 86400]')
    destination = absolute_path(destination)
    if not destination.is_relative_to('/tmp') or destination == Path('/tmp'):
        raise Failure('UNSUPPORTED', 'Explicit new snapshot directory must be below /tmp on a supported local filesystem')
    blender = absolute_path(blender)
    with nofollow_open(blender):
        pass
    request_bytes = read_bytes(request_path)
    params = material_request(parse_json_bytes(request_bytes))
    source = descriptor_path(params)
    if source.suffix.lower() != '.blend':
        raise Failure('INVALID_REQUEST', 'Source must be a .blend file')
    declared = [params, *params['resources'], *[params[k] for k in ('template', 'bindings') if k in params]]
    paths = [str(descriptor_path(row)) for row in declared]
    if len(set(paths)) != len(paths):
        raise Failure('CONFLICT', 'Source/resource/document paths must be distinct')
    if destination == source or any(Path(p).is_relative_to(destination) for p in paths):
        raise Failure('CONFLICT', 'Snapshot destination overlaps an input')
    observed_total = 0
    for row in declared:
        with nofollow_open(row['file']) as fd:
            size = os.fstat(fd).st_size
            if size > MAX_FILE:
                raise Failure('RESOURCE_LIMIT', 'Input exceeds 4 GiB limit')
            observed_total += size
    if observed_total > MAX_TOTAL:
        raise Failure('RESOURCE_LIMIT', 'Snapshot input total exceeds 16 GiB')
    new_directory(destination)
    new_directory(destination / 'inputs')
    new_directory(destination / 'resources')
    # Probe real guard capability BEFORE importing bytes or invoking Blender.
    probe = destination / 'guard-probe'
    write_new(probe, b'Linux snapshot guard capability probe\n')
    from filesystem import FileGuard
    with FileGuard(probe) as guard:
        guard.sha256()
    write_new(destination / 'submitted-request.json', request_bytes)
    mapping, copied, identities = {}, [], set()
    source_copy = copy_declared(params, destination / 'inputs' / 'original.blend', identities,
                                max_bytes=min(MAX_FILE, MAX_TOTAL))
    copied.append(source_copy)
    for index, row in enumerate(params['resources']):
        suffix = descriptor_path(row).suffix.lower()
        if not re.fullmatch(r'\.[a-z0-9]{1,12}', suffix):
            raise Failure('INVALID_REQUEST', 'Resources require a plain file extension')
        target = destination / 'resources' / (str(index).zfill(3) + '-' + row['expected_sha256'] + suffix)
        resource = copy_declared(row, target, identities, max_bytes=min(MAX_FILE, MAX_TOTAL - sum(r['bytes'] for r in copied)))
        mapping[row['file']] = {k: resource[k] for k in ('file', 'expected_sha256')}
        copied.append(resource)
    documents = {}
    for key in ('template', 'bindings'):
        if key in params:
            row = copy_declared(params[key], destination / 'inputs' / (key + '.json'), identities,
                                max_bytes=min(MAX_JSON, MAX_TOTAL - sum(r['bytes'] for r in copied)))
            copied.append(row)
            documents[key] = parse_json_bytes(read_bytes(row['file']))
            check_file_spellings(documents[key])
    if sum(row['bytes'] for row in copied) > MAX_TOTAL:
        raise Failure('RESOURCE_LIMIT', 'Snapshot input total exceeds 16 GiB')
    prepared = deepcopy(params)
    try:
        if documents:
            from material_workflow_addon.templates import instantiate_template
            bindings = deepcopy(documents['bindings'])
            rewrite_descriptors(bindings, mapping)
            expanded = instantiate_template(documents['template'], bindings)
            if 'manifest' in prepared:
                comparison = deepcopy(prepared['manifest'])
                rewrite_descriptors(comparison, mapping)
                from material_workflow_addon.contract import normalize_manifest
                if normalize_manifest(comparison) != expanded:
                    raise Failure('CONFLICT', 'Expanded template differs from submitted manifest')
            prepared['manifest'] = expanded
            prepared.pop('template', None)
            prepared.pop('bindings', None)
        else:
            rewrite_descriptors(prepared['manifest'], mapping)
        prepared['resources'] = list(mapping.values())
        prepared['file'] = source_copy['file']
        from material_workflow_addon.contract import normalize_params
        prepared = normalize_params(prepared)
    except WorkflowError as exc:
        raise Failure(exc.code, str(exc)) from exc
    plan = {'schema_version': '1.0', 'original_source': str(source), 'source_copy': source_copy,
            'snapshot': str(destination / 'snapshot.blend'), 'resources': mapping}
    write_json_new(destination / 'worker-input.json', plan)
    worker = ROOT / 'tools' / 'blenderctl' / 'linux_snapshot_worker.py'
    command = [str(blender), '--background', '--factory-startup', '--disable-autoexec', '--offline-mode',
               '--python-exit-code', '1', '--python', str(worker), '--', str(destination / 'worker-input.json')]
    with ExitStack() as stack:
        for row in [*copied, {'file': str(destination / 'worker-input.json'), 'expected_sha256': digest(destination / 'worker-input.json')}]:
            guard = stack.enter_context(FileGuard(row['file']))
            if guard.sha256() != row['expected_sha256']:
                raise Failure('CONFLICT', 'Staged bytes changed before Blender')
        with (destination / 'blender.log').open('xb') as log:
            try:
                process = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, timeout=timeout, check=False)
            except subprocess.TimeoutExpired as exc:
                raise Failure('TIMEOUT', 'Snapshot worker timed out; partial directory retained') from exc
        report = parse_json_bytes(read_bytes(destination / 'worker-report.json')) if (destination / 'worker-report.json').is_file() else {}
        if process.returncode or report.get('ok') is not True:
            error = report.get('error', {})
            raise Failure(error.get('code', 'WORKER_FAILED'), error.get('message', 'Snapshot worker failed; inspect blender.log'))
    prepared['file'] = plan['snapshot']
    with FileGuard(prepared['file']) as guard:
        prepared['expected_sha256'] = guard.sha256()
        if prepared['expected_sha256'] != report.get('snapshot_sha256'):
            raise Failure('CONFLICT', 'Snapshot differs from the worker verified save/reopen')
    request = {'schema_version': '1.0', 'command': 'material.run', 'params': prepared}
    from protocol import validate
    request = validate(request)
    write_json_new(destination / 'request.json', request)
    summary = {'schema_version': '1.0', 'ok': True, 'request': str(destination / 'request.json'),
               'snapshot': {'file': prepared['file'], 'expected_sha256': prepared['expected_sha256']},
               'imported_source': {'file': str(source), 'declared_sha256': params['expected_sha256']},
               'resources': prepared['resources'], 'rebound': report['rebound'],
               'original_source_write_attempted': False, 'original_source_mutation_prevented': False,
               'scope': 'Imported bytes match explicit hashes. Originals were not guarded. Normal CLI guards apply only when the new snapshot is run.',
               'template_expanded': bool(documents)}
    write_json_new(destination / 'snapshot-report.json', summary)
    return summary


def inventory(root):
    root = absolute_path(root)
    result, directories = [], []
    def traversal_error(error):
        raise Failure('IO_ERROR', 'Cannot enumerate complete job: ' + str(error)) from error
    with nofollow_open(root, directory=True):
        for base, dirs, files in os.walk(root, followlinks=False, onerror=traversal_error):
            dirs.sort(); files.sort()
            for name in dirs:
                path = Path(base) / name
                with nofollow_open(path, directory=True):
                    pass
                directories.append(path.relative_to(root).as_posix())
            for name in files:
                path = Path(base) / name
                with nofollow_open(path):
                    pass
                result.append(path.relative_to(root).as_posix())
                if len(result) > 10000:
                    raise Failure('RESOURCE_LIMIT', 'Archive exceeds 10000 files')
    return directories, result


def guarded_json(guard):
    raw = bytearray()
    for block in guard.chunks():
        if len(raw) + len(block) > MAX_JSON:
            raise Failure('RESOURCE_LIMIT', 'Authoritative JSON exceeds 8 MiB')
        raw.extend(block)
    return parse_json_bytes(bytes(raw))


def export_job(job, destination):
    if not sys.platform.startswith('linux'):
        raise Failure('UNSUPPORTED', 'Snapshot archival export is Linux-only')
    job, destination = absolute_path(job), absolute_path(destination)
    if not re.fullmatch('[0-9a-f]{32}', job.name):
        raise Failure('INVALID_REQUEST', 'Job directory must use the CLI 32-hex job ID')
    if destination.is_relative_to(job) or job.is_relative_to(destination):
        raise Failure('CONFLICT', 'Archive and job directories must not overlap')
    directories, files = inventory(job)
    from filesystem import FileGuard
    with ExitStack() as stack:
        guards = {name: stack.enter_context(FileGuard(job / name)) for name in files}
        if 'result.json' not in guards or 'status.json' not in guards:
            raise Failure('INVALID_REQUEST', 'Authoritative result.json and status.json are required')
        result = guarded_json(guards['result.json'])
        status = guarded_json(guards['status.json'])
        if (not isinstance(result, dict) or not isinstance(status, dict)
                or not {'schema_version', 'ok', 'error', 'job_id', 'command', 'data'} <= result.keys()
                or result.get('schema_version') != '1.0' or result.get('ok') is not True or result.get('error') is not None
                or result.get('job_id') != job.name or result.get('command') != 'material.run'
                or status.get('job_id') != job.name or status.get('state') != 'succeeded'
                or type(status.get('exit_code')) is not int or status['exit_code'] != 0):
            raise Failure('CONFLICT', 'Only an authoritative terminal succeeded material.run job can be archived')
        data = result.get('data')
        if not isinstance(data, dict) or not isinstance(data.get('resources'), list):
            raise Failure('INVALID_REQUEST', 'Successful result lacks material data/resource descriptors')
        candidate = absolute_path(data.get('candidate', ''))
        if not candidate.is_relative_to(job):
            raise Failure('CONFLICT', 'Candidate must remain inside its owning job directory')
        relative = candidate.relative_to(job).as_posix()
        if relative not in guards or guards[relative].sha256() != data.get('candidate_sha256'):
            raise Failure('CONFLICT', 'Candidate bytes do not match authoritative result')
        for row in data.get('resources', []):
            resource = descriptor_path(row)
            if not resource.is_relative_to(job):
                raise Failure('CONFLICT', 'Candidate resource lies outside its job')
            name = resource.relative_to(job).as_posix()
            if name not in guards or guards[name].sha256() != row['expected_sha256']:
                raise Failure('CONFLICT', 'Candidate resource differs from result')
        new_directory(destination)
        archived_job = new_directory(destination / job.name)
        for name in directories:
            new_directory(archived_job / name)
        entries, total = [], 0
        for name, guard in guards.items():
            target = archived_job / name
            h = hashlib.sha256(); count = 0
            with nofollow_open(target.parent, directory=True) as parent:
                fd = os.open(target.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
                with os.fdopen(fd, 'wb') as stream:
                    for block in guard.chunks():
                        count += len(block); total += len(block)
                        if total > MAX_TOTAL or count > MAX_FILE:
                            raise Failure('RESOURCE_LIMIT', 'Archive exceeds bounded size limits')
                        h.update(block); stream.write(block)
                    stream.flush(); os.fsync(stream.fileno())
            if digest(target) != h.hexdigest() or guard.sha256() != h.hexdigest():
                raise Failure('CONFLICT', 'Archive byte verification failed: ' + name)
            entries.append({'path': job.name + '/' + name, 'bytes': count, 'sha256': h.hexdigest()})
        if inventory(job) != (directories, files):
            raise Failure('CONFLICT', 'Job directory inventory changed during export')
        manifest = {'schema_version': '1.0', 'ok': True, 'job_id': job.name, 'command': 'material.run',
                    'source_job': str(job), 'job_directory': job.name,
                    'candidate': job.name + '/' + relative, 'files': entries,
                    'scope': 'Whole job preserved byte-for-byte, including relative dependency paths. Original reports retain their original absolute evidence paths.'}
    write_json_new(destination / 'archive-manifest.json', manifest)
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    p = sub.add_parser('stage', help='Import exact declared bytes and rebase a separate snapshot')
    p.add_argument('--request', required=True); p.add_argument('--destination', required=True)
    p.add_argument('--blender', required=True); p.add_argument('--timeout', type=float, default=180)
    p = sub.add_parser('export', help='Archive an authoritative succeeded material.run job and all dependencies')
    p.add_argument('--job', required=True); p.add_argument('--destination', required=True)
    args = parser.parse_args(argv)
    try:
        result = stage(args.request, args.destination, args.blender, timeout=args.timeout) if args.action == 'stage' else export_job(args.job, args.destination)
        print(json.dumps(result, ensure_ascii=False, indent=2)); return 0
    except (Failure, OSError) as exc:
        code = exc.code if isinstance(exc, Failure) else 'IO_ERROR'
        print(json.dumps({'ok': False, 'error': {'code': code, 'message': str(exc)}, 'partial_outputs_retained': True}, ensure_ascii=False))
        return CODES.get(code, 5)


if __name__ == '__main__':
    raise SystemExit(main())
