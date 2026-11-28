"""Portable review of the frozen add-on's explicit three-Action cue transaction."""
from material_draft_handoff import require
from protocol import Failure


def work_cue(phase, bpy, context, store, ui, request, state):
    from animation_event_store import dependencies, describe, planned_keys
    from animation_curves_workflow_addon import cue_binding, cue_transaction

    operation = request['operation']
    paths = dependencies(bpy)

    def saved():
        cue = ui.validate_saved(context)
        event, _ = store.inspect(context)
        return {**state, 'record': {'cue': cue, 'event': describe(event)}, 'dependencies': paths}

    if phase == 'verify' or operation == 'cue-inspect':
        return saved()
    try:
        decision = cue_binding.decide(**request['parameters'])
    except (KeyError, TypeError, ValueError, ArithmeticError) as error:
        raise Failure('INVALID_REQUEST', str(error)) from error
    event, _ = store.inspect(context)
    try:
        native = cue_transaction.bound_preview(store, context, decision, record_id=event['id'], revision=event['revision'])
    except ValueError as error:
        raise Failure('CONFLICT', str(error)) from error
    delta = decision['target_frame'] - event['rows'][0]['frame']
    plan = {'schema':'animation.event-plan.v1', 'edit':'cue', 'store':'cross',
            'source':{'file':request['file'],'sha256':request['expected_sha256']}, 'context':request['context'],
            'runtime':state['runtime'], 'timing':state['timing'], 'parameters':request['parameters'],
            'decision':decision, 'previous':describe(event), 'record_sha256':native['bound']['raw_sha256'],
            'participants':planned_keys(event['rows'], delta)}
    if operation == 'cue-plan':
        return {**state, 'plan':plan}
    require(plan == request['reviewed_plan'], 'CONFLICT', 'The reviewed cue or its participants changed')
    cue_transaction.execute(store, context, native)
    result = saved()
    require(result['record']['event']['revision'] == event['revision'] + 1, 'VALIDATION_FAILED',
            'Native cue commit did not advance the event revision')
    require(bpy.ops.wm.save_as_mainfile(filepath=request['stage'], copy=True, relative_remap=True,
                                       check_existing=False) == {'FINISHED'}, 'IO_ERROR', 'Could not save cue candidate')
    return result
