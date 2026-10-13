"""Versioned, lossless temporal receipts. Pure JSON representation; no bpy/cache.

project(full, definitions) registers deep-copied typed canonical definitions.
inflate(receipt, definitions) validates every supplied definition and returns the
exact full JSON value. Runtime callers must still evaluate/validate all semantics
and reject unexpected definition drift; this module grants no reuse eligibility.
"""
import copy
import hashlib
import json
import re
import math

VERSION = '1.0'
KINDS = frozenset(('animation-proof', 'source-declaration', 'resolved-media'))
FULL_KEYS = frozenset(('scene_frame', 'reference_frame', 'animation', 'temporal_bindings'))
RECEIPT_KEYS = FULL_KEYS | {'temporal_projection_version'}
BINDING_KEYS = frozenset(('node_id', 'declaration', 'resolved_media', 'active', 'scene_frame'))


def fresh_verification_budget(temporal_count, output_count):
    """Bound verification by declared work; retain the historical static budget."""
    if type(temporal_count) is not int or not 0 <= temporal_count <= 300 or type(output_count) is not int or not 0 <= output_count <= 1200:
        raise TemporalProjectionError('Fresh reader declared work exceeds the finite temporal/output contract')
    seconds = 180 if temporal_count == 0 else min(1200, max(180, math.ceil(180 + 2 * temporal_count + .12 * output_count)))
    return {'policy': 'declared_temporal_work_v1' if temporal_count else 'historical_static_180_v1',
        'timeout_seconds': seconds, 'temporal_count': temporal_count, 'output_count': output_count,
        'scope': 'Bounded fresh-process wait, scaled by complete declared evaluations and output files; no sampling or omitted verification; caller job timeout/cancel remains authoritative'}


def validate_fresh_verification_budget(declared, temporal_count, output_count):
    """Reject bool/float substitutions as well as changed count/policy fields."""
    actual = fresh_verification_budget(temporal_count, output_count)
    if _canonical(declared) != _canonical(actual):
        raise TemporalProjectionError('Fresh budget metadata must match exact canonical types and complete declared work')
    return actual


class TemporalProjectionError(ValueError):
    """Malformed/version-mismatched or altered evidence, never a soft fallback."""


def _fail(message):
    raise TemporalProjectionError(message)


def _canonical(value):
    def validate(item, ancestors):
        if type(item) in (dict, list):
            if id(item) in ancestors:
                _fail('Cyclic JSON evidence')
            nested = ancestors | {id(item)}
            if type(item) is dict:
                if any(type(k) is not str for k in item):
                    _fail('JSON evidence keys must be strings')
                values = item.values()
            else:
                values = item
            for child in values:
                validate(child, nested)
        elif type(item) not in (str, int, float, bool, type(None)):
            _fail('Evidence must contain only ordinary JSON values')
    validate(value, set())
    try:
        return json.dumps(value, sort_keys=True, ensure_ascii=False,
                          separators=(',', ':'), allow_nan=False).encode('utf-8')
    except (TypeError, ValueError, OverflowError) as error:
        _fail('Evidence is not finite canonical JSON: ' + str(error))


def _shape(value, keys, label):
    if type(value) is not dict or set(value) != set(keys):
        _fail(label + ' fields must match the versioned contract exactly')


def _frame(value, label):
    if type(value) is not int:
        _fail(label + ' must be an integer; bool is not a frame')


def _key(kind, value):
    if kind not in KINDS:
        _fail('Unknown canonical definition kind')
    payload = _canonical({'kind': kind, 'value': value})
    return kind + ':' + hashlib.sha256(payload).hexdigest()


def _bank(definitions):
    if type(definitions) is not dict:
        _fail('Definitions must be an ordinary JSON object')
    for key, value in definitions.items():
        if type(key) is not str or ':' not in key:
            _fail('Canonical key needs its kind tag and SHA256')
        kind, sha = key.split(':', 1)
        if kind not in KINDS or re.fullmatch('[0-9a-f]{64}', sha) is None:
            _fail('Unknown/invalid typed canonical key')
        if type(value) is not dict or _key(kind, value) != key:
            _fail('Canonical definition SHA/content differs: ' + key)


def _binding(value):
    _shape(value, BINDING_KEYS, 'Temporal binding')
    if type(value['node_id']) is not str or not value['node_id']:
        _fail('Temporal binding requires an explicit node_id')
    _frame(value['scene_frame'], 'Binding scene_frame')
    if type(value['active']) is not dict:
        _fail('Active mapping must be an explicit complete JSON object')


def project(value, definitions):
    """Losslessly project a full current core value; registration is all-or-none."""
    _bank(definitions)
    _shape(value, FULL_KEYS, 'Full temporal value')
    _frame(value['scene_frame'], 'scene_frame')
    _frame(value['reference_frame'], 'reference_frame')
    if type(value['animation']) is not dict or type(value['temporal_bindings']) is not list:
        _fail('Animation must be an object and temporal_bindings a list')
    _canonical(value)
    for binding in value['temporal_bindings']:
        _binding(binding)
        if type(binding['declaration']) is not dict or type(binding['resolved_media']) is not dict:
            _fail('Source declaration/resolved identity must be explicit objects')
    pending = {}
    def register(kind, payload):
        key = _key(kind, payload)
        existing = definitions if key in definitions else pending
        if key in existing and _canonical(existing[key]) != _canonical(payload):
            _fail('Registered canonical content differs from its hash')
        if key not in definitions:
            pending[key] = copy.deepcopy(payload)
        return key
    animation = value['animation']
    static = {k: v for k, v in animation.items() if k != 'evaluated'}
    if not static:
        _fail('Animation proof is missing, not inferred from evaluated values')
    small_animation = {'definition': register('animation-proof', static)}
    if 'evaluated' in animation:
        small_animation['evaluated'] = copy.deepcopy(animation['evaluated'])
    bindings = []
    for binding in value['temporal_bindings']:
        bindings.append({'node_id': binding['node_id'],
            'declaration': register('source-declaration', binding['declaration']),
            'resolved_media': register('resolved-media', binding['resolved_media']),
            'active': copy.deepcopy(binding['active']), 'scene_frame': binding['scene_frame']})
    receipt = {'temporal_projection_version': VERSION, 'scene_frame': value['scene_frame'],
        'reference_frame': value['reference_frame'], 'animation': small_animation,
        'temporal_bindings': bindings}
    definitions.update(pending)
    return receipt


def inflate(receipt, definitions):
    """Validate typed SHA anchors and reconstruct without aliases or guessing."""
    _bank(definitions)
    _shape(receipt, RECEIPT_KEYS, 'Temporal receipt')
    if receipt['temporal_projection_version'] != VERSION:
        _fail('Unknown temporal receipt version')
    _frame(receipt['scene_frame'], 'scene_frame')
    _frame(receipt['reference_frame'], 'reference_frame')
    _canonical(receipt)
    small_animation = receipt['animation']
    if type(small_animation) is not dict or 'definition' not in small_animation or set(small_animation) - {'definition', 'evaluated'}:
        _fail('Projected animation requires definition and only optional evaluated')
    def reference(key, kind):
        if type(key) is not str or not key.startswith(kind + ':'):
            _fail('Canonical reference kind differs from its field')
        if key not in definitions:
            _fail('Required canonical definition is missing: ' + key)
        return copy.deepcopy(definitions[key])
    animation = reference(small_animation['definition'], 'animation-proof')
    if 'evaluated' in animation:
        _fail('Canonical animation proof must exclude frame-evaluated values')
    if 'evaluated' in small_animation:
        animation['evaluated'] = copy.deepcopy(small_animation['evaluated'])
    if type(receipt['temporal_bindings']) is not list:
        _fail('temporal_bindings must be an explicit list')
    bindings = []
    for binding in receipt['temporal_bindings']:
        _binding(binding)
        bindings.append({'node_id': binding['node_id'],
            'declaration': reference(binding['declaration'], 'source-declaration'),
            'resolved_media': reference(binding['resolved_media'], 'resolved-media'),
            'active': copy.deepcopy(binding['active']), 'scene_frame': binding['scene_frame']})
    return {'scene_frame': receipt['scene_frame'], 'reference_frame': receipt['reference_frame'],
            'animation': animation, 'temporal_bindings': bindings}
