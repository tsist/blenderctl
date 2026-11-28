"""Public transport and offline discovery for the embedded hard-surface extension."""

import json
import sys
from pathlib import Path

from protocol import Failure
from scene_contract import NAME, BOOL, array, integer, obj, enum, number, validate


TOOLS = Path(__file__).resolve().parent.parent
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))
from hard_surface_workflow_addon.selector_spec import SELECTOR_SCHEMA
from hard_surface_workflow_addon import face_flow_spec

FACE_FLOW = {
    **obj({'object': NAME, **face_flow_spec.PROPERTIES}, ['object', 'layer']),
    'anyOf': [{'required': ['trace']}, {'required': ['scan']}],
    'dependentRequired': {'trace': ['reference']},
}


def normalize_face_flow(value):
    validate(value, FACE_FLOW, '$.params.face_flow')
    try:
        return {'object': value['object'], **face_flow_spec.normalize({
            key: entry for key, entry in value.items() if key != 'object'
        })}
    except ValueError as exc:
        raise Failure('INVALID_REQUEST', str(exc)) from exc


OPEN = {'type':'object', 'additionalProperties':True}
SURFACE_SWEEP = obj({'object': NAME, 'params': OPEN})
FLOW = obj({'op':{'const':'mesh.flow'}, 'object':NAME, 'tool':NAME, 'selector':SELECTOR_SCHEMA,
            'params':OPEN, 'repeat':integer(1,64), 'protect':array(SELECTOR_SCHEMA,0,128),
            'allow_expansion':BOOL}, ['op','object','tool','selector'])
STACK = obj({'op':{'const':'hard_surface.stack'}, 'object':NAME, 'tool':NAME, 'params':OPEN},
            ['op','object','tool'])
LOOKUP = obj({'object':NAME, 'selector':SELECTOR_SCHEMA})
AUDIT_PROBE = obj({'objects':array(NAME,1,2), 'point_mm':array(number(),3,3),
                   'direction':array(number(),3,3)})
AUDIT_MM_VECTOR = array(number(-1e9, 1e9), 3, 3)
AUDIT_DISTANCE = number(1e-5, 1e9)
AUDIT_BORE = obj({
    'kind': enum('BORE'), 'object': NAME, 'point_mm': AUDIT_MM_VECTOR,
    'direction': AUDIT_MM_VECTOR, 'stations_mm': array(number(-1e9, 1e9), 1, 16),
    'max_radius_mm': AUDIT_DISTANCE, 'axial_window_mm': array(number(-1e9, 1e9), 2, 2),
    'angular_samples': integer(8, 64), 'through_radius_mm': number(0, 1e9),
}, ['kind', 'object', 'point_mm', 'direction', 'stations_mm', 'max_radius_mm', 'axial_window_mm'])
AUDIT_WALL = obj({
    'kind': enum('WALL'), 'object': NAME, 'point_mm': AUDIT_MM_VECTOR,
    'max_search_mm': AUDIT_DISTANCE, 'max_thickness_mm': AUDIT_DISTANCE,
})
GEOMETRY_AUDIT = obj({'objects':array(NAME,1,16), 'representations':array(enum('SOURCE','EVALUATED'),1,2),
                      'self_intersection':BOOL, 'probes':array(AUDIT_PROBE,0,128),
                      'pairs':array(array(NAME,2,2),0,32),
                      'measurements':array({'oneOf':[AUDIT_BORE,AUDIT_WALL]},0,32),
                      'max_triangles':integer(1,300000)}, ['objects'])

# Kept in sync with hard_surface_workflow_addon.review_grid. The add-on module
# imports Blender, so the public CLI validates the same finite vocabulary here
# without loading the add-on outside the worker.
REVIEW_LAYOUTS = {'2x2': 4, '3x3': 9}
REVIEW_SHADINGS = ('CLAY', 'MATERIAL', 'SCENE')
REVIEW_DISPLAYS = ('DEFAULT', 'SOLID', 'CONTROL_CAGE', 'EVALUATED_WIRE')
REVIEW_PROJECTIONS = ('ORTHO', 'PERSP')
REVIEW_DIRECTIONS = ('TOP', 'BOTTOM', 'LEFT', 'RIGHT', 'FRONT', 'REAR',
                     'FRONT_3Q', 'REAR_3Q', 'TOP_3Q', 'WIREFRAME')
REVIEW_ALIASES = {'HERO': 'FRONT_3Q', 'WIRE': 'WIREFRAME'}
REVIEW_VIEW = {'oneOf': [
    {'type': 'string', 'minLength': 1, 'maxLength': 63,
     'pattern': r'^[^\x00-\x1f\x7f]+$'},
    obj({'camera': NAME}),
]}
REVIEW_FRAMING = obj({
    'center': array(number(-1e9, 1e9), 3, 3),
    'ortho_scale': number(1e-5, 1e9),
})
REVIEW_GRID = obj({
    'layout': enum(*REVIEW_LAYOUTS),
    'objects': {**array(NAME, 1, 1000), 'uniqueItems': True},
    'collection': NAME,
    'views': array(REVIEW_VIEW, 1, 9),
    'shading': enum(*REVIEW_SHADINGS),
    'display': enum(*REVIEW_DISPLAYS),
    'subdivision_level': integer(0, 3),
    'projection': enum(*REVIEW_PROJECTIONS),
    'cell_size': integer(128, 1024),
    'margin': number(0.03, 0.5),
    'samples': integer(1, 128),
    'framing': REVIEW_FRAMING,
}, [])


def normalize_review_grid(value):
    """Validate one read-only review-grid request without accepting output paths."""
    validate(value, REVIEW_GRID, '$.params.review_grid')
    params = dict(value)
    if 'objects' in params and 'collection' in params:
        raise Failure('INVALID_REQUEST', 'review_grid accepts objects or collection, not both')
    if 'objects' in params and len(set(params['objects'])) != len(params['objects']):
        raise Failure('INVALID_REQUEST', 'review_grid objects contains a duplicate name')
    layout = params.get('layout', '2x2')
    if 'views' in params:
        expected = REVIEW_LAYOUTS[layout]
        if len(params['views']) != expected:
            raise Failure('INVALID_REQUEST',
                          f'review_grid layout {layout} requires exactly {expected} views')
        for view in params['views']:
            if not isinstance(view, str):
                continue
            preset = REVIEW_ALIASES.get(view.upper(), view.upper())
            if preset not in REVIEW_DIRECTIONS:
                raise Failure('INVALID_REQUEST',
                              f'Unknown review_grid view {view!r}; use a supported direction or {{"camera": name}}')
    if 'framing' in params and params.get('projection', 'ORTHO') != 'ORTHO':
        raise Failure('INVALID_REQUEST', 'review_grid framing requires ORTHO projection')
    if 'subdivision_level' in params and params.get('display') != 'EVALUATED_WIRE':
        raise Failure('INVALID_REQUEST', 'review_grid subdivision_level requires display EVALUATED_WIRE')
    return params


def describe(tool=None, *, review_grid=False, geometry_audit=False, face_flow=False):
    file = TOOLS/'hard_surface_workflow_addon'/'catalog.json'
    if not file.is_file():
        raise Failure('NOT_FOUND','Hard Surface Workflow catalog is missing from the CLI package')
    with file.open(encoding='utf-8') as stream:
        data = json.load(stream)
    edition = {'version': data['version'], 'supported_blender': data['supported_blender']}
    if face_flow:
        return {
            **edition,
            'command': 'model inspect FILE --expected-sha256 SHA --face-flow REQUEST.json',
            'schema': FACE_FLOW,
            'example': {'object': 'Object', 'layer': 'CONTROL_CAGE',
                        'reference': {'source': 'Original tutorial URL or file', 'stage': '00:14:28',
                                      'region': 'Front wheel arch', 'landmark': 'Upper support edge'},
                        'trace': {'kind': 'EDGE_LOOP', 'edge': 42}, 'scan': {}},
            'constraints': {
                'trace': 'Requires all four reference fields. FACE_STRIP requires the face and an incident direction edge. An optional still-image landmark uses top-left pixel coordinates and the still SHA-256.',
                'scan': 'Returns geometric and topological candidates for manual review, with budget and hidden coverage. A scan-only request may omit reference.',
                'mesh_sha256': 'Supply a prior identity.mesh_sha256 when reusing its seed. Changing representation, context or mesh invalidates the binding.',
                'evaluation': 'EVALUATED reads the actual saved viewport dependency graph in Object Mode. It does not force a render or diagnostic subdivision level.',
            },
            'output': 'data.face_flow and face-flow-report.json in the job directory',
        }
    if geometry_audit:
        return {
            **edition,
            'command': 'model inspect FILE --expected-sha256 SHA --geometry-audit REQUEST.json',
            'schema': GEOMETRY_AUDIT,
            'example': {'objects': ['Object'], 'representations': ['SOURCE', 'EVALUATED'],
                        'self_intersection': True},
            'fields': {
                'objects': 'Exact mesh object names in the selected scene and view layer.',
                'representations': 'SOURCE reads the mesh datablock. EVALUATED includes evaluated modifiers.',
                'self_intersection': 'Report triangle interior crossings and positive-area coplanar overlaps separately for each object and representation. Inspect coverage and budget status.',
                'probes': 'Each probe identifies one or two objects and a world-space line by point_mm and direction.',
                'pairs': 'Exact distinct object-name pairs for cross-surface crossings and coplanar overlaps. Check containment and swept motion separately.',
                'measurements': 'BORE estimates the local center, sampled minimum chord and clear or blocked lines in a finite axial window. WALL snaps to the nearest unambiguous surface and measures inward along its world normal. Use world millimetres.',
                'max_triangles': 'Upper bound for each audited mesh representation.',
            },
            'output': 'data.geometry_audit and geometry-audit.json in the job directory',
        }
    if review_grid:
        return {**edition, 'command': 'model inspect FILE --expected-sha256 SHA --review-grid REQUEST.json',
                'schema': REVIEW_GRID, 'view_presets': list(REVIEW_DIRECTIONS), 'view_aliases': REVIEW_ALIASES,
                'layout_cells': REVIEW_LAYOUTS, 'constraints': {
                    'subdivision_level': 'Requires EVALUATED_WIRE; overrides enabled Subdivision Surface modifiers on temporary geometry.',
                    'framing': 'Requires ORTHO projection. Reuse the same center, ortho_scale and views for matched panels.',
                    'display': 'Read views[].display for each tile. In DEFAULT mode a wire tile may include evaluated solids; wireframe.included and omitted describe its objects.',
                    'edge_budget': 'Explicit CONTROL_CAGE and EVALUATED_WIRE require all target edges within 50000 and 100000 edges respectively. An over-budget request fails before export.'},
                'output': 'review-grid.png and review-grid.json in the job directory'}
    if tool is None:
        return {**edition, 'mesh_tools':list(data['mesh_tools']),
                'stack_tools':list(data['stack_tools']), 'selectors':data['selectors'],
                'mesh_operation':FLOW,'stack_operation':STACK,
                'selection_preview':'model inspect --flow-selection <json>',
                'candidate_selection':'model prepare --manifest <json> --flow-selection <json>',
                'saved_panel_controls':'model panel-controls --help',
                'review_grid':'model flow-tools --review-grid',
                'geometry_audit':'model flow-tools --geometry-audit',
                'face_flow':'model flow-tools --face-flow',
                'detail':'model flow-tools --tool <tool id>'}
    for group in ('mesh_tools','stack_tools'):
        if tool in data[group]:
            return {**data[group][tool], **edition}
    raise Failure('INVALID_REQUEST',f'Unknown hard surface tool: {tool}')
