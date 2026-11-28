"""Bind action navigation to the editions selected by the actual launchers."""

import json
import os
from pathlib import Path


HOST_52 = '5.2.1'
HOST_53 = '5.3.0'
PROJECT_HOSTS = (HOST_52, HOST_53)
ANIMATION_EDITION_COMMANDS = {
    'animation fk-character', 'animation event-store', 'animation pose-edit',
    'animation panel-events', 'animation project-events', 'animation record-live',
    'animation channel-inspect', 'animation channel-edit', 'animation inspect', 'animation run', 'animation clip-apply',
}
VIDEO_RELEASES = {
    '面板 0.1.2 + 分发 0.1.4（CLI 0.63.0）': '0.1.4',
    '面板 0.1.2 + 分发 0.1.5（CLI 0.63.0）': '0.1.5',
}
PHYSICS_HOSTS = {
    '0.8.1-dev.1 + 独立 CLI v1': HOST_52,
    '0.8.1-dev.2 + 公共物理 CLI': HOST_53,
}
MATERIAL_EDITIONS = {
    '0.9.1 / CLI 0.91.0': ('0.9.1', (HOST_52,)),
    '5.3 配置 GUI 0.8.0 / 公共 CLI 0.91.0': ('0.8.0', (HOST_53,)),
    '0.9.2 / CLI 0.91.0': ('0.9.2', PROJECT_HOSTS),
}
MATERIAL_REQUEST_EDITORS = {
    'read-current', 'capture-faces', 'edit-layer-draft', 'load-layer-template',
    'save-procedural-template', 'load-procedural-template', 'create-procedural',
    'update-procedural', 'export-batch-draft', 'edit-batch-panel',
    'edit-batch-assignment', 'commit-batch-assignment',
}
HARD_SURFACE_PREVIOUS_LOCAL_EDITION = '本地发行 0.3.4 / CLI 0.91.0'
HARD_SURFACE_LOCAL_EDITION = '本地发行 0.3.7 / CLI 0.91.0'
HARD_SURFACE_EDITIONS = {
    '公开发行 0.3.1 / CLI 0.91.0': '0.3.1',
    '公开发行 0.3.2 / CLI 0.91.0': '0.3.2',
    HARD_SURFACE_PREVIOUS_LOCAL_EDITION: '0.3.4',
    '本地发行 0.3.5 / CLI 0.91.0': '0.3.5',
    '本地发行 0.3.6 / CLI 0.91.0': '0.3.6',
    HARD_SURFACE_LOCAL_EDITION: '0.3.7',
}


def require_host(host, supported, product):
    if host in supported:
        return None
    if host is None:
        reason = f'{product} 的版本对应只为工作区的已核对 Blender 构建提供。请先选择对应宿主。'
    else:
        reason = f'{product} 在所选宿主上没有这项发行版入口。'
    result = {'reason': reason}
    if len(supported) == 1 and supported[0] == HOST_53:
        result['suggested_host'] = supported[0]
    return result


def toon_route(edition, host):
    supported = {'0.51.1': HOST_52, '0.52.1': HOST_53}
    unavailable = require_host(host, (supported[edition],), f'Toon Workflow {edition}')
    if unavailable:
        return unavailable
    variable = 'BLENDERCTL_TOON_BUNDLE_521' if host == HOST_52 else 'BLENDERCTL_TOON_BUNDLE'
    override = os.environ.get(variable)
    if override:
        bundle = Path(override)
        try:
            manifest = json.loads((bundle / 'bundle.json').read_text(encoding='utf-8-sig')) if bundle.is_absolute() else None
        except (OSError, ValueError):
            manifest = None
        if (not isinstance(manifest, dict) or manifest.get('version') != edition
                or manifest.get('schema') != 'toon-unified-bundle.v1'
                or not (bundle / 'toon_workflow_cli_v1/cli.py').is_file()):
            return {'reason': f'{variable} 当前覆盖的目录无法核对为 Toon Workflow {edition}。'}
    return {'context': f'按宿主选择 Toon {edition}；保持当前 {variable} 选择，先用同一宿主的 toon-workflow describe 核对 version 与实现指纹。会话动作还要核对已连接的面板。'}


def animation_route(edition, action, command, host):
    if action == 'support.cap-copy' and edition == '0.4.4':
        return require_host(host, (HOST_53,), '动画支撑补偿工程交接') or {
            'session_open_fields': {'edition': edition},
            'request_fields': {'operation': 'status', 'params': {}},
            'context': '沿用补偿生成会话和作业目录，给新查询独立的 request_id。核对 read_only.job_id 和已验状态，从 artifacts.candidate 读取路径及 SHA。',
        }
    if action.startswith('support.'):
        if edition == '0.4.3':
            return {'reason': '动画支撑 0.4.3 的动作证据依赖原 Linux x86_64 固定构建，Windows 新会话请选择 0.4.4。'}
        unavailable = require_host(host, PROJECT_HOSTS, f'动画支撑 {edition}')
        return unavailable or {
            'session_open_fields': {'edition': edition},
            'context': f'仅在 open 请求指定 edition={edition}；后续请求用原 session_id、作业目录及 Blender，核对 open 回执 data.host.edition 与 build。',
        }
    if edition == '0.4.4' and command in ANIMATION_EDITION_COMMANDS:
        if host == HOST_53 and command == 'animation project-events':
            return {'context': '用 event_id 定位接触帧。写入前 review 同一源与参与者，将计划和 SHA 提交到新工作副本，再 inspect 检查事件、锁及关系。'}
        if host == HOST_53 and command in {'animation inspect', 'animation run'} and action.startswith('clip-event.rig.'):
            return {'context': '使用 5.3.0 的四组参与者清单，从当前工程捕获再接续改时。保持源、原生绑定和实现指纹，复查窗口外的手调键。'}
        if host == HOST_53 and command == 'animation clip-apply' and action in {'asset.project.source', 'asset.project.override'}:
            return {'context': '选定四角色片段库与接收工程，采用源放置或显式覆盖放置。核对资源和实现指纹，接续独立重开后的集合、事件与 Action。'}
        if host == HOST_53 and action == 'template.practice' and command == 'animation fk-character':
            return {'context': '对本版已保存的受管双段 FK 人形指定三个练习帧、起止和峰值膝角，将独立 Action 保存到新工程。'}
        if host == HOST_53 and command == 'animation event-store':
            return {'context': '在 Windows Blender 5.3.0 使用 0.4.4 的双角色、角色与灯光或 Cue 入口。用参与者身份与已保存关键帧规划事件，核对帧率和目标后提交同一计划及 SHA。'}
        if host == HOST_53 and command == 'animation panel-events' and action in {
            'head-motion.paired-explicit.plan', 'head-motion.paired-explicit.apply',
            'clip-event.rig.capture.review', 'clip-event.rig.retime.review',
        }:
            return {'context': '按已保存骨架、关键帧及参与者预审。转头提交需沿用当前计划和 SHA，片段改时需匹配当前工程的捕获记录。'}
        pose_53 = command == 'animation pose-edit' and action in {
            'pose.set_key', 'pose.move_key', 'pose.delete_key', 'pose.prepare-axis', 'pose.retime-action', 'pose.capture-batch',
        }
        needs_pose_writer = (
            command in {'animation pose-edit', 'animation event-store', 'animation project-events'}
            or action == 'template.practice'
            or action.startswith(('head-motion.', 'clip-event.rig.', 'asset.project.'))
        )
        hosts = (HOST_52,) if needs_pose_writer and not pose_53 else PROJECT_HOSTS
        if pose_53 and host == HOST_53:
            return {'context': '在 Windows Blender 5.3.0 使用 0.4.4 的本地兼容入口。核对本次计划的作用范围与 SHA；成功回执含 runtime.compatibility_variant。'}
        return require_host(host, hosts, f'动画 {edition} {action}') or {
            'context': ('在已连接且启用 0.4.4 的三维视口使用 --edition 0.4.4，并沿用会话 ID。'
                        if command == 'animation record-live' else
                        f'使用 --edition {edition}，按实际回执核对发行版与 Blender 构建。'),
        }
    restricted = ('fk-character', 'event-store', 'pose-edit', 'panel-events', 'record-live', 'project-events')
    if edition == '0.4.3' and any(name in command.split() for name in restricted):
        return require_host(host, (HOST_52,), '动画曲线 0.4.3 的面板适配') or {}
    return {}


def material_route(edition, command, host):
    version, supported = MATERIAL_EDITIONS[edition]
    unavailable = require_host(host, supported, f'Material Workflow {version}')
    if unavailable:
        return unavailable
    tokens = command.split()
    operation = tokens[1] if len(tokens) > 1 and tokens[0] == 'material' else ''
    if operation in MATERIAL_REQUEST_EDITORS:
        return {'request_fields': {'edition': version},
                'context': '在 --params 请求 JSON 指定 edition；用产物报告的 product.edition 和 runtime.build 核对。'}
    if operation == 'handoff-live':
        return {'context': f'连接实际启用了 Material Workflow {version} 的会话；从 session.json 核对 edition、宿主和会话 ID。'}
    if operation in {'recover-submission', 'retry-submission'} or command == 'job cancel':
        return {'context': f'使用 Material Workflow {version} 原交接单或作业身份，并沿用原作业目录。消费端由公共 CLI 实现。'}
    if operation.startswith('projection-') or operation in {'run', 'batch', 'template-save', 'procedural-export'}:
        return {'context': f'目录版号 {version} 对应面板来源或动作证据；此命令由当前工作区公共处理器消费保存输入，核对源工程、清单和输出回执。'}
    return {}


def edition_route(product_id, row, host):
    """Return launcher options, request constraints, or a reason no route can be offered."""
    edition, action, command = (row[key] for key in ('version', 'action_id', 'cli_command'))
    if product_id == 'video' and edition in VIDEO_RELEASES:
        release = VIDEO_RELEASES[edition]
        return require_host(host, PROJECT_HOSTS, f'Video {release}') or {
            'globals': ['--edition', f'video@{release}'],
            'context': f'显式锁定分发 {release}，以 describe 的 contract.file 核对。恢复旧作业时沿用该分发和原作业目录。',
        }
    if product_id == 'physics' and edition in PHYSICS_HOSTS:
        return require_host(host, (PHYSICS_HOSTS[edition],), f'Physics {edition}') or {
            'context': '公共入口按 Blender 选择物理实现；用 physics describe 核对 data.blender_binary 和 data.capabilities.implementation_version。控制历史作业时沿用原清单。',
        }
    if product_id == 'toon' and edition in {'0.51.1', '0.52.1'}:
        return toon_route(edition, host)
    if product_id == 'animation':
        if edition in {'0.4.3', '0.4.4'}:
            return {'reason': '本行保留历史动画版本的审计证据。当前生产工具使用 Animation Curves 0.5.0。'}
        return animation_route(edition, action, command, host)
    if product_id == 'modeling' and edition == '0.1.14':
        return {'reason': '本行保留 0.1.14 的审计证据。当前 GLB 和 FBX 导入走工作区 Modeling 入口。'}
    if product_id == 'camera' and edition == 'Windows 0.8.1':
        return require_host(host, (HOST_52,), f'Camera Workflow {edition}') or {
            'context': '静态相机副本使用项目已验的 5.2.1。沿用原事务与资格包身份。现场打开还需连接同一资格包的 Blender 窗口。',
        }
    if product_id == 'camera' and edition == 'Windows 0.8.3':
        restriction = require_host(host, (HOST_53,), f'Camera Workflow {edition}')
        if restriction:
            return restriction
        if command.startswith('camera session-live '):
            return {'context': '附接实际载入 0.8.3 发行的 Blender 窗口，沿用同一会话身份。'}
        if command.startswith('camera shot-table'):
            return {'context': '使用已保存的工程、源 SHA 和明确场景。镜头表变更保存到具名新副本。'}
        if command.startswith(('camera copy', 'blenderctl.ps1 camera copy')):
            return {'reason': '0.8.3 的旧副本入口已归档。当前工程使用 1.0 Camera Workflow。'}
        return {}
    if product_id == 'render-batch' and edition == '本地批渲染 0.3.1-dev':
        return require_host(host, PROJECT_HOSTS, edition) or {
            'globals': ['--edition', 'render-batch@0.3.1-dev'],
            'context': '恢复时沿用提交时的 Blender、作业根和批次编号。',
        }
    if product_id == 'render-batch' and command.startswith('render batch-panel '):
        return require_host(host, PROJECT_HOSTS, edition) or {}
    if product_id == 'material' and edition in MATERIAL_EDITIONS:
        return material_route(edition, command, host)
    if product_id == 'hard-surface' and edition in HARD_SURFACE_EDITIONS:
        version = HARD_SURFACE_EDITIONS[edition]
        unavailable = require_host(host, (HOST_53,), 'Hard Surface Workflow')
        if unavailable:
            return unavailable
        if 'panel-controls' in command.split():
            request_fields = {'edition': version}
            sweep_operations = {'hard_surface.surface_sweep_panel_inspect': 'inspect-sweep',
                                'hard_surface.surface_sweep_panel_commit': 'commit-sweep'}
            if action in sweep_operations:
                request_fields['operation'] = sweep_operations[action]
            return {
                'request_fields': request_fields,
                'context': '在 --params 请求 JSON 指定 edition；核对回执 data.edition 与 data.release.zip_sha256。',
            }
        if edition == HARD_SURFACE_LOCAL_EDITION:
            return {'context': f'本地 {version} 的公共建模工具由当前工作区实现，仅支持 Blender 5.3.0。用 model flow-tools 核对工具参数和版本。'}
        return {'context': '该行保留历史版证据。公共建模命令使用当前工作区的 5.3.0 实现。'}
    if product_id == 'rigging' and edition == '0.42.3-rc1':
        if action == 'c94_workflow.inspection_drag' and host == HOST_53:
            return {
                'request_fields': {'operation': 'inspection-drag'},
                'context': '从已保存的定位工程提交原生编辑结果。用 markers 或 endpoints 指定端点，提供世界坐标 delta，或提供 surface、selected_only、offset 和 ray。输出写入新的工作副本。',
            }
        if action == 'cli.rigging.volume.check':
            return require_host(host, (HOST_53,), f'Rigging Workflow {edition}') or {
                'request_fields': {'operation': 'volume-check'},
                'context': '只读检查实际宿主、目标、闭合代理、骨列与体积预算。返回 ready_for_review 后核验本次骨架与作用域。',
            }
        return require_host(host, (HOST_52,), f'Rigging Workflow {edition}') or {
            'context': '使用具名 RC 适配器及冻结的 0.42.3-rc1。会话动作还要核对实际连接的 Blender 与插件。',
        }
    return {}
