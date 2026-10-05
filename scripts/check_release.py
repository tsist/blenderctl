# SPDX-License-Identifier: GPL-3.0-or-later
"""Audit public source candidates without printing potentially sensitive contents."""
import ast
import json
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
EXCLUDED = {'.git', '__pycache__', '.venv', 'runtime', 'dist', 'test-output'}
FORBIDDEN = {'AGENTS.md', 'secrets.json', 'config.json', '.env'}
SECRET = re.compile(r'gh[opusr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|sk-[A-Za-z0-9]{32,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----')
PRIVATE_PATH = re.compile(r'(?:F:[/\\]+Blender|C:[/\\]+Users)', re.I)


def public_files():
    if (ROOT / '.git').exists():
        result = subprocess.run(['git', 'ls-files', '-z'], cwd=ROOT, capture_output=True, check=True)
        paths = [ROOT / p.decode('utf-8') for p in result.stdout.split(b'\0') if p]
        if not paths:
            raise RuntimeError('Stage the reviewed source files before packaging')
        expanded = []
        for path in paths:
            if path.is_dir():
                dependency = subprocess.run(['git', 'ls-files', '-z'], cwd=path, capture_output=True, check=True)
                expanded.extend(path / p.decode('utf-8') for p in dependency.stdout.split(b'\0') if p)
            else:
                expanded.append(path)
        return sorted(expanded)
    return sorted(p for p in ROOT.rglob('*') if p.is_file() and not set(p.relative_to(ROOT).parts) & EXCLUDED)


def audit(paths):
    errors = []
    for path in paths:
        relative = path.relative_to(ROOT)
        if set(relative.parts) & EXCLUDED or any(part in {'.codex', 'research'} for part in relative.parts):
            errors.append(str(relative) + ': forbidden directory')
        if path.name in FORBIDDEN or path.suffix.lower() in {'.blend', '.blend1', '.pyc', '.exe', '.dll'}:
            errors.append(str(relative) + ': forbidden filename/type')
        if path.is_symlink():
            errors.append(str(relative) + ': symlinks are not distributable')
        try:
            content = path.read_text(encoding='utf-8-sig')
        except UnicodeError:
            errors.append(str(relative) + ': unexpected binary')
            continue
        for number, line in enumerate(content.splitlines(), 1):
            if SECRET.search(line) or PRIVATE_PATH.search(line):
                errors.append(f'{relative}:{number}: credential/private path pattern')
        if path.suffix == '.py':
            ast.parse(content, filename=str(relative))
    for required in ('LICENSE', 'README.md', 'README.zh-CN.md', 'SECURITY.md', 'CONTRIBUTING.md',
                     'THIRD_PARTY_NOTICES.md', 'tools/material_workflow_addon/blender_manifest.toml'):
        if ROOT / required not in paths:
            errors.append(required + ': required publication file absent')
    return errors


if __name__ == '__main__':
    files = public_files()
    failures = audit(files)
    print(json.dumps({'ok': not failures, 'files': len(files), 'errors': failures,
                      'scope': 'Allowlisted filenames, UTF-8, Python syntax, credential/private-path patterns; finite source check.'}, indent=2))
    sys.exit(1 if failures else 0)
