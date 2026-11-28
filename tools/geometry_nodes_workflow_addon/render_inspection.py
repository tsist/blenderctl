"""Evaluate read-only geometry inside a native RENDER dependency graph."""
from time import perf_counter

import bpy

from .contract import WorkflowError
from .inspection import _evaluated, _modifier_timings


def _signature(row):
    return {
        'bounds': row['bounds'],
        'instances': {key: row['instances'][key] for key in ('count', 'references', 'reference_counts')},
        'components': {
            name: {
                **{key: comp[key] for key in ('vertices', 'edges', 'faces', 'loops', 'points', 'curves', 'splines') if key in comp},
                'attributes': [{key: attr[key] for key in ('name', 'type', 'domain', 'count', 'stats') if key in attr}
                               for attr in comp.get('attributes', [])],
            }
            for name, comp in row['components'].items()
        },
    }


def _comparison(viewport, render, timings):
    first, second = _signature(viewport), _signature(render)
    differences = [name for name in first if first[name] != second[name]]
    finite = all(row['attribute_health']['status'] == 'passed' for row in (viewport, render))
    return {
        'status': 'match' if not differences and finite else 'mismatch',
        'compared': ['component topology counts', 'bounds', 'instance counts and references', 'full attribute values'],
        'different_sections': differences,
        'VIEWPORT': {'evaluated': viewport},
        'RENDER': {'evaluated': render, 'modifier_timings': timings},
    }


def compare(rows):
    if not rows:
        return {'status': 'skipped', 'reason': 'No geometry targets'}
    names = sorted({row['object'] for row in rows})
    snapshots = {}
    failure = None

    class GNW_RenderProbe(bpy.types.RenderEngine):
        bl_idname = 'GNW_RENDER_PROBE'
        bl_label = 'Geometry Workflow Render Probe'
        bl_use_preview = False
        bl_use_postprocess = False

        def render(self, depsgraph):
            nonlocal failure
            try:
                if depsgraph.mode != 'RENDER' or depsgraph.view_layer.name != view_layer.name:
                    raise WorkflowError('VALIDATION_FAILED', 'Probe requires a RENDER graph for the selected view layer')
                for name in names:
                    evaluated = _evaluated(bpy.data.objects[name], depsgraph=depsgraph)
                    timings = _modifier_timings(bpy.data.objects[name], depsgraph)
                    snapshots[name] = evaluated, timings
            except Exception as exc:
                failure = exc

    scene, view_layer = bpy.context.scene, bpy.context.view_layer
    original = scene.render.engine
    started = perf_counter()
    bpy.utils.register_class(GNW_RenderProbe)
    try:
        scene.render.engine = GNW_RenderProbe.bl_idname
        result = bpy.ops.render.render(layer=view_layer.name)
    finally:
        scene.render.engine = original
        bpy.utils.unregister_class(GNW_RenderProbe)
    if failure:
        raise WorkflowError('VALIDATION_FAILED', 'RENDER evaluation failed: ' + str(failure)) from failure
    if 'FINISHED' not in result or set(snapshots) != set(names):
        raise WorkflowError('VALIDATION_FAILED', 'RENDER probe did not evaluate every requested target')
    for row in rows:
        row['render_comparison'] = _comparison(row['evaluated'], *snapshots[row['object']])
    mismatch = [row['object'] for row in rows if row['render_comparison']['status'] != 'match']
    return {'status': 'mismatch' if mismatch else 'match', 'different_objects': mismatch,
            'elapsed_seconds': perf_counter() - started, 'backend': 'RenderEngine.render native RENDER dependency graph'}
