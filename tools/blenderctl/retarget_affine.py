# SPDX-License-Identifier: GPL-3.0-or-later
"""Explicit non-deform SVD helper adapter for finite positive affine bases."""
import math
from contextlib import contextmanager
import bpy
import numpy as np
from mathutils import Matrix,Quaternion,Vector
from protocol import Failure

def _matrix(transform):
    return Matrix.LocRotScale(Vector(transform['location']),Quaternion(transform['rotation_quaternion']),Vector(transform['scale']))

def factor_basis(matrix):
    """Return H,B with H @ B = matrix, verifying actual float32 mathutils TRS."""
    raw=np.asarray(matrix,dtype=np.float64)
    if raw.shape!=(4,4) or not np.isfinite(raw).all():raise Failure('UNSUPPORTED','Affine basis must be a finite 4x4 matrix')
    if np.max(np.abs(raw[3]-[0,0,0,1]))>1e-7:raise Failure('UNSUPPORTED','Projective matrix is outside SVD helper adapter')
    det=float(np.linalg.det(raw[:3,:3]))
    if not math.isfinite(det) or det<=1e-12:raise Failure('UNSUPPORTED','SVD helpers require nonsingular positive determinant')
    try:u,scale,vt=np.linalg.svd(raw[:3,:3])
    except np.linalg.LinAlgError as exc:raise Failure('VALIDATION_FAILED','Affine SVD did not converge') from exc
    if float(min(scale))<1e-8:raise Failure('UNSUPPORTED','Near singular affine basis is outside SVD helpers')
    if np.linalg.det(u)<0:u[:,-1]*=-1;scale[-1]*=-1
    if np.linalg.det(vt)<0:vt[-1,:]*=-1;scale[-1]*=-1
    h={'location':raw[:3,3].tolist(),'rotation_quaternion':list(Matrix(u.tolist()).to_quaternion()),'scale':scale.tolist()}
    b={'location':[0.,0.,0.],'rotation_quaternion':list(Matrix(vt.tolist()).to_quaternion()),'scale':[1.,1.,1.]}
    reconstructed=_matrix(h)@_matrix(b)
    error=max(abs(float(reconstructed[i][j])-raw[i,j]) for i in range(4) for j in range(4))
    if not math.isfinite(error) or error>=1e-5:raise Failure('VALIDATION_FAILED','SVD helper float32 reconstruction exceeds 1e-5: '+str(error))
    return h,b

@contextmanager
def independent_edit(target):
    """Prevent Blender's edit-time mirror coupling, then restore user settings."""
    mirror = target.data.use_mirror_x
    target.data.use_mirror_x = False
    try:
        yield
    finally:
        target.data.use_mirror_x = mirror

def create_helpers(target,names,context,max_rest_error=1e-6,report=None):
    """Insert helpers only into the explicitly supplied isolated target rig."""
    import modeling
    if type(max_rest_error) not in (int,float) or not math.isfinite(max_rest_error) or not 1e-7<=max_rest_error<=1e-4:
        raise Failure('INVALID_REQUEST','Helper rest error budget must be within 1e-7..1e-4')
    names=list(names)
    if target.type!='ARMATURE' or target.library or target.override_library or target.data.library or target.data.override_library:
        raise Failure('UNSUPPORTED','SVD helpers require a local non-override armature target')
    if target.data.users>1:raise Failure('CONFLICT','SVD helpers require isolated target armature data')
    if len(set(names))!=len(names) or not names:raise Failure('INVALID_REQUEST','Unique nonempty helper bone list required')
    mapping={name:'__S08_Affine_'+name for name in names};blueprint={}
    for name,helper in mapping.items():
        bone=target.data.bones.get(name)
        if bone is None:raise Failure('NOT_FOUND','Missing helper target bone '+name)
        if len(helper)>63 or len(helper.encode('utf-8'))>63 or any(ord(c)<32 or ord(c)==127 for c in helper):
            raise Failure('INVALID_REQUEST','Generated SVD helper name exceeds Blender name bounds: '+name)
        if helper in target.data.bones:raise Failure('CONFLICT','Generated SVD helper name already exists: '+helper)
        if bone.inherit_scale!='FULL' or not bone.use_inherit_rotation or not bone.use_local_location:
            raise Failure('UNSUPPORTED','SVD helpers require FULL scale, inherited rotation and local location')
        blueprint[name]={'matrix':bone.matrix_local.copy(),'length':bone.length,'parent':bone.parent.name if bone.parent else None,'deform':bone.use_deform}
    with independent_edit(target), modeling.operator_context(target,context):
        bpy.ops.object.mode_set(mode='EDIT')
        for name,helper in mapping.items():
            row=blueprint[name];original=target.data.edit_bones[name];bone=target.data.edit_bones.new(helper)
            if bone.name!=helper:raise Failure('CONFLICT','Blender changed generated helper name')
            # Preserve Blender's native representation without an additional
            # matrix-to-roll conversion for short or nearly reversed bones.
            bone.head=original.head.copy();bone.tail=original.tail.copy();bone.roll=original.roll
            bone.use_deform=False;bone.use_connect=False
            bone.inherit_scale='FULL';bone.use_inherit_rotation=True;bone.use_local_location=True
        for name,helper in mapping.items():
            row=blueprint[name];original=target.data.edit_bones[name];bone=target.data.edit_bones[helper]
            bone.parent=target.data.edit_bones.get(row['parent']) if row['parent'] else None
            original.use_connect=False;original.parent=bone;original.use_deform=row['deform']
            original.inherit_scale='FULL';original.use_inherit_rotation=True;original.use_local_location=True
        bpy.ops.object.mode_set(mode='OBJECT')
    rest_errors=[]
    for name,row in blueprint.items():
        bone=target.data.bones[name]
        error=max(abs(a-b) for x,y in zip(bone.matrix_local,row['matrix']) for a,b in zip(x,y))
        helper=target.data.bones[mapping[name]]
        helper_error=max(abs(a-b) for x,y in zip(helper.matrix_local,row['matrix']) for a,b in zip(x,y))
        length_error=abs(bone.length-row['length'])
        rest_errors.append({'bone':name,'original_matrix_error':error,'helper_matrix_error':helper_error,'length_error':length_error})
        if max(error,helper_error,length_error)>max_rest_error or bone.use_deform!=row['deform'] or helper.use_deform:
            raise Failure('VALIDATION_FAILED','Helper insertion exceeded declared rest budget or changed deform flags: '+name+'; '+str(rest_errors[-1]))
    if report is not None:
        report.update({'max_rest_error':max_rest_error,'maximum_observed_error':max(max(x['original_matrix_error'],x['helper_matrix_error'],x['length_error']) for x in rest_errors),'bones':rest_errors})
    return mapping
