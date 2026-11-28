"""Read-only implementation identity; valid from staged and deployed modules."""
import hashlib
import json
from pathlib import Path


def project_root():
    for parent in Path(__file__).resolve().parents:
        if (parent / 'blenderctl.ps1').is_file():
            return parent
    known = Path('<PROJECT_ROOT>')
    if (known / 'blenderctl.ps1').is_file():
        return known
    raise RuntimeError('Cannot locate Toon project root containing blenderctl.ps1')


ROOT = project_root()


def runtime_documents():
    paths = set((ROOT / 'tools/blenderctl').glob('*.py'))
    paths.update((ROOT / 'tools/toon_workflow_addon').glob('*.py'))
    paths.update(ROOT / 'docs/cli/schemas' / name
                 for name in ('toon.run.schema.json', 'toon.inspect.schema.json', 'toon.template-save.schema.json',
                              'toon.template-edit.schema.json', 'toon.template-apply.schema.json', 'toon.template-library.schema.json'))
    # Staged modules are actually imported, so they are part of the identity.
    # After deployment this resolves to the same formal files and deduplicates.
    paths.update(Path(__file__).resolve().parent / name for name in
                 ('toon_identity.py', 'toon_interface.py', 'toon_workflow_contract.py',
                  'toon_workflow.py', 'toon_verifier.py'))
    paths.add(Path(__file__).resolve().parent / 'toon_host.py')
    result = []
    for path in sorted(paths):
        if not path.is_file():
            raise FileNotFoundError('Toon runtime document missing: ' + str(path))
        result.append({'file': str(path.resolve()),
                       'expected_sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
    return result


def implementation_sha256():
    rows = [{**row, 'file': Path(row['file']).relative_to(ROOT).as_posix()}
            for row in runtime_documents()]
    return hashlib.sha256(json.dumps(rows, sort_keys=True, separators=(',', ':'),
                                    ensure_ascii=False).encode('utf-8')).hexdigest()
