# SPDX-License-Identifier: GPL-3.0-or-later
"""M1 static mesh studio. Source RNA is read only; rendering is caller owned.

Requires the selected source scene/view layer/frame to be active before capture.
Evaluated viewport geometry is accepted only with identical render modifiers.
No instance realization, motion evaluation, object-coordinate fixture, or GPU setup.
"""
import hashlib
import itertools
import json
import math
from pathlib import Path


def _sha(path):
    with open(path, 'rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def validate_materials(objects, engine):
    """Check actual face materials, including retained slots, before studio copies."""
    from .engine_compat import validate_material
    results = []
    for obj in objects:
        used = {face.material_index for face in obj.data.polygons}
        for index in sorted(used):
            if index < len(obj.material_slots):
                material = obj.material_slots[index].material
                if material:
                    results.append({'object': obj.name, 'slot': index,
                                    **validate_material(material, engine)})
    return results


def build_preview(manifest, target_obj):
    import bpy
    from mathutils import Vector
    context = manifest['context']
    source = bpy.context.scene
    layer = bpy.context.view_layer
    if (source.name != context['scene'] or layer.name != context['view_layer']
            or source.frame_current != context['frame']):
        raise ValueError('Source scene/view layer/frame must be active before preview capture')
    if target_obj.type != 'MESH' or target_obj.name not in layer.objects:
        raise ValueError('M1 requires one mesh belonging to the selected view layer')
    if target_obj.instance_type != 'NONE':
        raise ValueError('M1 does not realize object instances')
    for modifier in target_obj.modifiers:
        if modifier.show_viewport != modifier.show_render:
            raise ValueError('Viewport/render modifier mismatch: ' + modifier.name)
        if modifier.type == 'SUBSURF' and modifier.levels != modifier.render_levels:
            raise ValueError('Subdivision viewport/render levels differ')
    graph = bpy.context.evaluated_depsgraph_get()
    if any(i.is_instance and i.parent and i.parent.original == target_obj for i in graph.object_instances):
        raise ValueError('M1 evaluated instances need explicit realization')
    evaluated = target_obj.evaluated_get(graph)
    validate_materials([evaluated], manifest['preview']['engine'])
    mesh = bpy.data.meshes.new_from_object(evaluated, preserve_all_data_layers=True, depsgraph=graph)
    if not mesh.polygons:
        bpy.data.meshes.remove(mesh)
        raise ValueError('Preview mesh contains no surface')
    # Object material overrides are preserved, including empty slots.
    materials = [slot.material for slot in evaluated.material_slots]
    for index, material in enumerate(materials):
        mesh.materials[index] = material
    uv = manifest['target'].get('uv_layer')
    if uv:
        selected_uv = mesh.uv_layers.get(uv)
        if selected_uv is None:
            bpy.data.meshes.remove(mesh)
            raise ValueError('Requested UV layer missing from evaluated mesh: ' + uv)
        mesh.uv_layers.active = selected_uv
        selected_uv.active_render = True
    matrix = evaluated.matrix_world.copy()
    points = [matrix @ vertex.co for vertex in mesh.vertices]
    if len(points) > 500000 or any(not math.isfinite(x) for p in points for x in p):
        bpy.data.meshes.remove(mesh)
        raise ValueError('M1 requires finite geometry with at most 500000 vertices')
    lo = Vector([min(p[i] for p in points) for i in range(3)])
    hi = Vector([max(p[i] for p in points) for i in range(3)])
    size = (hi-lo).length
    if not 1e-5 <= size <= 1e6:
        bpy.data.meshes.remove(mesh)
        raise ValueError('Preview bounds outside supported extent')
    center = (lo+hi)*.5
    scene = bpy.data.scenes.new('MaterialWorkflowPreview')
    obj = bpy.data.objects.new('MW_Target', mesh)
    scene.collection.objects.link(obj)
    obj.matrix_world = matrix
    obj.location -= center
    points = [p-center for p in points]
    lo -= center
    hi -= center
    preview = manifest['preview']
    scene.frame_set(context['frame'])
    scene.render.engine = preview['engine']
    scene.render.resolution_x = preview['width']
    scene.render.resolution_y = preview['height']
    scene.render.resolution_percentage = 100
    scene.render.pixel_aspect_x = scene.render.pixel_aspect_y = 1
    scene.render.film_transparent = False
    scene.render.use_compositing = scene.render.use_sequencer = False
    if preview['engine'] == 'CYCLES':
        scene.cycles.samples = preview['samples']
        scene.cycles.use_denoising = preview['denoise']
    else:
        scene.eevee.taa_render_samples = preview['samples']
    scene.display_settings.display_device = 'sRGB'
    color = preview['color']
    scene.view_settings.view_transform = color['view']
    scene.view_settings.look = 'None'
    scene.view_settings.exposure = color['exposure']
    scene.view_settings.gamma = color['gamma']
    world = bpy.data.worlds.new('MW_World')
    world.use_nodes = True
    background = next(n for n in world.node_tree.nodes if n.bl_idname == 'ShaderNodeBackground')
    background.inputs['Color'].default_value = (.18, .18, .18, 1)
    background.inputs['Strength'].default_value = .25
    scene.world = world
    camera = bpy.data.objects.new('MW_Camera', bpy.data.cameras.new('MW_Camera'))
    scene.collection.objects.link(camera)
    scene.camera = camera
    # A single plane below the bbox cannot cover a vessel's interior or walls.
    floor_mesh = bpy.data.meshes.new('MW_Ground')
    z = lo.z-size*1e-4
    floor_mesh.from_pydata([(-size*20,-size*20,z),(size*20,-size*20,z),
                          (size*20,size*20,z),(-size*20,size*20,z)], [], [(0,1,2,3)])
    ground = bpy.data.objects.new('MW_Ground', floor_mesh)
    scene.collection.objects.link(ground)
    material = bpy.data.materials.new('MW_Ground')
    material.use_nodes = True
    shader = next(n for n in material.node_tree.nodes if n.bl_idname == 'ShaderNodeBsdfPrincipled')
    shader.inputs['Base Color'].default_value = (.18,.18,.18,1)
    shader.inputs['Roughness'].default_value = .8
    floor_mesh.materials.append(material)
    lights = {}
    for mode, entries in {
        'soft': [((-2,-3,4),100,2),((3,-1,2),35,2),((1,3,3),70,1.5)],
        'raking': [((-3,-1,.8),110,.45),((2,1,3),12,2)],
    }.items():
        lights[mode] = []
        for index, (direction, power, radius) in enumerate(entries):
            data = bpy.data.lights.new('MW_%s_%d' % (mode,index), 'AREA')
            data.energy = power*size*size
            data.shape = 'DISK'
            data.size = radius*size
            light = bpy.data.objects.new(data.name, data)
            scene.collection.objects.link(light)
            light.location = Vector(direction).normalized()*size*2
            light.rotation_euler = (-light.location).to_track_quat('-Z','Y').to_euler()
            light.hide_render = mode != 'soft'
            lights[mode].append(light)
    return {'scene': scene, 'view_layer': scene.view_layers[0], 'camera': camera,
            'object': obj, 'points': points, 'bounds': (lo,hi), 'size': size,
            'source_center': list(center), 'lights': lights, 'ground': ground, 'manifest': manifest,
            'limitations': ['single static evaluated mesh; no instances',
                            'viewport and render modifier settings must match',
                            'bounded static surface shaders only; context-dependent nodes are rejected']}


def configure_view(rig, view):
    import bpy
    from mathutils import Vector
    from bpy_extras.object_utils import world_to_camera_view
    scene, camera = rig['scene'], rig['camera']
    lo, hi = rig['bounds']
    focus = view.get('focus', [.5,.5,.5])
    zoom = float(view.get('zoom', 1))
    if len(focus) != 3 or any(not math.isfinite(x) or not 0 <= x <= 1 for x in focus) or not math.isfinite(zoom) or zoom <= 0:
        raise ValueError('Focus must be normalized bbox xyz and zoom positive')
    center = Vector([lo[i]+(hi[i]-lo[i])*focus[i] for i in range(3)])
    azimuth, elevation = math.radians(view['azimuth']), math.radians(view['elevation'])
    back = Vector((math.sin(azimuth)*math.cos(elevation),
                   -math.cos(azimuth)*math.cos(elevation),math.sin(elevation)))
    rotation = (-back).to_track_quat('-Z','Y')
    right, up = rotation @ Vector((1,0,0)), rotation @ Vector((0,1,0))
    corners = [Vector(p) for p in itertools.product(*[(lo[i],hi[i]) for i in range(3)])]
    bbox_center = (lo+hi)*.5
    aspect = scene.render.resolution_x/scene.render.resolution_y
    # HORIZONTAL sensor fit gives ortho_scale as horizontal span.
    span = max(2*max(abs((p-bbox_center).dot(right)) for p in corners),
               2*max(abs((p-bbox_center).dot(up)) for p in corners)*aspect)
    camera.data.type = 'ORTHO'
    camera.data.sensor_fit = 'HORIZONTAL'
    camera.data.ortho_scale = span*rig['manifest']['preview'].get('margin',1.15)/zoom
    camera.data.shift_x = camera.data.shift_y = 0
    camera.location = center+back*rig['size']*3
    camera.rotation_mode = 'QUATERNION'
    camera.rotation_quaternion = rotation
    camera.data.clip_start = max(rig['size']*.001,1e-6)
    camera.data.clip_end = rig['size']*10
    mode = view['lighting']
    if mode not in rig['lights']:
        raise ValueError('Unknown lighting mode')
    for name, lights in rig['lights'].items():
        for light in lights:
            light.hide_render = name != mode
    # Underground views must see the target instead of the opaque studio floor.
    rig['ground'].hide_render = view['elevation'] < 0
    # Explicit update of this preview layer without switching the user's scene.
    rig['view_layer'].update()
    ndc = [world_to_camera_view(scene,camera,p) for p in corners]
    fits = all(-2e-5 <= p.x <= 1.00002 and -2e-5 <= p.y <= 1.00002
               and camera.data.clip_start < p.z < camera.data.clip_end for p in ndc)
    whole = zoom <= 1 and all(abs(v-.5)<1e-6 for v in focus)
    if whole and not fits:
        raise ValueError('Independent bbox projection rejected whole-object framing')
    return {'id':view['id'], 'lighting':mode, 'projection':'ORTHO',
            'focus':list(focus), 'focus_world':list(center), 'zoom':zoom,
            'ortho_scale':camera.data.ortho_scale, 'source_center':rig['source_center'],
            'camera_matrix':[list(row) for row in camera.matrix_world],
            'projected_bounds':[[min(p[i] for p in ndc) for i in range(3)],
                                [max(p[i] for p in ndc) for i in range(3)]],
            'whole_object_fits':fits, 'framing':'pass' if whole else 'intentional_crop',
            'bbox_corners_checked':8, 'ground_visible':not rig['ground'].hide_render}


def compose_sheet(outputs, destination, max_edge=2048):
    """Display-byte collage; no OCIO or second view transform is applied.

    OIIO/numpy are used if available, otherwise an explicit capability error.
    Missing/invalid views get grey cells and an indexed error, never success.
    """
    try:
        import OpenImageIO as oiio
        import numpy as np
    except ImportError as error:
        raise RuntimeError('Contact sheets require available OpenImageIO and numpy; no installation attempted') from error
    if not outputs or len(outputs)>24 or not 32 <= max_edge <= 2048:
        raise ValueError('Sheet requires 1..24 views and max_edge 32..2048')
    destination = Path(destination).resolve()
    index_path = destination.with_suffix('.index.json')
    if destination.suffix.lower() != '.png' or destination.exists() or index_path.exists():
        raise ValueError('Sheet destination must be a new PNG path')
    cols = math.ceil(math.sqrt(len(outputs)))
    rows = math.ceil(len(outputs)/cols)
    cell = max_edge//max(cols,rows)
    if cell < 32:
        raise ValueError('max_edge too small for readable numbered cells')
    layout, decoded = [], []
    for index, item in enumerate(outputs):
        row = {'number':index+1, 'id':item.get('id',str(index+1)),
               'lighting':item.get('lighting'), 'cell':[index%cols,index//cols],
               'status':'failed', 'file':item.get('file')}
        pixels = None
        try:
            if item.get('status') in ('failed','fail','error','not_run'):
                raise ValueError(item.get('error') or 'View did not render successfully')
            path = Path(item['file'])
            if path.suffix.lower() != '.png':
                raise ValueError('Input must be display-referred PNG')
            digest = _sha(path)
            if item.get('sha256') and digest != item['sha256']:
                raise ValueError('Input SHA mismatch')
            inp = oiio.ImageInput.open(str(path))
            if inp is None:
                raise ValueError('Cannot decode PNG')
            try:
                spec = inp.spec()
                pixels = np.asarray(inp.read_image(format=oiio.UINT8))
                if pixels.ndim != 3 or pixels.shape[2] not in (3,4):
                    raise ValueError('PNG requires RGB or RGBA pixels')
                row.update(width=spec.width,height=spec.height,sha256=digest,bytes=path.stat().st_size)
            finally:
                inp.close()
            if pixels.shape[2] == 4:
                alpha = pixels[:,:,3:4].astype(np.float32)/255
                pixels = (pixels[:,:,:3]*alpha + 36*(1-alpha)).astype(np.uint8)
            row['status'] = 'pass'
        except Exception as error:
            row['error'] = str(error)
            pixels = None
        layout.append(row)
        decoded.append(pixels)
    # Numeric 3x5 labels map directly to the adjacent JSON index.
    digits = ['111101101101111','010110010010111','111001111100111',
              '111001111001111','101101111001001','111100111001111',
              '111100111101111','111001001001001','111101111101111','111101111001111']
    while True:
        canvas = np.full((rows*cell,cols*cell,3),36,dtype=np.uint8)
        for index,pixels in enumerate(decoded):
            x,y = index%cols*cell,index//cols*cell
            if pixels is not None:
                h,w = pixels.shape[:2]
                scale = min((cell-8)/w,(cell-26)/h)
                rw,rh = max(1,int(w*scale)),max(1,int(h*scale))
                # Area-independent byte resampling keeps source display encoding.
                ix = np.minimum((np.arange(rw)*w/rw).astype(int),w-1)
                iy = np.minimum((np.arange(rh)*h/rh).astype(int),h-1)
                ox,oy = x+(cell-rw)//2,y+22+(cell-22-rh)//2
                canvas[oy:oy+rh,ox:ox+rw] = pixels[iy[:,None],ix]
                layout[index]['image_rect'] = [ox,oy,rw,rh]
            for n,char in enumerate(str(index+1)):
                for bit,value in enumerate(digits[int(char)]):
                    if value=='1':
                        bx,by = x+5+n*8+(bit%3)*2,y+5+(bit//3)*2
                        canvas[by:by+2,bx:bx+2] = 230
        destination.parent.mkdir(parents=True,exist_ok=True)
        out = oiio.ImageOutput.create(str(destination))
        spec = oiio.ImageSpec(canvas.shape[1],canvas.shape[0],3,oiio.UINT8)
        if out is None:
            raise RuntimeError('PNG output unavailable')
        try:
            if not out.open(str(destination),spec) or not out.write_image(canvas):
                raise RuntimeError('PNG sheet encode failed: '+out.geterror())
        finally:
            out.close()
        if destination.stat().st_size <= 2*1024*1024:
            break
        cell = int(cell*.8)
        if cell < 32:
            raise RuntimeError('Cannot meet 2 MiB preview budget')
    check = oiio.ImageInput.open(str(destination))
    try:
        if check is None or check.read_image(format=oiio.UINT8) is None:
            raise RuntimeError('Sheet decode verification failed')
        width,height = check.spec().width,check.spec().height
    finally:
        if check:check.close()
    report = {'file':str(destination),'sha256':_sha(destination),
              'bytes':destination.stat().st_size,'width':width,'height':height,
              'rows':rows,'columns':cols,'layout':layout,
              'status':('pass' if all(r['status']=='pass' for r in layout)
                        else 'partial' if any(r['status']=='pass' for r in layout) else 'failed'),
              'color_scope':'display-referred source bytes; no additional view transform',
              'resampling':'nearest; source originals unchanged', 'decoded':True}
    index_path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    report['index_file'] = str(index_path)
    return report
