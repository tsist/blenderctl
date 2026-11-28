# SPDX-License-Identifier: GPL-3.0-or-later
"""Explicit selected declarations shared by live host and historical inspection."""
from dataclasses import fields, is_dataclass
import hashlib
import ntpath
from pathlib import Path

from protocol import Failure
from toon_image_public import canonical
from toon_image_backend import selected_chain_image_receipt_reader as reader

CONTEXT_FIELDS = ('scene', 'view_layer', 'frame', 'camera_name',
                  'occluder_object_names', 'member_object_names')
NUMERIC_FIELDS = ('selector', 'visible_enabled', 'hidden_enabled', 'chaining', 'style', 'budgets')


def need(value, message):
    if not value:
        raise Failure('CONFLICT', message)


def split_params(params):
    """Only normalized queued declarations belong here; no filesystem access."""
    recipe = params['recipe']
    export = dict(schema='toon-selected-saved-export-request.v1',
        file=params['file'], expected_sha256=params['expected_sha256'], resources=params['resources'],
        source_mode=params['source_mode'], context={k: recipe[k] for k in CONTEXT_FIELDS},
        export_limits=recipe['export_limits'])
    numeric = {k: recipe[k] for k in NUMERIC_FIELDS}
    return export, numeric


def expected_document(expected, checkpoint):
    need(type(expected) is reader.ExpectedSelectedRun, 'Selected Expected type differs')
    return {f.name: (getattr(expected, f.name).decode('utf-8', errors='strict')
        if f.name == 'canonical_request_utf8' else reader.thaw(getattr(expected, f.name), checkpoint))
        for f in fields(reader.ExpectedSelectedRun)}


def expected_from_document(raw):
    need(type(raw) is dict and set(raw) == {f.name for f in fields(reader.ExpectedSelectedRun)},
         'Exact seventeen-field Selected Expected document required')
    native = {'host_context_sha256', 'request_path', 'request_sha256', 'request_bytes', 'source_input_sha256'}
    values = {}
    for field in fields(reader.ExpectedSelectedRun):
        value = raw[field.name]
        if field.name == 'canonical_request_utf8':
            need(type(value) is str, 'Expected canonical request must be UTF-8 text')
            try:
                value = value.encode('utf-8', errors='strict')
            except UnicodeError as error:
                raise Failure('VALIDATION_FAILED', 'Expected request has invalid Unicode') from error
        elif field.name == 'image_shape':
            need(type(value) is list, 'Expected image shape must be a native JSON array')
            value = tuple(value)
        elif field.name not in native:
            value = reader.freeze_declaration(value)
        values[field.name] = value
    return reader.ExpectedSelectedRun(**values)


def cold_document(cold, checkpoint):
    need(type(cold) is reader.SelectedColdReport, 'Selected cold report type differs')
    def visit(value):
        checkpoint()
        if type(value) is reader.FrozenObject:
            return reader.thaw(value, checkpoint)
        if is_dataclass(value):
            return {f.name: visit(getattr(value, f.name)) for f in fields(value)}
        if type(value) is tuple:
            return [visit(item) for item in value]
        return value
    return visit(cold)


def validate_prepared(*, root, prepared, numeric_recipe, context, runtime, exported,
                      dependency_pin, backend_sources, tools, held):
    """Consume disk Expected, bound independently to the queued pre-numeric inputs."""
    need(prepared.request_path == str(root/'backend-request.json') and
         prepared.expected_path == str(root/'EXPECTED.json') and
         prepared.bundle_path == str(root/'bundle'), 'Prepared fixed paths differ')
    cap = 8*1024**2
    capture = held.document(root/'export/capture.json', exported['capture']['expected_sha256'], cap)
    request = dict(schema='toon-selected-mesh-chain-image-request.v1',
        input=dict(input_path=str(root/'export/geometry/toon-image-input.json'),
                   input_sha256=capture['input']['expected_sha256']),
        selector=numeric_recipe['selector'],
        policy=dict(model='selected_mesh_sources_all_nondegenerate_two_sided.v1',
            member_object_ids=capture['member_object_ids'], visible_enabled=numeric_recipe['visible_enabled'],
            hidden_enabled=numeric_recipe['hidden_enabled']),
        chaining=numeric_recipe['chaining'], style=numeric_recipe['style'], budgets=numeric_recipe['budgets'])
    request_bytes = canonical(request)
    request_sha = hashlib.sha256(request_bytes).hexdigest()
    need(request_sha == prepared.request_sha256, 'Prepared request differs from original recipe')
    held.file(prepared.request_path, request_sha, maximum=cap, size=len(request_bytes))
    need(canonical(held.document(prepared.request_path, request_sha, cap)) == request_bytes,
         'Prepared request canonical document differs')
    pins = {}
    for name, pin in backend_sources.items():
        path = tools/'toon_image_backend'/name
        row = held.files[ntpath.normcase(str(path))]
        need(row['sha256'] == pin, 'Held backend pin differs')
        pins[str(path)] = dict(bytes=row['bytes'], sha256=pin)
    numeric_context = dict(schema='toon-selected-image-numeric-context.v1', parent=context,
        export_request_sha256=context['export_request_sha256'], numeric_recipe_sha256=context['numeric_recipe_sha256'],
        capture_sha256=exported['capture']['expected_sha256'],
        dependency_audit_sha256=exported['dependency_audit']['expected_sha256'],
        dependency_implementation_sha256=dependency_pin,
        scene_export_sha256=capture['scene_export']['expected_sha256'],
        input_sha256=capture['input']['expected_sha256'], backend_request_sha256=request_sha,
        runtime=runtime, backend_sources=pins)
    encoded = canonical(numeric_context)
    need(hashlib.sha256(encoded).hexdigest() == prepared.host_context_sha256,
         'Full numeric context differs from original runtime/export/code declarations')
    held.file(root/'NUMERIC-CONTEXT.json', prepared.host_context_sha256, maximum=cap, size=len(encoded))
    raw = held.document(prepared.expected_path, prepared.expected_sha256, cap)
    original = expected_document(prepared.expected_run, held.checkpoint)
    encoded_expected = canonical(original)
    need(canonical(raw) == encoded_expected and hashlib.sha256(encoded_expected).hexdigest() == prepared.expected_sha256,
         'On-disk Expected is not the exact pre-numeric declaration')
    need(raw['canonical_request_utf8'].encode('utf-8') == request_bytes and
         raw['request_bytes'] == len(request_bytes) and raw['request_sha256'] == request_sha and
         raw['host_context_sha256'] == prepared.host_context_sha256, 'Expected request/context binding differs')
    return expected_from_document(raw)
