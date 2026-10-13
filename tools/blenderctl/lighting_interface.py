"""Small discoverable domain index and truthful compact projections."""
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from protocol import Failure,read_json,digest,VERSION
def descriptor(path): return {'file':str(path),'sha256':digest(path)}
def describe(operation=None,schema=False,section=None):
    from lighting_workflow_addon import contract
    from lighting_workflow_contract import schemas as domain_schemas
    schemas=domain_schemas()
    if section:
        props=contract.MANIFEST_SCHEMA.get('properties',{})
        if section not in props: raise Failure('INVALID_REQUEST','Unknown lighting manifest section')
        return {'section':section,'schema':props[section]}
    if schema:
        if operation not in schemas: raise Failure('INVALID_REQUEST','--schema requires a lighting operation')
        return {'operation':operation,'schema':schemas[operation]}
    return {'cli_version':VERSION,'plugin_version':'0.2.1','lighting_schema_version':'1.0',
        'operations':['lighting.run','lighting.inspect','lighting.template-save','lighting.template-edit','lighting.batch'],'actions':['create','patch','restore'],
        'state_source':'SAVED_DISK_V1; GUI handoff through explicit saved snapshot',
        'milestone':'M2: native controls, boards, linking, HDRI/sky, actual-state templates and bounded batches',
        'later_milestones':['character/NPR/interior/exterior','motion acceptance'],
        'contract':descriptor(Path(contract.__file__)),'compact':'--compact request/job result/wait; full report persists',
        'verification':'Scene protection, independent Blender reader, decoded renders; visual quality requires observation'}
def compact(result,requested_command=None):
    root=Path((result.get('artifacts') or {})['job_directory']) if (result.get('artifacts') or {}).get('job_directory') else None
    command=requested_command or result.get('command')
    if root and (root/'request.json').exists(): command=read_json(root/'request.json')['command']
    if not command or not command.startswith('lighting.'):raise Failure('INVALID_REQUEST','Not a lighting job')
    out={k:result[k] for k in ('schema_version','cli_version','command','job_id','ok','error') if k in result}
    report=read_json(root/'lighting-report.json') if root and (root/'lighting-report.json').exists() else result.get('data') or {}
    summary={k:report[k] for k in ('status','source','candidate','template','parent','revision','parent_sha256','implementation_sha256','verification','protection','recovery','checks','metrics','error','support') if k in report}
    if isinstance(summary.get('verification'),dict):
        summary['verification']={k:summary['verification'][k] for k in ('status','blender','build','non_target_protection','error') if k in summary['verification']}
    if isinstance(report.get('inspection'),dict):
        inspection=report['inspection']
        summary['inspection']={k:inspection[k] for k in ('scene','status','rig_id','camera','world','exposure','eevee','rigs','scene_lights') if k in inspection}
    for k in ('state','source_sha256','source_unchanged','health'): 
        if k in (result.get('data') or {}): summary[k]=result['data'][k]
    summary['previews']=[{k:r[k] for k in ('engine','camera','frame','file','sha256','status') if k in r} for r in report.get('previews',[])]
    if 'items' in report:
        summary['items']=[]
        for row in report['items']:
            item={k:row[k] for k in ('id','status','candidate','report','error','reused','recovery','pending_report_path') if k in row}
            for key in ('verification','reuse_verification'):
                if isinstance(row.get(key),dict):
                    item[key]={k:row[key][k] for k in ('status','blender','build','non_target_protection','error') if k in row[key]}
            summary['items'].append(item)
    if result.get('error'):summary['status']={'CANCELLED':'cancelled','TIMEOUT':'timed_out'}.get(result['error']['code'],'failed')
    out.update(output_format='compact',data=summary,artifacts={'job_directory':str(root)} if root else {})
    if root:
        for name in ('request','result','lighting-report','lighting-progress','lighting-reopen'):
            p=root/(name+'.json')
            if p.exists():out['artifacts'][name]=descriptor(p)
    return out
