"""Explicit batch drafts; single-material edits are committed only by button."""
import copy, json, uuid
from pathlib import Path
import bpy, bmesh
from bpy.props import BoolProperty, StringProperty, IntProperty, FloatProperty, FloatVectorProperty, EnumProperty
from . import ui, batch, core
from .batch_contract import normalize_manifest_batch, assignment_manifest, topological_order

def _raw(scene):
    if not scene.mw_batch_json: raise ValueError('请先载入批量清单或从单材质草稿添加分配')
    return json.loads(scene.mw_batch_json)

def _store(scene, value):
    scene.mw_batch_json=json.dumps(normalize_manifest_batch(value),ensure_ascii=False)

def get_manifest(scene, flush=True):
    # No implicit single-material draft commit, even when called by handoff.
    return normalize_manifest_batch(_raw(scene))

def _context(context):
    return {'scene':context.scene.name,'view_layer':context.view_layer.name,'frame':context.scene.frame_current}

def validate_context(context, manifest):
    if manifest['context']!=_context(context): raise ValueError('批量上下文不同；请显式点击读取当前上下文')

def _selected(scene, manifest):
    i=scene.mw_batch_index
    if i<0 or i>=len(manifest['assignments']): raise ValueError('请选择有效分配')
    return manifest['assignments'][i]

def _load_target(scene):
    row=_selected(scene,_raw(scene))
    scene.mw_batch_object=row['target']['object']; scene.mw_batch_slot=row['target']['material_slot']; scene.mw_batch_uv=row['target']['uv_layer']
    scene.mw_batch_faces=json.dumps(row['faces']); scene.mw_batch_dependencies=json.dumps(row['depends_on'])

def _load_view(scene):
    preview=_raw(scene)['preview']; view=preview['views'][scene.mw_batch_view_index]
    scene.mw_batch_engine=preview['engine']
    for key in ('width','height','samples'): setattr(scene,'mw_batch_'+key,preview[key])
    for prop,key in [('azimuth','azimuth'),('elevation','elevation'),('lighting','lighting'),('focus','focus'),('zoom','zoom')]: setattr(scene,'mw_batch_'+prop,view[key])

class BatchOperator(ui.MWOperator): pass

class MW_OT_batch_load(BatchOperator,bpy.types.Operator):
    bl_idname='material_workflow.batch_load'; bl_label='载入批量清单'
    def run(self,context):
        with open(bpy.path.abspath(context.scene.mw_batch_path),encoding='utf-8-sig') as handle: value=json.load(handle)
        _store(context.scene,value); context.scene.mw_batch_index=0; context.scene.mw_batch_view_index=0; _load_target(context.scene); _load_view(context.scene)

class MW_OT_batch_save(BatchOperator,bpy.types.Operator):
    bl_idname='material_workflow.batch_save'; bl_label='另存已提交批量草稿'
    def run(self,context):
        value=get_manifest(context.scene); path=Path(bpy.path.abspath(context.scene.mw_batch_path))
        if not path.is_absolute() or path.suffix.lower()!='.json': raise ValueError('请指定绝对 JSON 路径')
        with path.open('x',encoding='utf-8') as handle: json.dump(value,handle,ensure_ascii=False,indent=2)

class MW_OT_batch_context(BatchOperator,bpy.types.Operator):
    bl_idname='material_workflow.batch_context'; bl_label='读取当前场景/视图层/帧'
    def run(self,context):
        value=_raw(context.scene); value['context']=_context(context); _store(context.scene,value)

class MW_OT_batch_add(BatchOperator,bpy.types.Operator):
    bl_idname='material_workflow.batch_add'; bl_label='从单材质草稿＋活动网格添加'
    selected_objects: BoolProperty(name='添加所选网格', default=False)

    def run(self,context):
        scene=context.scene; source=ui.normalize_manifest(ui._flush(scene))
        if source['schema_version']!='1.1': raise ValueError('请先显式升级单材质草稿到 v2')
        objects = context.selected_objects if self.selected_objects else [context.object]
        if not objects or any(obj is None or obj.type != 'MESH' or not obj.data.uv_layers.active
                              for obj in objects):
            raise ValueError('请选择有 UV 的网格')
        if scene.mw_batch_json:
            value=get_manifest(scene); validate_context(context,value)
        else:
            value={'schema_version':'1.0','context':_context(context),'objects':[],'assignments':[],'preview':copy.deepcopy(source['preview'])}
        for obj in sorted(objects, key=lambda item: item.name):
            row = {key: copy.deepcopy(source[key]) for key in ('target', 'material', 'layers')}
            row.update(id='assignment_' + uuid.uuid4().hex[:12], faces=None, depends_on=[])
            row['material']['id'] = 'material_' + uuid.uuid4().hex[:12]
            row['target'].update(object=obj.name, material_slot=obj.active_material_index,
                                 uv_layer=obj.data.uv_layers.active.name)
            if obj.name not in value['objects']:
                value['objects'].append(obj.name)
            value['assignments'].append(row)
        _store(scene,value); scene.mw_batch_index=len(value['assignments'])-1; _load_target(scene); _load_view(scene)

class MW_OT_batch_select(BatchOperator,bpy.types.Operator):
    bl_idname='material_workflow.batch_select'; bl_label='选择分配'
    index:IntProperty()
    def run(self,context):
        scene=context.scene; value=get_manifest(scene)
        if not 0<=self.index<len(value['assignments']): raise ValueError('无效分配')
        scene.mw_batch_index=self.index; _load_target(scene)

class MW_OT_batch_edit_material(BatchOperator,bpy.types.Operator):
    bl_idname='material_workflow.batch_edit_material'; bl_label='载入分配到单材质编辑器'
    def run(self,context):
        scene=context.scene; value=get_manifest(scene); row=_selected(scene,value)
        ui._store(scene,assignment_manifest(value,row)); scene.mw_layer_index=0; ui._load_layer(scene)
        scene.mw_batch_edit_id=row['id']

class MW_OT_batch_commit_material(BatchOperator,bpy.types.Operator):
    bl_idname='material_workflow.batch_commit_material'; bl_label='确认单材质草稿回写此分配'
    def run(self,context):
        scene=context.scene; value=get_manifest(scene); row=_selected(scene,value)
        if scene.mw_batch_edit_id!=row['id']: raise ValueError('请先载入此分配，防止草稿串单')
        source=ui.normalize_manifest(ui._flush(scene))
        if source['schema_version']!='1.1' or source['material']['id']!=row['material']['id']: raise ValueError('单材质草稿身份已变化；请重新载入分配')
        row['layers']=copy.deepcopy(source['layers']); row['material']['name']=source['material']['name']; _store(scene,value)

class MW_OT_batch_target(BatchOperator,bpy.types.Operator):
    bl_idname='material_workflow.batch_target'; bl_label='提交目标/面索引/依赖'
    def run(self,context):
        scene=context.scene; value=get_manifest(scene); row=_selected(scene,value)
        row['target']={'object':scene.mw_batch_object,'material_slot':scene.mw_batch_slot,'uv_layer':scene.mw_batch_uv}
        row['faces']=json.loads(scene.mw_batch_faces); row['depends_on']=json.loads(scene.mw_batch_dependencies)
        if row['target']['object'] not in value['objects']: value['objects'].append(row['target']['object'])
        _store(scene,value)

class MW_OT_batch_faces(BatchOperator,bpy.types.Operator):
    bl_idname='material_workflow.batch_faces'; bl_label='从目标网格读取选面'
    preserve: BoolProperty(default=False)

    def run(self,context):
        if self.preserve:
            context.scene.mw_batch_faces = 'null'
            return
        scene=context.scene; row=_selected(scene,get_manifest(scene)); obj=context.object
        if obj is None or obj.type!='MESH' or obj.name!=row['target']['object']: raise ValueError('活动网格必须是此分配目标')
        if obj.mode=='EDIT':
            mesh=bmesh.from_edit_mesh(obj.data); mesh.faces.ensure_lookup_table(); mesh.faces.index_update(); faces=[f.index for f in mesh.faces if f.select]
        else: faces=[f.index for f in obj.data.polygons if f.select]
        if not faces: raise ValueError('没有选面，请选择面或点击「保留面分配」')
        scene.mw_batch_faces=json.dumps(faces)


class MW_OT_batch_dependency(BatchOperator, bpy.types.Operator):
    bl_idname = 'material_workflow.batch_dependency'
    bl_label = '切换前置分配'
    identity: StringProperty()

    def run(self, context):
        scene = context.scene
        value = get_manifest(scene)
        current = _selected(scene, value)
        if self.identity not in {row['id'] for row in value['assignments'] if row != current}:
            raise ValueError('请选择本批次其他分配')
        dependencies = json.loads(scene.mw_batch_dependencies)
        if self.identity in dependencies:
            dependencies.remove(self.identity)
        else:
            dependencies.append(self.identity)
        scene.mw_batch_dependencies = json.dumps(dependencies)

class MW_OT_batch_delete(BatchOperator,bpy.types.Operator):
    bl_idname='material_workflow.batch_delete'; bl_label='删除分配'
    def run(self,context):
        scene=context.scene; value=get_manifest(scene); row=_selected(scene,value)
        if len(value['assignments'])==1: raise ValueError('至少保留一个分配')
        if any(row['id'] in a['depends_on'] for a in value['assignments']): raise ValueError('其他分配依赖此项；请先显式移除依赖')
        value['assignments'].remove(row); _store(scene,value); scene.mw_batch_index=min(scene.mw_batch_index,len(value['assignments'])-1); _load_target(scene)

class MW_OT_batch_view(BatchOperator,bpy.types.Operator):
    bl_idname='material_workflow.batch_view'; bl_label='编辑批量机位'
    action:EnumProperty(items=[(x,x,'') for x in ('SELECT','ADD','DELETE','COMMIT','OBJECTS')])
    index:IntProperty()
    def run(self,context):
        scene=context.scene; value=get_manifest(scene); views=value['preview']['views']
        if self.action=='SELECT':
            if not 0<=self.index<len(views): raise ValueError('无效机位')
            scene.mw_batch_view_index=self.index; _load_view(scene); return
        i=scene.mw_batch_view_index
        if self.action=='ADD':
            view=copy.deepcopy(views[i]); view['id']='view_'+uuid.uuid4().hex[:12]; views.append(view); i=len(views)-1
        elif self.action=='DELETE':
            if len(views)==1: raise ValueError('至少保留一个机位')
            views.pop(i); i=min(i,len(views)-1)
        elif self.action=='OBJECTS':
            names=[o.name for o in context.selected_objects if o.type=='MESH']
            if not names or not set(names).issubset(value['objects']): raise ValueError('请选择批量范围内的网格')
            views[i]['objects']=names
        else:
            for key in ('azimuth','elevation','lighting','focus','zoom'):
                v=getattr(scene,'mw_batch_'+key); views[i][key]=list(v) if key=='focus' else v
        _store(scene,value); scene.mw_batch_view_index=i; _load_view(scene)

class MW_OT_batch_apply(BatchOperator,bpy.types.Operator):
    bl_idname='material_workflow.batch_apply'; bl_label='应用已提交批量草稿到当前场景'; bl_options={'REGISTER','UNDO'}
    def run(self,context):
        scene=context.scene; value=get_manifest(scene); validate_context(context,value); reports={}; rows={r['id']:r for r in value['assignments']}
        for identity in topological_order(value):
            row=rows[identity]
            if any(reports[d]['status']!='pass' for d in row['depends_on']): reports[identity]={'status':'blocked','error':'前提分配失败'}; continue
            try:
                existing=[m for m in bpy.data.materials if m.get(core.KEY)==row['material']['id']]
                material=existing[0] if len(existing)==1 else None
                if material is not None and core.STATE in material:
                    raise ValueError('旧 v1 材质须先通过单材质编辑器显式升级；批量不隐式升级')
                manifest=assignment_manifest(value,row)
                resources=core.existing_resource_paths(material,manifest)
                with ui.ensure_object_mode(context):
                    result = batch.apply_assignment(value,row,resources)
                material = context.scene.objects[row['target']['object']].material_slots[
                    row['target']['material_slot']].material
                for node in core._nodes(material).values():
                    ui.apply_node_theme(node, ui.DOMAIN, accent=node.type == 'FRAME')
                reports[identity]={'status':'pass','result':result}
            except Exception as error:
                reports[identity]={'status':'fail','error':str(error),'code':getattr(error,'code',None)}
                if getattr(error,'rollback_ok',True) is False:
                    for remaining in rows:
                        reports.setdefault(remaining,{'status':'blocked','error':'回滚失败，停止当前场景批量'})
                    break
        scene.mw_batch_report=json.dumps(reports,ensure_ascii=False); self.report({'INFO'},'批量完成；请查看逐项结果并另存工作副本')

class MW_OT_batch_preview(BatchOperator,bpy.types.Operator):
    bl_idname='material_workflow.batch_preview'; bl_label='提交批量预览设置'
    def run(self,context):
        scene=context.scene; value=get_manifest(scene); preview=value['preview']
        if preview['engine']!=scene.mw_batch_engine:
            preview.update(engine=scene.mw_batch_engine,device={'backend':'GRAPHICS' if scene.mw_batch_engine=='BLENDER_EEVEE' else 'CPU'},denoise=scene.mw_batch_engine=='CYCLES')
        for key in ('width','height','samples'): preview[key]=getattr(scene,'mw_batch_'+key)
        _store(scene,value)

class MW_PT_batch(bpy.types.Panel):
    bl_label='批量分配与机位'; bl_idname='MW_PT_batch'; bl_parent_id='MW_PT_panel'
    bl_space_type='VIEW_3D'; bl_region_type='UI'; bl_category=ui.THEME.tab_label
    def draw(self,context):
        MW_PT_batch.draw_contents(self.layout, context)

    @staticmethod
    def draw_contents(l, context):
        s=context.scene
        l.prop(s,'mw_batch_path',text='批量草稿文件')
        r=l.row(align=True)
        r.operator('material_workflow.batch_load',text='载入')
        r.operator('material_workflow.batch_save',text='另存')
        row = l.row()
        row.enabled = bool(s.mw_manifest_json)
        row.operator('material_workflow.batch_add', text='加入所选网格', icon='ADD').selected_objects = True
        if not s.mw_batch_json: return
        try:
            m = get_manifest(s)
            current = _selected(s, m)
            faces = json.loads(s.mw_batch_faces)
            dependencies = json.loads(s.mw_batch_dependencies)
        except (ValueError, TypeError, KeyError):
            l.label(text='批量草稿有待修正，请重新载入', icon='ERROR')
            return
        l.operator('material_workflow.batch_context', text='读取当前场景与视图层')
        for i,a in enumerate(m['assignments']):
            l.operator('material_workflow.batch_select', text=a['target']['object']+' · '+a['material']['name'],
                       depress=i==s.mw_batch_index).index=i
        r=l.row(); r.operator('material_workflow.batch_edit_material'); r.operator('material_workflow.batch_commit_material')
        l.prop_search(s,'mw_batch_object',s,'objects'); l.prop(s,'mw_batch_slot')
        target=s.objects.get(s.mw_batch_object)
        if target and target.type=='MESH': l.prop_search(s,'mw_batch_uv',target.data,'uv_layers')
        else: l.prop(s,'mw_batch_uv')
        row = l.row(align=True)
        row.operator('material_workflow.batch_faces', text='读取选面')
        row.operator('material_workflow.batch_faces', text='保留面分配').preserve = True
        l.label(text='保留原网格面分配' if faces is None else f'待分配面 · {len(faces)}')
        if len(m['assignments']) > 1:
            box = l.box()
            box.label(text='前置分配')
            for assignment in m['assignments']:
                identity = assignment['id']
                if identity != current['id']:
                    box.operator('material_workflow.batch_dependency', text=assignment['target']['object'],
                                 depress=identity in dependencies).identity = identity
        row = l.row(align=True)
        row.operator('material_workflow.batch_target', text='保存目标设置')
        row.operator('material_workflow.batch_delete', text='', icon='REMOVE')
        for p in ('engine','width','height','samples'): l.prop(s,'mw_batch_'+p)
        l.operator('material_workflow.batch_preview')
        for i,v in enumerate(m['preview']['views']):
            op=l.operator('material_workflow.batch_view',text=v['id']+' · '+', '.join(v['objects']),depress=i==s.mw_batch_view_index); op.action='SELECT'; op.index=i
        for p in ('azimuth','elevation','lighting','focus','zoom'): l.prop(s,'mw_batch_'+p)
        for action,label in [('COMMIT','提交机位参数'),('OBJECTS','机位对象取当前选择'),('ADD','增加机位'),('DELETE','删除机位')]: l.operator('material_workflow.batch_view',text=label).action=action
        l.operator('material_workflow.batch_apply', text='执行批量材质指派', icon='CHECKMARK')
        if s.mw_batch_report:
            labels = {'pass': '完成', 'fail': '失败', 'blocked': '依赖未满足'}
            for key,row in json.loads(s.mw_batch_report).items():
                label = next((a['target']['object'] for a in m['assignments'] if a['id'] == key), key)
                l.label(text=label+' · '+labels.get(row['status'], row['status'])+' '+row.get('error',''))

CLASSES=(MW_OT_batch_load,MW_OT_batch_save,MW_OT_batch_context,MW_OT_batch_add,MW_OT_batch_select,MW_OT_batch_edit_material,MW_OT_batch_commit_material,MW_OT_batch_target,MW_OT_batch_faces,MW_OT_batch_dependency,MW_OT_batch_delete,MW_OT_batch_view,MW_OT_batch_apply,MW_OT_batch_preview)
PROPS={
 'mw_batch_json':StringProperty(options={'HIDDEN'}),'mw_batch_report':StringProperty(options={'HIDDEN'}),'mw_batch_edit_id':StringProperty(options={'HIDDEN'}),
 'mw_batch_path':StringProperty(name='批量清单 JSON',subtype='FILE_PATH'),'mw_batch_index':IntProperty(min=0),'mw_batch_view_index':IntProperty(min=0),
 'mw_batch_object':StringProperty(name='目标对象'),'mw_batch_slot':IntProperty(name='材质槽',min=0),'mw_batch_uv':StringProperty(name='UV 层'),
 'mw_batch_faces':StringProperty(default='null', options={'HIDDEN'}),'mw_batch_dependencies':StringProperty(default='[]', options={'HIDDEN'}),
 'mw_batch_azimuth':FloatProperty(name='方位角',min=-360,max=360,default=45),'mw_batch_elevation':FloatProperty(name='仰角',min=-80,max=89,default=15),
 'mw_batch_lighting':EnumProperty(name='灯光',items=[('soft','柔光',''),('raking','掠射光','')]),
 'mw_batch_focus':FloatVectorProperty(name='局部焦点',size=3,min=0,max=1,default=(.5,.5,.5)),'mw_batch_zoom':FloatProperty(name='放大',min=.25,max=10,default=1)}
PROPS['mw_batch_engine']=EnumProperty(name='批量预览引擎',items=[('CYCLES','Cycles',''),('BLENDER_EEVEE','Eevee','')])
for key,low,high,default in [('width',64,2048,384),('height',64,2048,384),('samples',1,256,16)]: PROPS['mw_batch_'+key]=IntProperty(name={'width':'宽度','height':'高度','samples':'采样'}[key],min=low,max=high,default=default)

def register():
    for cls in CLASSES: bpy.utils.register_class(cls)
    for name,prop in PROPS.items(): setattr(bpy.types.Scene,name,prop)

def unregister():
    for name in PROPS:
        if hasattr(bpy.types.Scene,name): delattr(bpy.types.Scene,name)
    for cls in reversed(CLASSES): bpy.utils.unregister_class(cls)
