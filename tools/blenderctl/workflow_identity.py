"""Conservative static identity evidence; does not change execution guards."""
import ast
import hashlib
from pathlib import Path
import re
import sys

VERSION = '1.0'


def inventory(root):
    """Hash every regular file and derive bounded local Python dependencies.

    The caller selects the distribution root. Symlinks, parse failures and
    imports outside that root make the inventory incomplete for reuse proof.
    No source file, receipt or historical identity is modified.
    """
    root = Path(root).resolve()
    if not root.is_dir():
        raise ValueError('Inventory root must be an existing directory')
    files, unknown, modules = {}, [], {}
    paths = sorted(root.rglob('*'))
    for path in paths:
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            unknown.append({'file': relative, 'reason': 'symlink is outside regular-file proof'})
        elif path.is_file():
            files[relative] = {'sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'imports': []}
            if path.suffix == '.py':
                parts = list(path.relative_to(root).with_suffix('').parts)
                if parts[-1] == '__init__': parts.pop()
                modules['.'.join(parts)] = relative
    for relative, row in files.items():
        if not relative.endswith('.py'): continue
        module = next(name for name, value in modules.items() if value == relative)
        package = module if relative.endswith('/__init__.py') else module.rpartition('.')[0]
        try:
            tree = ast.parse((root / relative).read_text(encoding='utf-8-sig'))
        except (ValueError, SyntaxError, UnicodeError) as exc:
            unknown.append({'file': relative, 'reason': 'unparsed Python: ' + str(exc)})
            continue
        dependencies = set()
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import): names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    pieces = package.split('.') if package else []
                    if node.level > len(pieces) + 1:
                        unknown.append({'file': relative, 'reason': 'relative import escapes inventory'})
                        continue
                    base = '.'.join(pieces[:len(pieces) - node.level + 1])
                    name = '.'.join(filter(None, [base, node.module]))
                else: name = node.module or ''
                names = [name] + [name + '.' + alias.name for alias in node.names
                                  if name + '.' + alias.name in modules]
            elif isinstance(node, ast.Call):
                call = node.func
                if (isinstance(call, ast.Name) and call.id in ('__import__', 'eval', 'exec')) or (
                    isinstance(call, ast.Attribute) and call.attr in ('import_module', 'exec_module',
                                                                            'load_module', 'spec_from_file_location')):
                    unknown.append({'file': relative, 'reason': 'dynamic code/import requires global verification'})
                if (isinstance(call, ast.Name) and call.id == 'open') or (
                    isinstance(call, ast.Attribute) and call.attr in (
                        'read_text', 'read_bytes', 'open', 'load', 'loads', 'run', 'Popen',
                        'system', 'execv', 'execve')):
                    unknown.append({'file': relative, 'reason': 'runtime data/code dependency requires global verification'})
            for name in names:
                if name in modules:
                    dependencies.add(modules[name])
                    pieces = name.split('.')
                    for size in range(1, len(pieces)):
                        parent = modules.get('.'.join(pieces[:size]))
                        if parent: dependencies.add(parent)
                elif name.split('.')[0] not in sys.stdlib_module_names:
                    unknown.append({'file': relative, 'reason': 'unresolved import: ' + name})
        row['imports'] = sorted(dependencies)
    return {'identity_version': VERSION, 'root': str(root), 'files': files,
            'unknown': unknown, 'complete': not unknown}


def _closure(snapshot, seeds):
    pending, seen, missing = list(seeds), set(), []
    while pending:
        name = pending.pop()
        if name in seen: continue
        seen.add(name)
        if name not in snapshot['files']:
            missing.append(name)
            continue
        pending.extend(snapshot['files'][name]['imports'])
    return sorted(seen), sorted(missing)


def compare(before, after, roots):
    """Analyze named roots: {scope: {domain: [relative entry modules]}}.

    Scopes can be producer, consumer, render, package, or another explicit
    validation class. AST-derived transitive closures include package imports.
    Unknown evidence always requires full global validation and denies reuse.
    """
    changed = sorted(name for name in set(before['files']) | set(after['files'])
                     if before['files'].get(name) != after['files'].get(name))
    impacted, proofs = {}, {}
    unknown = list(before.get('unknown', [])) + list(after.get('unknown', []))
    for scope, domains in roots.items():
        impacted[scope] = []
        proofs[scope] = {}
        for domain, seeds in domains.items():
            old, old_missing = _closure(before, seeds)
            new, new_missing = _closure(after, seeds)
            if old_missing or new_missing:
                unknown.append({'scope': scope, 'domain': domain, 'reason': 'entry/dependency missing',
                                'files': sorted(set(old_missing + new_missing))})
            affected = sorted(set(changed) & (set(old) | set(new)))
            proofs[scope][domain] = {'before_closure': old, 'after_closure': new,
                                     'changed_dependencies': affected,
                                     'known': not old_missing and not new_missing}
            if affected: impacted[scope].append(domain)
    # Documentation is evidence for documentation/package validation, never
    # proof that producer behavior changed in an otherwise known closure.
    docs = [name for name in changed if Path(name).suffix.lower() in ('.md', '.rst', '.txt')]
    required = ['global'] if unknown else [scope for scope, domains in impacted.items() if domains]
    if docs:
        required = sorted(set(required) | {'documentation', 'package'})
    return {'identity_version': VERSION, 'changed': changed, 'impacted': impacted,
            'proof_scopes': proofs, 'unknown': unknown, 'required_validation': required,
            'strict_reuse_eligible': False,
            'reuse_reason': 'Identity impact alone cannot certify runtime receipt compatibility'}


def certify_reuse(before, after, roots, receipt, current):
    """Require exact inventories, known producer closure and immutable context.

    This is an analysis certificate, not permission to bypass an existing guard.
    Receipt/current must declare blender, build, inputs, params, output_sha256,
    inventory, producer_closure and closure_known. Missing evidence denies reuse.
    """
    analysis = compare(before, after, roots)
    required = ('blender', 'build', 'inputs', 'params', 'output_sha256',
                'inventory', 'producer_closure', 'closure_known')
    reasons = []
    if not before.get('complete') or not after.get('complete') or analysis['unknown']:
        reasons.append('Unknown dependency evidence requires full global verification')
    if before['files'] != after['files']: reasons.append('Full inventories differ')
    if not isinstance(receipt, dict) or not isinstance(current, dict):
        reasons.append('Receipt/context must be objects')
    else:
        if any(key not in receipt or key not in current for key in required): reasons.append('Missing exact identity evidence')
        elif any(receipt[key] != current[key] for key in required): reasons.append('Immutable receipt/context differs')
        if receipt.get('closure_known') is not True or current.get('closure_known') is not True:
            reasons.append('Receipt producer closure is not declared known')
        if not isinstance(current.get('output_sha256'), str) or not re.fullmatch(
                '[0-9a-f]{64}', current.get('output_sha256', '')):
            reasons.append('Exact output SHA256 evidence is required')
        producer = analysis['proof_scopes'].get('producer', {})
        expected = {name: row['after_closure'] for name, row in producer.items()}
        if not expected or current.get('producer_closure') != expected:
            reasons.append('Producer closure does not match AST-derived closure')
        if receipt.get('inventory') != before['files'] or current.get('inventory') != after['files']:
            reasons.append('Receipt/context does not contain the exact full inventory')
    return {'identity_version': VERSION, 'strict_reuse_eligible': not reasons,
            'no_unsafe_reuse': True, 'reasons': reasons, 'analysis': analysis,
            'guard_modified': False, 'runtime_executed': False}
