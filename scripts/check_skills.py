# SPDX-License-Identifier: GPL-3.0-or-later
"""Check the shipped skill metadata, local resource graph and private-state exclusions.

The public SKILL.md frontmatter intentionally uses only the simple YAML subset
checked here: name/description and an indented metadata.version string.
"""
import json
from pathlib import Path
import re
import sys
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
SKILLS = ROOT / 'skills'
LINK = re.compile(r'\[[^\]\n]+\]\(([^\n)]+)\)')


def check():
    manifest = json.loads((SKILLS / 'manifest.json').read_text(encoding='utf-8'))
    names = [entry['name'] for entry in manifest['skills']]
    errors, links = [], 0
    if len(names) != 4 or len(set(names)) != 4:
        errors.append('Bundle must identify four unique companion skills')
    for name in names:
        path = SKILLS / name / 'SKILL.md'
        content = path.read_text(encoding='utf-8')
        match = re.match(r'\A---\n(.*?)\n---\n', content, re.S)
        if not match:
            errors.append(name + ': missing YAML frontmatter')
            continue
        header = match.group(1)
        if not re.search(r'^name: ' + re.escape(name) + r'$', header, re.M):
            errors.append(name + ': folder/name mismatch')
        if not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', name) or len(name) > 64:
            errors.append(name + ': invalid skill name')
        description = re.search(r'^description: (.+)$', header, re.M)
        if not description or ': ' in description.group(1) or description.group(1).startswith(('{', '[', '*', '&')):
            errors.append(name + ': description must be a plain YAML scalar')
        if not re.search(r'^metadata:\n  version: "' + re.escape(manifest['bundle_version']) + r'"$', header, re.M):
            errors.append(name + ': bundle metadata version mismatch')
        ui = SKILLS / name / 'agents/openai.yaml'
        metadata = ui.read_text(encoding='utf-8')
        if '$' + name not in metadata or 'allow_implicit_invocation: false' in metadata:
            errors.append(name + ': invocation metadata is inconsistent')
    for path in SKILLS.rglob('*'):
        if not path.is_file() or '__pycache__' in path.parts:
            continue
        if path.suffix not in ('.md', '.py', '.yaml', '.json', '.txt', '.csv') and path.name != 'LICENSE':
            errors.append(str(path.relative_to(ROOT)) + ': unexpected skill asset')
            continue
        text = path.read_text(encoding='utf-8')
        if re.search(r'(?i)[CF]:[/\\]+(?:Users|Blender)|wm-\d{4}-\d{2}-\d{2}|17399', text):
            errors.append(str(path.relative_to(ROOT)) + ': private path/task reference')
        if path.suffix == '.md':
            for found in LINK.finditer(text):
                target = found.group(1).strip().strip('<>')
                if urlsplit(target).scheme or target.startswith('#'):
                    continue
                local = (path.parent / unquote(target.split('#')[0])).resolve()
                links += 1
                if not local.is_relative_to(SKILLS.resolve()) or not local.exists():
                    errors.append(str(path.relative_to(ROOT)) + ': broken/non-bundled local link ' + target)
    return {'ok': not errors, 'bundle_version': manifest['bundle_version'], 'skills': names,
            'local_links': links, 'errors': errors,
            'scope': 'Shipped metadata subset and resource graph; does not certify artistic behavior or private historical cases.'}


if __name__ == '__main__':
    result = check()
    print(json.dumps(result, ensure_ascii=True, indent=2))
    sys.exit(0 if result['ok'] else 1)
