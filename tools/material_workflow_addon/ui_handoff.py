# SPDX-License-Identifier: GPL-3.0-or-later
"""Explicit independent snapshot, async submission, and manual job refresh panel."""
import json
import os
from pathlib import Path
import sys
import bpy
from bpy.props import StringProperty, EnumProperty, IntProperty
from . import ui, ui_batch, handoff, job_bridge

def _environment_default(name,project=False):
    """Use explicit absolute launch defaults without creating or saving anything."""
    value=os.environ.get(name,'')
    if not value:return ''
    try:
        path=Path(value)
        if not path.is_absolute():return ''
        path=path.resolve()
        if project and (not path.is_dir() or not (path/'tools/blenderctl/cli.py').is_file()):return ''
        return str(path)
    except (OSError,RuntimeError,ValueError):
        return ''

def _backend_config(scene):
    python_binary=bpy.path.abspath(scene.mw_cli_python) if scene.mw_cli_python else None
    return job_bridge.configure(bpy.path.abspath(scene.mw_cli_root),bpy.app.binary_path,
                                python_binary=python_binary)

def _store(scene,receipt):
    scene.mw_handoff_receipt=receipt['receipt']
    scene.mw_handoff_state=receipt['state']
    scene.mw_handoff_info=json.dumps(receipt,ensure_ascii=False)
    scene.mw_handoff_candidate=receipt.get('candidate') or ''
    scene.mw_handoff_sheet=(receipt.get('sheet') or {}).get('file','')

class MW_OT_handoff(ui.MWOperator,bpy.types.Operator):
    bl_idname='material_workflow.handoff';bl_label='保存独立快照并提交后台'
    def run(self,context):
        scene=context.scene
        config=_backend_config(scene)
        mode=scene.mw_handoff_mode
        manifest=ui_batch.get_manifest(scene) if mode=='BATCH' else ui.normalize_manifest(ui._flush(scene))
        try:receipt=handoff.prepare(context,manifest,mode,bpy.path.abspath(scene.mw_handoff_root))
        except Exception as error:
            if getattr(error,'handoff_receipt',None):_store(scene,handoff.load_receipt(error.handoff_receipt))
            raise
        _store(scene,receipt)  # Even a failed/unknown submission has a recovery location.
        try: receipt=handoff.submit(receipt,config,scene.mw_handoff_timeout)
        finally: _store(scene,handoff.load_receipt(receipt['receipt']))
        self.report({'INFO'},'已提交后台；可继续编辑当前场景，点击刷新查看结果')

class MW_OT_handoff_refresh(ui.MWOperator,bpy.types.Operator):
    bl_idname='material_workflow.handoff_refresh';bl_label='读取记录 / 刷新后台状态'
    def run(self,context):
        _store(context.scene,handoff.refresh(bpy.path.abspath(context.scene.mw_handoff_receipt)))

class MW_OT_handoff_cancel(ui.MWOperator,bpy.types.Operator):
    bl_idname='material_workflow.handoff_cancel';bl_label='请求取消此后台作业'
    def run(self,context):
        _store(context.scene,handoff.cancel(bpy.path.abspath(context.scene.mw_handoff_receipt)))
        self.report({'INFO'},'已发送取消请求；请刷新确认终态')

class MW_OT_handoff_open(ui.MWOperator,bpy.types.Operator):
    bl_idname='material_workflow.handoff_open';bl_label='打开结果目录'
    def run(self,context):
        receipt=handoff.load_receipt(bpy.path.abspath(context.scene.mw_handoff_receipt))
        bpy.ops.wm.path_open(filepath=receipt['folder'])

class MW_OT_handoff_recover(ui.MWOperator,bpy.types.Operator):
    bl_idname='material_workflow.handoff_recover';bl_label='找回已接受的提交'
    def run(self,context):_store(context.scene,handoff.recover_submission(bpy.path.abspath(context.scene.mw_handoff_receipt)))

class MW_OT_handoff_retry(ui.MWOperator,bpy.types.Operator):
    bl_idname='material_workflow.handoff_retry';bl_label='重试原快照（尝试复用检查点）'
    def run(self,context):_store(context.scene,handoff.retry(bpy.path.abspath(context.scene.mw_handoff_receipt)))

class MW_PT_handoff(bpy.types.Panel):
    bl_label='独立快照与后台预览';bl_idname='MW_PT_handoff';bl_parent_id='MW_PT_panel'
    bl_space_type='VIEW_3D';bl_region_type='UI';bl_category='Material Workflow'
    def draw(self,context):
        s=context.scene;l=self.layout
        l.prop(s,'mw_cli_root');l.prop(s,'mw_cli_python');l.prop(s,'mw_handoff_root');l.prop(s,'mw_handoff_mode');l.prop(s,'mw_handoff_timeout')
        l.label(text='快照包含当前场景修改；后台结果保存在独立目录')
        l.label(text='批量只读取已提交草稿；不自动回写当前工程')
        if sys.platform.startswith('linux'):l.label(text='临时文件系统快照需及时归档；请保留完整结果目录')
        l.operator('material_workflow.handoff')
        l.prop(s,'mw_handoff_receipt');l.operator('material_workflow.handoff_refresh')
        if s.mw_handoff_state: l.label(text='后台状态：'+s.mw_handoff_state)
        if s.mw_handoff_info:
            try:
                data=json.loads(s.mw_handoff_info)
                if data.get('workflow_status'): l.label(text='工作流结果：'+data['workflow_status'])
                result=data.get('result') or {}
                if result.get('error'):l.label(text=str(result['error'].get('message',''))[:120],icon='ERROR')
                if data.get('error'):l.label(text=str(data['error'])[:120],icon='ERROR')
                if data.get('job_ref'):l.label(text='作业：'+data['job_ref']['job_id'])
                if data.get('cancel_request'):l.label(text='已请求取消；以刷新后的作业终态为准')
            except (ValueError,KeyError):l.label(text='请重新读取交接记录',icon='ERROR')
        if s.mw_handoff_candidate:l.prop(s,'mw_handoff_candidate')
        if s.mw_handoff_sheet:l.prop(s,'mw_handoff_sheet')
        row=l.row();row.operator('material_workflow.handoff_cancel');row.operator('material_workflow.handoff_open')
        l.operator('material_workflow.handoff_recover');l.operator('material_workflow.handoff_retry')
        if s.mw_last_error:l.label(text=s.mw_last_error[:120],icon='ERROR')

CLASSES=(MW_OT_handoff,MW_OT_handoff_refresh,MW_OT_handoff_cancel,MW_OT_handoff_open,MW_OT_handoff_recover,MW_OT_handoff_retry,MW_PT_handoff)
source_root=Path(__file__).resolve().parents[2]
PROPS={'mw_cli_root':StringProperty(name='Blender AI 项目目录',subtype='DIR_PATH',default=_environment_default('BLENDERCTL_ROOT',project=True) or (str(source_root) if (source_root/'tools/blenderctl/cli.py').is_file() else '')),
       'mw_cli_python':StringProperty(name='Python 解释器（可选）',description='留空时只查找当前 Blender 自带的 Python；多个候选时请指定绝对路径',subtype='FILE_PATH'),
       'mw_handoff_root':StringProperty(name='快照根目录',subtype='DIR_PATH',default=_environment_default('BLENDERCTL_SNAPSHOT_ROOT')),
       'mw_handoff_mode':EnumProperty(name='执行范围',items=[('SINGLE','单材质草稿','包含当前图层字段'),('BATCH','已提交批量草稿','先确认回写分配、目标和机位')]),
       'mw_handoff_timeout':IntProperty(name='后台期限（秒）',default=300,min=30,max=3600),
       'mw_handoff_receipt':StringProperty(name='交接记录 JSON',subtype='FILE_PATH'),
       'mw_handoff_candidate':StringProperty(name='候选工程',subtype='FILE_PATH'),
       'mw_handoff_sheet':StringProperty(name='合并预览',subtype='FILE_PATH'),
       'mw_handoff_state':StringProperty(options={'HIDDEN'}),'mw_handoff_info':StringProperty(options={'HIDDEN'})}

def register():
    for c in CLASSES:bpy.utils.register_class(c)
    for name,prop in PROPS.items():setattr(bpy.types.Scene,name,prop)
def unregister():
    for name in PROPS:
        if hasattr(bpy.types.Scene,name):delattr(bpy.types.Scene,name)
    for c in reversed(CLASSES):bpy.utils.unregister_class(c)
