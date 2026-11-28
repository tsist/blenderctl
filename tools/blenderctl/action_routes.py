"""Resolve an exact catalog action to an argument vector and a read-only invocation."""
import shutil
import sys
from pathlib import Path

from action_editions import ANIMATION_EDITION_COMMANDS, edition_route, require_host


DOMAINS = {
    "modeling": ("modeling",), "hard-surface": ("model",), "uv": ("uv",), "material": ("material", "job"),
    "asset-library": ("asset", "transaction", "scene"), "geometry": ("geometry",), "rigging": ("rigging-workflow",),
    "animation": ("animation",), "physics": ("physics",), "camera": ("camera",), "lighting": ("lighting",),
    "toon": ("toon-workflow", "job"), "compositing": ("compositing",), "render-batch": ("render",),
    "hair": ("hair",),
}

# Each list follows the order of the operations in the catalog. Callers supply
# their own project paths and IDs. No text from a parameter description is executed.
SEQUENCES = {
    "uv export-policy → uv import-policy": ("uv export-policy", "uv import-policy"),
    "material projection-bake → material projection-attach": ("material projection-bake", "material projection-attach"),
    "asset plan / transaction apply": ("asset plan", "transaction apply"),
    "geometry session capture + scene inspect + geometry inspect": (
        "geometry session --session-action capture", "scene inspect", "geometry inspect"),
    "compositing depth-control submit/read + job result": (
        "compositing depth-control submit", "compositing depth-control read", "job result"),
    "compositing preview-copy + compositing depth-control submit/read": (
        "compositing preview-copy", "compositing depth-control submit", "compositing depth-control read"),
}


def public(arguments, **extra):
    return {"runner": "public", "arguments": arguments.split(), **extra}


def public_descriptors(product, row):
    command = row["cli_command"]
    action = row["action_id"]
    if command in SEQUENCES:
        return [public(text, relation="step") for text in SEQUENCES[command]]
    if product == "animation" and row["version"] == "0.4.4" and command in ANIMATION_EDITION_COMMANDS:
        return [public(command + " --edition 0.4.4")]
    if product == "video":
        if action == "vwe.inspect":
            return [public("video inspect")]
        if action in {"cli.video.ripple", "cli.video.otio.export", "cli.video.otio.import", "cli.video.run"}:
            return [public(action.removeprefix("cli.").replace(".", " "))]
        if action in {"vwe.strip.name", "vwe.strip.lock", "vwe.strip.mute", "cli.video.select_candidate"}:
            return [public("video run"), public("request", relation="alternative",
                    context="JSON command: video.run。含 MOVIE 的纯剪辑可在 params.media_probe 指定媒体探测。")]
        return []
    if product == "render-batch" and "render batch " in command:
        options = command.split()
        operation = options[options.index("batch") + 1]
        if operation not in {"submit", "status", "wait", "resume", "cancel"}:
            return []
        entries = [public("render batch " + operation)]
        if "blenderctl request" in command:
            entries.append(public("request", relation="alternative", context="JSON command: render.batch." + operation))
        return entries
    if product == "toon" and command.startswith("--async request "):
        direct = command.partition(" / ")[2].removesuffix(" --params")
        if direct in {"toon-workflow edit", "toon-workflow run"}:
            return [public(direct), public("request", globals=["--async"], relation="alternative")]
    if product == "physics" and command == "python -B tools/physics_workflow_cli_v1/cli.py run":
        return [public("physics run")]
    if product == "geometry" and command == "geometry session":
        operation = {"gnw.display.status": "status", "cli.geometry.session.capture": "capture",
                     "cli.geometry.session.apply": "apply"}.get(action)
        if operation:
            return [public("geometry session --session-action " + operation)]
    command = command.removeprefix("blenderctl.ps1 ").removeprefix("blenderctl ")
    command = command.removeprefix("--blender-version 5.2.1 ")
    words = command.split()
    if len(words) < 2 or words[0] not in DOMAINS.get(product, ()):
        return []
    if words[:2] == ["rigging-workflow", "phase3"] and len(words) == 3:
        if words[2] in {"describe", "run", "live-open", "live-status", "live-capture", "live-result", "live-close"}:
            return [public(command)]
        return []
    if words[:2] in (["asset", "preview"], ["compositing", "depth-control"]):
        return [public(" ".join(words[:3]))] if len(words) == 3 else []
    if not words[1].replace("-", "").isalpha():
        return []
    fixed = words[:2]
    # Only include switches with known literal values. Hashes, paths and JSON
    # belong to the caller even when example notation appears in cli_command.
    if words[2:] in (["--live"], ["--normal"], ["--upgrade-v2"]):
        fixed += words[2:]
    elif len(words) == 4 and words[2] == "--operation" and words[3].replace("-", "").isalpha():
        fixed += words[2:]
    elif (len(words) > 2 and not words[2].startswith(("--", "<"))) or any(
            word in {"/", "+", "→"} for word in words[2:]):
        return []
    return [public(" ".join(fixed))]


def attach_launchers(row, product_id, host, blender, root, external):
    mapping = external.get(product_id, {})
    edition_mapping = mapping.get("editions", {}).get(row["version"], {})
    edition_descriptors = edition_mapping.get("actions", {}).get(row["action_id"])
    panel = edition_mapping.get("panel_dispatch", {})
    panel_route = None
    variant = panel.get("variants", {}).get(row["action_id"])
    operator = variant["operator"] if variant is not None else row["action_id"]
    supports_panel = host in panel.get("hosts", ())
    dispatchable = operator in panel.get("operators", ())
    if supports_panel and (dispatchable or operator in panel.get("schema_only", ())):
        row["native_schema_argv"] = [sys.executable, "-B", str(root / panel["entry"]), "describe",
                                     "--operator", operator, "--blender-version", host]
    if supports_panel and dispatchable:
        request_fields = {"operator": operator}
        if variant is not None:
            request_fields["parameters"] = variant["parameters"].copy()
        panel_route = {
            "runner": "python", "entry": panel["entry"], "arguments": ["dispatch"],
            "help_arguments": ["describe", "--operator", operator, "--blender-version", host],
            "request_fields": request_fields,
            "session_open_arguments": ["open", "--blender", blender],
            "context": panel["context"],
        }
        if operator == "c94_workflow.contact_detection_background":
            panel_route["session_open_fields"] = {"--background-detection": True}
            panel_route["context"] = ("生成足底报告时给 session_open_argv 加 --background-detection、--file、"
                                      "--expected-sha256 和新的 --session-dir。在会话内配置 contact-detection "
                                      "后执行 ENQUEUE，后续请求沿用该作业和会话。读取已保存的报告也可使用原生面板会话。")
        if operator == "c94_workflow.contact_correction_background":
            panel_route["session_open_fields"] = {"--background-correction": True}
            panel_route["context"] += (" 以 --background-correction 打开受管副本，在 contact-correction 选择原目标、"
                                       "报告、校准、问题 ID 与帧段。使用本会话的 correction-jobs 目录生成独立候选。"
                                       "预览后先 STOP 再保存。正式采用由用户在可见窗口逐问题确认。")
        if operator in {"c94_workflow.elastic_action", "c94_workflow.surface_dynamic_action"}:
            group = "elastic" if operator.endswith(".elastic_action") else "surface-dynamics"
            panel_route["session_open_fields"] = {"--background-dynamics": True}
            panel_route["context"] += (f" 使用 --background-dynamics 打开已保存的角色副本。在 {group} 组选择"
                                       "骨架和已有动态组，READ 后核对参数、采样帧段和输出名称，CAPTURE 后可 BAKE。"
                                       "交接时核查原动作及冷开后的次级运动。")
        if operator in {"c94_workflow.retarget_mmd_scene", "c94_workflow.retarget_mmd_background"}:
            panel_route["session_open_fields"] = {"--background-mmd": True}
            panel_route["context"] += (" 已登记的 MMD 动作可在 --background-mmd 会话重放。用 retarget-mmd-scene "
                                       "选登记来源、profile Text、clip、源帧率和目标。后台队列用 retarget-mmd-job "
                                       "及本会话 mmd-jobs 目录。收集候选后核对 Action、Slot、目标时间与恢复绑定。")
        if operator in panel.get("mmd_camera_session_operators", ()):
            panel_route["session_open_fields"] = {"--background-mmd-camera": True}
            panel_route["context"] = ("用 --background-mmd-camera 接手保存副本。在 mmd-camera-build 组设置会话自己的 camera-jobs 目录。"
                                      "pick_vmd 传带 SHA 的 VMD；构建与取消在同一会话执行。收集成功后追加、释放并另存。")
        if operator == "c94_workflow.author_rework":
            panel_route["session_open_fields"] = {"--background-author-rework": True}
            panel_route["context"] = ("用 --background-author-rework 打开保存副本，在 author-rework 组选择旧骨架和参考。"
                                      "CAPTURE 接收明确的网格身份，PREPARE 接实际模块配置 ID 和带 SHA 的模板，"
                                      "CANCEL 结束影响计划。")
        if operator in {"c94_workflow.skin_parts_capture", "c94_workflow.skin_parts_plan"}:
            panel_route["session_open_fields"] = {"--background-skin-parts": True}
            panel_route["context"] += (" 在 --background-skin-parts 会话的 skin-parts 设置组选择已有绑定的源和骨架。"
                                       "捕获保存的可见选点，或用 inspect_selection 获取源和拓扑身份后传入明确 RAW 点。"
                                       "PREPARE 给出选点覆盖的完整源面与裁面参数。取消或保存后需重新准备。")
        if (operator == "c94_workflow.skinning_policy_plan" and variant is not None
                and variant["parameters"] == {"action": "CANCEL"}):
            panel_route["session_open_fields"] = {"--background-skin-parts": True}
            panel_route["context"] = ("用 --background-skin-parts 打开保存副本。提交 action=CANCEL 清除旧计划和报告，"
                                      "以 before/after 核对报告、作用点及已有预览。")
        if operator in panel.get("volume_session_operators", ()):
            panel_route["session_open_fields"] = {"--background-volume": True}
            panel_route["request_fields"]["parameters"] = {}
            family = "多域" if operator.startswith("c94_workflow.skinning_plan_") else "单域"
            step = operator.rsplit("_", 1)[1]
            if step in {"start", "apply"}:
                instructions = ("先以相同请求参数调用 review-scope，确认输入指纹和审核范围。"
                                "dispatch 时附上 skeleton_review 的 file 与 sha256。"
                                "采用必须沿用启动时的会话、作业及审核回执。")
            else:
                instructions = ("dispatch 沿用启动该作业时的 session-dir、session-id 和当前上下文。"
                                "轮询、取消及结束无需提交 skeleton_review。文件重开后必须重新启动作业。")
            panel_route["context"] = f"在 --background-volume 中接手已填好{family}面板的保存副本。{instructions}"
        if operator in panel.get("volume_setup_operators", ()):
            panel_route["session_open_fields"] = {"--background-volume": True}
            panel_route["context"] = ("通过 --background-volume 打开保存的工作副本。按 describe 的 "
                                      "execution.volume_setup 提交此动作的参数，读取原生执行与观察回执。")
        if operator == "c94_workflow.local_skinning_action":
            panel_route["session_open_fields"] = {"--background-volume": True}
            panel_route["context"] = ("局部体积模式用 --background-volume 接手已保存的目标、骨架、代理和选区。"
                                      "用 parameters.action=CHECK 预检；START/APPLY 用 review-scope 取审核范围。"
                                      "同会话 dispatch 时附相符的骨架审核回执。绘制分支的现场条件见 describe。")
        if operator == "c94_workflow.retarget_mmd_import":
            panel_route["session_open_fields"] = {"--background-mmd-import": True}
            panel_route["context"] = ("通过 --background-mmd-import 打开工作副本，按 describe 提供 PMX/VMD 和 SHA，"
                                      "从同一会话收集实际导入的骨架和动作。")
        if (operator == "c94_workflow.native_maintenance_action"
                and (variant is None or variant["parameters"].get("action") in
                     {"CLONE", "REPAIR", "KEY_CONTROLS", "READ", "ADD_CONTROL", "ADD_PROPERTY", "ADD_KEY", "APPLY_LAYOUT",
                      "SELECT_PACK", "SELECT_CONTROL", "READ_BONE", "READ_VERTICES"})):
            panel_route["session_open_fields"] = {"--background-maintenance": True}
            panel_route["context"] += (" 用 --background-maintenance 接手已有维护包。从 status.maintenance_keys "
                                       "取具名骨架身份、帧和映射 SHA。按 describe 选择角色、采集骨位或点域、编辑布局或记录动作。")
            if variant is not None and variant["parameters"].get("action") == "CLONE":
                panel_route["context"] = ("用 --background-maintenance 打开保存副本。按 describe 从 status 选择来源与集合，"
                                          "先提交 inspect:true 核实际成员、依赖及输入 SHA，再用 SHA 生成原生独立维护包。")
            if variant is not None and variant["parameters"].get("action") == "REPAIR":
                if variant["parameters"].get("kind") == "WEIGHT":
                    panel_route["context"] = ("用 --background-maintenance 接手保存的维护包，按 describe 传实际网格、RAW 拓扑、"
                                              "点号、两组及逐点转移量。先运行 review-scope，取得同一范围的双审回执后再 dispatch。")
                else:
                    panel_route["context"] = ("用 --background-maintenance 接手已保存维护包。按 describe 指定约束或非连接骨，"
                                              "先以 inspect:true 读取实际影响和 plan_sha256，再按同一输入生成独立候选。")
        if operator == "c94_workflow.inspection_drag":
            panel_route["session_open_fields"] = {"--background-positioning": True}
            panel_route["context"] += (" 确定性回放使用 --background-positioning 与 parameters.drag_replay，"
                                       "由固定版的定位几何与编辑核心执行。先用 preview:true 查看受影响端点，"
                                       "再用相同输入和 expected_preview_sha256 确认提交。"
                                       "回执区分 SHARED_NATIVE_PREVIEW 和 SHARED_NATIVE_COMMIT。"
                                       "原生鼠标交互在有视口的会话中调用。")
        if (host == "5.3.0" and operator == "c94_workflow.inspection_edit"
                and (variant is None or variant["parameters"].get("action") in
                     {"MIRROR_L", "MIRROR_R", "RESTORE", "LOCK", "UNLOCK", "SYNC", "REMEMBER", "RADIUS", "PLANE"})):
            panel_route["session_open_fields"] = {"--background-positioning": True}
            if variant is None or variant["parameters"].get("action") == "RESTORE":
                panel_route["context"] += (" 后台恢复用 --background-positioning 打开工作副本，"
                                           "从 status.positioning_drag 读取 owner、rig、target_mesh 及 recovery.sha256，"
                                           "用 expected_checkpoint_sha256 指定本次恢复的检查点。")
            if variant is None or variant["parameters"].get("action") in {"MIRROR_L", "MIRROR_R"}:
                panel_route["context"] += (" 后台镜像用 --background-positioning 打开工作副本，从 status.positioning_drag "
                                           "读取 owner、rig、target_mesh，在 parameters.targets 加入来源侧的 endpoints 或 markers。"
                                           "MIRROR_L 从左复制到右，MIRROR_R 从右复制到左。")
            if variant is None or variant["parameters"].get("action") in {"LOCK", "UNLOCK"}:
                panel_route["context"] += (" 后台锁定使用 --background-positioning，从 status.positioning_drag "
                                           "读取工作区身份并在 targets 指定 markers 或 endpoints。")
            if variant is None or variant["parameters"].get("action") in {"SYNC", "REMEMBER", "RADIUS", "PLANE"}:
                panel_route["context"] += (" 后台定位状态按 describe 的 positioning_state 调用。"
                                           "先查看保存的手调范围、选择或镜像平面，再带相应指纹执行。")
        if operator == "c94_workflow.inspection_compare":
            panel_route["session_open_fields"] = {"--background-positioning": True}
            panel_route["context"] += (" 通过 --background-positioning 打开定位工作副本。"
                                       "从 status.positioning_drag 读取身份与 comparisons，"
                                       "以实际对照身份和显隐作为 expected_comparison。")
        if operator == "c94_workflow.inspection_nearest":
            panel_route["session_open_fields"] = {"--background-positioning": True}
            panel_route["context"] += (" 后台吸附按 describe 传定位项目和具名端点，"
                                       "从状态取目标表面身份。在 surface 指定选面范围及法线偏移。")
        if operator == "c94_workflow.inspection_center":
            panel_route["session_open_fields"] = {"--background-positioning": True}
            panel_route["context"] += (" 在 --background-positioning 从 status 读取定位工作区和中心预览身份。"
                                       "用明确网格和保存选区预览，再带预览及源几何指纹提交或撤回。")
        if variant is None and operator in {
                "c94_workflow.retarget_generic_target", "c94_workflow.retarget_general_job_prepare",
                "c94_workflow.retarget_general_job_enqueue", "c94_workflow.retarget_queue",
                "c94_workflow.retarget_background", "c94_workflow.retarget_action"}:
            panel_route["session_open_fields"] = {"--background-retarget": True}
            panel_route["context"] += (" 普通 FK 后台加 --background-retarget、--file、--expected-sha256 和新的"
                                       " --session-dir。先在 generic-target 明确确认本目标，再按原生设置准备策略，"
                                       "以 ENQUEUE_GENERIC 提交。本模式可用的 operation 见本动作 describe。")
    unavailable = {**mapping.get("unavailable", {}), **edition_mapping.get("unavailable", {})}
    reason = None if edition_descriptors is not None else unavailable.get(row["action_id"], unavailable.get("*"))
    if not reason and row["status"] in {"缺CLI", "待核", "仅界面", "该版停用"}:
        reason = row["status"]
    edition = edition_route(product_id, row, host) if not reason else {}
    supported_hosts = edition_mapping.get("action_hosts", {}).get(row["action_id"], edition_mapping.get("hosts"))
    if not reason and not edition.get("reason") and edition_descriptors is not None and supported_hosts:
        restriction = require_host(host, supported_hosts, row["version"])
        if restriction and panel_route:
            if restriction.get("suggested_host"):
                preferred = restriction["suggested_host"]
                row["specialist_host_lookup"] = {
                    "blender_version": preferred,
                    "argv": [str(root / "blenderctl.ps1"), "--blender-version", preferred, "discover",
                             "--product", product_id, "--edition", row["version"], "--action", row["action_id"]],
                    "context": "所选宿主使用原生面板请求。本行的专项脚本及其参数使用这里指定的宿主。",
                }
            edition_descriptors = [panel_route]
            panel_route = None
        else:
            edition = restriction or edition
    if panel_route:
        if edition_descriptors:
            edition_descriptors = [*edition_descriptors, {**panel_route, "relation": "alternative"}]
        else:
            edition_descriptors = [panel_route]
    elif edition_descriptors is None and dispatchable:
        edition = require_host(host, panel["hosts"], row["version"]) or edition
    reason = reason or edition.get("reason")
    descriptors = []
    if not reason:
        descriptors = edition_descriptors
        if descriptors is None:
            descriptors = mapping.get("actions", {}).get(row["action_id"])
        if descriptors is None:
            descriptors = mapping.get("commands", {}).get(row["cli_command"])
        if descriptors is None:
            descriptors = public_descriptors(product_id, row)
    row["launchers"] = [resolve(item, edition, root, blender) for item in descriptors]
    if not row["launchers"]:
        row["launcher_reason"] = reason or "该动作的逐项入口尚待核实"
        if edition.get("suggested_host"):
            row["suggested_host"] = edition["suggested_host"]
        if mapping.get("recovery_product"):
            row["recovery_product"] = mapping["recovery_product"]


def script_prefix(runner, path, item, blender):
    available = path.is_file()
    if runner == "powershell":
        interpreter = shutil.which("pwsh")
        prefix = [interpreter or "pwsh", "-NoProfile", "-File"]
        available = available and interpreter is not None
    else:
        prefix = [sys.executable, "-B"] if runner == "python" else []
    prefix.append(str(path))
    if item.get("pass_blender"):
        prefix.extend([item.get("blender_argument", "--blender"), blender])
    return prefix, available


def resolve(item, edition, root, blender):
    runner = item["runner"]
    fixed = item["arguments"]
    options = [*edition.get("globals", []), *item.get("globals", [])]
    if runner == "public":
        path = root / "blenderctl.ps1"
        prefix = [str(path), "--blender", blender, *options]
        available = path.is_file()
    elif runner == "external":
        executable = shutil.which(item["entry"])
        path = Path(executable) if executable else None
        prefix = [str(path) if path else item["entry"]]
        available = bool(path and path.is_file())
    elif runner in {"python", "script", "powershell"}:
        path = root / item["entry"]
        prefix, available = script_prefix(runner, path, item, blender)
    else:
        raise ValueError(f"Unknown action route runner: {runner}")
    result = {"runner": runner, "file_present": available,
              "command_argv": prefix + fixed, "argv": prefix + item.get("help_arguments", [*fixed, "--help"])}
    if "session_open_arguments" in item:
        result["session_open_argv"] = prefix + item["session_open_arguments"]
    if item.get("relation"):
        result["relation"] = item["relation"]
    if "implementation_edition" in item:
        result["implementation_edition"] = item["implementation_edition"]
    context = [note for note in (edition.get("context"), item.get("context")) if note]
    if context:
        result["context"] = " ".join(context)
    for field in ("request_fields", "session_open_fields"):
        fields = {**edition.get(field, {}), **item.get(field, {})}
        if fields:
            result[field] = fields
    return result
