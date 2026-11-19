"""Small shared-core workbench; detailed edge/island edits remain in Blender."""
import json
import math
import uuid
import copy
import bpy
import bmesh
from . import core
from .surface_ui import UVW_OT_surface_report,draw as draw_surface_report
from .job_ui import UVW_OT_watch_job,UVW_OT_unwatch_job,draw as draw_job

_STATUS={'needs_review':'待用途与视觉审阅','pass':'通过','fail':'未通过','unknown':'检查未完成',
         'not_assigned':'未绑定用途','recipe_bound':'用途配方已绑定并检查',
         'candidates_created':'候选已生成','validated':'已重新检查','candidate_edited':'局部编辑已完成，请查看检查结果',
         'adopted_for_editing':'已采用，可接续编辑','restored':'已恢复原编辑层与接缝',
         'candidate_copied':'当前UV已复制为新候选','rules_updated':'规则已保存，请查看检查结果',
         'seams_marked':'已按规则显式标记接缝（尚未展开UV）','lightmap_ready':'辅助 Lightmap UV2 已生成并检查（active_render 未变）'}

def _label(value):
    return _STATUS.get(value,value)

def _execute(context, request):
    """Expose real phases during synchronous work; do not invent percentages."""
    labels={'solve':'生成候选','checking':'检查重叠、间距与拉伸','local_edit':'处理局部编辑','micro_island_merge':'合并相邻微岛',
            'transfer_correspondence':'核对迁移对应','geometry_diagnostics':'定位基础网格问题',
            'generate':'准备候选','copy_uv':'复制UV','adopt':'采用候选','restore':'恢复编辑层',
            'edit':'准备局部编辑','pack':'整图排布','recipe':'检查用途配方','settings':'检查规则','seams':'更新接缝',
            'lightmap':'生成辅助Lightmap UV2','transfer':'准备迁移','inspect':'检查输入'}
    wm=context.window_manager
    wm.progress_begin(0,1)
    def progress(stage, **details):
        text=labels.get(stage,stage)
        suffix=' / '.join(str(details[k]) for k in ('object','method','action') if k in details)
        context.scene.uvw_status='正在'+text+('：'+suffix if suffix else '')
        context.workspace.status_text_set(context.scene.uvw_status+'（同步计算；大型任务请用后台 CLI）')
    try:
        progress('checking' if request['operation']=='validate' else request['operation'])
        result=core.execute(request,progress=progress)
        wm.progress_update(1)
        return result
    finally:
        wm.progress_end()
        context.workspace.status_text_set(None)

class UVW_OT_geometry_diagnostics(bpy.types.Operator):
    bl_idname='uvw.geometry_diagnostics'
    bl_label='定位网格问题并生成交接报告'
    def execute(self,context):
        try:
            request={'version':'1.0','operation':'inspect','scene':context.scene.name,
                     'view_layer':context.view_layer.name,'objects':_targets(context),'diagnostics':{'max_examples':1000}}
            result=_execute(context,request)['diagnostics']
            doc=bpy.data.texts.new('UV-Geometry-Handoff.json')
            doc.write(json.dumps(result,ensure_ascii=False,indent=2))
            context.scene.uvw_status='网格诊断：'+result['status']+'；完整交接见文本 '+doc.name
            self.report({'INFO'},context.scene.uvw_status)
            return {'FINISHED'}
        except Exception as e:
            self.report({'ERROR'},str(e));return {'CANCELLED'}

def _targets(context):
    return sorted(o.name for o in context.selected_objects if o.type=='MESH')

def _manifest(context,operation):
    s=context.scene
    names=_targets(context)
    if operation=='lightmap':
        return {'version':'1.0','operation':'lightmap','scene':s.name,'view_layer':context.view_layer.name,
                'objects':names,
                'lightmap':{'layer':s.uvw_lightmap_layer.strip() or 'UVMap_Lightmap',
                            'width':s.uvw_width,'height':s.uvw_height,
                            'box_div':s.uvw_lightmap_box_div,'margin_div':s.uvw_lightmap_margin_div,
                            'overwrite':s.uvw_lightmap_overwrite}}
    if operation not in ('generate','copy_uv'):
        rec=core.state(s)['candidates'].get(s.uvw_candidate)
        if rec:names=rec['objects']
    if operation in ('recipe','settings','seams'):
        return {'version':'1.0','operation':operation,'scene':s.name,'view_layer':context.view_layer.name,
                'objects':names,'candidate':s.uvw_candidate or 'mesh_seams'}
    manifest={'version':'1.0','operation':operation,'scene':s.name,'view_layer':context.view_layer.name,
            'objects':names,'candidate':s.uvw_candidate,'methods':[s.uvw_method]}
    if operation in ('generate','copy_uv'):
        manifest.update(width=s.uvw_width,height=s.uvw_height,
            constraints={n:c for n,c in json.loads(s.get('uvw_constraints','{}')).items() if n in names})
    if operation=='generate' and s.uvw_method=='MINIMUM_STRETCH':
        manifest.update(slim_iterations=s.uvw_slim_iterations,slim_no_flip=s.uvw_slim_no_flip)
    if operation=='copy_uv':
        manifest['source_layers']={n:context.view_layer.objects[n].data.uv_layers.active.name for n in names}
    return manifest

class UVW_OT_workflow(bpy.types.Operator):
    bl_idname='uvw.workflow'
    bl_label='UV 工作流'
    bl_options={'REGISTER','UNDO'}
    operation:bpy.props.EnumProperty(items=[(k,k,'') for k in ('generate','copy_uv','validate','adopt','restore')])

    def execute(self,context):
        try:
            if context.mode!='OBJECT':
                raise ValueError('请先回到物体模式执行候选操作，确保数据与检查记录一起撤销')
            request=_manifest(context,self.operation)
            if self.operation in ('generate','copy_uv'):request['candidate']='edit_'+uuid.uuid4().hex[:8]
            result=_execute(context,request)
            if self.operation=='generate':
                made=[r for r in result['candidates'] if r['status']!='rejected']
                if made:context.scene.uvw_candidate=made[0]['candidate']
            elif self.operation=='copy_uv':context.scene.uvw_candidate=result['candidate']
            context.scene.uvw_status=_label(result['status'])
            self.report({'INFO'},context.scene.uvw_status)
            return {'FINISHED'}
        except Exception as e:
            context.scene.uvw_status=str(e);self.report({'ERROR'},str(e));return {'CANCELLED'}

class UVW_OT_select(bpy.types.Operator):
    bl_idname='uvw.select_candidate'
    bl_label='查看候选'
    candidate:bpy.props.StringProperty()
    def execute(self,context):
        context.scene.uvw_candidate=self.candidate
        return {'FINISHED'}

class UVW_OT_constraints(bpy.types.Operator):
    bl_idname='uvw.constraints'
    bl_label='记录新候选约束'
    bl_options={'REGISTER','UNDO'}
    kind:bpy.props.EnumProperty(items=[('must_cut','必须切开',''),('must_not_cut','禁止切开',''),('locked_faces','固定选中面',''),('clear','清除对象约束','')])

    def execute(self,context):
        o=context.object
        if context.mode!='OBJECT' or not o or o.type!='MESH':
            self.report({'ERROR'},'请先在网格编辑模式选择边或面，按Tab回物体模式后记录约束');return {'CANCELLED'}
        allc=json.loads(context.scene.get('uvw_constraints','{}'))
        if self.kind=='clear':allc.pop(o.name,None)
        else:
            c=allc.setdefault(o.name,{'source_uv':o.data.uv_layers.active.name if o.data.uv_layers.active else ''})
            items=[e.index for e in o.data.edges if e.select and not e.hide] if self.kind!='locked_faces' else [f.index for f in o.data.polygons if f.select and not f.hide]
            c[self.kind]=sorted(set(c.get(self.kind,[]))|set(items))
            opposite={'must_cut':'must_not_cut','must_not_cut':'must_cut'}.get(self.kind)
            if opposite:c[opposite]=sorted(set(c.get(opposite,[]))-set(items))
        # Scene properties are not part of EditMesh Undo. Enter and finish this
        # transaction in Object Mode; native Tab preserves the mesh selection.
        serialized=json.dumps(allc,ensure_ascii=False)
        context.scene['uvw_constraints']=serialized
        self.report({'INFO'},'新建候选约束已记录；仅下次生成使用，当前候选保持');return {'FINISHED'}

class UVW_OT_preview(bpy.types.Operator):
    bl_idname='uvw.preview'
    bl_label='棋盘预览'
    def execute(self,context):
        try:
            from .diagnostics import render
            from pathlib import Path
            path=Path(bpy.app.tempdir)/('uvw-checker-'+uuid.uuid4().hex[:8]+'.png')
            render(context.scene,context.scene.uvw_candidate,str(path))
            self.report({'INFO'},'棋盘图已写入 '+str(path))
            image=bpy.data.images.load(str(path),check_existing=True)
            area=next((a for a in context.screen.areas if a.type=='IMAGE_EDITOR'),None)
            if area is None:
                area=context.area
                area.type='IMAGE_EDITOR'
                area.ui_type='IMAGE_EDITOR'
            area.spaces.active.image=image
            context.scene.uvw_status='棋盘预览：'+str(path)
            return {'FINISHED'}
        except Exception as e:self.report({'ERROR'},str(e));return {'CANCELLED'}

class UVW_OT_capture_edit(bpy.types.Operator):
    bl_idname='uvw.capture_edit_selection'
    bl_label='记录局部选择'
    bl_options={'REGISTER','UNDO'}
    whole_islands:bpy.props.BoolProperty(default=False,options={'HIDDEN'})

    def execute(self,context):
        s=context.scene;rec=core.state(s)['candidates'].get(s.uvw_candidate)
        # A failed new capture must never leave an older actionable selection.
        if 'uvw_edit_selection' in s:del s['uvw_edit_selection']
        if not rec or context.mode!='EDIT_MESH':
            self.report({'ERROR'},'请先选定候选，再在编辑模式选择面或UV点');return {'CANCELLED'}
        domain='ISLANDS' if self.whole_islands or s.uvw_edit_action in ('stack','unstack','orient','merge_micro_islands') else s.uvw_edit_domain
        selection={};loop_vertices={};captured=False
        try:
            if domain=='LOOPS' and s.tool_settings.use_uv_select_sync:
                raise ValueError('记录UV点前请关闭UV同步选择；面或整岛选择可保持同步')
            for o in context.objects_in_mode:
                if o.name not in rec['objects'] or o.type!='MESH':continue
                active=o.data.uv_layers.active
                if not active or active.name!=rec['layer']:
                    raise ValueError('请先在网格数据的 UV 贴图列表选中候选层，再记录选择：'+o.name)
                bm=bmesh.from_edit_mesh(o.data);bm.faces.ensure_lookup_table();bm.faces.index_update();bm.verts.index_update()
                if domain=='LOOPS':
                    rows=[(f.index,l.vert.index) for f in bm.faces for l in f.loops if l.uv_select_vert and f.select and not f.hide]
                    if rows:loop_vertices[o.name]=rows
                else:
                    ids=[f.index for f in bm.faces if f.select and not f.hide]
                    if ids:selection[o.name]={'domain':domain,'indices':ids}
            bpy.ops.object.mode_set(mode='OBJECT')
            for name,rows in loop_vertices.items():
                m=bpy.data.objects[name].data;ids=[]
                for fi,vi in rows:
                    matches=[i for i in m.polygons[fi].loop_indices if m.loops[i].vertex_index==vi]
                    if len(matches)!=1:raise ValueError('无法唯一定位选中的UV角点')
                    ids.append(matches[0])
                selection[name]={'domain':domain,'indices':sorted(set(ids))}
            if not selection:raise ValueError('没有选中的面或UV点')
            binding={'candidate':s.uvw_candidate,
                     'geometry':{n:core.geometry(bpy.data.objects[n]) for n in selection},
                     'uv_hash':{n:core._uv_fingerprint(bpy.data.objects[n].data,rec['layer']) for n in selection}}
            s['uvw_edit_selection']=json.dumps({'selection':selection,'binding':binding},ensure_ascii=False)
            captured=True
            self.report({'INFO'},f'已记录 {len(selection)} 个对象；已回物体模式，可执行并完整撤销')
            return {'FINISHED'}
        except Exception as e:self.report({'ERROR'},str(e));return {'CANCELLED'}
        finally:
            if not captured and context.object and context.object.mode=='OBJECT':bpy.ops.object.mode_set(mode='EDIT')


class UVW_OT_edit(bpy.types.Operator):
    bl_idname='uvw.edit_candidate'
    bl_label='执行局部编辑'
    bl_options={'REGISTER','UNDO'}

    def execute(self,context):
        s=context.scene
        try:
            if context.mode!='OBJECT':
                raise ValueError('请先记录局部选择；记录成功后在物体模式执行，确保坐标与检查记录一起撤销')
            capture=json.loads(s.get('uvw_edit_selection','{}'))
            if not capture:raise ValueError('请先记录局部选择')
            rec=core.state(s)['candidates'].get(s.uvw_candidate)
            for name in capture['selection']:
                obj=context.view_layer.objects.get(name)
                active=obj.data.uv_layers.active if obj and obj.type=='MESH' else None
                if not rec or not active or active.name!=rec['layer']:
                    raise ValueError('活动UV层已切换，请回到候选层并重新记录选择：'+name)
            kind=s.uvw_edit_action;action={'kind':kind}
            if kind in ('translate','split'):action['delta_px']=list(s.uvw_edit_delta)
            elif kind=='rotate':action['angle_degrees']=s.uvw_edit_angle
            elif kind=='scale':action['factors']=list(s.uvw_edit_scale)
            elif kind in ('mirror','align'):action['axis']=s.uvw_edit_axis
            elif kind=='density':action['target_px_m']=s.uvw_edit_density
            elif kind in ('pin','lock'):action['enabled']=s.uvw_edit_enabled
            elif kind=='relax':action['iterations']=s.uvw_edit_iterations
            elif kind=='unwrap':
                action['method']=s.uvw_edit_unwrap_method
                if s.uvw_edit_unwrap_method=='MINIMUM_STRETCH':
                    action['iterations']=s.uvw_slim_iterations
                    action['no_flip']=s.uvw_slim_no_flip
            elif kind=='pack':action.update(rotate=s.uvw_pack_rotate,scale=s.uvw_pack_scale,pin_method='LOCKED')
            elif kind=='stitch':
                if not capture.get('stitch_pairs'):raise ValueError('请先记录缝合两侧')
                action.update(pairs=capture['stitch_pairs'],limit_px=s.uvw_stitch_limit,seam_mode=s.uvw_stitch_seams)
            elif kind=='orient':
                if not capture.get('edge_loops'):raise ValueError('请先记录活动面上的有向边')
                action.update(edge_loops=capture['edge_loops'],angle_degrees=s.uvw_direction_angle)
            elif kind=='unstack':action['offset_px']=list(s.uvw_unstack_offset)
            elif kind=='merge_micro_islands':action['threshold_px2']=s.uvw_micro_island_area
            elif kind=='stack':
                draft=capture.get('stack_draft')
                if not draft or draft['mirrored']!=s.uvw_stack_mirrored:raise ValueError('请按当前镜像设置重新记录移动岛和配对草稿')
                _check_binding(context,rec,draft['reference_binding'])
                action.update(reference=draft['reference'],pairs=draft['pairs'],mirrored=draft['mirrored'],group_id=s.uvw_stack_group)
            request=_manifest(context,'edit')
            request.update(selection=capture['selection'],selection_binding=capture['binding'],action=action)
            result=_execute(context,request)
            s.uvw_status=_label(result['status'])+'；'+_label(result['record']['metrics']['status'])
            self.report({'INFO'},s.uvw_status);return {'FINISHED'}
        except Exception as e:s.uvw_status=str(e);self.report({'ERROR'},str(e));return {'CANCELLED'}


class UVW_OT_capture_stitch(bpy.types.Operator):
    bl_idname='uvw.capture_stitch'
    bl_label='记录缝合两侧'
    bl_options={'REGISTER','UNDO'}

    def execute(self,context):
        s=context.scene;o=context.object
        if 'uvw_edit_selection' in s:del s['uvw_edit_selection']
        try:
            rec=core.state(s)['candidates'].get(s.uvw_candidate)
            if not rec or context.mode!='EDIT_MESH' or not o or o.name not in rec['objects']:
                raise ValueError('请在候选网格编辑模式选择接缝边，并设定静态侧活动面')
            if o.data.uv_layers.active.name!=rec['layer']:raise ValueError('请先采用候选编辑层')
            bm=bmesh.from_edit_mesh(o.data);bm.faces.index_update();bm.edges.index_update()
            active=bm.faces.active
            if active is None:raise ValueError('请先选择一个活动面作为不移动的一侧')
            static_face=active.index
            edges=[e.index for e in bm.edges if e.select and not e.hide and e.is_manifold and active in e.link_faces]
            if not edges:raise ValueError('没有与活动面相邻的选中双面接缝边')
            bpy.ops.object.mode_set(mode='OBJECT')
            mesh=o.data;uv=core.coordinates(mesh,rec['layer']);by_edge={};nodes={};keys={}
            for p in mesh.polygons:
                ls=list(p.loop_indices)
                for a,b in zip(ls,ls[1:]+ls[:1]):
                    key=(mesh.loops[a].vertex_index,*map(float,uv[a]));keys[a]=key;nodes.setdefault(key,[]).append(a)
                    by_edge.setdefault(mesh.loops[a].edge_index,[]).append((p.index,a,b))
            mapping={}
            for ei in edges:
                rows=by_edge[ei];stat=next(r for r in rows if r[0]==static_face);move=next(r for r in rows if r[0]!=static_face)
                target={mesh.loops[i].vertex_index:i for i in stat[1:]}
                for a in move[1:]:
                    b=target[mesh.loops[a].vertex_index]
                    if keys[a]==keys[b]:continue
                    for i in nodes[keys[a]]:
                        if i in mapping and keys[mapping[i]]!=keys[b]:raise ValueError('缝合目标不一致，请缩小选择')
                        mapping[i]=b
            if not mapping:raise ValueError('选中边的 UV 已连续')
            selection={o.name:{'domain':'LOOPS','indices':sorted(mapping)}}
            binding={'candidate':s.uvw_candidate,'geometry':{o.name:core.geometry(o)},
                     'uv_hash':{o.name:core._uv_fingerprint(mesh,rec['layer'])}}
            s['uvw_edit_selection']=json.dumps({'selection':selection,'binding':binding,
                'stitch_pairs':{o.name:[[i,mapping[i]] for i in sorted(mapping)]}},ensure_ascii=False)
            s.uvw_edit_action='stitch';s.uvw_status='缝合两侧已记录；活动面保持，另一侧移动'
            self.report({'INFO'},s.uvw_status);return {'FINISHED'}
        except Exception as e:
            self.report({'ERROR'},str(e));return {'CANCELLED'}


def _record(context):
    rec=core.state(context.scene)['candidates'].get(context.scene.uvw_candidate)
    if not rec:raise ValueError('请先选定候选')
    return rec


def _binding(context,rec,names):
    return {'candidate':context.scene.uvw_candidate,
            'geometry':{n:core.geometry(context.view_layer.objects[n]) for n in names},
            'uv_hash':{n:core._uv_fingerprint(context.view_layer.objects[n].data,rec['layer']) for n in names}}


def _check_binding(context,rec,binding):
    if not binding or binding.get('candidate')!=context.scene.uvw_candidate:
        raise ValueError('记录属于其他候选，请重新记录')
    names=list(binding.get('geometry',{}))
    if not names or set(names)!=set(binding.get('uv_hash',{})) or set(names)-set(rec['objects']):
        raise ValueError('记录的对象范围无效，请重新记录')
    if binding!=_binding(context,rec,names):raise ValueError('模型或UV已经改变，请重新记录选择')
    for n in names:
        if context.view_layer.objects[n].data.uv_layers.active.name!=rec['layer']:
            raise ValueError('请回到候选编辑层后重新记录：'+n)


def _whole_members(context,rec,capture):
    """Expand captured face seeds, then return exact individual islands."""
    from .policies import island_member
    _check_binding(context,rec,capture.get('binding'))
    members=[]
    for name,row in capture['selection'].items():
        if row['domain']!='ISLANDS':raise ValueError('规则绑定与复用需要重新记录完整岛范围')
        o=context.view_layer.objects[name];groups,_=core.islands(o,rec['layer'])
        if any(f>=len(groups) for f in row['indices']):raise ValueError('记录的面已经失效')
        chosen={groups[f] for f in row['indices']}
        for group in sorted(chosen):
            faces=[f for f,g in enumerate(groups) if g==group]
            island_member(o,rec['layer'],faces);members.append({'object':name,'faces':faces})
    if not members:raise ValueError('没有记录的完整岛')
    return sorted(members,key=lambda m:(m['object'],min(m['faces'])))


def _settings(context,rec):
    from .policies import defaults
    from .recipes import binding,require_binding
    current=binding([context.view_layer.objects[n] for n in rec['objects']],rec['manifest'],context.scene.unit_settings.scale_length)
    value=copy.deepcopy(rec.get('settings')) if rec.get('settings') else defaults(current)
    require_binding(value,current)
    return value


def _apply_settings(context,value):
    request=_manifest(context,'settings');request['settings']=value
    result=_execute(context,request)
    context.scene.uvw_status='规则已保存；'+_label(result['record']['metrics']['status'])+'；视觉仍需审阅'
    return result


class UVW_OT_capture_direction(bpy.types.Operator):
    bl_idname='uvw.capture_direction'
    bl_label='记录活动面有向边'
    bl_options={'REGISTER','UNDO'}

    def execute(self,context):
        s=context.scene
        if 'uvw_edit_selection' in s:del s['uvw_edit_selection']
        try:
            rec=_record(context);o=context.object
            if context.mode!='EDIT_MESH' or not o or o.name not in rec['objects']:
                raise ValueError('请在候选编辑模式中选择活动面上的一条边')
            if o.data.uv_layers.active.name!=rec['layer']:raise ValueError('请先采用候选编辑层')
            bm=bmesh.from_edit_mesh(o.data);bm.faces.index_update();bm.edges.index_update()
            face=bm.faces.active
            if face is None or face.hide:raise ValueError('请指定未隐藏的活动面')
            chosen=[(i,l) for i,l in enumerate(face.loops) if l.edge.select and not l.edge.hide]
            if len(chosen)!=1:raise ValueError('活动面上必须恰好选中一条边')
            pos=chosen[0][0];fi=face.index
            bpy.ops.object.mode_set(mode='OBJECT')
            ids=list(o.data.polygons[fi].loop_indices);edge=[ids[pos],ids[(pos+1)%len(ids)]]
            selection={o.name:{'domain':'ISLANDS','indices':[fi]}}
            capture={'selection':selection,'binding':_binding(context,rec,[o.name]),'edge_loops':edge}
            _whole_members(context,rec,capture)
            s['uvw_edit_selection']=json.dumps(capture,ensure_ascii=False)
            s.uvw_edit_domain='ISLANDS';s.uvw_status=f'有向边已记录：{o.name}，角点 {edge[0]} → {edge[1]}；按活动面绕序'
            self.report({'INFO'},s.uvw_status);return {'FINISHED'}
        except Exception as e:self.report({'ERROR'},str(e));return {'CANCELLED'}


class UVW_OT_stack_reference(bpy.types.Operator):
    bl_idname='uvw.stack_reference'
    bl_label='记录参考完整岛'
    bl_options={'REGISTER','UNDO'}

    def execute(self,context):
        s=context.scene
        for key in ('uvw_stack_reference','uvw_edit_selection'):
            if key in s:del s[key]
        try:
            rec=_record(context);o=context.object
            if context.mode!='EDIT_MESH' or not o or o.name not in rec['objects']:raise ValueError('请在候选编辑模式指定参考岛的活动面')
            if o.data.uv_layers.active.name!=rec['layer']:raise ValueError('请先采用候选编辑层')
            bm=bmesh.from_edit_mesh(o.data);bm.faces.index_update();f=bm.faces.active
            if f is None or f.hide:raise ValueError('请指定未隐藏的参考活动面')
            seed=f.index;bpy.ops.object.mode_set(mode='OBJECT')
            capture={'selection':{o.name:{'domain':'ISLANDS','indices':[seed]}},'binding':_binding(context,rec,[o.name])}
            member=_whole_members(context,rec,capture)[0]
            s['uvw_stack_reference']=json.dumps({'reference':{'object':o.name,'face':seed},'faces':member['faces'],'binding':capture['binding']},ensure_ascii=False)
            s.uvw_status=f'参考岛已记录：{o.name}，面 {member["faces"]}；接着选择移动岛'
            self.report({'INFO'},s.uvw_status);return {'FINISHED'}
        except Exception as e:self.report({'ERROR'},str(e));return {'CANCELLED'}


def _stack_draft(context,rec,capture,reference,mirrored):
    from .island_tools import _mapping
    _check_binding(context,rec,reference.get('binding'))
    members=_whole_members(context,rec,capture)
    ref=context.view_layer.objects[reference['reference']['object']];ref_faces=sorted(reference['faces']);pairs={}
    for member in members:
        o=context.view_layer.objects[member['object']];faces=sorted(member['faces'])
        if o.name==ref.name and set(faces)&set(ref_faces):raise ValueError('参考完整岛不能同时作为移动岛')
        if len(faces)!=len(ref_faces):raise ValueError('面数不同，不能按面次序配对；请使用相同拓扑的岛')
        rows=[]
        for moving,static in zip(faces,ref_faces):
            a=list(o.data.polygons[moving].loop_indices);b=list(ref.data.polygons[static].loop_indices)
            if len(a)!=len(b):raise ValueError('对应面的角点数不同，无法生成完整配对')
            if mirrored:b=list(reversed(b))
            rows.extend([i,j] for i,j in zip(a,b))
        _mapping(o,faces,ref,ref_faces,rows,mirrored)
        pairs.setdefault(o.name,[]).extend(rows)
    return {'reference':reference['reference'],'reference_binding':reference['binding'],'pairs':pairs,'mirrored':mirrored,
            'moving_members':members,'pairing':'sorted_faces_then_face_loop_order'}


class UVW_OT_stack_moving(bpy.types.Operator):
    bl_idname='uvw.stack_moving'
    bl_label='记录移动岛并生成配对草稿'
    bl_options={'REGISTER','UNDO'}

    def execute(self,context):
        s=context.scene
        if 'uvw_edit_selection' in s:del s['uvw_edit_selection']
        try:
            rec=_record(context);reference=json.loads(s.get('uvw_stack_reference','{}'))
            if not reference:raise ValueError('请先独立记录参考完整岛')
            if context.mode!='EDIT_MESH':raise ValueError('请在编辑模式选择移动岛上的面')
            selection={}
            for o in context.objects_in_mode:
                if o.name not in rec['objects'] or o.type!='MESH':continue
                if o.data.uv_layers.active.name!=rec['layer']:raise ValueError('请先采用候选编辑层：'+o.name)
                bm=bmesh.from_edit_mesh(o.data);bm.faces.index_update()
                faces=[f.index for f in bm.faces if f.select and not f.hide]
                if faces:selection[o.name]={'domain':'ISLANDS','indices':faces}
            if not selection:raise ValueError('没有选中的移动岛')
            bpy.ops.object.mode_set(mode='OBJECT')
            capture={'selection':selection,'binding':_binding(context,rec,list(selection))}
            capture['stack_draft']=_stack_draft(context,rec,capture,reference,s.uvw_stack_mirrored)
            s['uvw_edit_selection']=json.dumps(capture,ensure_ascii=False);s.uvw_edit_domain='ISLANDS'
            s.uvw_status='配对草稿已生成；核对下方角点对应后点击执行，尚未移动UV'
            self.report({'INFO'},s.uvw_status);return {'FINISHED'}
        except Exception as e:self.report({'ERROR'},str(e));return {'CANCELLED'}


class UVW_OT_seams(bpy.types.Operator):
    bl_idname='uvw.seams'
    bl_label='更新接缝'
    bl_options={'REGISTER','UNDO'}
    mode:bpy.props.EnumProperty(items=[(k,k,'') for k in ('MARK','CLEAR','REQUIRE_CUT','REQUIRE_JOIN','FREE','MARK_BY_ANGLE','MARK_FROM_SHARP')])

    def execute(self,context):
        s=context.scene
        try:
            if context.mode!='OBJECT':raise ValueError('请选边后按Tab回物体模式，再更新接缝以完整撤销')
            request=_manifest(context,'seams')
            if self.mode in ('MARK_BY_ANGLE','MARK_FROM_SHARP'):
                if not s.uvw_candidate or s.uvw_candidate not in core.state(s)['candidates']:
                    request.pop('candidate',None)
                if self.mode=='MARK_BY_ANGLE':
                    request['seam_action']={'mode':'MARK_BY_ANGLE','angle_degrees':s.uvw_seam_angle,
                                            'include_boundaries':s.uvw_seam_include_boundaries,
                                            'sync_sharp':s.uvw_seam_sync_sharp,
                                            'clear_existing':s.uvw_seam_clear_existing}
                else:
                    request['seam_action']={'mode':'MARK_FROM_SHARP',
                                            'include_boundaries':s.uvw_seam_include_boundaries,
                                            'clear_existing':s.uvw_seam_clear_existing}
                result=_execute(context,request)
                if result.get('status')=='rules_updated':
                    s.uvw_status='候选接缝已按规则更新；'+_label(result['record']['metrics']['status'])
                else:
                    total=sum(r['after_seam_count'] for r in result.get('objects',{}).values())
                    s.uvw_status=f'已显式标缝（共{total}条）；未改UV，可用于后续展开'
                self.report({'INFO'},s.uvw_status);return {'FINISHED'}
            rec=_record(context);edges={}
            for n in rec['objects']:
                o=context.view_layer.objects[n]
                if o.select_get():
                    if o.data.uv_layers.active.name!=rec['layer']:raise ValueError('请先采用当前候选编辑层：'+n)
                    ids=[e.index for e in o.data.edges if e.select and not e.hide]
                    if ids:edges[n]=ids
            if not edges:raise ValueError('没有候选对象上的选中边')
            request['seam_action']={'mode':self.mode,'edges':edges}
            result=_execute(context,request);s.uvw_status='接缝规则已更新；'+_label(result['record']['metrics']['status'])
            self.report({'INFO'},s.uvw_status);return {'FINISHED'}
        except Exception as e:self.report({'ERROR'},str(e));return {'CANCELLED'}


class UVW_OT_suggest_seams(bpy.types.Operator):
    bl_idname='uvw.suggest_seams'
    bl_label='只读预览硬边/角度接缝建议'

    def execute(self,context):
        s=context.scene
        try:
            if context.mode!='OBJECT':raise ValueError('请回到物体模式预览接缝建议')
            names=_targets(context)
            request={'version':'1.0','operation':'inspect','scene':s.name,'view_layer':context.view_layer.name,
                     'objects':names,'seam_suggestions':{'angle_degrees':s.uvw_seam_angle,'include_boundaries':s.uvw_seam_include_boundaries}}
            result=_execute(context,request)
            rows=result['seam_suggestions']['objects']
            ang=sum(r['counts']['suggested_by_angle'] for r in rows.values())
            shp=sum(r['counts']['suggested_from_sharp'] for r in rows.values())
            cur=sum(r['counts']['existing_seams'] for r in rows.values())
            s.uvw_status=f'接缝建议(只读)：按角度{ang}条 / 按锐边{shp}条 / 现有{cur}条'
            self.report({'INFO'},s.uvw_status);return {'FINISHED'}
        except Exception as e:self.report({'ERROR'},str(e));return {'CANCELLED'}


class UVW_OT_lightmap(bpy.types.Operator):
    bl_idname='uvw.generate_lightmap'
    bl_label='生成辅助 Lightmap UV2'
    bl_options={'REGISTER','UNDO'}

    def execute(self,context):
        s=context.scene
        try:
            if context.mode!='OBJECT':raise ValueError('请回到物体模式生成 Lightmap UV2，以完整撤销')
            request=_manifest(context,'lightmap')
            result=_execute(context,request)
            lm=result['lightmap']
            s.uvw_status=f'辅助层 {lm["layer"]} 完成：重叠{lm["overlap_pairs"]}，active_render 未变'
            self.report({'INFO'},s.uvw_status);return {'FINISHED'}
        except Exception as e:s.uvw_status=str(e);self.report({'ERROR'},str(e));return {'CANCELLED'}


class UVW_OT_settings(bpy.types.Operator):
    bl_idname='uvw.settings'
    bl_label='更新当前候选使用规则'
    bl_options={'REGISTER','UNDO'}
    change:bpy.props.EnumProperty(items=[(k,k,'') for k in ('MODE','DEFAULT_DENSITY','REGION','CLEAR_REGION','DIRECTION','CLEAR_DIRECTION')])

    def execute(self,context):
        s=context.scene
        try:
            if context.mode!='OBJECT':raise ValueError('请回物体模式更新规则，以完整撤销')
            rec=_record(context);value=_settings(context,rec)
            if self.change=='MODE':value.update(mode=s.uvw_usage_mode,reference=s.uvw_usage_reference)
            elif self.change=='DEFAULT_DENSITY':
                value['density'].update(default_px_m=s.uvw_target_density if s.uvw_target_enabled else None,relative_tolerance=s.uvw_density_tolerance/100)
            elif self.change in ('REGION','CLEAR_REGION'):
                capture=json.loads(s.get('uvw_edit_selection','{}'));members=_whole_members(context,rec,capture)
                keys={(m['object'],tuple(m['faces'])) for m in members}
                value['density']['regions']=[r for r in value['density']['regions'] if (r['object'],tuple(sorted(r['faces']))) not in keys]
                if self.change=='REGION':value['density']['regions'].extend({**m,'target_px_m':s.uvw_region_density} for m in members)
            else:
                capture=json.loads(s.get('uvw_edit_selection','{}'));_check_binding(context,rec,capture.get('binding'))
                _whole_members(context,rec,capture)
                loops=capture.get('edge_loops')
                if not loops or len(capture['selection'])!=1:raise ValueError('请先记录活动面上的一条有向边')
                name=next(iter(capture['selection']))
                value['directions']=[d for d in value['directions'] if not(d['object']==name and set(d['loops'])==set(loops))]
                if self.change=='DIRECTION':value['directions'].append({'object':name,'loops':loops,'angle_degrees':s.uvw_direction_angle,'tolerance_degrees':s.uvw_direction_tolerance})
            _apply_settings(context,value);_load_settings_fields(s)
            self.report({'INFO'},s.uvw_status);return {'FINISHED'}
        except Exception as e:self.report({'ERROR'},str(e));return {'CANCELLED'}


class UVW_OT_settings_file(bpy.types.Operator):
    bl_idname='uvw.settings_file'
    bl_label='布局使用规则文件'
    bl_options={'REGISTER','UNDO'}
    direction:bpy.props.EnumProperty(items=[('IMPORT','导入并检查',''),('EXPORT','导出当前规则','')])
    filepath:bpy.props.StringProperty(subtype='FILE_PATH')
    filter_glob:bpy.props.StringProperty(default='*.json',options={'HIDDEN'})

    def invoke(self,context,event):
        context.window_manager.fileselect_add(self);return {'RUNNING_MODAL'}

    def execute(self,context):
        from pathlib import Path
        try:
            if context.mode!='OBJECT':raise ValueError('请回物体模式操作规则文件')
            rec=_record(context);path=Path(self.filepath)
            if not path.is_absolute() or path.suffix.lower()!='.json':raise ValueError('请选择绝对路径JSON文件')
            if self.direction=='EXPORT':
                with path.open('x',encoding='utf-8') as f:json.dump(_settings(context,rec),f,ensure_ascii=False,indent=2,allow_nan=False)
                self.report({'INFO'},'当前布局规则已导出，保留模型绑定');return {'FINISHED'}
            if path.stat().st_size>1048576:raise ValueError('规则文件过大')
            value=json.loads(path.read_text(encoding='utf-8-sig'))
            from .policies import normalize
            from .recipes import binding,require_binding
            value=normalize(value,rec['objects'])
            require_binding(value,binding([context.view_layer.objects[n] for n in rec['objects']],rec['manifest'],context.scene.unit_settings.scale_length))
            _apply_settings(context,value);_load_settings_fields(context.scene)
            self.report({'INFO'},'规则已按原模型绑定导入并检查；没有自动重绑');return {'FINISHED'}
        except Exception as e:self.report({'ERROR'},str(e));return {'CANCELLED'}


def _load_settings_fields(s):
    rec=core.state(s)['candidates'].get(s.uvw_candidate,{})
    value=rec.get('settings',{})
    s.uvw_usage_mode=value.get('mode','EXCLUSIVE');s.uvw_usage_reference=value.get('reference','Explicit exclusive UV workflow')
    density=value.get('density',{});target=density.get('default_px_m')
    s.uvw_target_enabled=target is not None;s.uvw_target_density=target if target is not None else rec.get('manifest',{}).get('density_px_m',256)
    s.uvw_density_tolerance=density.get('relative_tolerance',.05)*100


def _recipe_policy(s):
    return {'name':s.uvw_recipe_name,'purpose':s.uvw_recipe_purpose,
        'reference':s.uvw_recipe_reference,'density_basis':s.uvw_recipe_density_basis,'stretch_p95_max':s.uvw_recipe_p95,'stretch_max':s.uvw_recipe_max,
        'density_relative_max':s.uvw_recipe_density/100,'border_px_min':s.uvw_recipe_border,'gap_px_min':s.uvw_recipe_gap}


def _load_recipe_fields(s,context=None):
    rec=core.state(s)['candidates'].get(s.uvw_candidate,{})
    p=rec.get('recipe',{}).get('policy',{})
    for key in ('name','purpose','reference'):setattr(s,'uvw_recipe_'+key,p.get(key,''))
    s.uvw_recipe_density_basis=p.get('density_basis','ISLAND_AREA_REFERENCE')
    for key,source,default,factor in (('p95','stretch_p95_max',1,1),('max','stretch_max',1,1),
            ('density','density_relative_max',.05,100),('border','border_px_min',8,1),('gap','gap_px_min',16,1)):
        setattr(s,'uvw_recipe_'+key,p.get(source,default)*factor)
    _load_settings_fields(s)
    for key in ('uvw_edit_selection','uvw_stack_reference'):
        if key in s:del s[key]


def _recipe_draft_changed(s,rec):
    bound=rec.get('recipe',{}).get('policy')
    if not bound:return True
    return any(not math.isclose(value,bound[key],rel_tol=1e-6,abs_tol=1e-7) if isinstance(value,(int,float))
               else value!=bound[key] for key,value in _recipe_policy(s).items())


def _ui_recipe(context):
    from .recipes import binding
    s=context.scene;rec=core.state(s)['candidates'].get(s.uvw_candidate)
    if not rec:raise ValueError('请先选定候选')
    return {'version':'1.0','policy':_recipe_policy(s),
        'binding':binding([context.view_layer.objects[n] for n in rec['objects']],rec['manifest'],s.unit_settings.scale_length)}


class UVW_OT_recipe(bpy.types.Operator):
    bl_idname='uvw.recipe'
    bl_label='绑定用途配方并检查'
    bl_options={'REGISTER','UNDO'}

    def execute(self,context):
        try:
            if context.mode!='OBJECT':raise ValueError('请回到物体模式绑定用途配方')
            request=_manifest(context,'recipe');request['recipe']=_ui_recipe(context)
            result=_execute(context,request)
            context.scene.uvw_status='用途配方已绑定；'+_label(result['record']['metrics']['status'])+'；视觉仍需审阅'
            self.report({'INFO'},context.scene.uvw_status);return {'FINISHED'}
        except Exception as e:self.report({'ERROR'},str(e));return {'CANCELLED'}


class UVW_OT_recipe_file(bpy.types.Operator):
    bl_idname='uvw.recipe_file'
    bl_label='用途配方文件'
    bl_options={'REGISTER','UNDO'}
    direction:bpy.props.EnumProperty(items=[('IMPORT','导入并检查',''),('EXPORT','导出当前配方','')])
    filepath:bpy.props.StringProperty(subtype='FILE_PATH')
    filter_glob:bpy.props.StringProperty(default='*.json',options={'HIDDEN'})

    def invoke(self,context,event):
        context.window_manager.fileselect_add(self);return {'RUNNING_MODAL'}

    def execute(self,context):
        from pathlib import Path
        try:
            if context.mode!='OBJECT':raise ValueError('请回到物体模式操作配方')
            path=Path(self.filepath)
            if not path.is_absolute() or path.suffix.lower()!='.json':raise ValueError('请选择绝对路径的 JSON 文件')
            if self.direction=='EXPORT':
                rec=core.state(context.scene)['candidates'].get(context.scene.uvw_candidate)
                if not rec or not rec.get('recipe'):raise ValueError('当前候选尚未绑定配方')
                with path.open('x',encoding='utf-8') as stream:json.dump(rec['recipe'],stream,ensure_ascii=False,indent=2,allow_nan=False)
                self.report({'INFO'},'配方已导出，包含本模型绑定');return {'FINISHED'}
            if path.stat().st_size>65536:raise ValueError('配方文件过大')
            request=_manifest(context,'recipe');request['recipe']=json.loads(path.read_text(encoding='utf-8-sig'))
            result=_execute(context,request);context.scene.uvw_status='配方已导入；'+_label(result['record']['metrics']['status'])
            _load_recipe_fields(context.scene)
            self.report({'INFO'},context.scene.uvw_status);return {'FINISHED'}
        except Exception as e:self.report({'ERROR'},str(e));return {'CANCELLED'}


def _draw_rules(layout,s,rec,q):
    value=rec.get('settings',{});box=layout.box();box.prop(s,'uvw_settings_open',text='布局使用与重点规则（独立于用途配方）')
    if not s.uvw_settings_open:return
    mode=value.get('mode','EXCLUSIVE');box.label(text='当前生效：'+('平铺重复' if mode=='TILED' else '独占图集'))
    box.label(text='以下为操作参数；点击对应按钮才修改，保留其余规则')
    box.prop(s,'uvw_usage_mode',text='使用方式');box.prop(s,'uvw_usage_reference',text='用途说明')
    box.operator('uvw.settings',text='保存使用方式').change='MODE'
    if mode=='TILED':box.label(text='平铺模式：图边与岛间间距不适用；仍检查岛内折叠')
    else:box.label(text=f'已声明复用组：{len(value.get("reuse_groups",[]))}；不等于独占烘焙许可')
    box.prop(s,'uvw_target_enabled',text='设置默认像素密度')
    row=box.row();row.enabled=s.uvw_target_enabled;row.prop(s,'uvw_target_density',text='默认像素/米')
    if 'density_px_m' in rec['manifest']:box.label(text=f'原候选密度目标：{rec["manifest"]["density_px_m"]:g} px/m；原门仍独立有效')
    box.prop(s,'uvw_density_tolerance',text='指定目标允许偏差%')
    box.operator('uvw.settings',text='保存默认密度与容差').change='DEFAULT_DENSITY'
    box.label(text='重点区域：记录完整岛选择后设置；仅改规则，不缩放UV')
    box.operator('uvw.capture_edit_selection',text='记录重点完整岛选择').whole_islands=True
    box.prop(s,'uvw_region_density',text='选中岛目标像素/米')
    row=box.row();row.operator('uvw.settings',text='设置选中岛目标').change='REGION';row.operator('uvw.settings',text='清除选中岛目标').change='CLEAR_REGION'
    for r in value.get('density',{}).get('regions',[]):box.label(text=f'{r["object"]} 面{r["faces"]}：{r["target_px_m"]:g} px/m')
    box.label(text='方向：先记录活动面有向边，再保存目标或执行转向')
    box.operator('uvw.capture_direction',text='记录活动面有向边')
    box.prop(s,'uvw_direction_angle',text='目标角度');box.prop(s,'uvw_direction_tolerance',text='方向允许误差°')
    row=box.row();row.operator('uvw.settings',text='设置有向边规则').change='DIRECTION';row.operator('uvw.settings',text='清除此边规则').change='CLEAR_DIRECTION'
    for d in value.get('directions',[]):box.label(text=f'{d["object"]} {d["loops"][0]}→{d["loops"][1]}：{d["angle_degrees"]:g}° ±{d["tolerance_degrees"]:g}°')
    row=box.row();row.operator('uvw.settings_file',text='导入布局规则').direction='IMPORT';row.operator('uvw.settings_file',text='导出布局规则').direction='EXPORT'
    box.label(text='导入保留原模型绑定；陌生模型拒绝，不自动重绑')
    seams=box.box();seams.label(text='当前候选接缝：先选边，再Tab回物体模式')
    row=seams.row();row.operator('uvw.seams',text='标记接缝').mode='MARK';row.operator('uvw.seams',text='清除标记').mode='CLEAR'
    row=seams.row();row.operator('uvw.seams',text='必须切开').mode='REQUIRE_CUT';row.operator('uvw.seams',text='必须连续').mode='REQUIRE_JOIN'
    seams.operator('uvw.seams',text='解除所选边硬约束').mode='FREE'
    seams.label(text='只改规则/标记，不自动移动或焊接UV')


def _draw_stack(layout,s):
    layout.prop(s,'uvw_stack_mirrored',text='镜像复用（反向角点配对）')
    layout.prop(s,'uvw_stack_group',text='复用组标识')
    layout.operator('uvw.stack_reference',text='1 记录活动面所在参考岛')
    layout.operator('uvw.stack_moving',text='2 记录移动岛并生成配对草稿')
    reference=json.loads(s.get('uvw_stack_reference','{}'))
    if reference:layout.label(text=f'参考：{reference["reference"]["object"]} 面{reference["faces"]}')
    draft=json.loads(s.get('uvw_edit_selection','{}')).get('stack_draft')
    if draft:
        layout.label(text='配对草稿：面索引排序 → 面内角点顺序；不按坐标猜测')
        if draft['mirrored']!=s.uvw_stack_mirrored:layout.label(text='镜像选项已改变，请重新生成草稿',icon='ERROR')
        for m in draft['moving_members']:layout.label(text=f'移动：{m["object"]} 面{m["faces"]}')
        for name,rows in draft['pairs'].items():
            layout.label(text=f'{name} → {draft["reference"]["object"]}，共{len(rows)}对')
            # All pairs are reviewable, including larger meshes; no hidden
            # nearest-coordinate mapping or truncated actionable draft.
            for start in range(0,len(rows),4):layout.label(text='  '.join(f'{a}→{b}' for a,b in rows[start:start+4]))
        layout.label(text='核对后点击下方执行才移动；不同拓扑拒绝')


def _transfer_object_mode(context):
    if context.mode!='OBJECT':raise ValueError('请先按Tab回物体模式；迁移记录和执行不会自动退出编辑模式')


def _transfer_source(context):
    from .transfer import source_identity
    captured=json.loads(context.scene.get('uvw_transfer_source','{}'))
    if not captured:raise ValueError('请先记录活动源对象和UV层')
    if captured['scene']!=context.scene.name or captured['view_layer']!=context.view_layer.name:
        raise ValueError('源记录属于其他场景或视图层，请重新记录')
    obj=context.view_layer.objects.get(captured['source_object'])
    if not obj or obj.type!='MESH' or obj.mode!='OBJECT':raise ValueError('记录的源网格已失效，请重新记录')
    active=obj.data.uv_layers.active
    if not active or active.name!=captured['source_uv'] or source_identity(obj,captured['source_uv'])!=captured['source_binding']:
        raise ValueError('源模型、UV或活动层已经改变，请重新记录源并生成草稿')
    return obj,captured


def _transfer_draft(context,value):
    """Validate request shape without resolving objects or replacing bindings."""
    if not isinstance(value,dict) or value.get('operation')!='transfer':raise ValueError('请选择完整的transfer请求JSON，不能导入普通配方或布局规则')
    from .contract import normalize
    normalize(value)
    if value['scene']!=context.scene.name or value['view_layer']!=context.view_layer.name:
        raise ValueError('请求属于其他场景或视图层；导入不会自动重绑')
    return value


class UVW_OT_transfer_source(bpy.types.Operator):
    bl_idname='uvw.transfer_source'
    bl_label='记录迁移源对象与UV'
    bl_options={'REGISTER','UNDO'}

    def execute(self,context):
        s=context.scene
        try:
            _transfer_object_mode(context)
            for key in ('uvw_transfer_source','uvw_transfer_draft'):
                if key in s:del s[key]
            from .transfer import source_identity
            obj=context.object
            if not obj or obj.type!='MESH' or not obj.data.uv_layers.active:raise ValueError('请将有UV的网格设为活动对象')
            layer=obj.data.uv_layers.active.name
            capture={'scene':s.name,'view_layer':context.view_layer.name,'source_object':obj.name,
                     'source_uv':layer,'source_binding':source_identity(obj,layer)}
            s['uvw_transfer_source']=json.dumps(capture,ensure_ascii=False,allow_nan=False)
            s.uvw_status=f'已记录源：{obj.name} / {layer}；请改选目标后生成草稿'
            self.report({'INFO'},s.uvw_status);return {'FINISHED'}
        except Exception as e:self.report({'ERROR'},str(e));return {'CANCELLED'}


class UVW_OT_transfer_prepare(bpy.types.Operator):
    bl_idname='uvw.transfer_prepare'
    bl_label='记录目标并生成迁移草稿'
    bl_options={'REGISTER','UNDO'}

    def execute(self,context):
        s=context.scene
        try:
            _transfer_object_mode(context)
            if 'uvw_transfer_draft' in s:del s['uvw_transfer_draft']
            source,captured=_transfer_source(context)
            targets=sorted((o for o in context.selected_objects if o.type=='MESH' and o.name!=source.name),key=lambda o:o.name)
            if not targets:raise ValueError('请选择至少一个目标网格；源对象不能同时作为目标')
            from .transfer import make_request
            candidate=s.uvw_transfer_candidate.strip() or 'transfer_'+uuid.uuid4().hex[:8]
            manifest=make_request(s,context.view_layer,source,captured['source_uv'],targets,candidate,
                mode=s.uvw_transfer_mode,width=s.uvw_transfer_width,height=s.uvw_transfer_height,
                max_distance_m=s.uvw_transfer_distance,normal_degrees=s.uvw_transfer_normal,
                uv_tolerance_px=s.uvw_transfer_uv_tolerance,pair_budget=s.uvw_transfer_budget)
            _transfer_draft(context,manifest)
            s['uvw_transfer_draft']=json.dumps(manifest,ensure_ascii=False,allow_nan=False)
            s.uvw_status=f'迁移草稿已记录：{len(targets)}个目标；全部源面对应全部目标面，尚未执行'
            self.report({'INFO'},s.uvw_status);return {'FINISHED'}
        except Exception as e:self.report({'ERROR'},str(e));return {'CANCELLED'}


class UVW_OT_transfer_execute(bpy.types.Operator):
    bl_idname='uvw.transfer_execute'
    bl_label='执行已记录迁移并检查'
    bl_options={'REGISTER','UNDO'}

    def execute(self,context):
        s=context.scene
        try:
            _transfer_object_mode(context)
            manifest=json.loads(s.get('uvw_transfer_draft','{}'))
            _transfer_draft(context,manifest)
            # The captured manifest owns all source/target bindings. Never
            # rebuild it from today's selection, source capture or UI fields.
            result=_execute(context,manifest)
            s.uvw_candidate=result.get('candidate',manifest['candidate'])
            s.uvw_status='迁移已执行；'+_label(result.get('record',{}).get('metrics',{}).get('status','unknown'))+'；用途配方未继承，外观仍需检查'
            self.report({'INFO'},s.uvw_status);return {'FINISHED'}
        except Exception as e:self.report({'ERROR'},str(e));return {'CANCELLED'}


class UVW_OT_transfer_file(bpy.types.Operator):
    bl_idname='uvw.transfer_file'
    bl_label='迁移请求文件（只记录不执行）'
    bl_options={'REGISTER','UNDO'}
    direction:bpy.props.EnumProperty(items=[('IMPORT','导入完整请求',''),('EXPORT','导出已记录请求','')])
    filepath:bpy.props.StringProperty(subtype='FILE_PATH')
    filter_glob:bpy.props.StringProperty(default='*.json',options={'HIDDEN'})

    def invoke(self,context,event):
        context.window_manager.fileselect_add(self);return {'RUNNING_MODAL'}

    def execute(self,context):
        from pathlib import Path
        try:
            _transfer_object_mode(context)
            path=Path(self.filepath)
            if not path.is_absolute() or path.suffix.lower()!='.json':raise ValueError('请选择绝对路径JSON请求文件')
            if self.direction=='EXPORT':
                manifest=_transfer_draft(context,json.loads(context.scene.get('uvw_transfer_draft','{}')))
                with path.open('x',encoding='utf-8') as stream:json.dump(manifest,stream,ensure_ascii=False,indent=2,allow_nan=False)
                self.report({'INFO'},'请求已导出，保留原模型与UV绑定；没有执行迁移');return {'FINISHED'}
            manifest=_transfer_draft(context,json.loads(path.read_text(encoding='utf-8-sig')))
            context.scene['uvw_transfer_draft']=json.dumps(manifest,ensure_ascii=False,allow_nan=False)
            context.scene.uvw_status='完整迁移请求已导入；保留原绑定，不自动重绑，也未执行'
            self.report({'INFO'},context.scene.uvw_status);return {'FINISHED'}
        except Exception as e:self.report({'ERROR'},str(e));return {'CANCELLED'}


def _draw_transfer(layout,s):
    box=layout.box();box.prop(s,'uvw_transfer_open',text='从已有UV迁移到新候选（M3）')
    if not s.uvw_transfer_open:return
    box.label(text='物体模式记录与执行；保留旧UV，新建候选；用途配方不继承')
    box.operator('uvw.transfer_source',text='1 记录活动源对象及当前UV')
    try:
        captured=json.loads(s.get('uvw_transfer_source','{}'))
        if captured:box.label(text=f'已记源：{captured["source_object"]} / {captured["source_uv"]}')
    except (ValueError,KeyError):box.label(text='源记录无效，请重新记录',icon='ERROR')
    box.prop(s,'uvw_transfer_mode',text='对应方式')
    box.label(text='面板草稿范围：全部源面 → 每个目标全部面')
    box.label(text='分区域或显式角点映射请导入完整请求，不自动猜对应')
    box.prop(s,'uvw_transfer_candidate',text='新候选编号（留空自动）')
    row=box.row();row.prop(s,'uvw_transfer_width',text='宽度');row.prop(s,'uvw_transfer_height',text='高度')
    if s.uvw_transfer_mode=='PROJECTED':
        box.prop(s,'uvw_transfer_distance',text='最大投影距离（米）');box.prop(s,'uvw_transfer_normal',text='法线最大夹角°')
        box.prop(s,'uvw_transfer_uv_tolerance',text='UV一致容差（像素）')
        box.prop(s,'uvw_transfer_budget',text='三角配对预算')
    box.label(text='距离/夹角/UV容差是测量约束，不是外观批准')
    box.operator('uvw.transfer_prepare',text='2 记录所选目标并生成绑定草稿')
    try:
        draft=json.loads(s.get('uvw_transfer_draft','{}'))
        if draft:
            detail=box.box();detail.label(text=f'已记录请求：{draft["candidate"]}；{draft["width"]}×{draft["height"]}')
            for target,row in draft['transfer'].items():
                detail.label(text=f'{row["source_object"]} / {row["source_uv"]} → {target}：{row["mode"]}')
                if row['mode']=='PROJECTED':
                    detail.label(text=f'距离≤{row["max_distance_m"]:g}m；夹角≤{row["normal_degrees"]:g}°；UV容差{row["uv_tolerance_px"]:g}px')
                    for region in row.get('regions',[]):detail.label(text=f'明确范围：源{len(region["source_faces"])}面 → 目标{len(region["target_faces"])}面')
                elif row['mode']=='EXPLICIT':detail.label(text=f'显式角点映射：{len(row["corner_map"])}项；{row["winding"]}')
                else:detail.label(text='按索引对应全部面及角点；拓扑与绑定由执行检查')
            detail.label(text='上方参数是下一份草稿；执行只使用此已记录请求')
            detail.operator('uvw.transfer_execute',text='3 执行已记录请求并检查')
    except (ValueError,KeyError,TypeError):box.label(text='草稿结构无效，请重新记录或导入完整请求',icon='ERROR')
    row=box.row();row.operator('uvw.transfer_file',text='导入完整请求（不执行）').direction='IMPORT'
    row.operator('uvw.transfer_file',text='导出已记录请求').direction='EXPORT'
    box.label(text='源或目标改变会拒绝旧绑定；导入不会重采样或自动重绑')


class UVW_PT_panel(bpy.types.Panel):
    bl_label='UV Workflow'
    bl_idname='UVW_PT_workflow'
    bl_space_type='VIEW_3D'
    bl_region_type='UI'
    bl_category='UV Workflow'
    def draw(self,context):
        l=self.layout;s=context.scene
        l.label(text='选定模型 → 新候选 → 检查 → 接续编辑')
        draw_job(l)
        l.operator('uvw.geometry_diagnostics',text='定位网格问题 / 建模交接',icon='VIEWZOOM')
        draw_surface_report(l,context)
        l.prop(s,'uvw_method',text='展开方法')
        if s.uvw_method=='MINIMUM_STRETCH':
            row=l.row(align=True);row.prop(s,'uvw_slim_iterations',text='迭代');row.prop(s,'uvw_slim_no_flip',text='防翻转')
        row=l.row(align=True);row.prop(s,'uvw_width',text='宽度');row.prop(s,'uvw_height',text='高度')
        seambox=l.box();seambox.label(text='显式接缝助手（先标缝，再按接缝展开；不隐式修改）')
        row=seambox.row(align=True);row.prop(s,'uvw_seam_angle',text='角度°');row.prop(s,'uvw_seam_include_boundaries',text='含边界')
        row=seambox.row(align=True);row.prop(s,'uvw_seam_sync_sharp',text='同步锐边');row.prop(s,'uvw_seam_clear_existing',text='先清除旧缝')
        seambox.operator('uvw.suggest_seams',text='预览接缝建议数（只读）',icon='VIEWZOOM')
        row=seambox.row(align=True)
        row.operator('uvw.seams',text='按角度标缝').mode='MARK_BY_ANGLE'
        row.operator('uvw.seams',text='从锐边标缝').mode='MARK_FROM_SHARP'
        l.operator('uvw.workflow',text='生成新候选',icon='UV').operation='generate'
        l.operator('uvw.workflow',text='复制各对象当前UV为新候选').operation='copy_uv'
        l.label(text='复制会分配新编号，保留原层；使用上方新候选图幅')
        lmbox=l.box();lmbox.label(text='辅助 Lightmap UV2（独立包，保留原 UV 与 active_render）')
        lmbox.prop(s,'uvw_lightmap_layer',text='UV2层名')
        row=lmbox.row(align=True);row.prop(s,'uvw_lightmap_box_div',text='细分');row.prop(s,'uvw_lightmap_margin_div',text='间距')
        lmbox.prop(s,'uvw_lightmap_overwrite',text='允许覆盖已有同名非渲染层')
        lmbox.operator('uvw.generate_lightmap',text='生成辅助 UVMap_Lightmap',icon='LIGHT_SUN')
        _draw_transfer(l,s)
        l.prop(s,'uvw_candidate',text='候选编号')
        saved=core.state(s);candidates=saved['candidates']
        adopted=saved.get('adopted')
        for cid,rec in candidates.items():
            box=l.box();box.operator('uvw.select_candidate',text=cid,depress=cid==s.uvw_candidate).candidate=cid
            box.label(text='上次检查：'+_label(rec['status']))
        if s.uvw_candidate in candidates:
            recipe=candidates[s.uvw_candidate]['manifest']
            l.label(text=f"候选图幅：{recipe['width']} × {recipe['height']} 像素")
            q=candidates[s.uvw_candidate]['metrics']
            l.label(text='完整数值检查：'+_label(q['status']))
            l.label(text='手改后请重新检查；采用前会核对')
            purpose=q.get('purpose_checks',{})
            l.label(text='基础网格用途数值：'+_label(purpose.get('status','not_assigned')))
            l.label(text='基础网格检查；最终表面见上方磁盘快照报告')
            if purpose.get('violations'):l.label(text='用途超限：'+', '.join(purpose['violations']))
            if purpose.get('unknown'):l.label(text='待核对：'+', '.join('模型/单位/图幅绑定已失效' if x=='binding' else x for x in purpose['unknown']))
            recipebox=l.box();recipebox.prop(s,'uvw_recipe_open',text='用途配方（只绑定当前模型）')
            if s.uvw_recipe_open:
                current=candidates[s.uvw_candidate];bound=current.get('recipe')
                if bound:
                    p=bound['policy'];b=bound['binding']
                    recipebox.label(text='已生效：'+p['name'])
                    recipebox.label(text='依据：'+p['reference'])
                    recipebox.label(text=f"拉伸 {p['stretch_p95_max']:.4g} / {p['stretch_max']:.4g}；密度 {p['density_relative_max']*100:.4g}%")
                    if current.get('settings',{}).get('mode')=='TILED':recipebox.label(text=f"图边/岛间：不适用（平铺用途）；{b['width']}×{b['height']}")
                    else:recipebox.label(text=f"图边 {p['border_px_min']:g}px / 岛间 {p['gap_px_min']:g}px；{b['width']}×{b['height']}")
                else:recipebox.label(text='当前候选未绑定用途配方')
                if _recipe_draft_changed(s,current):recipebox.label(text='下方为未生效草稿；点击绑定后才使用',icon='INFO')
                recipebox.prop(s,'uvw_recipe_name',text='配方名');recipebox.prop(s,'uvw_recipe_purpose',text='用途')
                recipebox.prop(s,'uvw_recipe_reference',text='标准依据')
                recipebox.prop(s,'uvw_recipe_density_basis',text='密度验收依据')
                row=recipebox.row();row.prop(s,'uvw_recipe_p95',text='95%上限');row.prop(s,'uvw_recipe_max',text='最大上限')
                recipebox.prop(s,'uvw_recipe_density',text='岛均密度偏差%')
                row=recipebox.row();row.enabled=current.get('settings',{}).get('mode')!='TILED'
                row.prop(s,'uvw_recipe_border',text='图边像素');row.prop(s,'uvw_recipe_gap',text='岛间像素')
                recipebox.operator('uvw.recipe',text='按填写标准绑定并检查')
                row=recipebox.row();row.operator('uvw.recipe_file',text='导入').direction='IMPORT';row.operator('uvw.recipe_file',text='导出').direction='EXPORT'
                recipebox.label(text='换模型须重新审定；数值通过不代替棋盘检查')
            if q.get('anisotropy',{}).get('complete'):
                a=q['anisotropy'];l.label(text=f"格子长短轴比：95% {a['area_p95']:.3f} / 最大 {a['max']:.3f}")
            density=q.get('density',{})
            if density.get('complete'):
                l.label(text=f"原始岛均密度：{density['reference_px_per_m']:.3f} px/m；偏差 {density['relative_max_deviation']*100:.3f}%")
            configured=density.get('configured')
            if configured:
                delta=configured.get('relative_max_deviation')
                l.label(text='指定密度目标偏差：'+(f'{delta*100:.3f}%' if delta is not None else '检查未完成'))
            basis=purpose.get('density_basis')
            if basis:l.label(text='用途密度依据：'+('按指定密度' if basis=='CONFIGURED_TARGETS' else '按岛均匀'))
            overlap=q.get('overlap',{})
            l.label(text=f'原始重叠对：{overlap.get("raw_positive_pairs",overlap.get("positive_pairs","未知"))}')
            l.label(text=f'声明复用：{overlap.get("declared_positive_pairs",0)}；意外重叠：{overlap.get("undeclared_positive_pairs",overlap.get("positive_pairs","未知"))}')
            if not overlap.get('complete',False):l.label(text='重叠检查未完成；当前计数不能视为无重叠',icon='INFO')
            if q.get('usage_policy',{}).get('mode')=='TILED':l.label(text='平铺用途：图边与岛间间距不适用（原始边界只作诊断）')
            _draw_rules(l,s,candidates[s.uvw_candidate],q)
            row=l.row();col=row.row();col.enabled=not adopted and q.get('status')=='pass'
            col.operator('uvw.workflow',text='采用以编辑').operation='adopt'
            col=row.row();col.enabled=bool(adopted and adopted['candidate']==s.uvw_candidate)
            col.operator('uvw.workflow',text='恢复').operation='restore'
            if adopted:l.label(text='正在编辑：'+adopted['candidate'])
            l.operator('uvw.workflow',text='手改后重新检查',icon='CHECKMARK').operation='validate'
            l.operator('uvw.preview',text='渲染当前视口求值棋盘',icon='RENDER_STILL')
            edit=l.box();edit.label(text='局部编辑（保留未选部分）')
            edit.prop(s,'uvw_edit_domain',text='选择范围')
            if s.uvw_edit_action=='stitch':edit.operator('uvw.capture_stitch',text='记录接缝边两侧（活动面固定）')
            elif s.uvw_edit_action=='orient':edit.operator('uvw.capture_direction',text='记录活动面有向边与完整岛')
            elif s.uvw_edit_action!='stack':edit.operator('uvw.capture_edit_selection',text='记录选择并准备编辑')
            edit.prop(s,'uvw_edit_action',text='操作')
            kind=s.uvw_edit_action
            if kind in ('translate','split'):edit.prop(s,'uvw_edit_delta',text='移动像素')
            elif kind=='rotate':edit.prop(s,'uvw_edit_angle',text='角度')
            elif kind=='scale':edit.prop(s,'uvw_edit_scale',text='比例')
            elif kind in ('mirror','align'):edit.prop(s,'uvw_edit_axis',text='方向')
            elif kind=='density':edit.prop(s,'uvw_edit_density',text='像素/米')
            elif kind in ('pin','lock'):edit.prop(s,'uvw_edit_enabled',text='固定')
            elif kind=='relax':edit.prop(s,'uvw_edit_iterations',text='放松迭代')
            elif kind=='unwrap':
                edit.prop(s,'uvw_edit_unwrap_method',text='重展方法')
                if s.uvw_edit_unwrap_method=='MINIMUM_STRETCH':
                    row=edit.row(align=True);row.prop(s,'uvw_slim_iterations',text='迭代');row.prop(s,'uvw_slim_no_flip',text='防翻转')
            elif kind=='pack':
                edit.prop(s,'uvw_pack_rotate',text='允许旋转');edit.prop(s,'uvw_pack_scale',text='允许缩放')
                edit.label(text='仅整岛；有固定岛请关闭缩放')
                edit.label(text='未选岛不避障；全候选检查决定是否通过')
                edit.label(text='长方图先使用短边内接方形区域')
            elif kind=='stitch':
                edit.prop(s,'uvw_stitch_limit',text='最大移动像素');edit.prop(s,'uvw_stitch_seams',text='接缝标记')
                edit.label(text='选择接缝边；活动面不动，不移动整岛')
            elif kind=='orient':
                edit.prop(s,'uvw_direction_angle',text='目标角度');edit.label(text='转动完整岛，使活动面有向边达到目标角度')
                capture=json.loads(s.get('uvw_edit_selection','{}'))
                if capture.get('edge_loops'):edit.label(text=f'有向角点：{capture["edge_loops"][0]} → {capture["edge_loops"][1]}')
            elif kind=='stack':_draw_stack(edit,s)
            elif kind=='merge_micro_islands':
                edit.prop(s,'uvw_micro_island_area',text='微岛面积阈值（px²）')
                edit.label(text='在 UV 贴图列表选中尚未采用的候选层')
                edit.label(text='全选面后记录选择；合并并重排，检查通过才写入')
            elif kind=='unstack':
                edit.prop(s,'uvw_unstack_offset',text='逐岛分离像素');edit.label(text='完整岛按排序依次乘以位移；更新复用声明')
            if kind in ('relax','unwrap'):edit.label(text='选面或整岛；保持选区和UV岛边界')
            if kind=='split':
                edit.label(text='选择面区域；移动所选并切开边界')
                edit.label(text='保留旧切口；位移不代表保证最小间距')
            if kind=='sync_pins':
                edit.label(text='原生固定/解固定后先重新检查，再记录选区')
                edit.label(text='所选固定点采用当前位置；锁面与未选保持')
            if kind=='straighten':edit.label(text='选择一条开放UV边链，保留端点')
            edit.operator('uvw.edit_candidate',text='执行并检查')
            edit.label(text='物体模式执行以完整撤销；Tab继续选区')
            if context.mode=='OBJECT':edit.operator('object.editmode_toggle',text='继续原生编辑（Tab）')
        box=l.box();box.label(text='新建候选约束（活动对象）')
        box.label(text='仅下次生成使用；不修改当前候选')
        box.label(text='编辑模式选区 → Tab → 记录约束')
        if context.mode=='EDIT_MESH':box.operator('object.editmode_toggle',text='准备记录约束（Tab）')
        controls=box.column();controls.enabled=context.mode=='OBJECT'
        row=controls.row();row.operator('uvw.constraints',text='必须切开').kind='must_cut';row.operator('uvw.constraints',text='禁止切开').kind='must_not_cut'
        controls.operator('uvw.constraints',text='固定选中面的 UV').kind='locked_faces'
        controls.operator('uvw.constraints',text='清除对象约束').kind='clear'
        box.label(text='当前候选请用上方固定、锁定或同步')
        if s.uvw_status:l.label(text=s.uvw_status[:90])

CLASSES=(UVW_OT_geometry_diagnostics,UVW_OT_surface_report,UVW_OT_watch_job,UVW_OT_unwatch_job,UVW_OT_workflow,UVW_OT_select,UVW_OT_constraints,UVW_OT_preview,UVW_OT_capture_edit,UVW_OT_edit,
         UVW_OT_capture_stitch,UVW_OT_capture_direction,UVW_OT_stack_reference,UVW_OT_stack_moving,
         UVW_OT_seams,UVW_OT_suggest_seams,UVW_OT_lightmap,UVW_OT_settings,UVW_OT_settings_file,UVW_OT_recipe,UVW_OT_recipe_file,
         UVW_OT_transfer_source,UVW_OT_transfer_prepare,UVW_OT_transfer_execute,UVW_OT_transfer_file,UVW_PT_panel)

def register():
    for c in CLASSES:bpy.utils.register_class(c)
    bpy.types.Scene.uvw_method=bpy.props.EnumProperty(items=[('SMART','智能投影','无硬接缝约束'),('SMART_ABF','自动接缝＋角度优化','有限候选，独立检查'),('SMART_REPAIR','投影＋边界折叠修复','三角面；自动局部切缝，仅修支持的边界扇，不支持固定UV或硬接缝约束'),('ANGLE_BASED','角度展开','保留接缝约束'),('CONFORMAL','共形展开','保留接缝约束'),('MINIMUM_STRETCH','最小拉伸 (SLIM)','防翻转最小拉伸展开（保留接缝约束）')])
    bpy.types.Scene.uvw_candidate=bpy.props.StringProperty(name='候选编号',default='',update=_load_recipe_fields)
    bpy.types.Scene.uvw_width=bpy.props.IntProperty(name='新候选宽度',default=2048,min=1,max=16384)
    bpy.types.Scene.uvw_height=bpy.props.IntProperty(name='新候选高度',default=2048,min=1,max=16384)
    bpy.types.Scene.uvw_status=bpy.props.StringProperty(name='状态',default='')
    bpy.types.Scene.uvw_edit_domain=bpy.props.EnumProperty(items=[('ISLANDS','面所在的完整岛','以选择的面定位完整UV岛'),('FACES','仅选中的面','其他面UV保持'),('LOOPS','仅选中的UV点','其他角点保持')])
    bpy.types.Scene.uvw_edit_action=bpy.props.EnumProperty(items=[(k,label,'') for k,label in [('translate','平移'),('rotate','旋转'),('scale','缩放'),('mirror','镜像'),('align','对齐'),('straighten','拉直边链'),('density','匹配密度'),('pin','固定/释放UV点'),('lock','锁定/释放区域'),('relax','局部放松'),('unwrap','固定边界重展'),('stitch','成对缝合'),('pack','整岛排布'),('sync_pins','同步所选原生固定点'),('split','切分所选面并移动'),('orient','有向边整岛转向'),('stack','显式配对复用'),('unstack','解除复用并分离'),('merge_micro_islands','合并微岛并排布')]])
    bpy.types.Scene.uvw_edit_delta=bpy.props.FloatVectorProperty(size=2,default=(0,0))
    bpy.types.Scene.uvw_edit_angle=bpy.props.FloatProperty(default=90)
    bpy.types.Scene.uvw_edit_scale=bpy.props.FloatVectorProperty(size=2,default=(1,1),min=0.000001)
    bpy.types.Scene.uvw_edit_axis=bpy.props.EnumProperty(items=[('U','U',''),('V','V','')])
    bpy.types.Scene.uvw_edit_density=bpy.props.FloatProperty(default=256,min=0.000001)
    bpy.types.Scene.uvw_edit_enabled=bpy.props.BoolProperty(default=True)
    bpy.types.Scene.uvw_edit_iterations=bpy.props.IntProperty(default=10,min=1,max=1000)
    bpy.types.Scene.uvw_edit_unwrap_method=bpy.props.EnumProperty(items=[('ANGLE_BASED','角度展开',''),('CONFORMAL','共形展开',''),('MINIMUM_STRETCH','最小拉伸 (SLIM)','')])
    bpy.types.Scene.uvw_slim_iterations=bpy.props.IntProperty(name='SLIM迭代',default=10,min=1,max=100)
    bpy.types.Scene.uvw_slim_no_flip=bpy.props.BoolProperty(name='SLIM防翻转',default=True)
    bpy.types.Scene.uvw_seam_angle=bpy.props.FloatProperty(name='硬边角度阈值°',default=60.0,min=0.0,max=180.0)
    bpy.types.Scene.uvw_seam_include_boundaries=bpy.props.BoolProperty(name='包含边界边',default=True)
    bpy.types.Scene.uvw_seam_sync_sharp=bpy.props.BoolProperty(name='同步标记锐边',default=False)
    bpy.types.Scene.uvw_seam_clear_existing=bpy.props.BoolProperty(name='先清除旧接缝',default=False)
    bpy.types.Scene.uvw_lightmap_layer=bpy.props.StringProperty(name='Lightmap UV层',default='UVMap_Lightmap')
    bpy.types.Scene.uvw_lightmap_box_div=bpy.props.IntProperty(name='Box细分',default=12,min=1,max=48)
    bpy.types.Scene.uvw_lightmap_margin_div=bpy.props.FloatProperty(name='间距比例',default=0.1,min=0.005,max=0.5)
    bpy.types.Scene.uvw_lightmap_overwrite=bpy.props.BoolProperty(name='覆盖同名层',default=False)
    bpy.types.Scene.uvw_pack_rotate=bpy.props.BoolProperty(default=True)
    bpy.types.Scene.uvw_pack_scale=bpy.props.BoolProperty(default=True)
    bpy.types.Scene.uvw_stitch_limit=bpy.props.FloatProperty(default=128,min=.001)
    bpy.types.Scene.uvw_stitch_seams=bpy.props.EnumProperty(items=[('KEEP','保留标记','数值缝合与标记分别保留'),('CLEAR_JOINED','清除已缝合边标记','仅清除此次明确缝合且已连续的边')])
    bpy.types.Scene.uvw_recipe_open=bpy.props.BoolProperty(default=False)
    bpy.types.Scene.uvw_recipe_density_basis=bpy.props.EnumProperty(items=[('ISLAND_AREA_REFERENCE','按岛均匀','保留原始岛均面积参考；不自动改为重点目标'),('CONFIGURED_TARGETS','按指定密度','需全部岛有默认或区域目标；缺失为检查未完成')])
    for key in ('name','purpose','reference'):setattr(bpy.types.Scene,'uvw_recipe_'+key,bpy.props.StringProperty(default=''))
    bpy.types.Scene.uvw_recipe_p95=bpy.props.FloatProperty(default=1,min=1,precision=4)
    bpy.types.Scene.uvw_recipe_max=bpy.props.FloatProperty(default=1,min=1,precision=4)
    bpy.types.Scene.uvw_recipe_density=bpy.props.FloatProperty(default=5,min=0,max=100)
    bpy.types.Scene.uvw_recipe_border=bpy.props.FloatProperty(default=8,min=0)
    bpy.types.Scene.uvw_recipe_gap=bpy.props.FloatProperty(default=16,min=0)
    bpy.types.Scene.uvw_settings_open=bpy.props.BoolProperty(default=False)
    bpy.types.Scene.uvw_usage_mode=bpy.props.EnumProperty(items=[('EXCLUSIVE','独占图集','未声明的跨岛重叠仍失败'),('TILED','平铺重复','明确重复用途，图边和岛间间距不适用')])
    bpy.types.Scene.uvw_usage_reference=bpy.props.StringProperty(default='Explicit exclusive UV workflow')
    bpy.types.Scene.uvw_target_enabled=bpy.props.BoolProperty(default=False)
    bpy.types.Scene.uvw_target_density=bpy.props.FloatProperty(default=256,min=.000001)
    bpy.types.Scene.uvw_region_density=bpy.props.FloatProperty(default=256,min=.000001)
    bpy.types.Scene.uvw_density_tolerance=bpy.props.FloatProperty(default=5,min=0,max=100)
    bpy.types.Scene.uvw_direction_angle=bpy.props.FloatProperty(default=0)
    bpy.types.Scene.uvw_direction_tolerance=bpy.props.FloatProperty(default=.1,min=0,max=89.999)
    bpy.types.Scene.uvw_stack_mirrored=bpy.props.BoolProperty(default=False)
    bpy.types.Scene.uvw_stack_group=bpy.props.StringProperty(default='reuse_group')
    bpy.types.Scene.uvw_unstack_offset=bpy.props.FloatVectorProperty(size=2,default=(64,0))
    bpy.types.Scene.uvw_micro_island_area=bpy.props.FloatProperty(default=64.0,min=.001,precision=2)
    bpy.types.Scene.uvw_transfer_open=bpy.props.BoolProperty(default=False)
    bpy.types.Scene.uvw_transfer_mode=bpy.props.EnumProperty(items=[('INDEXED','同拓扑索引对应','全部源面对应每个目标全部面'),('PROJECTED','受限表面投影','全源面投影到各目标全部面；分域请导入JSON')])
    bpy.types.Scene.uvw_transfer_candidate=bpy.props.StringProperty(default='')
    bpy.types.Scene.uvw_transfer_width=bpy.props.IntProperty(default=2048,min=1,max=16384)
    bpy.types.Scene.uvw_transfer_height=bpy.props.IntProperty(default=2048,min=1,max=16384)
    bpy.types.Scene.uvw_transfer_distance=bpy.props.FloatProperty(default=.001,min=0,precision=6)
    bpy.types.Scene.uvw_transfer_normal=bpy.props.FloatProperty(default=5,min=0,max=30)
    bpy.types.Scene.uvw_transfer_uv_tolerance=bpy.props.FloatProperty(default=.01,min=.000001,max=1,precision=6)
    bpy.types.Scene.uvw_transfer_budget=bpy.props.IntProperty(default=2000000,min=1,max=10000000)

def unregister():
    from .job_ui import clear
    clear()
    for name in ('uvw_method','uvw_candidate','uvw_width','uvw_height','uvw_status','uvw_edit_domain','uvw_edit_action','uvw_edit_delta',
                 'uvw_edit_angle','uvw_edit_scale','uvw_edit_axis','uvw_edit_density','uvw_edit_enabled',
                 'uvw_edit_iterations','uvw_edit_unwrap_method','uvw_slim_iterations','uvw_slim_no_flip',
                 'uvw_seam_angle','uvw_seam_include_boundaries','uvw_seam_sync_sharp','uvw_seam_clear_existing',
                 'uvw_lightmap_layer','uvw_lightmap_box_div','uvw_lightmap_margin_div','uvw_lightmap_overwrite',
                 'uvw_pack_rotate','uvw_pack_scale',
                 'uvw_stitch_limit','uvw_stitch_seams','uvw_recipe_open','uvw_recipe_name','uvw_recipe_purpose',
                 'uvw_recipe_reference','uvw_recipe_p95','uvw_recipe_max','uvw_recipe_density','uvw_recipe_border','uvw_recipe_gap',
                 'uvw_recipe_density_basis','uvw_settings_open','uvw_usage_mode','uvw_usage_reference','uvw_target_enabled',
                 'uvw_target_density','uvw_region_density','uvw_density_tolerance','uvw_direction_angle','uvw_direction_tolerance',
                 'uvw_stack_mirrored','uvw_stack_group','uvw_unstack_offset','uvw_micro_island_area',
                 'uvw_transfer_open','uvw_transfer_mode','uvw_transfer_candidate','uvw_transfer_width','uvw_transfer_height',
                 'uvw_transfer_distance','uvw_transfer_normal','uvw_transfer_uv_tolerance','uvw_transfer_budget'):
        if hasattr(bpy.types.Scene,name):delattr(bpy.types.Scene,name)
    for c in reversed(CLASSES):bpy.utils.unregister_class(c)
