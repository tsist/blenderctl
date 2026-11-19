"""Read-only monitor for an explicitly selected public UV job directory."""
import json
import time
from pathlib import Path
import bpy

_JOB = None
_TERMINAL = {'succeeded','failed','cancelled','timed_out'}
_STATES = _TERMINAL | {'queued','running','cancelling'}


def _read(path):
    if path.stat().st_size>2*1024*1024:raise ValueError('进度文件超过2MiB')
    return json.loads(path.read_text(encoding='utf-8-sig'))


def _redraw():
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type=='VIEW_3D':area.tag_redraw()


def _poll():
    if _JOB is None:return None
    try:
        root=Path(_JOB['directory']);status=_read(root/'status.json')
        if status.get('job_id')!=_JOB['job_id'] or status.get('state') not in _STATES:
            raise ValueError('作业身份或状态结构改变')
        _JOB['status']=status
        progress_path=root/'uv-progress.json'
        if progress_path.exists():
            progress=_read(progress_path)
            if progress.get('uv_progress_version')!='1.0':raise ValueError('不支持此进度版本')
            _JOB['progress']=progress
        _JOB['read_at']=time.time();_JOB['error']=None;_JOB['read_retries']=0;_JOB['retrying']=False
    except OSError as exc:
        # A Windows atomic progress write can briefly deny a concurrent reader.
        # Keep the last sample explicit and retry a bounded number of timer ticks.
        _JOB['read_retries']=_JOB.get('read_retries',0)+1
        _JOB['retrying']=_JOB['read_retries']<5
        _JOB['error']=str(exc)
    except Exception as exc:
        _JOB['error']=str(exc);_JOB['retrying']=False
    _redraw()
    if _JOB.get('retrying'):return 1.0
    if _JOB.get('error') or _JOB.get('status',{}).get('state') in _TERMINAL:return None
    return 1.0


def clear():
    global _JOB
    if bpy.app.timers.is_registered(_poll):bpy.app.timers.unregister(_poll)
    _JOB=None
    _redraw()


class UVW_OT_watch_job(bpy.types.Operator):
    bl_idname='uvw.watch_job'
    bl_label='查看后台 UV 作业进度'
    filepath:bpy.props.StringProperty(subtype='FILE_PATH')
    filter_glob:bpy.props.StringProperty(default='*.json',options={'HIDDEN'})

    def invoke(self,context,event):
        context.window_manager.fileselect_add(self);return {'RUNNING_MODAL'}

    def execute(self,context):
        global _JOB
        try:
            path=Path(self.filepath).resolve()
            if path.name!='status.json':raise ValueError('请选择公共UV作业目录中的 status.json')
            request=_read(path.parent/'request.json');status=_read(path)
            if request.get('command') not in ('uv.run','uv.inspect'):
                raise ValueError('这里读取单项 uv.run / uv.inspect；批量请使用 CLI job wait')
            if path.parent.name!=status.get('job_id') or status.get('state') not in _STATES:
                raise ValueError('作业目录、编号或状态不匹配')
            clear()
            _JOB={'directory':str(path.parent),'job_id':status['job_id'],'command':request['command'],
                  'source':request['params']['file'],'status':status,'progress':{},'error':None}
            next_interval=_poll()
            # The selected disk job is independent of the currently opened blend.
            # Keep monitoring when a different working file is opened.
            if next_interval:bpy.app.timers.register(_poll,first_interval=next_interval,persistent=True)
            return {'FINISHED'}
        except Exception as exc:
            self.report({'ERROR'},str(exc));return {'CANCELLED'}


class UVW_OT_unwatch_job(bpy.types.Operator):
    bl_idname='uvw.unwatch_job'
    bl_label='停止查看（作业继续）'
    def execute(self,context):
        clear();return {'FINISHED'}


def draw(layout):
    box=layout.box();box.operator('uvw.watch_job',text='查看后台作业进度')
    if not _JOB:return
    # Keep the action above progress rows, which can appear or disappear live.
    box.operator('uvw.unwatch_job',text='停止查看（后台作业继续）')
    status=_JOB.get('status',{});progress=_JOB.get('progress',{})
    labels={'queued':'排队','running':'运行中','cancelling':'正在取消','succeeded':'执行完成',
            'failed':'执行失败','cancelled':'已取消','timed_out':'已超时'}
    box.label(text='作业 '+_JOB['job_id'][:12]+'：'+labels.get(status.get('state'),'未知'))
    box.label(text='源文件：'+Path(_JOB['source']).name)
    phases={'source_open':'读取输入','apply':'处理UV','checking':'检查UV','solve':'展开候选',
            'complete':'执行结束','save':'保存候选','verify':'独立重开检查',
            'surface_evaluate':'求值最终表面','surface_check':'检查最终表面',
            'geometry_diagnostics':'定位网格问题','local_edit':'局部编辑'}
    if progress.get('stage'):box.label(text='最近阶段：'+phases.get(progress['stage'],progress['stage']))
    if progress.get('elapsed_seconds') is not None:box.label(text=f"阶段记录耗时：{progress['elapsed_seconds']:.1f} 秒")
    if status.get('heartbeat_at') and status.get('state') not in _TERMINAL:
        box.label(text=f"状态更新于 {max(0,time.time()-status['heartbeat_at']):.0f} 秒前")
    if _JOB.get('error'):box.label(text=('暂时不可读，正在重试：' if _JOB.get('retrying') else '读取已停止：')+_JOB['error'][:80],icon='ERROR')
    box.label(text='作业状态不代表UV质量或当前工作区通过')
