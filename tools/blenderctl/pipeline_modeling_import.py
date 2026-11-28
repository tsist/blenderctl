"""Local-only Modeling Workflow request admitted to a worker DAG."""

from protocol import Failure


def normalize(params):
    manifest = params.get('manifest') if isinstance(params, dict) else None
    if not isinstance(manifest, dict) or manifest.get('operation') != 'import_local':
        raise Failure('INVALID_REQUEST', 'Modeling DAG work requires operation=import_local')
    from modeling_workflow_contract import normalize_params

    result = normalize_params(params)
    if result['manifest']['preview']['enabled']:
        raise Failure('UNSUPPORTED', 'Modeling DAG import requires preview.enabled=false')
    return result
