# SPDX-License-Identifier: GPL-3.0-or-later
"""Read-only native surface analysis with durable per-observation evidence."""
import math,time
from contextlib import ExitStack
import bpy,numpy as np
import adaptive_sampling,mesh_clip,nodes
from protocol import Failure,atomic_json,digest


def run(params,spec,job):
    started=time.monotonic();settings=spec['sampling'];profile=params['driver_profile']
    cache={};metadata=[];first=None;count=0;result=None
    progress=job/'exchange-analysis-progress.json'
    def checkpoint(phase,**extra):
        atomic_json(progress,{'phase':phase,'source':params['file'],'source_sha256':params['expected_sha256'],
                    'settings':spec,'sample_count':len(cache),'vertex_samples':count*len(cache),'samples':metadata,**extra})
    with ExitStack() as stack:
        hashes=nodes.resource_guards(params,stack)
        bpy.ops.wm.open_mainfile(filepath=params['file'],load_ui=False,use_scripts=False)
        if tuple(bpy.data.version[:2])!=tuple(bpy.app.version[:2]):raise Failure('UNSUPPORTED','Analysis requires matching Blender major/minor')
        scene=mesh_clip.prepare_source(params,spec,job)
        if scene.unit_settings.scale_length!=1 or scene.render.fps_base!=1:raise Failure('UNSUPPORTED','Analysis requires meter scale one and integer fps')
        fps=scene.render.fps
        def sample(t):
            nonlocal first,count
            if t in cache:return cache[t]
            if len(cache)>=settings['max_evaluations']:raise adaptive_sampling.BudgetReached('max_evaluations')
            if count and count*(len(cache)+1)>settings['max_vertex_samples']:raise adaptive_sampling.BudgetReached('max_vertex_samples')
            rows=mesh_clip.capture(spec['objects'],t,profile)
            actual=float(bpy.context.scene.frame_current_final)
            if first is None:
                first=rows;count=sum(len(r['vertices']) for r in rows)
                if count>settings['max_vertex_samples']:raise adaptive_sampling.BudgetReached('max_vertex_samples')
                for i,row in enumerate(first):
                    topology=job/('analysis-topology-%02d.npz'%i)
                    np.savez(topology,triangles=row['triangle_indices'],vertices=row['vertices'])
            elif any(not mesh_clip.topology_match(a,b) for a,b in zip(first,rows)):
                checkpoint('rejected',reason='topology_or_uv_changed',requested_frame=t,actual_frame=actual)
                raise Failure('UNSUPPORTED','Adaptive source topology/UV changed; see analysis progress')
            vertices=np.concatenate([r['vertices'] for r in rows]);path=job/('analysis-sample-%04d.npy'%len(metadata))
            with open(path,'xb') as out:np.save(out,vertices,allow_pickle=False)
            cache[t]=(vertices,actual)
            metadata.append({'requested_frame':t,'actual_frame':actual,'file':str(path),'sha256':digest(path),'bytes':path.stat().st_size})
            checkpoint('capture')
            return cache[t]
        try:
            result=adaptive_sampling.analyze(spec['frame_start'],spec['frame_end'],settings,sample)
            atomic_json(job/'exchange-analysis-intervals.json',result)
            # The request budget counts distinct source observations. Reopen/replay
            # is a second bounded pass over the same observations, not extra times.
            checkpoint('source_reopen_reverse')
            bpy.ops.wm.open_mainfile(filepath=params['file'],load_ui=False,use_scripts=False);mesh_clip.prepare_source(params,spec,job)
            replay=[];worst=0.
            for t in reversed(list(cache)):
                rows=mesh_clip.capture(spec['objects'],t,profile)
                if any(not mesh_clip.topology_match(a,b) for a,b in zip(first,rows)):raise Failure('VALIDATION_FAILED','Analysis source replay topology/UV changed')
                actual=float(bpy.context.scene.frame_current_final);error=mesh_clip.maximum(np.concatenate([r['vertices'] for r in rows]),cache[t][0]);worst=max(worst,error)
                replay.append({'requested_frame':t,'actual_frame':actual,'error':error})
                if actual!=cache[t][1] or error>min(settings['max_position_error'],1e-6):
                    atomic_json(job/'exchange-analysis-replay.json',replay)
                    raise Failure('VALIDATION_FAILED','Analysis source not repeatable after independent reopen')
            atomic_json(job/'exchange-analysis-replay.json',replay)
            report={'exchange_analysis_version':'1.0','operation':'analyze','settings':spec,**result,
                    'source':params['file'],'source_sha256':params['expected_sha256'],'source_saved':False,
                    'source_fps':fps,'sample_count':len(cache),'vertex_samples':count*len(cache),'source_replay_max_error':worst,
                    'source_replay':'pass' if cache else 'not_run','resource_hashes':hashes,'samples':metadata,
                    'meshes':[{'source':r['name'],'vertices':len(r['vertices'])} for r in first or []],
                    'seconds':time.monotonic()-started,
                    'scope':'Source-only quarter/midpoint witnesses. Pass applies only to sampled intervals; no export, continuous-time, clothing-fit or render-quality certificate. Budget counts unique source observations; replay evaluates the same observations once more.'}
            atomic_json(job/'exchange-analysis-report.json',report);checkpoint('complete',outcome=result['outcome'],stop_reason=result['stop_reason'])
            return report
        except Exception as exc:
            # Captures already on disk remain usable evidence after any failure.
            checkpoint('failed',error_type=type(exc).__name__,error=str(exc))
            raise
