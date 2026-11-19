"""Display a verified disk report without claiming it describes unsaved GUI edits."""
import hashlib
import json
import re
from pathlib import Path
import bpy

_REPORT = None
# A full two-mode declared-reuse report for the verified 332,916-triangle house
# is 87 MB. Retain a bounded reader while admitting this actual supported case.
REPORT_BYTE_LIMIT = 128 * 1024 * 1024


class UVW_OT_surface_report(bpy.types.Operator):
    bl_idname = 'uvw.surface_report'
    bl_label = '读取磁盘快照的最终表面报告'
    filepath: bpy.props.StringProperty(subtype='FILE_PATH')
    filter_glob: bpy.props.StringProperty(default='*.json', options={'HIDDEN'})

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        global _REPORT
        try:
            path=Path(self.filepath)
            if path.stat().st_size>REPORT_BYTE_LIMIT:raise ValueError('报告超过128MiB，请使用完整报告的外部查看入口')
            report=json.loads(path.read_text(encoding='utf-8-sig'))
            source=report['source'];saved=Path(bpy.data.filepath)
            if not bpy.data.filepath or Path(source['file']).resolve()!=saved.resolve():
                raise ValueError('报告必须属于当前已打开文件的磁盘快照')
            with saved.open('rb') as stream:
                sha=hashlib.file_digest(stream,'sha256').hexdigest()
            if sha!=source['sha256']:raise ValueError('磁盘文件已变更，报告失效；请重新运行 uv.inspect')
            surface=report['surface'];spec=surface['request'];samples=surface['samples']
            frames=spec.get('frames');modes=spec.get('modes')
            if (not isinstance(frames,list) or not frames or len(frames)>8 or
                any(type(f) is not int or not -1048574<=f<=1048574 for f in frames) or
                len(set(frames))!=len(frames) or not isinstance(modes,list) or not modes or
                any(m not in ('VIEWPORT','RENDER') for m in modes) or len(set(modes))!=len(modes) or
                not isinstance(samples,list) or not samples):
                raise ValueError('报告必须包含合法且不重复的帧、模式和实际样本')
            if (report.get('uv_report_version')!='1.0' or report.get('status')!='inspected' or
                report.get('checks',{}).get('saves')!='none' or report.get('checks',{}).get('source_sha256')!='passed' or
                surface!=report.get('inspection',{}).get('surface') or surface.get('controls_restored') is not True or
                surface.get('sample_count')!=len(samples) or
                len(samples)!=len(spec['frames'])*len(spec['modes']) or
                {(s['frame'],s['mode']) for s in samples}!={(f,m) for f in spec['frames'] for m in spec['modes']}):
                raise ValueError('报告缺少完整样本、身份或恢复证据')
            if any(type(s.get('complete')) is not bool for s in samples):
                raise ValueError('报告样本 complete 必须是明确的布尔值 true/false')
            aggregate='fail' if any(s['status']=='fail' for s in samples) else 'unknown' if any(s['status']!='pass' or s['complete'] is not True for s in samples) else 'pass'
            if aggregate!=surface['status']:raise ValueError('报告汇总与样本不一致')
            usage=spec.get('usage')
            if usage not in ('EXCLUSIVE','CANDIDATE_EXACT') or surface.get('usage')!=usage:
                raise ValueError('报告的布局用途不一致')
            if usage=='CANDIDATE_EXACT':
                candidate=spec.get('candidate');names=list(spec.get('layers',{}))
                if not isinstance(candidate,str) or re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,47}',candidate) is None:
                    raise ValueError('共享布局报告缺少明确候选')
                for sample in samples:
                    if sample['status']!='pass':continue
                    exact=sample.get('exact_identity',{});rows=exact.get('objects',[])
                    if (sample.get('reuse_used') is not True or sample.get('candidate')!=candidate or
                        exact.get('comparison')!='finite_numeric_values_no_tolerance' or
                        'first_mismatch' not in exact or exact['first_mismatch'] is not None or
                        len(rows)!=len(names) or {r.get('object') for r in rows}!=set(names)):
                        raise ValueError('共享布局通过样本缺少完整对应证明')
                    for row in rows:
                        sha=row.get('base_sha256')
                        if (row.get('match') is not True or 'first_mismatch' not in row or row['first_mismatch'] is not None or
                            not isinstance(sha,str) or re.fullmatch('[0-9a-f]{64}',sha) is None or row.get('evaluated_sha256')!=sha):
                            raise ValueError('共享布局通过样本的表面身份不一致')
            stat=saved.stat()
            _REPORT={'source':source,'surface':surface,'path':str(path.resolve()),'stamp':(stat.st_size,stat.st_mtime_ns)}
            self.report({'INFO'},'已读取磁盘快照报告；不代表未保存编辑通过')
            return {'FINISHED'}
        except Exception as exc:
            _REPORT=None;self.report({'ERROR'},str(exc));return {'CANCELLED'}


def draw(layout, context):
    box=layout.box()
    box.label(text='最终表面：已保存快照 / 明确帧与模式')
    box.operator('uvw.surface_report',text='读取后台检查报告')
    if not _REPORT:
        box.label(text='后台 uv.inspect 检查后，选择 uv-report.json')
        return
    source=_REPORT['source']
    if not bpy.data.filepath or Path(bpy.data.filepath).resolve()!=Path(source['file']).resolve():
        box.label(text='已切换文件，请读取本文件报告');return
    try:
        stat=Path(source['file']).stat()
        if (stat.st_size,stat.st_mtime_ns)!=_REPORT['stamp']:
            box.label(text='磁盘文件已变更，请重新检查',icon='ERROR');return
    except OSError:
        box.label(text='报告源文件无法读取',icon='ERROR');return
    surface=_REPORT['surface']
    box.label(text='快照结果：'+{'pass':'硬检查通过','fail':'未通过','unknown':'检查未完成'}.get(surface['status'],surface['status']))
    if surface['request'].get('usage')=='CANDIDATE_EXACT':
        box.label(text='按已声明共享布局检查：'+surface['request']['candidate'])
        box.label(text='共享内容可重复采样；不代表独占烘焙通过')
    if not surface['request'].get('policy'):box.label(text='未指定本次最终表面的用途数值门槛')
    for sample in surface['samples']:
        box.label(text=f"帧 {sample['frame']} / {sample['mode']}：{sample['status']}")
        if surface['request'].get('usage')=='CANDIDATE_EXACT':
            box.label(text='共享表面对应已核对' if sample.get('reuse_used') else '共享对应未获验证')
    box.label(text='快照 SHA：'+source['sha256'][:12])
    if bpy.data.is_dirty:box.label(text='当前有未保存更改；上方仅为历史快照',icon='INFO')
    box.label(text='视觉另审；不认证渲染器置换或自适应细分')
    box.label(text='工作区编辑不会自动刷新这份报告')
