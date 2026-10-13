"""Owned compositor sequence namespaces and bounded, explicit SDR video receipts."""
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import copy
from fractions import Fraction

from protocol import Failure, atomic_json, digest, read_json


class ImmutableMediaDigests:
    """Memoize content only while the same real deny-write/delete handles live."""
    def __init__(self, checked):
        from contextlib import ExitStack
        self.checked = checked
        self.stack = ExitStack()
        self.entries = {}
        self.decoded_images = {}
        self.decoded_movies = {}
        self.directories = {}
        self.active = True
        self.stats = {'initial_full_SHA': 0, 'initial_full_SHA_bytes': 0,
            'reuse': 0, 'live_identity_checks': 0, 'unknown_full_SHA': 0,
            'parent_directory_guards': 0, 'status': 'active',
            'decoded_file_full_reads': 0, 'decoded_file_reuse': 0, 'decoded_file_unknown': 0,
            'scope': 'This process and this attempt only; sequence/tracking SHA and independent decoded file inventory under live immutable FileGuard and protected ancestor paths. First complete decode, selection-key identity and per-use live handle/path checks required; namespace/Action/RNA/protection checks remain independent'}

    def _parents(self, path):
        from filesystem import FileGuard
        # Top-down acquisition prevents an ancestor rename from redirecting
        # the child path between identity checks and fresh file-open checks.
        for parent in reversed(path.parents):
            key = os.path.normcase(str(parent))
            if key not in self.directories:
                guard = self.stack.enter_context(FileGuard(parent, directory=True))
                self.directories[key] = (guard, guard.identity())
                self.stats['parent_directory_guards'] += 1
            guard, identity = self.directories[key]
            if not guard.handle or guard.identity() != identity:
                raise Failure('CONFLICT', 'Immutable media ancestor guard identity changed: ' + str(parent))

    def __call__(self, value):
        from filesystem import FileGuard
        if not self.active:
            raise Failure('CONFLICT', 'Immutable media digest scope has ended')
        path = Path(os.path.abspath(value))
        key = os.path.normcase(str(path))
        row = self.checked.get(key)
        if row is None:
            # Unknown inputs are never memoized or promoted to declared inputs.
            with FileGuard(path) as guard:
                self.stats['unknown_full_SHA'] += 1
                return guard.sha256()
        guard = row.get('guard')
        if type(guard) is not FileGuard or guard.renameable or not guard.handle:
            raise Failure('CONFLICT', 'Digest reuse requires a live ordinary immutable FileGuard: ' + str(path))
        self._parents(path)
        identity = guard.identity()
        cached = self.entries.get(key)
        if cached and (cached['guard'] is not guard or cached['identity'] != identity or cached['sha256'] != row['sha256']):
            raise Failure('CONFLICT', 'Immutable media handle/content identity changed: ' + str(path))
        with FileGuard(path) as current:
            if current.identity() != identity:
                raise Failure('CONFLICT', 'Immutable media path now identifies a different file: ' + str(path))
        self.stats['live_identity_checks'] += 1
        if cached:
            self.stats['reuse'] += 1
            return cached['sha256']
        actual = guard.sha256()
        if actual != row['sha256']:
            raise Failure('CONFLICT', 'First complete guarded media SHA differs: ' + str(path))
        self.stats['initial_full_SHA'] += 1
        self.stats['initial_full_SHA_bytes'] += path.stat().st_size
        self.entries[key] = {'guard': guard, 'identity': identity, 'sha256': actual}
        return actual

    def decoded_image_snapshot(self, binding, required_outputs, inventory_only, decode):
        """Reuse successful OIIO facts, never RNA, pixels, failures or unknown inputs."""
        path = Path(os.path.abspath(binding['file']))
        actual_sha = self(path)  # checks live original and freshly opened path identity on every use
        if actual_sha != binding['expected_sha256']:
            raise Failure('CONFLICT', 'Independent decoded input SHA differs: ' + str(path))
        path_key = os.path.normcase(str(path))
        if path_key not in self.checked:
            self.stats['decoded_file_unknown'] += 1
            return decode()
        key = (path_key, actual_sha, binding['colorspace'], binding['alpha_mode'], binding.get('layer'),
               tuple(sorted(set(required_outputs))), bool(inventory_only))
        if key in self.decoded_images:
            self.stats['decoded_file_reuse'] += 1
            return copy.deepcopy(self.decoded_images[key])
        result = decode()
        if result['sha256'] != actual_sha or os.path.normcase(result['file']) != path_key or result['reader'] != 'OpenImageIO':
            raise Failure('CONFLICT', 'Independent decoded result identity differs')
        self.stats['decoded_file_full_reads'] += 1
        self.decoded_images[key] = copy.deepcopy(result)
        return result

    def close(self):
        self.entries.clear()
        self.decoded_images.clear()
        self.decoded_movies.clear()
        self.directories.clear()
        self.active = False
        self.stats['status'] = 'ended_and_cleared'
        self.stack.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def decoded_movie_snapshot(self, binding, width, height, decode):
        """Cache only complete opaque/interop EXR facts under live attempt guards."""
        path = Path(os.path.abspath(binding['file']))
        actual_sha = self(path)
        if actual_sha != binding['expected_sha256']:
            raise Failure('CONFLICT', 'Independent Movie EXR SHA differs: ' + str(path))
        for name in ('decoded_movie_full_reads', 'decoded_movie_reuse', 'decoded_movie_unknown'):
            self.stats.setdefault(name, 0)
        path_key = os.path.normcase(str(path))
        if path_key not in self.checked:
            self.stats['decoded_movie_unknown'] += 1
            return decode()
        key = ('owned-movie-opaque-linear-v1', path_key, actual_sha, width, height)
        if key in self.decoded_movies:
            self.stats['decoded_movie_reuse'] += 1
            return copy.deepcopy(self.decoded_movies[key])
        result = decode()
        if (type(result) is not dict or result.get('sha256') != actual_sha or
            os.path.normcase(result.get('file', '')) != path_key or result.get('reader') != 'OpenImageIO' or
            result.get('shape') != [height, width, 4] or result.get('alpha') != 'ONE' or
            result.get('container') != 'openexr' or result.get('precision') != 'float32' or
            result.get('interpretation') != 'Linear Rec.709 PREMUL' or result.get('status') != 'passed'):
            raise Failure('CONFLICT', 'Independent Movie EXR result identity/strict summary differs')
        # No pixels or RNA references survive the callback.
        json.dumps(result, allow_nan=False)
        self.stats['decoded_movie_full_reads'] += 1
        self.decoded_movies[key] = copy.deepcopy(result)
        return result


def guarded_digest_scope(stack, checked):
    from compositing_workflow_addon import core
    from compositing_verifier import immutable_image_decode_scope
    provider = stack.enter_context(ImmutableMediaDigests(checked))
    stack.enter_context(core.immutable_media_digest_scope(provider))
    stack.enter_context(immutable_image_decode_scope(provider))
    return provider


def declaration_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()


def guard_rows(rows, stack, checked):
    from filesystem import FileGuard
    for row in rows:
        path = str(Path(row['file']).resolve())
        key = os.path.normcase(path)
        expected = row.get('sha256', row.get('expected_sha256'))
        if key in checked:
            if checked[key]['sha256'] != expected:
                raise Failure('CONFLICT', 'Conflicting media SHA: ' + path)
            continue
        guard = stack.enter_context(FileGuard(path))
        if guard.sha256() != expected:
            raise Failure('CONFLICT', 'Media SHA changed: ' + path)
        checked[key] = {'file': path, 'sha256': expected, 'guard': guard}


def documents(manifest, video=None):
    from compositing_workflow_addon.contract import media_sources
    rows = []
    for _, spec in media_sources(manifest):
        rows.extend(spec['frames'] if spec['kind'] == 'image_sequence' else [spec])
        if spec['kind'] == 'movie':
            rows.extend(spec['decoder_runtime'].values())
    rows.extend(manifest.get('tracking_imports', []))
    rows.extend((video or {}).get('runtime', {}).values())
    return rows


def staged_resources(resolved):
    return [{'file': frame['file'], 'sha256': frame['sha256']}
            for entry in resolved.values() for frame in entry['frames']]


def collect_direct_movie_documents(entry):
    """Collect only persisted descriptors; never open the decode receipt."""
    try:
        if entry['kind'] != 'decoded_movie' or type(entry['version']) is not int or entry['version'] != 1:
            raise Failure('CONFLICT', 'Expected an explicit decoded Movie entry')
        return [entry['original_movie'], *entry['decoder_runtime'].values(), entry['decode_receipt'],
                *({'file': frame['file'], 'sha256': frame['sha256']} for frame in entry['frames'])]
    except (KeyError, TypeError, AttributeError) as exc:
        raise Failure('CONFLICT', 'Malformed direct Movie descriptors') from exc


def _guard_movie_rows(rows, stack, checked, allowed=None):
    """Check declaration eligibility first, then pin ancestors and ordinary files."""
    from filesystem import FileGuard
    import re
    normalized = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get('file'), str) or not Path(row['file']).is_absolute():
            raise Failure('CONFLICT', 'Movie closure requires explicit absolute file descriptors')
        sha = row.get('sha256', row.get('expected_sha256'))
        if not isinstance(sha, str) or re.fullmatch('[0-9a-f]{64}', sha) is None:
            raise Failure('CONFLICT', 'Movie closure descriptor SHA is malformed')
        path = Path(os.path.abspath(row['file']))
        key = os.path.normcase(str(path.resolve()))
        if allowed is not None and allowed.get(key) != sha:
            raise Failure('CONFLICT', 'Undeclared Movie closure path/SHA: ' + str(path))
        normalized.append({'file': str(path), 'sha256': sha})
    parents = getattr(stack, '_compositing_movie_directory_guards', None)
    if parents is None:
        parents = {}
        stack._compositing_movie_directory_guards = parents
    for row in normalized:
        path = Path(row['file'])
        for parent in reversed(path.parents):
            key = os.path.normcase(str(parent))
            if key not in parents:
                guard = stack.enter_context(FileGuard(parent, directory=True))
                parents[key] = (guard, guard.identity())
            guard, identity = parents[key]
            if not guard.handle or guard.identity() != identity:
                raise Failure('CONFLICT', 'Movie ancestor guard identity changed: ' + str(parent))
        if path.is_symlink():
            raise Failure('CONFLICT', 'Movie closure file is a symlink: ' + str(path))
    guard_rows(normalized, stack, checked)
    for row in normalized:
        path = Path(row['file'])
        current_row = checked[os.path.normcase(str(path.resolve()))]
        guard = current_row.get('guard')
        if type(guard) is not FileGuard or guard.renameable or not guard.handle:
            raise Failure('CONFLICT', 'Movie closure requires a live ordinary immutable FileGuard')
        with FileGuard(path) as current:
            if current.identity() != guard.identity():
                raise Failure('CONFLICT', 'Movie closure current path identity differs: ' + str(path))
    return normalized


def guarded_movie_documents(entry, stack, checked, allowed=None):
    """Pin direct files before receipt read, then pin probe/log before any use."""
    if stack is None or checked is None:
        raise Failure('CONFLICT', 'Movie closure needs an explicit guard stack and checked map')
    direct = _guard_movie_rows(collect_direct_movie_documents(entry), stack, checked, allowed)
    receipt_file = Path(entry['decode_receipt']['file'])
    owner = Path(entry['owner_job_directory'])
    if not owner.is_absolute() or receipt_file.resolve().parent != owner.resolve():
        raise Failure('CONFLICT', 'Movie receipt escapes its original owned job')
    receipt = read_json(receipt_file)
    if (not isinstance(receipt, dict) or receipt.get('status') != 'complete' or
        receipt.get('complete_EOS') is not True or receipt.get('declaration_sha256') != entry['declaration_sha256'] or
        receipt.get('frames') != entry['frames']):
        raise Failure('CONFLICT', 'Movie closure requires a complete matching decode receipt')
    try:
        sidecars = [receipt['full_probe'], receipt['decoder_log']]
        if any(Path(row['file']).resolve().parent != owner.resolve() for row in sidecars):
            raise Failure('CONFLICT', 'Movie sidecar escapes its original owned job')
    except (KeyError, TypeError) as exc:
        raise Failure('CONFLICT', 'Movie receipt sidecar descriptors are malformed') from exc
    expanded = _guard_movie_rows(sidecars, stack, checked, allowed)
    merged = {}
    for row in direct + expanded:
        key = os.path.normcase(str(Path(row['file']).resolve()))
        if key in merged and merged[key]['sha256'] != row['sha256']:
            raise Failure('CONFLICT', 'Conflicting Movie closure SHA: ' + row['file'])
        merged[key] = row
    return list(merged.values())


def movie_snapshot(spec, resolved):
    """Complete Movie provenance and all finite opaque interop EXR pixels."""
    from compositing_movie import validate_resolved_movie, read_linear_exr
    from compositing_workflow_addon import core
    validate_resolved_movie(spec, resolved, digest=core._media_content_sha256)
    members = []
    width, height = resolved['movie']['width'], resolved['movie']['height']
    provider = core._immutable_media_provider
    for frame in resolved['frames']:
        def decode():
            values = read_linear_exr(frame['file'], width, height)
            return {'file': str(Path(os.path.abspath(frame['file']))), 'sha256': frame['sha256'],
                'reader': 'OpenImageIO', 'shape': list(values.shape), 'alpha': 'ONE', 'container': 'openexr',
                'precision': 'float32', 'interpretation': 'Linear Rec.709 PREMUL', 'status': 'passed'}
        binding = {'file': frame['file'], 'expected_sha256': frame['sha256']}
        proof = (provider.decoded_movie_snapshot(binding, width, height, decode)
                 if isinstance(provider, ImmutableMediaDigests) else decode())
        members.append({'source_frame': frame['source_frame'], 'source_pts': frame['source_pts'],
            'scene_frame': frame['scene_frame'], **{k: v for k, v in proof.items() if k != 'reader'}})
    return {'status': 'passed', 'kind': 'movie', 'declaration_sha256': resolved['declaration_sha256'],
        'original_movie': resolved['original_movie'], 'decode_receipt': resolved['decode_receipt'],
        'movie': resolved['movie'], 'transform': resolved['transform'], 'members': members,
        'mapping': spec['mapping'],
        'scope': 'Complete H264 SDR8 left420 CFR EOS + original PTS/domain proof; explicit YUV/range/matrix/TRC; all derived finite opaque RGBA32f LinearRec709 EXRs independently decoded'}


def sequence_snapshot(spec, resolved):
    """Every member independently decoded; an anchor is never closure proof."""
    from compositing_verifier import image_file_snapshot
    from compositing_workflow_addon import core
    if declaration_hash(spec) != resolved['declaration_sha256'] or spec['mapping'] != resolved['mapping']:
        raise Failure('CONFLICT', 'Sequence declaration/receipt differs')
    frames = resolved['frames']
    directory = Path(resolved['staging_directory']).resolve()
    owner = Path(resolved['owner_job_directory']).resolve()
    if not directory.is_relative_to(owner / 'media') or directory.is_symlink():
        raise Failure('CONFLICT', 'Sequence namespace escapes owned job')
    if len(frames) != len(spec['frames']) or sorted(p.name for p in directory.iterdir()) != sorted(Path(f['file']).name for f in frames):
        raise Failure('CONFLICT', 'Sequence namespace coverage differs')
    rows, shape = [], None
    for index, (original, frame) in enumerate(zip(spec['frames'], frames), 1):
        path = Path(frame['file'])
        if (path.resolve().parent != directory or path.is_symlink() or
            path.name != 'frame-%06d%s' % (index, path.suffix.lower()) or
            frame['stage_index'] != index or frame['source_frame'] != original['source_frame'] or
            frame['scene_frame'] != spec['mapping']['scene_start'] + index - 1 or
            frame['original_file'] != original['file'] or
            frame['original_sha256'] != original['expected_sha256'] or frame['sha256'] != original['expected_sha256']):
            raise Failure('CONFLICT', 'Sequence original/stage/scene receipt differs')
        decoded = image_file_snapshot({**spec, 'file': frame['file'], 'expected_sha256': frame['sha256']}, ['Image'],
                                      immutable_digest=core._media_content_sha256)
        if len(decoded['subimages']) != 1:
            raise Failure('UNSUPPORTED', 'Sequence requires one flat image per member')
        part = decoded['subimages'][0]
        signature = (decoded['container'], part['width'], part['height'], tuple(part['channels']),
                     part['precision'], tuple(part['channel_precision']), path.suffix.lower())
        if part['channels'] not in (['R', 'G', 'B'], ['R', 'G', 'B', 'A']):
            raise Failure('UNSUPPORTED', 'Sequence requires flat RGB/RGBA members')
        if shape is not None and signature != shape:
            raise Failure('CONFLICT', 'Sequence members have inconsistent decoded container/dimensions/channels/precision')
        shape = signature
        rows.append({'source_frame': frame['source_frame'], 'stage_index': index,
                     'scene_frame': frame['scene_frame'], 'independent_file': decoded})
    if resolved['anchor'] != {'file': frames[0]['file'], 'sha256': frames[0]['sha256'], 'stage_index': 1}:
        raise Failure('CONFLICT', 'Sequence anchor differs from member one')
    return {'status': 'passed', 'declaration_sha256': resolved['declaration_sha256'], 'members': rows,
            'mapping': spec['mapping'], 'scope': 'Complete finite declared flat image sequence; all staged members independently decoded'}


def prepare(manifest, job, stack, checked, progress):
    from compositing_workflow_addon import core
    from compositing_workflow_addon.contract import media_sources, validate, TRACKING_DOCUMENT_SCHEMA
    from compositing_verifier import image_file_snapshot
    resolved, evidence = {}, []
    bindings = dict(media_sources(manifest))
    for key, spec in bindings.items():
        if spec['kind'] == 'movie':
            from compositing_movie import prepare_movie
            entry = prepare_movie(spec, key, job, progress=progress)
            guarded_movie_documents(entry, stack, checked)
            resolved[key] = entry
            evidence.append({'node_id': key, **movie_snapshot(spec, entry)})
            continue
        if spec['kind'] != 'image_sequence':
            continue
        directory = Path(job) / 'media' / ('primary' if key == '$source' else 'node-' + key)
        directory.mkdir(parents=True, exist_ok=False)
        frames, signature = [], None
        for index, original in enumerate(spec['frames'], 1):
            progress('media_member', binding=key, source_frame=original['source_frame'])
            decoded = image_file_snapshot({**spec, **original}, ['Image'], immutable_digest=core._media_content_sha256)
            part = decoded['subimages'][0]
            current = (decoded['container'], part['width'], part['height'], part['channels'],
                       part['precision'], part['channel_precision'])
            if len(decoded['subimages']) != 1 or part['channels'] not in (['R', 'G', 'B'], ['R', 'G', 'B', 'A']):
                raise Failure('UNSUPPORTED', 'Sequence requires flat RGB/RGBA members')
            if signature is not None and signature != current:
                raise Failure('CONFLICT', 'Sequence decoded members differ')
            signature = current
            extension = {'openexr': '.exr', 'png': '.png', 'tiff': '.tif', 'jpeg': '.jpg'}.get(decoded['container'])
            if extension is None:
                raise Failure('UNSUPPORTED', 'Sequence container outside verified native scope')
            destination = directory / ('frame-%06d%s' % (index, extension))
            temporary = directory / ('member-%06d.partial' % index)
            source_guard = checked[os.path.normcase(str(Path(original['file']).resolve()))]['guard']
            with temporary.open('xb') as stream:
                for block in source_guard.chunks(lambda: progress('media_copy', binding=key, stage_index=index)):
                    stream.write(block)
                stream.flush()
                os.fsync(stream.fileno())
            from filesystem import FileGuard
            with FileGuard(temporary, renameable=True) as guard:
                if guard.sha256() != original['expected_sha256']:
                    raise Failure('CONFLICT', 'Sequence copy SHA differs')
                guard.rename_new(destination)
            row = {'source_frame': original['source_frame'], 'stage_index': index,
                   'scene_frame': spec['mapping']['scene_start'] + index - 1,
                   'original_file': original['file'], 'original_sha256': original['expected_sha256'],
                   'file': str(destination), 'sha256': original['expected_sha256']}
            frames.append(row)
            guard_rows([row], stack, checked)
        entry = {'kind': 'image_sequence', 'version': 1, 'declaration_sha256': declaration_hash(spec),
                 'owner_job_directory': str(job), 'staging_directory': str(directory),
                 'anchor': {'file': frames[0]['file'], 'sha256': frames[0]['sha256'], 'stage_index': 1},
                 'frames': frames, 'mapping': spec['mapping']}
        resolved[key] = entry
        evidence.append({'node_id': key, **sequence_snapshot(spec, entry)})
    tracking = []
    for imported in manifest.get('tracking_imports', []):
        value = read_json(imported['file'])
        validate(value, TRACKING_DOCUMENT_SCHEMA)
        spec = bindings.get(imported['source_binding'])
        expected = (list(range(1, spec['mapping']['frame_count'] + 1)) if spec and spec['kind'] == 'movie' else
            [f['source_frame'] for f in spec['frames']] if spec and spec['kind'] == 'image_sequence' else [])
        if (not spec or spec['kind'] not in {'image_sequence', 'movie'} or value['source_binding'] != imported['source_binding'] or
            [s['source_frame'] for s in value['samples']] != expected):
            raise Failure('CONFLICT', 'Tracking document lacks exact dense original source domain')
        tracking.append({'file': imported['file'], 'sha256': imported['expected_sha256'],
                         'samples': len(value['samples']), 'source_binding': value['source_binding'], 'status': 'passed'})
    atomic_json(Path(job) / 'compositing-media-receipt.json', {'resolved_media': resolved, 'sequence_decode': evidence, 'tracking': tracking})
    return resolved, evidence, tracking


def persisted_media():
    """Validate ownership/native RNA first, then expose complete immutable closure."""
    import bpy
    from compositing_workflow_addon import core
    rows, images = {}, {}
    for tree in bpy.data.node_groups:
        for node in tree.nodes:
            if core.MEDIA not in node:
                continue
            scene = bpy.data.scenes.get(node.get(core.SCENE_TAG, ''))
            if scene is None or not node.get(core.OWNER):
                raise Failure('CONFLICT', 'Temporal node lacks Scene ownership')
            spec = json.loads(node[core.BINDING])
            core._validate_sequence_node(node, spec, scene, node[core.OWNER])
            entry = json.loads(node[core.MEDIA])
            key = node.image.name
            if key in images and images[key]['resolved_media'] != entry:
                raise Failure('CONFLICT', 'Shared sequence Image has conflicting receipts')
            images[key] = {'node_id': node[core.NODE_ID], 'declaration': spec, 'resolved_media': entry}
            for frame in entry['frames']:
                rows[os.path.normcase(frame['file'])] = {'file': frame['file'], 'sha256': frame['sha256']}
    return images, list(rows.values())


def persisted_documents(stack=None, checked=None, allowed=None):
    """Collect saved temporal declarations before any core hydrator reads them."""
    import bpy
    from compositing_workflow_addon import core
    rows = []
    for tree in bpy.data.node_groups:
        for node in tree.nodes:
            if core.MEDIA not in node:
                continue
            entry = json.loads(node[core.MEDIA])
            spec = json.loads(node[core.BINDING])
            if spec['kind'] == 'movie':
                rows.extend(guarded_movie_documents(entry, stack, checked, allowed))
            else:
                rows.extend({'file': f['file'], 'sha256': f['sha256']} for f in entry['frames'])
                rows.extend(spec['frames'])
        if core.ANIM in tree:
            rows.extend(json.loads(tree[core.ANIM]).get('tracking_imports', []))
    for scene in bpy.data.scenes:
        state = json.loads(scene.get(core.STATE, '{}'))
        historical = [*state.get('action_history', []), *json.loads(scene.get(core.ARCHIVE, '[]'))]
        for entry in historical:
            rows.extend(entry.get('tracking_imports', []))
    return rows


def _process(binary, args, progress, label, timeout=300):
    import time
    with subprocess.Popen([binary, *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0) as process:
        started = time.monotonic()
        try:
            while True:
                progress(label)
                try:
                    stdout, stderr = process.communicate(timeout=.25)
                    break
                except subprocess.TimeoutExpired:
                    if time.monotonic() - started > timeout:
                        raise Failure('TIMEOUT', label + ' exceeded bounded deadline')
            if process.returncode:
                raise Failure('VALIDATION_FAILED', label + ': ' + stderr.decode('utf-8', 'replace')[-3000:])
            return stdout.decode('utf-8', 'replace')
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)


def composite_black_png(row, destination, *, transfer=None):
    """Decode explicitly unassociated PNG and matte once in baked display domain."""
    import OpenImageIO as oiio
    import numpy as np
    from compositing_verifier import decode_image
    decode_image(row)
    config = oiio.ImageSpec()
    config.attribute('oiio:UnassociatedAlpha', 1)
    reader = oiio.ImageInput.open(row['file'], config)
    if reader is None:
        raise Failure('VALIDATION_FAILED', 'Cannot open explicit unassociated PNG')
    try:
        values = np.asarray(reader.read_image(format=oiio.FLOAT))
        metadata = {'colorspace': reader.spec().get_string_attribute('oiio:ColorSpace') or None,
            'unassociated_alpha': reader.spec().get_int_attribute('oiio:UnassociatedAlpha', 0)}
        if reader.format_name().lower() != 'png' or values.shape != (row['height'], row['width'], 4) or not np.isfinite(values).all():
            raise Failure('VALIDATION_FAILED', 'Explicit unassociated RGBA PNG decode failed')
        if metadata['unassociated_alpha'] != 1:
            raise Failure('VALIDATION_FAILED', 'PNG reader did not confirm unassociated Alpha')
    finally:
        reader.close()
    matte = np.clip(values[:, :, :3] * values[:, :, 3:4], 0, 1)
    converted = matte
    if transfer == 'sRGB_to_BT709':
        linear = np.where(matte <= .04045, matte / 12.92, ((matte + .055) / 1.055) ** 2.4)
        converted = np.where(linear < .018, 4.5 * linear, 1.099 * linear ** .45 - .099)
    elif transfer is not None:
        raise Failure('UNSUPPORTED', 'Unknown explicit video transfer conversion')
    rgb = np.rint(np.clip(converted, 0, 1) * 255).astype(np.uint8)
    writer = oiio.ImageOutput.create(str(destination))
    image_spec = oiio.ImageSpec(row['width'], row['height'], 3, oiio.UINT8)
    if transfer is None and metadata['colorspace']:
        image_spec.attribute('oiio:ColorSpace', metadata['colorspace'])
    try:
        if not writer.open(str(destination), image_spec) or not writer.write_image(rgb):
            raise Failure('VALIDATION_FAILED', 'Cannot write explicit black-composited video input')
    finally:
        writer.close()
    return {'file': str(destination), 'sha256': digest(destination),
        'source_png': {'file': row['file'], 'sha256': row['sha256']},
        'input_decode': metadata, 'alpha_math': 'unassociated RGB multiplied by Alpha exactly once; opaque RGB result',
        'transfer': {'operation': transfer or 'none', 'input': 'sRGB' if transfer else metadata['colorspace'],
            'output': 'BT.709' if transfer else metadata['colorspace'],
            'scope': 'Explicit sRGB EOTF then BT.709 OETF; both use Rec.709/sRGB primaries; 8-bit output quantization' if transfer else 'No transfer conversion; source metadata retained without inference'},
        'domain': 'source PNG display/view already baked; display-domain black matte, not scene-linear physical compositing'}


def encode_video(spec, report, job, progress):
    video = spec.get('video', {})
    if not video.get('enabled'):
        return {'status': 'not_requested'}
    frames = spec['frames']
    receipts = {r['frame']: r for r in report['frame_receipts'] if r['role'] == 'result'}
    if set(receipts) != set(frames) or frames != list(range(frames[0], frames[0] + len(frames))):
        raise Failure('CONFLICT', 'Video requires complete continuous frame receipts')
    runtime = video['runtime']
    ffmpeg, ffprobe = runtime['ffmpeg']['file'], runtime['ffprobe']['file']
    versions = {k: _process(row['file'], ['-version'], progress, k + '_version').splitlines()[0] for k, row in runtime.items()}
    encoders = _process(ffmpeg, ['-hide_banner', '-encoders'], progress, 'ffmpeg_encoders')
    if 'libx264' not in encoders:
        raise Failure('UNSUPPORTED', 'Explicit ffmpeg lacks libx264 encoder')
    directory = Path(job) / 'video-input'
    directory.mkdir(exist_ok=False)
    inputs = []
    source_spaces = {next(r for r in receipts[frame]['outputs'] if r['format'] == 'PNG').get('output_color_space', {}).get('name') for frame in frames}
    transfer = 'sRGB_to_BT709' if source_spaces == {'sRGB'} else None
    for index, frame in enumerate(frames, 1):
        receipt = receipts[frame]
        if receipt['status'] not in ('verified', 'reused'):
            raise Failure('CONFLICT', 'Video frame receipt is unverified')
        row = next(r for r in receipt['outputs'] if r['format'] == 'PNG')
        path = directory / ('frame-%06d.png' % index)
        inputs.append({**composite_black_png(row, path, transfer=transfer), 'scene_frame': frame})
    fps = spec['timeline']['scene_fps']
    rate = Fraction(fps['numerator'], fps['denominator'])
    destination = Path(job) / 'compositing-preview.mp4'
    color_args = ['-vf', 'scale=out_color_matrix=bt709:out_range=tv,setparams=range=limited:color_primaries=bt709:color_trc=bt709:colorspace=bt709', '-color_primaries', 'bt709',
        '-color_trc', 'bt709', '-colorspace', 'bt709', '-color_range', 'tv'] if transfer else []
    _process(ffmpeg, ['-hide_banner', '-nostdin', '-v', 'error', '-n', '-framerate', str(rate),
        '-start_number', '1', '-i', str(directory / 'frame-%06d.png'), '-frames:v', str(len(frames)),
        *color_args,
        '-c:v', 'libx264', '-crf', '18', '-preset', 'medium', '-pix_fmt', 'yuv420p', '-an',
        '-fps_mode', 'cfr', '-movflags', '+faststart', str(destination)], progress, 'video_encode')
    probe = json.loads(_process(ffprobe, ['-v', 'error', '-show_streams', '-show_frames',
        '-show_entries', 'stream=codec_type,codec_name,width,height,avg_frame_rate,r_frame_rate,time_base,pix_fmt,color_range,color_space,color_transfer,color_primaries:frame=best_effort_timestamp',
        '-of', 'json', str(destination)], progress, 'video_full_pts'))
    streams = probe.get('streams', [])
    if len(streams) != 1 or streams[0].get('codec_type') != 'video' or streams[0].get('codec_name') != 'h264':
        raise Failure('VALIDATION_FAILED', 'Video must have exactly one H264 stream and no audio')
    stream = streams[0]
    if (stream['width'], stream['height']) != (spec['width'], spec['height']) or Fraction(stream['avg_frame_rate']) != rate or Fraction(stream['r_frame_rate']) != rate:
        raise Failure('VALIDATION_FAILED', 'Video dimensions/rational rate differs')
    timestamps = [Fraction(f['best_effort_timestamp']) * Fraction(stream['time_base']) for f in probe.get('frames', [])]
    if len(timestamps) != len(frames) or any(t != Fraction(i, 1) / rate for i, t in enumerate(timestamps)):
        raise Failure('VALIDATION_FAILED', 'Video complete CFR PTS differs')
    color_metadata = {key: stream.get(key, 'not_observed') for key in ('color_range', 'color_space', 'color_transfer', 'color_primaries')}
    if transfer and color_metadata != {'color_range': 'tv', 'color_space': 'bt709', 'color_transfer': 'bt709', 'color_primaries': 'bt709'}:
        raise Failure('VALIDATION_FAILED', 'Actual H264 CICP/range differs from explicit BT709 conversion')
    _process(ffmpeg, ['-hide_banner', '-nostdin', '-v', 'error', '-xerror', '-i', str(destination),
        '-map', '0:v:0', '-f', 'null', '-'], progress, 'video_full_decode')
    readback = []
    if transfer:
        import OpenImageIO as oiio
        import numpy as np
        decoded_directory = Path(job) / 'video-readback'
        decoded_directory.mkdir(exist_ok=False)
        for index in sorted({0, len(frames)//2, len(frames)-1}):
            path = decoded_directory / ('frame-%06d.png' % (index + 1))
            _process(ffmpeg, ['-hide_banner', '-nostdin', '-v', 'error', '-n', '-i', str(destination),
                '-vf', 'select=eq(n\\,%d),scale=in_color_matrix=bt709:out_range=pc' % index,
                '-fps_mode', 'vfr', '-frames:v', '1', '-pix_fmt', 'rgb24', str(path)], progress, 'video_color_readback')
            arrays = []
            for source in (inputs[index]['file'], str(path)):
                reader = oiio.ImageInput.open(source)
                try:
                    arrays.append(np.asarray(reader.read_image(format=oiio.FLOAT)))
                finally:
                    reader.close()
            if arrays[0].shape != arrays[1].shape or not all(np.isfinite(a).all() for a in arrays):
                raise Failure('VALIDATION_FAILED', 'Video representative color readback malformed')
            delta = np.abs(arrays[0] - arrays[1])
            mean, maximum = float(delta.mean()), float(delta.max())
            if mean > .08:
                raise Failure('VALIDATION_FAILED', 'Video lossy representative RGB readback error exceeds preview bound')
            readback.append({'scene_frame': frames[index], 'file': str(path), 'sha256': digest(path),
                'mean_absolute_error': mean, 'max_absolute_error': maximum,
                'scope': 'Representative lossy x264 CRF18 YUV420 RGB decode against explicit BT709 transfer-converted opaque RGB inputs; not pixel-exact or full-frame colorimetric conformance'})
    return {'status': 'passed', 'file': str(destination), 'sha256': digest(destination),
            'kind': video['kind'], 'alpha_policy': video['alpha_policy'], 'runtime': runtime,
            'versions': versions, 'encoder': 'libx264', 'crf': 18, 'preset': 'medium',
            'pixel_format': 'yuv420p', 'frames': len(frames), 'scene_fps': fps,
            'pts': [str(t) for t in timestamps], 'black_composited_inputs': inputs,
            'color_metadata': color_metadata, 'representative_color_readback': readback,
            'color_scope': 'Baked source display-domain matte; explicit sRGB to BT709 transfer and actual CICP/range observed; representative lossy readback only' if transfer else
                'Source display/TRC not identified as sRGB; no inferred conversion or color tags, color consistency not_observed; SDR preview only',
            'validation': 'single stream/no audio/full decode/complete CFR timestamps/dimensions/rational FPS'}
