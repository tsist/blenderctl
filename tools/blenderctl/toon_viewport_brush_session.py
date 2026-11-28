"""Exact in-window handoff for native Pencil brush and curve drafts."""

import hashlib
import json
import math
import struct
import uuid

from material_draft_handoff import fingerprint, require
from protocol import Failure


PARAMS = {
    'brush-status': {'view_id'},
    'brush-open-author': {'view_id','expected_context','actual'},
    'brush-open-dialog': {'view_id','expected_context','actual'},
    'brush-commit': {'view_id','expected_context','actual'},
    'brush-point': {'view_id','expected_context','editor_id','collection','index'},
    'brush-configure': {'view_id','expected_context','editor_id','patch'},
    'curve-select': {'view_id','expected_context','group','slot'},
    'curve-configure': {'view_id','expected_context','curve_identity','patch'},
    'curve-discard': {'view_id','expected_context','curve_identity'},
    'curve-restore': {'view_id','expected_context','curve_identity'},
}
IMAGE_SLOTS = {'brush','size','alpha','color','distortion'}
REFERENCE_SLOTS = {f'{channel}_{kind}' for channel in ('VISIBLE','HIDDEN') for kind in ('size','alpha')}
SELECTIONS = {
    'section':{'shape','stroke','color','maps','curves','reduction','raster','chain'},
    'map_name':IMAGE_SLOTS,'distance_channel':{'VISIBLE','HIDDEN'},
    'distance_kind':{'size','alpha'},'curve_kind':{'size','alpha'},
}


def block_ref(block):
    return None if block is None else {'name':block.name,'uid':str(block.session_uid)}


def picker_state(bpy, scene, brush, editor):
    pickers = brush.pickers(scene)
    return dict(images={key:block_ref(getattr(pickers,'image_'+key)) for key in sorted(IMAGE_SLOTS)},
                references={key:block_ref(getattr(pickers,'reference_'+key)) for key in sorted(REFERENCE_SLOTS)},
                seam=dict(enabled=editor.seam_on,object=block_ref(pickers.seam_object),edge=editor.seam_edge,
                          endpoint=int(editor.seam_endpoint)),
                choices=dict(images=[block_ref(item) for item in bpy.data.images],
                             objects=[block_ref(item) for item in scene.objects]))


def text_ref(text):
    if text is None:
        return None
    return {'name':text.name_full,'sha256':hashlib.sha256(text.as_string().encode('utf8')).hexdigest()}


def collect(bpy, owner, viewport):
    native = owner['native']
    brush = native.brush_ui
    scene = owner['scene']
    settings = scene.toon_viewport_preview
    draft = bpy.data.texts.get(scene.get('toon_brush_effect_draft',''))
    editor = brush._EDITORS.get(scene.as_pointer())
    editor_state = None
    if editor is not None:
        if owner.get('brush_editor') is not editor:
            owner.update(brush_editor=editor, brush_editor_id=str(uuid.uuid4()))
        editor_state = dict(editor_id=owner['brush_editor_id'],actual=editor.actual,points={
            brush._key(path): [[point.position,point.value] for point in getattr(editor,brush._key(path))]
            for path in brush._CURVES},
            pickers=picker_state(bpy,scene,brush,editor),
            navigation={key:getattr(editor,key) for key in SELECTIONS})
    else:
        owner.pop('brush_editor', None)
    curve = settings.curve_tree
    curve_state = None
    if curve is not None and curve.get('toon_viewport_curve_role') == native.curve_editor.ROLE:
        signature = native.curve_editor.signature(curve,settings)
        curve_state = dict(tree=curve.name,identity=curve['identity'],group=settings.curve_group,slot=settings.curve_slot,
                           original=json.loads(curve['original']), current=json.loads(signature),
                           pending=signature != curve['last_signature'])
    state = dict(view_id=viewport['view_id'], file=viewport['file'],scene=viewport['scene'],view_layer=viewport['view_layer'],
                 window_id=viewport['window_id'],revision=settings.preset_revision,author=text_ref(settings.preset_text),
                 draft=None if draft is None else {**text_ref(draft),'actual':draft.get('actual_brush')},
                 dialog=editor_state,curve=curve_state,curve_target={'group':settings.curve_group,'slot':settings.curve_slot})
    return {**state, 'expected_context':fingerprint(state)}


def checked_ref(value, blocks):
    if value is None:
        return None
    require(isinstance(value,dict) and value.keys()=={'name','uid'} and isinstance(value['name'],str)
            and isinstance(value['uid'],str),'INVALID_REQUEST','Choose a saved Blender datablock by name and UID')
    result = next((row for row in blocks if block_ref(row)==value),None)
    require(result is not None,'NOT_FOUND','Datablock is absent or no longer has this identity: '+value['name'])
    return result


def modal_patch(dialog, requested):
    allowed = {'images','references','seam','point','navigation','active_image','active_reference','active_point'}
    require(isinstance(requested,dict) and 0<len(requested) and requested.keys()<=allowed,
            'INVALID_REQUEST','Use named modal fields or the currently selected editor section')
    patch = dict(requested)
    selections = patch.get('navigation',{})
    require(isinstance(selections,dict) and selections.keys()<=SELECTIONS.keys()
            and all(isinstance(value,str) and value in SELECTIONS[key] for key,value in selections.items()),
            'INVALID_REQUEST','Invalid section or option for the real brush dialog')
    current = {**dialog['navigation'],**selections}
    if 'active_image' in patch:
        require(current['section']=='maps' and 'images' not in patch,'CONFLICT','Select the Maps section and one map role first')
        patch['images']={current['map_name']:patch.pop('active_image')}
    if 'active_reference' in patch:
        require(current['section']=='reduction' and 'references' not in patch,'CONFLICT',
                'Select the Reduction section, channel and quantity first')
        patch['references']={f"{current['distance_channel']}_{current['distance_kind']}":patch.pop('active_reference')}
    if 'active_point' in patch:
        require(current['section'] in ('curves','reduction') and 'point' not in patch,'CONFLICT',
                'Select an along-line or reduction curve first')
        selected = patch.pop('active_point')
        require(isinstance(selected,dict) and 'collection' not in selected,'INVALID_REQUEST',
                'The active dialog selection supplies the curve collection')
        collection = (f"curves__{current['curve_kind']}" if current['section']=='curves'
                      else f"reduction__{current['distance_channel']}__{current['distance_kind']}__curve")
        patch['point']={**selected,'collection':collection}
    return patch


def configure(bpy, scene, brush, editor, dialog, requested):
    patch = modal_patch(dialog,requested)
    picker = brush.pickers(scene)
    operations = [(editor,key,value) for key,value in patch.get('navigation',{}).items()]
    for section, slots, scope, prefix in [('images',IMAGE_SLOTS,bpy.data.images,'image_'),
                                            ('references',REFERENCE_SLOTS,scene.objects,'reference_')]:
        if section not in patch:
            continue
        rows = patch[section]
        require(isinstance(rows,dict) and rows and rows.keys()<=slots,'INVALID_REQUEST','Unknown picker in '+section)
        for key, ref in rows.items():
            item = checked_ref(ref,scope)
            if section=='images' and item is not None:
                width,height = item.size
                require(width>0 and height>0 and width*height<=1048576,'INVALID_REQUEST',
                        'Choose a loaded Pencil image up to 1,048,576 pixels')
            operations.append((picker,prefix+key,item))
    if 'seam' in patch:
        values = patch['seam']
        require(isinstance(values,dict) and values.keys() in ({'enabled'},{'enabled','object','edge','endpoint'})
                and type(values['enabled']) is bool,'INVALID_REQUEST','Seam takes enabled or the full object, edge and endpoint')
        if values['enabled']:
            require(values.keys()=={'enabled','object','edge','endpoint'} and type(values['edge']) is int
                    and type(values['endpoint']) is int and values['endpoint'] in (0,1),
                    'INVALID_REQUEST','An enabled seam needs a mesh, edge index and endpoint 0 or 1')
            obj = checked_ref(values['object'],scene.objects)
            require(obj is not None and obj.type=='MESH','INVALID_REQUEST','Choose a mesh for the seam object')
            mesh = obj.evaluated_get(bpy.context.evaluated_depsgraph_get()).data
            require(0<=values['edge']<len(mesh.edges),
                    'INVALID_REQUEST','Choose an existing mesh edge in the saved seam object')
            operations += [(picker,'seam_object',obj),(editor,'seam_edge',values['edge']),
                           (editor,'seam_endpoint',str(values['endpoint']))]
        else:
            require(values.keys()=={'enabled'},'INVALID_REQUEST','Disable the seam without replacing its staged target')
        operations.append((editor,'seam_on',values['enabled']))
    if 'point' in patch:
        point = patch['point']
        id_fields = {'collection','index'}
        require(isinstance(point,dict) and id_fields < point.keys() <= id_fields | {'position','value'}
                and isinstance(point['collection'],str) and point['collection'] in dialog['points']
                and type(point['index']) is int and 0<=point['index']<len(dialog['points'][point['collection']]),
                'INVALID_REQUEST','Choose an exact existing point from the active curve collection')
        source = [row[:] for row in dialog['points'][point['collection']]]
        native = getattr(editor,point['collection'])[point['index']]
        for index, name, high in ((0,'position',1),(1,'value',64)):
            if name in point:
                value = point[name]
                require(type(value) in (int,float) and 0<=value<=high and math.isfinite(value),
                        'INVALID_REQUEST','Invalid curve '+name)
                value = struct.unpack('f',struct.pack('f',value))[0]
                source[point['index']][index] = value
                operations.append((native,name,value))
        try:
            brush.E.curve(source)
        except ValueError as exc:
            raise Failure('INVALID_REQUEST',str(exc)) from exc
    original = [(target,name,getattr(target,name)) for target,name,_ in operations]
    try:
        for target,name,value in operations:
            setattr(target,name,value)
            require(getattr(target,name)==value,'VALIDATION_FAILED','Native draft field did not read back: '+name)
    except Exception:
        for target,name,value in reversed(original):
            setattr(target,name,value)
        raise


def select_curve(scene, native, params):
    settings = scene.toon_viewport_preview
    curve = native.curve_editor
    previous = (settings.curve_group,settings.curve_slot)
    plan = native._preset(scene)
    group,slot = params['group'],params['slot']
    require(any(row['id']==group for row in plan['groups']),'NOT_FOUND','Unknown active Pencil curve group')
    try:
        curve.targets(plan,group,slot)
        with native.group_ui.guard(scene):
            settings.curve_group,settings.curve_slot = (group,slot)
        curve.initialize(scene)
    except (ValueError,RuntimeError,TypeError,KeyError) as exc:
        with native.group_ui.guard(scene):
            settings.curve_group,settings.curve_slot = previous
        raise Failure('CONFLICT','Curve selection could not load a complete author: '+str(exc)) from exc


def configure_mapping(scene, native, patch):
    settings = scene.toon_viewport_preview
    curve = native.curve_editor
    tree = curve._owned(settings)
    require(tree is not None,'CONFLICT','Load the exact original CurveMapping first')
    try:
        curve._check_target(settings,tree)
    except ValueError as exc:
        raise Failure('CONFLICT',str(exc)) from exc
    require(isinstance(patch,dict) and patch.keys() <= {'enabled','points'} and patch,'INVALID_REQUEST',
            'Use enabled switches and/or explicit CurveMapping points')
    names = set(curve.KEYS)
    switches = patch.get('enabled',{})
    edits = patch.get('points',[])
    require(isinstance(switches,dict) and switches.keys()<=names and all(type(value) is bool for value in switches.values())
            and isinstance(edits,list) and 0<len(edits)+len(switches)<=64,
            'INVALID_REQUEST','Choose the exact size_curve/alpha_curve switches and point edits')
    codec = curve.codec()
    captured = {key:codec._capture_curve(tree.nodes[key].mapping,bool(getattr(settings,'curve_'+key+'_enabled')))
                for key in names}
    operations = [(settings,'curve_'+key+'_enabled',value) for key,value in switches.items()]
    seen = set()
    touched = set()
    for edit in edits:
        fields = {'curve','index'}
        require(isinstance(edit,dict) and fields < edit.keys() <= fields | {'x','y','handle'}
                and isinstance(edit['curve'],str) and edit['curve'] in names and type(edit['index']) is int,
                'INVALID_REQUEST','A CurveMapping point needs a known curve, existing index and x, y or handle')
        key,index = edit['curve'],edit['index']
        points = captured[key]['points']
        require(0<=index<len(points) and (key,index) not in seen,'INVALID_REQUEST','Unknown or repeated CurveMapping point')
        seen.add((key,index))
        mapping = tree.nodes[key].mapping
        point = mapping.curves[0].points[index]
        touched.add(key)
        for field in ('x','y'):
            if field in edit:
                value = edit[field]
                require(type(value) in (int,float) and -16<=value<=16 and math.isfinite(value),
                        'INVALID_REQUEST','CurveMapping coordinates must be finite within -16..16')
                points[index][field] = struct.unpack('f',struct.pack('f',value))[0]
                operations.append((point.location,field,points[index][field]))
        if 'handle' in edit:
            points[index]['handle'] = edit['handle']
            operations.append((point,'handle_type',edit['handle']))
    try:
        for value in captured.values():
            codec._curve(value)
    except ValueError as exc:
        raise Failure('INVALID_REQUEST',str(exc)) from exc
    originals = [(target,key,getattr(target,key)) for target,key,_ in operations]
    try:
        for target,key,value in operations:
            setattr(target,key,value)
            require(getattr(target,key)==value,'VALIDATION_FAILED','Native CurveMapping field did not read back')
        for key in touched:
            tree.nodes[key].mapping.update()
        curve.synchronize(scene)
    except Exception:
        for target,key,value in reversed(originals):
            setattr(target,key,value)
        for key in touched:
            tree.nodes[key].mapping.update()
        raise


def apply(bpy, owner, viewport, operation, params):
    before = collect(bpy, owner, viewport)
    if operation == 'brush-status':
        return before
    require(params['expected_context'] == before['expected_context'], 'CONFLICT', 'Saved author or live brush draft changed')
    scene = owner['scene']
    native = owner['native']
    settings = scene.toon_viewport_preview
    require(scene.library is None and scene.override_library is None and scene.is_editable,
            'UNSUPPORTED', 'Brush drafts require the local editable scene')
    require(sum(window.scene == scene for window in bpy.context.window_manager.windows)==1,
            'CONFLICT', 'Close the other window for this Scene before editing its shared brush draft')
    if operation in ('brush-open-author', 'brush-open-dialog'):
        actual = params['actual']
        require(before['dialog'] is None, 'CONFLICT', 'Resolve the open full-brush dialog first')
        require(before['curve'] is None or not before['curve']['pending'], 'CONFLICT',
                'Resolve the existing CurveMapping draft first')
        try:
            native.brush_ui.get_effects(scene,actual)
        except ValueError as exc:
            raise Failure('NOT_FOUND', 'Unknown actual brush in the current author') from exc
        if operation == 'brush-open-author':
            result = bpy.ops.toon_viewport.open_brush_author(actual=actual)
            require(result == {'FINISHED'}, 'WORKER_FAILED', 'Native brush author Text did not open')
            after = collect(bpy, owner, viewport)
            require(after['draft'] is not None and after['draft']['actual'] == actual and after['author'] == before['author'],
                    'VALIDATION_FAILED', 'Native text editor did not expose the requested brush draft')
        else:
            result = bpy.ops.toon_viewport.full_brush_effects('INVOKE_DEFAULT',actual=actual)
            require(result == {'RUNNING_MODAL'}, 'WORKER_FAILED', 'Native full-brush dialog did not open')
            after = collect(bpy, owner, viewport)
            require(after['dialog'] is not None and after['dialog']['actual'] == actual and after['author'] == before['author']
                    and after['draft'] == before['draft'], 'VALIDATION_FAILED', 'Native dialog did not expose the requested brush')
        return after
    if operation == 'brush-commit':
        draft = before['draft']
        require(draft is not None and draft['actual']==params['actual'], 'CONFLICT', 'The open draft belongs to a different brush')
        require(before['dialog'] is None,'CONFLICT','Resolve the open full-brush dialog first')
        require(before['curve'] is None or not before['curve']['pending'], 'CONFLICT',
                'Resolve the existing CurveMapping draft first')
        text = bpy.data.texts[draft['name']]
        require(text.library is None and text.get('toon_brush_effect_draft'), 'UNSUPPORTED', 'Expected an owned local artist draft')
        try:
            payload = json.loads(text.as_string())
            require(isinstance(payload,dict) and payload.keys()=={'actual','effects'} and payload['actual']==params['actual'],
                    'CONFLICT', 'The draft payload has a different brush identity')
            existing = native.brush_ui.get_effects(scene,params['actual'])
            desired = native.brush_ui.E.checked(native.brush_ui.E.merge(existing,payload['effects']))
        except (ValueError,TypeError,KeyError) as exc:
            require(False, 'INVALID_REQUEST', 'Invalid complete pencil brush draft: ' + str(exc))
        try:
            event = native.brush_ui.commit_author(scene,text)
        except (ValueError,RuntimeError) as exc:
            require(False, 'CONFLICT', str(exc))
        require(event.get('updated_actuals')==[params['actual']] and native.brush_ui.get_effects(scene,params['actual'])==desired,
                'VALIDATION_FAILED','The complete author did not read back on the requested brush')
        after = collect(bpy,owner,viewport)
        require(after['draft']==before['draft'],'VALIDATION_FAILED','The artist draft Text changed during commit')
        return {**after,'committed_actual':params['actual']}
    if operation in ('brush-point','brush-configure'):
        dialog = before['dialog']
        require(dialog is not None and dialog['editor_id']==params['editor_id'],'CONFLICT','The original brush dialog is no longer open')
        if operation=='brush-configure':
            configure(bpy,scene,native.brush_ui,owner['brush_editor'],dialog,params['patch'])
            after = collect(bpy,owner,viewport)
            require(after['author']==before['author'] and after['draft']==before['draft'],'VALIDATION_FAILED',
                    'Staging modal brush fields unexpectedly changed the saved author')
            return after
        collection, index = params['collection'],params['index']
        require(collection in dialog['points'], 'INVALID_REQUEST','Choose a curve collection from the open dialog')
        old = dialog['points'][collection]
        require(index==-1 or 0<index<len(old)-1,'CONFLICT','Only existing interior curve points can be removed')
        require(bpy.ops.toon_viewport.brush_curve_point(collection=collection,index=index)=={'FINISHED'},
                'CONFLICT','The native point edit failed')
        after = collect(bpy,owner,viewport)
        current = after['dialog']['points']
        require(after['author']==before['author'] and after['draft']==before['draft']
                and all(current[key]==value for key,value in dialog['points'].items() if key!=collection)
                and (current[collection]==old[:index]+old[index+1:] if index>=0 else len(current[collection])==len(old)+1),
                'VALIDATION_FAILED', 'The point operation changed another draft or collection')
        return {**after,'curve_collection':collection,'point_count':len(current[collection])}

    curve = before['curve']
    require(before['dialog'] is None,'CONFLICT','Resolve the open full-brush dialog first')
    if operation=='curve-select':
        require(curve is None or not curve['pending'],'CONFLICT','Resolve the existing CurveMapping draft first')
        select_curve(scene,native,params)
        after = collect(bpy,owner,viewport)
        require(after['curve_target']=={'group':params['group'],'slot':params['slot']} and after['author']==before['author']
                and after['curve'] is not None,'VALIDATION_FAILED','The exact curve group/slot did not load')
        return after
    require(curve is not None and curve['identity']==params['curve_identity'],'CONFLICT','The loaded CurveMapping identity changed')
    if operation=='curve-configure':
        configure_mapping(scene,native,params['patch'])
        after = collect(bpy,owner,viewport)
        require(after['curve']['identity']==curve['identity'] and not after['curve']['pending']
                and after['draft']==before['draft'],'VALIDATION_FAILED','Configured curve did not reach its author')
        return after
    if operation == 'curve-discard':
        require(bpy.ops.toon_viewport.discard_curves()=={'FINISHED'},'CONFLICT','Native Pencil refused to discard pending edits')
    else:
        require(bpy.ops.toon_viewport.restore_curves()=={'FINISHED'},'CONFLICT','Native Pencil refused to restore the loaded curve')
    after = collect(bpy,owner,viewport)
    restored = after['curve']
    require(restored is not None and restored['identity']==curve['identity'] and not restored['pending']
            and after['draft']==before['draft'],'VALIDATION_FAILED','The native curve did not reach its resolved state')
    if operation=='curve-discard':
        require(after['author']==before['author'],'VALIDATION_FAILED','Discard changed the saved curve author')
    return after
