# SPDX-License-Identifier: GPL-3.0-or-later
"""Explicit font dependencies and verified curve/font conversion."""
import hashlib,math
from pathlib import Path
import bpy
from protocol import Failure
from dependencies import path_value
from font_binary import inspect_bytes
from inspection import identity

SLOTS=('font','font_bold','font_italic','font_bold_italic')
def text_roles(curve):
    roles={}
    if len(curve.body_format)!=len(curve.body):raise Failure('UNSUPPORTED','Text formatting count differs from Unicode characters')
    for ch,style in zip(curve.body,curve.body_format):
        if style.use_small_caps:raise Failure('UNSUPPORTED','Small caps requires a separate glyph policy')
        slot='font_bold_italic' if style.use_bold and style.use_italic else 'font_bold' if style.use_bold else 'font_italic' if style.use_italic else 'font'
        roles[slot]=roles.get(slot,'')+ch
    return roles

def font_info(font,text):
    if font is None or font.filepath=='<builtin>':return {'status':'builtin_unverified','font':identity(font) if font else None,'missing':[]}
    path=path_value(font.filepath,font.library)
    if font.packed_file:data=bytes(font.packed_file.data);storage='packed'
    else:
        if not Path(path).is_file():return {'status':'missing_file','font':identity(font),'file':path}
        from filesystem import FileGuard
        with FileGuard(path) as guard:
            expected=guard.sha256();data=Path(path).read_bytes()
            if hashlib.sha256(data).hexdigest()!=expected:raise Failure('CONFLICT','Font changed while being inspected')
        storage='external'
    result=inspect_bytes(data,text)
    result.update(status='missing_glyphs' if result['missing'] else 'unsupported_shaping' if result['unsupported_shaping'] else 'verified_cmap',font=identity(font),file=path,storage=storage)
    return result

def curve_info(item):
    c=item.data
    info={'object':item.name,'type':item.type,'data':identity(c),'blender_version':bpy.app.version_string,'blender_build_hash':bpy.app.build_hash.decode(),
        'settings':{k:getattr(c,k) for k in ('dimensions','resolution_u','render_resolution_u','fill_mode','bevel_depth','bevel_resolution','extrude','offset','use_fill_caps')},
        'splines':[{'type':s.type,'cyclic':s.use_cyclic_u,'resolution':s.resolution_u,'points':len(s.bezier_points) if s.type=='BEZIER' else len(s.points)} for s in c.splines],
        'fonts':[],'status':'inspected'}
    if item.type=='FONT':
        info['body']=c.body;info['layout']={k:getattr(c,k) for k in ('size','shear','space_character','space_word','space_line','align_x','align_y')}
        try:roles=text_roles(c)
        except Failure as exc:info.update(status=exc.code,message=str(exc));return info
        for slot,text in roles.items():
            try:r=font_info(getattr(c,slot),text)
            except Failure as exc:r={'status':exc.code,'message':str(exc)}
            info['fonts'].append({'slot':slot,**r})
        if any(f['status']!='verified_cmap' for f in info['fonts']):info['status']='font_not_verified'
    return info

def create_font(op):
    from protocol import digest
    from filesystem import FileGuard
    path=op['font']['file']
    with FileGuard(path) as guard:
        if guard.sha256()!=op['font']['expected_sha256']:raise Failure('CONFLICT','Font SHA differs')
        data=Path(path).read_bytes();info=inspect_bytes(data,op['body'])
        if info['missing']:raise Failure('VALIDATION_FAILED','Missing glyphs: '+', '.join(info['missing']))
        if info['unsupported_shaping']:raise Failure('UNSUPPORTED','Shaping/variation/control characters require another adapter: '+', '.join(info['unsupported_shaping']))
        font=bpy.data.fonts.load(path)
        # Pack only the newly created VFont; no external writes and no fallback dependency.
        font.pack()
        if not font.packed_file or hashlib.sha256(font.packed_file.data).hexdigest()!=info['sha256']:raise Failure('VALIDATION_FAILED','Packed font differs from verified bytes')
    return font

def preflight(item,spec):
    c=item.data
    if c.render_resolution_u not in (0,c.resolution_u):raise Failure('UNSUPPORTED','Explicit viewport/render curve resolution must agree')
    if c.bevel_mode!='ROUND' or c.bevel_object or c.taper_object:raise Failure('UNSUPPORTED','Bevel/taper object dependency requires a dedicated conversion adapter')
    if item.type=='CURVE':
        if not c.splines:raise Failure('INVALID_REQUEST','Curve has no splines')
        for s in c.splines:
            if s.type not in ('POLY','BEZIER'):raise Failure('UNSUPPORTED','Verified conversion supports POLY/BEZIER splines')
            points=s.bezier_points if s.type=='BEZIER' else s.points
            if len(points)<(3 if s.use_cyclic_u else 2):raise Failure('INVALID_REQUEST','Insufficient curve control points')
            vectors=[v for p in points for v in ([p.co,p.handle_left,p.handle_right] if s.type=='BEZIER' else [p.co])]
            if not all(math.isfinite(x) for v in vectors for x in v):raise Failure('INVALID_REQUEST','Non-finite curve control point')
            if c.dimensions=='2D' and any(abs(p.co.z)>1e-8 for p in points):raise Failure('INVALID_REQUEST','2D curve control points must lie in local XY')
        if spec['fonts']:raise Failure('INVALID_REQUEST','Curve conversion does not consume font declarations')
    else:
        if c.follow_curve or any(abs(b.x)+abs(b.y)+abs(b.width)+abs(b.height)>0 for b in c.text_boxes):raise Failure('UNSUPPORTED','Text-on-curve or text-box layout requires another adapter')
        declared={str(Path(f['file']).resolve()):f['expected_sha256'] for f in spec['fonts']}
        if len(declared)!=len(spec['fonts']):raise Failure('INVALID_REQUEST','Duplicate font declaration')
        consumed=set()
        for slot,text in text_roles(c).items():
            font=getattr(c,slot);r=font_info(font,text)
            if r['status']!='verified_cmap':raise Failure('VALIDATION_FAILED','Font slot '+slot+': '+r['status']+' '+str(r.get('missing',[])))
            path=str(Path(r['file']).resolve())
            if declared.get(path)!=r['sha256']:raise Failure('CONFLICT','Font source/hash not explicitly declared for '+slot)
            consumed.add(path)
        if set(declared)!=consumed:raise Failure('INVALID_REQUEST','Unused font declarations')
    return curve_info(item)

def mesh_data(mesh):
    return {'positions':[list(v.co) for v in mesh.vertices],'edges':[list(e.vertices) for e in mesh.edges],
        'faces':[list(p.vertices) for p in mesh.polygons],'materials':[identity(m) if m else None for m in mesh.materials],
        'face_materials':[p.material_index for p in mesh.polygons]}

def convert(op,context):
    import modeling,scenes
    item=modeling.editable(op,types=('CURVE','FONT'))
    if op['data_scope']=='shared':raise Failure('UNSUPPORTED','Conversion is per object; use single_user or reject_shared')
    if item.modifiers:raise Failure('UNSUPPORTED','Apply modifiers explicitly before curve conversion')
    if item.data.asset_data or item.data.get('asset_id'):raise Failure('UNSUPPORTED','Identified curve data requires an identity policy')
    source=preflight(item,op['verify']);matrix=[list(r) for r in item.matrix_world]
    graph=bpy.context.evaluated_depsgraph_get();evaluated=item.evaluated_get(graph);mesh=evaluated.to_mesh(preserve_all_data_layers=True,depsgraph=graph)
    try:before=mesh_data(mesh)
    finally:evaluated.to_mesh_clear()
    if not before['positions'] or not before['faces']:raise Failure('VALIDATION_FAILED','Conversion produces no surface; use explicit fill or bevel')
    if not all(math.isfinite(v) for p in before['positions'] for v in p):raise Failure('VALIDATION_FAILED','Evaluated curve contains non-finite coordinates')
    name=item.name+'.Converted';scenes.fresh(bpy.data.meshes,name)
    with modeling.operator_context(item,context):modeling.finished(bpy.ops.object.convert(target='MESH',keep_original=False,merge_customdata=False))
    item=scenes.find(bpy.data.objects,op['object']);scenes.named(item.data,name);modeling.bounded(item.data);scenes.update();after=mesh_data(item.data)
    if len(before['positions'])!=len(after['positions']):raise Failure('VALIDATION_FAILED','Conversion changed evaluated vertex count')
    if not all(math.isfinite(v) for p in after['positions'] for v in p):raise Failure('VALIDATION_FAILED','Converted curve contains non-finite coordinates')
    delta=max(abs(a-b) for p,q in zip(before['positions'],after['positions']) for a,b in zip(p,q))
    if not math.isfinite(delta) or delta>op['verify']['max_coordinate_error']:raise Failure('VALIDATION_FAILED','Conversion coordinate error exceeded declared tolerance')
    if any(before[k]!=after[k] for k in ('edges','faces','materials','face_materials')):raise Failure('VALIDATION_FAILED','Conversion changed evaluated topology or material mapping')
    if scenes.compare(matrix,[list(r) for r in item.matrix_world]):raise Failure('VALIDATION_FAILED','Conversion changed object world transform')
    return {'curve_conversion_report_version':'1.0','adapter':'CURVE_FONT_V1','source':source,'target':identity(item.data),
        'vertices':len(after['positions']),'faces':len(after['faces']),'maximum_coordinate_error':delta,'coordinate_tolerance':op['verify']['max_coordinate_error'],
        'topology':'exact_evaluated_match','materials':'exact_match','world_transform':'preserved','glyph_validation':'cmap_not_visual_quality',
        'source_data_retained':'unreferenced source data retained by candidate lifecycle'}
