"""Replay the native selector dialog without requiring a live UI token."""
import json

from material_draft_handoff import require
import toon_viewport_lines_contract as contract

KINDS = ('objects', 'collections', 'materials')


def validate(row, edit):
    fields = {'group', 'kind', 'query'}
    require(type(row) is dict and fields <= row.keys() <= fields | ({'batch', 'choices'} if edit else set())
            and contract.name(row['group']) and row['kind'] in KINDS and isinstance(row['query'], str)
            and len(row['query']) <= 255 and all(ord(char) >= 32 for char in row['query']),
            'INVALID_REQUEST', 'Use a native group, selector kind and query string')
    if not edit:
        return
    require('batch' in row or 'choices' in row, 'INVALID_REQUEST', 'Select with a native batch action or explicit checkboxes')
    if 'batch' in row:
        require(row['batch'] in ('ALL', 'NONE', 'INVERT'), 'INVALID_REQUEST', 'Unknown native batch selection')
    choices = row.get('choices', [])
    require(type(choices) is list and 0 <= len(choices) <= 4096, 'INVALID_REQUEST', 'Expected bounded picker checkbox edits')
    for choice in choices:
        require(type(choice) is dict and choice.keys() == {'name', 'library', 'selected'} and contract.name(choice['name'])
                and (choice['library'] is None or contract.name(choice['library'], 1024)) and type(choice['selected']) is bool,
                'INVALID_REQUEST', 'Each checkbox needs exact name, library and Boolean selected')
    identities = [(choice['name'], choice['library']) for choice in choices]
    require(len(identities) == len(set(identities)) and ('batch' in row or choices), 'INVALID_REQUEST',
            'Expected unique checkbox identities or an explicit native batch selection')


def run(context, args, *, apply):
    from toon_workflow.toon_viewport_preview import selector_bindings, selector_picker, selector_ui

    token = selector_picker.begin(context, args['group'], args['kind'])
    try:
        stage = selector_picker.state(context)
        stage.query = args['query']
        if 'batch' in args:
            selector_picker.select(context, token, args['batch'])
        indexed = {row.identity: row for row in stage.choices}
        for choice in args.get('choices', []):
            key = selector_bindings.key({'name': choice['name'], 'library': choice['library']})
            row = indexed.get(key)
            require(row is not None and selector_picker.shown(row, stage.query) and (not row.missing or not choice['selected']),
                    'CONFLICT', 'Checkbox is absent, filtered out or no longer selectable: ' + choice['name'])
            row.selected = choice['selected']
        shown, selected = [], []
        for row in stage.choices:
            visible = selector_picker.shown(row, stage.query)
            if visible or row.selected:
                ref = json.loads(row.identity)
                if row.selected:
                    selected.append(ref)
                if visible:
                    shown.append({**ref, 'selected': row.selected, 'missing': row.missing})
        result = {'group': args['group'], 'kind': args['kind'], 'query': stage.query,
                  'total': len(stage.choices), 'visible': shown, 'selected': selected}
        if apply:
            selector_picker.accept(context, token)
            saved = selector_ui._seed(context.scene, args['group'])
            require(saved['enabled'] and {selector_bindings.key(ref) for ref in saved[args['kind']]} ==
                    {selector_bindings.key(ref) for ref in selected}, 'VALIDATION_FAILED',
                    'Native selector acceptance wrote different identities')
        return result
    finally:
        selector_picker.clear(context, token)
