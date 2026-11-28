"""Original bounded 2.5D supplemental-light prototype. No file/UI/model operations."""
import copy
import math

LEGACY_VERSION='relight-kernel-draft-0.1'
PROJECTION_VERSION='relight-kernel-draft-0.2'
VERSION='relight-kernel-draft-0.3'
ANIME_VERSION='relight-kernel-draft-0.4'
ANIME_VIEWS=('RESULT','PARA','FORM','DARK_SIDE','RIM','LINE','DELTA','DEPTH','NORMAL','VALIDITY')
ANIME_FIELDS=('form','wrap','para_spread','back_dark','back_span','dark_side_reach',
              'shadow_lo','shadow_hi','shadow_tint','shadow_color','cel','cel_steps','cel_soft',
              'rim_width','rim_edge0','rim_edge1','rim_gain','rim_white_mix','rim_light_ids',
              'line_core_lo','line_core_hi','solo_light_id','subject_source','view','delta_gain')

class ProjectionError(ValueError):
    """A rejected receiver-plane revision; prepared input remains reusable."""
    code='INVALID_REQUEST'

def need(condition,message):
    if not condition:raise ValueError(message)

def number(v,lo,hi):
    try:good=type(v) in (float,int) and math.isfinite(v) and lo<=v<=hi
    except OverflowError:good=False
    need(good,'Invalid finite number/range')
    return float(v)

def vector(v,lo=-10000,hi=10000):
    need(type(v) is list and len(v)==3,'Expected JSON vector3')
    return [number(x,lo,hi) for x in v]

def exact(v,keys):need(type(v) is dict and set(v)==set(keys),'Unknown/missing fields')

def normalize(config):
    config=copy.deepcopy(config)
    need(type(config) is dict,'Config must be object')
    need(type(config.get('version')) is str and config['version'] in (LEGACY_VERSION,PROJECTION_VERSION,VERSION,ANIME_VERSION),'Unsupported version')
    fields=('version','frame','lights','mix','bypass','material','line_protect','quality')
    if config['version']==ANIME_VERSION:extras=('projection','base','anime')
    elif config['version']==VERSION:extras=('projection','base')
    elif config['version']==PROJECTION_VERSION:extras=('projection',)
    else:extras=()
    need(set(config)==set(fields+extras) or (config['version']==ANIME_VERSION and set(config)==set(fields+extras+('environment',))),'Unknown/missing fields')
    need(type(config['bypass']) is bool,'Unsupported bypass type')
    number(config['mix'],0,1);number(config['line_protect'],0,1)
    f=config['frame']
    need(type(f) is dict,'Frame must be object')
    if f.get('kind')=='screen_heightfield':exact(f,('kind','height_scale'));number(f['height_scale'],.0001,10)
    else:
        exact(f,('kind','fx','fy','cx','cy'));need(f['kind']=='metric_perspective','Unsupported geometry space')
        number(f['fx'],.001,100000);number(f['fy'],.001,100000);number(f['cx'],-100000,100000);number(f['cy'],-100000,100000)
    m=config['material'];exact(m,('roughness','f0'));number(m['roughness'],.02,1);vector(m['f0'],0,1)
    q=config['quality'];exact(q,('shadow_steps','shadow_bias','sun_reach','max_work'))
    need(type(q['shadow_steps']) is int and 0<=q['shadow_steps']<=64,'Invalid shadow step budget')
    number(q['shadow_bias'],.000001,10);number(q['sun_reach'],.001,10000)
    need(type(q['max_work']) is int and 1<=q['max_work']<=2147483647,'Invalid work budget')
    need(type(config['lights']) is list and len(config['lights'])<=8,'Max8 lights')
    ids=[]
    for l in config['lights']:
        need(type(l) is dict and type(l.get('type')) is str,'Light must be typed object')
        common=('id','type','intensity','color','diffuse','specular','samples')
        extras={'POINT':('position','radius','softening','reference_distance'),
                'SPOT':('position','radius','softening','reference_distance','aim','cone_deg','blend'),
                'AREA':('position','softening','reference_distance','aim','shape','size','size_y'),
                'SUN':('direction','anchor','angle_deg')}
        need(l.get('type') in extras,'Unknown light type');exact(l,common+extras[l['type']])
        need(type(l['id']) is str and 0<len(l['id'])<=64 and l['id'] not in ids,'Unique light id required');ids.append(l['id'])
        number(l['intensity'],0,32);vector(l['color'],0,16);number(l['diffuse'],0,8);number(l['specular'],0,8)
        need(type(l['samples']) is int and l['samples'] in (1,4,8,16,32),'Explicit bounded light samples')
        if l['type']=='SUN':
            direction=vector(l['direction']);need(sum(x*x for x in direction)>1e-12,'Zero sun direction')
            vector(l['anchor']);number(l['angle_deg'],0,90)
        else:
            position=vector(l['position']);number(l['softening'],.000001,1000);number(l['reference_distance'],.000001,1000)
            if l['type'] in ('POINT','SPOT'):number(l['radius'],0,1000)
            if l['type'] in ('SPOT','AREA'):
                aim=vector(l['aim']);need(sum((a-b)**2 for a,b in zip(aim,position))>1e-12,'Aim equals position')
            if l['type']=='SPOT':number(l['cone_deg'],.001,179.9);number(l['blend'],0,1)
            if l['type']=='AREA':
                need(l['shape'] in ('SQUARE','RECTANGLE','DISK','ELLIPSE'),'Unknown area shape')
                number(l['size'],.000001,1000);number(l['size_y'],.000001,1000)
                need(l['shape'] in ('RECTANGLE','ELLIPSE') or l['size_y']==l['size'],'Symmetric shape size_y differs')
    if config['version'] in (PROJECTION_VERSION,VERSION,ANIME_VERSION):
        projection=config['projection'];exact(projection,('enabled','point','normal','strength','transmission','light_ids'))
        need(type(projection['enabled']) is bool,'Projection enabled must be bool')
        vector(projection['point']);n=vector(projection['normal'])
        need(any(x!=0 for x in n),'Zero receiver plane normal')
        number(projection['strength'],0,1);vector(projection['transmission'],0,1)
        selected=projection['light_ids']
        need(type(selected) is list and all(type(i) is str and i in ids for i in selected),'Projection requires existing light ids')
        need(len(set(selected))==len(selected),'Projection requires unique light ids')
    if config['version'] in (VERSION,ANIME_VERSION):
        base=config['base'];exact(base,('exposure','contrast','saturation','mask'))
        number(base['exposure'],-4,4);number(base['contrast'],.25,2);number(base['saturation'],0,2)
        need(base['mask'] in ('ALL','LIGHT'),'Base mask must be ALL or LIGHT')
    if config['version']==ANIME_VERSION:
        need(f['kind']=='screen_heightfield','Anime 0.4 prototype currently requires screen_heightfield')
        need(not config['projection']['enabled'],'Anime 0.4 prototype does not combine controlled projection')
        a=config['anime'];exact(a,ANIME_FIELDS)
        for key,lo,hi in (('form',0,1),('wrap',0,4),('para_spread',.05,8),('back_dark',0,1),
                          ('back_span',.001,10),('dark_side_reach',0,8),('shadow_lo',0,1),('shadow_hi',0,1),
                          ('shadow_tint',0,1),('cel',0,1),('cel_soft',0,1),('rim_width',0,128),
                          ('rim_edge0',0,1),('rim_edge1',0,1),('rim_gain',0,8),('rim_white_mix',0,1),
                          ('line_core_lo',0,1),('line_core_hi',0,1),('delta_gain',.01,100)):
            number(a[key],lo,hi)
        vector(a['shadow_color'],0,1)
        need(a['shadow_lo']<a['shadow_hi'] and a['rim_edge0']<a['rim_edge1'] and a['line_core_lo']<a['line_core_hi'],'Anime thresholds must be strictly ordered')
        need(type(a['cel_steps']) is int and 2<=a['cel_steps']<=8,'Anime cel steps must be int 2..8')
        need(a['view'] in ANIME_VIEWS,'Unknown anime diagnostic view')
        need(a['subject_source'] in ('LIGHT_MASK','DEPTH_VALID','ALPHA'),'Unknown anime subject source')
        need(type(a['solo_light_id']) is str and (not a['solo_light_id'] or a['solo_light_id'] in ids),'Anime Solo requires an existing light id or empty string')
        need(type(a['rim_light_ids']) is list and all(type(i) is str and i in ids for i in a['rim_light_ids']),'Anime rim requires existing light ids')
        need(len(set(a['rim_light_ids']))==len(a['rim_light_ids']),'Anime rim light ids must be unique')
        if 'environment' in config:
            e=config['environment'];exact(e,('subject_tint','subject_exposure','background_tint','background_exposure'))
            vector(e['subject_tint'],0,4);vector(e['background_tint'],0,4)
            number(e['subject_exposure'],-4,4);number(e['background_exposure'],-4,4)
    return config

class Backend:
    def __init__(self,module,device=None):self.xp=module;self.device=device;self.torch=module.__name__=='torch'
    def array(self,v):return self.xp.tensor(v,dtype=self.xp.float32,device=self.device) if self.torch else self.xp.asarray(v,dtype=self.xp.float32)
    def zeros(self,shape):return self.xp.zeros(shape,dtype=self.xp.float32,device=self.device) if self.torch else self.xp.zeros(shape,dtype=self.xp.float32)
    def copy(self,v):return v.clone() if self.torch else v.copy()
    def sum(self,v):return self.xp.sum(v,dim=-1) if self.torch else self.xp.sum(v,axis=-1)
    def stack(self,v):return self.xp.stack(v,dim=-1) if self.torch else self.xp.stack(v,axis=-1)
    def clip(self,v,lo,hi):return self.xp.clamp(v,lo,hi) if self.torch else self.xp.clip(v,lo,hi)
    def norm(self,v):return v/self.xp.sqrt(self.clip(self.sum(v*v),1e-12,1e30))[...,None]
    def finite(self,v):return bool(self.xp.isfinite(v).all())
    def all(self,v):return bool(v.all())

def geometry(scalar,frame,b):
    h,w=scalar.shape
    x=b.array([i+.5 for i in range(w)])[None,:]+b.zeros((h,1))
    y=b.array([i+.5 for i in range(h)])[:,None]+b.zeros((1,w))
    if frame['kind']=='screen_heightfield':
        p=b.stack(((x-w/2)/h,(h/2-y)/h,scalar*frame['height_scale']))
        v=b.zeros((h,w,3));v[...,2]=1
    else:
        p=b.stack(((x-frame['cx'])*scalar/frame['fx'],-(y-frame['cy'])*scalar/frame['fy'],-scalar))
        v=b.norm(-p)
    return p,v

def unit(v):
    length=math.sqrt(sum(x*x for x in v));return [x/length for x in v]

def basis(direction):
    f=unit(direction);up=[0,1,0] if abs(f[1])<.99 else [1,0,0]
    cross=lambda a,b:[a[1]*b[2]-a[2]*b[1],a[2]*b[0]-a[0]*b[2],a[0]*b[1]-a[1]*b[0]]
    r=unit(cross(f,up));return f,r,cross(r,f)

def samples(light):
    n=light['samples'];typ=light['type']
    if typ=='SUN':
        f,r,u=basis(light['direction']);out=[]
        for i in range(n):
            if n==1:out.append([-x for x in f]);continue
            angle=(i+.5)*2.399963229728653
            cos_t=1-(i+.5)/n*(1-math.cos(math.radians(light['angle_deg']/2)));sin_t=math.sqrt(max(0,1-cos_t*cos_t))
            out.append([-f[j]*cos_t+sin_t*(r[j]*math.cos(angle)+u[j]*math.sin(angle)) for j in range(3)])
        return out
    center=light['position']
    if n==1:return [center[:]]
    if typ in ('POINT','SPOT'):
        out=[]
        for i in range(n):
            z=1-2*(i+.5)/n;rad=math.sqrt(max(0,1-z*z));a=(i+.5)*2.399963229728653
            out.append([center[0]+light['radius']*rad*math.cos(a),center[1]+light['radius']*rad*math.sin(a),center[2]+light['radius']*z])
        return out
    f,r,u=basis([a-c for a,c in zip(light['aim'],center)]);out=[]
    # Equal-weight deterministic rectangle lattice or disk/ellipse spiral.
    cols=2**int(math.ceil(math.log2(n)/2));rows=n//cols
    for i in range(n):
        if light['shape'] in ('DISK','ELLIPSE'):
            radius=math.sqrt((i+.5)/n);a=(i+.5)*2.399963229728653
            x=radius*math.cos(a)*light['size']/2;y=radius*math.sin(a)*light['size_y']/2
        else:
            x=((i%cols+.5)/cols-.5)*light['size'];y=((i//cols+.5)/rows-.5)*light['size_y']
        out.append([center[j]+r[j]*x+u[j]*y for j in range(3)])
    return out

def visibility(p,l,reach,scalar,valid,config,b):
    q=config['quality'];steps=q['shadow_steps'];h,w=scalar.shape
    visibility=b.zeros((h,w))+1
    if steps==0:return visibility
    # Evaluate the same bounded samples in one batch, avoiding dozens of tiny
    # GPU launches per lamp. Nearest-depth sampling and all predicates are unchanged.
    fractions=b.array([step/(steps+1) for step in range(1,steps+1)])
    ray=p[None,...]+l[None,...]*(fractions[:,None,None]*reach)[...,None]
    if config['frame']['kind']=='screen_heightfield':
        px=ray[...,0]*h+w/2;py=h/2-ray[...,1]*h
        compare=ray[...,2];surface=scalar*config['frame']['height_scale'];front=True
    else:
        f=config['frame'];z=-ray[...,2];safe=b.clip(z,1e-8,1e30)
        px=f['fx']*ray[...,0]/safe+f['cx'];py=f['cy']-f['fy']*ray[...,1]/safe
        compare=z;surface=scalar;front=z>0
    inside=(px>=0)&(px<w)&(py>=0)&(py<h)&front
    ix=b.clip(b.xp.floor(px),0,w-1);iy=b.clip(b.xp.floor(py),0,h-1)
    ix=ix.to(dtype=b.xp.int64) if b.torch else ix.astype('int64')
    iy=iy.to(dtype=b.xp.int64) if b.torch else iy.astype('int64')
    sampled=surface[iy,ix];eligible=inside&valid[iy,ix]
    blocked=eligible & ((compare<sampled-q['shadow_bias']) if config['frame']['kind']=='screen_heightfield' else (compare>sampled+q['shadow_bias']))
    blocked_any=blocked.any(dim=0) if b.torch else blocked.any(axis=0)
    return visibility*(~blocked_any)

def receiver_geometry(scalar,frame,projection,b):
    """Camera rays intersect one plane; return points and intersection validity.

    Inactive invalid rays are zeroed only to keep bounded array math finite.
    The caller rejects every invalid ray in the actual receiver/Alpha domain.
    """
    h,w=scalar.shape
    x=b.array([i+.5 for i in range(w)])[None,:]+b.zeros((h,1))
    y=b.array([i+.5 for i in range(h)])[:,None]+b.zeros((1,w))
    # Magnitude does not affect a plane; hypot also handles tiny JSON normals
    # without imposing the existing lamp-direction magnitude threshold.
    length=math.hypot(*projection['normal'])
    plane_normal=b.array([x/length for x in projection['normal']]);point=b.array(projection['point'])
    if frame['kind']=='screen_heightfield':
        origin=b.stack(((x-w/2)/h,(h/2-y)/h,b.zeros((h,w))))
        ray=b.zeros((h,w,3));ray[...,2]=1
    else:
        origin=b.zeros((h,w,3))
        ray=b.stack(((x-frame['cx'])/frame['fx'],(frame['cy']-y)/frame['fy'],-(b.zeros((h,w))+1)))
    denominator=b.sum(ray*plane_normal);numerator=b.sum((point-origin)*plane_normal)
    valid=b.xp.isfinite(denominator)&(denominator!=0)
    safe=b.xp.where(valid,denominator,b.zeros((h,w))+1)
    t=numerator/safe
    points=origin+ray*t[...,None]
    finite_points=b.xp.isfinite(points).all(dim=-1) if b.torch else b.xp.isfinite(points).all(axis=-1)
    valid=valid&b.xp.isfinite(t)&finite_points
    if frame['kind']=='metric_perspective':valid=valid&(t>0)
    points=b.xp.where(valid[...,None],points,b.zeros((h,w,3)))
    return points,plane_normal,valid


def projection_visibility(p,l,reach,scalar,valid,caster,config,b):
    """Nearest depth with continuous coverage, never repeated opacity products."""
    q=config['quality'];steps=q['shadow_steps'];h,w=scalar.shape
    vis=b.zeros((h,w))+1
    if steps==0:return vis
    surface=scalar*config['frame']['height_scale'] if config['frame']['kind']=='screen_heightfield' else scalar
    for step in range(1,steps+1):
        ray=p+l*(reach*(step/(steps+1)))[...,None]
        if config['frame']['kind']=='screen_heightfield':
            px=ray[...,0]*h+w/2;py=h/2-ray[...,1]*h;compare=ray[...,2];front=True
        else:
            f=config['frame'];z=-ray[...,2];safe=b.clip(z,1e-8,1e30)
            px=f['fx']*ray[...,0]/safe+f['cx'];py=f['cy']-f['fy']*ray[...,1]/safe;compare=z;front=z>0
        inside=(px>=0)&(px<w)&(py>=0)&(py<h)&front
        ix=b.clip(b.xp.floor(px),0,w-1);iy=b.clip(b.xp.floor(py),0,h-1)
        ix=ix.to(dtype=b.xp.int64) if b.torch else ix.astype('int64')
        iy=iy.to(dtype=b.xp.int64) if b.torch else iy.astype('int64')
        sampled=surface[iy,ix];eligible=inside&valid[iy,ix]
        blocked=eligible & ((compare<sampled-q['shadow_bias']) if config['frame']['kind']=='screen_heightfield' else (compare>sampled+q['shadow_bias']))
        coverage=caster[iy,ix]*blocked
        vis=b.xp.minimum(vis,1-coverage)
    return vis


def projection_sample(p,plane_normal,light,source,config,b):
    """Use the light's original sample and emission/beam, plus receiver facing."""
    h,w=p.shape[:2]
    if light['type']=='SUN':
        l=b.zeros((h,w,3))+b.array(source);distance=b.zeros((h,w))+config['quality']['sun_reach']
    else:
        delta=b.array(source)-p;distance=b.xp.sqrt(b.clip(b.sum(delta*delta),1e-12,1e30));l=delta/distance[...,None]
    # Controlled shadow opacity uses bounded angular coverage. Inverse-square
    # radiance belongs to supplemental light, not to a shadow's opacity.
    gate=b.clip(b.sum(l*plane_normal),0,1)
    if light['type'] in ('SPOT','AREA'):
        direction=b.array(unit([a-p0 for a,p0 in zip(light['aim'],light['position'])]))
        cosine=b.sum(-l*direction)
        if light['type']=='SPOT':
            outer=math.cos(math.radians(light['cone_deg']/2));inner=math.cos(math.radians(light['cone_deg']/2*(1-light['blend'])))
            if light['blend']==0:beam=(cosine>=outer)
            else:
                t=b.clip((cosine-outer)/(inner-outer),0,1);beam=t*t*(3-2*t)
            gate=gate*beam
        else:gate=gate*b.clip(cosine,0,1)
    return l,distance,gate


def base_tone(rgba,light_mask,line_mask,normal_valid,config,b):
    """Controlled 2D base grade; not albedo recovery or old-light inversion."""
    source=rgba[...,:3];base=config['base'];weights=b.array([.2126,.7152,.0722])
    if base['exposure']==0 and base['contrast']==1 and base['saturation']==1:
        return source,b.zeros(source.shape)
    exposed=source*(2**base['exposure']);luma=b.sum(exposed*weights)
    safe=b.clip(luma,1e-8,1e30);pivot=.18
    contrasted=pivot*(safe/pivot)**base['contrast']
    ratio=b.xp.where(luma>1e-8,contrasted/safe,b.zeros(luma.shape)+1)
    graded=exposed*ratio[...,None];gray=b.sum(graded*weights)[...,None]
    graded=gray+(graded-gray)*base['saturation']
    region=b.zeros(luma.shape)+1 if base['mask']=='ALL' else light_mask
    protected=region*normal_valid*(rgba[...,3]>0)*(1-config['line_protect']*line_mask)
    delta=(graded-source)*protected[...,None]*config['mix']
    return source+delta,delta


def _float(v,b):
    return v.to(dtype=b.xp.float32) if b.torch else v.astype(b.xp.float32)


def _smoothstep(lo,hi,x,b):
    t=b.clip((x-lo)/(hi-lo),0,1)
    return t*t*(3-2*t)


def _cel_quant(x,steps,soft,b):
    hard=b.clip(b.xp.floor(x*steps)/(steps-1),0,1)
    return hard*(1-soft)+x*soft


def _indices(n,b):
    return b.xp.arange(n,device=b.device,dtype=b.xp.int64) if b.torch else b.xp.arange(n,dtype=b.xp.int64)


def _shift(v,dx,dy,b):
    h,w=v.shape[:2]
    x=b.clip(_indices(w,b)+dx,0,w-1);y=b.clip(_indices(h,b)+dy,0,h-1)
    return v[y[:,None],x[None,:]]


def _box_blur(v,radius,b):
    if radius<=0:return v
    out=b.zeros(v.shape)
    for offset in range(-radius,radius+1):out=out+_shift(v,offset,0,b)
    out=out/(2*radius+1);result=b.zeros(v.shape)
    for offset in range(-radius,radius+1):result=result+_shift(out,0,offset,b)
    return result/(2*radius+1)


def _as_index(v,b):
    return v.to(dtype=b.xp.int64) if b.torch else v.astype('int64')


def _sample_bilinear(v,x,y,b):
    h,w=v.shape[:2]
    inside=(x>=0)&(x<=w-1)&(y>=0)&(y<=h-1)
    x0=b.xp.floor(x);y0=b.xp.floor(y);wx=x-x0;wy=y-y0
    ix0=_as_index(b.clip(x0,0,w-1),b);iy0=_as_index(b.clip(y0,0,h-1),b)
    ix1=_as_index(b.clip(x0+1,0,w-1),b);iy1=_as_index(b.clip(y0+1,0,h-1),b)
    top=v[iy0,ix0]*(1-wx)+v[iy0,ix1]*wx
    bottom=v[iy1,ix0]*(1-wx)+v[iy1,ix1]*wx
    return top*(1-wy)+bottom*wy,inside


def _sample_nearest(v,x,y,b):
    h,w=v.shape[:2]
    inside=(x>=0)&(x<=w-1)&(y>=0)&(y<=h-1)
    ix=_as_index(b.clip(b.xp.floor(x+.5),0,w-1),b);iy=_as_index(b.clip(b.xp.floor(y+.5),0,h-1),b)
    return v[iy,ix],inside


def _environment_tint(rgb,tint,exposure,b):
    """Luma-preserving artistic tint with an independent exposure target."""
    weights=b.array([.2126,.7152,.0722]);tinted=rgb*b.array(tint)
    source_luma=b.sum(rgb*weights);tinted_luma=b.sum(tinted*weights)
    target=source_luma*(2**exposure)
    scale=target/b.clip(tinted_luma,1e-8,1e30)
    return tinted*scale[...,None]


def _environment(rgba,light_mask,config,b):
    """Optional subject/background environment look; neutral when absent."""
    source=rgba[...,:3]
    if 'environment' not in config:return rgba,b.zeros(source.shape)
    e=config['environment']
    subject=_environment_tint(source,e['subject_tint'],e['subject_exposure'],b)
    background=_environment_tint(source,e['background_tint'],e['background_exposure'],b)
    graded=subject*light_mask[...,None]+background*(1-light_mask[...,None])
    alpha=rgba[...,3]>0
    out=b.copy(rgba);out[...,:3]=b.xp.where(alpha[...,None],graded,source)
    delta=b.xp.where(alpha[...,None],out[...,:3]-source,b.zeros(source.shape))
    return out,delta


def _anime_base(rgba,light_mask,config,b):
    """Independent 2D grade; unlike legacy 0.3 it never requires Normal-validity."""
    source=rgba[...,:3];base=config['base'];weights=b.array([.2126,.7152,.0722])
    if base['exposure']==0 and base['contrast']==1 and base['saturation']==1:
        return source,b.zeros(source.shape)
    exposed=source*(2**base['exposure']);luma=b.sum(exposed*weights)
    safe=b.clip(luma,1e-8,1e30);pivot=.18
    contrasted=pivot*(safe/pivot)**base['contrast']
    ratio=b.xp.where(luma>1e-8,contrasted/safe,b.zeros(luma.shape)+1)
    graded=exposed*ratio[...,None];gray=b.sum(graded*weights)[...,None]
    graded=gray+(graded-gray)*base['saturation']
    region=b.zeros(luma.shape)+1 if base['mask']=='ALL' else light_mask
    delta=(graded-source)*(region*(rgba[...,3]>0))[...,None]*config['mix']
    return source+delta,delta


def _anime_rim(scalar,depth_valid,subject,light,config,b):
    """Six depth taps toward the screen light, then blur and clip to the raw inner band."""
    h,w=scalar.shape;a=config['anime'];width=a['rim_width']
    if width<=0 or light['id'] not in a['rim_light_ids']:
        zero=b.zeros((h,w));return zero,zero
    x=b.array([i+.5 for i in range(w)])[None,:]+b.zeros((h,1))
    y=b.array([i+.5 for i in range(h)])[:,None]+b.zeros((1,w))
    center=light.get('position',light.get('anchor'))
    lx=center[0]*h+w/2;ly=h/2-center[1]*h
    dx=lx-x;dy=ly-y;length=b.xp.sqrt(dx*dx+dy*dy);safe=b.clip(length,1e-4,1e30)
    ux=dx/safe;uy=dy/safe;band=b.zeros((h,w))
    for i in range(6):
        t=width*(i+.5)/6;sx=x+ux*t;sy=y+uy*t
        sampled,inside=_sample_bilinear(scalar,sx,sy,b)
        valid,_=_sample_nearest(depth_valid,sx,sy,b);valid=valid&inside
        matte,_=_sample_nearest(subject,sx,sy,b)
        # Invalid depth outside a verified matte may be far background; invalid
        # depth inside the subject contributes nothing instead of invented geometry.
        sample_ok=inside&(valid|(matte<.5))
        sampled=b.xp.where(valid,sampled,b.zeros((h,w)))
        band=band+b.xp.where(sample_ok,_smoothstep(a['rim_edge0'],a['rim_edge1'],scalar-sampled,b),b.zeros((h,w)))
    raw=band/6*subject*_float(depth_valid,b)*_float(length>1e-4,b)
    blurred=_box_blur(raw,max(1,int(round(width*.3))),b)
    rim=blurred*_smoothstep(0,.1,raw,b)
    return raw,rim


def _render_anime(rgba,scalar,normal,depth_valid,normal_valid,light_mask,highlight_mask,line_mask,c,b,local,roughness,diagnostics):
    h,w=scalar.shape;a=c['anime'];source=rgba[...,:3];alpha=rgba[...,3]>0
    working,environment_delta=_environment(rgba,light_mask,c,b);working_source=working[...,:3]
    active_alpha=_float(alpha,b);region=light_mask*active_alpha
    if a['subject_source']=='LIGHT_MASK':subject=light_mask
    elif a['subject_source']=='DEPTH_VALID':subject=_float(depth_valid,b)
    else:subject=active_alpha
    base_rgb,base_delta=_anime_base(working,light_mask,c,b)
    p,v=geometry(scalar,c['frame'],b);ndv=b.clip(b.sum(normal*v),0,1)
    f0=b.array(c['material']['f0']);fresnel=f0+(1-f0)*(1-ndv[...,None])**5
    exponent=2/(roughness**2)-2;form=a['form']*_float(normal_valid,b)
    tint_max=b.zeros((h,w));any_light=b.zeros((h,w));light_sum=b.zeros((h,w,3))
    para_view=b.zeros((h,w));form_view=b.zeros((h,w));rim_view=b.zeros((h,w));rim_peak=b.zeros((h,w))
    parts=[]
    for light in c['lights']:
        if light['intensity']==0 or (a['solo_light_id'] and light['id']!=a['solo_light_id']):continue
        para_avg=b.zeros((h,w));form_avg=b.zeros((h,w));amount_avg=b.zeros((h,w));tint_avg=b.zeros((h,w))
        spec_avg=b.zeros((h,w));shadow_avg=b.zeros((h,w))
        for source_point in samples(light):
            if light['type']=='SUN':
                l=b.zeros((h,w,3))+b.array(source_point);distance=b.zeros((h,w))+c['quality']['sun_reach'];gate=b.zeros((h,w))+1
            else:
                delta=b.array(source_point)-p;distance=b.xp.sqrt(b.clip(b.sum(delta*delta),1e-12,1e30));l=delta/distance[...,None];gate=b.zeros((h,w))+1
            if light['type'] in ('SPOT','AREA'):
                direction=b.array(unit([x-y for x,y in zip(light['aim'],light['position'])]))
                cosine=b.sum(-l*direction)
                if light['type']=='SPOT':
                    outer=math.cos(math.radians(light['cone_deg']/2));inner=math.cos(math.radians(light['cone_deg']/2*(1-light['blend'])))
                    if light['blend']==0:gate=cosine>=outer
                    else:
                        t=b.clip((cosine-outer)/(inner-outer),0,1);gate=t*t*(3-2*t)
                else:gate=b.clip(cosine,0,1)
            gate=_float(gate,b)
            falloff=gate/(1+(distance/max(light['reference_distance'],.000001))**2)
            center=light.get('position',light.get('anchor'))
            plx=center[0]-p[...,0];ply=center[1]-p[...,1]
            screen_distance=b.xp.sqrt(plx*plx+ply*ply)/max(a['para_spread']*light['reference_distance'],.05)
            back_side=b.clip((p[...,2]-center[2])/a['back_span'],0,1)*_float(depth_valid,b)
            para=(1/(1+screen_distance*screen_distance))*(1-a['back_dark']*back_side)*gate
            wrapped=b.clip((b.sum(normal*l)+a['wrap'])/(1+a['wrap']),0,1)
            shadow=visibility(p,l,distance,scalar,depth_valid,c,b)
            form_key=wrapped*shadow*b.clip(falloff*a['dark_side_reach'],0,1)
            key=(1-form)*para+form*form_key
            amount=(1-form)*para+form*(wrapped*wrapped*shadow*falloff)
            if a['cel']>0:
                quant=_cel_quant(key,a['cel_steps'],a['cel_soft'],b)
                amount=(1-a['cel'])*amount+a['cel']*((1-form)*quant+form*quant*falloff)
                key=(1-a['cel'])*key+a['cel']*quant
            para_tint=(1-a['cel'])*para+a['cel']*_cel_quant(para,a['cel_steps'],a['cel_soft'],b) if a['cel']>0 else para
            tint=(1-form)*para_tint+form*key
            count=light['samples'];para_avg=para_avg+para/count;form_avg=form_avg+form_key/count
            amount_avg=amount_avg+amount/count;tint_avg=tint_avg+tint/count;shadow_avg=shadow_avg+shadow/count
            cosine=b.clip(b.sum(normal*l),0,1);half=b.norm(l+v);ndh=b.clip(b.sum(normal*half),0,1)
            spec_avg=spec_avg+(ndh**exponent*(cosine>0)*(shadow*falloff*light['intensity']*light['specular']/count))
        color=b.array(light['color']);presence=min(1.,max(0.,light['intensity']/.5))
        tint_max=b.xp.maximum(tint_max,tint_avg*presence);any_light=b.xp.maximum(any_light,b.zeros((h,w))+presence)
        diffuse=base_rgb*(amount_avg*light['diffuse']*light['intensity']*region)[...,None]*color
        spec=spec_avg[...,None]*fresnel*color*(highlight_mask*region*form)[...,None]
        rim_raw,rim=_anime_rim(scalar,depth_valid,subject,light,c,b)
        rim_amount=rim*light['intensity']*a['rim_gain']*region
        rim_rgb=(working_source*(1-a['rim_white_mix'])+a['rim_white_mix'])*color*rim_amount[...,None]
        delta=(diffuse+spec+rim_rgb)*c['mix'];light_sum=light_sum+delta
        parts.append({'id':light['id'],'diffuse':diffuse*c['mix'],'specular':spec*c['mix'],
                      'visibility':shadow_avg,'delta':delta})
        rim_peak=b.xp.maximum(rim_peak,rim_amount)
        if diagnostics is not None:
            para_view=b.xp.maximum(para_view,para_avg);form_view=b.xp.maximum(form_view,form_avg)
            rim_view=b.xp.maximum(rim_view,rim*region)
    dark=(1-_smoothstep(a['shadow_lo'],a['shadow_hi'],tint_max,b))*any_light*a['shadow_tint']*region*c['mix']
    encoded=b.array(a['shadow_color']);shadow_color=b.xp.where(encoded<=.04045,encoded/12.92,((encoded+.055)/1.055)**2.4)
    shaded=base_rgb*((1-dark)[...,None]+dark[...,None]*shadow_color)
    lit=shaded+light_sum
    rim_here=b.clip(rim_peak*10,0,1)
    line_core=_smoothstep(a['line_core_lo'],a['line_core_hi'],line_mask,b)
    line_eff=(line_mask*(1-rim_here)+line_core*rim_here)*c['line_protect']*active_alpha*c['mix']
    final=lit*(1-line_eff[...,None])+working_source*line_eff[...,None]
    total=final-source;global_delta=total-light_sum
    out=b.copy(rgba)
    out[...,:3]=b.xp.where(alpha[...,None],final,source)
    need(b.finite(out) and b.finite(total),'Anime output overflow; no clamping/cleaning')
    if diagnostics is not None:
        diagnostics.update(para=para_view,form=form_view,dark_side=dark,rim=rim_view,line=line_eff,
                           global_delta=global_delta,delta=total,depth=scalar,normal=normal,subject=subject,
                           depth_valid=_float(depth_valid,b),normal_valid=_float(normal_valid,b),base_delta=base_delta,
                           environment_delta=environment_delta)
    return out,total,parts


def render(rgba,scalar,normal,depth_valid,normal_valid,light_mask,highlight_mask,line_mask,config,b,*,local=None,diagnostics=None):
    c=normalize(config);h,w=scalar.shape
    need(4<=h<=2048 and 4<=w<=2048,'Explicit domain4..2048')
    need(tuple(rgba.shape)==(h,w,4) and tuple(normal.shape)==(h,w,3),'Array domain mismatch')
    for a in (rgba,scalar,normal,light_mask,highlight_mask,line_mask):
        need(str(a.dtype) in ('float32','torch.float32') and b.finite(a),'Finite float32 required without cast')
    for a in (depth_valid,normal_valid,light_mask,highlight_mask,line_mask):need(tuple(a.shape)==(h,w),'Mask domain mismatch')
    for a in (depth_valid,normal_valid):need(str(a.dtype) in ('bool','torch.bool'),'Boolean validity required')
    need(b.all((~normal_valid)|depth_valid),'Normal-validity must be a subset of Depth-validity')
    for a in (light_mask,highlight_mask,line_mask):need(b.all((a>=0)&(a<=1)),'Mask0..1')
    projection=c.get('projection');project=bool(projection and projection['enabled'])
    if local is not None:
        exact(local,('rough_mul','rough_add','receiver','caster'))
        for a in local.values():
            need(hasattr(a,'shape') and hasattr(a,'dtype'),'Local arrays required without scalar/string cast')
            need(tuple(a.shape)==(h,w) and str(a.dtype) in ('float32','torch.float32') and b.finite(a),'Local finite float32 HW required without cast')
            need(b.all((a>=0)&(a<=1)),'Local maps0..1')
        roughness=c['material']['roughness']*local['rough_mul']+local['rough_add']
        # Ordered float32 convex maps can round the endpoints by one ulp.
        need(b.all((roughness>=.02-2e-7)&(roughness<=1+2e-7)),'Effective roughness .02..1 required')
        roughness=b.clip(roughness,.02,1)
        if b.all(local['rough_mul']==1) and b.all(local['rough_add']==0):roughness=c['material']['roughness']
    else:
        need(not project,'Enabled projection requires explicit local maps')
        roughness=c['material']['roughness']
    need(b.all((rgba[...,3]>=0)&(rgba[...,3]<=1)),'Straight Alpha0..1')
    need(b.all(abs(b.sum(normal[normal_valid]*normal[normal_valid])-1)<=2e-5),'Unit normal required in valid domain')
    if c['frame']['kind']=='metric_perspective':need(b.all(scalar[depth_valid]>0),'Positive axial Z required')
    else:need(b.all((scalar[depth_valid]>=0)&(scalar[depth_valid]<=1)),'near-one proxy0..1 required')
    work=h*w*sum(l['samples'] for l in c['lights'])*max(1,c['quality']['shadow_steps'])
    selected=[]
    if project:
        selected=[l for l in c['lights'] if l['id'] in projection['light_ids'] and l['intensity']>0]
        if not c['bypass'] and c['mix']!=0:
            work+=h*w*sum(l['samples'] for l in selected)*max(1,c['quality']['shadow_steps'])
    need(work<=c['quality']['max_work'],'Explicit operation budget exceeded')
    out=b.copy(rgba);total=b.zeros((h,w,3));contributions=[]
    if c['bypass'] or c['mix']==0:return out,total,contributions
    if c['version']==ANIME_VERSION:
        return _render_anime(rgba,scalar,normal,depth_valid,normal_valid,light_mask,highlight_mask,line_mask,c,b,local,roughness,diagnostics)
    lit_rgb=rgba[...,:3]
    if c['version']==VERSION:
        lit_rgb,base_delta=base_tone(rgba,light_mask,line_mask,normal_valid,c,b)
        total=total+base_delta
    p,v=geometry(scalar,c['frame'],b)
    plane=None;plane_normal=None;receiver_active=None;caster=None
    sum_intensity=sum(l['intensity'] for l in selected)
    if project:
        receiver_active=(local['receiver']>0)&(rgba[...,3]>0)
        caster=local['caster']*(1-local['receiver'])
        if sum_intensity>0 and c['quality']['shadow_steps']>0 and not b.all(~receiver_active):
            plane,plane_normal,plane_valid=receiver_geometry(scalar,c['frame'],projection,b)
            if not b.all((~receiver_active)|plane_valid):
                raise ProjectionError('Invalid receiver-plane intersection: parallel/nonfinite or nonpositive metric t')
    protected=(1-c['line_protect']*line_mask)*normal_valid*(rgba[...,3]>0)
    ndv=b.clip(b.sum(normal*v),0,1);f0=b.array(c['material']['f0'])
    fresnel=f0+(1-f0)*(1-ndv[...,None])**5
    exponent=2/(roughness**2)-2
    for light in c['lights']:
        if light['intensity']==0:continue
        diff=b.zeros((h,w));spec=b.zeros((h,w,3));shadow_avg=b.zeros((h,w))
        projected_avg=b.zeros((h,w)) if project else None
        direction=None
        if light['type'] in ('SPOT','AREA'):direction=b.array(unit([a-p0 for a,p0 in zip(light['aim'],light['position'])]))
        for source in samples(light):
            if light['type']=='SUN':
                l=b.zeros((h,w,3))+b.array(source);distance=b.zeros((h,w))+c['quality']['sun_reach'];gain=b.zeros((h,w))+1
            else:
                delta=b.array(source)-p;distance=b.xp.sqrt(b.clip(b.sum(delta*delta),1e-12,1e30));l=delta/distance[...,None]
                gain=light['reference_distance']**2/(distance*distance+light['softening']**2)
            if light['type']=='SPOT':
                cos=b.sum(-l*direction);outer=math.cos(math.radians(light['cone_deg']/2));inner=math.cos(math.radians(light['cone_deg']/2*(1-light['blend'])))
                if light['blend']==0:beam=(cos>=outer)
                else:
                    t=b.clip((cos-outer)/(inner-outer),0,1);beam=t*t*(3-2*t)
                gain=gain*beam
            elif light['type']=='AREA':gain=gain*b.clip(b.sum(-l*direction),0,1)
            shadow=visibility(p,l,distance,scalar,depth_valid,c,b);shadow_avg=shadow_avg+shadow/light['samples']
            if project:
                projected=b.zeros((h,w))+1
                if plane is not None and light['id'] in projection['light_ids']:
                    pl,reach,gate=projection_sample(plane,plane_normal,light,source,c,b)
                    traced=projection_visibility(plane,pl,reach,scalar,depth_valid,caster,c,b)
                    projected=1-gate*receiver_active*(1-traced)
                projected_avg=projected_avg+projected/light['samples']
            cosine=b.clip(b.sum(normal*l),0,1);weight=gain*shadow*light['intensity']/light['samples']
            diff=diff+cosine*weight*light['diffuse']
            half=b.norm(l+v);ndh=b.clip(b.sum(normal*half),0,1)
            spec=spec+(ndh**exponent*(cosine>0)*weight*light['specular'])[...,None]*fresnel
        color=b.array(light['color'])
        d=(lit_rgb*(diff*light_mask*protected)[...,None])*color
        s=spec*(highlight_mask*protected)[...,None]*color
        delta=(d+s)*c['mix']
        part={'id':light['id'],'diffuse':d*c['mix'],'specular':s*c['mix'],'visibility':shadow_avg,'delta':delta}
        if project:
            projected_delta=b.zeros((h,w,3))
            if sum_intensity>0 and light['id'] in projection['light_ids']:
                weight=projection['strength']*local['receiver']*(1-c['line_protect']*line_mask)*(rgba[...,3]>0)
                weight=weight*(light['intensity']/sum_intensity)*(1-projected_avg)*c['mix']
                projected_delta=-rgba[...,:3]*weight[...,None]*(1-b.array(projection['transmission']))
            part.update(projection_visibility=projected_avg,projection_delta=projected_delta)
            part['delta']=delta+projected_delta
        total=total+part['delta']
        contributions.append(part)
    # Hidden RGB is an immutable payload: even adding zero may flip -0 to +0.
    # Compute the lit result only as the selected value for positive Alpha.
    out[...,:3]=b.xp.where((rgba[...,3]>0)[...,None],out[...,:3]+total,rgba[...,:3])
    need(b.finite(out) and b.finite(total),'Output overflow; no clamping/cleaning')
    return out,total,contributions
