"""Small JSON-only companion CLI. Compilation never launches a job."""
import argparse
import json
from pathlib import Path
import sys
import uuid

sys.dont_write_bytecode = True
_bytecode_prefix = Path(__file__).resolve().parent / ('_absent_cli_pycache_' + uuid.uuid4().hex)
if _bytecode_prefix.exists() or 'template_library' in sys.modules:
    raise RuntimeError('CLI source policy requires fresh absent prefix and no preloaded template module')
sys.pycache_prefix = str(_bytecode_prefix)
import template_library as library
if Path(library.__file__).resolve() != Path(__file__).resolve().parent / 'template_library.py' or _bytecode_prefix.exists():
    raise RuntimeError('CLI template import source identity conflict')


def parser():
    p = argparse.ArgumentParser(description='Declarative SDR video template library')
    sub = p.add_subparsers(dest='command', required=True)
    sub.add_parser('list')
    describe = sub.add_parser('describe')
    _template_arguments(describe)
    schema = sub.add_parser('schema')
    schema.add_argument('--kind', choices=('template', 'input', 'batch', 'catalog'), default='template')
    compile_parser = sub.add_parser('compile')
    _template_arguments(compile_parser)
    compile_parser.add_argument('--input', required=True)
    compile_parser.add_argument('--expected-input-sha', required=True)
    compile_parser.add_argument('--output-dir', required=True)
    save = sub.add_parser('save-template')
    save.add_argument('--template-file', required=True)
    save.add_argument('--expected-template-sha', required=True)
    save.add_argument('--library-dir', required=True)
    batch = sub.add_parser('compile-batch')
    batch.add_argument('--batch', required=True)
    batch.add_argument('--expected-batch-sha', required=True)
    batch.add_argument('--output-dir', required=True)
    return p


def _template_arguments(p):
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument('--template')
    group.add_argument('--template-file')
    p.add_argument('--expected-template-sha')


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == 'list':
            value = library.load_catalog()
        elif args.command == 'schema':
            value = library.schemas()[args.kind]
        elif args.command == 'describe':
            template, desc = library.load_template(args.template, template_file=args.template_file,
                                                   expected_template_sha256=args.expected_template_sha)
            value = {'template': template, 'descriptor': desc}
        elif args.command == 'compile':
            value = library.compile_to_directory(template_id=args.template, template_file=args.template_file,
                expected_template_sha256=args.expected_template_sha, input_file=args.input,
                expected_input_sha256=args.expected_input_sha, output_dir=args.output_dir)
        elif args.command == 'save-template':
            value = library.save_template(template_file=args.template_file, expected_template_sha256=args.expected_template_sha,
                                          library_dir=args.library_dir)
        else:
            value = library.compile_batch(batch_file=args.batch, expected_batch_sha256=args.expected_batch_sha,
                                          output_dir=args.output_dir)
        print(json.dumps({'ok': True, 'result': value}, ensure_ascii=False, allow_nan=False))
        return 0
    except (library.TemplateError, OSError, ValueError) as exc:
        print(json.dumps({'ok': False, 'error': {'code': getattr(exc, 'code', 'IO_ERROR'), 'message': str(exc)}}, ensure_ascii=False))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
