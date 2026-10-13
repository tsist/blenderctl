"""Pure Host contract for isolated M4; imports no bpy or numeric runtime."""
import copy,hashlib,json,math,os,re
from pathlib import Path

class EncodeError(Exception):
    def __init__(self,code,message):self.code=code;super().__init__(message)

def require(condition,message,code='VALIDATION_FAILED'):
    if not condition:raise EncodeError(code,message)

def exact(value,keys,label):require(isinstance(value,dict) and set(value)==set(keys),label+' exact fields required')
def descriptor(value):
    exact(value,['file','expected_sha256'],'descriptor')
    require(isinstance(value['file'],str) and Path(value['file']).is_absolute(),'Absolute path required')
    require(isinstance(value['expected_sha256'],str) and re.fullmatch('[0-9a-f]{64}',value['expected_sha256']),'SHA256 required')
    return value
def from_row(value):return descriptor({'file':value['file'],'expected_sha256':value['sha256']})
def signature(value):return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()

AGX_CONFIG_SHA='df5714c85d5afb5e9762281503a0c48a9f65dadb22d32a3aee50c454a0821f62'
AGX_VIEW='AgX - HDR 1000 nits'
def mapping_policy(p):
    c=p['encode_color']
    return c.get('mapping_policy',{'kind':'LINEAR_RETAIN_REFERENCE'})

def normalize_params(params):
    p=copy.deepcopy(params);keys=['encode_version','producer','select','fps','encode_color','alpha','mastering','runtime','quality','budget']
    require(set(p)==set(keys) or set(p)==set(keys+['resume']),'Encode request exact fields required')
    require(p['encode_version']=='1.0-candidate','Unknown candidate version')
    descriptor(p['producer'])
    s=p['select'];require(s.get('mode') in ['produced_frames','still_repeat'],'Selection mode required')
    exact(s,['mode','frames','repeat_count'] if s['mode']=='still_repeat' else ['mode','frames'],'select')
    require(isinstance(s['frames'],list) and 1<=len(s['frames'])<=300 and all(type(f)==int for f in s['frames']) and len(set(s['frames']))==len(s['frames']),'Unique explicit source frames required')
    if s['mode']=='still_repeat':require(len(s['frames'])==1 and type(s['repeat_count'])==int and 1<=s['repeat_count']<=300,'Explicit bounded still repetition required')
    else:require(all(b==a+1 for a,b in zip(s['frames'],s['frames'][1:])),'Produced frames must be contiguous in request order')
    require(isinstance(p['fps'],list) and len(p['fps'])==2 and all(type(x)==int and 1<=x<=120000 for x in p['fps']) and 1<=p['fps'][0]/p['fps'][1]<=120,'Explicit rational fps1..120 required')
    c=p['encode_color'];policy=mapping_policy(p)
    shared=['view','source_display_policy','look','white_balance','curves','gamma','exposure','gamut_policy']
    require(policy.get('kind') in ['LINEAR_RETAIN_REFERENCE','OCIO_DISPLAY_VIEW'],'Explicit supported mapping policy required')
    hdr=policy['kind']=='OCIO_DISPLAY_VIEW'
    if hdr:
        exact(c,shared+['mapping_policy'],'AgX encode_color')
        exact(policy,['kind','display','view','ocio_config_sha256','declared_display_reference_white_nits','extra_gain'],'OCIO mapping policy')
        require(policy=={'kind':'OCIO_DISPLAY_VIEW','display':'Rec.2100-PQ','view':AGX_VIEW,'ocio_config_sha256':AGX_CONFIG_SHA,'declared_display_reference_white_nits':100,'extra_gain':'NONE'},'Only exact SHA-bound AgXHDR view, no extra gain qualified')
    else:
        exact(c,shared+['reference_white_nits']+(['mapping_policy'] if 'mapping_policy' in c else []),'Standard encode_color')
        exact(policy,['kind'],'Linear mapping policy')
        require(c['reference_white_nits'] in [100,203],'Reference white must be explicit100 or203')
    require(c['view']==(AGX_VIEW if hdr else 'Standard') and c['source_display_policy']=='EXPLICIT_OVERRIDE' and c['look']=='None' and c['white_balance'] is False and c['curves'] is False and type(c['gamma']) in [int,float] and c['gamma']==1 and c['gamut_policy']==('OCIO_VIEW' if hdr else 'RETAIN_REFERENCE'),'Only explicit profile/no look/WB/curves/gamma1 qualified')
    require(type(c['exposure']) in [int,float] and math.isfinite(c['exposure']) and -20<=c['exposure']<=20,'Bounded finite exposure required')
    a=p['alpha'];require(a.get('mode') in ['require_opaque','composite_matte'],'Alpha policy required')
    exact(a,['mode'] if a['mode']=='require_opaque' else ['mode','association','matte_rgb_output_linear'],'alpha')
    if a['mode']=='composite_matte':
        require(a['association']=='PREMUL','Only explicitly declared premultiplied compositing qualified')
        require(isinstance(a['matte_rgb_output_linear'],list) and len(a['matte_rgb_output_linear'])==3 and all(type(x) in [int,float] and math.isfinite(x) and x>=0 for x in a['matte_rgb_output_linear']),'Finite nonnegative linear matte required')
    m=p['mastering'];exact(m,['kind','primaries','white_point','min_nits','max_nits','statement_source'],'mastering')
    require(m['kind']=='technical_target' and m['primaries']==('P3D65' if hdr else 'BT2020') and m['white_point']==[.3127,.329] and m['min_nits']==.0001 and m['max_nits']==(1000 if hdr else 10000) and isinstance(m['statement_source'],str) and bool(m['statement_source'].strip()),'Profile-matched explicit virtual technical target required; no measured Master claim')
    r=p['runtime'];exact(r,['python','ffmpeg','ffprobe','modules'],'runtime')
    for k in ['python','ffmpeg','ffprobe']:descriptor(r[k])
    exact(r['modules'],['PyOpenColorIO','OpenImageIO','numpy','python_native'],'runtime.modules')
    for name,items in r['modules'].items():
        require(isinstance(items,list) and bool(items),'Runtime module file manifest required:'+name)
        for row in items:descriptor(row)
    q=p['quality'];exact(q,['crf','preset','gray_rois'],'quality')
    require(type(q['crf'])==int and 0<=q['crf']<=18 and q['preset'] in ['medium','slow'],'Qualified fidelity encoder settings required')
    require(isinstance(q['gray_rois'],list) and len(q['gray_rois'])<=128,'Explicit gray ROI list required')
    for roi in q['gray_rois']:
        exact(roi,['label','rect'],'gray ROI');require(isinstance(roi['label'],str) and isinstance(roi['rect'],list) and len(roi['rect'])==4 and all(type(x)==int and x>=0 for x in roi['rect']),'Integer ROI rectangle required')
        require(roi['rect'][2]>0 and roi['rect'][3]>0,'ROI area must be positive')
    b=p['budget'];exact(b,['max_pixel_frames','timeout_seconds'],'budget')
    require(type(b['max_pixel_frames'])==int and 1<=b['max_pixel_frames']<=2147483648 and type(b['timeout_seconds'])==int and 1<=b['timeout_seconds']<=7200,'Explicit bounded work budget required')
    if 'resume' in p:descriptor(p['resume'])
    return p

def anchor_documents(p):
    p=normalize_params(p)
    return [p['producer'],*[p['runtime'][k] for k in ['python','ffmpeg','ffprobe']],*[r for rows in p['runtime']['modules'].values() for r in rows],*([p['resume']] if 'resume' in p else [])]

def producer_documents(report,p):
    require(report.get('compositing_report_version')=='1.0' and report.get('status')=='passed','Passed actual M2/M3 producer report required')
    require(report.get('verification',{}).get('status')=='passed' and report.get('output_verification',{}).get('status')=='passed','Independent saved and output verification required')
    require(report['verification'].get('candidate_sha256')==report['candidate']['sha256'] and report['output_verification'].get('candidate_sha256')==report['candidate']['sha256'],'Producer candidate verifier identity differs')
    color=report['source_color_identity'];view=color['view']
    if mapping_policy(p)['kind']=='OCIO_DISPLAY_VIEW':require(color['ocio']['sha256']==mapping_policy(p)['ocio_config_sha256'],'AgX producer OCIO SHA differs from explicit mapping policy','CONFLICT')
    require(view.get('look') in ['None','NONE',''] and not view.get('use_white_balance') and not view.get('use_curve_mapping') and view.get('gamma')==1,'Unsupported source look/WB/curves/gamma; no implicit approximation')
    require(color['ocio'].get('file_transform_scan')=='complete' and not color['ocio'].get('unresolved_file_transforms'),'Producer OCIO closure unknown')
    files=report['color_immutable_inputs']['files'];declared=[{'file':color['ocio']['file'],'sha256':color['ocio']['sha256']},*color['ocio']['luts']]
    require(report['color_immutable_inputs'].get('status')=='guarded' and len(files)==17 and {(x['file'],x['sha256']) for x in files}=={(x['file'],x['sha256']) for x in declared},'Matched configured OCIO+16LUT guarded closure required')
    outputs=report['outputs'];selected=[]
    for frame in p['select']['frames']:
        matches=[o for o in outputs if o.get('role')=='result' and o.get('frame')==frame and o.get('format')=='OPEN_EXR']
        require(len(matches)==1,'Exactly one selected producer result EXR per frame required')
        o=matches[0];cs=o.get('output_color_space',{});ws=o.get('working_space',{})
        require(o.get('depth')=='32' and cs.get('status')=='linear_space_RNA' and cs.get('has_linear_colorspace') and cs.get('interop_id') and ws.get('status')=='configured_scene_linear_role','Actual output linear-space RNA/interop and working role required')
        require(o['ocio_identity'].get('file')==color['ocio']['file'] and o['ocio_identity'].get('sha256')==color['ocio']['sha256'],'Output OCIO differs from producer')
        require(o.get('decode',{}).get('finite') and o['decode'].get('precision')=='float' and o['decode'].get('channels')==['R','G','B','A'],'Producer independently decoded RGBA32f required')
        selected.append(o)
    require(len({(o['width'],o['height'],o['output_color_space']['name'],o['output_color_space']['interop_id']) for o in selected})==1,'Frame dimensions/color-space must agree')
    independently_read={(x['file'],x['sha256']) for x in report['output_verification'].get('outputs',[])}
    require(all((o['file'],o['sha256']) in independently_read for o in selected),'Selected frame missing matching independent output receipt')
    return [from_row(report['source']),from_row(report['candidate']),*[from_row(o) for o in outputs+report.get('baselines',[])],*[from_row(x) for x in files]],selected
