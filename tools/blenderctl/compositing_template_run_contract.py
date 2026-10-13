"""Explicit single-template caller contract; no template/member file read in normalize_params."""
import copy
from pathlib import Path
from protocol import Failure
# The canonical Host contract initializes the local addon path in fresh workers.
import compositing_workflow_contract as _host_contract
from compositing_workflow_addon import contract as c

SOURCE_SCHEMA=getattr(c,'M3B_SOURCE_SCHEMA',c.SOURCE_SCHEMA)
BINDING_SCHEMA=getattr(c,'M3B_BINDING_SCHEMA',c.BINDING_SCHEMA)
WORKFLOW_VERSIONS=['1.0','1.1','1.2']+(['1.3'] if hasattr(c,'M3B_MANIFEST_SCHEMA') else [])
SLOT_KINDS=['image','render_layers','image_sequence']+(['movie'] if hasattr(c,'MOVIE_SCHEMA') else [])
FRAME=c.LEGACY_MANIFEST_SCHEMA['properties']['frame']
CONTEXT_SCHEMA=c.obj({'scene':c.NAME,'view_layer':c.NAME,'workflow_id':c.ID,'reference_frame':FRAME},
    ('scene','view_layer','workflow_id','reference_frame'))
BINDINGS_SCHEMA={'type':'object','maxProperties':128,'additionalProperties':copy.deepcopy(BINDING_SCHEMA),
    'propertyNames':copy.deepcopy(c.ID)}
TEMPLATE_RUN_SCHEMA=c.obj({'file':c.TEXT,'expected_sha256':c.SHA,'template':c.FILE,'context':CONTEXT_SCHEMA,
    'source':SOURCE_SCHEMA,'bindings':BINDINGS_SCHEMA,'resources':c.RESOURCES,'preview':c.PREVIEW_SCHEMA,
    'resume':{**copy.deepcopy(c.FILE),'description':"Resume descriptor points to the prior outer template-run-report.json. The worker authenticates its owning terminal parent, resolves the exact normal_run_report at that parent/compositing-report.json with its SHA and native signature, then passes that native checkpoint to the unchanged normal compositing Host. Do not provide a native report or draft authority as the outer resume descriptor."},'implementation_sha256':c.SHA},
    ('file','expected_sha256','template','context','source','bindings','resources','preview'))

# Validate the detached data actually emitted by template-save. Historic producer
# identities are provenance, not new-instance resource dependencies.
SLOT_SCHEMA=c.obj({'kind':{'enum':SLOT_KINDS},
    'node_type':{'enum':['CompositorNodeImage','CompositorNodeRLayers']},'requires_layer':c.BOOL},
    ('kind','node_type','requires_layer'))
DETACHED_NODE_SCHEMA=c.obj({'id':c.ID,'type':{'enum':list(c.NODE_TYPES)},
    'inputs':c.FIELDS,'properties':c.FIELDS,'outputs':c.FIELDS},('id','type'))
DETACHED_GRAPH_SCHEMA=c.obj({'nodes':{'type':'array','minItems':1,'maxItems':128,'items':DETACHED_NODE_SCHEMA},
    'links':{'type':'array','minItems':1,'maxItems':256,'items':c.LINK_SCHEMA}},('nodes','links'))
RECIPE_SCHEMA=c.obj({'workflow_version':{'enum':WORKFLOW_VERSIONS},'components':copy.deepcopy(c.M3_MANIFEST_SCHEMA['properties']['components']),
    'graph':DETACHED_GRAPH_SCHEMA,'animation':c.ANIMATION_SCHEMA},('workflow_version',))
HISTORICAL_RUNTIME_SCHEMA=c.obj({'version':c.NAME,'version_tuple':{'type':'array','minItems':3,'maxItems':3,
    'items':{'type':'integer','minimum':0,'maximum':9999}},'build':c.NAME,'binary_sha256':c.SHA},
    ('version','version_tuple','build','binary_sha256'))
TEMPLATE_DOCUMENT_SCHEMA=c.obj({'template_version':{'const':'1.0'},'template_id':c.ID,'recipe':RECIPE_SCHEMA,
    'source_slot':c.obj({'required':{'type':'boolean','const':True}},('required',)),
    'binding_slots':{'type':'object','maxProperties':128,'additionalProperties':SLOT_SCHEMA,'propertyNames':c.ID},
    'provenance':c.obj({'producer':c.FILE,'implementation_sha256':c.SHA,
        'runtime':HISTORICAL_RUNTIME_SCHEMA,
        'candidate':c.obj({'file':c.TEXT,'sha256':c.SHA},('file','sha256')),'scope':c.TEXT},
        ('producer','implementation_sha256','runtime','candidate','scope'))},
    ('template_version','template_id','recipe','source_slot','binding_slots','provenance'))

def fail(code,message,details=None):
    error=Failure(code,message);error.details=details;raise error

def checked(value,schema):
    try:c._json(value);c.validate(value,schema)
    except c.WorkflowError as exc:fail(exc.code,str(exc),exc.details)
    return copy.deepcopy(value)

def descriptor(value):
    row=checked(value,c.FILE)
    if not Path(row['file']).is_absolute():fail('INVALID_REQUEST','Template-run descriptors require absolute paths')
    row['file']=str(Path(row['file']).resolve());return row

def _same_path(left,right):return str(Path(left).resolve()).casefold()==str(Path(right).resolve()).casefold()

def _append_template(resources,template):
    rows=[descriptor(r) for r in resources];keys=[r['file'].casefold() for r in rows]
    if len(set(keys))!=len(keys):fail('INVALID_REQUEST','Duplicate explicit resource paths are forbidden')
    matching=[r for r in rows if _same_path(r['file'],template['file'])]
    if matching:
        if matching[0]['expected_sha256']!=template['expected_sha256']:fail('CONFLICT','Template resource SHA conflicts with descriptor')
    else:rows.append(copy.deepcopy(template))
    checked(rows,c.RESOURCES)
    return rows

def _semantic_probe(value):
    """Pure shape/domain/resource validation against the existing single-run contract.

    The temporary graph is never persisted, evaluated or sent to a producer. The
    actual recipe/version remains unknown until protected template read in worker.
    """
    context=value['context']
    media=[value['source'],*value['bindings'].values()]
    version='1.3' if any(row.get('kind')=='movie' for row in media) else '1.2'
    manifest={'workflow_version':version,'workflow_id':context['workflow_id'],'scene':context['scene'],
        'view_layer':context['view_layer'],'operation':'create','frame':context['reference_frame'],
        'reference_frame':context['reference_frame'],'source':copy.deepcopy(value['source'])}
    if value['bindings']:
        manifest['graph']={'nodes':[{'id':key,'type':'CompositorNodeRLayers' if binding['kind']=='render_layers' else 'CompositorNodeImage',
            'binding':copy.deepcopy(binding)} for key,binding in value['bindings'].items()],
            'links':[{'from_node':'$input','from_socket':'Image','to_node':'$output','to_socket':'Image'}]}
    else:manifest['components']=[{'id':'template-contract-only','kind':'exposure','params':{'ev':0}}]
    request={'file':value['file'],'expected_sha256':value['expected_sha256'],'resources':value['resources'],
        'manifest':manifest,'preview':value['preview']}
    try:return c.normalize_run_params(request)
    except c.WorkflowError as exc:fail(exc.code,str(exc),exc.details)

def normalize_params(value):
    """Pure caller normalization, independent of template contents and source bytes."""
    out=checked(value,TEMPLATE_RUN_SCHEMA)
    source=descriptor({'file':out['file'],'expected_sha256':out['expected_sha256']})
    out.update(source);out['template']=descriptor(out['template'])
    keys=list(out['bindings'])
    if len({k.casefold() for k in keys})!=len(keys):fail('INVALID_REQUEST','Binding slot IDs must be casefold unique')
    for key,binding in out['bindings'].items():
        checked(key,c.ID);checked(binding,BINDING_SCHEMA)
    out['resources']=_append_template(out['resources'],out['template'])
    probe=_semantic_probe(out)
    out['source']=probe['manifest']['source'];out['preview']=probe['preview']
    if out['bindings']:
        out['bindings']={n['id']:n['binding'] for n in probe['manifest']['graph']['nodes']}
    # Preserve explicit old-version boundary: worker validates real recipe/version
    # via instantiate_from_file + unchanged single Host normalizer.
    if 'resume' in out:out['resume']=descriptor(out['resume'])
    return out

def normalize_host_params(value):
    from compositing_identity import implementation_sha256
    out=normalize_params(value);current=implementation_sha256()
    if out.get('implementation_sha256',current)!=current:fail('CONFLICT','Template-run implementation changed')
    out['implementation_sha256']=current
    return out

def normalize_command(command,value):
    if command!='compositing.template-run':fail('INVALID_REQUEST','Unknown template-run command')
    return normalize_host_params(value)

def document_projection(params):
    """Only input descriptors are consumed; this is not an instantiated manifest."""
    out={'resources':copy.deepcopy(params['resources']),'preview':copy.deepcopy(params['preview']),
        'manifest':{'source':copy.deepcopy(params['source']),
            'graph':{'nodes':[{'binding':copy.deepcopy(b)} for b in params['bindings'].values()]}}}
    # Outer template-run reports are authority anchors, never native run reports.
    # Worker authority validation resolves their exact normal_run_report before
    # the normal Host expands and consumes that native recovery closure.
    return out

def anchor_documents(params,command=None):
    out=normalize_params(params)
    return [out['template'],*([out['resume']] if 'resume' in out else [])]

def input_documents(params,command=None):
    from compositing_workflow_contract import input_documents as single_inputs
    out=normalize_params(params)
    rows=[{'file':out['file'],'expected_sha256':out['expected_sha256']},out['template'],
        *([out['resume']] if 'resume' in out else []),
        *single_inputs(document_projection(out))]
    merged={}
    for row in rows:
        row=descriptor(row);key=row['file'].casefold()
        if key in merged and merged[key]['expected_sha256']!=row['expected_sha256']:fail('CONFLICT','Conflicting template-run input SHA')
        merged[key]=row
    return list(merged.values())

def validate_template_document(value):
    value=checked(value,TEMPLATE_DOCUMENT_SCHEMA);recipe=value['recipe']
    if value['provenance']['scope'].startswith('MOCK_ONLY:'):fail('VALIDATION_FAILED','Mock template cannot certify a real consumer')
    descriptor(value['provenance']['producer'])
    if not Path(value['provenance']['candidate']['file']).is_absolute():fail('INVALID_REQUEST','Historical candidate provenance path must be absolute')
    if ('graph' in recipe)==('components' in recipe):fail('INVALID_REQUEST','Template requires exactly one complete graph/component recipe')
    if recipe['workflow_version'] not in ('1.2','1.3') and 'animation' in recipe:fail('INVALID_REQUEST','Template animation requires its original explicit 1.2 version')
    slots=value['binding_slots'];nodes={n['id']:n for n in recipe.get('graph',{}).get('nodes',[])}
    if len(nodes)!=len(recipe.get('graph',{}).get('nodes',[])):fail('INVALID_REQUEST','Template graph node IDs are duplicated')
    required={key for key,node in nodes.items() if node['type'] in {'CompositorNodeImage','CompositorNodeRLayers'}}
    if set(slots)!=required:fail('INVALID_REQUEST','Detached template must declare every source node binding slot, with no extra slots')
    for key,slot in slots.items():
        checked(key,c.ID);checked(slot,SLOT_SCHEMA)
        if slot['node_type']!=nodes[key]['type']:fail('INVALID_REQUEST','Template slot node_type differs from detached graph')
        if slot['kind']=='render_layers':
            if slot['node_type']!='CompositorNodeRLayers' or slot['requires_layer']:fail('INVALID_REQUEST','Render Layers slot shape differs')
        elif slot['node_type']!='CompositorNodeImage':fail('INVALID_REQUEST','Image/sequence slot requires CompositorNodeImage')
        if slot['kind'] in ('image_sequence','movie') and slot['requires_layer']:fail('UNSUPPORTED','Temporal template slots cannot require multilayer selection')
    return value

def schemas():
    return {'template-run':TEMPLATE_RUN_SCHEMA,'template-document':TEMPLATE_DOCUMENT_SCHEMA}
