"""Capture a saved mesh selection into a replayable uv.run generate request."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile

ENTRY = Path(__file__).resolve()
sys.path.insert(0, str(ENTRY.parent))
from filesystem import FileGuard
from protocol import CODES, DEFAULT_BLENDER, Failure, atomic_json, envelope, read_json
from uv_saved_reader import new_destination, run_saved_reader
from uv_workflow_contract import addon_contract, normalize_params

COMMAND = 'uv.capture-constraints'
MARKER = '--uv-constraint-worker'
KINDS = ('must_cut', 'must_not_cut', 'locked_faces', 'clear')


def capture(request):
    import bpy
    from uv_workflow import _check_identity, _resources
    from uv_workflow_addon import core, ui

    base = request['base']
    _check_identity(base)
    status = bpy.ops.wm.open_mainfile(filepath=base['file'], load_ui=True, use_scripts=False)
    if status != {'FINISHED'} or Path(bpy.data.filepath).resolve() != Path(base['file']):
        raise Failure('VALIDATION_FAILED', 'Blender did not reopen the specified working copy')
    if tuple(bpy.data.version[:2]) != tuple(bpy.app.version[:2]):
        raise Failure('UNSUPPORTED', 'Saved file and Blender must have the same major/minor version')
    manifest = base['manifest']
    context, kind = bpy.context, request['kind']
    scene, obj = context.scene, context.object
    if scene.name != manifest['scene'] or context.view_layer.name != manifest['view_layer']:
        raise Failure('CONFLICT', 'The saved active scene or view layer differs from the generate request')
    if context.mode != 'OBJECT' or not obj or obj.name != request['object'] or obj.type != 'MESH':
        raise Failure('CONFLICT', 'Save the named active mesh in Object Mode after making the edge or face selection')
    _, targets = core._resolve(manifest)
    _resources(base)
    if request['baseline'] == 'explicit_manifest':
        previous = manifest['constraints']
    else:
        try:
            saved = json.loads(scene.get('uvw_constraints', '{}'))
        except (TypeError, ValueError) as exc:
            raise Failure('CONFLICT', 'Invalid pending constraint data in the saved scene') from exc
        if not isinstance(saved, dict):
            raise Failure('CONFLICT', 'Expected per-object pending constraints in the saved scene')
        previous = {obj_name: saved[obj_name] for obj_name in manifest['objects'] if obj_name in saved}
    # The panel edits its Scene draft in an isolated process. Only the generate
    # request is published. Existing candidate records and the .blend stay intact.
    scene['uvw_constraints'] = json.dumps(previous, ensure_ascii=False)
    bpy.utils.register_class(ui.UVW_OT_constraints)
    try:
        outcome = bpy.ops.uvw.constraints(kind=kind)
    except RuntimeError as exc:
        raise Failure('VALIDATION_FAILED', str(exc)) from exc
    if outcome != {'FINISHED'}:
        raise Failure('VALIDATION_FAILED', 'UV panel did not record the requested constraint')
    current = json.loads(scene['uvw_constraints'])
    updated = normalize_params({**base, 'manifest': {**manifest, 'constraints': current}}, 'uv.run')
    # Validate each addressed index and fixed UV source on the actual mesh.
    for target in targets:
        core._constraints(target, updated['manifest'])
    components = obj.data.polygons if kind == 'locked_faces' else obj.data.edges
    selected_count = None if kind == 'clear' else sum(item.select and not item.hide for item in components)
    return {'params': updated, 'baseline': request['baseline'], 'object': obj.name, 'mesh': obj.data.name,
            'kind': kind, 'selected_count': selected_count, 'pending_changed': current != previous,
            'source_unchanged': True, 'runtime': {'blender': bpy.app.version_string, 'build': bpy.app.build_hash.decode()}}


def worker(input_path, output_path):
    try:
        response = {'data': capture(read_json(input_path))}
    except (Failure, addon_contract.WorkflowError) as exc:
        response = {'error': {'code': exc.code, 'message': str(exc)}}
    except (TypeError, ValueError, RuntimeError, KeyError) as exc:
        response = {'error': {'code': 'VALIDATION_FAILED', 'message': str(exc)}}
    atomic_json(output_path, response)


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Failure('INVALID_REQUEST', message)


def main():
    cli = Parser(description=__doc__)
    cli.add_argument('--blender', type=Path, default=DEFAULT_BLENDER)
    cli.add_argument('--timeout', type=float, default=120)
    cli.add_argument('--jobs-dir', help=argparse.SUPPRESS)
    cli.add_argument('--transactions-dir', help=argparse.SUPPRESS)
    display = cli.add_mutually_exclusive_group()
    for flag in ('--compact', '--full', '--human'):
        display.add_argument(flag, dest='display', action='store_const', const=flag)
    action = cli.add_subparsers(required=True).add_parser('capture-constraints', description=__doc__,
        epilog='Omit manifest.constraints to use saved panel rules. Supply it to continue an explicit draft. '
               'Save the mesh in Object Mode after selecting components. Clear removes the named object’s pending rules.')
    action.add_argument('--params', type=Path, required=True, help='uv.run generate params with source, SHA and resources')
    action.add_argument('--object', required=True, help='Saved active mesh in manifest.objects')
    action.add_argument('--kind', choices=KINDS, required=True)
    action.add_argument('--output', type=Path, required=True, help='New absolute generate params .json')
    args = data = error = None
    try:
        args = cli.parse_args()
        dest = new_destination(args.output, '.json')
        supplied = read_json(args.params)
        base = normalize_params(supplied, 'uv.run')
        if base['manifest']['operation'] != 'generate' or args.object not in base['manifest']['objects']:
            raise Failure('INVALID_REQUEST', 'Provide generate params containing the specified target object')
        if not args.blender.is_absolute() or not args.blender.is_file():
            raise Failure('NOT_FOUND', 'Selected Blender executable is unavailable')
        request = {'base': base, 'object': args.object, 'kind': args.kind,
                   'baseline': 'explicit_manifest' if 'constraints' in supplied['manifest'] else 'saved_scene'}
        data = run_saved_reader(args.blender, request, ENTRY, MARKER, args.timeout)
        draft = data.pop('params')
        with tempfile.TemporaryDirectory(prefix='.uv-constraints-', dir=dest.parent) as staging:
            staged = Path(staging) / 'params.json'
            atomic_json(staged, draft)
            with FileGuard(staged, renameable=True) as guard:
                checksum = guard.sha256()
                guard.rename_new(dest)
        data.update(output={'file': str(dest), 'sha256': checksum},
                    source={key: base[key] for key in ('file', 'expected_sha256')})
        if args.display == '--full':
            data['params'] = draft
    except (Failure, addon_contract.WorkflowError) as exc:
        error = {'code': exc.code, 'message': str(exc)}
    except subprocess.TimeoutExpired:
        error = {'code': 'TIMEOUT', 'message': 'Timed out while reading pending UV constraints'}
    except OSError as exc:
        error = {'code': 'IO_ERROR', 'message': str(exc)}
    sys.stdout.reconfigure(encoding='utf-8')
    if args and args.display == '--human':
        print(error['message'] if error else data['output']['file'])
    else:
        print(json.dumps(envelope(COMMAND, data=None if error else data, error=error), ensure_ascii=False))
    return CODES[error['code']] if error else 0


if __name__ == '__main__':
    if '--background' in sys.argv and MARKER in sys.argv:
        worker(*(Path(arg) for arg in sys.argv[sys.argv.index(MARKER) + 1:]))
    else:
        sys.exit(main())
