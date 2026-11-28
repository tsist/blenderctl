"""Host validation and immutable inputs for the public modeling work unit."""
import copy
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from protocol import Failure, digest, read_json
from modeling_identity import implementation_sha256, runtime_documents


def normalize_params(value):
    from modeling_workflow_addon import contract
    if not isinstance(value, dict) or set(value) - {'manifest', 'implementation_sha256', 'studio_export'} or 'manifest' not in value:
        raise Failure('INVALID_REQUEST', 'Modeling params require manifest with optional implementation_sha256 or studio_export')
    current = implementation_sha256()
    if value.get('implementation_sha256', current) != current:
        raise Failure('CONFLICT', 'Modeling implementation changed')
    try:
        manifest = contract.normalize_manifest(copy.deepcopy(value['manifest']))
        result = {'manifest': manifest, 'implementation_sha256': current}
        if 'studio_export' in value:
            export = value['studio_export']
            identities = ('project_id', 'operator_id', 'download_job')
            fields = ('file', 'expected_sha256', *identities)
            contract.obj(export, fields, fields)
            reference = contract.file_descriptor({key: export[key] for key in fields[:2]})
            reference.update({key: contract.identity(export[key]) for key in identities})
            if (manifest['operation'] != 'import_local' or Path(manifest['input']['file']).suffix.lower() not in ('.fbx','.glb')
                    or manifest['provenance']['provider'] != 'local'):
                raise contract.ContractError('studio_export requires import_local, a .fbx or .glb source and local provenance')
            if manifest.get('rig_handoff') and Path(manifest['input']['file']).suffix.lower() != '.glb':
                raise contract.ContractError('Studio rig_handoff expects GLB')
            if Path(reference['file']).suffix.lower() != '.json':
                raise contract.ContractError('studio_export must name a completed .json export manifest')
            task = manifest['provenance'].setdefault('task_id', reference['project_id'])
            if task != reference['project_id']:
                raise contract.ContractError('Studio provenance task_id must identify the expected project')
            result['studio_export'] = reference
    except contract.ContractError as exc:
        raise Failure('INVALID_REQUEST', str(exc)) from exc
    return result


def read_studio_export(reference, source):
    """Bind the completed Studio export to the locked FBX or GLB bytes."""
    from modeling_workflow_addon.contract import ContractError, canonical_path
    receipt = Path(reference['file'])
    if receipt.stat().st_size > 16 * 1024 * 1024:
        raise Failure('INVALID_REQUEST', 'Studio export manifest exceeds 16 MiB')
    if digest(receipt) != reference['expected_sha256']:
        raise Failure('CONFLICT', 'Studio export manifest changed')
    doc = read_json(receipt)
    artifact = doc.get('artifact') if isinstance(doc, dict) else None
    suffix = Path(source['file']).suffix.lower()
    if suffix not in ('.fbx', '.glb'):
        raise Failure('INVALID_REQUEST', 'Studio export input must be FBX or GLB')
    format_name, kind = ('fbx', 'FBX_BINARY') if suffix == '.fbx' else ('gltf', 'GLB')
    if (not isinstance(artifact, dict) or artifact.get('ok') is not True
            or artifact.get('format') != format_name or artifact.get('kind') != kind
            or artifact.get('packaging') != 'embedded' or type(artifact.get('bytes')) is not int):
        raise Failure('INVALID_REQUEST', f'Expected a completed Studio {kind} export with embedded packaging')
    ids = ('project_id', 'operator_id', 'download_job')
    if any(doc.get(key) != reference[key] for key in ids):
        raise Failure('CONFLICT', 'Studio export project, operator or download identity differs')
    try:
        paths_match = canonical_path(artifact.get('path')) == canonical_path(source['file'])
    except ContractError as exc:
        raise Failure('INVALID_REQUEST', 'Studio export artifact path is invalid') from exc
    # A GLB can be handed off from a byte-identical local copy. The existing
    # native FBX contract also binds the exact original source path.
    if ((suffix == '.fbx' and not paths_match) or artifact.get('sha256') != source['expected_sha256']
            or artifact['bytes'] != Path(source['file']).stat().st_size):
        raise Failure('CONFLICT', f'Studio export path, SHA-256 or size differs from the {suffix[1:].upper()} input')
    return {**{key: reference[key] for key in ids},
            'export': {key: reference[key] for key in ('file', 'expected_sha256')},
            'format': format_name, 'kind': kind, 'packaging': 'embedded', 'bytes': artifact['bytes'],
            'sha256_scope': 'local_fingerprint_only', 'publisher_signature_verified': False}


def input_documents(params):
    manifest = params['manifest']
    docs = runtime_documents()
    if manifest['operation'] == 'import_local':
        docs.append(manifest['input'])
        if 'studio_export' in params:
            docs.append({key: params['studio_export'][key] for key in ('file', 'expected_sha256')})
    if manifest['operation'] in ('approval_append_prepare', 'approval_append_apply',
                                 'outstanding_increment_prepare', 'outstanding_increment_apply'):
        # The source ledger is intentionally mutable. Holding a host FileGuard
        # on it would deny the operation's own atomic replacement. The append
        # module owns its exact source SHA, evidence identities and CAS checks.
        docs.append(manifest['request_descriptor'])
        from modeling_workflow_addon.providers.approval_append import _read as guarded_append_read
        from modeling_workflow_addon.contract import normalize_provider_request
        request_value, _ = guarded_append_read(manifest['request_descriptor'])
        request = normalize_provider_request(request_value)
        for row in request.get('inputs', []):
            docs.append({'file': row['file'], 'expected_sha256': row['expected_sha256']})
    if manifest['operation'] == 'submit':
        for row in manifest.get('request', {}).get('inputs', []):
            docs.append({'file': row['file'], 'expected_sha256': row['expected_sha256']})
    if manifest['operation'] == 'resume_pre_submit':
        from modeling_workflow_addon.providers.pre_submit_recovery import TrustedPreSubmitRecovery
        from modeling_workflow_addon.contract import canonical_path
        statement = TrustedPreSubmitRecovery.from_descriptor(manifest['pre_submit_recovery_descriptor']).document
        docs.append(manifest['pre_submit_recovery_descriptor'])
        docs.append(statement['verification']['evidence_descriptor'])
        for row in manifest['request']['inputs']:
            docs.append({'file': row['file'], 'expected_sha256': row['expected_sha256']})
        # Ledger remains mutable; reject path aliases before the host guards it.
        if any(canonical_path(row['file']) == canonical_path(manifest['ledger_file'])
               for row in [*docs, manifest['authorization_descriptor']]):
            raise Failure('CONFLICT', 'Immutable pre-submit recovery evidence aliases mutable ledger')
    if manifest['operation'] == 'submit_batch':
        for request in manifest['requests']:
            for row in request.get('inputs', []):
                docs.append({'file': row['file'], 'expected_sha256': row['expected_sha256']})
    if manifest['operation'] == 'bind_unknown':
        from modeling_workflow_addon.providers.budget import TrustedBinding
        statement = TrustedBinding.from_descriptor(manifest['binding_descriptor']).document
        docs.append(manifest['binding_descriptor'])
        docs.append(statement['verification']['evidence_descriptor'])
    if manifest['operation'] == 'resume_download':
        docs.extend((manifest['recovery_descriptor'], manifest['partial_descriptor']))
    if manifest['operation'] == 'download_batch':
        for item in manifest['items']:
            if item['operation'] == 'resume_download':
                docs.extend((item['recovery_descriptor'], item['partial_descriptor']))
    if manifest.get('authorization_descriptor'):
        docs.append(manifest['authorization_descriptor'])
    if manifest.get('provider') == 'tripo-overseas' and 'ledger_file' in manifest:
        from modeling_workflow_addon.contract import canonical_path
        ledger_path = canonical_path(manifest['ledger_file'])
        if any(canonical_path(row['file']) == ledger_path for row in docs):
            raise Failure('CONFLICT', 'Immutable overseas input aliases mutable ledger')
    return docs


def schemas():
    from modeling_workflow_addon import contract
    domains = contract.schemas()
    manifest = domains['manifest']
    sha = {'type': 'string', 'pattern': '^[0-9a-f]{64}$'}
    identity = {'type': 'string', 'pattern': '^[A-Za-z0-9_.-]{1,256}$'}
    studio_export = {'type': 'object', 'additionalProperties': False,
                     'required': ['file', 'expected_sha256', 'project_id', 'operator_id', 'download_job'],
                     'properties': {'file': {'type': 'string', 'description': 'Absolute completed Studio export manifest path'},
                                    'expected_sha256': sha, 'project_id': identity, 'operator_id': identity,
                                    'download_job': identity}}
    run = {'type': 'object', 'additionalProperties': False, 'required': ['manifest'],
           'properties': {'manifest': manifest, 'implementation_sha256': sha, 'studio_export': studio_export}}
    studio = {**run, 'required': ['manifest', 'studio_export'],
              'properties': {**run['properties'], 'manifest': domains['import_local']}}
    return {**domains, 'run': run, 'studio_fbx': studio}
