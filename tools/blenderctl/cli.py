"""Project entry point: python tools/blenderctl/cli.py --help."""
import argparse
import json
from pathlib import Path
import sys

from protocol import (CODES, COMMANDS, DEFAULT_BLENDER, DEFAULT_JOBS, DEFAULT_TRANSACTIONS, SCHEMA,
                      VERSION, Failure, envelope, read_json, validate)
import runner


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Failure("INVALID_REQUEST", message)


def parser():
    p = Parser(description="blenderctl: isolated Blender CLI and recoverable file transactions")
    p.add_argument("--version", action="version", version=VERSION)
    p.add_argument("--blender", type=Path, default=DEFAULT_BLENDER)
    p.add_argument("--jobs-dir", type=Path, default=DEFAULT_JOBS)
    p.add_argument("--transactions-dir", type=Path, default=DEFAULT_TRANSACTIONS)
    p.add_argument("--timeout", type=float, default=120)
    p.add_argument("--async", dest="background", action="store_true", help="submit a detached job")
    p.add_argument("--human", action="store_true", help="brief human-readable output; default is JSON")
    p.add_argument("--compact", action="store_true", help="bounded evidence summary with hashed full reports; disk results stay complete")
    p.add_argument("--full", action="store_true", help="read full workflow envelope instead of its default evidence summary")
    sub = p.add_subparsers(dest="command", required=True, parser_class=Parser)
    doctor=sub.add_parser("doctor", help="inspect isolated worker environment")
    da=doctor.add_subparsers(dest='doctor_action',parser_class=Parser)
    da.add_parser('verify-install').add_argument('directory')
    for group,actions in [('resource',('init','status')),('cache',('inspect','plan-clean'))]:
        act=sub.add_parser(group).add_subparsers(dest='action',required=True,parser_class=Parser)
        for action in actions:
            a=act.add_parser(action)
            if group=='resource':a.add_argument('domain')
            if group=='cache' or action=='init':a.add_argument('--manifest',type=Path,required=True)
    sub.add_parser("capabilities", help="runtime registrations; existence is not functional support")
    pipes=sub.add_parser('pipeline',help='bounded candidate DAGs, isolated failures and explicit resume').add_subparsers(dest='action',required=True,parser_class=Parser)
    for action in ('plan','run'):
        a=pipes.add_parser(action);a.add_argument('--manifest',type=Path,required=True)
        if action=='run':a.add_argument('--reuse-job',help='prior pipeline in the same jobs directory; verify before reuse')
    pipes.add_parser('resume').add_argument('job_id')
    work=sub.add_parser('workflow',help='full-scale preflight and explicit sequential work units').add_subparsers(dest='action',required=True,parser_class=Parser)
    work.add_parser('preflight').add_argument('--requests',type=Path,required=True)
    work.add_parser('run').add_argument('--manifest',type=Path,required=True)
    work.add_parser('resume').add_argument('job_id')
    for group,actions in [('exchange',('formats','export','import','convert','analyze')),('extension',('inspect','prepare','run','review','approve')),('sculpt',('review','replay'))]:
        act=sub.add_parser(group).add_subparsers(dest='action',required=True,parser_class=Parser)
        for action in actions:
            a=act.add_parser(action)
            if action in ('formats','inspect'):continue
            a.add_argument('--manifest',type=Path,required=True)
            if group=='extension' and action=='run':a.add_argument('--review-receipt',type=Path,help='descriptor of a locally confirmed exact-request review receipt')
            if action in ('export','import','analyze'):a.add_argument('file');a.add_argument('--expected-sha256',required=True)
            if action in ('export','run','analyze'):a.add_argument('--resources',type=Path)
            if group=='exchange' and action in ('export','analyze'):a.add_argument('--driver-profile',type=Path,help='explicit source-bound native simple-driver profile')
            if group=='exchange' and action in ('export','analyze'):a.add_argument('--simulation-receipt',type=Path,help='hashed RIG_CLOTH_V1 cache receipt for native physics source')
    for group,actions in [('render',('devices','run')),('media',('prepare','export'))]:
        act=sub.add_parser(group).add_subparsers(dest='action',required=True,parser_class=Parser)
        for action in actions:
            a=act.add_parser(action)
            if action=='devices':continue
            a.add_argument('--manifest',type=Path,required=True)
            if action!='prepare':
                a.add_argument('file');a.add_argument('--expected-sha256',required=True);a.add_argument('--resources',type=Path)
            if action=='run':a.add_argument('--simulation-receipt',type=Path,help='JSON file descriptor containing file and expected_sha256')
            if group=='render' and action=='run':a.add_argument('--driver-profile',type=Path,help='explicit source-bound native simple-driver profile')
    tracking=sub.add_parser('tracking',help='bounded native clip tracking and camera reconstruction')
    tracking_actions=tracking.add_subparsers(dest='action',required=True,parser_class=Parser)
    for action in ('prepare','inspect','solve'):
        a=tracking_actions.add_parser(action)
        if action!='inspect':a.add_argument('--manifest',type=Path,required=True)
        if action!='prepare':
            a.add_argument('file');a.add_argument('--expected-sha256',required=True);a.add_argument('--resources',type=Path,required=True)
        if action=='inspect':a.add_argument('--clip',required=True)
    scene=sub.add_parser('scene',help='explicit scene construction/edit candidates and read-only scene reports')
    scene_actions=scene.add_subparsers(dest='action',required=True,parser_class=Parser)
    build=scene_actions.add_parser('prepare',help='build from empty or edit a hashed input; writes only a new job candidate')
    build.add_argument('--manifest',type=Path,required=True)
    build.add_argument('--file',help='optional existing blend; requires --expected-sha256')
    build.add_argument('--expected-sha256')
    scene_inspect=scene_actions.add_parser('inspect',help='read scene/view-layer/instance state; never saves')
    scene_inspect.add_argument('file');scene_inspect.add_argument('--expected-sha256');scene_inspect.add_argument('--context',type=Path,help='explicit read-only scene/view-layer/frame/selection context JSON')
    model=sub.add_parser('model',help='bounded mesh/modifier/UV candidates and geometry diagnostics')
    model_actions=model.add_subparsers(dest='action',required=True,parser_class=Parser)
    model_build=model_actions.add_parser('prepare',help='model in a new candidate; existing input requires SHA256')
    model_build.add_argument('--manifest',type=Path,required=True);model_build.add_argument('--file');model_build.add_argument('--expected-sha256')
    model_inspect=model_actions.add_parser('inspect',help='inspect base/evaluated geometry and UV; never saves')
    model_inspect.add_argument('file');model_inspect.add_argument('--expected-sha256');model_inspect.add_argument('--context',type=Path)
    node=sub.add_parser('node',help='material/shader/compositor/geometry node candidates and reports')
    node_actions=node.add_subparsers(dest='action',required=True,parser_class=Parser)
    node_build=node_actions.add_parser('prepare');node_build.add_argument('--manifest',type=Path,required=True);node_build.add_argument('--file');node_build.add_argument('--expected-sha256');node_build.add_argument('--resources',type=Path)
    node_inspect=node_actions.add_parser('inspect');node_inspect.add_argument('file');node_inspect.add_argument('--expected-sha256');node_inspect.add_argument('--context',type=Path)
    rig=sub.add_parser('rig',help='local skeleton, skin, pose and animation candidates')
    rig_actions=rig.add_subparsers(dest='action',required=True,parser_class=Parser)
    rig_build=rig_actions.add_parser('prepare');rig_build.add_argument('--manifest',type=Path,required=True);rig_build.add_argument('--file');rig_build.add_argument('--expected-sha256')
    rig_build.add_argument('--driver-profile',type=Path,help='source-bound native driver profile for explicit S08 adapters')
    rig_inspect=rig_actions.add_parser('inspect');rig_inspect.add_argument('file');rig_inspect.add_argument('--expected-sha256');rig_inspect.add_argument('--context',type=Path)
    rig_inspect.add_argument('--suggest-mapping',type=Path,help='JSON source/target armatures; suggestions are never applied')
    rig_package=rig_actions.add_parser('package',help='whole native rig project, byte-identical candidate and evaluated reopen witnesses')
    rig_package.add_argument('file');rig_package.add_argument('--expected-sha256',required=True);rig_package.add_argument('--manifest',type=Path,required=True);rig_package.add_argument('--driver-profile',type=Path,required=True);rig_package.add_argument('--simulation-receipt',type=Path)
    simulation=sub.add_parser('simulation',help='bounded physics/special-data candidates and verified cache receipts')
    simulation_actions=simulation.add_subparsers(dest='action',required=True,parser_class=Parser)
    simulation_build=simulation_actions.add_parser('prepare');simulation_build.add_argument('--manifest',type=Path,required=True);simulation_build.add_argument('--file');simulation_build.add_argument('--expected-sha256')
    simulation_build.add_argument('--driver-profile',type=Path,help='source-bound drivers for a pure hair manifest')
    simulation_inspect=simulation_actions.add_parser('inspect');simulation_inspect.add_argument('file');simulation_inspect.add_argument('--expected-sha256');simulation_inspect.add_argument('--context',type=Path)
    simulation_inspect.add_argument('--hair-frames',type=int,nargs='+',help='1..32 unique frames for managed hair samples');simulation_inspect.add_argument('--driver-profile',type=Path,help='source-bound drivers; requires --hair-frames')
    simulation_bake=simulation_actions.add_parser('bake');simulation_bake.add_argument('file');simulation_bake.add_argument('--expected-sha256',required=True);simulation_bake.add_argument('--manifest',type=Path,required=True)
    simulation_bake.add_argument('--driver-profile',type=Path,help='source-bound native drivers for RIG_CLOTH_V1 or HAIR_CLOTH_V1')
    simulation_bake.add_argument('--resources',type=Path,help='hashed image dependencies for GEOMETRY_ZONE_V1')
    animation=sub.add_parser('animation',help='read-only evaluated pose and mesh samples')
    sample=animation.add_subparsers(dest='action',required=True,parser_class=Parser).add_parser('sample')
    sample.add_argument('file');sample.add_argument('--manifest',type=Path,required=True);sample.add_argument('--expected-sha256')
    sample.add_argument('--driver-profile',type=Path,help='explicit source-bound native simple-driver profile')
    video=sub.add_parser('video',help='protected saved-file native VSE editing; development candidate')
    va=video.add_subparsers(dest='action',required=True,parser_class=Parser)
    vd=va.add_parser('describe',help='cheap video contract discovery; no Blender or job')
    vd.add_argument('--operation',choices=['run','inspect','manifest','output','clip'])
    vd.add_argument('--schema',action='store_true');vd.add_argument('--section')
    vr=va.add_parser('run');vr.add_argument('file');vr.add_argument('--expected-sha256',required=True)
    vr.add_argument('--manifest',type=Path,required=True);vr.add_argument('--resources',type=Path,required=True)
    vr.add_argument('--output',type=Path);vr.add_argument('--resume',type=Path);vr.add_argument('--resume-sha256')
    vi=va.add_parser('inspect');vi.add_argument('file');vi.add_argument('--expected-sha256',required=True)
    vi.add_argument('--resources',type=Path,required=True);vi.add_argument('--scene',required=True);vi.add_argument('--workflow-id')
    compositing=sub.add_parser('compositing',help='protected native compositing graphs and observed saved-file outputs')
    ca=compositing.add_subparsers(dest='action',required=True,parser_class=Parser)
    cd=ca.add_parser('describe',help='cheap compositing contract discovery; no Blender or job')
    cd.add_argument('--operation',choices=['run','inspect','manifest','preview','encode','template-save','template-run','template-document','batch','study']);cd.add_argument('--schema',action='store_true');cd.add_argument('--section',help='legacy manifest section (encode uses its profile fields); preview is --operation preview --schema')
    cd.add_argument('--param',help='exact top-level field of --operation schema; mutually exclusive with --schema/--section; not a request validator')
    ce=ca.add_parser('encode',help='Host-only named-reference HDR10 codec work unit; no viewing/certification claim');ce.add_argument('--manifest',type=Path,required=True)
    for action in ('template-save','template-run','batch','study'):
        action_parser=ca.add_parser(action)
        action_parser.add_argument('--params',type=Path,required=True,help='complete UTF-8 params object')
    cr=ca.add_parser('run');cr.add_argument('file');cr.add_argument('--expected-sha256',required=True)
    cr.add_argument('--manifest',type=Path,required=True);cr.add_argument('--resources',type=Path,required=True);cr.add_argument('--preview',type=Path)
    cr.add_argument('--resume',type=Path);cr.add_argument('--resume-sha256')
    ci=ca.add_parser('inspect');ci.add_argument('file');ci.add_argument('--expected-sha256',required=True)
    ci.add_argument('--resources',type=Path,required=True);ci.add_argument('--scene',required=True);ci.add_argument('--workflow-id',required=True)
    lighting=sub.add_parser('lighting',help='protected saved-file lighting plans, previews and restore')
    la=lighting.add_subparsers(dest='action',required=True,parser_class=Parser)
    ld=la.add_parser('describe',help='cheap lighting domain contract discovery')
    ld.add_argument('--operation',choices=['run','inspect','manifest','template','target','template-save','template-edit','batch']);ld.add_argument('--schema',action='store_true');ld.add_argument('--section')
    lr=la.add_parser('run');lr.add_argument('file');lr.add_argument('--expected-sha256',required=True)
    lm=lr.add_mutually_exclusive_group(required=True)
    lm.add_argument('--manifest',type=Path);lm.add_argument('--template',type=Path)
    lr.add_argument('--template-sha256');lr.add_argument('--target',type=Path)
    lr.add_argument('--resources',type=Path,required=True);lr.add_argument('--preview',type=Path)
    lr.add_argument('--resume',type=Path);lr.add_argument('--resume-sha256')
    li=la.add_parser('inspect');li.add_argument('file');li.add_argument('--expected-sha256',required=True)
    li.add_argument('--resources',type=Path,required=True);li.add_argument('--scene',required=True);li.add_argument('--rig-id')
    ls=la.add_parser('template-save',help='export actual saved rig as a new versioned template')
    ls.add_argument('file');ls.add_argument('--expected-sha256',required=True);ls.add_argument('--resources',type=Path,required=True)
    ls.add_argument('--scene',required=True);ls.add_argument('--rig-id',required=True);ls.add_argument('--template-id',required=True);ls.add_argument('--revision',type=int,required=True)
    le=la.add_parser('template-edit',help='create a new template revision; preserve original bytes')
    le.add_argument('--template',type=Path,required=True);le.add_argument('--template-sha256',required=True)
    le.add_argument('--revision',type=int,required=True);le.add_argument('--edits',type=Path,required=True)
    lb=la.add_parser('batch',help='bounded sequential independent candidates and partial recovery')
    lb.add_argument('--manifest',type=Path,required=True);lb.add_argument('--resume',type=Path);lb.add_argument('--resume-sha256')
    geometry=sub.add_parser('geometry',help='managed native geometry graphs, observed output and live-session handoff')
    geometry_actions=geometry.add_subparsers(dest='action',required=True,parser_class=Parser)
    gd=geometry_actions.add_parser('describe',help='read-only geometry contract index; no Blender or job')
    gd.add_argument('--operation',choices=['run','inspect','manifest','report']);gd.add_argument('--schema',action='store_true')
    gd.add_argument('--section',choices=['context','targets','preview','observations'])
    gr=geometry_actions.add_parser('run',help='one protected project candidate, independent target outcomes and reopen')
    gr.add_argument('--manifest',type=Path,required=True);gr.add_argument('--file');gr.add_argument('--expected-sha256')
    gr.add_argument('--resources',type=Path,required=True);gr.add_argument('--resume',type=Path);gr.add_argument('--resume-sha256')
    gi=geometry_actions.add_parser('inspect',help='read-only actual graph, instances, geometry and optional field observations')
    gi.add_argument('file');gi.add_argument('--expected-sha256',required=True);gi.add_argument('--resources',type=Path,required=True)
    gi.add_argument('--context',type=Path);gi.add_argument('--targets',type=Path);gi.add_argument('--observations',type=Path);gi.add_argument('--discover',action='store_true')
    gs=geometry_actions.add_parser('session',help='bounded request to an explicitly owned Blender session queue')
    gs.add_argument('--session-dir',required=True);gs.add_argument('--session-action',choices=['capture','status','apply'],default='status')
    gs.add_argument('--payload',type=Path);gs.add_argument('--request-id');gs.add_argument('--wait-seconds',type=float,default=0)
    material=sub.add_parser('material',help='isolated material output')
    material_actions=material.add_subparsers(dest='action',required=True,parser_class=Parser)
    describe=material_actions.add_parser('describe',help='read-only material capability/schema index; no Blender or job')
    describe.add_argument('--operation',choices=['run','batch','study','template-save','preview'])
    describe.add_argument('--schema',action='store_true',help='return the small selected request schema')
    describe.add_argument('--section',choices=['manifest','preview','layers','target','context','study'],help='read only a selected manifest or study section')
    workflow=material_actions.add_parser('run',help='run a standalone material workflow in one isolated worker')
    workflow.add_argument('file');workflow.add_argument('--expected-sha256',required=True);workflow.add_argument('--resources',type=Path,required=True)
    workflow_input=workflow.add_mutually_exclusive_group(required=True)
    workflow_input.add_argument('--manifest',type=Path);workflow_input.add_argument('--template',type=Path)
    workflow.add_argument('--template-sha256');workflow.add_argument('--bindings',type=Path);workflow.add_argument('--bindings-sha256')
    template_save=material_actions.add_parser('template-save',help='export a managed material as a portable template')
    template_save.add_argument('file');template_save.add_argument('--expected-sha256',required=True)
    for flag in ('material-id','template-id','name','version'):template_save.add_argument('--'+flag,required=True)
    template_save.add_argument('--resources',type=Path,required=True)
    batch_material=material_actions.add_parser('batch',help='run isolated model-wide material assignments and previews')
    batch_material.add_argument('file');batch_material.add_argument('--expected-sha256',required=True)
    batch_material.add_argument('--manifest',type=Path,required=True);batch_material.add_argument('--resources',type=Path,required=True)
    batch_material.add_argument('--resume',type=Path);batch_material.add_argument('--resume-sha256')
    study_material=material_actions.add_parser('study',help='bounded material candidates, texture warnings and progress in one owned worker')
    study_material.add_argument('file');study_material.add_argument('--expected-sha256',required=True)
    for flag in ('manifest','resources','study'):study_material.add_argument('--'+flag,type=Path,required=True)
    study_material.add_argument('--resume',type=Path);study_material.add_argument('--resume-sha256')
    preview=material_actions.add_parser('preview')
    preview.add_argument('file');preview.add_argument('--expected-sha256',required=True);preview.add_argument('--material',required=True);preview.add_argument('--resolution',type=int,default=256);preview.add_argument('--samples',type=int,default=16);preview.add_argument('--frame',type=int,default=1);preview.add_argument('--resources',type=Path)
    texture=sub.add_parser('texture',help='bounded CPU image baking')
    bake=texture.add_subparsers(dest='action',required=True,parser_class=Parser).add_parser('bake')
    bake.add_argument('file');bake.add_argument('--expected-sha256',required=True);bake.add_argument('--object',required=True);bake.add_argument('--scene',required=True);bake.add_argument('--view-layer',required=True);bake.add_argument('--uv-layer',required=True);bake.add_argument('--type',choices=['EMIT','DIFFUSE_COLOR','NORMAL'],required=True);bake.add_argument('--resolution',type=int,default=256);bake.add_argument('--samples',type=int,default=16);bake.add_argument('--margin',type=int,default=8);bake.add_argument('--frame',type=int,default=1);bake.add_argument('--resources',type=Path)
    inspect = sub.add_parser("inspect", help="read .blend data and typed external references; never save")
    inspect.add_argument("file")
    inspect.add_argument('--expected-sha256',help='verify the saved disk snapshot before opening')
    query = sub.add_parser("query", help="detailed snapshot or exact selector; never saves the source")
    query.add_argument("file")
    query.add_argument("--selector", type=Path, help="JSON file: type/name/library or asset_id")
    query.add_argument("--expected-sha256")
    diff = sub.add_parser("diff", help="compare scoped content, IDs and references of two blend files")
    diff.add_argument("file")
    diff.add_argument("other")
    diff.add_argument("--expected-sha256")
    diff.add_argument("--other-sha256")
    dep = sub.add_parser("dependency", help="typed resource audits and same-content relink candidates")
    dep_actions = dep.add_subparsers(dest="action", required=True, parser_class=Parser)
    audit = dep_actions.add_parser("audit")
    audit.add_argument("file")
    audit.add_argument("--expected-sha256")
    for action in ('plan-relink','prepare'):
        command_parser=dep_actions.add_parser(action)
        command_parser.add_argument('--file',required=True)
        command_parser.add_argument('--expected-sha256',required=True)
        if action=='plan-relink':command_parser.add_argument('--manifest',type=Path,required=True)
        else:command_parser.add_argument('--plan',type=Path,required=True,help='JSON descriptor containing plan file and expected_sha256')
    validation = sub.add_parser("validate", help="scoped structure/dependency checks; writes validation.json in job")
    validation.add_argument("file")
    validation.add_argument("--expected-sha256")
    for command_parser in (query, diff, audit, validation):
        command_parser.add_argument("--profile", type=Path, help="JSON profile: explicit timeline/resources/evaluation/asset records")
    registration = sub.add_parser("identity", help="prepare persistent UUIDs in an isolated working candidate")
    reg = registration.add_subparsers(dest="action", required=True, parser_class=Parser).add_parser("prepare")
    reg.add_argument("file")
    reg.add_argument("--selectors", type=Path, required=True)
    reg.add_argument("--expected-sha256", required=True)
    asset = sub.add_parser("asset", help="prepare explicit asset edits and working index documents")
    asset_actions = asset.add_subparsers(dest="action", required=True, parser_class=Parser)
    previews=asset_actions.add_parser('preview',help='automatic framing, rendered previews and input-bound receipts').add_subparsers(dest='preview_action',required=True,parser_class=Parser)
    preview=previews.add_parser('prepare');preview.add_argument('file');preview.add_argument('--expected-sha256',required=True);preview.add_argument('--manifest',type=Path,required=True)
    for name in ('resources','driver-profile','simulation-receipt','reuse-receipt'):preview.add_argument('--'+name,type=Path)
    batch_preview=previews.add_parser('batch');batch_preview.add_argument('--manifest',type=Path,required=True);batch_preview.add_argument('--reuse-job')
    asset_actions.add_parser('plan',help='merge complete file shards and plan a validated library transaction').add_argument('--manifest',type=Path,required=True)
    for action in ("prepare", "index"):
        command_parser = asset_actions.add_parser(action)
        command_parser.add_argument("file")
        command_parser.add_argument("--manifest", type=Path, required=True)
        command_parser.add_argument("--expected-sha256", required=True)
    probe = sub.add_parser("probe", help="development probe; only writes inside its new job directory")
    probe.add_argument("case", choices=("scene", "geometry", "nodes", "animation", "render", "exchange"))
    request = sub.add_parser("request", help="execute a strict UTF-8 JSON request")
    request.add_argument("file", type=Path)
    project = sub.add_parser("project", help="plan working copies or explicit recoverable file replacements")
    projects = project.add_subparsers(dest="action", required=True, parser_class=Parser)
    projects.add_parser('package').add_argument('--manifest',type=Path,required=True)
    for action in ('verify','prepare-copy'):
        a=projects.add_parser(action,help='verify a hashed disk project or prepare an independently reopened candidate')
        a.add_argument('file');a.add_argument('--expected-sha256',required=True);a.add_argument('--resources',type=Path,required=True)
        if action=='prepare-copy':a.add_argument('--manifest',type=Path,required=True)
    for group, actions in (('link', ('prepare',)), ('override', ('prepare', 'resync'))):
        operations = projects.add_parser(group, help='bounded library candidates with protected source files').add_subparsers(dest='project_action', required=True, parser_class=Parser)
        for action in actions:
            command_parser = operations.add_parser(action)
            command_parser.add_argument('--manifest', type=Path, required=True)
            command_parser.add_argument('--file', required=group == 'override')
            command_parser.add_argument('--expected-sha256', required=group == 'override')
    single = projects.add_parser("plan-copy", help="dry-run: freeze source hash and new destination")
    single.add_argument("file")
    single.add_argument("--output", required=True)
    batch = projects.add_parser("plan-batch", help="dry-run: read a JSON array of file/output items")
    batch.add_argument("manifest", type=Path)
    files = projects.add_parser("plan-files", help="dry-run: explicit kind/mode/file/output manifest (blend/catalog/json)")
    files.add_argument("manifest", type=Path)
    transaction = sub.add_parser("transaction", help="apply/recover/rollback/status a frozen copy plan")
    tx = transaction.add_subparsers(dest="action", required=True, parser_class=Parser)
    for action in ("apply", "recover", "rollback", "status"):
        tx.add_parser(action).add_argument("transaction_id")
    job = sub.add_parser("job", help="inspect or cancel jobs")
    actions = job.add_subparsers(dest="action", required=True, parser_class=Parser)
    for name in ("status", "result", "cancel", "cleanup-isolation"):
        actions.add_parser(name).add_argument("job_id")
    wait=actions.add_parser('wait',help='wait up to 55 seconds for final result, then return current progress')
    wait.add_argument('job_id');wait.add_argument('--wait-seconds',type=float,default=30)
    return p


def main(argv=None):
    args = None
    compact_command = None
    code = 0
    try:
        args = parser().parse_args(argv)
        if args.compact and args.full:
            raise Failure('INVALID_REQUEST', 'Choose --compact or --full')
        if args.command == 'video' and args.action == 'describe':
            if args.background or args.compact:raise Failure('INVALID_REQUEST','--async/--compact does not apply to video describe')
            from video_interface import describe
            print(json.dumps(describe(args.operation,args.schema,args.section),ensure_ascii=False,allow_nan=False))
            return 0
        if args.command == 'compositing' and args.action == 'describe':
            if args.background or args.compact:raise Failure('INVALID_REQUEST','--async/--compact does not apply to compositing describe')
            from compositing_interface import describe
            print(json.dumps(describe(args.operation,args.schema,args.section,args.param),ensure_ascii=False,allow_nan=False))
            return 0
        if args.command == 'lighting' and args.action == 'describe':
            if args.background or args.compact:raise Failure('INVALID_REQUEST','--async/--compact does not apply to lighting describe')
            from lighting_interface import describe
            print(json.dumps(describe(args.operation,args.schema,args.section),ensure_ascii=False,allow_nan=False))
            return 0
        if args.command == 'geometry' and args.action == 'describe':
            if args.background or args.compact:
                raise Failure('INVALID_REQUEST','--async/--compact does not apply to geometry describe')
            from geometry_interface import describe
            print(json.dumps(describe(args.operation,args.schema,args.section),ensure_ascii=False,allow_nan=False))
            return 0
        if args.command == 'geometry' and args.action == 'session':
            if args.background or args.compact:raise Failure('INVALID_REQUEST','Session requests use their own bounded wait')
            from geometry_session import dispatch
            payload=read_json(args.payload) if args.payload else {}
            payload.update(session_dir=args.session_dir,action=args.session_action,wait_seconds=args.wait_seconds)
            if args.request_id:payload['request_id']=args.request_id
            result=dispatch(payload)
            print(json.dumps(result,ensure_ascii=False,allow_nan=False))
            error = result.get('error')
            return 0 if result.get('ok') else CODES.get(error.get('code'),8) if isinstance(error,dict) else 8
        if args.command == 'material' and args.action == 'describe':
            if args.background or args.compact:
                raise Failure('INVALID_REQUEST', '--async/--compact does not apply to material describe')
            from material_interface import describe
            print(json.dumps(describe(args.operation, args.schema, args.section), ensure_ascii=False, allow_nan=False))
            return 0
        if args.compact:
            if args.command in ('material', 'geometry', 'workflow', 'lighting', 'compositing', 'video'):
                compact_command = args.command + '.' + args.action
            elif args.command == 'request':
                compact_request = read_json(args.file)
                if not isinstance(compact_request, dict):
                    raise Failure('INVALID_REQUEST', 'Request must be an object')
                compact_command = compact_request.get('command')
            elif args.command == 'job':
                compact_command = read_json(runner.job_path(args.jobs_dir, args.job_id) / 'request.json').get('command')
            else:
                action = getattr(args, 'action', None)
                compact_command = args.command + '.' + action if action else args.command
            if compact_command not in COMMANDS:
                raise Failure('INVALID_REQUEST', '--compact requires an executable request/job command')
        if args.command == "job":
            if args.background:
                raise Failure("INVALID_REQUEST", "--async does not apply to job commands")
            job = runner.job_path(args.jobs_dir, args.job_id)
            if args.action == "result":
                path = job / "result.json"
                if not path.is_file():
                    raise Failure("CONFLICT", "Result is not ready; use job status")
                result = read_json(path)
                code = CODES[result["error"]["code"]] if result["error"] else 0
            elif args.action == 'wait':
                result = runner.wait_for_job(job, args.wait_seconds)
                code = CODES[result['error']['code']] if result.get('error') else 0
            else:
                result = getattr(runner, args.action.replace('-','_'))(job)
        else:
            if args.command == "request":
                request = read_json(args.file)
            elif args.command=='doctor' and args.doctor_action:
                request={'schema_version':SCHEMA,'command':'doctor.verify_install','params':{'directory':args.directory}}
            elif args.command in ('resource','cache'):
                params={}
                if getattr(args,'domain',None):params['domain']=args.domain
                if getattr(args,'manifest',None):params['manifest']=read_json(args.manifest)
                request={'schema_version':SCHEMA,'command':args.command+'.'+args.action.replace('-','_'),'params':params}
            elif args.command=='workflow':
                params={'requests':read_json(args.requests)} if args.action=='preflight' else {'manifest':read_json(args.manifest)} if args.action=='run' else {'job_id':args.job_id}
                request={'schema_version':SCHEMA,'command':'workflow.'+args.action,'params':params}
            elif args.command=='pipeline':
                params={'job_id':args.job_id} if args.action=='resume' else {'manifest':read_json(args.manifest)}
                if getattr(args,'reuse_job',None):params['reuse_job']=args.reuse_job
                request={'schema_version':SCHEMA,'command':'pipeline.'+args.action,'params':params}
            elif args.command == "project":
                if args.action=='package':
                    params={'manifest':read_json(args.manifest)};command='project.package'
                elif args.action in ('verify','prepare-copy'):
                    params={'file':args.file,'expected_sha256':args.expected_sha256,'resources':read_json(args.resources)}
                    if args.action=='prepare-copy':params['manifest']=read_json(args.manifest)
                    command='project.'+args.action.replace('-','_')
                elif args.action in ('link', 'override'):
                    params = {'manifest': read_json(args.manifest)}
                    for key in ('file', 'expected_sha256'):
                        if getattr(args, key, None):params[key] = getattr(args, key)
                    command = 'project.' + args.action + '.' + args.project_action
                else:
                    params = {"file": args.file, "output": args.output} if args.action == "plan-copy" else {"items": read_json(args.manifest)}
                    command = "project." + args.action.replace("-", "_")
                request = {"schema_version": SCHEMA, "command": command, "params": params}
            elif args.command == "transaction":
                request = {"schema_version": SCHEMA, "command": "transaction." + args.action, "params": {"transaction_id": args.transaction_id}}
            elif args.command == "asset":
                params={'manifest':read_json(args.manifest)}
                if args.action=='preview':
                    if args.preview_action=='prepare':
                        params.update(file=args.file,expected_sha256=args.expected_sha256)
                        for name in ('resources','driver_profile','simulation_receipt','reuse_receipt'):
                            if getattr(args,name,None):params[name]=read_json(getattr(args,name))
                    elif args.reuse_job:params['reuse_job']=args.reuse_job
                    command='asset.preview.'+args.preview_action
                else:
                    if args.action!='plan':params.update(file=args.file,expected_sha256=args.expected_sha256)
                    command='asset.'+args.action
                request = {"schema_version": SCHEMA, "command": command, "params": params}
            elif args.command in ('render','media','exchange','extension','sculpt'):
                params={}
                for key in ('manifest','resources','simulation_receipt','driver_profile','review_receipt'):
                    if getattr(args,key,None):params[key]=read_json(getattr(args,key))
                for key in ('file','expected_sha256'):
                    if getattr(args,key,None):params[key]=getattr(args,key)
                request={'schema_version':SCHEMA,'command':args.command+'.'+args.action,'params':params}
            elif args.command=='tracking':
                params={}
                for key in ('manifest','resources'):
                    if getattr(args,key,None):params[key]=read_json(getattr(args,key))
                for key in ('file','expected_sha256','clip'):
                    if getattr(args,key,None):params[key]=getattr(args,key)
                request={'schema_version':SCHEMA,'command':'tracking.'+args.action,'params':params}
            elif args.command in ('scene','model','node','rig','simulation'):
                params={'manifest':read_json(args.manifest)} if args.action in ('prepare','bake','package') else {}
                if args.file:params['file']=args.file
                if args.expected_sha256:params['expected_sha256']=args.expected_sha256
                if args.action=='inspect' and args.context:params['context']=read_json(args.context)
                if getattr(args,'resources',None):params['resources']=read_json(args.resources)
                if getattr(args,'driver_profile',None):params['driver_profile']=read_json(args.driver_profile)
                if getattr(args,'suggest_mapping',None):params['suggest_mapping']=read_json(args.suggest_mapping)
                if getattr(args,'hair_frames',None):params['hair_frames']=args.hair_frames
                if getattr(args,'simulation_receipt',None):params['simulation_receipt']=read_json(args.simulation_receipt)
                request={'schema_version':SCHEMA,'command':args.command+'.'+args.action,'params':params}
            elif args.command=='video':
                params={'file':args.file,'expected_sha256':args.expected_sha256,'resources':read_json(args.resources)}
                if args.action=='run':
                    params['manifest']=read_json(args.manifest)
                    if args.output:params['output']=read_json(args.output)
                    if args.resume:
                        if not args.resume_sha256:raise Failure('INVALID_REQUEST','--resume requires SHA')
                        params['resume']={'file':str(args.resume.resolve()),'expected_sha256':args.resume_sha256}
                    elif args.resume_sha256:raise Failure('INVALID_REQUEST','Resume SHA needs report')
                else:
                    params['scene']=args.scene
                    if args.workflow_id:params['workflow_id']=args.workflow_id
                request={'schema_version':SCHEMA,'command':'video.'+args.action,'params':params}
            elif args.command=='compositing' and args.action in ('template-save','template-run','batch','study'):
                request={'schema_version':SCHEMA,'command':'compositing.'+args.action,'params':read_json(args.params)}
            elif args.command=='compositing':
                if args.action=='encode':
                    params=read_json(args.manifest)
                else:
                    params={'file':args.file,'expected_sha256':args.expected_sha256,'resources':read_json(args.resources)}
                    if args.action=='run':
                        params['manifest']=read_json(args.manifest)
                        if args.preview:params['preview']=read_json(args.preview)
                        if args.resume:
                            if not args.resume_sha256:raise Failure('INVALID_REQUEST','--resume requires SHA')
                            params['resume']={'file':str(args.resume.resolve()),'expected_sha256':args.resume_sha256}
                        elif args.resume_sha256:raise Failure('INVALID_REQUEST','Resume SHA needs report')
                    else:params.update(scene=args.scene,workflow_id=args.workflow_id)
                request={'schema_version':SCHEMA,'command':'compositing.'+args.action,'params':params}
            elif args.command=='lighting':
                if args.action=='batch':
                    params=read_json(args.manifest)
                elif args.action=='template-edit':
                    params={'template':{'file':str(args.template.resolve()),'expected_sha256':args.template_sha256},'revision':args.revision,'edits':read_json(args.edits)}
                else:
                    params={'file':args.file,'expected_sha256':args.expected_sha256,'resources':read_json(args.resources)}
                if args.action=='run':
                    if args.template:
                        if not args.template_sha256 or not args.target:raise Failure('INVALID_REQUEST','Template run requires --template-sha256 and --target')
                        params['template']={'file':str(args.template.resolve()),'expected_sha256':args.template_sha256}
                        params['target']=read_json(args.target)
                    else:
                        if args.template_sha256 or args.target:raise Failure('INVALID_REQUEST','Template SHA/target require template')
                        params['manifest']=read_json(args.manifest)
                    if args.preview:params['preview']=read_json(args.preview)
                if args.action in ('run','batch'):
                    if args.resume:
                        if not args.resume_sha256:raise Failure('INVALID_REQUEST','--resume requires SHA')
                        params['resume']={'file':str(args.resume.resolve()),'expected_sha256':args.resume_sha256}
                    elif args.resume_sha256:raise Failure('INVALID_REQUEST','Resume SHA needs report')
                if args.action in ('inspect','template-save'):
                    params['scene']=args.scene
                    if args.rig_id:params['rig_id']=args.rig_id
                    if args.action=='template-save':params.update(template_id=args.template_id,revision=args.revision)
                request={'schema_version':SCHEMA,'command':'lighting.'+args.action,'params':params}
            elif args.command=='geometry':
                params={'resources':read_json(args.resources)}
                if args.action=='run':params['manifest']=read_json(args.manifest)
                if getattr(args,'file',None):params['file']=args.file
                if getattr(args,'expected_sha256',None):params['expected_sha256']=args.expected_sha256
                for key in ('context','targets','observations'):
                    if getattr(args,key,None):params[key]=read_json(getattr(args,key))
                if getattr(args,'discover',False):params['discover']=True
                if getattr(args,'resume',None):
                    if not args.resume_sha256:raise Failure('INVALID_REQUEST','--resume requires --resume-sha256')
                    params['resume']={'file':str(args.resume.resolve()),'expected_sha256':args.resume_sha256}
                elif getattr(args,'resume_sha256',None):raise Failure('INVALID_REQUEST','--resume-sha256 requires --resume')
                request={'schema_version':SCHEMA,'command':'geometry.'+args.action,'params':params}
            elif args.command=='animation':
                params={'file':args.file,'manifest':read_json(args.manifest)}
                if args.expected_sha256:params['expected_sha256']=args.expected_sha256
                if args.driver_profile:params['driver_profile']=read_json(args.driver_profile)
                request={'schema_version':SCHEMA,'command':'animation.sample','params':params}
            elif args.command=='material' and args.action in ('batch','study'):
                params={'file':args.file,'expected_sha256':args.expected_sha256,'manifest':read_json(args.manifest),'resources':read_json(args.resources)}
                if args.action=='study':params['study']=read_json(args.study)
                if args.resume:
                    if not args.resume_sha256:raise Failure('INVALID_REQUEST','--resume requires --resume-sha256')
                    params['resume']={'file':str(args.resume.resolve()),'expected_sha256':args.resume_sha256}
                elif args.resume_sha256:raise Failure('INVALID_REQUEST','--resume-sha256 requires --resume')
                request={'schema_version':SCHEMA,'command':'material.'+args.action,'params':params}
            elif args.command=='material' and args.action=='run':
                params={'file':args.file,'expected_sha256':args.expected_sha256,'resources':read_json(args.resources)}
                if args.manifest:
                    if args.template_sha256 or args.bindings or args.bindings_sha256:raise Failure('INVALID_REQUEST','Template options require --template')
                    params['manifest']=read_json(args.manifest)
                else:
                    if not args.template_sha256 or not args.bindings or not args.bindings_sha256:raise Failure('INVALID_REQUEST','--template requires --template-sha256, --bindings and --bindings-sha256')
                    params.update(template={'file':str(args.template.resolve()),'expected_sha256':args.template_sha256},bindings={'file':str(args.bindings.resolve()),'expected_sha256':args.bindings_sha256})
                request={'schema_version':SCHEMA,'command':'material.run','params':params}
            elif args.command=='material' and args.action=='template-save':
                params={key:getattr(args,key) for key in ('file','expected_sha256','material_id','template_id','name','version')}
                params['resources']=read_json(args.resources)
                request={'schema_version':SCHEMA,'command':'material.template-save','params':params}
            elif args.command in ('material','texture'):
                keys=['file','expected_sha256','resolution','samples','frame']+(['material'] if args.command=='material' else ['object','scene','view_layer','uv_layer','type','margin'])
                params={k:getattr(args,k) for k in keys}
                if args.resources:params['resources']=read_json(args.resources)
                request={'schema_version':SCHEMA,'command':args.command+'.'+args.action,'params':params}
            elif args.command=='dependency' and args.action!='audit':
                key='manifest' if args.action=='plan-relink' else 'plan'
                request={'schema_version':SCHEMA,'command':'dependency.'+args.action,'params':{'file':args.file,'expected_sha256':args.expected_sha256,key:read_json(getattr(args,key))}}
            elif args.command in ("query", "diff", "dependency", "validate", "identity"):
                params = {"file": args.file}
                if args.expected_sha256:
                    params["expected_sha256"] = args.expected_sha256
                if getattr(args, "profile", None):
                    params["profile"] = read_json(args.profile)
                if args.command == "identity":
                    params["selectors"] = read_json(args.selectors)
                if args.command == "query" and args.selector:
                    params["selector"] = read_json(args.selector)
                if args.command == "diff":
                    params["other"] = args.other
                    if args.other_sha256:
                        params["other_sha256"] = args.other_sha256
                request = {"schema_version": SCHEMA, "command": {"dependency": "dependency.audit", "identity": "identity.prepare"}.get(args.command, args.command), "params": params}
            else:
                params = {"file": args.file} if args.command == "inspect" else {"case": args.case} if args.command == "probe" else {}
                if args.command=='inspect' and args.expected_sha256:params['expected_sha256']=args.expected_sha256
                request = {"schema_version": SCHEMA, "command": args.command, "params": params}
            if request.get('command') == 'geometry.session':
                if args.background or args.compact:raise Failure('INVALID_REQUEST','Session requests use their own bounded wait')
                from geometry_session import dispatch
                normalized=validate(request)
                result=dispatch(normalized['params'])
                print(json.dumps(result,ensure_ascii=False,allow_nan=False))
                error = result.get('error')
                return 0 if result.get('ok') else CODES.get(error.get('code'),8) if isinstance(error,dict) else 8
            job = runner.prepare(request, args.blender, args.jobs_dir, args.timeout, args.transactions_dir)
            if args.background:
                result = runner.submit(job)
            else:
                result, code = runner.execute(job)
    except Failure as exc:
        result = envelope(error={"code": exc.code, "message": str(exc)})
        code = CODES[exc.code]
    except OSError as exc:
        result = envelope(error={"code": "IO_ERROR", "message": str(exc)})
        code = 9
    except ValueError as exc:
        result = envelope(error={"code": "INVALID_REQUEST", "message": str(exc)})
        code = 2
    if args and not args.full and not args.compact:
        if args.command == 'workflow':
            compact_command = 'workflow.' + args.action
        elif args.command == 'request' and 'request' in locals() and isinstance(request, dict) and str(request.get('command', '')).startswith('workflow.'):
            compact_command = request['command']
    if args and (args.compact or (compact_command and compact_command.startswith('workflow.') and not args.full)):
        try:
            if compact_command and compact_command.startswith('video.'):
                from video_interface import compact
            elif compact_command and compact_command.startswith('compositing.'):
                from compositing_interface import compact
            elif compact_command and compact_command.startswith('lighting.'):
                from lighting_interface import compact
            elif compact_command and compact_command.startswith('geometry.'):
                from geometry_interface import compact
            elif compact_command and compact_command.startswith('material.'):
                from material_interface import compact
            else:
                from workflow_evidence import compact
            result = compact(result, compact_command)
        except (Failure, OSError, ValueError):
            # Projection is optional. Full execution evidence and exit code win.
            pass
    if args and args.human:
        print(f"{'OK' if result['ok'] else 'ERROR'} {result['command'] or ''} job={result['job_id'] or '-'}")
        print(json.dumps(result["data"] if result["ok"] else result["error"], ensure_ascii=False, indent=2))
    else:
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return code


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
