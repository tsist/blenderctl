"""Native, parameter-driven Geometry Nodes blueprints (metres and radians)."""
from copy import deepcopy
from math import tau

_DEFAULTS = {
    'spiral_stair': {'steps':16,'height':3.0,'radius':1.0,'turn_angle':tau,
                     'column_radius':0.12,'tread_thickness':0.055,
                     'rail_height':0.9,'rail_radius':0.025,'post_radius':0.018},
    'curve_fence': {'length':12.0,'amplitude':0.8,'spacing':1.0,'height':1.0,
                    'post_radius':0.035,'rail_radius':0.025,
                    'path_object':None,'use_external_path':False},
    'surface_scatter': {'size':10.0,'relief':1.2,'density':3.0,'seed':17,
                        'scale':1.0,'max_slope':0.65},
}

def recipe_defaults(name):
    if name not in _DEFAULTS:
        raise ValueError('Unknown geometry recipe: '+str(name))
    return deepcopy(_DEFAULTS[name])

class _Graph:
    def __init__(self,name,params):
        self.data={'id':name,'interface':[], 'nodes':[], 'links':[]}
        self.parameters=params
        self.socket('geometry','Geometry','OUTPUT','NodeSocketGeometry')
        self.socket('source','Geometry','INPUT','NodeSocketGeometry')
        self.node('in','NodeGroupInput');self.node('out','NodeGroupOutput')
    def socket(self,key,name,direction,typ,default=None,minimum=None,maximum=None,unit=None):
        s={'key':key,'name':name,'direction':direction,'type':typ}
        if default is not None:s['default']=default
        if minimum is not None:s['min']=minimum
        if maximum is not None:s['max']=maximum
        if unit:s['unit']=unit
        s['semantic']=key
        self.data['interface'].append(s)
    def parameter(self,key,minimum=0.001,maximum=100.0,unit='m'):
        val=self.parameters[key]
        typ='NodeSocketInt' if isinstance(val,int) else 'NodeSocketFloat'
        self.socket(key,key,'INPUT',typ,val,minimum,maximum,unit)
        return ('in',key)
    def node(self,id,typ,inputs=None,properties=None):
        n={'id':id,'type':typ,'location':[len(self.data['nodes'])%8*210,-len(self.data['nodes'])//8*180]}
        if properties:n['properties']=properties
        self.data['nodes'].append(n)
        for socket,value in (inputs or {}).items():self.value(id,socket,value)
        return id
    def value(self,id,socket,value):
        if isinstance(value,tuple):self.link(value,(id,socket))
        else:
            n=next(n for n in self.data['nodes'] if n['id']==id)
            n.setdefault('inputs',{})[socket]=value
    def link(self,source,target):self.data['links'].append({'from':list(source),'to':list(target)})
    def math(self,id,operation,a,b=None):
        self.node(id,'ShaderNodeMath',{'Value':a}, {'operation':operation})
        if b is not None:self.value(id,'Value_001',b)
        return (id,'Value')
    def xyz(self,id,x=0.0,y=0.0,z=0.0):
        self.node(id,'ShaderNodeCombineXYZ',{'X':x,'Y':y,'Z':z});return(id,'Vector')
    def transform(self,id,geometry,translation=None,rotation=None,scale=None):
        ins={'Geometry':geometry}
        if translation is not None:ins['Translation']=translation
        if rotation is not None:ins['Rotation']=rotation
        if scale is not None:ins['Scale']=scale
        self.node(id,'GeometryNodeTransform',ins);return(id,'Geometry')
    def join(self,id,*geometries):
        self.node(id,'GeometryNodeJoinGeometry')
        for x in geometries:self.link(x,(id,'Geometry'))
        return(id,'Geometry')
    def output(self,geometry):self.link(geometry,('out','Geometry'));return self.data

def _stair(p):
    g=_Graph('spiral_stair',p)
    count=g.parameter('steps',3,256,'count');height=g.parameter('height',0.2,30)
    radius=g.parameter('radius',0.3,10);turn=g.parameter('turn_angle',0.2,12*tau,'rad')
    inner=g.parameter('column_radius',0.03,0.5);thick=g.parameter('tread_thickness',0.01,0.2)
    railheight=g.parameter('rail_height',0.2,2);railradius=g.parameter('rail_radius',0.005,0.1)
    postradius=g.parameter('post_radius',0.005,0.08)
    rise=g.math('rise','DIVIDE',height,count);angle=g.math('angle','DIVIDE',turn,count)
    # Polar mapping of a radial/angular grid gives a genuinely curved annular tread.
    g.node('tread_grid','GeometryNodeMeshGrid',{'Size X':1.0,'Size Y':1.0,'Vertices X':2,'Vertices Y':9})
    g.node('position','GeometryNodeInputPosition');g.node('split','ShaderNodeSeparateXYZ',{'Vector':('position','Position')})
    radial_fraction=g.math('radial_fraction','ADD',('split','X'),0.5)
    width=g.math('radial_width','SUBTRACT',radius,inner)
    r=g.math('r','ADD',g.math('r_scaled','MULTIPLY',radial_fraction,width),inner)
    angular_fraction=g.math('angular_fraction','ADD',('split','Y'),0.5)
    tread_angle=g.math('tread_angle','MULTIPLY',g.math('tread_fraction','MULTIPLY',angle,0.96),angular_fraction)
    x=g.math('tread_x','MULTIPLY',g.math('cos','COSINE',tread_angle),r)
    y=g.math('tread_y','MULTIPLY',g.math('sin','SINE',tread_angle),r)
    g.node('polar','GeometryNodeSetPosition',{'Geometry':('tread_grid','Mesh'),'Position':g.xyz('polar_position',x,y,0.0)})
    offset=g.xyz('thickness_vector',z=g.math('negative_thickness','MULTIPLY',thick,-1.0))
    g.node('extrude','GeometryNodeExtrudeMesh',{'Mesh':('polar','Geometry'),'Offset':offset,'Offset Scale':0.0,'Individual':False},{'mode':'FACES'})
    g.node('repeat_in','GeometryNodeRepeatInput',{'Iterations':count});g.node('repeat_out','GeometryNodeRepeatOutput')
    g.data['repeat_zones']=[{'input':'repeat_in','output':'repeat_out','items':[{'name':'Geometry','type':'GEOMETRY'}]}]
    level=g.math('level','ADD',('repeat_in','Iteration'),1.0)
    z=g.math('level_z','MULTIPLY',level,rise)
    theta=g.math('level_theta','MULTIPLY',('repeat_in','Iteration'),angle)
    tread=g.transform('placed_tread',('extrude','Mesh'),g.xyz('tread_translation',z=z),g.xyz('tread_rotation',z=theta))
    g.node('post','GeometryNodeMeshCylinder',{'Vertices':12,'Radius':postradius,'Depth':railheight},{'fill_type':'NGON'})
    mid=g.math('post_theta','ADD',theta,g.math('half_angle','MULTIPLY',angle,0.48))
    postx=g.math('post_x','MULTIPLY',g.math('post_cos','COSINE',mid),radius)
    posty=g.math('post_y','MULTIPLY',g.math('post_sin','SINE',mid),radius)
    postz=g.math('post_z','ADD',z,g.math('half_rail','MULTIPLY',railheight,0.5))
    post=g.transform('placed_post',('post','Mesh'),g.xyz('post_translation',postx,posty,postz))
    cumulative=g.join('repeat_join',('repeat_in','Geometry'),tread,post)
    g.link(cumulative,('repeat_out','Geometry'))
    columnheight=g.math('column_height','ADD',height,railheight)
    g.node('column','GeometryNodeMeshCylinder',{'Vertices':32,'Radius':inner,'Depth':columnheight},{'fill_type':'NGON'})
    column=g.transform('placed_column',('column','Mesh'),g.xyz('column_translation',z=g.math('column_center','MULTIPLY',columnheight,0.5)))
    # The continuous sweep is tied to the same rise/angle as the discrete treads.
    span=g.math('rail_span','SUBTRACT',height,rise)
    turns=g.math('rail_turns','DIVIDE',g.math('rail_angle','SUBTRACT',turn,angle),tau)
    g.node('spiral','GeometryNodeCurveSpiral',{'Resolution':128,'Rotations':turns,'Start Radius':radius,'End Radius':radius,'Height':span,'Reverse':True})
    curve=g.transform('placed_spiral',('spiral','Curve'),g.xyz('rail_translation',z=g.math('rail_start','ADD',rise,railheight)),g.xyz('rail_rotation',z=g.math('rail_start_angle','MULTIPLY',angle,0.48)))
    g.node('profile','GeometryNodeCurvePrimitiveCircle',{'Resolution':12,'Radius':railradius},{'mode':'RADIUS'})
    g.node('railing','GeometryNodeCurveToMesh',{'Curve':curve,'Profile Curve':('profile','Curve'),'Fill Caps':True})
    return g.output(g.join('final',('repeat_out','Geometry'),column,('railing','Mesh')))

def _fence(p):
    g=_Graph('curve_fence',p)
    length=g.parameter('length',2,100);amp=g.parameter('amplitude',0,10)
    spacing=g.parameter('spacing',0.15,5);height=g.parameter('height',0.2,3)
    postradius=g.parameter('post_radius',0.01,0.2);railradius=g.parameter('rail_radius',0.005,0.15)
    span=g.math('path_span','MULTIPLY',length,0.95)
    half=g.math('half_span','MULTIPLY',span,0.5);minus=g.math('minus_half','MULTIPLY',half,-1.0)
    g.node('line','GeometryNodeCurvePrimitiveLine',{'Start':g.xyz('start',x=minus),'End':g.xyz('end',x=half)},{'mode':'POINTS'})
    g.node('sample','GeometryNodeResampleCurve',{'Curve':('line','Curve'),'Count':129})
    g.node('position','GeometryNodeInputPosition');g.node('split','ShaderNodeSeparateXYZ',{'Vector':('position','Position')})
    phase=g.math('phase','MULTIPLY',g.math('normalized_x','DIVIDE',('split','X'),span),tau)
    y=g.math('bend','MULTIPLY',g.math('sine','SINE',phase),amp)
    g.node('path','GeometryNodeSetPosition',{'Geometry':('sample','Curve'),'Offset':g.xyz('bend_offset',y=y)})
    g.socket('path_object','path_object','INPUT','NodeSocketObject',p['path_object'])
    g.socket('use_external_path','use_external_path','INPUT','NodeSocketBool',p['use_external_path'])
    g.node('external_path','GeometryNodeObjectInfo',{'Object':('in','path_object'),'As Instance':False},{'transform_space':'RELATIVE'})
    g.node('select_path','GeometryNodeSwitch',{'Switch':('in','use_external_path'),'False':('path','Geometry'),'True':('external_path','Geometry')},{'input_type':'GEOMETRY'})
    path=('select_path','Output')
    g.node('points','GeometryNodeCurveToPoints',{'Curve':path,'Length':spacing},{'mode':'LENGTH'})
    g.node('post','GeometryNodeMeshCylinder',{'Vertices':12,'Radius':postradius,'Depth':height},{'fill_type':'NGON'})
    post=g.transform('post_centered',('post','Mesh'),g.xyz('post_center',z=g.math('half_height','MULTIPLY',height,0.5)))
    g.node('posts','GeometryNodeInstanceOnPoints',{'Points':('points','Points'),'Instance':post})
    g.node('profile','GeometryNodeCurvePrimitiveCircle',{'Resolution':12,'Radius':railradius},{'mode':'RADIUS'})
    upper=g.transform('upper_path',path,g.xyz('upper_offset',z=height))
    mid=g.transform('mid_path',path,g.xyz('mid_offset',z=g.math('mid_height','MULTIPLY',height,0.55)))
    paths=g.join('rail_paths',upper,mid)
    g.node('rails','GeometryNodeCurveToMesh',{'Curve':paths,'Profile Curve':('profile','Curve'),'Fill Caps':True})
    return g.output(g.join('final',('posts','Instances'),('rails','Mesh')))

def _scatter(p):
    g=_Graph('surface_scatter',p)
    size=g.parameter('size',1,100);relief=g.parameter('relief',0,5)
    density=g.parameter('density',0,20,'per_m2');seed=g.parameter('seed',0,1000000,'seed')
    scale=g.parameter('scale',0.1,5,'factor');slope=g.parameter('max_slope',0.0,1.55,'rad')
    g.node('grid','GeometryNodeMeshGrid',{'Size X':size,'Size Y':size,'Vertices X':65,'Vertices Y':65})
    g.node('position','GeometryNodeInputPosition')
    g.node('noise','ShaderNodeTexNoise',{'Vector':('position','Position'),'Scale':0.45,'Detail':2.0})
    h=g.math('noise_height','MULTIPLY',g.math('noise_center','SUBTRACT',('noise','Fac'),0.5),relief)
    g.node('terrain','GeometryNodeSetPosition',{'Geometry':('grid','Mesh'),'Offset':g.xyz('relief_offset',z=h)})
    g.node('normal','GeometryNodeInputNormal');g.node('normal_xyz','ShaderNodeSeparateXYZ',{'Vector':('normal','Normal')})
    allowed=g.math('slope_allowed','GREATER_THAN',('normal_xyz','Z'),g.math('slope_cos','COSINE',slope))
    g.node('grass_blade','GeometryNodeMeshCone',{'Vertices':5,'Radius Top':0.0,'Radius Bottom':0.055,'Depth':0.45},{'fill_type':'NGON'})
    blades=[]
    for k,rot in enumerate(([0.0,0.22,0.0],[0.22,-0.16,2.1],[-0.18,-0.12,4.2])):
        blades.append(g.transform('blade_'+str(k),('grass_blade','Mesh'),[0,0,0.22],rot))
    grass=g.join('grass_clump',*blades)
    g.node('rock','GeometryNodeMeshIcoSphere',{'Radius':0.25,'Subdivisions':1})
    rock=g.transform('rock_shape',('rock','Mesh'),[0,0,0.1],None,[1,0.8,0.65])
    g.node('random_size','FunctionNodeRandomValue',{'Min':0.65,'Max':1.35,'Seed':seed},{'data_type':'FLOAT'})
    randscale=g.math('instance_size','MULTIPLY',('random_size','Value'),scale)
    sv=g.xyz('scale_vector',randscale,randscale,randscale)
    outputs=[]
    for kind,source,den,s in [('grass',grass,density,seed),('stone',rock,g.math('stone_density','MULTIPLY',density,0.12),g.math('stone_seed','ADD',seed,73.0))]:
        g.node(kind+'_points','GeometryNodeDistributePointsOnFaces',{'Mesh':('terrain','Geometry'),'Selection':allowed,'Density':den,'Seed':s},{'distribute_method':'RANDOM'})
        g.node(kind+'_instances','GeometryNodeInstanceOnPoints',{'Points':(kind+'_points','Points'),'Instance':source,'Rotation':(kind+'_points','Rotation'),'Scale':sv})
        outputs.append((kind+'_instances','Instances'))
    return g.output(g.join('final',('terrain','Geometry'),*outputs))

def build_recipe(name,parameters=None):
    p=recipe_defaults(name)
    unknown=set(parameters or {})-set(p)
    if unknown:raise ValueError('Unknown recipe parameters: '+', '.join(sorted(unknown)))
    for key,val in (parameters or {}).items():
        if key=='path_object':
            if val is not None and (not isinstance(val,dict) or set(val)!={'kind','name'} or val['kind']!='OBJECT' or not isinstance(val['name'],str)):
                raise ValueError('path_object requires explicit OBJECT resource')
            p[key]=val;continue
        if key=='use_external_path':
            if not isinstance(val,bool):raise ValueError('Boolean required: '+key)
            p[key]=val;continue
        if isinstance(val,bool) or not isinstance(val,(int,float)):
            raise ValueError('Recipe parameter must be numeric: '+key)
        if isinstance(p[key],int) and not isinstance(val,int):raise ValueError('Integer required: '+key)
        p[key]=val
    return {'spiral_stair':_stair,'curve_fence':_fence,'surface_scatter':_scatter}[name](p)
