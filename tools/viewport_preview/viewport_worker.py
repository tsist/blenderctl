# SPDX-License-Identifier: GPL-3.0-or-later
"""Blender 5.3 real-viewport preview worker running inside an isolated Windows Desktop.

Supports both one-shot execution (--job-spec <json>) and hot session daemon mode
(--session-dir <dir> --idle-timeout <sec>). Never saves or modifies source .blend files.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
import time
import traceback
from typing import Any

import bpy
from mathutils import Vector
import numpy as np

THIS_DIR = Path(__file__).resolve().parent
if str(THIS_DIR) not in sys.path:
    sys.path.insert(0, str(THIS_DIR))

import compositor_board
import framing


GEOMETRY_TYPES = {"MESH", "CURVE", "SURFACE", "FONT", "META", "GPENCIL", "GREASEPENCIL", "POINTCLOUD", "VOLUME"}
VALID_MODES = ("wireframe", "solid", "solid_wire", "matcap", "face_orientation", "material", "rendered")
DEFAULT_MATCAP = "metal_carpaint.exr"
DEFAULT_STUDIO_WORLD = "studio.exr"


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _find_v3d():
    wm = bpy.context.window_manager
    if not wm.windows:
        return None, None, None, None
    window = wm.windows[0]
    for area in window.screen.areas:
        if area.type == "VIEW_3D":
            region = next((r for r in area.regions if r.type == "WINDOW"), None)
            if region is not None:
                return window, area, region, area.spaces.active
    return window, None, None, None


def _capture_rgb(window, region) -> np.ndarray:
    """Grab top-down uint8 HxWx3 RGB array from the exact VIEW_3D WINDOW region."""
    w, h = int(region.width), int(region.height)
    x0, y0 = int(region.x), int(region.y)
    buf = window.screenshot(region=((x0, y0), (x0 + w, y0 + h)), use_alpha=False)
    arr = np.frombuffer(buf, dtype=np.uint8)
    expected4 = w * h * 4
    expected3 = w * h * 3
    if arr.size == expected4:
        rgb = arr.reshape(h, w, 4)[:, :, :3]
    elif arr.size == expected3:
        rgb = arr.reshape(h, w, 3)
    else:
        raise RuntimeError(f"Unexpected Window.screenshot buffer size {arr.size} for {w}x{h}")
    # Flip from bottom-to-top to top-down and copy so memoryview can be released
    return np.ascontiguousarray(rgb[::-1])


def _thumb_diff(a: np.ndarray, b: np.ndarray) -> float:
    if a.shape != b.shape:
        return 255.0
    sa = a[::4, ::4].astype(np.float32)
    sb = b[::4, ::4].astype(np.float32)
    return float(np.mean(np.abs(sa - sb)))


def _resolve_targets(scene, view_layer, spec: dict[str, Any]):
    obj_names = spec.get("objects")
    col_name = spec.get("collection")
    if obj_names and col_name:
        raise ValueError("Specify either objects or collection, not both")

    if obj_names:
        candidates = []
        missing = []
        for name in obj_names:
            o = scene.objects.get(name)
            if o is None:
                missing.append(name)
            else:
                candidates.append(o)
        if missing:
            raise ValueError(f"Target objects not found in scene {scene.name!r}: {missing}")
    elif col_name:
        col = bpy.data.collections.get(col_name)
        if col is None:
            raise ValueError(f"Collection {col_name!r} not found")
        candidates = list(col.all_objects)
    else:
        candidates = list(view_layer.objects)

    # Expand collection instances if explicitly targeted or present
    targets = []
    seen = set()
    for obj in candidates:
        if obj.as_pointer() in seen:
            continue
        if obj.name not in view_layer.objects:
            continue
        if not obj.visible_get(view_layer=view_layer) and not obj_names:
            continue
        if obj.type in GEOMETRY_TYPES or (obj.type == "EMPTY" and obj.instance_type == "COLLECTION"):
            seen.add(obj.as_pointer())
            targets.append(obj)

    if not targets:
        raise ValueError("No visible target geometry objects found to frame")
    return targets


def _scene_has_lighting(scene, view_layer) -> bool:
    for obj in view_layer.objects:
        if obj.type == "LIGHT" and obj.visible_get(view_layer=view_layer) and not obj.hide_viewport:
            if getattr(obj.data, "energy", 0.0) > 0.0:
                return True
    world = scene.world
    if world is not None:
        if world.use_nodes and world.node_tree:
            for node in world.node_tree.nodes:
                if node.type == "BACKGROUND":
                    strength = float(node.inputs["Strength"].default_value)
                    col = node.inputs["Color"].default_value
                    linked_col = bool(node.inputs["Color"].is_linked)
                    # Linked HDRI / Sky texture counts as authored lighting;
                    # Blender's default flat dark-grey world (0.05, 0.05, 0.05) does not.
                    if linked_col and strength > 0.05:
                        return True
                    if not linked_col and (max(col[0], col[1], col[2]) * strength) >= 0.25:
                        return True
        else:
            if max(world.color) >= 0.25:
                return True
    return False


def _create_fallback_lights(scene, center: Vector, span: float) -> list[Any]:
    """Add soft temporary three-point studio lights for unlit clay/model files."""
    created = []
    # Add subtle neutral world if missing
    if scene.world is None:
        w = bpy.data.worlds.new("_PreviewTempWorld")
        w.use_nodes = True
        bg = w.node_tree.nodes.get("Background")
        if bg:
            bg.inputs["Color"].default_value = (0.18, 0.20, 0.24, 1.0)
            bg.inputs["Strength"].default_value = 0.45
        scene.world = w
        created.append(w)

    specs = (
        ("_PreviewKey", (0.75, -1.1, 1.15), 26.0, 1.1),
        ("_PreviewFill", (-1.0, -0.5, 0.45), 11.0, 1.4),
        ("_PreviewRim", (-0.3, 1.15, 0.85), 15.0, 0.9),
    )
    for name, offset, power, size_mult in specs:
        ldata = bpy.data.lights.new(name, "AREA")
        ldata.energy = power * (span ** 2)
        ldata.shape = "DISK"
        ldata.size = size_mult * span
        lobj = bpy.data.objects.new(name, ldata)
        scene.collection.objects.link(lobj)
        lobj.location = center + Vector(offset) * span
        lobj.rotation_euler = (center - lobj.location).to_track_quat("-Z", "Y").to_euler()
        created.extend((lobj, ldata))
    return created


def _configure_cycles_preview(scene, spec: dict[str, Any]) -> dict[str, Any]:
    """Configure Cycles GPU viewport preview, preferring DLSS with explicit fallback."""
    scene.render.engine = "CYCLES"
    prefs = bpy.context.preferences.addons["cycles"].preferences

    backend = "CPU"
    device_name = "CPU"
    try:
        prefs.compute_device_type = "OPTIX"
        prefs.refresh_devices()
        optix_devs = [d for d in prefs.devices if d.type == "OPTIX"]
        if optix_devs:
            for d in prefs.devices:
                d.use = (d.type == "OPTIX")
            scene.cycles.device = "GPU"
            backend = "OPTIX"
            device_name = optix_devs[0].name
        else:
            prefs.compute_device_type = "CUDA"
            prefs.refresh_devices()
            cuda_devs = [d for d in prefs.devices if d.type == "CUDA"]
            if cuda_devs:
                for d in prefs.devices:
                    d.use = (d.type == "CUDA")
                scene.cycles.device = "GPU"
                backend = "CUDA"
                device_name = cuda_devs[0].name
            else:
                scene.cycles.device = "CPU"
    except Exception:
        scene.cycles.device = "CPU"

    has_dlss = False
    try:
        has_dlss = bool(prefs.has_dlss_gpu_devices())
    except Exception:
        has_dlss = False

    has_optix_denoise = False
    try:
        has_optix_denoise = bool(prefs.has_optixdenoiser_gpu_devices())
    except Exception:
        has_optix_denoise = False

    req_denoiser = str(spec.get("denoiser", "auto")).upper()
    strict_denoiser = bool(spec.get("strict_denoiser", False))
    dlss_quality = str(spec.get("dlss_quality", "BALANCED")).upper()
    if dlss_quality not in ("NONE", "QUALITY", "BALANCED", "PERFORMANCE", "ULTRA_PERFORMANCE"):
        raise ValueError(f"Unsupported dlss_quality: {dlss_quality!r}")

    samples = int(spec.get("samples", 8))
    scene.cycles.preview_samples = max(1, min(128, samples))
    scene.cycles.use_preview_denoising = True
    scene.cycles.preview_denoising_start_sample = 1

    actual_denoiser = "OPENIMAGEDENOISE"
    fallback_reason = None

    if req_denoiser in ("AUTO", "DLSS"):
        if has_dlss:
            scene.cycles.preview_denoiser = "DLSS"
            if hasattr(scene.cycles, "preview_denoising_upscale_quality"):
                scene.cycles.preview_denoising_upscale_quality = dlss_quality
            # DLSS is incompatible with preview adaptive sampling & scrambling
            scene.cycles.use_preview_adaptive_sampling = False
            actual_denoiser = "DLSS"
        else:
            reason = "NVIDIA DLSS Ray Reconstruction requires NVIDIA driver >= 620 and RTX GPU (compute capability >= 7.5)"
            if strict_denoiser:
                raise RuntimeError(f"Strict DLSS requested but unavailable on this host: {reason}")
            fallback_reason = "driver_below_620_or_unsupported"
            if has_optix_denoise:
                scene.cycles.preview_denoiser = "OPTIX"
                actual_denoiser = "OPTIX"
            else:
                scene.cycles.preview_denoiser = "OPENIMAGEDENOISE"
                actual_denoiser = "OPENIMAGEDENOISE"
    elif req_denoiser == "OPTIX":
        if has_optix_denoise:
            scene.cycles.preview_denoiser = "OPTIX"
            actual_denoiser = "OPTIX"
        elif strict_denoiser:
            raise RuntimeError("Strict OPTIX denoiser requested but no OptiX GPU device available")
        else:
            fallback_reason = "optix_unavailable"
            scene.cycles.preview_denoiser = "OPENIMAGEDENOISE"
            actual_denoiser = "OPENIMAGEDENOISE"
    elif req_denoiser in ("OIDN", "OPENIMAGEDENOISE"):
        scene.cycles.preview_denoiser = "OPENIMAGEDENOISE"
        actual_denoiser = "OPENIMAGEDENOISE"
    elif req_denoiser == "NONE":
        scene.cycles.use_preview_denoising = False
        actual_denoiser = "NONE"
    else:
        raise ValueError(f"Unknown denoiser {req_denoiser!r}")

    return {
        "render_engine": "CYCLES",
        "device_backend": backend,
        "device_name": device_name,
        "samples": scene.cycles.preview_samples,
        "requested_denoiser": req_denoiser,
        "actual_denoiser": actual_denoiser,
        "dlss_available": has_dlss,
        "dlss_quality": dlss_quality if actual_denoiser == "DLSS" else None,
        "fallback_reason": fallback_reason,
    }


def _configure_eevee_preview(scene, spec: dict[str, Any]) -> dict[str, Any]:
    scene.render.engine = "BLENDER_EEVEE"
    samples = int(spec.get("samples", 16))
    if hasattr(scene.eevee, "taa_samples"):
        scene.eevee.taa_samples = max(1, min(128, samples))
    if hasattr(scene.eevee, "use_raytracing"):
        scene.eevee.use_raytracing = True
    return {
        "render_engine": "BLENDER_EEVEE",
        "device_backend": "VULKAN_GRAPHICS",
        "device_name": "EEVEE Next Viewport",
        "samples": samples,
        "requested_denoiser": None,
        "actual_denoiser": None,
        "dlss_available": None,
        "dlss_quality": None,
        "fallback_reason": None,
    }


def _clean_viewport_chrome(space) -> None:
    """Hide all distracting UI panels, gizmos, axes, origins and text overlays."""
    for attr in ("show_region_toolbar", "show_region_ui", "show_region_header", "show_gizmo"):
        try:
            setattr(space, attr, False)
        except Exception:
            pass

    ov = space.overlay
    for attr in (
        "show_floor",
        "show_ortho_grid",
        "show_axis_x",
        "show_axis_y",
        "show_axis_z",
        "show_cursor",
        "show_text",
        "show_stats",
        "show_extras",
        "show_bones",
        "show_object_origins",
        "show_object_origins_all",
        "show_relationship_lines",
        "show_outline_selected",
        "show_face_orientation",
        "show_wireframes",
    ):
        try:
            setattr(ov, attr, False)
        except Exception:
            pass


def _apply_display_mode(
    scene,
    space,
    mode: str,
    spec: dict[str, Any],
    *,
    render_info_cache: dict[str, Any],
) -> tuple[dict[str, Any], str | None, tuple[int, int, int] | None]:
    """Configure shading + overlays for one display mode. Returns (mode_meta, badge, badge_bg)."""
    _clean_viewport_chrome(space)
    sh = space.shading
    ov = space.overlay

    # Reset X-Ray and standard flags
    sh.show_xray = False
    sh.show_xray_wireframe = False
    sh.show_shadows = False
    sh.show_cavity = False
    sh.show_specular_highlight = True
    sh.show_backface_culling = False
    sh.show_object_outline = False
    sh.background_type = "VIEWPORT"
    sh.background_color = (0.16, 0.17, 0.19)

    wire_overlay = spec.get("wire_overlay")
    wire_opacity = float(spec.get("wire_opacity", 0.45))
    wire_threshold = float(spec.get("wire_threshold", 1.0))

    badge = None
    badge_bg = None
    mode_meta: dict[str, Any] = {"mode": mode}

    if mode == "wireframe":
        sh.type = "WIREFRAME"
        xray = bool(spec.get("xray", True))
        sh.show_xray_wireframe = xray
        sh.xray_alpha_wireframe = float(spec.get("xray_alpha", 0.5))
        wcol = str(spec.get("wire_color", "OBJECT")).upper()
        if wcol in ("THEME", "OBJECT", "RANDOM"):
            sh.wireframe_color_type = wcol
        ov.show_overlays = False
        mode_meta.update(xray=xray, xray_alpha=sh.xray_alpha_wireframe, wire_color=sh.wireframe_color_type)

    elif mode in ("solid", "solid_wire"):
        sh.type = "SOLID"
        sh.light = "STUDIO"
        scol = str(spec.get("solid_color", "SINGLE")).upper()
        if scol not in ("SINGLE", "MATERIAL", "OBJECT", "RANDOM", "VERTEX", "TEXTURE"):
            scol = "SINGLE"
        sh.color_type = scol
        sh.single_color = (0.68, 0.70, 0.73)
        sh.show_cavity = True
        sh.cavity_type = "BOTH"
        sh.curvature_ridge_factor = 1.15
        sh.curvature_valley_factor = 0.85
        sh.show_specular_highlight = True
        if mode == "solid_wire":
            ov.show_overlays = True
            ov.show_wireframes = True
            ov.wireframe_opacity = max(0.0, min(1.0, wire_opacity))
            ov.wireframe_threshold = max(0.0, min(1.0, wire_threshold))
            mode_meta.update(wire_opacity=ov.wireframe_opacity, wire_threshold=ov.wireframe_threshold)
        else:
            ov.show_overlays = False
        mode_meta["solid_color"] = scol

    elif mode == "matcap":
        sh.type = "SOLID"
        sh.light = "MATCAP"
        matcap_name = str(spec.get("matcap") or DEFAULT_MATCAP)
        available = [sl.name for sl in bpy.context.preferences.studio_lights if sl.type == "MATCAP"]
        if matcap_name not in available:
            # Support partial prefix match like 'carpaint' or 'reflection_horizontal'
            match = next((m for m in available if matcap_name.lower() in m.lower()), None)
            matcap_name = match or (DEFAULT_MATCAP if DEFAULT_MATCAP in available else (available[0] if available else ""))
        if matcap_name:
            sh.studio_light = matcap_name
        sh.show_cavity = False
        ov.show_overlays = False
        mode_meta["matcap"] = matcap_name

    elif mode == "face_orientation":
        sh.type = "SOLID"
        sh.light = "STUDIO"
        sh.color_type = "SINGLE"
        sh.single_color = (0.65, 0.65, 0.65)
        ov.show_overlays = True
        ov.show_face_orientation = True
        if wire_overlay is not None:
            ov.show_wireframes = True
            ov.wireframe_opacity = max(0.0, min(1.0, float(wire_overlay)))
            ov.wireframe_threshold = max(0.0, min(1.0, wire_threshold))

    elif mode == "material":
        sh.type = "MATERIAL"
        use_scene_lights = bool(spec.get("use_scene_lights", False))
        use_scene_world = bool(spec.get("use_scene_world", False))
        sh.use_scene_lights = use_scene_lights
        sh.use_scene_world = use_scene_world
        if not use_scene_world:
            hdri = str(spec.get("studio_world") or DEFAULT_STUDIO_WORLD)
            avail_w = [sl.name for sl in bpy.context.preferences.studio_lights if sl.type == "WORLD"]
            if hdri in avail_w:
                sh.studio_light = hdri
            sh.studiolight_background_alpha = 0.0
            sh.studiolight_intensity = 1.0
        ov.show_overlays = False
        mode_meta.update(use_scene_lights=use_scene_lights, use_scene_world=use_scene_world)

    elif mode == "rendered":
        engine_choice = str(spec.get("render_engine", "cycles")).lower()
        if engine_choice == "eevee":
            rinfo = render_info_cache.setdefault("eevee", _configure_eevee_preview(scene, spec))
            scene.render.engine = "BLENDER_EEVEE"
            badge = "EEVEE NEXT"
            badge_bg = (42, 122, 152)
        else:
            rinfo = render_info_cache.setdefault("cycles", _configure_cycles_preview(scene, spec))
            scene.render.engine = "CYCLES"
            if rinfo["actual_denoiser"] == "DLSS":
                badge = f"DLSS {rinfo['dlss_quality']}"
                badge_bg = (45, 138, 72)
            elif rinfo["fallback_reason"]:
                badge = f"{rinfo['actual_denoiser']} (FALLBACK)"
                badge_bg = (168, 96, 28)
            else:
                badge = f"CYCLES {rinfo['actual_denoiser']}"
                badge_bg = (52, 112, 168)

        sh.type = "RENDERED"
        sh.use_scene_lights_render = True
        sh.use_scene_world_render = True
        ov.show_overlays = False
        mode_meta.update(rinfo)

    else:
        raise ValueError(f"Unsupported display mode {mode!r}; expected one of {VALID_MODES}")

    # Orthogonal semi-transparent wireframe overlay for ANY mode (except pure wireframe)
    if wire_overlay is not None and mode != "wireframe":
        opacity = max(0.0, min(1.0, float(wire_overlay)))
        if opacity > 0.0:
            ov.show_overlays = True
            ov.show_wireframes = True
            ov.wireframe_opacity = opacity
            ov.wireframe_threshold = max(0.0, min(1.0, wire_threshold))
            mode_meta["wire_overlay"] = opacity
            mode_meta["wire_threshold"] = ov.wireframe_threshold

    return mode_meta, badge, badge_bg


class CaptureTask:
    """Non-blocking state machine driven by bpy.app.timers so Blender's event loop redraws live."""

    def __init__(self, spec: dict[str, Any], loaded_file: str | None, loaded_sha: str | None, on_done):
        self.spec = spec
        self.loaded_file = loaded_file
        self.loaded_sha = loaded_sha
        self.on_done = on_done
        self.t0 = time.monotonic()

        self.phase = "open"
        self.source_path = Path(spec["file"]).resolve()
        self.source_sha = ""
        self.temp_created: list[Any] = []
        self.hidden_backup: list[tuple[Any, bool]] = []
        self.render_info_cache: dict[str, Any] = {}
        self.tiles_plan: list[dict[str, Any]] = []
        self.captured_tiles: list[dict[str, Any]] = []
        self.tile_index = 0

        self.rows = 2
        self.cols = 2
        self.cell_size = int(spec.get("cell_size", 640))
        self.points = []
        self.center = Vector((0.0, 0.0, 0.0))
        self.lo = Vector((0.0, 0.0, 0.0))
        self.hi = Vector((0.0, 0.0, 0.0))
        self.span = 1.0
        self.shared_ortho_half = 0.5
        self.targets_names: list[str] = []
        self.added_fallback_lights = False
        self.framing_out: dict[str, Any] = {}

        # Per-tile convergence tracking
        self.prev_tile_rgb: np.ndarray | None = None
        self.last_poll_rgb: np.ndarray | None = None
        self.tile_start_t = 0.0
        self.content_first_t: float | None = None
        self.saw_change = False
        self.stable_hits = 0
        self.cur_mode_meta: dict[str, Any] = {}
        self.cur_view_meta: dict[str, Any] = {}
        self.cur_badge: str | None = None
        self.cur_badge_bg: tuple[int, int, int] | None = None

    def _cleanup_scene_overrides(self) -> None:
        for obj, orig_hide in self.hidden_backup:
            try:
                obj.hide_viewport = orig_hide
            except Exception:
                pass
        self.hidden_backup.clear()
        if self.temp_created:
            try:
                bpy.data.batch_remove(ids=self.temp_created)
            except Exception:
                pass
            self.temp_created.clear()

    def _fail(self, exc: Exception) -> None:
        self._cleanup_scene_overrides()
        self.on_done({
            "ok": False,
            "error": {
                "type": type(exc).__name__,
                "message": str(exc),
                "traceback": traceback.format_exc(),
            },
            "elapsed_seconds": round(time.monotonic() - self.t0, 3),
        }, self.loaded_file, self.loaded_sha)

    def step(self) -> float | None:
        try:
            if self.phase == "open":
                return self._step_open()
            if self.phase == "maximize":
                return self._step_maximize()
            if self.phase == "prepare_scene":
                return self._step_prepare_scene()
            if self.phase == "setup_tile":
                return self._step_setup_tile()
            if self.phase == "poll_tile":
                return self._step_poll_tile()
            if self.phase == "finalize":
                return self._step_finalize()
        except Exception as exc:
            self._fail(exc)
            return None
        return None

    def _step_open(self) -> float:
        if not self.source_path.is_file():
            raise FileNotFoundError(f"Source blend file not found: {self.source_path}")
        self.source_sha = _sha256_file(self.source_path)
        expected = self.spec.get("expected_sha256")
        if expected and expected.lower() != self.source_sha.lower():
            raise ValueError(
                f"SHA256 mismatch for {self.source_path}: expected {expected}, actual {self.source_sha}"
            )

        need_load = (
            self.loaded_file is None
            or os.path.normcase(self.loaded_file) != os.path.normcase(str(self.source_path))
            or self.loaded_sha != self.source_sha
            or bool(self.spec.get("force_reload", False))
        )
        if need_load:
            bpy.context.preferences.view.show_splash = False
            bpy.ops.wm.open_mainfile(filepath=str(self.source_path), load_ui=False, use_scripts=False)
            self.loaded_file = str(self.source_path)
            self.loaded_sha = self.source_sha
            self.phase = "maximize"
            return 0.15
        self.phase = "prepare_scene"
        return 0.05

    def _step_maximize(self) -> float:
        w, a, r, sp = _find_v3d()
        if w is None or a is None or r is None:
            raise RuntimeError("No VIEW_3D area found in Blender window")
        if len(w.screen.areas) > 1:
            with bpy.context.temp_override(window=w, area=a, region=r):
                bpy.ops.screen.screen_full_area(use_hide_panels=True)
            self.phase = "prepare_scene"
            return 0.18
        self.phase = "prepare_scene"
        return 0.05

    def _step_prepare_scene(self) -> float:
        scene_name = self.spec.get("scene")
        scene = bpy.data.scenes.get(scene_name) if scene_name else bpy.context.scene
        if scene is None:
            raise ValueError(f"Scene {scene_name!r} not found")
        bpy.context.window.scene = scene

        layer_name = self.spec.get("view_layer")
        view_layer = scene.view_layers.get(layer_name) if layer_name else bpy.context.view_layer
        if view_layer is None:
            raise ValueError(f"View layer {layer_name!r} not found in scene {scene.name!r}")
        bpy.context.window.view_layer = view_layer

        if self.spec.get("frame") is not None:
            scene.frame_set(int(self.spec["frame"]))
        view_layer.update()

        # Deselect everything so orange selection outlines never pollute preview
        for o in view_layer.objects:
            if o.select_get(view_layer=view_layer):
                o.select_set(False, view_layer=view_layer)

        targets = _resolve_targets(scene, view_layer, self.spec)
        self.targets_names = [o.name for o in targets]

        # Optional --isolate: hide non-target geometry while keeping lights/world intact
        if bool(self.spec.get("isolate", False)):
            keep_ptrs = {o.as_pointer() for o in targets}
            for o in view_layer.objects:
                if o.as_pointer() not in keep_ptrs and o.type in GEOMETRY_TYPES:
                    self.hidden_backup.append((o, bool(o.hide_viewport)))
                    o.hide_viewport = True
            view_layer.update()

        depsgraph = bpy.context.evaluated_depsgraph_get()
        self.points, self.lo, self.hi, self.span = framing.collect_target_points(targets, depsgraph)
        self.center = (self.lo + self.hi) * 0.5

        compare_modes = self.spec.get("compare_modes")
        if compare_modes:
            modes_list = [m.strip().lower() for m in compare_modes]
            if not 1 <= len(modes_list) <= 9:
                raise ValueError("compare_modes accepts 1..9 display modes")
            for m in modes_list:
                if m not in VALID_MODES:
                    raise ValueError(f"Invalid mode in compare_modes: {m!r}")
            view_tok = self.spec.get("view") or "FRONT_3Q"
            proj_ov = self.spec.get("projection")
            view_spec = framing.parse_view_token(view_tok, proj_ov)
            n = len(modes_list)
            if n == 1:
                self.rows, self.cols = 1, 1
            elif n == 2:
                self.rows, self.cols = 1, 2
            elif n <= 4:
                self.rows, self.cols = 2, 2
            else:
                self.rows, self.cols = 3, 3
            self.tiles_plan = [{"view": view_spec, "mode": m} for m in modes_list]
        else:
            mode = str(self.spec.get("mode", "solid_wire")).lower()
            if mode not in VALID_MODES:
                raise ValueError(f"Unsupported display mode {mode!r}; expected one of {VALID_MODES}")
            layout = str(self.spec.get("layout", "quad")).lower()
            custom_views = self.spec.get("views")
            if self.spec.get("view") and not custom_views:
                layout = "single"
                custom_views = [self.spec["view"]]
            (self.rows, self.cols), resolved_views = framing.resolve_views(
                layout, custom_views, self.spec.get("projection")
            )
            self.tiles_plan = [{"view": vs, "mode": mode} for vs in resolved_views]

        self.shared_ortho_half = framing.compute_shared_ortho_extent(
            self.points, self.center, [t["view"] for t in self.tiles_plan]
        )

        # Fallback studio lighting for rendered mode on unlit scenes (unless strict_scene_lights)
        needs_rendered = any(t["mode"] == "rendered" for t in self.tiles_plan)
        if needs_rendered and not bool(self.spec.get("strict_scene_lights", False)):
            if not _scene_has_lighting(scene, view_layer):
                self.temp_created.extend(_create_fallback_lights(scene, self.center, self.span))
                self.added_fallback_lights = True
                view_layer.update()

        self.framing_out = {
            "center": [round(float(x), 6) for x in self.center],
            "span": round(float(self.span), 6),
            "ortho_half_extent": round(float(self.shared_ortho_half), 6),
            "bounds_min": [round(float(x), 6) for x in self.lo],
            "bounds_max": [round(float(x), 6) for x in self.hi],
        }

        self.tile_index = 0
        self.phase = "setup_tile"
        return 0.02

    def _step_setup_tile(self) -> float:
        if self.tile_index >= len(self.tiles_plan):
            self.phase = "finalize"
            return 0.02

        w, a, r, sp = _find_v3d()
        scene = bpy.context.window.scene
        item = self.tiles_plan[self.tile_index]
        view_spec = item["view"]
        mode = item["mode"]

        # Configure display mode first, then apply exact framing
        self.cur_mode_meta, self.cur_badge, self.cur_badge_bg = _apply_display_mode(
            scene, sp, mode, self.spec, render_info_cache=self.render_info_cache
        )

        self.cur_view_meta = framing.apply_viewport_view(
            sp,
            sp.region_3d,
            view_spec,
            self.points,
            self.center,
            self.span,
            margin=float(self.spec.get("margin", 0.10)),
            lens=float(self.spec.get("lens", 50.0)),
            shared_ortho_half=self.shared_ortho_half,
            fixed_framing=self.spec.get("framing"),
        )
        if view_spec.get("projection") == "CAMERA":
            with bpy.context.temp_override(window=w, area=a, region=r):
                try:
                    bpy.ops.view3d.view_center_camera()
                except Exception:
                    pass
        else:
            if self.cur_view_meta.get("projection") == "ORTHO":
                self.framing_out["ortho_view_distance"] = self.cur_view_meta["view_distance"]
            elif self.cur_view_meta.get("projection") == "PERSP":
                self.framing_out["persp_view_distance"] = self.cur_view_meta["view_distance"]

        a.tag_redraw()
        self.tile_start_t = time.monotonic()
        self.content_first_t = None
        self.last_poll_rgb = None
        self.saw_change = self.prev_tile_rgb is None
        self.stable_hits = 0
        self.phase = "poll_tile"

        # Initial settle delay depends on rendering weight
        if mode == "rendered":
            return 0.35
        if mode == "material":
            return 0.25
        return 0.12

    def _step_poll_tile(self) -> float:
        w, a, r, sp = _find_v3d()
        item = self.tiles_plan[self.tile_index]
        mode = item["mode"]
        now = time.monotonic()
        elapsed = now - self.tile_start_t
        settle_timeout = float(self.spec.get("settle_timeout", 15.0))

        cur_rgb = _capture_rgb(w, r)
        # Detect flat uniform viewport background (e.g. while Cycles/DLSS NGX kernels are initializing)
        frame_std = float(cur_rgb[::4, ::4].std())
        has_content = frame_std >= 1.5
        if has_content and self.content_first_t is None:
            self.content_first_t = now

        if not self.saw_change and self.prev_tile_rgb is not None:
            if has_content and _thumb_diff(cur_rgb, self.prev_tile_rgb) > 0.35:
                self.saw_change = True
            elif not has_content:
                # Viewport cleared to uniform background while rebuilding new view/kernel
                self.saw_change = True

        ready = False
        if mode in ("wireframe", "solid", "solid_wire", "matcap", "face_orientation"):
            # Raster modes settle immediately once the new view redraws
            if self.saw_change and elapsed >= 0.12:
                ready = True
        elif mode == "material":
            # EEVEE Material Preview may compile shaders on first tile
            if self.saw_change and has_content and self.last_poll_rgb is not None:
                diff = _thumb_diff(cur_rgb, self.last_poll_rgb)
                since_content = now - (self.content_first_t or now)
                if diff < 0.25 and since_content >= 0.30:
                    self.stable_hits += 1
                else:
                    self.stable_hits = 0
                if self.stable_hits >= 2:
                    ready = True
        else:
            # Cycles (DLSS / OptiX / OIDN) or EEVEE RENDERED viewport progressive convergence.
            # Never treat a uniform flat grey loading screen (has_content == False) as converged.
            min_after_content = 0.50 if self.cur_mode_meta.get("render_engine") == "CYCLES" else 0.35
            if self.saw_change and has_content and self.last_poll_rgb is not None:
                diff = _thumb_diff(cur_rgb, self.last_poll_rgb)
                since_content = now - (self.content_first_t or now)
                if diff < 0.20 and since_content >= min_after_content:
                    self.stable_hits += 1
                else:
                    self.stable_hits = 0
                if self.stable_hits >= 3:
                    ready = True

        if not ready and elapsed >= settle_timeout:
            ready = True

        self.last_poll_rgb = cur_rgb
        if not ready:
            a.tag_redraw()
            return 0.15 if mode == "rendered" else 0.10

        # Build concise tile header
        vlabel = self.cur_view_meta.get("label", f"VIEW {self.tile_index + 1}")
        vproj = self.cur_view_meta.get("projection", "")
        if self.spec.get("compare_modes"):
            header = f"{mode.upper()} | {vlabel}"
        else:
            header = f"{vlabel} ({vproj})" if vproj and vproj != "CAMERA" else vlabel

        self.captured_tiles.append({
            "rgb": cur_rgb,
            "header": header,
            "badge": self.cur_badge,
            "badge_bg": self.cur_badge_bg,
            "meta": {
                **self.cur_view_meta,
                **self.cur_mode_meta,
                "settle_seconds": round(elapsed, 3),
                "frame_std": round(frame_std, 2),
                "has_content": has_content,
            },
        })
        self.prev_tile_rgb = cur_rgb
        self.tile_index += 1
        self.phase = "setup_tile"
        return 0.02

    def _step_finalize(self) -> None:
        out_png = Path(self.spec["output"]).resolve()
        out_json = Path(self.spec.get("report_path") or out_png.with_suffix(".json")).resolve()

        # Compose descriptive top banner
        if self.spec.get("compare_modes"):
            mode_tag = "COMPARE: " + "/".join(m.upper() for m in self.spec["compare_modes"])
        else:
            mode_tag = str(self.spec.get("mode", "solid_wire")).upper()
            if self.spec.get("wire_overlay") is not None and mode_tag != "WIREFRAME":
                mode_tag += f"+WIRE({float(self.spec['wire_overlay']):.2f})"

        focus_tag = (
            "COL:" + str(self.spec["collection"])
            if self.spec.get("collection")
            else ("OBJ:" + ",".join(self.spec["objects"][:2]) + ("+" if len(self.spec["objects"]) > 2 else "")
                  if self.spec.get("objects")
                  else "SCENE")
        )
        iso_tag = " [ISOLATE]" if self.spec.get("isolate") else ""
        banner = f"{self.source_path.name} | {mode_tag} | {focus_tag}{iso_tag}"

        board_rgb, tile_rects = compositor_board.compose_board(
            self.captured_tiles,
            self.rows,
            self.cols,
            self.cell_size,
            banner,
        )
        saved_board = compositor_board.save_rgb_png(board_rgb, out_png)

        saved_tiles = []
        if bool(self.spec.get("save_tiles", False)):
            tiles_dir = out_png.parent / f"{out_png.stem}_tiles"
            for idx, tile in enumerate(self.captured_tiles):
                t_rgb = compositor_board.fit_tile_square(tile["rgb"], self.cell_size)
                t_path = tiles_dir / f"tile_{idx + 1:02d}_{tile['meta'].get('mode', 'view')}.png"
                saved_tiles.append(compositor_board.save_rgb_png(t_rgb, t_path))

        self._cleanup_scene_overrides()

        # Verify source bytes remained untouched
        post_sha = _sha256_file(self.source_path)
        report = {
            "ok": True,
            "preview_version": "1.0",
            "blender_version": bpy.app.version_string,
            "blender_build": bpy.app.build_hash.decode() if isinstance(bpy.app.build_hash, bytes) else str(bpy.app.build_hash),
            "gpu_backend": getattr(bpy.context.preferences.system, "gpu_backend", None),
            "source_file": str(self.source_path),
            "source_sha256": self.source_sha,
            "source_unchanged": (post_sha == self.source_sha),
            "scene": bpy.context.window.scene.name,
            "view_layer": bpy.context.window.view_layer.name,
            "frame": int(bpy.context.window.scene.frame_current),
            "focus_objects": self.targets_names,
            "isolate": bool(self.spec.get("isolate", False)),
            "added_fallback_lights": self.added_fallback_lights,
            "output_png": saved_board,
            "output_tiles": saved_tiles,
            "report_file": str(out_json),
            "framing": self.framing_out,
            "tiles": tile_rects,
            "elapsed_seconds": round(time.monotonic() - self.t0, 3),
        }
        _atomic_json(out_json, report)
        self.on_done(report, self.loaded_file, self.loaded_sha)
        return None


def _run_oneshot(spec_path: Path) -> None:
    spec = json.loads(spec_path.read_text(encoding="utf-8-sig"))
    result_path = Path(spec["worker_result_path"])

    def done(payload: dict[str, Any], _lf, _ls) -> None:
        _atomic_json(result_path, payload)
        bpy.ops.wm.quit_blender()

    task = CaptureTask(spec, None, None, done)
    bpy.app.timers.register(task.step, first_interval=0.08, persistent=True)


def _run_session_daemon(session_dir: Path, idle_timeout: float) -> None:
    req_dir = session_dir / "requests"
    res_dir = session_dir / "responses"
    req_dir.mkdir(parents=True, exist_ok=True)
    res_dir.mkdir(parents=True, exist_ok=True)
    status_file = session_dir / "daemon_status.json"
    stop_flag = session_dir / "stop.request"

    state: dict[str, Any] = {
        "loaded_file": None,
        "loaded_sha": None,
        "busy": False,
        "active_task": None,
        "last_activity": time.monotonic(),
        "requests_served": 0,
    }

    def write_status(phase: str) -> None:
        _atomic_json(status_file, {
            "pid": os.getpid(),
            "phase": phase,
            "loaded_file": state["loaded_file"],
            "loaded_sha": state["loaded_sha"],
            "requests_served": state["requests_served"],
            "idle_timeout": idle_timeout,
            "updated_at": time.time(),
        })

    write_status("ready")

    def poll_queue() -> float | None:
        if stop_flag.exists():
            write_status("stopped")
            bpy.ops.wm.quit_blender()
            return None

        if state["busy"]:
            return 0.05

        if time.monotonic() - state["last_activity"] >= idle_timeout:
            write_status("idle_timeout_exit")
            bpy.ops.wm.quit_blender()
            return None

        pending = sorted(req_dir.glob("*.json"))
        if not pending:
            return 0.08

        req_file = pending[0]
        req_id = req_file.stem
        try:
            spec = json.loads(req_file.read_text(encoding="utf-8-sig"))
        except Exception:
            # File may still be flushing
            return 0.05
        try:
            req_file.unlink()
        except OSError:
            pass

        state["busy"] = True
        state["last_activity"] = time.monotonic()
        write_status("capturing")

        def done(payload: dict[str, Any], lf, ls) -> None:
            state["loaded_file"] = lf
            state["loaded_sha"] = ls
            state["requests_served"] += 1
            state["busy"] = False
            state["active_task"] = None
            state["last_activity"] = time.monotonic()
            _atomic_json(res_dir / f"{req_id}.json", payload)
            write_status("ready")

        task = CaptureTask(spec, state["loaded_file"], state["loaded_sha"], done)
        state["active_task"] = task
        bpy.app.timers.register(task.step, first_interval=0.02, persistent=True)
        return 0.05

    bpy.app.timers.register(poll_queue, first_interval=0.1, persistent=True)


def main() -> None:
    argv = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
    if "--job-spec" in argv:
        idx = argv.index("--job-spec")
        _run_oneshot(Path(argv[idx + 1]))
    elif "--session-dir" in argv:
        sdir = Path(argv[argv.index("--session-dir") + 1])
        idle = float(argv[argv.index("--idle-timeout") + 1]) if "--idle-timeout" in argv else 180.0
        _run_session_daemon(sdir, idle)
    else:
        raise SystemExit("Expected --job-spec <json> or --session-dir <dir>")


if __name__ == "__main__":
    main()
