"""Replay the native selector, line-function and merge pointer fields."""
from copy import deepcopy
import math

from material_draft_handoff import require
import toon_viewport_lines_contract as contract

KINDS = {'objects': 'binding_object', 'materials': 'binding_material', 'collections': 'binding_collection'}
LINE_TARGETS = {'materials': 'materials', 'draw_materials': 'materials', 'mask_materials': 'materials',
                'draw_objects': 'objects', 'mask_objects': 'objects'}


def normalize(row):
    require(type(row) is dict and row.get('dialog') in ('SELECTOR', 'LINE_FUNCTION', 'MERGE_COLLECTION'),
            'INVALID_REQUEST', 'Choose native SELECTOR, LINE_FUNCTION or MERGE_COLLECTION binding')
    dialog = row['dialog']
    if dialog == 'SELECTOR':
        require(row.keys() == {'dialog', 'group', 'kind', 'target'} and contract.name(row['group']) and row['kind'] in KINDS,
                'INVALID_REQUEST', 'Selector binding needs a group, kind and exact target')
    elif dialog == 'LINE_FUNCTION':
        require(row.keys() == {'dialog', 'container', 'kind', 'target'} and contract.name(row['container']) and
                row['kind'] in LINE_TARGETS, 'INVALID_REQUEST', 'Line-function binding needs a container, kind and exact target')
    else:
        require(row.keys() == {'dialog', 'domain', 'target'} and (row['domain'] is None or contract.name(row['domain'], 128)),
                'INVALID_REQUEST', 'Merge binding needs a named domain or null to remove')
    row['target'] = contract.ref(row['target'], 'binding target')


def slots(scene):
    from toon_workflow.toon_viewport_preview.selector_capture import reference

    settings = scene.toon_viewport_preview
    return {field: reference(block) if (block := getattr(settings, field)) else None for field in KINDS.values()}


def normalize_controls(row):
    require(type(row) is dict and row.keys() == {'container', 'category', 'gates', 'replace', 'color', 'amount'}
            and contract.name(row['container']) and row['category'] in contract.CATEGORIES,
            'INVALID_REQUEST', 'Use the native line-function container and edge category')
    gates = row['gates']
    require(type(gates) is dict and gates.keys() == set(contract.GATES) and all(type(value) is bool for value in gates.values()),
            'INVALID_REQUEST', 'Specify the four native line-function switches')
    value = contract.override_value({'on': row['replace'], 'color': row['color'], 'amount': row['amount']}, 'replacement')
    row.update(replace=value['on'], color=value['color'], amount=value['amount'])


def apply_controls(bpy, scene, row):
    from toon_workflow.toon_viewport_preview import line_function_ui

    before = deepcopy(line_function_ui._rows(scene))
    cid = row['container']
    category = row['category']
    require(cid in before, 'NOT_FOUND', 'Saved line-function container is absent')
    finished = bpy.ops.toon_viewport.line_function_edit(identifier=cid, category=category,
        replace=row['replace'], color=row['color'], amount=row['amount'], **row['gates'])
    after = line_function_ui._rows(scene)
    require(finished == {'FINISHED'} and cid in after, 'CONFLICT', 'Native line-function dialog did not finish')
    override = after[cid]['overrides'][category]
    require(override['on'] is row['replace'] and len(override['color']) == 3
            and all(math.isclose(a, b, abs_tol=1e-6) for a, b in zip(override['color'], row['color']))
            and math.isclose(override['amount'], row['amount'], abs_tol=1e-6),
            'VALIDATION_FAILED', 'Native replacement color, toggle or amount differs')
    before[cid]['overrides'][category] = override
    before[cid].update(row['gates'])
    require(after == before, 'VALIDATION_FAILED', 'Native line-function dialog changed a different target or category')
    return {'container': cid, 'category': category, 'gates': row['gates'], 'override': override}


def apply(bpy, context, action):
    from toon_workflow.toon_viewport_preview import line_function_ui, line_hierarchy, selector_ui
    from toon_workflow.toon_viewport_preview.selector_capture import reference
    from toon_workflow.toon_viewport_preview.addon import _preset

    scene = context.scene
    dialog = action['dialog']
    if dialog == 'SELECTOR':
        kind = action['kind']
    elif dialog == 'LINE_FUNCTION':
        kind = LINE_TARGETS[action['kind']]
    else:
        kind = 'collections'
    block = getattr(bpy.data, kind).get(action['target']['name'])
    require(block is not None and reference(block) == action['target'], 'NOT_FOUND', 'Exact local binding target is absent')
    field = KINDS[kind]
    setattr(scene.toon_viewport_preview, field, block)
    if dialog == 'SELECTOR':
        result = bpy.ops.toon_viewport.selector_add(group=action['group'], kind=kind)
        values = selector_ui._seed(scene, action['group'])[kind]
        accepted = action['target'] in values
    elif dialog == 'LINE_FUNCTION':
        result = bpy.ops.toon_viewport.line_function_target(identifier=action['container'], kind=action['kind'])
        values = line_function_ui._rows(scene)[action['container']][action['kind']]
        accepted = action['target'] in values
    else:
        domain = action['domain']
        result = bpy.ops.toon_viewport.merge_edit(action='merge_collection', domain=domain or 'Merge', remove=domain is None)
        values = [row['id'] for row in line_hierarchy.records(_preset(scene)['raw'])['merge_domains']
                  if action['target'] in row['collections']]
        accepted = values == ([domain] if domain else [])
    require(result == {'FINISHED'} and slots(scene)[field] == action['target'] and accepted,
            'VALIDATION_FAILED', 'Native dialog did not apply the chosen binding identity')
    return {'field': field, 'target': action['target'], 'saved_action_values': values}
