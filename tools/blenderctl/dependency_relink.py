# SPDX-License-Identifier: GPL-3.0-or-later
"""Same-content path repair into new job-owned blend candidates."""
import copy
import json
from contextlib import ExitStack
from pathlib import Path
import bpy
from protocol import Failure, read_json, atomic_json
from filesystem import FileGuard
from inspection import all_ids, record, path_value
import typed_dependencies as typed

RELINK_KINDS={'image','image_sequence_binding','library','font','sound','movieclip','vse_image','vse_movie','vse_sound','declared_plugin','declared_cache'}

def check_file(descriptor):
    with FileGuard(descriptor['file']) as guard:
        if guard.sha256()!=descriptor['expected_sha256']:raise Failure('CONFLICT','Relink input SHA changed: '+descriptor['file'])

def build_plan(params):
    spec=params['manifest'];check_file(spec['baseline']);baseline=read_json(spec['baseline']['file'])
    source={k:params[k] for k in ('file','expected_sha256')};typed.validate_report(baseline,source)
    if baseline.get('cycles'):raise Failure('UNSUPPORTED','Cyclic library relink requires a dedicated adapter')
    if any(r['kind'] in typed.CACHE_KINDS for r in baseline['resources']):raise Failure('UNSUPPORTED','Use simulation bake mode relocate for receipt-bound cache candidates')
    rows={r['resource_id']:r for r in baseline['resources']};mapping=[];seen=set();path_map={}
    for change in spec['mappings']:
        ident=change['resource_id']
        if ident in seen or ident not in rows:raise Failure('CONFLICT','Relink resource ID is duplicate or absent from baseline')
        seen.add(ident);row=rows[ident]
        if row['kind'] not in RELINK_KINDS or not row['complete']:raise Failure('UNSUPPORTED','Resource has no verified relink adapter')
        old={typed.pathkey(m['file']):m for m in row['members']}
        given=[typed.pathkey(m['from']) for m in change['members']]
        if len(set(given))!=len(given) or set(given)!=set(old):raise Failure('CONFLICT','Relink requires an exact one-to-one map of every external member')
        targets=set()
        for member in change['members']:
            before=old[typed.pathkey(member['from'])];after=member['to'];check_file(after)
            if before['expected_sha256']!=after['expected_sha256']:raise Failure('CONFLICT','Same-name or same-size files are not same-content replacements')
            after={**after,'file':typed.canon(after['file'])};before_key=typed.pathkey(before['file']);after_key=typed.pathkey(after['file'])
            if after_key in targets:raise Failure('CONFLICT','Relink targets must be one-to-one')
            targets.add(after_key)
            if before_key in path_map and path_map[before_key]!=after:raise Failure('CONFLICT','Shared resource received conflicting mappings')
            path_map[before_key]=after
        mapping.append({'resource_id':ident,'owner_file':row['owner_file'],'kind':row['kind'],'members':change['members']})
    # Image sequences/UDIM and strip lists must remain expressible by one path.
    for row in baseline['resources']:
        members=row['members']
        if not any(typed.pathkey(m['file']) in path_map for m in members):continue
        if len(members)>1 or row.get('source') in ('TILED','SEQUENCE') or row['kind'].startswith('declared_') or row['kind']=='vse_image':
            changed=[path_map.get(typed.pathkey(m['file']),m) for m in members]
            if len({typed.pathkey(Path(m['file']).parent) for m in changed})!=1 or any(Path(a['file']).name!=Path(b['file']).name for a,b in zip(members,changed)):raise Failure('CONFLICT','Pattern relink requires one directory and unchanged member basenames')
    inputs={typed.pathkey(d['file']):d for d in baseline['documents'] if typed.pathkey(d['file']) not in path_map}
    inputs.update({typed.pathkey(d['file']):d for d in path_map.values()})
    for d in inputs.values():check_file(d)
    return {'version':'1.0','adapter':'SAME_CONTENT_RELINK_V1','source':source,'baseline':spec['baseline'],'manifest':spec,'profile':baseline['profile'],'mappings':mapping,'path_map':path_map,'inputs':list(inputs.values()),'blender_build':bpy.app.build_hash.decode(),'implementation_sha256':typed.implementation(),'policy':'new candidate files only; original files retained; exact baseline member SHA; no filename inference','source_saved':False}

def plan_relink(params,job):
    plan=build_plan(params);path=job/'dependency-relink-plan.json';atomic_json(path,plan)
    return {'version':'1.0','plan':str(path),'plan_sha256':typed.digest(path),'mapping_count':len(plan['mappings']),'inputs':plan['inputs'],'source_saved':False}

def find_owner(row):
    ident=row['owner'];found=[i for i in all_ids() if not i.library and i.bl_rna.identifier==ident['type'] and i.name==ident['name']]
    if len(found)!=1:raise Failure('CONFLICT','Relink owner no longer resolves uniquely: '+str(ident))
    return found[0]

def editable_path(row):
    path=row['resolved_path']
    if path:
        # Windows path keys are case-insensitive; Blender template tokens are
        # not. Never write the normcase audit representation back verbatim.
        for token in ('<UDIM>','<UVTILE>'):
            if token in row['raw_path']:path=path.replace(token.lower(),token)
    return path

def set_path(row,path):
    owner=find_owner(row);kind=row['kind'];binding=row['binding']
    if getattr(owner,'override_library',None):raise Failure('UNSUPPORTED','Override resource mutation requires a dedicated adapter')
    if kind=='image_sequence_binding':
        tree=owner if isinstance(owner,bpy.types.NodeTree) else owner.node_tree
        node=tree.nodes.get(binding['node']) if tree else None
        if not node or not getattr(node,'image',None) or node.image.library:raise Failure('CONFLICT','Sequence binding requires its local image')
        owner=node.image
    elif kind.startswith('vse_'):
        strip=owner.sequence_editor.strips_all.get(binding['strip']) if owner.sequence_editor else None
        if not strip or strip.type!=binding['type']:raise Failure('CONFLICT','Sequencer binding changed')
        if kind=='vse_image':strip.directory=path;return
        owner=strip.sound if kind=='vse_sound' else strip
    elif kind.startswith('declared_'):
        owner[binding['property']]=path;return
    owner.filepath=path
    # Authored node image manifests contain absolute member paths. They are
    # rewritten below with the very same exact content mapping, never guessed.

def local_content():
    return sorted([record(i) for i in all_ids() if not i.library and not isinstance(i,bpy.types.Library)],key=lambda x:(x['type'],x['name']))

def sync_sound_strips(changes):
    """Blender keeps a second non-RNA filename inside a SOUND strip.

    Reassigning Sound, including through None, does not update it. Rebuild only
    simple top-level strips and prove every scalar setting remained identical.
    """
    targets={typed.pathkey(p) for p in changes.values()}
    def state(strip):
        result={}
        for prop in strip.bl_rna.properties:
            if prop.identifier=='rna_type' or prop.type not in ('STRING','BOOLEAN','INT','FLOAT','ENUM'):continue
            value=getattr(strip,prop.identifier);result[prop.identifier]=list(value) if getattr(prop,'is_array',False) else value
        result['sound']=strip.sound.name if strip.sound else None
        return result
    for scene in bpy.data.scenes:
        if scene.library or not scene.sequence_editor:continue
        editor=scene.sequence_editor
        affected=[s for s in editor.strips_all if s.type=='SOUND' and s.sound and typed.pathkey(path_value(s.sound.filepath,s.sound.library)) in targets]
        for strip in affected:
            if scene.animation_data or strip not in list(editor.strips) or strip.modifiers or strip.connections or strip.retiming_keys or int(strip.frame_start)!=strip.frame_start:
                raise Failure('UNSUPPORTED','Sound relink requires an unanimated top-level strip without modifiers, connections or retiming')
            if any(getattr(other,'input_'+str(i),None)==strip for other in editor.strips_all for i in (1,2,3)):
                raise Failure('UNSUPPORTED','Referenced sound effect strips require a separate relink adapter')
            saved=state(strip);sound=strip.sound;sounds=set(bpy.data.sounds)
            free=next((c for c in range(1,129) if all(s.channel!=c for s in editor.strips)),None)
            if free is None:raise Failure('UNSUPPORTED','Sound relink needs one temporary empty channel')
            replacement=editor.strips.new_sound('__blenderctl_relink_sound',filepath=path_value(sound.filepath,sound.library),channel=free,frame_start=int(strip.frame_start))
            replacement.sound=sound;editor.strips.remove(strip)
            replacement.name=saved['name'];replacement.channel=saved['channel'];replacement.frame_final_start=saved['frame_final_start'];replacement.frame_final_end=saved['frame_final_end']
            for field in ('mute','lock','select','select_left_handle','select_right_handle','blend_type','blend_alpha','effect_fader','use_default_fade','color_tag','show_retiming_keys','volume','pan','sound_offset','show_waveform','pitch_correction'):
                setattr(replacement,field,saved[field])
            for temporary in set(bpy.data.sounds)-sounds:
                if temporary.users:raise Failure('VALIDATION_FAILED','Unexpected sound data user while rebuilding strip')
                bpy.data.sounds.remove(temporary)
            if state(replacement)!=saved:raise Failure('VALIDATION_FAILED','Sound strip reconstruction changed its timing or settings')

def replace_paths(value,mapping):
    if isinstance(value,dict):return {k:replace_paths(v,mapping) for k,v in value.items()}
    if isinstance(value,list):return [replace_paths(v,mapping) for v in value]
    if isinstance(value,str):
        if value in mapping:return mapping[value]
        if value.startswith(('{','[')):
            try:return json.dumps(replace_paths(json.loads(value),mapping),sort_keys=True)
            except (ValueError,TypeError):pass
    return value

def prepare(params,job):
    check_file(params['plan']);plan=read_json(params['plan']['file'])
    expected=build_plan({**{k:params[k] for k in ('file','expected_sha256')},'manifest':plan['manifest']})
    if plan!=expected:raise Failure('CONFLICT','Relink plan does not match its source, baseline or exact mapping')
    baseline=read_json(plan['baseline']['file']);path_map=plan['path_map'];sources={typed.pathkey(v['file']):v['file'] for v in baseline['visited']}
    if len(sources)>32:raise Failure('UNSUPPORTED','Relink candidate graph exceeds 32 blend files')
    graph={key:[] for key in sources}
    for edge in baseline['libraries']:graph[typed.pathkey(edge['from'])].append(typed.pathkey(edge['to']))
    ordered=[];done=set()
    def visit(key):
        if key in done:return
        done.add(key)
        for child in graph.get(key,[]):visit(child)
        ordered.append(key)
    visit(typed.pathkey(params['file']))
    destination=job/'dependency-candidates';destination.mkdir()
    candidates={key:str(destination/('root.blend' if key==typed.pathkey(params['file']) else f'library-{index:03d}.blend')) for index,key in enumerate(ordered)}
    comparison=[];exact_paths={}
    with ExitStack() as stack:
        for descriptor in plan['inputs']:
            guard=stack.enter_context(FileGuard(descriptor['file']))
            if guard.sha256()!=descriptor['expected_sha256']:raise Failure('CONFLICT','Relink source changed')
        for key in ordered:
            original=sources[key];load_file=path_map.get(key,{'file':original})['file']
            bpy.ops.wm.open_mainfile(filepath=load_file,load_ui=False,use_scripts=False)
            local_rows=[r for r in baseline['resources'] if typed.pathkey(r['owner_file'])==key]
            # Restore original-owner resolution before relocating a same-byte
            # library. Parent location must never silently rebase relative paths.
            for row in local_rows:
                if row['kind']=='library':continue
                if row['resolved_path']:set_path(row,editable_path(row))
            for lib in list(bpy.data.libraries):
                if lib.is_library_indirect:continue
                match=[r for r in local_rows if r['kind']=='library' and r['owner']['name']==lib.name]
                if len(match)!=1:raise Failure('CONFLICT','Native library edge no longer matches baseline')
                target=typed.pathkey(match[0]['resolved_path']);lib.filepath=candidates[target];lib.reload()
            bpy.ops.file.make_paths_absolute()
            before=local_content();changes={}
            for row in local_rows:
                if row['kind']=='library' or not row['members']:continue
                members=row['members'];replaced=[path_map.get(typed.pathkey(m['file']),m) for m in members]
                if all(a['file']==b['file'] for a,b in zip(members,replaced)):continue
                old=editable_path(row)
                directory_kind=row['kind'].startswith('declared_') or row['kind']=='vse_image'
                patterned=len(members)>1 or row.get('source') in ('TILED','SEQUENCE')
                new=str(Path(replaced[0]['file']).parent) if directory_kind else str(Path(replaced[0]['file']).parent/Path(old).name) if patterned else replaced[0]['file']
                changes[old]=new;changes[path_value(old)]=new
                set_path(row,new)
                for a,b in zip(members,replaced):changes[a['file']]=b['file']
            # Only the built-in authored image descriptor is normalized; opaque
            # user strings remain untouched and remain part of content checking.
            from nodes import RESOURCE_KEY
            for img in bpy.data.images:
                if not img.library and img.get(RESOURCE_KEY):img[RESOURCE_KEY]=replace_paths(img[RESOURCE_KEY],changes)
            sync_sound_strips(changes)
            want=replace_paths(before,changes);candidate=candidates[key]
            bpy.ops.wm.save_as_mainfile(filepath=candidate,check_existing=False,compress=False,relative_remap=False)
            bpy.ops.wm.open_mainfile(filepath=candidate,load_ui=False,use_scripts=False)
            after=local_content()
            if typed.fingerprint(want)!=typed.fingerprint(after):
                atomic_json(job/f'content-difference-{len(comparison)}.json',{'expected':want,'observed':after})
                raise Failure('VALIDATION_FAILED','Candidate changed non-target local data; difference retained')
            comparison.append({'source':original,'candidate':candidate,'local_datablocks':len(after),'content':'pass','candidate_sha256':typed.digest(candidate)})
            exact_paths.update(changes)
        root=candidates[typed.pathkey(params['file'])]
        closure=typed.audit({'file':root,'expected_sha256':typed.digest(root),'profile':plan['profile']},job)
        atomic_json(job/'dependency-closure.json',closure)
        if closure['closure']!='closed':raise Failure('VALIDATION_FAILED','Candidate dependency closure is partial; report retained')
        # Original file guards remain held through every save/reopen/check.
        for descriptor in plan['inputs']:check_file(descriptor)
    report={'version':'1.0','candidate':root,'candidate_sha256':typed.digest(root),'closure':'closed','closure_report':str(job/'dependency-closure.json'),'closure_sha256':typed.digest(job/'dependency-closure.json'),'mappings':plan['mappings'],'candidates':comparison,'resources':closure['documents'],'reopen':'pass','local_content':'pass','runtime_validation':'separate domain commands required','source_saved':False}
    atomic_json(job/'dependency-relink-report.json',report)
    return report
