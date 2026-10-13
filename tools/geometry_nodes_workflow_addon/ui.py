"""Small workflow status panel. Public values remain standard modifier inputs."""
import bpy
from pathlib import Path


class GNW_OT_session_start(bpy.types.Operator):
    bl_idname = 'gnw.session_start'
    bl_label = 'Start CLI Session'
    bl_description = 'Start an owned queue with the selected matching CLI implementation'

    def execute(self, context):
        from . import session
        try:
            settings = context.window_manager
            session.start(settings.gnw_session_directory, settings.gnw_cli_runtime_directory or None)
        except Exception as exc:
            self.report({'ERROR'}, str(exc)); return {'CANCELLED'}
        return {'FINISHED'}


class GNW_OT_session_stop(bpy.types.Operator):
    bl_idname = 'gnw.session_stop'
    bl_label = 'Stop CLI Session'
    bl_description = 'Stop this queue while retaining checkpoints and results'

    def execute(self, context):
        from . import session
        session.stop(); return {'FINISHED'}


class GNW_PT_workflow(bpy.types.Panel):
    bl_label = 'Geometry Workflow'
    bl_idname = 'GNW_PT_workflow'
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Geometry'

    def draw(self, context):
        from . import session
        from .core import ROLE, KEY
        layout = self.layout; obj = context.object
        layout.label(text='Target: ' + (obj.name if obj else 'None'))
        if obj:
            for mod in obj.modifiers:
                if mod.type == 'NODES' and mod.node_group:
                    group = mod.node_group
                    layout.label(text=mod.name + ': ' + ('Managed core / user wrapper' if group.get(ROLE) == 'wrapper' else 'Unmanaged'))
                    if group.get(KEY): layout.label(text='ID: ' + str(group.get(KEY)))
        info = session.status(); last = info['last']
        if info['session']:
            layout.operator('gnw.session_stop')
        else:
            layout.prop(context.window_manager, 'gnw_cli_runtime_directory', text='CLI directory')
            layout.prop(context.window_manager, 'gnw_session_directory', text='New queue')
            layout.operator('gnw.session_start')
        layout.label(text='Session: ' + ('Connected queue' if info['session'] else 'Not started'))
        layout.label(text='Task: ' + str(last.get('status', 'idle')))
        if last.get('ok') is False: layout.label(text='Check: ' + str(last.get('error', 'Failed'))[:100], icon='ERROR')
        if last.get('candidate'): layout.label(text='Candidate: ' + last['candidate'])
        layout.label(text='Parameters: standard modifier panel')


def register():
    source_runtime = Path(__file__).resolve().parents[1] / 'blenderctl'
    bpy.types.WindowManager.gnw_cli_runtime_directory = bpy.props.StringProperty(
        name='CLI runtime directory', subtype='DIR_PATH', default=str(source_runtime) if source_runtime.is_dir() else '')
    bpy.types.WindowManager.gnw_session_directory = bpy.props.StringProperty(name='New session queue directory', subtype='DIR_PATH')
    for cls in (GNW_OT_session_start, GNW_OT_session_stop, GNW_PT_workflow): bpy.utils.register_class(cls)


def unregister():
    for cls in (GNW_PT_workflow, GNW_OT_session_stop, GNW_OT_session_start): bpy.utils.unregister_class(cls)
    del bpy.types.WindowManager.gnw_session_directory
    del bpy.types.WindowManager.gnw_cli_runtime_directory
