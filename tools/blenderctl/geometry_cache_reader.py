"""Guarded source caches and private native-reader copies for isolated workers."""
from contextlib import ExitStack
from pathlib import Path
import bpy
from protocol import Failure,atomic_json,digest

def stage_read_snapshot(manifest, guards, job, candidate, requested_frames):
    """Copy guarded source bytes to a private native-reader cache before eval.

    Blender's Windows cache reader conflicts with the source deny-WRITE lock.
    Original handles stay held; only job-owned copies are exposed to that reader.
    """
    import copy, hashlib
    from geometry_temporal import validate_cache, configure_from_cache, validate_loaded_bindings
    from geometry_identity import implementation_sha256
    from geometry_nodes_workflow_addon import core
    from geometry_cache_layout import plan_layout
    root=Path(manifest['root']).resolve(); destination=(Path(job)/'cr').resolve()
    # Preserve each bake's meta/blob relationship, but discard unnecessary
    # producer path prefixes in this private reader copy. Plan before any IO.
    snapshot,copies=plan_layout(manifest,destination)
    snapshot['candidate']=candidate
    destination.mkdir(exist_ok=False)
    witness={'status':'copying','source_root':str(root),'root':str(destination),'files':[]}
    witness_path=Path(job)/'geometry-cache-read-snapshot.json'
    atomic_json(witness_path,witness)
    for row in copies:
        source=Path(row['source']); output=(destination/row['path']).resolve()
        if not source.is_relative_to(root) or not output.is_relative_to(destination):
            raise Failure('CONFLICT','Cache snapshot path leaves declared root')
        output.parent.mkdir(parents=True,exist_ok=True); checksum=hashlib.sha256(); size=0
        with output.open('xb') as stream:
            for block in guards[str(source)].chunks():
                stream.write(block); checksum.update(block); size+=len(block)
        if checksum.hexdigest()!=row['sha256'] or size!=row['size']:
            raise Failure('CONFLICT','Guarded cache snapshot copy differs')
        witness['files'].append({'path':row['path'],'size':size,'sha256':checksum.hexdigest()})
    for bake in snapshot['bakes']:
        Path(bake['metadata']['global']['bake_directory']).mkdir(parents=True,exist_ok=True)
    witness['layout']='compact_per_bake_v1'
    witness['copy_mapping']=[{'source':row['source'],'path':row['path'],'sha256':row['sha256']} for row in copies]
    # Keep receipts outside the input tree: a one-node capture can bake at '.'.
    snapshot_path=Path(job)/'geometry-cache-read-manifest.json'; atomic_json(snapshot_path,snapshot)
    descriptor={'file':str(snapshot_path),'expected_sha256':digest(snapshot_path)}
    snapshot=validate_cache(descriptor,identity=implementation_sha256())
    token=None; committed=[]; cached_targets={bake['target'] for bake in snapshot['bakes']}
    try:
        for target in snapshot['targets']:
            if target['id'] not in cached_targets:continue
            obj=bpy.data.objects[target['object']]; old=obj.modifiers[target['modifier']]
            token=core.stage_fresh_modifier(obj,old,old.node_group)
            mapping={target['id']:target}
            configure_from_cache(snapshot,mapping,before_first_evaluation=True)
            validate_loaded_bindings(snapshot,mapping)
            core.commit_fresh_modifier(token); committed.append(target['id']); token=None
        validate_loaded_bindings(snapshot)
    except Exception:
        if token and token.get('status')!='committed': core.rollback_fresh_modifier(token)
        if committed:
            # This isolated reader worker never saves. Restore the guarded disk
            # source if a later target fails after an earlier in-memory commit.
            bpy.ops.wm.open_mainfile(filepath=candidate['file'],load_ui=False,use_scripts=False)
        raise
    from geometry_temporal import _samples
    from geometry_verifier import near
    samples=snapshot['samples']
    nonempty=[row for row in samples if any(t['evaluation'].get('evaluated',{}).get('bounds') is not None
              or t['evaluation'].get('instances',{}).get('count',0)>0 for t in row['targets'])]
    covered=[row for row in samples if row['frame'] in requested_frames]
    choices=nonempty or covered
    if not choices:raise Failure('VALIDATION_FAILED','Cache receipt has no nonempty or requested-frame witness')
    reference=choices[len(choices)//2]; actual=_samples(reference['frame'],snapshot['targets'],snapshot.get('observations',[]))
    mismatch=near(reference,actual)
    witness['frame_checks']=[{'frame':reference['frame'],'phase':'preflight','status':'failed' if mismatch else 'passed','mismatch':mismatch}]
    atomic_json(witness_path,witness)
    if mismatch:raise Failure('VALIDATION_FAILED','Private cache reader witness differs: '+mismatch)
    witness.update(status='configured_before_first_evaluation',manifest=descriptor,
                   bytes=sum(row['size'] for row in witness['files']),
                   protection='Original deny-WRITE/DELETE FileGuards remain held; private reader copies rehashed after reading')
    atomic_json(witness_path,witness)
    return snapshot


def prepare_read_snapshot(manifest,job,candidate,requested_frames):
    from filesystem import FileGuard
    guards={};root=Path(manifest['root']).resolve()
    with ExitStack() as stack:
        for row in manifest['files']:
            path=(root/row['path']).resolve()
            if not path.is_relative_to(root):raise Failure('CONFLICT','Cache reader path leaves source root')
            guard=stack.enter_context(FileGuard(path))
            if guard.sha256()!=row['sha256']:raise Failure('CONFLICT','Cache reader source changed')
            guards[str(path)]=guard
        return stage_read_snapshot(manifest,guards,job,candidate,requested_frames)
