# SPDX-License-Identifier: GPL-3.0-or-later
"""Discoverable disk-state and orchestration boundaries, not GUI-state inference."""
STATE={'state_source':'SAVED_DISK_V1','gui_session':'NOT_CONNECTED','unsaved_gui_edits':'NOT_OBSERVED',
       'gui_state_detection':False,'gui_autosave':False,'live_session_input':False,
       'unsaved_gui_edits_meaning':'NOT_OBSERVABLE_FROM_BACKGROUND; NOT_OBSERVED does not mean checked-and-clean'}

def disk_state():return dict(STATE)

EXCLUSIONS={
    'diagnostic_preflight':('doctor','capabilities','render.devices','exchange.formats','extension.inspect','doctor.verify_install'),
    'explicit_transaction':('asset.plan','project.plan_copy','project.plan_batch','project.plan_files','transaction.apply','transaction.recover','transaction.rollback','transaction.status'),
    'host_orchestrator':('pipeline.plan','pipeline.run','pipeline.resume','asset.preview.batch'),
    'host_resource_management':('resource.init','resource.status','cache.inspect','cache.plan_clean','project.package'),
    'standalone_workflow':('material.run','material.template-save','material.batch','material.study'),
    'review_checkpoint':('extension.review','extension.approve','sculpt.review'),
    'development_fixture':('probe',),
}
REASONS={
    'diagnostic_preflight':'Run standalone to discover the host before scheduling work; not an artifact producer.',
    'explicit_transaction':'Publication and rollback remain explicit host transactions, outside a replayable DAG.',
    'host_orchestrator':'Do not nest supervisors, batching or resume inside a worker DAG.',
    'host_resource_management':'Host resource/cache/package operations have separate lifecycle and retention semantics.',
    'standalone_workflow':'Runs its own bounded stages in one worker; not yet a replayable DAG entry and never a nested supervisor.',
    'review_checkpoint':'Inspect the review package and make the required trust or artistic decision outside automatic replay.',
    'development_fixture':'Development fixture generation is not a production pipeline operation.',
}

def capabilities():
    from protocol import COMMANDS
    from pipeline_contract import ALLOWED
    exclusions=[{'command':command,'classification':kind,'reason':REASONS[kind]} for kind,commands in EXCLUSIONS.items() for command in commands]
    omitted=[row['command'] for row in exclusions]
    if len(omitted)!=len(set(omitted)) or set(omitted)&set(ALLOWED) or set(COMMANDS)!=set(ALLOWED)|set(omitted):
        raise RuntimeError('Pipeline route dispositions must classify every request command exactly once')
    return {'version':'1.0',**disk_state(),
            'snapshot_identity':'Absolute file path and observed/declared SHA; hashes do not certify the current GUI state.',
            'recovery':'Save a separate snapshot from the authoring application, then submit its absolute path and SHA. Reopen successful job candidates; never merge into a live unsaved session automatically.',
            'dependency_policy':'Unknown or dynamic dependencies are not inferred closed; use the explicit domain/typed-closure contract.',
            'pipeline_commands':list(ALLOWED),'pipeline_exclusions':exclusions}
