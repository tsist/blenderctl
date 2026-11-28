# SPDX-License-Identifier: GPL-3.0-or-later
"""Painted parent declarations for live acceptance and historical inspection.

Pure Expected serialization never discovers sources or grants parent authority.
The host reconstructs bindings from held pre-numeric inputs in validate_prepared.
"""
import copy
from dataclasses import fields,is_dataclass
import ntpath
import os
from pathlib import Path

from protocol import Failure
import toon_image_prepared_run as legacy
from toon_selected_image_prepared_run import _native,_fields,_same,_sha,_canonical,_need
from toon_image_backend import painted_image_receipt_reader as reader

CONTEXT_FIELDS=('scene','view_layer','frame','camera_name','occluder_object_names','member_object_names')
AUTHORED='toon-painted-authored-image-request.v1'
INTERSECTION='toon-painted-intersection-image-request.v1'


def split_params(params):
    """Only normalized queued public declarations; no producer selection IO."""
    recipe=params['recipe']
    _same(recipe['profile'],'painted-mesh-image.v1','Painted public profile required')
    _same(recipe['member_object_names'],recipe['occluder_object_names'],'Painted members must equal occluders')
    export=dict(schema='toon-selected-saved-export-request.v1',file=params['file'],
        expected_sha256=params['expected_sha256'],resources=copy.deepcopy(params['resources']),source_mode=params['source_mode'],
        context={k:copy.deepcopy(recipe[k]) for k in CONTEXT_FIELDS},export_limits=copy.deepcopy(recipe['export_limits']))
    numeric=copy.deepcopy(recipe['producer'])
    _native(export);_native(numeric)
    _need(type(numeric) is dict and numeric.get('schema') in (AUTHORED,INTERSECTION),'Explicit painted producer required','INVALID_REQUEST')
    return export,numeric


def expected_document(expected,checkpoint=lambda:None):
    _need(type(expected) is reader.ExpectedPaintedRun,'Painted Expected type differs')
    return {f.name:(getattr(expected,f.name).decode('utf-8',errors='strict') if f.name=='request_utf8'
        else reader.thaw(getattr(expected,f.name),checkpoint)) for f in fields(reader.ExpectedPaintedRun)}


def expected_from_document(raw,checkpoint=lambda:None):
    """Rebuild immutable declarations, recompute context and check fixed roles."""
    _native(raw)
    _fields(raw,tuple(f.name for f in fields(reader.ExpectedPaintedRun)),'Exact painted Expected fields required')
    _need(type(raw['request_utf8']) is str,'Expected request UTF-8 text required')
    try:data=raw['request_utf8'].encode('utf-8',errors='strict')
    except UnicodeError as error:raise Failure('VALIDATION_FAILED','Expected request Unicode differs') from error
    _need(0<len(data)<=1024**2,'Expected request byte budget','RESOURCE_LIMIT')
    request=legacy.strict_json(data)
    _need(type(request) is dict and request.get('schema') in (AUTHORED,INTERSECTION),'Expected painted schema differs','UNSUPPORTED')
    selected=request['schema']==AUTHORED
    _fields(request,('schema','input','groups','settings')+tuple(k for k in
        (('budgets',) if selected else ('limits','pipeline_budgets')) if k in request),'Exact painted request fields required')
    _need(_canonical(request)==data,'Expected request must use actual canonical bytes')
    _same(raw['receipt_schema'],reader.SELECTED if selected else reader.INTERSECTION,'Expected receipt/producer schema differs')
    _fields(request['input'],('input_path','input_sha256'),'Exact Expected input required')
    _same(request['input'],dict(input_path=raw['source_input_path'],input_sha256=raw['source_input_sha256']),'Expected request/input differs')
    root=legacy.local_path(raw['request_path']).parent
    _same(raw['request_path'],str(root/'backend-request.json'),'Expected request fixed role differs')
    _same(raw['bundle_path'],str(root/'bundle'),'Expected bundle fixed role differs')
    _same(raw['source_input_path'],str(root/'export/geometry/toon-image-input.json'),'Expected source-input fixed role differs')
    hashes=raw['input_hashes']
    _need(type(hashes) is dict and 2<=len(hashes)<=65,'Expected complete input hash dictionary required')
    paths={str(root/'export/geometry/toon-image-input.json')}|{
        str(root/'export/geometry'/('export-%06d.npz'%i)) for i in range(len(hashes)-1)}
    _same(set(hashes),paths,'Expected input hashes escaped complete fixed geometry roles')
    _need(type(raw['image_shape']) is list,'Expected image shape native array required')
    result=reader.normalize_expected_painted_run(host_context_sha256=raw['host_context_sha256'],receipt_schema=raw['receipt_schema'],
        bundle_path=raw['bundle_path'],request_path=raw['request_path'],request_utf8=data,source_input_path=raw['source_input_path'],
        source_input_sha256=raw['source_input_sha256'],input_hashes=hashes,code_identity=raw['code_identity'],
        image_shape=raw['image_shape'],checkpoint=checkpoint)
    _same(expected_document(result,checkpoint),raw,'Rebuilt Expected request/extent/context differs')
    return result


def cold_document(cold,checkpoint=lambda:None):
    _need(type(cold) is reader.PaintedColdReport,'Painted cold report type differs')
    def visit(value):
        checkpoint()
        if type(value) is reader.FrozenObject:return reader.thaw(value,checkpoint)
        if is_dataclass(value):return {f.name:visit(getattr(value,f.name)) for f in fields(value)}
        if type(value) is tuple:return [visit(v) for v in value]
        _need(value is None or type(value) in (str,int,float,bool),'Cold report basic field required')
        return value
    result=visit(cold)
    _native(result)
    return result


def validate_prepared(*,root,prepared,numeric_recipe,context,runtime,exported,
        dependency_pin,backend_sources,tools,held):
    """Reconstruct Expected from queued original recipe and held capture/code."""
    root=legacy.local_path(str(root));tools=Path(tools)
    _need(prepared.request_path==str(root/'backend-request.json') and prepared.expected_path==str(root/'EXPECTED.json') and
        prepared.bundle_path==str(root/'bundle'),'Prepared fixed roles differ')
    cap=8*1024**2
    _same(context.get('schema'),'toon-painted-image-host-context.v1','Parent context schema differs')
    _same(context.get('run_root'),str(root),'Parent context root differs')
    _same(context.get('numeric_recipe_sha256'),_sha(_canonical(numeric_recipe)),'Parent numeric recipe SHA differs')
    capture=held.document(root/'export/capture.json',exported['capture']['expected_sha256'],cap)
    _same(capture.get('schema'),'toon-painted-image-saved-capture.v1','Painted capture schema differs')
    _same(context.get('export_request_sha256'),_sha(_canonical(capture['declared_request'])),'Parent export request SHA differs')
    request=copy.deepcopy(numeric_recipe)
    request['input']=dict(input_path=str(root/'export/geometry/toon-image-input.json'),input_sha256=capture['input']['expected_sha256'])
    request_bytes=_canonical(request);request_sha=_sha(request_bytes)
    _same(request_sha,prepared.request_sha256,'Prepared request differs from original recipe')
    held.file(prepared.request_path,request_sha,maximum=cap,size=len(request_bytes))
    _same(held.document(prepared.request_path,request_sha,cap),request,'Prepared request document differs')
    selected=numeric_recipe['schema']==AUTHORED
    _need(selected or numeric_recipe['schema']==INTERSECTION,'Explicit painted schema required')
    code={}
    for name,pin in backend_sources.items():
        path=tools/'toon_image_backend'/name
        row=held.files[ntpath.normcase(str(path))]
        _same(row['sha256'],pin,'Held backend pin differs')
        code[str(path)]=dict(bytes=row['bytes'],sha256=pin) if selected else pin
    sealed=dict(schema='toon-painted-image-numeric-context.v1',parent=context,
        export_request_sha256=context['export_request_sha256'],numeric_recipe_sha256=context['numeric_recipe_sha256'],
        capture_sha256=exported['capture']['expected_sha256'],dependency_audit_sha256=exported['dependency_audit']['expected_sha256'],
        dependency_implementation_sha256=dependency_pin,scene_export_sha256=capture['scene_export']['expected_sha256'],
        input_sha256=capture['input']['expected_sha256'],backend_request_sha256=request_sha,runtime=runtime,backend_sources=code)
    encoded=_canonical(sealed)
    _same(_sha(encoded),prepared.host_context_sha256,'Independent full numeric context differs')
    held.file(root/'NUMERIC-CONTEXT.json',prepared.host_context_sha256,maximum=cap,size=len(encoded))
    _same(held.document(root/'NUMERIC-CONTEXT.json',prepared.host_context_sha256,cap),sealed,'Held numeric context document differs')
    raw=held.document(prepared.expected_path,prepared.expected_sha256,cap)
    original=expected_document(prepared.expected_run,held.checkpoint)
    _same(raw,original,'Disk Expected differs from sealed object')
    _same(_sha(_canonical(raw)),prepared.expected_sha256,'Expected document digest differs')
    expected=expected_from_document(raw,held.checkpoint)
    hashes={str(root/'export/geometry/toon-image-input.json'):capture['input']['expected_sha256']}
    for item in capture['npz']:
        hashes[item['file']['file']]=item['file']['expected_sha256']
    _same(reader.thaw(expected.input_hashes,held.checkpoint),hashes,'Expected original input locks differ')
    _same(reader.thaw(expected.code_identity,held.checkpoint),code,'Expected approved code identity differs')
    _same(list(expected.image_shape),[capture['height'],capture['width'],4],'Expected image dimensions differ')
    _need(expected.request_utf8==request_bytes,'Expected canonical request differs')
    _same(expected.host_context_sha256,prepared.host_context_sha256,'Expected sealed context differs')
    _need(not os.path.lexists(root/'bundle'),'Bundle appeared before independent preparation validation')
    return expected
