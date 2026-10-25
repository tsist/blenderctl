"""Isolated Cycles PBR signal baking and disk-only material reconstruction.

The final, linked NodeReroutes carrying ``mwp_channel`` are the contract.  A
successful operator is insufficient: every saved image is discarded, decoded
again and compared, and only a complete report can be reconstructed.  No source
scene, object, mesh, material, image, UV selection or render setting is changed.
"""
from array import array
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import struct
import uuid

from .contract import WorkflowError


SOCKETS = {
    'base_color': 'Base Color', 'roughness': 'Roughness', 'metallic': 'Metallic',
    'alpha': 'Alpha', 'transmission': 'Transmission Weight', 'ior': 'IOR',
    'coat_weight': 'Coat Weight', 'coat_roughness': 'Coat Roughness',
}
CHANNELS = tuple(SOCKETS) + ('normal', 'height')
SCHEMA = 'mwp-bake-1.0'


def _fail(code, message):
    raise WorkflowError(code, message)


def _sha(path):
    with open(path, 'rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def _plain(value):
    return list(value) if hasattr(value, '__len__') and not isinstance(value, str) else value


def _settings(settings):
    if not isinstance(settings, dict) or set(settings)-{'channels', 'size', 'margin', 'samples', 'bit_depth'}:
        _fail('bake_settings', 'Unknown bake settings')
    out = {'channels': list(CHANNELS), 'size': 2048, 'margin': 16,
           'samples': 16, 'bit_depth': '16', **deepcopy(settings)}
    channels = out['channels']
    if (not isinstance(channels, list) or not channels or
            any(not isinstance(c, str) or c not in CHANNELS for c in channels) or len(set(channels)) != len(channels)):
        _fail('bake_settings', 'channels must be distinct supported channel names')
    for key, low, high in [('size', 16, 2048), ('margin', 0, 256), ('samples', 1, 256)]:
        if type(out[key]) is not int or not low <= out[key] <= high:
            _fail('bake_settings', key+' is outside the supported range')
    if out['margin']*2 >= out['size'] or out['bit_depth'] not in ('8', '16'):
        _fail('bake_settings', 'Invalid padding or bit depth')
    return out


def _constant(socket, stack=()):
    """Conservative constant folding. Unknown signals are baked, never guessed."""
    if not socket.is_linked:
        return _plain(socket.default_value) if hasattr(socket, 'default_value') else None
    link = socket.links[0]
    n = link.from_node
    if n in stack or len(stack) > 64:
        return None
    if n.bl_idname == 'NodeReroute':
        return _constant(n.inputs[0], stack+(n,))
    if n.bl_idname in ('ShaderNodeValue', 'ShaderNodeRGB'):
        return _plain(link.from_socket.default_value)
    if n.bl_idname == 'ShaderNodeMath':
        a = _constant(n.inputs[0], stack+(n,))
        b = _constant(n.inputs[1], stack+(n,))
        if not isinstance(a, (int, float)) or not isinstance(b, (int, float)):
            return None
        operations = {'ADD': lambda: a+b, 'SUBTRACT': lambda: a-b,
                      'MULTIPLY': lambda: a*b, 'DIVIDE': lambda: a/b if b else 0,
                      'MINIMUM': lambda: min(a,b), 'MAXIMUM': lambda: max(a,b),
                      'MULTIPLY_ADD': lambda: a*b+_constant(n.inputs[2], stack+(n,))}
        if n.operation not in operations:
            return None
        try:
            value = operations[n.operation]()
            return max(0, min(1, value)) if n.use_clamp else value
        except (TypeError, ValueError, OverflowError):
            return None
    if n.bl_idname == 'ShaderNodeMixRGB' and n.blend_type == 'MIX':
        values = [_constant(s, stack+(n,)) for s in n.inputs[:3]]
        f,a,b = values
        if isinstance(f, (int,float)) and isinstance(a,list) and isinstance(b,list):
            f = max(0, min(1,f))
            rgb = [(1-f)*a[i]+f*b[i] for i in range(3)]+[a[3]]
            return [max(0,min(1,v)) for v in rgb] if n.use_clamp else rgb
    return None


def _static_tree(tree, stack=()):
    if tree in stack or len(stack) > 8 or tree.animation_data or tree.library:
        _fail('bake_source', 'Only bounded local static shader trees are supported')
    for node in tree.nodes:
        if node.bl_idname in ('ShaderNodeObjectInfo', 'ShaderNodeParticleInfo',
                             'ShaderNodeHairInfo', 'ShaderNodeCameraData', 'ShaderNodeLightPath',
                             'ShaderNodeShaderToRGB'):
            _fail('bake_context', 'Context-dependent signal cannot be isolated: '+node.bl_idname)
        if node.bl_idname == 'ShaderNodeTexCoord':
            if node.from_instancer:
                _fail('bake_context', 'Instancer coordinates are outside static-target bake')
            for label in ('Camera', 'Window', 'Reflection'):
                if node.outputs[label].is_linked:
                    _fail('bake_context', 'View-dependent texture coordinates cannot be baked as PBR signals')
        if node.bl_idname == 'ShaderNodeAmbientOcclusion' and not node.only_local:
            _fail('bake_context', 'AO must declare Only Local; isolated bake does not substitute scene occluders')
        if node.bl_idname == 'ShaderNodeTexImage' and node.image is not None and node.image.source not in ('FILE','GENERATED'):
            _fail('bake_context', 'Animated/tiled image sources require a separate bake contract')
        if node.bl_idname == 'ShaderNodeGroup':
            if node.node_tree is None:
                _fail('bake_source', 'Missing shader group')
            _static_tree(node.node_tree, stack+(tree,))


def _thickness_constant(socket, frames=(), stack=()):
    """Resolve bounded static scalars, including instance-bound control groups.

    No color/vector coercion or dynamic signal approximation is performed.
    Unknown expressions are rejected by the caller, never silently omitted.
    """
    key = (socket.as_pointer(), tuple(node.as_pointer() for node in frames))
    if key in stack or len(stack) > 64:
        return None
    stack = stack + (key,)
    if not socket.is_linked:
        return _plain(socket.default_value) if hasattr(socket, 'default_value') else None
    if len(socket.links) != 1:
        return None
    link = socket.links[0]; node = link.from_node
    if node.mute:
        return None
    if node.bl_idname == 'NodeReroute':
        return _thickness_constant(node.inputs[0], frames, stack)
    if node.bl_idname == 'ShaderNodeGroup':
        outputs = [n for n in node.node_tree.nodes if n.bl_idname == 'NodeGroupOutput' and n.is_active_output]
        if len(outputs) != 1:
            return None
        matches = [s for s in outputs[0].inputs if s.identifier == link.from_socket.identifier]
        return _thickness_constant(matches[0], frames+(node,), stack) if len(matches) == 1 else None
    if node.bl_idname == 'NodeGroupInput':
        if not frames or frames[-1].node_tree != node.id_data:
            return None
        matches = [s for s in frames[-1].inputs if s.identifier == link.from_socket.identifier]
        return _thickness_constant(matches[0], frames[:-1], stack) if len(matches) == 1 else None
    return _constant(socket)


def _declared_thickness(value):
    from .procedural_contract import _float32
    if (not isinstance(value, dict) or set(value) != {'mode', 'space', 'value'}
            or value['mode'] not in ('AUTO', 'EXPLICIT') or value['space'] != 'OBJECT'):
        _fail('bake_thickness', 'Thickness requires AUTO/EXPLICIT, OBJECT space and an explicit value/null field')
    if value['mode'] == 'AUTO':
        if value['value'] is not None:
            _fail('bake_thickness', 'AUTO Thickness must have null value; it is not a connected zero')
        return dict(value)
    number = value['value']
    if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number) or not 0 <= number <= 1000:
        _fail('bake_thickness', 'EXPLICIT Thickness must be a finite scalar in object-space range 0..1000')
    stored = _float32(number, 'thickness.value')
    return {'mode': 'EXPLICIT', 'space': 'OBJECT', 'value': stored}


def _output_thickness(output):
    socket = output.inputs.get('Thickness')
    if socket is None:
        _fail('bake_thickness', 'Runtime Material Output lacks Thickness')
    if not socket.is_linked:
        return {'mode': 'AUTO', 'space': 'OBJECT', 'value': None}
    return _declared_thickness({'mode': 'EXPLICIT', 'space': 'OBJECT', 'value': _thickness_constant(socket)})


def _material_contract(material):
    if material is None or not material.use_nodes or material.library or material.animation_data:
        _fail('bake_source', 'A local static node material is required')
    tree = material.node_tree
    if 'mwp_state' in material:
        from .procedural_core import verify_managed_state
        if not verify_managed_state(material)['ok']:
            _fail('managed_conflict', 'Managed shader structure was edited before bake')
    _static_tree(tree)
    reroutes = {}
    for node in tree.nodes:
        key = node.get('mwp_channel')
        if key is not None:
            if key not in CHANNELS or key in reroutes or node.bl_idname != 'NodeReroute' or not node.inputs[0].is_linked:
                _fail('bake_channel', 'Missing, duplicate, unlinked or invalid final channel: '+str(key))
            reroutes[key] = node
    required = set(SOCKETS)|{'height'}
    if required-set(reroutes):
        _fail('bake_channel', 'Missing final signal reroutes: '+str(sorted(required-set(reroutes))))
    outputs = [n for n in tree.nodes if n.bl_idname == 'ShaderNodeOutputMaterial' and
               n.is_active_output and n.target in ('ALL', 'CYCLES')]
    if len(outputs) != 1:
        _fail('bake_source', 'One active Cycles-compatible material output is required')
    output = outputs[0]
    _output_thickness(output)
    surface = output.inputs['Surface']
    if not surface.is_linked or surface.links[0].from_node.bl_idname != 'ShaderNodeBsdfPrincipled':
        _fail('bake_source', 'Final surface must be a single Principled BSDF')
    shader = surface.links[0].from_node
    if any(output.inputs[k].is_linked for k in ('Volume', 'Displacement')):
        _fail('bake_source', 'Volume and true displacement are outside this surface bake')
    for channel, name in SOCKETS.items():
        socket = shader.inputs.get(name)
        if socket is None or not socket.is_linked or socket.links[0].from_node != reroutes[channel]:
            _fail('bake_channel', 'Final '+channel+' reroute does not drive Principled '+name)
    normal = shader.inputs['Normal']
    if normal.is_linked:
        bump = normal.links[0].from_node
        if (bump.bl_idname != 'ShaderNodeBump' or not bump.inputs['Height'].is_linked or
                bump.inputs['Height'].links[0].from_node != reroutes['height'] or bump.inputs['Normal'].is_linked):
            _fail('bake_source', 'Normal must be generated only from the final total-height Bump')
        distance = float(material.get('mwp_height_distance', -1))
        if (not math.isfinite(distance) or distance < 0 or bump.inputs['Distance'].is_linked or
                abs(bump.inputs['Distance'].default_value-distance) > 1e-7 or bump.inputs['Strength'].is_linked):
            _fail('bake_source', 'Height distance metadata must match the final Bump')
        bump_meta = {'distance':distance, 'strength':float(bump.inputs['Strength'].default_value),
                     'invert': bool(bump.invert)}
    else:
        bump_meta = {'distance':float(material.get('mwp_height_distance', 0)), 'strength':0., 'invert':False}
    permitted = set(SOCKETS.values())|{'Normal', 'Coat Normal'}
    if any(s.is_linked for s in shader.inputs if s.name not in permitted):
        _fail('bake_source', 'Uncaptured linked Principled input')
    coat = shader.inputs['Coat Normal']
    follows = bool(coat.is_linked)
    if follows and (not normal.is_linked or coat.links[0].from_socket != normal.links[0].from_socket):
        _fail('bake_source', 'Independent coat normals need a separate bake contract')
    return reroutes, shader, output, bump_meta, follows


def _cross(a,b,c):
    return (b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0])


def _intersection_area(subject, clip):
    """Positive-area triangle intersection; shared UV edges are not overlaps."""
    polygon = list(subject)
    if _cross(*clip) < 0:
        clip = list(reversed(clip))
    for i in range(3):
        a,b = clip[i], clip[(i+1)%3]
        old, polygon = polygon, []
        if not old:
            break
        for j,p in enumerate(old):
            q = old[(j+1)%len(old)]
            cp,cq = _cross(a,b,p), _cross(a,b,q)
            if cp >= 0:
                polygon.append(p)
            if (cp >= 0) != (cq >= 0):
                t = cp/(cp-cq)
                polygon.append((p[0]+t*(q[0]-p[0]), p[1]+t*(q[1]-p[1])))
    return abs(sum(polygon[i][0]*polygon[(i+1)%len(polygon)][1]-
                   polygon[(i+1)%len(polygon)][0]*polygon[i][1] for i in range(len(polygon))))/2


def _uv_check(mesh, uv_name, slots, *, allow_degenerate=False):
    uv = mesh.uv_layers.get(uv_name)
    if uv is None:
        _fail('bake_uv', 'Requested UV layer is missing: '+uv_name)
    mesh.calc_loop_triangles()
    triangles, degenerate = [], 0
    for tri in mesh.loop_triangles:
        if mesh.polygons[tri.polygon_index].material_index not in slots:
            continue
        points = [tuple(uv.data[i].uv) for i in tri.loops]
        if any(not math.isfinite(v) or not -1e-6 <= v <= 1+1e-6 for p in points for v in p):
            _fail('bake_uv', 'Target UVs must be finite and contained in one 0..1 tile')
        area = abs(_cross(*points))/2
        if area < 1e-12:
            degenerate += 1
            continue
        triangles.append(points)
    if not triangles or (degenerate and not allow_degenerate):
        _fail('bake_uv', 'Target faces have empty or degenerate UV coverage')
    if len(triangles) > 200000:
        _fail('bake_uv', 'UV validation is bounded to 200000 target triangles')
    grid = max(1,min(128,int(math.sqrt(len(triangles)))))
    buckets, tested, overlap, tests = {}, set(), None, 0
    for index,t in enumerate(triangles):
        lo = [max(0,min(grid-1,int(min(p[k] for p in t)*grid))) for k in (0,1)]
        hi = [max(0,min(grid-1,int(max(p[k] for p in t)*grid))) for k in (0,1)]
        for x in range(lo[0],hi[0]+1):
            for y in range(lo[1],hi[1]+1):
                bucket = buckets.setdefault((x,y),[])
                for other in bucket:
                    pair = (other,index)
                    if pair in tested:
                        continue
                    tested.add(pair)
                    tests += 1
                    if tests > 3000000:
                        _fail('bake_uv', 'UV overlap validation exceeded bounded pair budget')
                    if _intersection_area(t,triangles[other]) > 1e-10:
                        overlap = pair
                        break
                if overlap:
                    break
                bucket.append(index)
            if overlap:
                break
        if overlap:
            _fail('bake_uv_overlap', 'Positive-area target UV overlap: triangle pair '+str(overlap))
    return {'layer':uv_name, 'triangles':len(triangles), 'degenerate_triangles':degenerate,
            'positive_area_overlap':False, 'tested_pairs':tests,
            'degenerate_policy':'Bevel-generated zero-area strips only' if allow_degenerate else 'reject',
            'scope':'target-material faces only; non-target slots use separate discard images'}


def _pixels(image):
    import numpy as np
    out = np.empty(len(image.pixels), dtype=np.float32)
    image.pixels.foreach_get(out)
    return out.reshape((-1,4))


def _stats(pixels):
    import numpy as np
    covered = pixels[:,3] > .5
    if not np.any(covered) or not np.isfinite(pixels).all():
        _fail('bake_pixels', 'No covered finite bake pixels')
    values = pixels[covered,:3]
    return {'covered_pixels':int(np.count_nonzero(covered)),
            'min':values.min(axis=0).tolist(), 'max':values.max(axis=0).tolist(),
            'mean':values.mean(axis=0,dtype=np.float64).tolist()}


def _signal_pixels(image, *, freshly_baked=False):
    """Return comparable linear signals, respecting Blender's image buffers.

    Cycles bake writes target-space sRGB even to a generated float buffer.
    Reloaded 16-bit PNGs have float, scene-linear buffers; reloaded byte PNGs
    retain encoded bytes. This distinction is verified by the fixture.
    """
    import numpy as np
    pixels=_pixels(image)
    if image.colorspace_settings.name=='sRGB' and (freshly_baked or not image.is_float):
        rgb=pixels[:,:3]
        pixels[:,:3]=np.where(rgb <= .04045,rgb/12.92,((rgb+.055)/1.055)**2.4)
    return pixels


def _image_header(path):
    with open(path,'rb') as stream:
        head = stream.read(33)
    if head[:8] == b'\x89PNG\r\n\x1a\n':
        width,height = struct.unpack('>II',head[16:24])
        return {'format':'PNG', 'width':width, 'height':height, 'bit_depth':str(head[24]), 'png_color_type':head[25]}
    if head[:4] == b'v/1\x01':
        def cstring(stream):
            out=bytearray()
            while True:
                value=stream.read(1)
                if not value or value==b'\0':break
                out.extend(value)
                if len(out)>4096:_fail('bake_file','Invalid EXR attribute name')
            return bytes(out).decode('ascii')
        attributes={}
        with open(path,'rb') as stream:
            stream.seek(8)
            for _ in range(128):
                name=cstring(stream)
                if not name:break
                kind=cstring(stream);count=struct.unpack('<I',stream.read(4))[0]
                if count>4*1024*1024:_fail('bake_file','Oversize EXR attribute')
                attributes[name]=(kind,stream.read(count))
            else:_fail('bake_file','EXR header exceeds supported bound')
        channels=attributes.get('channels',('',b''))[1]
        depths={};offset=0
        while offset<len(channels) and channels[offset]:
            end=channels.index(0,offset);name=channels[offset:end].decode('ascii')
            pixel_type=struct.unpack_from('<I',channels,end+1)[0]
            depths[name]={0:'32-uint',1:'16',2:'32'}.get(pixel_type,'unknown')
            offset=end+17
        if not depths or len(set(depths.values()))!=1:
            _fail('bake_file','Unsupported mixed/empty EXR channel precision')
        window=attributes.get('dataWindow',('',b''))[1]
        if len(window)!=16:_fail('bake_file','Missing EXR data window')
        x0,y0,x1,y1=struct.unpack('<4i',window)
        return {'format':'OPEN_EXR','width':x1-x0+1,'height':y1-y0+1,
                'bit_depth':next(iter(depths.values())),'channel_depths':depths}
    _fail('bake_file', 'Unexpected encoded image format: '+str(path))


def _save_verified(image, path, color_space, bit_depth):
    import bpy
    import numpy as np
    before = _signal_pixels(image,freshly_baked=True)
    stats = _stats(before)
    image.filepath_raw = str(path)
    image.file_format = 'OPEN_EXR' if path.suffix == '.exr' else 'PNG'
    image.save()
    if not path.is_file():
        _fail('bake_file', 'Image.save did not create its requested file')
    header = _image_header(path)
    if header['format'] == 'PNG' and header['bit_depth'] != bit_depth:
        _fail('bake_file', 'Encoded PNG depth differs from requested depth: '+str(header))
    # Completely destroy the old image and its pixel buffer, then decode disk.
    bpy.data.images.remove(image)
    loaded = bpy.data.images.load(str(path), check_existing=False)
    try:
        loaded.colorspace_settings.name = color_space
        loaded.alpha_mode = 'STRAIGHT'
        after = _signal_pixels(loaded)
        if list(loaded.size) != [header.get('width',loaded.size[0]),header.get('height',loaded.size[1])] or before.shape != after.shape:
            _fail('bake_file', 'Decoded size differs from baked image')
        decoded_stats = _stats(after)
        error = float(np.max(np.abs(before-after)))
        tolerance = .012 if bit_depth == '8' else (.004 if path.suffix == '.exr' else .0002)
        if error > tolerance:
            worst=int(np.argmax(np.max(np.abs(before-after),axis=1)))
            _fail('bake_roundtrip', 'Saved/reloaded pixels changed by '+str(error)+
                  '; pixel '+str(worst)+' before='+str(before[worst].tolist())+
                  ' after='+str(after[worst].tolist())+' color_space='+color_space+
                  ' loaded_space='+loaded.colorspace_settings.name)
        return {'file':str(path), 'sha256':_sha(path), 'bytes':path.stat().st_size,
                'width':int(loaded.size[0]), 'height':int(loaded.size[1]),
                'format':header['format'], 'bit_depth':header['bit_depth'],
                'color_space':color_space, 'decoded':True, 'buffer_recreated':True,
                'roundtrip_max_error':error, 'baked_pixel_stats':stats,
                'pixel_stats':decoded_stats,
                'comparison_domain':'scene-linear color / raw data; sRGB bake buffer decoded explicitly'}
    finally:
        bpy.data.images.remove(loaded)


def _write_report(path, report):
    # Exclusive creation is intentional, including partial reports.
    with open(path,'x',encoding='utf-8') as stream:
        json.dump(report,stream,indent=2,ensure_ascii=False,allow_nan=False)


def bake_channels(obj, material, output_dir, settings, uv_layer='UVMap'):
    """Bake a static target's specified material faces; return a complete report.

    Nonconstant omitted optical signals are automatically added. Constants are
    retained as declared parameters. Height is unnormalized signed float EXR;
    IOR is explicitly encoded (IOR - 1) / 3 in PNG. Each call owns a new UUID
    subdirectory, and a failed call leaves ``partial.json`` and an exception
    carrying ``report_path``. The caller retains responsibility for visual QA.
    """
    import bpy
    config = _settings(settings)
    output_dir = Path(output_dir)
    if not output_dir.is_absolute():
        _fail('bake_path', 'An absolute output directory is required')
    if obj is None or obj.type != 'MESH' or obj.mode != 'OBJECT' or obj.library or obj.animation_data or obj.constraints or obj.data.animation_data:
        _fail('bake_source', 'A local static Object-mode mesh without constraints is required')
    if obj.name not in bpy.context.view_layer.objects or obj.instance_type != 'NONE' or obj.data.shape_keys:
        _fail('bake_source', 'Target must belong to the current layer, without instances or shape keys')
    supported_modifiers = {'BEVEL', 'WEIGHTED_NORMAL', 'TRIANGULATE'}
    for mod in obj.modifiers:
        if mod.type not in supported_modifiers or mod.show_render != mod.show_viewport:
            _fail('bake_modifier', 'Unsupported or viewport/render-mismatched modifier: '+mod.name)
    reroutes, shader, output, bump_meta, coat_follows = _material_contract(material)
    slots = {i for i,s in enumerate(obj.material_slots) if s.material == material}
    used = {p.material_index for p in obj.data.polygons}
    slots &= used
    if not slots:
        _fail('bake_source', 'Material is not used by any target polygon')
    uv_source = _uv_check(obj.data,uv_layer,slots)
    constants = {channel:_constant(node.inputs[0]) for channel,node in reroutes.items() if channel != 'normal'}
    for channel,value in list(constants.items()):
        if channel!='base_color' and isinstance(value,list):
            # A grayscale MixRGB is common for layered scalar channels. For a
            # colored signal let Blender perform its actual socket conversion
            # in the bake graph instead of guessing luminance coefficients.
            constants[channel]=(float(value[0]) if len(value)>=3 and
                max(value[:3])-min(value[:3])<1e-8 else None)
    # Scalar/range validation is also checked against actual pixels after bake.
    for channel,value in constants.items():
        if value is None:
            continue
        values = value if isinstance(value,list) else [value]
        if any(not isinstance(v,(int,float)) or not math.isfinite(v) for v in values):
            _fail('bake_channel', 'Non-finite constant '+channel)
        if channel == 'base_color' and (not isinstance(value,list) or len(value)!=4):
            _fail('bake_channel', 'Base color signal must be RGBA')
        if channel != 'base_color' and isinstance(value,list):
            _fail('bake_channel', 'Scalar channel has a color/vector signal: '+channel)
        if channel != 'height':
            lo,hi = (1,4) if channel == 'ior' else (0,1)
            if any(not lo-1e-7 <= v <= hi+1e-7 for v in values):
                _fail('bake_channel', 'Out-of-range '+channel+' constant')
    channels = list(config['channels'])
    for channel in SOCKETS:
        if constants[channel] is None and channel not in channels:
            channels.append(channel)
    if constants['height'] is None and 'normal' not in channels and 'height' not in channels:
        channels.append('height')
    run_id = uuid.uuid4().hex
    run_dir = output_dir.resolve()/('bake-'+run_id)
    run_dir.mkdir(parents=True,exist_ok=False)
    report = {'schema':SCHEMA, 'id':run_id, 'status':'running', 'output_dir':str(run_dir),
              'blender':{'version':bpy.app.version_string,'build_hash':bpy.app.build_hash.decode()},
              'source':{'object':obj.name,'mesh':obj.data.name,'material':material.name,
                        'material_slots':sorted(slots),'matrix_world':[list(r) for r in obj.matrix_world],
                        'frame':bpy.context.scene.frame_current},
              'settings':config, 'requested_channels':config['channels'],
              'required_added_channels':[c for c in channels if c not in config['channels']],
              'uv_source':uv_source, 'uv_layer':uv_layer,
              'constants':constants, 'height':bump_meta,
              'thickness':_output_thickness(output),
              'coat_normal_follows_base':coat_follows,
              'shader_defaults':{s.name:_plain(s.default_value) for s in shader.inputs if hasattr(s,'default_value') and not s.is_linked},
              'shader_properties':{p:getattr(shader,p) for p in ('distribution','subsurface_method') if hasattr(shader,p)},
              'material_properties':{p:getattr(material,p) for p in ('surface_render_method','thickness_mode','use_transparency_overlap','use_raytrace_refraction') if hasattr(material,p)},
              'channels':{}, 'normal':{'space':'TANGENT','convention':'OpenGL','swizzle':['+X','+Y','+Z'],
                                      'includes_height':True,'rebuild_apply_height_again':False},
              'delivery':{'complete':False, 'source_saved':False, 'disk_decode_verified':False,
                          'independent_reopen':'not_run', 'visual_equivalence':'not_run'},
              'scope':['one static target; only specified material slots write final images',
                       'isolated target; AO must use Only Local',
                       'UV overlap rejected inside target faces; cross-slot overlap writes separate discard images',
                       'normal uses final Cycles shading including total height; height file remains an auxiliary channel']}
    scene = mesh = duplicate = temp_material = discard_material = discard_image = None
    pending_images = []
    failure = None
    try:
        scene = bpy.data.scenes.new('MWP_Bake_'+run_id)
        scene.frame_set(report['source']['frame'])
        scene.render.engine = 'CYCLES'
        scene.cycles.device = 'CPU'
        scene.cycles.samples = config['samples']
        scene.cycles.use_denoising = False
        scene.render.bake.use_clear = True
        scene.render.bake.margin = config['margin']
        scene.render.bake.margin_type = 'EXTEND'
        scene.render.bake.use_selected_to_active = False
        mesh = obj.data.copy()
        duplicate = obj.copy()
        duplicate.data = mesh
        duplicate.parent = None
        duplicate.matrix_world = obj.matrix_world.copy()
        duplicate.hide_render = False
        duplicate.hide_viewport = False
        scene.collection.objects.link(duplicate)
        temp_material = material.copy()
        for i in range(len(duplicate.material_slots)):
            duplicate.material_slots[i].link = 'DATA'
        # Every non-target face has its own bake image, never the final atlas.
        discard_material = bpy.data.materials.new('MWP_Discard_'+run_id)
        discard_material.use_nodes = True
        discard_image = bpy.data.images.new('MWP_Discard_'+run_id,width=16,height=16,alpha=True)
        discard_node = discard_material.node_tree.nodes.new('ShaderNodeTexImage')
        discard_node.image = discard_image
        discard_material.node_tree.nodes.active = discard_node
        for i in range(len(mesh.materials)):
            mesh.materials[i] = temp_material if i in slots else discard_material
        uv = mesh.uv_layers[uv_layer]
        mesh.uv_layers.active = uv
        uv.active_render = True
        layer = scene.view_layers[0]
        layer.objects.active = duplicate
        duplicate.select_set(True,view_layer=layer)
        with bpy.context.temp_override(scene=scene,view_layer=layer,active_object=duplicate,object=duplicate,
                                       selected_objects=[duplicate],selected_editable_objects=[duplicate]):
            graph = bpy.context.evaluated_depsgraph_get()
            evaluated = duplicate.evaluated_get(graph)
            evaluated_mesh = evaluated.to_mesh(preserve_all_data_layers=True,depsgraph=graph)
            try:
                report['uv_evaluated'] = _uv_check(evaluated_mesh,uv_layer,slots,
                    allow_degenerate=any(m.type=='BEVEL' for m in duplicate.modifiers))
                report['evaluated_geometry'] = {'vertices':len(evaluated_mesh.vertices),
                    'polygons':len(evaluated_mesh.polygons),'modifiers':[m.type for m in duplicate.modifiers]}
            finally:
                evaluated.to_mesh_clear()
            nodes = temp_material.node_tree.nodes
            links = temp_material.node_tree.links
            routes = {n.get('mwp_channel'):n for n in nodes if n.get('mwp_channel')}
            target_output = nodes[output.name]
            source_socket = nodes[shader.name].outputs['BSDF']
            emit = nodes.new('ShaderNodeEmission')
            emit.inputs['Strength'].default_value = 1
            image_node = nodes.new('ShaderNodeTexImage')
            # Explicit PNG IOR encoding; signed Height is kept as float EXR.
            subtract = nodes.new('ShaderNodeMath'); subtract.operation='SUBTRACT'; subtract.inputs[1].default_value=1
            divide = nodes.new('ShaderNodeMath'); divide.operation='DIVIDE'; divide.inputs[1].default_value=3
            links.new(subtract.outputs[0],divide.inputs[0])
            scalar = nodes.new('ShaderNodeMath'); scalar.operation='ADD'; scalar.inputs[1].default_value=0
            for channel in channels:
                color_space = 'sRGB' if channel == 'base_color' else 'Non-Color'
                image = bpy.data.images.new('MWP_'+channel+'_'+run_id,width=config['size'],height=config['size'],
                    alpha=True,float_buffer=channel=='height' or config['bit_depth']=='16')
                pending_images.append(image)
                image.generated_color = (0,0,0,0)
                image.colorspace_settings.name = color_space
                image.alpha_mode = 'STRAIGHT'
                image_node.image = image
                for n in nodes:n.select=False
                image_node.select=True
                nodes.active=image_node
                for link in list(target_output.inputs['Surface'].links):links.remove(link)
                if channel == 'normal':
                    links.new(source_socket,target_output.inputs['Surface'])
                    result = bpy.ops.object.bake(type='NORMAL',normal_space='TANGENT',normal_r='POS_X',normal_g='POS_Y',normal_b='POS_Z',
                        target='IMAGE_TEXTURES',margin=config['margin'],margin_type='EXTEND',use_clear=True,uv_layer=uv_layer)
                else:
                    for link in list(emit.inputs['Color'].links):links.remove(link)
                    signal = routes[channel].outputs[0]
                    if channel == 'ior':
                        links.new(signal,subtract.inputs[0]); signal=divide.outputs[0]
                    elif channel!='base_color':
                        links.new(signal,scalar.inputs[0]);signal=scalar.outputs[0]
                    links.new(signal,emit.inputs['Color'])
                    links.new(emit.outputs[0],target_output.inputs['Surface'])
                    result = bpy.ops.object.bake(type='EMIT',target='IMAGE_TEXTURES',margin=config['margin'],
                        margin_type='EXTEND',use_clear=True,uv_layer=uv_layer)
                if result != {'FINISHED'}:
                    _fail('bake_operator', channel+' bake did not finish: '+str(result))
                pixels = _pixels(image)
                stats = _stats(pixels)
                if channel not in ('height','normal'):
                    if min(stats['min']) < -1e-6 or max(stats['max']) > 1.00001:
                        _fail('bake_range', channel+' is outside its declared PNG encoding range')
                suffix = '.exr' if channel=='height' else '.png'
                entry = _save_verified(image,run_dir/(channel+suffix),color_space,config['bit_depth'])
                pending_images.pop()
                if [entry['width'],entry['height']] != [config['size'],config['size']]:
                    _fail('bake_file', 'Actual encoded dimensions differ from requested size')
                entry.update(channel=channel, bake_type='NORMAL' if channel=='normal' else 'EMIT',
                    encoding=({'kind':'linear_range','source_min':1.,'source_max':4.,'decode_scale':3.,'decode_offset':1.}
                              if channel=='ior' else {'kind':'signed_float','zero':0.,'distance':bump_meta['distance']}
                              if channel=='height' else {'kind':'srgb_color'} if channel=='base_color' else {'kind':'unit_data'}))
                report['channels'][channel] = entry
        report['status']='complete'
        report['delivery'].update(complete=True,disk_decode_verified=True)
    except Exception as error:
        failure = error
        report['status']='partial'
        report['error']={'code':getattr(error,'code','bake_failed'),'message':str(error)}
    finally:
        # Only IDs created by this call are removed. No source ID ever receives a write.
        for image in pending_images:
            try:
                if image.name in bpy.data.images:bpy.data.images.remove(image)
            except ReferenceError:
                pass
        if duplicate is not None:bpy.data.objects.remove(duplicate,do_unlink=True)
        if mesh is not None:bpy.data.meshes.remove(mesh)
        for mat in (temp_material,discard_material):
            if mat is not None:bpy.data.materials.remove(mat)
        if discard_image is not None:bpy.data.images.remove(discard_image)
        if scene is not None:bpy.data.scenes.remove(scene)
    report_path = run_dir/('partial.json' if failure else 'report.json')
    report['report_file']=str(report_path)
    _write_report(report_path,report)
    if failure:
        error = WorkflowError(getattr(failure,'code','bake_failed'),str(failure)+'; partial report: '+str(report_path))
        error.report_path=str(report_path)
        error.report=report
        raise error from failure
    return report


def rebuild_material(report, name=None):
    """Create a new material using only report-declared disk images/parameters."""
    import bpy
    if isinstance(report,(str,Path)):
        with open(report,encoding='utf-8') as stream:report=json.load(stream)
    if report.get('schema') != SCHEMA or report.get('status') != 'complete' or not report.get('delivery',{}).get('complete'):
        _fail('bake_incomplete', 'Only a complete verified bake report can be reconstructed')
    # Old reports had no Thickness field and were always rebuilt unlinked.
    thickness = _declared_thickness(report.get('thickness', {'mode':'AUTO','space':'OBJECT','value':None}))
    if report.get('material_properties', {}).get('thickness_mode', 'SPHERE') not in ('SPHERE', 'SLAB'):
        _fail('bake_thickness', 'Unsupported Thickness mode')
    material = None
    loaded = []
    try:
        material = bpy.data.materials.new(name or report['source']['material']+'_Baked')
        material.use_nodes=True
        tree=material.node_tree; nodes=tree.nodes; links=tree.links
        nodes.clear()
        output=nodes.new('ShaderNodeOutputMaterial')
        shader=nodes.new('ShaderNodeBsdfPrincipled')
        uv=nodes.new('ShaderNodeUVMap'); uv.uv_map=report['uv_layer']
        links.new(shader.outputs['BSDF'],output.inputs['Surface'])
        if thickness['mode'] == 'EXPLICIT':
            value = nodes.new('ShaderNodeValue'); value.name = 'Baked Thickness'
            value.outputs[0].default_value = thickness['value']
            links.new(value.outputs[0], output.inputs['Thickness'])
        for key,value in report.get('shader_defaults',{}).items():
            if shader.inputs.get(key) is not None:shader.inputs[key].default_value=value
        for key,value in report.get('shader_properties',{}).items():
            if hasattr(shader,key):setattr(shader,key,value)
        for key,value in report.get('material_properties',{}).items():
            if hasattr(material,key):setattr(material,key,value)
        for channel,socket in SOCKETS.items():
            value=report['constants'].get(channel)
            if value is not None:shader.inputs[socket].default_value=value
            elif channel not in report['channels']:
                _fail('bake_incomplete','Nonconstant channel omitted from package: '+channel)
        signals={}
        for channel,entry in report['channels'].items():
            if channel not in CHANNELS or not entry.get('decoded'):
                _fail('bake_file','Unknown or unverified image channel')
            path=Path(entry['file'])
            if not path.is_absolute() or not path.is_file() or _sha(path)!=entry['sha256']:
                _fail('bake_sha','Missing or changed baked image: '+str(path))
            image=bpy.data.images.load(str(path),check_existing=False);loaded.append(image)
            image.colorspace_settings.name=entry['color_space'];image.alpha_mode='STRAIGHT'
            if list(image.size)!=[entry['width'],entry['height']]:
                _fail('bake_file','Decoded dimensions changed')
            _stats(_pixels(image))
            # Keep Height as an available asset but never connect it twice.
            if channel=='height' and 'normal' in report['channels']:
                bpy.data.images.remove(image);loaded.pop();continue
            node=nodes.new('ShaderNodeTexImage');node.name='Baked '+channel;node.label=channel;node.image=image
            node.interpolation='Linear';node.extension='EXTEND'
            links.new(uv.outputs['UV'],node.inputs['Vector'])
            signal=node.outputs['Color']
            if channel=='ior':
                if entry['encoding']!={'kind':'linear_range','source_min':1.,'source_max':4.,'decode_scale':3.,'decode_offset':1.}:
                    _fail('bake_encoding','Unknown IOR encoding')
                multiply=nodes.new('ShaderNodeMath');multiply.operation='MULTIPLY';multiply.inputs[1].default_value=3
                add=nodes.new('ShaderNodeMath');add.operation='ADD';add.inputs[1].default_value=1
                links.new(signal,multiply.inputs[0]);links.new(multiply.outputs[0],add.inputs[0]);signal=add.outputs[0]
            if channel=='normal':
                normal=nodes.new('ShaderNodeNormalMap');normal.space='TANGENT';normal.uv_map=report['uv_layer']
                links.new(signal,normal.inputs['Color']);signal=normal.outputs['Normal']
            signals[channel]=signal
            if channel in SOCKETS:links.new(signal,shader.inputs[SOCKETS[channel]])
        normal=signals.get('normal')
        if normal is None and ('height' in signals or report['constants'].get('height') not in (None,0,0.)):
            bump=nodes.new('ShaderNodeBump')
            bump.inputs['Distance'].default_value=report['height']['distance']
            bump.inputs['Strength'].default_value=report['height']['strength'];bump.invert=report['height']['invert']
            if 'height' in signals:links.new(signals['height'],bump.inputs['Height'])
            else:bump.inputs['Height'].default_value=report['constants']['height']
            normal=bump.outputs['Normal']
        if normal is not None:
            links.new(normal,shader.inputs['Normal'])
            if report.get('coat_normal_follows_base'):links.new(normal,shader.inputs['Coat Normal'])
        if report['constants'].get('height') is None and 'normal' not in signals and 'height' not in signals:
            _fail('bake_incomplete','Nonconstant height/normal omitted')
        material['mwp_bake_report']=json.dumps(report,ensure_ascii=False,separators=(',',':'))
        material['mwp_rebuild']='disk-images-and-declared-parameters-only'
        material['mwp_height_distance']=report['height']['distance']
        return material
    except Exception:
        if material is not None:bpy.data.materials.remove(material)
        for image in loaded:
            try:bpy.data.images.remove(image)
            except ReferenceError:pass
        raise
