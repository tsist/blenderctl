"""Discoverable disk-state and orchestration boundaries, not GUI-state inference."""
STATE={'state_source':'SAVED_DISK_V1','gui_session':'NOT_CONNECTED','unsaved_gui_edits':'NOT_OBSERVED',
       'gui_state_detection':False,'gui_autosave':False,'live_session_input':False,
       'unsaved_gui_edits_meaning':'NOT_OBSERVABLE_FROM_BACKGROUND; NOT_OBSERVED does not mean checked-and-clean'}

def disk_state():return dict(STATE)

EXCLUSIONS={
    'diagnostic_preflight':('doctor','capabilities','render.devices','exchange.formats','extension.inspect','doctor.verify_install','workflow.preflight'),
    'explicit_transaction':('asset.plan','project.plan_copy','project.plan_batch','project.plan_files','transaction.apply','transaction.recover','transaction.rollback','transaction.status'),
    'host_orchestrator':('pipeline.plan','pipeline.run','pipeline.resume','asset.preview.batch','workflow.run','workflow.resume'),
    'host_media_work_unit':('compositing.encode',),
    'host_depth_work_unit':('compositing.depth-prepare','compositing.depth-inspect'),
    'host_depth_estimation':('compositing.depth-estimate',),
    'host_depth_relight':('compositing.depth-relight',),
    'host_depth_session':('compositing.depth-session',),
    'host_depth_sequence':('compositing.depth-sequence',),
    'host_resource_management':('resource.init','resource.status','cache.inspect','cache.plan_clean','project.package'),
    'standalone_workflow':('toon.run','toon.inspect','toon.template-save','toon.template-edit','toon.template-apply','modeling.run','material.run','material.template-save','material.batch','material.study','geometry.run','geometry.inspect','geometry.session','lighting.run','lighting.inspect','lighting.template-save','lighting.template-edit','lighting.batch','compositing.run','compositing.inspect','compositing.template-save','compositing.template-run','compositing.batch','compositing.study'),
    'review_checkpoint':('extension.review','extension.approve','sculpt.review'),
    'development_fixture':('probe',),
}
EXCLUSIONS['standalone_workflow'] += ('material.procedural-run','material.procedural-export')
EXCLUSIONS['standalone_workflow'] += ('material.projection-bake',)
EXCLUSIONS['standalone_workflow'] += ('rigging-workflow.run','rigging-workflow.session')
EXCLUSIONS['standalone_workflow'] += ('animation.inspect','animation.run','animation.clip-apply')
EXCLUSIONS['standalone_workflow'] += ('toon-image.run','toon-image.inspect')
EXCLUSIONS['standalone_workflow'] += ('uv.run','uv.inspect','uv.grid-transfer')
EXCLUSIONS['host_orchestrator'] += ('uv.batch','uv.batch-resume')
REASONS={
    'diagnostic_preflight':'Run standalone to discover the host before scheduling work; not an artifact producer.',
    'explicit_transaction':'Publication and rollback remain explicit host transactions, outside a replayable DAG.',
    'host_orchestrator':'Do not nest supervisors, batching or resume inside a worker DAG.',
    'host_media_work_unit':'One Host-only owned codec work unit; named reference and immutable inputs; not a Blender worker DAG or nested public supervisor.',
    'host_depth_work_unit':'Experimental Host-only preparation or cold reading of declared existing depth; not automatic RGB estimation, a Blender worker DAG, or relight consumer certification.',
    'host_depth_relight':'Experimental single-batch supplemental-light consumer of succeeded depth bundles; owned numeric work, not a DAG, live session or model producer.',
    'host_depth_session':'Experimental bounded owned resident CUDA preview session of succeeded depth bundles; separate host lifecycle and revision transport, outside replayable DAG. Running guarded preview is not final parent success.',
    'host_depth_sequence':'Experimental finite shot work unit with ordered frame identities and frozen shot statistics; complete parent and immutable member validation are required. Outside replayable DAG.',
    'host_depth_estimation':'Experimental fixed local DAV2 Large CUDA0 FP32 inference for declared new RGB; separate owned Host producer, not a replayable DAG or geometric/lighting consumer certification.',
    'host_resource_management':'Host resource/cache/package operations have separate lifecycle and retention semantics.',
    'standalone_workflow':'Runs its own bounded stages in one worker; not yet a replayable DAG entry and never a nested supervisor.',
    'review_checkpoint':'Inspect the review package and make the required trust or artistic decision outside automatic replay.',
    'development_fixture':'Development fixture generation is not a production pipeline operation.',
}

def capabilities():
    from protocol import COMMANDS
    from pipeline_contract import ALLOWED
    classifications = {command: kind for kind, commands in EXCLUSIONS.items() for command in commands}
    exclusions = []
    for command in COMMANDS:
        if command in ALLOWED:
            continue
        kind = classifications.get(command, 'awaiting_dag_qualification')
        reason = REASONS.get(kind, 'Standalone command requires separate bounded DAG evidence.')
        exclusions.append({'command': command, 'classification': kind, 'reason': reason})
    return {'version':'1.0',**disk_state(),
            'snapshot_identity':'Absolute file path and observed/declared SHA; hashes do not certify the current GUI state.',
            'recovery':'Save a separate snapshot from the authoring application, then submit its absolute path and SHA. Reopen successful job candidates; never merge into a live unsaved session automatically.',
            'dependency_policy':'Unknown or dynamic dependencies are not inferred closed; use the explicit domain/typed-closure contract.',
            'pipeline_commands':list(ALLOWED),'pipeline_exclusions':exclusions}
