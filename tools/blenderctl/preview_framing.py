# SPDX-License-Identifier: GPL-3.0-or-later
"""World-space point framing with independent projection checks."""
import math
import bpy
from mathutils import Vector
from bpy_extras.object_utils import world_to_camera_view
from protocol import Failure
DIRECTIONS={'FRONT':(0,-1,0),'RIGHT':(1,0,0),'BACK':(0,1,0),'THREE_QUARTER':(1,-1,.65)}
def bounds(points):
    if not points:raise Failure('VALIDATION_FAILED','Preview target has no evaluated surface vertices')
    if any(not math.isfinite(x) for p in points for x in p):raise Failure('VALIDATION_FAILED','Nonfinite preview geometry')
    lo=Vector(tuple(min(p[i] for p in points) for i in range(3)));hi=Vector(tuple(max(p[i] for p in points) for i in range(3)));extent=(hi-lo).length
    if not 1e-5<=extent<=1e6 or max(abs(x) for p in (lo,hi) for x in p)>1e7:raise Failure('UNSUPPORTED','Preview scale/coordinates outside 1e-5..1e6 extent and 1e7 coordinate bound')
    return lo,hi
def fit(scene,camera,points,view,projection,margin):
    lo,hi=bounds(points);center=(lo+hi)*.5;size=(hi-lo).length;back=Vector(DIRECTIONS[view]).normalized();q=(-back).to_track_quat('-Z','Y');right=q@Vector((1,0,0));up=q@Vector((0,1,0));aspect=scene.render.resolution_x/scene.render.resolution_y
    projected=[((p-center).dot(right),(p-center).dot(up),(p-center).dot(back)) for p in points];keep=1-2*margin
    camera.data.type=projection;camera.data.sensor_fit='HORIZONTAL';camera.data.sensor_width=36;camera.data.lens=50;camera.data.shift_x=camera.data.shift_y=0
    if projection=='ORTHO':
        camera.data.ortho_scale=max(2*max(abs(p[0]) for p in projected)/keep,2*max(abs(p[1]) for p in projected)*aspect/keep);distance=2*size
    else:
        tan_x=.36;tan_y=tan_x/aspect;distance=max(z+max(abs(x)/(tan_x*keep),abs(y)/(tan_y*keep)) for x,y,z in projected)+size*1e-5
    near=min(distance-p[2] for p in projected);far=max(distance-p[2] for p in projected)
    camera.location=center+back*distance;camera.rotation_mode='QUATERNION';camera.rotation_quaternion=q;camera.data.clip_start=max(near*.25,1e-6);camera.data.clip_end=max(far*2,camera.data.clip_start*10);scene.camera=camera;bpy.context.view_layer.update()
    ndc=[world_to_camera_view(scene,camera,p) for p in points];tolerance=2e-5
    if any(p.x<margin-tolerance or p.x>1-margin+tolerance or p.y<margin-tolerance or p.y>1-margin+tolerance or not camera.data.clip_start<p.z<camera.data.clip_end for p in ndc):raise Failure('VALIDATION_FAILED','Frustum oracle rejected preview framing')
    return {'view':view,'projection':projection,'margin':margin,'world_bounds':[list(lo),list(hi)],'camera_matrix':[list(r) for r in camera.matrix_world],'ortho_scale':camera.data.ortho_scale,'lens':camera.data.lens,'clip':[camera.data.clip_start,camera.data.clip_end],'projected_bounds':[[min(p[i] for p in ndc) for i in range(3)],[max(p[i] for p in ndc) for i in range(3)]],'vertices_checked':len(points),'frustum':'pass'}
