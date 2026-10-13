"""Verify an explicit public inventory; never run Blender or scan private state."""
from __future__ import annotations
import ast
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
ADDON = 'tools/video_editing_workflow_addon/'
BLOCKED = {'.git', '__pycache__', '.venv', 'audit', 'research', 'tests-output', 'dist'}
IDENTITY = '4a6e44b18e1187baf1569e0f93f3eda4ae2ffa170a3d73b8c7083c0ca8f38bcb'

def sha(data):
    return hashlib.sha256(data).hexdigest()

def safe_file(root, name):
    parts = PurePosixPath(name).parts
    if not name or '\\' in name or ':' in name or name.startswith('/') or set(parts) & (BLOCKED | {'..'}):
        raise ValueError('Unsafe path: ' + name)
    path = root.joinpath(*parts)
    if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root.resolve()):
        raise ValueError('Not an internal regular file: ' + name)
    return path

def load_publication(root=ROOT):
    return json.loads((root / 'PUBLICATION.json').read_text(encoding='utf-8'))

def release_files(publication):
    return sorted(['PUBLICATION.json'] + [row['file'] for row in publication['files']])

def private_findings(text):
    normalized = text.replace('\\', '/').lower()
    patterns = [r'gh[pousr]_[A-Za-z0-9]{30,}', r'github_pat_[A-Za-z0-9_]{40,}',
                r'sk-[A-Za-z0-9_-]{32,}', r'AKIA[A-Z0-9]{16}',
                r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----']
    return ((('173' + '99') in normalized) or re.search(r'f:/+blender ai|wm-20\d\d-\d\d-\d\d-|runtime/+audit/', normalized)
            or any(re.search(p, text) for p in patterns))

def verify(root=ROOT):
    publication = load_publication(root)
    names = release_files(publication)
    if len(names) != len(set(names)):
        raise ValueError('Duplicate inventory path')
    for row in publication['files']:
        if sha(safe_file(root, row['file']).read_bytes()) != row['sha256']:
            raise ValueError('Inventory hash mismatch: ' + row['file'])
    runtime_paths = set((root / 'tools/blenderctl').glob('*.py'))
    runtime_paths.update((root / ADDON).glob('*.py'))
    runtime_paths.add(root / ADDON / 'blender_manifest.toml')
    runtime = [{'file': p.relative_to(root).as_posix(), 'expected_sha256': sha(p.read_bytes())}
               for p in sorted(runtime_paths)]
    identity = sha(json.dumps(runtime, sort_keys=True, separators=(',', ':')).encode())
    if len(runtime) != 189 or identity != IDENTITY or identity != publication['runtime_identity']:
        raise ValueError('Original 189-file video runtime identity changed')
    if not set(row['file'] for row in runtime) <= set(names):
        raise ValueError('Runtime missing from release inventory')
    for name in names:
        path = safe_file(root, name)
        if path.suffix.lower() in {'.pyc', '.blend', '.blend1', '.exe', '.dll'} or path.name in {'AGENTS.md', '.env', 'secrets.json'}:
            raise ValueError('Forbidden release file: ' + name)
        text = path.read_text(encoding='utf-8-sig')
        if private_findings(text):
            raise ValueError('Private content pattern in: ' + name)
        if path.suffix == '.py':
            ast.parse(text, filename=name)
        if path.suffix == '.json':
            json.loads(text)
    for path in (root / 'skills').glob('*/SKILL.md'):
        body = path.read_text(encoding='utf-8-sig')
        if not body.startswith('---\n') or not re.search(r'^name: ' + re.escape(path.parent.name) + r'\s*$', body, re.M):
            raise ValueError('Invalid skill frontmatter: ' + str(path))
        for ref in re.findall(r'\]\((references/[^)]+|scripts/[^)]+)\)', body):
            safe_file(root, (path.parent.relative_to(root) / ref).as_posix())
    if (root / '.git').exists():
        result = subprocess.run(['git', 'ls-files', '-z'], cwd=root, capture_output=True, check=True)
        tracked = {p.decode('utf-8') for p in result.stdout.split(b'\0') if p}
        if tracked and tracked != set(names):
            raise ValueError('Git tracked files differ from release inventory: ' + repr(sorted(tracked ^ set(names))))
    return {'status': 'passed', 'runtime_files': len(runtime), 'runtime_identity': identity,
            'release_files': len(names), 'scope': 'Source hashes, syntax, explicit inventory, skill links and finite privacy checks'}

if __name__ == '__main__':
    try:
        print(json.dumps(verify(), indent=2))
    except (ValueError, OSError, KeyError, SyntaxError, TypeError) as exc:
        print(json.dumps({'status': 'failed', 'error': str(exc)}), file=sys.stderr)
        sys.exit(1)
