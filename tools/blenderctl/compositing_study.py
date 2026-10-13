"""M5 prototype: finite same-condition static candidates and explicit comparison artifacts."""
import copy,html,json,math
from contextlib import ExitStack
from pathlib import Path
from protocol import atomic_json,read_json,digest
from compositing_batch_contract import normalize_study_params,expand_study,fail
import compositing_batch as batch

def conditions(child):
    color=child.get('source_color_identity',{});ocio=color.get('ocio',{})
    preview=child.get('preview_spec',{})
    return {'source':child['source'],'preview':{k:preview.get(k) for k in ('width','height','frames','samples','engine','camera','device','precision','compositor_device','allow_cpu_fallback')},'device':child.get('device'),'view':color.get('view'),'display':color.get('display'),'working_space':color.get('working_space'),'ocio':{k:ocio.get(k) for k in ('file','sha256','luts','file_transform_scan')},'outputs_color':[{'format':r['format'],'working_space':r.get('working_space'),'output_color_space':r.get('output_color_space')} for r in child['outputs']]}

def comparisons(rows,job):
    import OpenImageIO as oiio
    import numpy as np
    job=Path(job);images=[];htmlrows=[];thumbnails=[]
    for row in rows:
        if row['status']!='passed':continue
        output=next((r for r in row['outputs'] if r['format']=='PNG'),None)
        if not output:continue
        with ExitStack() as stack:
            batch.guard({'file':output['file'],'expected_sha256':output['sha256']},stack)
            reader=oiio.ImageInput.open(output['file'])
            if not reader:fail('Comparison PNG failed to decode','VALIDATION_FAILED')
            try:
                spec=reader.spec();data=np.asarray(reader.read_image(format=oiio.FLOAT),dtype=np.float64)
                if reader.format_name()!='png' or list(spec.channelnames)!=['R','G','B','A'] or not np.isfinite(data).all():fail('Comparison requires actual finite PNG RGBA','VALIDATION_FAILED')
                scale=min(1,256/max(spec.width,spec.height));w=max(1,round(spec.width*scale));h=max(1,round(spec.height*scale))
                ix=np.linspace(0,spec.width-1,w).round().astype(int);iy=np.linspace(0,spec.height-1,h).round().astype(int)
                # JPEG inspection displays decoded RGB without Alpha; originals
                # preserve RGBA and remain the authoritative Alpha evidence.
                rgb=np.clip(data[iy[:,None],ix[None,:],:3]*255,0,255).astype(np.uint8)
            finally:reader.close()
        thumb=job/('thumb-'+row['id']+'.jpg');write=oiio.ImageOutput.create(str(thumb));s=oiio.ImageSpec(w,h,3,oiio.UINT8);s.attribute('CompressionQuality',90)
        if not write.open(str(thumb),s) or not write.write_image(rgb):fail('JPEG comparison encode failed','VALIDATION_FAILED')
        write.close();thumbnails.append(batch.reference(thumb));images.append((row['id'],rgb));htmlrows.append('<figure><img src="'+html.escape(thumb.name,quote=True)+'"><figcaption>'+html.escape(row['id'])+' <a href="'+html.escape(Path(output['file']).as_uri(),quote=True)+'">Original RGBA PNG</a><pre>'+html.escape(json.dumps(row.get('changes',[]),ensure_ascii=False,indent=2))+'</pre></figcaption></figure>')
    if not images:return {'status':'not_run_no_passed_png','files':[]}
    cols=min(3,len(images));rowsn=math.ceil(len(images)/cols);cell=256;canvas=np.zeros((cell*rowsn,cell*cols,3),dtype=np.uint8)
    for i,(_,rgb) in enumerate(images):y=(i//cols)*cell;x=(i%cols)*cell;canvas[y:y+rgb.shape[0],x:x+rgb.shape[1]]=rgb
    contact=job/'study-contact.jpg';writer=oiio.ImageOutput.create(str(contact));spec=oiio.ImageSpec(canvas.shape[1],canvas.shape[0],3,oiio.UINT8)
    if not writer.open(str(contact),spec) or not writer.write_image(canvas):fail('Contact JPEG encode failed','VALIDATION_FAILED')
    writer.close();reader=oiio.ImageInput.open(str(contact));assert reader and reader.format_name()=='jpeg';reader.close()
    page=job/'study-comparison.html';page.write_text('<!doctype html><meta charset="utf-8"><title>Compositing study</title><style>body{font:16px sans-serif;background:#171717;color:#eee}main{display:flex;flex-wrap:wrap}figure{margin:16px}img{max-width:256px}pre{font-size:12px;max-width:300px;white-space:pre-wrap}</style><h1>Baseline and finite candidates</h1><p>JPEG previews discard Alpha and display decoded RGB. Inspect original PNG/EXR for Alpha and technical quality. Choose manually; no automatic ranking.</p><main>'+''.join(htmlrows)+'</main>',encoding='utf-8')
    labels=job/'study-contact-labels.json';atomic_json(labels,{'layout':'row_major','ids':[id for id,_ in images],'display_scope':'Decoded RGB without Alpha; authoritative original RGBA retained'})
    return {'status':'passed','files':[batch.reference(p) for p in (contact,page,labels)]+thumbnails,'preview_count':len(images),'user_decision':'required','selected_candidate':None}

def run(params,job,*,single_runner=None,runtime=None,cancel_parent=None):
    params=normalize_study_params(params);job=Path(job).resolve();job.mkdir(parents=True,exist_ok=True)
    if (job/'study-report.json').exists():fail('Study requires a new attempt','CONFLICT')
    mock=single_runner is not None or runtime is not None;runtime=runtime or batch.default_runtime();expanded,diffs=expand_study(params)
    report={'report_version':'1.0','operation':'compositing.study','status':'running','attempt_directory':str(job),'request':batch.original(params),'request_fingerprint':batch.fingerprint(batch.original(params)),'implementation_sha256':params['implementation_sha256'],'runtime':runtime,'evidence_kind':'mock_only' if mock else 'normal_host','certified_producer':False,'cases':[],'decision':{'selected_candidate':None,'ranking':'not_run'},'scope':'Finite static same-condition comparison; mock never certifies public producer'}
    atomic_json(job/'study-report.json',report)
    lifecycle=ExitStack();parents=lifecycle.enter_context(ExitStack());artifacts=lifecycle.enter_context(ExitStack())
    try:
        from compositing_batch_contract import input_documents
        for row in input_documents(params,'compositing.study'):batch.guard(row,parents)
        if not mock:
            import bpy
            batch.guard({'file':bpy.app.binary_path,'expected_sha256':runtime['binary_sha256']},parents)
        authority=None
        if 'resume' in params:
            batch.guard(params['resume'],parents);prior=read_json(params['resume']['file']);root=Path(params['resume']['file']).resolve().parent
            if prior.get('operation')!='compositing.study' or prior.get('request_fingerprint')!=report['request_fingerprint']:fail('Study resume request differs','CONFLICT')
            authority=batch.verify_parent_attempt(root,prior,parents,expected_command='compositing.study',expected_request=batch.original(params),expected_identity=params['implementation_sha256'],expected_runtime=runtime,allow_mock=mock)
            child=prior.get('batch_report')
            if not child or Path(child['file']).resolve()!=root/'batch/batch-report.json':fail('Study batch checkpoint not exact owned path','CONFLICT')
            batch.guard(child,parents);expanded['resume']=child
        result=batch.run(expanded,job/'batch',single_runner=single_runner,runtime=runtime if mock else None,cancel_parent=cancel_parent or job,_resume_authority=authority)
        batch.cancel(job,cancel_parent);report.update(batch_report=batch.reference(job/'batch/batch-report.json'),cases=copy.deepcopy(result['items']),status=result.get('domain_status_observed',result['status']))
        batch.guard(report['batch_report'],artifacts)
        baseline=next((r for r in report['cases'] if r['id']=='baseline'),None)
        if baseline is None or baseline['status']!='passed':fail('Study baseline failed; successful candidates retained','VALIDATION_FAILED')
        condition=None
        for row in report['cases']:
            batch.cancel(job,cancel_parent);row['changes']=diffs[row['id']]
            if row['status']=='passed':
                batch.guard(row['report'],artifacts);child=read_json(row['report']['file'])
                for entry in batch.child_artifacts(child):batch.guard(entry,artifacts)
                actual=conditions(child)
                if condition is None:condition=actual
                elif actual!=condition:fail('Study actual conditions differ','VALIDATION_FAILED')
        report['conditions']=condition;report['comparison']=comparisons(report['cases'],job)
        for row in report['comparison'].get('files',[]):batch.guard(row,artifacts)
        report['closure_guard_validation']=batch.recheck(artifacts)
        report['parent_guard_validation']=batch.recheck(parents)
        from compositing_identity import implementation_sha256
        if implementation_sha256()!=params['implementation_sha256']:fail('Study implementation changed','CONFLICT')
        if not mock and batch.default_runtime()!=runtime:fail('Study runtime changed before acceptance','CONFLICT')
        batch.cancel(job,cancel_parent);observed=report['status'];report.update(status='mock_only' if mock else observed,domain_status_observed=observed,certified_producer=not mock and observed in ('passed','partial'))
        atomic_json(job/'study-report.json',report)
    except BaseException as error:
        report.update(status={'CANCELLED':'cancelled','TIMEOUT':'timed_out'}.get(getattr(error,'code',None),'failed'),error=batch.error_row(error),certified_producer=False,comparison={'status':'not_run','reason':'Baseline/condition/output comparison did not complete'})
        if (job/'batch/batch-report.json').exists():report['batch_report']=batch.reference(job/'batch/batch-report.json')
        atomic_json(job/'study-report.json',report);raise
    finally:lifecycle.close()
    return report
