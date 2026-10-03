# SPDX-License-Identifier: GPL-3.0-or-later
"""Independent bounded reconstruction checks; no native-average-only acceptance."""
import math
import numpy as np


def evaluate(observation, width, height, focal, spec, frame_count, native_error, calibration=None):
    failures=[]
    def fail(code,detail):failures.append({'code':code,'detail':detail})
    tracks={t['name']:{m['frame']:m for m in t['markers'] if not m['mute']} for t in observation['tracks']}
    cameras={c['frame']:np.asarray(c['matrix'],dtype=float) for c in observation['cameras']}
    bundles={b['name']:np.asarray(b['co'],dtype=float) for b in observation['bundles']}
    frames=list(range(1,frame_count+1));errors=[];depths=[];coverage=[];angles=[]
    if set(cameras)!=set(frames):fail('MISSING_CAMERAS','Reconstruction must include every declared frame')
    if len(bundles)<spec['min_tracks']:fail('INSUFFICIENT_BUNDLES','Too few reconstructed tracks')
    if not math.isfinite(native_error) or native_error>spec['max_average_error']:fail('NATIVE_ERROR','Native average error exceeds budget')
    finite=all(np.isfinite(v).all() for v in list(cameras.values())+list(bundles.values())+[np.asarray(m['co'],dtype=float) for t in observation['tracks'] for m in t['markers']])
    if not finite:fail('NONFINITE','Camera or bundle contains non-finite values')
    if finite:
        for frame in frames:
            present=[n for n in bundles if frame in tracks.get(n,{})]
            coverage.append({'frame':frame,'tracks':len(present)})
            if len(present)<spec['min_tracks']:fail('FRAME_COVERAGE',f'Frame {frame} has too few reconstructed markers')
            matrix=cameras.get(frame)
            if matrix is None:continue
            try:inverse=np.linalg.inv(matrix)
            except np.linalg.LinAlgError:fail('SINGULAR_CAMERA',str(frame));continue
            if np.max(np.abs(matrix[3]-[0,0,0,1]))>1e-7 or abs(np.linalg.det(matrix[:3,:3])-1)>1e-3 or np.max(np.abs(matrix[:3,:3].T@matrix[:3,:3]-np.eye(3)))>1e-3:
                fail('NONRIGID_CAMERA',str(frame));continue
            for name in present:
                local=inverse@np.append(bundles[name],1)
                depth=-float(local[2]);depths.append(depth)
                if depth<=1e-8:fail('NONPOSITIVE_DEPTH',f'{name} frame {frame}');continue
                uv=np.asarray(tracks[name][frame]['co'])*[width,height]
                normalized=local[:2]/depth
                if calibration:
                    r2=float(normalized@normalized)
                    radial=1+calibration['k1']*r2+calibration['k2']*r2**2+calibration['k3']*r2**3
                    projected=focal*normalized*radial+calibration['principal_point_pixels']
                else:projected=focal*normalized+[width/2,height/2]
                error=float(np.linalg.norm(projected-uv))
                if not math.isfinite(error):fail('NONFINITE_REPROJECTION',f'{name} frame {frame}')
                else:errors.append(error)
        count=len(tracks)*frame_count
        observed=sum(sum(f in ms for f in frames) for ms in tracks.values())
        missing=1-observed/count if count else 1
        if missing>spec['max_missing_fraction']:fail('MISSING_MARKERS','Missing or muted marker fraction exceeds budget')
        if not errors or max(errors)>spec['max_reprojection_error']:fail('REPROJECTION','Independent maximum reprojection error exceeds budget')
        xyz=np.asarray(list(bundles.values()))
        singular=np.linalg.svd(xyz-xyz.mean(axis=0),compute_uv=False) if len(xyz)>=3 else np.zeros(3)
        ratio=float(singular[-1]/singular[0]) if singular[0]>1e-12 else 0
        if ratio<spec['min_nonplanarity_ratio']:fail('PLANAR_OR_DEGENERATE','Bundle nonplanarity ratio is below declared budget')
        ca=cameras.get(spec['keyframe_a']);cb=cameras.get(spec['keyframe_b'])
        if ca is not None and cb is not None:
            for name,point in bundles.items():
                if spec['keyframe_a'] not in tracks.get(name,{}) or spec['keyframe_b'] not in tracks.get(name,{}):continue
                a=point-ca[:3,3];b=point-cb[:3,3];den=float(np.linalg.norm(a)*np.linalg.norm(b))
                if den>1e-15:angles.append(math.degrees(math.acos(float(np.clip(np.dot(a,b)/den,-1,1)))))
        median=float(np.median(angles)) if angles else 0
        if median<spec['min_parallax_degrees']:fail('LOW_PARALLAX','Median keyframe triangulation angle is below budget')
    else:missing=1;ratio=0;median=0
    return {'tracking_quality_version':'1.0','passed':not failures,'failures':failures,
            'frames':coverage,'bundle_count':len(bundles),'camera_count':len(cameras),
            'missing_fraction':missing,'native_average_error':float(native_error) if math.isfinite(native_error) else None,
            'reprojection_max_pixels':max(errors) if errors else None,
            'reprojection_rms_pixels':math.sqrt(sum(e*e for e in errors)/len(errors)) if errors else None,
            'minimum_depth':min(depths) if depths else None,'nonplanarity_ratio':ratio,
            'median_parallax_degrees':median,'limits':spec,
            'scope':'Declared fixed square-pixel intrinsics, '+('POLYNOMIAL radial distortion' if calibration else 'zero distortion')+', static nonplanar scene; reconstruction has monocular similarity gauge'}
