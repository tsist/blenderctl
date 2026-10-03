# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared library planning and pre/post-commit validation on the existing transaction core."""
import copy,os,re
from contextlib import ExitStack
from pathlib import Path
from protocol import Failure,SCHEMA,ROOT,read_json,atomic_json,digest
from verification_contract import fields,require

NAMES={'index':'asset-index.json','history':'asset-history.json','catalog':'blender_assets.cats.txt'}
def canonical(p):return os.path.normcase(str(Path(p).resolve()))
def descriptor(d):
    fields(d,('file','expected_sha256'),('file','expected_sha256'))
    require(isinstance(d['file'],str) and '\0' not in d['file'] and Path(d['file']).is_absolute() and Path(d['file']).is_file(),'Input requires an existing absolute file')
    require(isinstance(d['expected_sha256'],str) and bool(re.fullmatch('[0-9a-f]{64}',d['expected_sha256'])),'Input requires SHA256')
    d['file']=str(Path(d['file']).resolve())
    return d

def normalize(m):
    fields(m,('library_root','base','updates','catalogs'),('library_root','base','updates','catalogs'))
    require(isinstance(m['library_root'],str) and Path(m['library_root']).is_absolute() and Path(m['library_root']).is_dir(),'Library root must be an existing absolute directory')
    m['library_root']=str(Path(m['library_root']).resolve())
    fields(m['base'],NAMES,NAMES)
    for v in m['base'].values():require(v is None or isinstance(v,str) and bool(re.fullmatch('[0-9a-f]{64}',v)),'Base values must be SHA256 or null for absent')
    require(isinstance(m['updates'],list) and 1<=len(m['updates'])<=50,'Requires 1..50 complete file updates')
    for u in m['updates']:
        fields(u,('candidate','output','index','lifecycle','acceptance','retirement'),('candidate','output','index','lifecycle'))
        descriptor(u['candidate']);descriptor(u['index'])
        require(isinstance(u['output'],str) and Path(u['output']).is_absolute() and Path(u['output']).suffix.lower()=='.blend','Output must be an absolute blend path')
        u['output']=str(Path(u['output']).resolve())
        require(Path(u['output']).is_relative_to(m['library_root']),'Output outside library')
        require(u['lifecycle'] in ('working','published') and u.get('retirement','archived') in ('archived','quarantine'),'Invalid lifecycle')
        if u['lifecycle']=='published':require('acceptance' in u,'Published assets require acceptance evidence')
        if 'acceptance' in u:descriptor(u['acceptance'])
    require(isinstance(m['catalogs'],list) and len(m['catalogs'])<=1000,'Invalid Catalog definitions')
    for c in m['catalogs']:
        fields(c,('uuid','path','simple_name'),('uuid','path','simple_name'))
        require(all(isinstance(v,str) and v and not any(x in v for x in ('\0','\n','\r',':')) for v in c.values()),'Invalid Catalog fields')
    require(len({c['uuid'].lower() for c in m['catalogs']})==len(m['catalogs']),'Duplicate Catalog UUID definitions')
    return m

def primary(record):
    files=[f for f in record.get('files',[]) if f.get('role')=='primary']
    require(len(files)==1 and Path(files[0]['path']).is_absolute(),'Record requires one absolute primary file')
    return canonical(files[0]['path'])

def check_records(records):
    from asset_validation import schema_errors
    require(isinstance(records,list) and len(records)<=1000,'Index requires at most 1000 records')
    schema=read_json(ROOT/'docs/cli/schemas/asset-record.schema.json')
    for record in records:
        require(not list(schema_errors(record,schema)),'Invalid asset record schema')
        primary(record)

def plan(manifest,launch,job,checkpoint):
    from filesystem import FileGuard,PathLocks
    import transactions as tx
    m=normalize(copy.deepcopy(manifest));root=Path(m['library_root'])
    require(not job.is_relative_to(root) and not Path(launch['transactions_root']).resolve().is_relative_to(root),'Jobs and transactions must be outside this asset library')
    with PathLocks([root]),ExitStack() as stack:
        held={}
        def hold(d):
            name=d['file']
            if name not in held:held[name]=stack.enter_context(FileGuard(name))
            if held[name].sha256(checkpoint)!=d['expected_sha256']:raise Failure('CONFLICT','Input differs from reviewed hash: '+name)
        base={}
        for k,name in NAMES.items():
            p=root/name;expected=m['base'][k]
            if expected is None:
                if os.path.lexists(p):raise Failure('CONFLICT','Expected absent library document: '+str(p))
                base[k]=[] if k!='catalog' else {}
            else:
                hold({'file':str(p),'expected_sha256':expected})
                base[k]=tx.validate_document(p,'catalog') if k=='catalog' else read_json(p)
        require(isinstance(base['index'],list) and isinstance(base['history'],list),'Index/history must be arrays')
        check_records(base['index'])
        for record in base['index']:
            name=primary(record)
            require(Path(name).is_relative_to(canonical(root)) and record['lifecycle'] in ('working','published'),'Invalid active index scope')
            if name not in held:held[name]=stack.enter_context(FileGuard(name))
            entry=next(f for f in record['files'] if f['role']=='primary')
            if entry['sha256'] is not None and held[name].sha256(checkpoint)!=entry['sha256']:raise Failure('CONFLICT','Base index primary hash differs: '+name)
        history_inputs={}
        for entry in base['history']:
            require(isinstance(entry,dict) and isinstance(entry.get('record'),dict),'Invalid history entry')
            check_records([entry['record']])
            d=descriptor({'file':entry.get('preserved_file'),'expected_sha256':entry.get('preserved_sha256')})
            hold(d);history_inputs[d['file']]=d
        outputs=[canonical(u['output']) for u in m['updates']]
        require(len(set(outputs))==len(outputs),'Duplicate update outputs')
        old_ids=[r['asset_id'] for r in base['index']];require(len(set(old_ids))==len(old_ids),'Duplicate base index asset IDs')
        retained=[r for r in base['index'] if primary(r) not in outputs];retired=[r for r in base['index'] if primary(r) in outputs]
        merged=list(retained);items=[];retire_modes={}
        for u in m['updates']:
            hold(u['candidate']);hold(u['index'])
            records=read_json(u['index']['file']);check_records(records)
            for r in records:
                require(primary(r)==canonical(u['output']),'Shard output differs from planned output')
                r['lifecycle']=u['lifecycle']
                if u['lifecycle']=='published':
                    require(int(r['version'][1:])>=1 and bool(re.fullmatch(re.escape(r['type'])+r'_.+_'+re.escape(r['version'])+r'(?:_[A-Za-z0-9]+)*',Path(u['output']).stem)),'Published filename must carry the declared type and version, with optional variant/LOD suffixes')
                    hold(u['acceptance']);r['validation']={'status':'pass','receipt':u['acceptance']}
                for old in retired:
                    if old['asset_id']==r['asset_id'] and old['lifecycle']=='published' and (old['version']==r['version'] or int(r['version'][1:])<=int(old['version'][1:])):
                        raise Failure('CONFLICT','Published asset changes require an increasing new version')
                merged.append(r)
            retire_modes[canonical(u['output'])]=u.get('retirement','archived')
            items.append({'kind':'blend_exact','mode':'replace' if Path(u['output']).exists() else 'create','file':u['candidate']['file'],'output':u['output']})
        ids=[r['asset_id'] for r in merged]
        if len(set(ids))!=len(ids):raise Failure('CONFLICT','Asset ID remains active in another file; retire that file in the same batch')
        definitions=base['catalog']
        for c in m['catalogs']:definitions[c['uuid']]={'path':c['path'],'simple_name':c['simple_name']}
        catalog_text='VERSION 1\n'+''.join(f"{uid}:{c['path']}:{c['simple_name']}\n" for uid,c in sorted(definitions.items()))
        cat=job/NAMES['catalog'];cat.write_text(catalog_text,encoding='utf-8');tx.validate_document(cat,'catalog')
        file_map={primary(r):primary(r) for r in retained}
        file_map.update({canonical(u['output']):u['candidate']['file'] for u in m['updates']})
        for logical,actual in file_map.items():
            if actual not in held:
                held[actual]=stack.enter_context(FileGuard(actual))
        scope={'root':str(root),'records':merged,'catalog_text':catalog_text,'file_map':file_map}
        validation=tx.worker(job,launch,{'schema_version':SCHEMA,'command':'_library_validate','params':scope},checkpoint,{})
        # Apply/recover will hold these unchanged files and resource/evidence inputs.
        stable={**history_inputs,**{d['file']:d for d in validation['inputs']}}
        for logical,actual in file_map.items():
            if logical not in outputs:stable[actual]={'file':actual,'expected_sha256':held[actual].sha256(checkpoint)}
        if set(map(canonical,stable)) & set(outputs):raise Failure('CONFLICT','An updated output is also a protected external resource')
        for d in stable.values():hold(d)
        atomic_json(job/NAMES['index'],merged);atomic_json(job/NAMES['history'],base['history'])
        for k in ('catalog','history','index'):
            items.append({'kind':'catalog' if k=='catalog' else 'json','mode':'replace' if m['base'][k] else 'create','file':str(job/NAMES[k]),'output':str(root/NAMES[k])})
        created=tx.create(items,launch['transactions_root'],launch,checkpoint,mixed=True)
        directory=Path(created['transaction_directory']);p,s=tx.load(directory)
        backups={canonical(i['output']):i.get('target_backup') for i in p['items']}
        old_hashes={canonical(i['output']):i['target_before']['output_sha256'] for i in p['items'] if i['target_before']}
        history=base['history']+[{'record':r,'lifecycle':retire_modes[primary(r)],'transaction_id':directory.name,'preserved_file':backups[primary(r)],'preserved_sha256':old_hashes[primary(r)],'reason':'superseded_or_retired_by_explicit_file_update'} for r in retired]
        atomic_json(job/NAMES['history'],history)
        hist=next(i for i in p['items'] if i['output']==str(root/NAMES['history']))
        with FileGuard(hist['file']) as guard:hist.update(source_sha256=guard.sha256(checkpoint),source_identity=guard.identity(),source_bytes=Path(hist['file']).stat().st_size)
        p['schema_version']='1.3';p['asset_scope']={**scope,'stable_inputs':list(stable.values())}
        p['policy']['purpose']='validated_asset_library';s['plan_hash']=tx.canonical_hash(p)
        atomic_json(directory/'plan.json',p);tx.persist(directory,s)
        atomic_json(job/'library-preflight.json',validation)
        return {**created,'plan':p,'plan_hash':s['plan_hash'],'active_assets':len(merged),'history_entries':len(history),'business_preflight':'pass','next_step':'transaction apply; index is committed last; recover or rollback on interruption'}

def acquire_inputs(scope,stack,checkpoint):
    from filesystem import FileGuard
    for d in scope['stable_inputs']:
        guard=stack.enter_context(FileGuard(d['file']))
        if guard.sha256(checkpoint)!=d['expected_sha256']:raise Failure('CONFLICT','Library resource/evidence changed after planning: '+d['file'])

def validate_transaction(directory,plan,state,launch,checkpoint,final=False):
    import transactions as tx
    scope=copy.deepcopy(plan['asset_scope']);scope.pop('stable_inputs')
    for i,r in zip(plan['items'],state['items']):
        if i['kind']=='blend_exact':scope['file_map'][canonical(i['output'])]=i['output'] if final or r['state']=='committed' else r['stage_path']
    result=tx.worker(directory,launch,{'schema_version':SCHEMA,'command':'_library_validate','params':scope},checkpoint,{})
    atomic_json(directory/('library-final.json' if final else 'library-staged.json'),result)
    return result

def validate_worker(scope):
    from inspection import snapshot,validate_snapshot
    from asset_validation import validate_records
    from dependencies import file_fingerprint
    inputs={};reports=[]
    def evidence(d):
        descriptor(d);actual=file_fingerprint(d['file'])
        require(actual['sha256']==d['expected_sha256'],'Evidence hash mismatch')
        inputs[d['file']]=d
    for logical,actual in scope['file_map'].items():
        data=snapshot(actual);data['file']=logical
        records=[r for r in scope['records'] if primary(r)==logical]
        marked=[b['asset_id'] for b in data['datablocks'] if b['asset']]
        if None in marked or sorted(marked)!=sorted(r['asset_id'] for r in records):raise Failure('VALIDATION_FAILED','Shard does not exactly cover active asset marks: '+logical)
        findings=validate_records(records,[{'library_root':scope['root'],'text':scope['catalog_text']}],data,file_map=scope['file_map'])
        findings+=validate_snapshot(data)['findings']
        if any(f['severity']=='error' for f in findings):raise Failure('VALIDATION_FAILED','Library file failed validation: '+str(findings))
        published=any(r['lifecycle']=='published' for r in records)
        for d in data['dependencies']['items']:
            if d['role']=='output':continue
            if published and d['status'] not in ('exists','packed','builtin','generated','no_external_path'):raise Failure('VALIDATION_FAILED','Published dependency coverage is incomplete')
            for path in d.get('expected_files',[d.get('resolved_path')]):
                if path and Path(path).is_file() and path not in d.get('packed_members',[]):
                    inputs[path]={'file':path,'expected_sha256':file_fingerprint(path)['sha256']}
        for r in records:
            for entry in r['files']:
                if entry['role']=='primary':continue
                raw=Path(entry['path']);path=raw if raw.is_absolute() else Path(entry['path_base'])/raw
                if path.is_file():
                    path=str(path.resolve());inputs[path]={'file':path,'expected_sha256':file_fingerprint(path)['sha256']}
            if r['lifecycle']!='published':continue
            source=r['source'];evidence({'file':source['license_evidence_path'],'expected_sha256':source.get('license_evidence_sha256')})
            receipt=r['validation'].get('receipt');require(isinstance(receipt,dict),'Published entry needs an acceptance receipt')
            evidence(receipt);proof=read_json(receipt['file'])
            require(proof.get('candidate_sha256')==digest(actual) and r['asset_id'] in proof.get('asset_ids',[]) and proof.get('intended_use')==source['intended_use'],'Acceptance receipt does not bind this asset/version/use')
            checks=proof.get('checks',{})
            for name in ('import','preview','usage'):
                require(isinstance(checks.get(name),dict) and checks[name].get('status')=='pass','Missing passed acceptance check: '+name)
                evidence(checks[name]['evidence'])
        reports.append({'file':logical,'assets':len(records),'lifecycle':'published' if published else 'working_or_retired','findings':findings})
    return {'ok':True,'files':reports,'inputs':list(inputs.values()),'scope':'declared_use_and_available_validators; acceptance and license meaning are supplied attestations'}
