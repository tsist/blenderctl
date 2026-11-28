# SPDX-License-Identifier: GPL-3.0-or-later
"""Host preparation from a protected export, before starting numerical work.

This is an unregistered candidate. The caller owns job authorization, source and
resource guards, implementation selection, stage exit and eventual job status.
No numerical result/receipt supplies ExpectedRun fields.
"""
from collections.abc import Mapping
from dataclasses import asdict, dataclass
import hashlib
import json
import ntpath
import os
from pathlib import Path, PureWindowsPath
import re

from filesystem import FileGuard
from protocol import Failure
from toon_image_backend import intersection_image_receipt_reader as reader


def need(value, message, code='CONFLICT'):
    if not value:
        raise Failure(code, message)


def plain(value):
    if isinstance(value, Mapping):
        return {k: plain(v) for k, v in value.items()}
    if type(value) is tuple:
        return [plain(v) for v in value]
    return value


def canonical(value):
    return (json.dumps(plain(value), ensure_ascii=False, sort_keys=True,
                       separators=(',', ':'), allow_nan=False) + '\n').encode('utf-8')


def sha(value):
    return hashlib.sha256(value).hexdigest()


def local_path(value):
    need(type(value) is str and value, 'Absolute local path required', 'INVALID_REQUEST')
    text = value.replace('/', '\\')
    text.encode('utf-8', errors='strict')
    need(re.match(r'^[A-Za-z]:\\', text) is not None and ':' not in text[2:],
         'Local drive path without stream/device/UNC required', 'UNSUPPORTED')
    parts = text[3:].split('\\')
    need(all(p and p not in ('.', '..') and not p.endswith((' ', '.')) and
             not any(ord(c) < 32 or c in '<>:"|?*' for c in p) for p in parts),
         'Noncanonical path component', 'INVALID_REQUEST')
    return Path(str(PureWindowsPath(text)))


def strict_json(data):
    def pairs(items):
        out = {}
        for k, v in items:
            need(k not in out, 'Duplicate JSON key', 'VALIDATION_FAILED')
            out[k] = v
        return out
    def constant(_):
        raise Failure('VALIDATION_FAILED', 'Nonfinite JSON number')
    return json.loads(data, object_pairs_hook=pairs, parse_constant=constant)


class HeldFiles:
    """Existing FileGuard composition; caller supplies its lifetime ExitStack."""
    def __init__(self, stack, checkpoint):
        self.stack = stack
        self.checkpoint = checkpoint
        self.directories = {}
        self.files = {}
        self.identities = set()

    def directory(self, path):
        path = local_path(str(path))
        for item in (*reversed(path.parents), path):
            key = ntpath.normcase(str(item))
            if key in self.directories:
                continue
            self.checkpoint()
            guard = self.stack.enter_context(FileGuard(item, directory=True))
            info = guard._information()
            need(info.attributes & 0x10 and not info.attributes & 0x400, 'Ordinary directory required')
            self.directories[key] = guard
        return self.directories[ntpath.normcase(str(path))]

    def file(self, path, expected, *, maximum, size=None):
        path = local_path(str(path))
        need(type(expected) is str and re.fullmatch('[0-9a-f]{64}', expected), 'Invalid trusted digest')
        key = ntpath.normcase(str(path))
        if key in self.files:
            row = self.files[key]
            need(row['sha256'] == expected and (size is None or row['bytes'] == size), 'Conflicting input descriptor')
            need(row['bytes'] <= maximum, 'Input exceeds tightened bound', 'RESOURCE_LIMIT')
            return row
        self.directory(path.parent)
        guard = self.stack.enter_context(FileGuard(path))
        info = guard._information()
        actual_size = (info.size_high << 32) | info.size_low
        identity = (info.volume, (info.index_high << 32) | info.index_low)
        need(info.links == 1 and identity not in self.identities, 'Input hardlink/identity alias')
        need(actual_size <= maximum, 'Input byte cap exceeded', 'RESOURCE_LIMIT')
        need(size is None or actual_size == size, 'Input size differs')
        need(guard.sha256(self.checkpoint) == expected, 'Input digest differs')
        row = dict(file=str(path), sha256=expected, bytes=actual_size, identity=identity, guard=guard)
        self.files[key] = row
        self.identities.add(identity)
        return row

    def document(self, path, expected, maximum):
        row = self.file(path, expected, maximum=maximum)
        data = b''.join(row['guard'].chunks(self.checkpoint))
        need(len(data) == row['bytes'] and sha(data) == expected, 'Held JSON changed')
        return strict_json(data)

    def recheck(self):
        for row in self.files.values():
            self.checkpoint()
            info = row['guard']._information()
            need(((info.size_high << 32) | info.size_low) == row['bytes'] and info.links == 1,
                 'Held input metadata changed')
            need(row['guard'].sha256(self.checkpoint) == row['sha256'], 'Held input changed')

    def source_names(self, root):
        """No-follow bounded Python inventory, holding every traversed directory."""
        root = Path(root)
        names = []
        def walk(directory, depth):
            need(depth <= 8, 'Source inventory depth exceeded', 'RESOURCE_LIMIT')
            self.directory(directory)
            with os.scandir(directory) as entries:
                for entry in entries:
                    self.checkpoint()
                    info = entry.stat(follow_symlinks=False)
                    need(not info.st_file_attributes & 0x400, 'Source inventory reparse entry')
                    if info.st_file_attributes & 0x10:
                        if entry.name != '__pycache__':
                            walk(Path(entry.path), depth + 1)
                    elif entry.name.endswith('.py'):
                        names.append(Path(entry.path).relative_to(root).as_posix())
                        need(len(names) <= 4096, 'Source inventory count exceeded', 'RESOURCE_LIMIT')
        walk(root, 0)
        return sorted(names)

    def flat_names(self, root):
        self.directory(root)
        names = set()
        with os.scandir(root) as entries:
            for entry in entries:
                self.checkpoint()
                attrs = entry.stat(follow_symlinks=False).st_file_attributes
                need(not attrs & (0x400 | 0x10), 'Unexpected snapshot directory/reparse entry')
                need(entry.name not in names, 'Duplicate snapshot entry')
                names.add(entry.name)
                need(len(names) <= 3074, 'Snapshot member cap exceeded', 'RESOURCE_LIMIT')
        return names


@dataclass(frozen=True, slots=True)
class PreparedRun:
    request_path: str
    request_sha256: str
    expected_path: str
    expected_sha256: str
    expected_run: reader.ExpectedRun
    host_context_sha256: str


def prepare_exported_run(*, run_root, params, capture_sha256, dependency_audit,
                         host_context, expected_runtime, expected_backend_sources,
                         expected_dependency_implementation, held):
    """Create-only request/expectation under host run_root after export exit.

    run_root/export/capture.json and export/snapshot have fixed host-owned roles.
    Caller retains held through numerical production and final acceptance.
    expected_runtime and expected_backend_sources come from host launch identity,
    not from the capture or later producer receipt. No parent accepted is returned.
    """
    root = local_path(str(run_root))
    held.directory(root)
    recipe = plain(params['recipe'])
    need(not recipe['budgets'].get('source'), 'Source overrides are not implemented by current exporter', 'UNSUPPORTED')
    need(type(host_context) is dict and host_context.get('schema') == 'toon-image-host-context.v1', 'Trusted context required')
    need(host_context.get('params_sha256') == sha(canonical(params)), 'Original params context differs')
    need(host_context.get('run_root') == str(root) and
         type(host_context.get('nonce')) is str and re.fullmatch('[0-9a-f]{32}', host_context['nonce']),
         'Run-root/nonce context differs')
    capture = held.document(root / 'export' / 'capture.json', capture_sha256, 8 * 1024**2)
    need(capture.get('schema') == 'toon-image-saved-capture.v1' and
         capture.get('source_mode') == 'SAVED_DISK_V1' and
         capture.get('declared_recipe') == recipe and capture.get('inputs_unchanged') is True and
         capture.get('source_saved') is False and capture.get('numerical_producer_run') is False,
         'Capture does not bind original recipe and read-only export')
    need(capture.get('runtime') == expected_runtime, 'Export runtime differs from independent host identity')
    wanted_inputs = [{'file': str(local_path(params['file'])), 'expected_sha256': params['expected_sha256']},
                     *[{'file': str(local_path(r['file'])), 'expected_sha256': r['expected_sha256']}
                       for r in params['resources']]]
    captured_inputs = [capture['source'], *capture['resources']]
    need([{k: r[k] for k in ('file', 'expected_sha256')} for r in captured_inputs] == wanted_inputs,
         'Capture source/resource identity differs from original parameters')
    need(capture['verified_inputs_after'] == captured_inputs, 'Capture input after-bindings differ')
    total_input_bytes = 0
    for item in captured_inputs:
        actual = held.file(item['file'], item['expected_sha256'], maximum=512 * 1024**2)
        need(type(item.get('bytes')) is int and item['bytes'] == actual['bytes'] and
             type(item.get('volume')) is int and type(item.get('file_index')) is int and
             (item['volume'], item['file_index']) == actual['identity'] and
             type(item.get('links')) is int and item['links'] == 1,
             'Capture input metadata differs from parent held handle')
        total_input_bytes += actual['bytes']
    audit_path = root / 'export' / 'resources.json'
    need(type(dependency_audit) is dict and set(dependency_audit) == {'file', 'expected_sha256', 'bytes'} and
         dependency_audit['file'] == str(audit_path) and type(dependency_audit['bytes']) is int,
         'Dependency audit descriptor differs from fixed host role')
    audit_row = held.file(audit_path, dependency_audit['expected_sha256'],
                          maximum=8 * 1024**2, size=dependency_audit['bytes'])
    audit = held.document(audit_path, dependency_audit['expected_sha256'], 8 * 1024**2)
    need(audit.get('version') == '1.0' and audit.get('adapter') == 'TYPED_FILES_V1' and
         audit.get('closure') == 'closed' and audit.get('issues') == [] and
         audit.get('source_saved') is False and audit.get('source') == wanted_inputs[0] and
         audit.get('blender_build') == expected_runtime['blender_build'] and
         audit.get('implementation_sha256') == expected_dependency_implementation,
         'Dependency audit provenance/closure differs')
    need(audit.get('profile') == dict(closure=dict(adapter='TYPED_FILES_V1', max_files=101, max_depth=8),
         timeline=dict(scene=recipe['scene'], start=recipe['frame'], end=recipe['frame'], step=1)),
         'Dependency audit profile differs')
    documents = audit.get('documents')
    need(type(documents) is list and len(documents) == len(wanted_inputs), 'Dependency document count differs')
    actual_documents = {}
    for item in documents:
        need(type(item) is dict and set(item) == {'file', 'expected_sha256'}, 'Invalid dependency document')
        key = ntpath.normcase(str(local_path(item['file'])))
        need(key not in actual_documents, 'Duplicate dependency document')
        actual_documents[key] = item['expected_sha256']
    need(actual_documents == {ntpath.normcase(item['file']): item['expected_sha256'] for item in wanted_inputs} and
         type(audit.get('file_count')) is int and audit['file_count'] == len(wanted_inputs) and
         type(audit.get('bytes')) is int and audit['bytes'] == total_input_bytes,
         'Dependency audit differs from parent held input set')
    rejected = {'point_cache', 'fluid_cache', 'rigidbody_cache', 'geometry_nodes_cache',
                'geometry_nodes_bake', 'cache_file', 'volume', 'declared_cache', 'declared_plugin'}
    need(type(audit.get('resources')) is list and all(type(item) is dict and
         item.get('kind') not in rejected and item.get('requirement') != 'unknown' and
         item.get('source') != 'SEQUENCE' and not item.get('frame_mapping') for item in audit['resources']),
         'Dynamic or unknown resource outside static saved intake')
    need(capture.get('object_names') == recipe['occluder_object_names'] and
         capture.get('member_object_names') == recipe['member_object_names'] and
         [capture.get(k) for k in ('scene', 'view_layer', 'frame', 'camera_name')] ==
         [recipe[k] for k in ('scene', 'view_layer', 'frame', 'camera_name')], 'Capture domain differs')
    snapshot_root = root / 'export' / 'snapshot'
    held.directory(snapshot_root)
    exported = capture['snapshot']
    need(exported['path'] == str(snapshot_root / 'manifest.json') and
         exported['receipt_path'] == str(snapshot_root / 'receipt.json'), 'Snapshot descriptor path differs')
    manifest = held.document(snapshot_root / 'manifest.json', exported['sha256'], 8 * 1024**2)
    held.file(snapshot_root / 'receipt.json', exported['receipt_sha256'], maximum=8 * 1024**2)
    need(type(manifest.get('objects')) is list and 0 < len(manifest['objects']) <= 1024, 'Invalid source object count')
    need([o['object_id'] for o in manifest['objects']] == recipe['occluder_object_names'], 'Snapshot object order differs')
    total = 0
    expected_members = {'manifest.json', 'receipt.json'}
    for i, obj in enumerate(manifest['objects']):
        for role in ('world', 'triangles', 'polygon'):
            row = obj['arrays'][role]
            expected_path = snapshot_root / (f'{i:06d}.{role}.npy')
            expected_members.add(expected_path.name)
            need(row['path'] == str(expected_path), 'Array path outside fixed export grammar')
            held_row = held.file(expected_path, row['sha256'], maximum=536870912, size=row['bytes'])
            total += held_row['bytes']
            need(total <= 536870912, 'Export array byte budget exceeded', 'RESOURCE_LIMIT')
    need(held.flat_names(snapshot_root) == expected_members, 'Snapshot member set differs')
    product = Path(reader.__file__).parent
    actual_names = held.source_names(product)
    need(actual_names == sorted(expected_backend_sources), 'Backend source inventory differs from launch identity')
    for name in actual_names:
        held.file(product / name, expected_backend_sources[name], maximum=8 * 1024**2)
    # Imports occur only after all selected product sources are held against edits.
    from toon_image_backend import intersection_image_runner as producer
    from toon_image_backend.intersections import paged_snapshot, image_pipeline
    from toon_image_backend.intersections.camera_exact import bind_camera_exact
    snapshot = paged_snapshot.open_snapshot_arrays(exported['path'], exported['sha256'], [root])
    try:
        need(snapshot.snapshot_id == exported['snapshot_id'], 'Snapshot identity differs')
    finally:
        # Only scalar metadata/input locks are needed below. Release every mmap
        # before any later camera, serialization, checkpoint or publication error.
        for obj in snapshot.objects:
            for array in (obj.world_vertices, obj.triangle_vertex_indices, obj.triangle_polygon):
                if getattr(array, '_mmap', None) is not None:
                    array._mmap.close()
    policy = dict(recipe['policy'], member_object_ids=recipe['member_object_names'])
    request = dict(schema='toon-intersection-image-request.v1',
        snapshot=dict(manifest_path=exported['path'], manifest_sha256=exported['sha256']),
        camera_exact=capture['camera_exact'], policy=policy, style=recipe['style'], budgets=recipe['budgets'])
    producer.validate_request(request)
    bound_camera = bind_camera_exact(request['camera_exact'])
    need(0 < bound_camera['width'] <= 2048 and 0 < bound_camera['height'] <= 2048 and
         [capture['width'], capture['height']] == [bound_camera['width'], bound_camera['height']], 'Camera dimensions differ')
    request_path = root / 'backend-request.json'
    request_data = canonical(request)
    sealed_context = dict(schema='toon-image-numeric-context.v1', parent=host_context,
        capture_sha256=capture_sha256, dependency_audit_sha256=audit_row['sha256'],
        dependency_implementation_sha256=expected_dependency_implementation, snapshot_id=snapshot.snapshot_id,
        snapshot_manifest_sha256=snapshot.manifest_sha256,
        backend_request_sha256=sha(request_data), runtime=expected_runtime,
        producer_sources=expected_backend_sources)
    context_data = canonical(sealed_context)
    context_sha = sha(context_data)
    expected = reader.ExpectedRun(host_context_sha256=context_sha,
        backend_request_sha256=sha(request_data), snapshot_id=snapshot.snapshot_id,
        snapshot_manifest_sha256=snapshot.manifest_sha256, camera_id=bound_camera['camera_id'],
        image_shape=(bound_camera['height'], bound_camera['width'], 4),
        recorded_request_path=str(request_path), recorded_snapshot_manifest_path=snapshot.manifest_path,
        recorded_input_locks=tuple(snapshot.input_locks), policy_canonical_json=canonical(policy),
        style_canonical_json=canonical(recipe['style']),
        producer_outer_code_sha256=tuple(sorted(producer.code_hashes(False).items())),
        producer_inner_code_sha256=tuple(sorted(image_pipeline._code_hashes(False).items())),
        runtime_python=expected_runtime['python'], runtime_numpy=expected_runtime['numpy'])
    serialized = asdict(expected)
    for key in ('policy_canonical_json', 'style_canonical_json'):
        serialized[key] = serialized[key].decode('utf-8')
    expected_data = canonical(serialized)
    expected_path = root / 'EXPECTED.json'
    held.checkpoint()
    for path, data in ((root / 'NUMERIC-CONTEXT.json', context_data),
                       (request_path, request_data), (expected_path, expected_data)):
        with path.open('xb') as stream:
            stream.write(data)
        held.file(path, sha(data), maximum=8 * 1024**2)
    held.recheck()
    need(held.flat_names(snapshot_root) == expected_members, 'Snapshot membership changed while preparing')
    need(held.source_names(product) == actual_names, 'Backend membership changed while preparing')
    return PreparedRun(str(request_path), sha(request_data), str(expected_path),
                       sha(expected_data), expected, context_sha)
