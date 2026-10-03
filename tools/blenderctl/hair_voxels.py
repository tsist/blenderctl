# SPDX-License-Identifier: GPL-3.0-or-later
"""Defined scalar envelope: max over quadratic compact segment kernels."""
import numpy as np
from protocol import Failure
def rasterize(points,sizes,voxel,radius,density,limit):
    points=np.asarray(points,dtype=np.float64)
    if not np.isfinite(points).all():raise Failure('VALIDATION_FAILED','Nonfinite curve field source')
    lo=np.floor((points.min(axis=0)-radius)/voxel).astype(np.int64);hi=np.ceil((points.max(axis=0)+radius)/voxel).astype(np.int64)
    if max(abs(lo).max(),abs(hi).max())>100000000:raise Failure('UNSUPPORTED','VDB index magnitude exceeds bounded domain')
    shape=hi-lo+1;count=int(np.prod(shape,dtype=np.float64))
    if count>limit:raise Failure('UNSUPPORTED',f'Volume dense bounding grid requires {count} voxels, budget {limit}')
    segments=[];work=0;start=0
    for size in sizes:
        for a,b in zip(points[start:start+size-1],points[start+1:start+size]):
            low=np.maximum(lo,np.floor((np.minimum(a,b)-radius)/voxel).astype(np.int64));high=np.minimum(hi,np.ceil((np.maximum(a,b)+radius)/voxel).astype(np.int64));work+=int(np.prod(high-low+1))
            if work>50000000:raise Failure('UNSUPPORTED','Volume exceeds 50 million voxel/segment evaluations per frame')
            if np.dot(b-a,b-a)<1e-18:raise Failure('UNSUPPORTED','Degenerate curve segment in volume source')
            segments.append((a,b,low,high))
        start+=size
    if start!=len(points):raise Failure('VALIDATION_FAILED','Curve field topology mismatch')
    grid=np.zeros(tuple(shape),np.float32)
    for a,b,low,high in segments:
        x,y,z=np.meshgrid(*[np.arange(low[i],high[i]+1,dtype=np.float64)*voxel for i in range(3)],indexing='ij');v=np.stack((x,y,z),axis=-1);ab=b-a;t=np.clip(np.sum((v-a)*ab,axis=-1)/np.dot(ab,ab),0,1);distance=np.linalg.norm(v-(a+t[...,None]*ab),axis=-1);values=density*np.maximum(0,1-distance/radius)**2
        region=tuple(slice(int(low[i]-lo[i]),int(high[i]-lo[i])+1) for i in range(3));np.maximum(grid[region],values.astype(np.float32),out=grid[region])
    if not np.isfinite(grid).all() or not np.any(grid>0):raise Failure('VALIDATION_FAILED','Empty or invalid curve density field')
    return grid,lo,work
