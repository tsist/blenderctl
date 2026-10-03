# SPDX-License-Identifier: GPL-3.0-or-later
"""Self-contained pinhole review copy derived from an already verified solve."""
import bpy,json,math
from mathutils import Matrix,Vector
from protocol import Failure,atomic_json,digest
from tracking_contract import validate,REVIEW_REPORT

def create(meta,observation,primary,job):
    spec=meta['solve'];recipe=meta['recipe'];scene=bpy.context.scene
    camera=scene.camera;points=bpy.data.objects[spec['points_name']];points.hide_render=True
    first=Matrix(observation['cameras'][0]['matrix']).inverted()
    radius_pixels=spec['preview_points']['radius_pixels'];vertices=[];faces=[]
    # Static spheres preserve the measured centre; radii are anchored to frame one.
    for item in observation['bundles']:
        x,y,z=item['co'];local=first@Vector((x,y,z));radius=-local.z*radius_pixels/recipe['focal_length_pixels']
        base=len(vertices);rings=8;segments=16
        for j in range(rings+1):
            phi=math.pi*j/rings
            for k in range(segments):
                theta=math.tau*k/segments;vertices.append((x+radius*math.sin(phi)*math.cos(theta),y+radius*math.sin(phi)*math.sin(theta),z+radius*math.cos(phi)))
        for j in range(rings):
            for k in range(segments):
                a=base+j*segments+k;b=base+j*segments+(k+1)%segments;faces.append((a,b,b+segments,a+segments))
    mesh=bpy.data.meshes.new('TrackingReviewDots');mesh.from_pydata(vertices,[],faces)
    dots=bpy.data.objects.new('TrackingReviewDots',mesh);scene.collection.objects.link(dots)
    material=bpy.data.materials.new('TrackingReviewEmission');material.use_nodes=True
    nodes=material.node_tree.nodes;nodes.clear();output=nodes.new('ShaderNodeOutputMaterial');emission=nodes.new('ShaderNodeEmission')
    emission.inputs['Color'].default_value=(1,.08,.015,1);material.node_tree.links.new(emission.outputs[0],output.inputs['Surface']);mesh.materials.append(material)
    for clip in list(bpy.data.movieclips):bpy.data.movieclips.remove(clip)
    scene['tracking_review_source_sha256']=primary['candidate_sha256']
    scene['tracking_review_space']='UNDISTORTED_PINHOLE; apply declared lens model before overlay on raw footage'
    name=dots.name;path=job/'tracking-review.blend';bpy.ops.wm.save_as_mainfile(filepath=str(path),relative_remap=False,check_existing=False)
    bpy.ops.wm.open_mainfile(filepath=str(path),load_ui=False,use_scripts=False)
    errors=[];start=recipe.get('scene_start',1)
    for row in observation['cameras']:
        bpy.context.scene.frame_set(start+row['frame']-1);mat=bpy.context.scene.camera.matrix_world
        errors.append(max(abs(mat[i][j]-row['matrix'][i][j]) for i in range(4) for j in range(4)))
    actual=bpy.data.objects[name].data
    if len(actual.vertices)!=len(vertices) or len(actual.polygons)!=len(faces):raise Failure('VALIDATION_FAILED','Review mesh reopen topology changed')
    geometry_error=max(abs(v.co[k]-expected[k]) for v,expected in zip(actual.vertices,vertices) for k in range(3))
    if max(errors)>1e-5 or geometry_error>1e-5 or bpy.data.movieclips or bpy.data.images or bpy.data.sounds or bpy.data.libraries or any(font.filepath not in ('','<builtin>') for font in bpy.data.fonts):raise Failure('VALIDATION_FAILED','Review copy reopen differs or contains external media')
    report={'version':'1.0','primary_sha256':primary['candidate_sha256'],'review_sha256':digest(path),'point_count':len(observation['bundles']),
            'radius_pixels_at_first_frame':radius_pixels,'camera_reopen_max_error':max(errors),'geometry_reopen_max_error':geometry_error,
            'projection_space':'UNDISTORTED_PINHOLE','calibration':recipe.get('calibration'),'external_files':[],
            'scope':'static review spheres at solved bundle centres; original solved camera animation, no metric scale or raw-distorted overlay claim'}
    validate(report,REVIEW_REPORT);atomic_json(job/'tracking-review.json',report)
    if digest(primary['candidate'])!=primary['candidate_sha256']:raise Failure('CONFLICT','Primary tracking candidate changed during review-copy creation')
    return {'review_candidate':str(path),'review_candidate_sha256':digest(path),'review_report':str(job/'tracking-review.json')}
