"""Pure-Python contract and normalization layer for Physics Nodes Workflow v1.0.

Zero bpy dependency so both the Blender addon and external CLI overlay validate
requests identically before invoking Blender.
"""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Any

CONTRACT_ID = "BLENDER_PHYSICS_WORKFLOW_CONTRACT_V1"
SCHEMA_VERSION = "1.0"

OPERATIONS = (
    "build",
    "update",
    "parameters",
    "bake",
    "invalidate_cache",
    "inspect",
    "adopt",
    "export",
)

DOMAINS = ("rigid_scatter", "cloth_rope", "fluid_emission")

BRIDGE_MODES = (
    "GN_NATIVE",
    "BULLET_BRIDGE",
    "XPBD_ASSET_BRIDGE",
    "MANTAFLOW_BRIDGE",
)

EXPORT_FORMATS = ("NONE", "STATIC_MESH", "ALEMBIC", "OPENVDB")

PRESET_DEFINITIONS: dict[str, dict[str, Any]] = {
    "rigid_drop_pack": {
        "domain": "rigid_scatter",
        "title": "Rigid Scatter & Drop Pack",
        "description": "Procedural rigid body scatter/drop with ramp & floor collision, bounce, friction, sleep threshold, and optional tether.",
        "defaults": {
            "substeps": 4,
            "seed": 42,
            "frame_start": 1,
            "frame_end": 120,
            "gravity": [0.0, 0.0, -9.81],
            "drag": 0.04,
            "restitution": 0.35,
            "friction": 0.45,
            "sleep_velocity": 0.08,
            "contact_thickness": 0.08,
            "particle_radius": 0.12,
            "ground_z": 0.0,
            "scatter_count": 48,
            "angular_spin": 1.2,
            "stiffness": 0.0,
            "anchor_pos": [0.0, 0.0, 2.5],
            "rest_length": 0.0,
            "wind_vector": [0.0, 0.0, 0.0],
            "wind_turbulence": 0.0,
            "emit_rate": 0,
            "emit_speed": 0.0,
            "emit_end_frame": 1,
            "lifetime": 250,
            "sdf_voxel_size": 0.08,
            "buoyancy": 0.0,
        },
    },
    "rigid_chain_pendulum": {
        "domain": "rigid_scatter",
        "title": "Rigid Chain & Tether Pendulum",
        "description": "Tethered rigid links/beads with anchor distance constraint, gravity, drag, and ground/obstacle collision.",
        "defaults": {
            "substeps": 6,
            "seed": 17,
            "frame_start": 1,
            "frame_end": 120,
            "gravity": [0.0, 0.0, -9.81],
            "drag": 0.02,
            "restitution": 0.25,
            "friction": 0.3,
            "sleep_velocity": 0.03,
            "contact_thickness": 0.06,
            "particle_radius": 0.14,
            "ground_z": 0.0,
            "scatter_count": 24,
            "angular_spin": 0.5,
            "stiffness": 0.65,
            "anchor_pos": [0.0, 0.0, 3.2],
            "rest_length": 1.8,
            "wind_vector": [1.5, 0.0, 0.0],
            "wind_turbulence": 0.3,
            "emit_rate": 0,
            "emit_speed": 0.0,
            "emit_end_frame": 1,
            "lifetime": 250,
            "sdf_voxel_size": 0.08,
            "buoyancy": 0.0,
        },
    },
    "cloth_banner_wind": {
        "domain": "cloth_rope",
        "title": "Cloth Banner & Wind Field",
        "description": "Pinned cloth/ribbon mesh with edge-relaxation elasticity, directional wind turbulence, and obstacle collision.",
        "defaults": {
            "substeps": 5,
            "seed": 101,
            "frame_start": 1,
            "frame_end": 120,
            "gravity": [0.0, 0.0, -9.81],
            "drag": 0.12,
            "restitution": 0.1,
            "friction": 0.4,
            "sleep_velocity": 0.01,
            "contact_thickness": 0.04,
            "particle_radius": 0.03,
            "ground_z": 0.0,
            "scatter_count": 0,
            "angular_spin": 0.0,
            "stiffness": 0.72,
            "anchor_pos": [0.0, 0.0, 0.0],
            "rest_length": 0.0,
            "wind_vector": [4.5, 1.2, 0.4],
            "wind_turbulence": 2.2,
            "emit_rate": 0,
            "emit_speed": 0.0,
            "emit_end_frame": 1,
            "lifetime": 250,
            "sdf_voxel_size": 0.08,
            "buoyancy": 0.0,
        },
    },
    "rope_catenary_swing": {
        "domain": "cloth_rope",
        "title": "Rope Catenary & Swing",
        "description": "Flexible rope/cable with pinned anchors, gravity sag, wind sway, and collider deflection.",
        "defaults": {
            "substeps": 6,
            "seed": 73,
            "frame_start": 1,
            "frame_end": 120,
            "gravity": [0.0, 0.0, -9.81],
            "drag": 0.08,
            "restitution": 0.15,
            "friction": 0.35,
            "sleep_velocity": 0.01,
            "contact_thickness": 0.03,
            "particle_radius": 0.03,
            "ground_z": 0.0,
            "scatter_count": 0,
            "angular_spin": 0.0,
            "stiffness": 0.85,
            "anchor_pos": [0.0, 0.0, 0.0],
            "rest_length": 0.0,
            "wind_vector": [2.0, 0.8, 0.0],
            "wind_turbulence": 1.4,
            "emit_rate": 0,
            "emit_speed": 0.0,
            "emit_end_frame": 1,
            "lifetime": 250,
            "sdf_voxel_size": 0.08,
            "buoyancy": 0.0,
        },
    },
    "fluid_fountain_sdf": {
        "domain": "fluid_emission",
        "title": "Fluid Fountain (Particle -> SDF Mesh)",
        "description": "Node particle emitter with seeded spray, obstacle deflection, and SDF GridToMesh liquid surface reconstruction.",
        "defaults": {
            "substeps": 3,
            "seed": 303,
            "frame_start": 1,
            "frame_end": 120,
            "gravity": [0.0, 0.0, -9.81],
            "drag": 0.03,
            "restitution": 0.25,
            "friction": 0.15,
            "sleep_velocity": 0.02,
            "contact_thickness": 0.05,
            "particle_radius": 0.16,
            "ground_z": 0.0,
            "scatter_count": 24,
            "angular_spin": 0.0,
            "stiffness": 0.0,
            "anchor_pos": [0.0, 0.0, 0.0],
            "rest_length": 0.0,
            "wind_vector": [0.3, 0.0, 0.0],
            "wind_turbulence": 0.6,
            "emit_rate": 24,
            "emit_speed": 6.5,
            "emit_end_frame": 90,
            "lifetime": 110,
            "sdf_voxel_size": 0.075,
            "buoyancy": 0.0,
        },
    },
    "smoke_plume_volume": {
        "domain": "fluid_emission",
        "title": "Buoyant Smoke Plume (Points -> Volume/Mesh)",
        "description": "Buoyant rising gas emitter with curl/noise turbulence and PointsToVolume/VolumeToMesh plume output.",
        "defaults": {
            "substeps": 2,
            "seed": 505,
            "frame_start": 1,
            "frame_end": 120,
            "gravity": [0.0, 0.0, 2.8],
            "drag": 0.18,
            "restitution": 0.1,
            "friction": 0.1,
            "sleep_velocity": 0.0,
            "contact_thickness": 0.06,
            "particle_radius": 0.24,
            "ground_z": 0.0,
            "scatter_count": 18,
            "angular_spin": 0.0,
            "stiffness": 0.0,
            "anchor_pos": [0.0, 0.0, 0.0],
            "rest_length": 0.0,
            "wind_vector": [0.8, 0.2, 0.4],
            "wind_turbulence": 2.4,
            "emit_rate": 18,
            "emit_speed": 2.2,
            "emit_end_frame": 100,
            "lifetime": 95,
            "sdf_voxel_size": 0.1,
            "buoyancy": 2.8,
        },
    },
}


class PhysicsContractError(ValueError):
    """Raised when a workflow request violates the contract."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _require_vec3(value: Any, name: str) -> list[float]:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise PhysicsContractError("VALIDATION_ERROR", f"{name} must be a 3-element float list")
    try:
        return [float(value[0]), float(value[1]), float(value[2])]
    except (TypeError, ValueError) as exc:
        raise PhysicsContractError("VALIDATION_ERROR", f"{name} contains non-numeric value") from exc


def _require_range(value: Any, name: str, lo: float, hi: float, is_int: bool = False) -> int | float:
    try:
        parsed = int(value) if is_int else float(value)
    except (TypeError, ValueError) as exc:
        raise PhysicsContractError("VALIDATION_ERROR", f"{name} must be numeric") from exc
    if parsed < lo or parsed > hi:
        raise PhysicsContractError("VALIDATION_ERROR", f"{name}={parsed} out of allowed bounds [{lo}, {hi}]")
    return parsed


def compute_parameters_hash(params: dict[str, Any], bridge_mode: str, preset_id: str, collider_object: str | None) -> str:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "preset_id": preset_id,
        "bridge_mode": bridge_mode,
        "collider_object": collider_object or "",
        "parameters": params,
    }
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def normalize_request(raw: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise PhysicsContractError("VALIDATION_ERROR", "Request payload must be a JSON object")

    schema_version = str(raw.get("schema_version", SCHEMA_VERSION))
    if schema_version != SCHEMA_VERSION:
        raise PhysicsContractError("VALIDATION_ERROR", f"Unsupported schema_version: {schema_version}")

    operation = str(raw.get("operation", "build")).strip()
    if operation not in OPERATIONS:
        raise PhysicsContractError("VALIDATION_ERROR", f"Unsupported operation: {operation}")

    preset_id = str(raw.get("preset_id", "rigid_drop_pack")).strip()
    if preset_id not in PRESET_DEFINITIONS:
        raise PhysicsContractError("VALIDATION_ERROR", f"Unknown preset_id: {preset_id}")

    preset = PRESET_DEFINITIONS[preset_id]
    domain = str(raw.get("domain", preset["domain"])).strip()
    if domain not in DOMAINS:
        raise PhysicsContractError("VALIDATION_ERROR", f"Unsupported domain: {domain}")
    if domain != preset["domain"] and "domain" in raw:
        raise PhysicsContractError(
            "VALIDATION_ERROR",
            f"preset_id '{preset_id}' belongs to domain '{preset['domain']}', got '{domain}'",
        )

    bridge_mode = str(raw.get("bridge_mode", "GN_NATIVE")).strip()
    if bridge_mode not in BRIDGE_MODES:
        raise PhysicsContractError("VALIDATION_ERROR", f"Unsupported bridge_mode: {bridge_mode}")

    export_format = str(raw.get("export_format", "NONE")).strip()
    if export_format not in EXPORT_FORMATS:
        raise PhysicsContractError("VALIDATION_ERROR", f"Unsupported export_format: {export_format}")

    params = copy.deepcopy(preset["defaults"])
    user_params = raw.get("parameters") or {}
    if not isinstance(user_params, dict):
        raise PhysicsContractError("VALIDATION_ERROR", "parameters must be a JSON object")
    for k, v in user_params.items():
        if k not in params:
            raise PhysicsContractError("VALIDATION_ERROR", f"Unknown physics parameter: {k}")
        params[k] = v

    # Allow top-level overrides for convenience
    for top_k in ("substeps", "seed", "frame_start", "frame_end"):
        if top_k in raw and raw[top_k] is not None:
            params[top_k] = raw[top_k]

    params["substeps"] = int(_require_range(params["substeps"], "substeps", 1, 8, is_int=True))
    params["seed"] = int(_require_range(params["seed"], "seed", 0, 2**31 - 1, is_int=True))
    params["frame_start"] = int(_require_range(params["frame_start"], "frame_start", 1, 100000, is_int=True))
    params["frame_end"] = int(_require_range(params["frame_end"], "frame_end", params["frame_start"], 100000, is_int=True))
    params["gravity"] = _require_vec3(params["gravity"], "gravity")
    params["wind_vector"] = _require_vec3(params["wind_vector"], "wind_vector")
    params["anchor_pos"] = _require_vec3(params["anchor_pos"], "anchor_pos")
    params["drag"] = float(_require_range(params["drag"], "drag", 0.0, 20.0))
    params["restitution"] = float(_require_range(params["restitution"], "restitution", 0.0, 1.0))
    params["friction"] = float(_require_range(params["friction"], "friction", 0.0, 1.0))
    params["sleep_velocity"] = float(_require_range(params["sleep_velocity"], "sleep_velocity", 0.0, 10.0))
    params["contact_thickness"] = float(_require_range(params["contact_thickness"], "contact_thickness", 0.001, 2.0))
    params["particle_radius"] = float(_require_range(params["particle_radius"], "particle_radius", 0.001, 5.0))
    params["ground_z"] = float(_require_range(params["ground_z"], "ground_z", -10000.0, 10000.0))
    params["scatter_count"] = int(_require_range(params["scatter_count"], "scatter_count", 0, 10000, is_int=True))
    params["angular_spin"] = float(_require_range(params["angular_spin"], "angular_spin", 0.0, 50.0))
    params["stiffness"] = float(_require_range(params["stiffness"], "stiffness", 0.0, 1.0))
    params["rest_length"] = float(_require_range(params["rest_length"], "rest_length", 0.0, 1000.0))
    params["wind_turbulence"] = float(_require_range(params["wind_turbulence"], "wind_turbulence", 0.0, 50.0))
    params["emit_rate"] = int(_require_range(params["emit_rate"], "emit_rate", 0, 5000, is_int=True))
    params["emit_speed"] = float(_require_range(params["emit_speed"], "emit_speed", 0.0, 100.0))
    params["emit_end_frame"] = int(_require_range(params["emit_end_frame"], "emit_end_frame", 1, 100000, is_int=True))
    params["lifetime"] = int(_require_range(params["lifetime"], "lifetime", 1, 100000, is_int=True))
    params["sdf_voxel_size"] = float(_require_range(params["sdf_voxel_size"], "sdf_voxel_size", 0.01, 2.0))
    params["buoyancy"] = float(_require_range(params["buoyancy"], "buoyancy", -50.0, 50.0))

    target_object = str(raw.get("target_object", "")).strip()
    modifier_name = str(raw.get("modifier_name", "PhysicsNodes")).strip() or "PhysicsNodes"
    collider_object = str(raw["collider_object"]).strip() if raw.get("collider_object") else None
    instance_collection = str(raw["instance_collection"]).strip() if raw.get("instance_collection") else None

    return {
        "schema_version": SCHEMA_VERSION,
        "contract_id": CONTRACT_ID,
        "operation": operation,
        "domain": domain,
        "preset_id": preset_id,
        "bridge_mode": bridge_mode,
        "target_object": target_object,
        "modifier_name": modifier_name,
        "collider_object": collider_object,
        "instance_collection": instance_collection,
        "bake": bool(raw.get("bake", False)),
        "bake_directory": str(raw["bake_directory"]).strip() if raw.get("bake_directory") else None,
        "force_rebuild": bool(raw.get("force_rebuild", False)),
        "verify_cold_reopen": bool(raw.get("verify_cold_reopen", False)),
        "export_format": export_format,
        "export_path": str(raw["export_path"]).strip() if raw.get("export_path") else None,
        "export_frame": int(_require_range(raw.get("export_frame", params["frame_end"]), "export_frame", params["frame_start"], params["frame_end"], is_int=True)),
        "parameters": params,
        "parameters_hash": compute_parameters_hash(params, bridge_mode, preset_id, collider_object),
    }


def describe_contract() -> dict[str, Any]:
    return {
        "contract_id": CONTRACT_ID,
        "schema_version": SCHEMA_VERSION,
        "operations": list(OPERATIONS),
        "domains": list(DOMAINS),
        "bridge_modes": list(BRIDGE_MODES),
        "export_formats": list(EXPORT_FORMATS),
        "presets": {
            k: {
                "domain": v["domain"],
                "title": v["title"],
                "description": v["description"],
                "defaults": copy.deepcopy(v["defaults"]),
            }
            for k, v in PRESET_DEFINITIONS.items()
        },
        "bounds": {
            "substeps": [1, 8],
            "windows_bake_parent_max": 247,
            "windows_bake_file_max": 259,
        },
    }
