"""Conservative UV transfer numerics for the M3 transfer shard.

This module intentionally implements only the interfaces and support envelope
described in ``CONTRACT.md``.  It does not modify topology, and it does not
approximate an arbitrary-surface nearest-point map.

Public entry points
-------------------
indexed_map(source, target)
topology(source, target, uv, corner_map, winding="PRESERVE", *, vertex_domain="ALL")
surface(source, target, uv, regions, width, height, max_distance_m,
        normal_degrees, uv_tolerance_px, pair_budget, cancel=None)

All geometry is in world metres after the caller has applied scene unit scale
and object transforms.  This module applies neither object transforms nor unit
scale.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Callable

import numpy as np


ERROR_CODES = frozenset(
    {"INVALID_REQUEST", "CONFLICT", "UNSUPPORTED", "RESOURCE_LIMIT", "VALIDATION_FAILED"}
)

# These are deliberately small numerical tolerances, not quality thresholds.
# They are public so callers can reproduce and audit every decision.
NUMERIC_POLICY = {
    "area_absolute_epsilon_m2": 1.0e-15,
    "area_relative_epsilon": 1.0e-11,
    "area_epsilon_max_target_fraction": 1.0e-4,
    "clip_cross_epsilon_m2": 1.0e-15,
    "polygon_point_epsilon_m": 1.0e-9,
    "polygon_cleanup_epsilon_m": 1.0e-12,
    "barycentric_epsilon": 1.0e-9,
    "distance_epsilon_m": 1.0e-9,
    "normal_angle_epsilon_deg": 1.0e-9,
    "uv_pixel_epsilon_px": 1.0e-9,
    "uv_pixel_metric": "max(abs(du)*width, abs(dv)*height)",
}


class TransferError(Exception):
    """A transfer failure that the parent can map to a public error."""

    def __init__(self, code: str, message: str, details: Mapping[str, Any] | None = None):
        if code not in ERROR_CODES:
            raise ValueError(f"unsupported TransferError code: {code!r}")
        self.code = str(code)
        self.message = str(message)
        normalized = _jsonable(dict(details or {}))
        try:
            # Round-trip through JSON so callers always receive native JSON
            # containers/scalars, never numpy scalars or arrays.
            normalized = json.loads(
                json.dumps(normalized, allow_nan=False, default=str)
            )
        except (TypeError, ValueError) as exc:
            normalized = {
                "details_serialization_failed": True,
                "serialization_error": str(exc),
                "details_repr": repr(details),
            }
        self.details = normalized
        super().__init__(f"{self.code}: {self.message}")

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": self.details}


def _jsonable(value: Any) -> Any:
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, float):
        if math.isnan(value):
            return "NaN"
        if math.isinf(value):
            return "Infinity" if value > 0 else "-Infinity"
    return value


def _fail(code: str, message: str, **details: Any) -> None:
    raise TransferError(code, message, details)


def _python_index(value: Any, what: str) -> int:
    if isinstance(value, (bool, np.bool_)):
        _fail("INVALID_REQUEST", f"{what} must be an integer, not bool")
    try:
        index = int(value)
    except (TypeError, ValueError, OverflowError):
        _fail("INVALID_REQUEST", f"{what} must be an integer", value=repr(value))
    try:
        if float(value) != float(index):
            _fail("INVALID_REQUEST", f"{what} must be integral", value=repr(value))
    except (TypeError, ValueError):
        _fail("INVALID_REQUEST", f"{what} must be integral", value=repr(value))
    return index


def _index_sequence(values: Any, what: str, *, allow_empty: bool = False) -> tuple[int, ...]:
    if isinstance(values, np.ndarray):
        if values.ndim == 0:
            _fail("INVALID_REQUEST", f"{what} must be a sequence of indices")
        values = values.tolist()
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        _fail("INVALID_REQUEST", f"{what} must be a sequence of indices")
    out = tuple(_python_index(v, f"{what}[{i}]") for i, v in enumerate(values))
    if not allow_empty and not out:
        _fail("INVALID_REQUEST", f"{what} must not be empty")
    return out


def _real_number(value: Any, what: str) -> float:
    if isinstance(value, (bool, np.bool_)):
        _fail("INVALID_REQUEST", f"{what} must be a finite number")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        _fail("INVALID_REQUEST", f"{what} must be a finite number", value=repr(value))
    if not math.isfinite(number):
        _fail("INVALID_REQUEST", f"{what} must be finite", value=repr(value))
    return number


def _vec(values: np.ndarray) -> list[float]:
    return [float(v) for v in values]


@dataclass(frozen=True)
class _Triangle:
    index: int
    face: int
    loops: tuple[int, int, int]
    vertices: tuple[int, int, int]


@dataclass
class _Mesh:
    name: str
    positions: np.ndarray
    faces: tuple[tuple[int, ...], ...]
    face_loops: tuple[tuple[int, ...], ...]
    triangles: tuple[_Triangle, ...]
    loop_vertex: tuple[int, ...]
    loop_face: tuple[int, ...]
    triangles_by_face: tuple[tuple[int, ...], ...]
    face_normal: np.ndarray
    face_area: np.ndarray

    @property
    def vertex_count(self) -> int:
        return int(self.positions.shape[0])

    @property
    def face_count(self) -> int:
        return len(self.faces)

    @property
    def loop_count(self) -> int:
        return len(self.loop_vertex)

    def triangle_points(self, triangle: _Triangle) -> np.ndarray:
        return np.asarray(self.positions[list(triangle.vertices)], dtype=np.float64)


def _newell_normal(points: np.ndarray) -> tuple[np.ndarray, float]:
    """Return a unit face normal and the magnitude of the Newell sum."""

    normal = np.zeros(3, dtype=np.float64)
    count = int(points.shape[0])
    for i in range(count):
        x_i, y_i, z_i = points[i]
        x_j, y_j, z_j = points[(i + 1) % count]
        normal[0] += (y_i - y_j) * (z_i + z_j)
        normal[1] += (z_i - z_j) * (x_i + x_j)
        normal[2] += (x_i - x_j) * (y_i + y_j)
    magnitude = float(np.linalg.norm(normal))
    if magnitude <= 1.0e-30 or not math.isfinite(magnitude):
        return np.zeros(3, dtype=np.float64), magnitude
    return normal / magnitude, magnitude


def _parse_mesh(
    mesh: Mapping[str, Any],
    role: str,
    *,
    require_triangles: bool,
) -> _Mesh:
    if not isinstance(mesh, Mapping):
        _fail("INVALID_REQUEST", f"{role} mesh must be a mapping")
    name = mesh.get("name", f"<{role}>")
    if not isinstance(name, str):
        _fail("INVALID_REQUEST", f"{role}.name must be a string")

    try:
        positions = np.asarray(mesh.get("positions"), dtype=np.float64)
    except (TypeError, ValueError, OverflowError):
        _fail("INVALID_REQUEST", f"{role}.positions must be an N x 3 numeric array")
    if positions.ndim != 2 or positions.shape[1] != 3 or positions.shape[0] < 1:
        _fail(
            "INVALID_REQUEST",
            f"{role}.positions must have shape (N, 3), N >= 1",
            shape=list(positions.shape),
        )
    if not np.all(np.isfinite(positions)):
        _fail("INVALID_REQUEST", f"{role}.positions contains a non-finite value")

    raw_faces = mesh.get("faces")
    raw_face_loops = mesh.get("face_loops")
    if isinstance(raw_faces, (str, bytes)) or not isinstance(
        raw_faces, (Sequence, np.ndarray)
    ):
        _fail("INVALID_REQUEST", f"{role}.faces must be a sequence")
    if isinstance(raw_face_loops, (str, bytes)) or not isinstance(
        raw_face_loops, (Sequence, np.ndarray)
    ):
        _fail("INVALID_REQUEST", f"{role}.face_loops must be a sequence")
    if len(raw_faces) != len(raw_face_loops):
        _fail(
            "INVALID_REQUEST",
            f"{role}.faces and {role}.face_loops must have equal length",
            faces=len(raw_faces),
            face_loops=len(raw_face_loops),
        )
    if len(raw_faces) < 1:
        _fail("INVALID_REQUEST", f"{role}.faces must not be empty")

    faces: list[tuple[int, ...]] = []
    face_loops: list[tuple[int, ...]] = []
    loop_face: dict[int, int] = {}
    loop_vertex: dict[int, int] = {}
    for face_index, (raw_vertices, raw_loops) in enumerate(zip(raw_faces, raw_face_loops)):
        vertices = _index_sequence(raw_vertices, f"{role}.faces[{face_index}]")
        loops = _index_sequence(raw_loops, f"{role}.face_loops[{face_index}]")
        if len(vertices) < 3:
            _fail(
                "INVALID_REQUEST",
                f"{role} face {face_index} has fewer than three vertices",
                face=face_index,
            )
        if len(vertices) != len(loops):
            _fail(
                "INVALID_REQUEST",
                f"{role} face {face_index} vertex/loop counts differ",
                face=face_index,
                vertices=len(vertices),
                loops=len(loops),
            )
        if len(set(vertices)) != len(vertices):
            _fail(
                "INVALID_REQUEST",
                f"{role} face {face_index} repeats a vertex index",
                face=face_index,
            )
        if len(set(loops)) != len(loops):
            _fail(
                "INVALID_REQUEST",
                f"{role} face {face_index} repeats a loop index",
                face=face_index,
            )
        for vertex, loop in zip(vertices, loops):
            if vertex < 0 or vertex >= positions.shape[0]:
                _fail(
                    "INVALID_REQUEST",
                    f"{role} face {face_index} has an out-of-range vertex",
                    face=face_index,
                    vertex=vertex,
                    vertex_count=int(positions.shape[0]),
                )
            if loop < 0:
                _fail(
                    "INVALID_REQUEST",
                    f"{role} face {face_index} has a negative loop index",
                    face=face_index,
                    loop=loop,
                )
            if loop in loop_face:
                _fail(
                    "INVALID_REQUEST",
                    f"{role} loop {loop} occurs in more than one face",
                    loop=loop,
                    first_face=loop_face[loop],
                    second_face=face_index,
                )
            loop_face[loop] = face_index
            loop_vertex[loop] = vertex
        faces.append(vertices)
        face_loops.append(loops)

    loop_count = len(loop_face)
    if sorted(loop_face) != list(range(loop_count)):
        _fail(
            "INVALID_REQUEST",
            f"{role} loop indices must be contiguous 0..L-1",
            loop_count=loop_count,
            minimum=min(loop_face) if loop_face else None,
            maximum=max(loop_face) if loop_face else None,
        )
    ordered_loop_vertex = tuple(loop_vertex[i] for i in range(loop_count))
    ordered_loop_face = tuple(loop_face[i] for i in range(loop_count))

    raw_triangles = mesh.get("triangles")
    triangles: list[_Triangle] = []
    triangles_by_face: list[list[int]] = [[] for _ in faces]
    if raw_triangles is None:
        if require_triangles:
            _fail("INVALID_REQUEST", f"{role}.triangles is required")
    else:
        if isinstance(raw_triangles, (str, bytes)) or not isinstance(
            raw_triangles, (Sequence, np.ndarray)
        ):
            _fail("INVALID_REQUEST", f"{role}.triangles must be a sequence")
        for triangle_index, raw_triangle in enumerate(raw_triangles):
            if not isinstance(raw_triangle, Mapping):
                _fail(
                    "INVALID_REQUEST",
                    f"{role}.triangles[{triangle_index}] must be a mapping",
                    triangle=triangle_index,
                )
            face = _python_index(
                raw_triangle.get("face"), f"{role}.triangles[{triangle_index}].face"
            )
            if face < 0 or face >= len(faces):
                _fail(
                    "INVALID_REQUEST",
                    f"{role} triangle {triangle_index} has an invalid face",
                    triangle=triangle_index,
                    face=face,
                    face_count=len(faces),
                )
            loops = _index_sequence(
                raw_triangle.get("loops"), f"{role}.triangles[{triangle_index}].loops"
            )
            if len(loops) != 3:
                _fail(
                    "INVALID_REQUEST",
                    f"{role} triangle {triangle_index} must have exactly three loops",
                    triangle=triangle_index,
                    loops=len(loops),
                )
            if len(set(loops)) != 3:
                _fail(
                    "INVALID_REQUEST",
                    f"{role} triangle {triangle_index} repeats a loop",
                    triangle=triangle_index,
                )
            for loop in loops:
                if loop < 0 or loop >= loop_count:
                    _fail(
                        "INVALID_REQUEST",
                        f"{role} triangle {triangle_index} has an invalid loop",
                        triangle=triangle_index,
                        loop=loop,
                        loop_count=loop_count,
                    )
                if loop_face[loop] != face:
                    _fail(
                        "INVALID_REQUEST",
                        f"{role} triangle {triangle_index} loop does not belong to its face",
                        triangle=triangle_index,
                        loop=loop,
                        declared_face=face,
                        actual_face=loop_face[loop],
                    )
            vertices = tuple(loop_vertex[loop] for loop in loops)
            if len(set(vertices)) != 3:
                _fail(
                    "INVALID_REQUEST",
                    f"{role} triangle {triangle_index} uses a repeated vertex",
                    triangle=triangle_index,
                    vertices=list(vertices),
                )
            item = _Triangle(
                index=triangle_index,
                face=face,
                loops=(loops[0], loops[1], loops[2]),
                vertices=(vertices[0], vertices[1], vertices[2]),
            )
            triangles.append(item)
            triangles_by_face[face].append(triangle_index)

    normals = np.zeros((len(faces), 3), dtype=np.float64)
    areas = np.zeros(len(faces), dtype=np.float64)
    for face_index, face_vertices in enumerate(faces):
        normal, magnitude = _newell_normal(positions[list(face_vertices)])
        if magnitude > 1.0e-30:
            normals[face_index] = normal
            areas[face_index] = 0.5 * magnitude

    return _Mesh(
        name=name,
        positions=positions,
        faces=tuple(faces),
        face_loops=tuple(face_loops),
        triangles=tuple(triangles),
        loop_vertex=ordered_loop_vertex,
        loop_face=ordered_loop_face,
        triangles_by_face=tuple(tuple(v) for v in triangles_by_face),
        face_normal=normals,
        face_area=areas,
    )


def _parse_uv(uv: Any, source: _Mesh) -> np.ndarray:
    try:
        array = np.asarray(uv, dtype=np.float64)
    except (TypeError, ValueError, OverflowError):
        _fail("INVALID_REQUEST", "uv must be an L x 2 numeric array")
    if array.shape != (source.loop_count, 2):
        _fail(
            "INVALID_REQUEST",
            "uv must have shape (source loop count, 2)",
            shape=list(array.shape),
            expected=[source.loop_count, 2],
        )
    if not np.all(np.isfinite(array)):
        _fail("INVALID_REQUEST", "uv contains a non-finite value")
    return array


def _cyclic_alignment(target: Sequence[int], source: Sequence[int], reverse: bool) -> int | None:
    if len(target) != len(source):
        return None
    count = len(source)
    for start in range(count):
        if reverse:
            if all(target[i] == source[(start - i) % count] for i in range(count)):
                return start
        else:
            if all(target[i] == source[(start + i) % count] for i in range(count)):
                return start
    return None


def indexed_map(source: Mapping[str, Any], target: Mapping[str, Any]) -> list[int]:
    """Return identity corner mapping only for identical indexed topology.

    The returned list is the target-loop -> source-loop map expected by
    ``topology``: ``range(source.loop_count)``.  Equal vertex/face counts with a
    different face or loop order is not enough: this function never guesses a
    correspondence from positions.
    """

    src = _parse_mesh(source, "source", require_triangles=False)
    tgt = _parse_mesh(target, "target", require_triangles=False)
    if src.vertex_count != tgt.vertex_count:
        _fail(
            "CONFLICT",
            "indexed_map requires equal vertex counts",
            source_vertices=src.vertex_count,
            target_vertices=tgt.vertex_count,
        )
    if len(src.faces) != len(tgt.faces):
        _fail(
            "CONFLICT",
            "indexed_map requires equal face counts",
            source_faces=len(src.faces),
            target_faces=len(tgt.faces),
        )
    if len(src.face_loops) != len(tgt.face_loops):
        _fail(
            "CONFLICT",
            "indexed_map requires equal face-loop counts",
            source_faces=len(src.face_loops),
            target_faces=len(tgt.face_loops),
        )
    for face_index, (source_face, target_face) in enumerate(zip(src.faces, tgt.faces)):
        if source_face != target_face:
            _fail(
                "CONFLICT",
                "indexed_map found a different face vertex sequence",
                face=face_index,
                source_face=list(source_face),
                target_face=list(target_face),
            )
    for face_index, (source_loops, target_loops) in enumerate(
        zip(src.face_loops, tgt.face_loops)
    ):
        if source_loops != target_loops:
            _fail(
                "CONFLICT",
                "indexed_map found a different face loop sequence",
                face=face_index,
                source_face_loops=list(source_loops),
                target_face_loops=list(target_loops),
            )
    if src.loop_count != tgt.loop_count:
        _fail(
            "CONFLICT",
            "indexed_map requires equal loop counts for an identity corner map",
            source_loops=src.loop_count,
            target_loops=tgt.loop_count,
        )
    return list(range(src.loop_count))


def topology(
    source: Mapping[str, Any],
    target: Mapping[str, Any],
    uv: Any,
    corner_map: Any,
    winding: str = "PRESERVE",
    *,
    vertex_domain: str = "ALL",
) -> tuple[np.ndarray, dict[str, Any]]:
    """Copy UV through a full corner/face bijection and the selected vertex domain.

    ALL requires every mesh vertex. FACE_REFERENCED explicitly requires every
    vertex referenced by face corners, excluding vertices without UV corners.
    Neither mode changes geometry or infers a map for excluded vertices.
    """

    if not isinstance(vertex_domain, str) or vertex_domain not in ("ALL", "FACE_REFERENCED"):
        _fail("INVALID_REQUEST", "vertex_domain must be ALL or FACE_REFERENCED",
              vertex_domain=repr(vertex_domain))
    if winding not in ("PRESERVE", "REVERSE"):
        _fail(
            "INVALID_REQUEST",
            "winding must be PRESERVE or REVERSE",
            winding=repr(winding),
        )
    src = _parse_mesh(source, "source", require_triangles=False)
    tgt = _parse_mesh(target, "target", require_triangles=False)
    source_uv = _parse_uv(uv, src)

    if isinstance(corner_map, (str, bytes)) or not isinstance(
        corner_map, (Sequence, np.ndarray)
    ):
        _fail("INVALID_REQUEST", "corner_map must be a sequence")
    if len(corner_map) != tgt.loop_count:
        _fail(
            "CONFLICT",
            "corner_map length does not equal the target loop count",
            corner_map_length=len(corner_map),
            target_loops=tgt.loop_count,
        )
    corner_map_tuple = _index_sequence(corner_map, "corner_map")
    if sorted(corner_map_tuple) != list(range(src.loop_count)):
        _fail(
            "CONFLICT",
            "corner_map is not a source-loop bijection",
            mapped_loops=len(corner_map_tuple),
            unique_mapped_loops=len(set(corner_map_tuple)),
            source_loops=src.loop_count,
        )

    if vertex_domain == "ALL" and src.vertex_count != tgt.vertex_count:
        _fail(
            "CONFLICT",
            "topology requires a vertex bijection, but vertex counts differ",
            source_vertices=src.vertex_count,
            target_vertices=tgt.vertex_count,
        )

    vertex_map: dict[int, int] = {}
    for target_loop, source_loop in enumerate(corner_map_tuple):
        target_vertex = tgt.loop_vertex[target_loop]
        source_vertex = src.loop_vertex[source_loop]
        if target_vertex in vertex_map and vertex_map[target_vertex] != source_vertex:
            _fail(
                "CONFLICT",
                "corner_map maps one target vertex to multiple source vertices",
                target_vertex=target_vertex,
                first_source_vertex=vertex_map[target_vertex],
                second_source_vertex=source_vertex,
                target_loop=target_loop,
            )
        vertex_map[target_vertex] = source_vertex
    source_domain = (set(range(src.vertex_count)) if vertex_domain == "ALL"
                     else set(src.loop_vertex))
    target_domain = (set(range(tgt.vertex_count)) if vertex_domain == "ALL"
                     else set(tgt.loop_vertex))
    if set(vertex_map) != target_domain or len(set(vertex_map.values())) != len(target_domain):
        _fail(
            "CONFLICT",
            "corner_map does not induce a full target/source vertex bijection"
            if vertex_domain == "ALL" else
            "corner_map does not induce a face-referenced target/source vertex bijection",
            mapped_vertices=len(vertex_map),
            unique_source_vertices=len(set(vertex_map.values())),
            target_vertices=tgt.vertex_count,
            source_vertices=src.vertex_count,
            note="unreferenced vertices cannot participate in a corner map",
        )
    if set(vertex_map.values()) != source_domain:
        _fail(
            "CONFLICT",
            "corner_map does not cover every source vertex" if vertex_domain == "ALL"
            else "corner_map does not cover every face-referenced source vertex",
            mapped_source_vertices=len(set(vertex_map.values())),
            source_vertices=src.vertex_count,
        )

    source_face_by_loop_set = {
        frozenset(loops): face_index for face_index, loops in enumerate(src.face_loops)
    }
    face_map: list[int] = []
    for target_face, target_loops in enumerate(tgt.face_loops):
        mapped_loops = tuple(corner_map_tuple[loop] for loop in target_loops)
        source_face = source_face_by_loop_set.get(frozenset(mapped_loops))
        if source_face is None:
            _fail(
                "CONFLICT",
                "a target face maps to no single source face",
                target_face=target_face,
                mapped_loops=list(mapped_loops),
            )
        reverse = winding == "REVERSE"
        start = _cyclic_alignment(mapped_loops, src.face_loops[source_face], reverse)
        if start is None:
            _fail(
                "CONFLICT",
                "mapped loops do not preserve the required cyclic winding",
                target_face=target_face,
                source_face=source_face,
                winding=winding,
                target_mapped_loops=list(mapped_loops),
                source_loops=list(src.face_loops[source_face]),
            )

        source_face_vertices = src.faces[source_face]
        if reverse:
            aligned_source_vertices = tuple(
                source_face_vertices[(start - i) % len(source_face_vertices)]
                for i in range(len(source_face_vertices))
            )
        else:
            aligned_source_vertices = tuple(
                source_face_vertices[(start + i) % len(source_face_vertices)]
                for i in range(len(source_face_vertices))
            )
        mapped_source_vertices = tuple(src.loop_vertex[loop] for loop in mapped_loops)
        if mapped_source_vertices != aligned_source_vertices:
            _fail(
                "CONFLICT",
                "corner_map does not induce the face vertex bijection required by winding",
                target_face=target_face,
                source_face=source_face,
                winding=winding,
                mapped_source_vertices=list(mapped_source_vertices),
                expected_source_vertices=list(aligned_source_vertices),
            )
        face_map.append(source_face)

    if sorted(face_map) != list(range(src.face_count)):
        _fail(
            "CONFLICT",
            "corner_map does not induce a full face bijection",
            target_to_source_face_map=face_map,
            source_face_count=src.face_count,
        )

    output = np.empty((tgt.loop_count, 2), dtype=np.float32)
    for target_loop, source_loop in enumerate(corner_map_tuple):
        output[target_loop] = source_uv[source_loop].astype(np.float32)
    if not np.all(np.isfinite(output)):
        _fail(
            "INVALID_REQUEST",
            "source UV cannot be represented as finite float32 values",
        )

    full_vertex_bijection = (
        set(vertex_map) == set(range(tgt.vertex_count))
        and set(vertex_map.values()) == set(range(src.vertex_count))
        and len(vertex_map) == len(set(vertex_map.values()))
    )
    vertex_map_identity = (full_vertex_bijection
                           and all(vertex_map[i] == i for i in range(tgt.vertex_count)))
    corner_map_list = list(corner_map_tuple)
    report = {
        "mode": "topology",
        "winding": winding,
        "source": {
            "name": src.name,
            "vertices": src.vertex_count,
            "faces": src.face_count,
            "loops": src.loop_count,
        },
        "target": {
            "name": tgt.name,
            "vertices": tgt.vertex_count,
            "faces": tgt.face_count,
            "loops": tgt.loop_count,
        },
        "checks": {
            "corner_bijection": True,
            "face_bijection": True,
            "vertex_bijection": full_vertex_bijection,
            "winding_consistent": True,
        },
        "mapping_summary": {
            "corner_count": tgt.loop_count,
            "face_count": tgt.face_count,
            "vertex_count": tgt.vertex_count,
            "corner_map_identity": corner_map_list == list(range(tgt.loop_count)),
            "face_map_identity": face_map == list(range(tgt.face_count)),
            "vertex_map_identity": vertex_map_identity,
        },
        "uv": {
            "output_dtype": "float32",
            "values_are_direct_source_loop_copies": True,
            "float32_tolerance_applied": False,
            "copied_before_required_float32_cast": True,
            "max_abs_roundtrip_error": float(
                np.max(np.abs(output.astype(np.float64) - source_uv[corner_map_list]))
            ),
        },
        "support": {
            "class": "same indexed topology plus explicit corner/face/vertex bijection",
            "geometry_may_deform": True,
            "topology_modified": False,
            "source_purpose_approval_inherited": False,
        },
    }
    if vertex_domain == "FACE_REFERENCED":
        # Legacy checks.vertex_bijection and vertex_map_identity retain their
        # full-mesh meaning, even when the selected smaller domain is complete.
        report["vertex_domain"] = vertex_domain
        report["checks"].update(
            face_referenced_vertex_bijection=True,
            full_vertex_bijection_required=False,
        )
        report["mapping_summary"].update(
            mapped_vertex_count=len(vertex_map),
            face_referenced_vertex_map_identity=all(t == s for t, s in vertex_map.items()),
        )
        for role, mesh, domain in (("source", src, source_domain),
                                   ("target", tgt, target_domain)):
            excluded = sorted(set(range(mesh.vertex_count)) - domain)
            report[role].update(
                face_referenced_vertices=len(domain),
                unreferenced_vertices=len(excluded),
                excluded_vertex_indices=excluded[:1000],
                excluded_vertex_indices_truncated=len(excluded) > 1000,
            )
        report["support"]["class"] = (
            "explicit corner/face and face-referenced vertex bijection"
        )
        report["support"]["vertex_domain_note"] = (
            "Complete correspondence covers all face-referenced vertices only; "
            "unreferenced vertices have no UV corners and are excluded, with no "
            "geometry writes or inferred correspondence for excluded vertices."
        )
    return output, report


class _Budget:
    def __init__(self, budget: int, cancel: Callable[[], Any] | None):
        self.budget = int(budget)
        self.cancel = cancel
        self.used = 0

    def check_cancel(self, context: Mapping[str, Any]) -> None:
        if self.cancel is None:
            return
        # Caller-owned cancellation/progress exceptions must propagate
        # unchanged.  Only a normal truthy return means cancellation.
        stop = self.cancel()
        if stop:
            _fail(
                "RESOURCE_LIMIT",
                "surface transfer cancelled",
                reason="cancelled",
                used=self.used,
                consumed=self.used,
                budget=self.budget,
                remaining=self.budget - self.used,
                per_call=True,
                deduct_from_manifest=True,
                context=dict(context),
            )

    def charge(self, context: Mapping[str, Any]) -> None:
        self.check_cancel(context)
        if self.used >= self.budget:
            _fail(
                "RESOURCE_LIMIT",
                "pair_budget exhausted before coverage was proved",
                reason="pair_budget_exhausted",
                used=self.used,
                consumed=self.used,
                budget=self.budget,
                remaining=self.budget - self.used,
                per_call=True,
                deduct_from_manifest=True,
                context=dict(context),
            )
        self.used += 1


def _cross2(a: np.ndarray, b: np.ndarray) -> float:
    return float(a[0] * b[1] - a[1] * b[0])


def _clip_halfplane(poly: np.ndarray, a: np.ndarray, b: np.ndarray) -> np.ndarray:
    edge = b - a
    if float(np.linalg.norm(edge)) <= 1.0e-30:
        return np.empty((0, 2), dtype=np.float64)
    epsilon = float(NUMERIC_POLICY["clip_cross_epsilon_m2"])
    output: list[np.ndarray] = []
    previous = poly[-1]
    previous_value = _cross2(edge, previous - a)
    previous_inside = previous_value >= -epsilon
    for current in poly:
        current_value = _cross2(edge, current - a)
        current_inside = current_value >= -epsilon
        if current_inside:
            if not previous_inside:
                denominator = _cross2(edge, current - previous)
                parameter = 0.0 if abs(denominator) <= 1.0e-30 else -previous_value / denominator
                parameter = min(1.0, max(0.0, parameter))
                output.append(previous + parameter * (current - previous))
            output.append(current)
        elif previous_inside:
            denominator = _cross2(edge, current - previous)
            parameter = 0.0 if abs(denominator) <= 1.0e-30 else -previous_value / denominator
            parameter = min(1.0, max(0.0, parameter))
            output.append(previous + parameter * (current - previous))
        previous = current
        previous_value = current_value
        previous_inside = current_inside
    if not output:
        return np.empty((0, 2), dtype=np.float64)
    return np.asarray(output, dtype=np.float64)


def _clean_polygon(poly: np.ndarray) -> np.ndarray:
    if poly.ndim != 2 or poly.shape[0] == 0:
        return np.empty((0, 2), dtype=np.float64)
    epsilon = float(NUMERIC_POLICY["polygon_cleanup_epsilon_m"])
    cleaned: list[np.ndarray] = []
    for point in poly:
        if not cleaned or float(np.linalg.norm(point - cleaned[-1])) > epsilon:
            cleaned.append(point)
    if len(cleaned) > 1 and float(np.linalg.norm(cleaned[0] - cleaned[-1])) <= epsilon:
        cleaned.pop()
    if len(cleaned) < 3:
        return np.empty((0, 2), dtype=np.float64)
    return np.asarray(cleaned, dtype=np.float64)


def _signed_area(poly: np.ndarray) -> float:
    if poly.shape[0] < 3:
        return 0.0
    x = poly[:, 0]
    y = poly[:, 1]
    return 0.5 * float(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1)))


def _area(poly: np.ndarray) -> float:
    return abs(_signed_area(poly))


def _normalize_ccw(poly: np.ndarray) -> np.ndarray:
    if poly.shape[0] >= 3 and _signed_area(poly) < 0.0:
        return poly[::-1].copy()
    return poly


def _clip_polygon(subject: np.ndarray, clip: np.ndarray) -> np.ndarray:
    output = _clean_polygon(subject)
    if output.shape[0] < 3:
        return np.empty((0, 2), dtype=np.float64)
    clip_ccw = _normalize_ccw(_clean_polygon(clip))
    if clip_ccw.shape[0] < 3:
        return np.empty((0, 2), dtype=np.float64)
    for index in range(clip_ccw.shape[0]):
        if output.shape[0] < 3:
            return np.empty((0, 2), dtype=np.float64)
        output = _clean_polygon(
            _clip_halfplane(output, clip_ccw[index], clip_ccw[(index + 1) % clip_ccw.shape[0]])
        )
    return _normalize_ccw(output)


def _project(points: np.ndarray, origin: np.ndarray, axis_u: np.ndarray, axis_v: np.ndarray) -> np.ndarray:
    delta = points - origin
    return np.column_stack((delta @ axis_u, delta @ axis_v))


def _barycentric_2d(triangle: np.ndarray, point: np.ndarray) -> np.ndarray:
    a, b, c = triangle
    denominator = _cross2(b - a, c - a)
    if abs(denominator) <= 1.0e-30:
        _fail(
            "VALIDATION_FAILED",
            "cannot evaluate barycentrics on a degenerate projected triangle",
            denominator=denominator,
        )
    weight_a = _cross2(b - point, c - point) / denominator
    weight_b = _cross2(c - point, a - point) / denominator
    weight_c = 1.0 - weight_a - weight_b
    return np.asarray([weight_a, weight_b, weight_c], dtype=np.float64)


def _safe_barycentric(triangle: np.ndarray, point: np.ndarray, context: Mapping[str, Any]) -> np.ndarray:
    weights = _barycentric_2d(triangle, point)
    epsilon = float(NUMERIC_POLICY["barycentric_epsilon"])
    if float(np.min(weights)) < -epsilon or float(np.max(weights)) > 1.0 + epsilon:
        _fail(
            "VALIDATION_FAILED",
            "a clipped point is materially outside its source triangle",
            reason="internal_barycentric_out_of_range",
            barycentric=_vec(weights),
            point_2d=_vec(point),
            context=dict(context),
        )
    weights = np.maximum(weights, 0.0)
    total = float(np.sum(weights))
    if total <= 0.0:
        _fail(
            "VALIDATION_FAILED",
            "projected barycentric weights have zero sum",
            point_2d=_vec(point),
            context=dict(context),
        )
    return weights / total


def _point_in_convex(poly: np.ndarray, point: np.ndarray) -> bool:
    polygon = _normalize_ccw(poly)
    if polygon.shape[0] < 3:
        return False
    epsilon = float(NUMERIC_POLICY["polygon_point_epsilon_m"])
    for index in range(polygon.shape[0]):
        a = polygon[index]
        b = polygon[(index + 1) % polygon.shape[0]]
        edge = b - a
        length = float(np.linalg.norm(edge))
        if length <= 1.0e-30:
            continue
        if _cross2(edge, point - a) < -epsilon * length:
            return False
    return True


def _pixel_delta(
    first: np.ndarray, second: np.ndarray, width: float, height: float
) -> tuple[float, float]:
    delta = np.asarray(first, dtype=np.float64) - np.asarray(second, dtype=np.float64)
    pixel = max(abs(float(delta[0])) * width, abs(float(delta[1])) * height)
    return pixel, float(np.linalg.norm(delta))


def _target_frame(
    mesh: _Mesh, triangle: _Triangle
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray, float]:
    points = mesh.triangle_points(triangle)
    p0, p1, p2 = points
    edge = p1 - p0
    edge_length = float(np.linalg.norm(edge))
    raw_normal = np.cross(edge, p2 - p0)
    normal_length = float(np.linalg.norm(raw_normal))
    if edge_length <= 1.0e-30 or normal_length <= 1.0e-30:
        _fail(
            "VALIDATION_FAILED",
            "target triangle is geometrically degenerate",
            target_name=mesh.name,
            target_face=triangle.face,
            target_triangle=triangle.index,
            edge_length_m=edge_length,
            normal_length=normal_length,
        )
    normal = raw_normal / normal_length
    axis_u = edge / edge_length
    axis_v = np.cross(normal, axis_u)
    target_2d = _project(points, p0, axis_u, axis_v)
    area_2d = _area(target_2d)
    if not math.isfinite(area_2d) or area_2d <= 0.0:
        _fail(
            "VALIDATION_FAILED",
            "target triangle has no positive projected area",
            target_face=triangle.face,
            target_triangle=triangle.index,
            area_m2=area_2d,
        )
    return p0, axis_u, axis_v, normal, target_2d, area_2d


@dataclass
class _Patch:
    source_triangle: _Triangle
    source_triangle_2d: np.ndarray
    source_uv: np.ndarray
    polygon: np.ndarray
    area: float
    max_vertex_distance: float

    def source_uv_at(self, point: np.ndarray) -> np.ndarray:
        context = {
            "source_name": self.source_triangle.index,
            "source_face": self.source_triangle.face,
            "source_triangle": self.source_triangle.index,
        }
        weights = _safe_barycentric(self.source_triangle_2d, point, context)
        return weights @ self.source_uv


def _parse_regions(
    regions: Any, target: _Mesh, source: _Mesh
) -> list[dict[str, Any]]:
    if (
        isinstance(regions, (str, bytes))
        or not isinstance(regions, (Sequence, np.ndarray))
        or len(regions) == 0
    ):
        _fail("INVALID_REQUEST", "regions must be a non-empty sequence")
    target_owner: dict[int, int] = {}
    parsed: list[dict[str, Any]] = []
    for region_index, raw_region in enumerate(regions):
        if not isinstance(raw_region, Mapping):
            _fail(
                "INVALID_REQUEST",
                f"regions[{region_index}] must be a mapping",
                region=region_index,
            )
        target_faces = _index_sequence(
            raw_region.get("target_faces"), f"regions[{region_index}].target_faces"
        )
        source_faces = _index_sequence(
            raw_region.get("source_faces"), f"regions[{region_index}].source_faces"
        )
        if len(set(target_faces)) != len(target_faces):
            _fail(
                "INVALID_REQUEST",
                f"regions[{region_index}].target_faces repeats a face",
                region=region_index,
            )
        if len(set(source_faces)) != len(source_faces):
            _fail(
                "INVALID_REQUEST",
                f"regions[{region_index}].source_faces repeats a face",
                region=region_index,
            )
        for face in target_faces:
            if face < 0 or face >= target.face_count:
                _fail(
                    "INVALID_REQUEST",
                    f"regions[{region_index}] has an invalid target face",
                    region=region_index,
                    face=face,
                    target_face_count=target.face_count,
                )
            if face in target_owner:
                _fail(
                    "CONFLICT",
                    "a target face appears in more than one region",
                    target_face=face,
                    first_region=target_owner[face],
                    second_region=region_index,
                )
            target_owner[face] = region_index
        source_triangles: list[int] = []
        for face in source_faces:
            if face < 0 or face >= source.face_count:
                _fail(
                    "INVALID_REQUEST",
                    f"regions[{region_index}] has an invalid source face",
                    region=region_index,
                    face=face,
                    source_face_count=source.face_count,
                )
            face_triangles = source.triangles_by_face[face]
            if not face_triangles:
                _fail(
                    "INVALID_REQUEST",
                    "a selected source face has no triangles",
                    region=region_index,
                    source_face=face,
                )
            if source.face_area[face] <= 0.0 or not np.any(source.face_normal[face]):
                _fail(
                    "VALIDATION_FAILED",
                    "a selected source face is geometrically degenerate",
                    region=region_index,
                    source_face=face,
                    source_face_area_m2=float(source.face_area[face]),
                )
            source_triangles.extend(face_triangles)
            used_loops: set[int] = set()
            for triangle_index in face_triangles:
                used_loops.update(source.triangles[triangle_index].loops)
            missing_loops = sorted(set(source.face_loops[face]) - used_loops)
            if missing_loops:
                _fail(
                    "INVALID_REQUEST",
                    "a selected source face has triangles that omit some face loops",
                    region=region_index,
                    source_face=face,
                    missing_loops=missing_loops,
                )
        parsed.append(
            {
                "region": region_index,
                "target_faces": list(target_faces),
                "source_faces": list(source_faces),
                "source_triangles": source_triangles,
            }
        )
    expected_faces = set(range(target.face_count))
    if set(target_owner) != expected_faces:
        missing = sorted(expected_faces - set(target_owner))
        _fail(
            "INVALID_REQUEST",
            "regions do not cover every target face exactly once",
            missing_target_faces=missing,
            covered_target_faces=sorted(target_owner),
        )
    return parsed


def _validate_surface_inputs(
    target: _Mesh, source: _Mesh, width: float, height: float
) -> None:
    if width <= 0.0 or height <= 0.0:
        _fail(
            "INVALID_REQUEST",
            "width and height must be positive",
            width=width,
            height=height,
        )
    target_loop_use = [0] * target.loop_count
    for triangle in target.triangles:
        for loop in triangle.loops:
            target_loop_use[loop] += 1
    unused = [loop for loop, count in enumerate(target_loop_use) if count == 0]
    if unused:
        _fail(
            "INVALID_REQUEST",
            "target triangles do not reference every target loop",
            unused_target_loops=unused,
        )
    for face, face_loops in enumerate(target.face_loops):
        used_loops: set[int] = set()
        for triangle_index in target.triangles_by_face[face]:
            used_loops.update(target.triangles[triangle_index].loops)
        missing_loops = sorted(set(face_loops) - used_loops)
        if missing_loops:
            _fail(
                "INVALID_REQUEST",
                "target triangles omit loops of their own face",
                target_face=face,
                missing_loops=missing_loops,
            )
    faces_without_triangles = [
        face for face, indices in enumerate(target.triangles_by_face) if not indices
    ]
    if faces_without_triangles:
        _fail(
            "INVALID_REQUEST",
            "target faces without triangles cannot receive UV",
            target_faces=faces_without_triangles,
        )
    if not source.triangles:
        _fail("INVALID_REQUEST", "source mesh has no triangles")


def surface(
    source: Mapping[str, Any],
    target: Mapping[str, Any],
    uv: Any,
    regions: Any,
    width: float,
    height: float,
    max_distance_m: float,
    normal_degrees: float,
    uv_tolerance_px: float,
    pair_budget: int,
    cancel: Callable[[], Any] | None = None,
) -> tuple[np.ndarray, dict[str, Any]]:
    """Transfer UV through an explicit, complete, single-layer projection domain.

    This is not a nearest-surface transfer.  Every target triangle must be
    covered by clipped source triangles from its explicitly declared region.
    """

    source_mesh = _parse_mesh(source, "source", require_triangles=True)
    target_mesh = _parse_mesh(target, "target", require_triangles=True)
    source_uv = _parse_uv(uv, source_mesh)
    width_value = _real_number(width, "width")
    height_value = _real_number(height, "height")
    max_distance = _real_number(max_distance_m, "max_distance_m")
    normal_limit = _real_number(normal_degrees, "normal_degrees")
    pixel_tolerance = _real_number(uv_tolerance_px, "uv_tolerance_px")
    _validate_surface_inputs(target_mesh, source_mesh, width_value, height_value)
    if max_distance < 0.0:
        _fail("INVALID_REQUEST", "max_distance_m must be non-negative")
    if normal_limit < 0.0 or normal_limit > 180.0:
        _fail("INVALID_REQUEST", "normal_degrees must be in [0, 180]")
    if pixel_tolerance <= 0.0:
        _fail("INVALID_REQUEST", "uv_tolerance_px must be positive")
    if cancel is not None and not callable(cancel):
        _fail("INVALID_REQUEST", "cancel must be callable or None")
    budget_value = _python_index(pair_budget, "pair_budget")
    if budget_value <= 0:
        _fail("INVALID_REQUEST", "pair_budget must be positive", pair_budget=budget_value)
    region_defs = _parse_regions(regions, target_mesh, source_mesh)
    face_to_region = {
        face: index
        for index, region in enumerate(region_defs)
        for face in region["target_faces"]
    }
    budget = _Budget(budget_value, cancel)
    budget.check_cancel({"phase": "surface_start"})

    uv_pixel_epsilon = float(NUMERIC_POLICY["uv_pixel_epsilon_px"])
    distance_epsilon = float(NUMERIC_POLICY["distance_epsilon_m"])
    normal_epsilon = float(NUMERIC_POLICY["normal_angle_epsilon_deg"])
    area_absolute = float(NUMERIC_POLICY["area_absolute_epsilon_m2"])
    area_relative = float(NUMERIC_POLICY["area_relative_epsilon"])
    area_fraction_limit = float(NUMERIC_POLICY["area_epsilon_max_target_fraction"])
    clip_epsilon = float(NUMERIC_POLICY["clip_cross_epsilon_m2"])

    loop_values: list[list[tuple[int, np.ndarray]]] = [
        [] for _ in range(target_mesh.loop_count)
    ]
    coverage_records: list[dict[str, Any]] = []
    final_check_records: list[dict[str, Any]] = []
    total_target_area = 0.0
    total_patch_area = 0.0
    total_patch_count = 0
    max_overlap_area = 0.0
    max_missing_area = 0.0
    min_coverage_margin = math.inf
    min_area_epsilon = math.inf
    max_area_epsilon = 0.0
    max_area_epsilon_fraction = 0.0
    max_patch_vertex_distance = 0.0
    max_patch_uv_error_px = 0.0
    max_patch_uv_error_uv = 0.0
    max_corner_disagreement_px = 0.0
    min_forward_angle_deg: float | None = None
    max_failed_normal_angle_deg = 0.0
    total_candidates = 0
    normal_rejected_candidates = 0
    degenerate_candidates = 0
    empty_candidates = 0

    for triangle in target_mesh.triangles:
        budget.check_cancel(
            {
                "phase": "target_triangle",
                "target_face": triangle.face,
                "target_triangle": triangle.index,
            }
        )
        region_index = face_to_region[triangle.face]
        region = region_defs[region_index]
        p0, axis_u, axis_v, target_normal, target_2d, target_area = _target_frame(
            target_mesh, triangle
        )
        area_epsilon = max(area_absolute, area_relative * target_area)
        area_fraction = area_epsilon / target_area
        max_area_epsilon_fraction = max(max_area_epsilon_fraction, area_fraction)
        if area_fraction > area_fraction_limit:
            _fail(
                "UNSUPPORTED",
                "target triangle is below the supported numerical area scale",
                reason="target_triangle_below_numeric_scale",
                target_name=target_mesh.name,
                target_face=triangle.face,
                target_triangle=triangle.index,
                target_area_m2=target_area,
                area_epsilon_m2=area_epsilon,
                area_epsilon_target_fraction=area_fraction,
                threshold_area_epsilon_target_fraction=area_fraction_limit,
                note=(
                    "absolute area epsilon would exceed the supported fraction of "
                    "the target triangle area; a missing region could be masked"
                ),
            )
        min_area_epsilon = min(min_area_epsilon, area_epsilon)
        max_area_epsilon = max(max_area_epsilon, area_epsilon)
        total_target_area += target_area

        candidate_stats = {
            "total": 0,
            "normal_rejected": 0,
            "degenerate": 0,
            "empty_intersection": 0,
            "patches": 0,
        }
        patches: list[_Patch] = []
        normal_fail_examples: list[dict[str, Any]] = []
        candidate_triangles = region["source_triangles"]
        for source_triangle_index in candidate_triangles:
            candidate_stats["total"] += 1
            total_candidates += 1
            budget.charge(
                {
                    "phase": "candidate",
                    "target_face": triangle.face,
                    "target_triangle": triangle.index,
                    "source_triangle": source_triangle_index,
                    "region": region_index,
                }
            )
            source_triangle = source_mesh.triangles[source_triangle_index]
            source_points = source_mesh.triangle_points(source_triangle)
            source_face_normal = source_mesh.face_normal[source_triangle.face]
            cosine = float(np.clip(float(np.dot(source_face_normal, target_normal)), -1.0, 1.0))
            angle_deg = math.degrees(math.acos(cosine))
            if min_forward_angle_deg is None or angle_deg < min_forward_angle_deg:
                min_forward_angle_deg = angle_deg
            forward = cosine > 0.0
            if not forward or angle_deg > normal_limit + normal_epsilon:
                candidate_stats["normal_rejected"] += 1
                normal_rejected_candidates += 1
                max_failed_normal_angle_deg = max(max_failed_normal_angle_deg, angle_deg)
                if len(normal_fail_examples) < 8:
                    normal_fail_examples.append(
                        {
                            "source_face": source_triangle.face,
                            "source_triangle": source_triangle.index,
                            "angle_deg": angle_deg,
                            "cosine": cosine,
                            "forward": forward,
                        }
                    )
                continue

            source_2d = _project(source_points, p0, axis_u, axis_v)
            source_projected_area = _area(source_2d)
            if source_projected_area <= area_epsilon:
                candidate_stats["degenerate"] += 1
                degenerate_candidates += 1
                continue
            clipped = _clip_polygon(source_2d, target_2d)
            patch_area = _area(clipped)
            if clipped.shape[0] < 3 or patch_area <= area_epsilon:
                candidate_stats["empty_intersection"] += 1
                empty_candidates += 1
                continue
            clipped = _normalize_ccw(clipped)

            patch_vertex_distance = 0.0
            for point_2d in clipped:
                weights = _safe_barycentric(
                    source_2d,
                    point_2d,
                    {
                        "target_face": triangle.face,
                        "target_triangle": triangle.index,
                        "source_face": source_triangle.face,
                        "source_triangle": source_triangle.index,
                    },
                )
                point_3d = weights @ source_points
                distance = abs(float(np.dot(point_3d - p0, target_normal)))
                patch_vertex_distance = max(patch_vertex_distance, distance)
            if patch_vertex_distance > max_distance + distance_epsilon:
                _fail(
                    "VALIDATION_FAILED",
                    "a positive-area candidate patch exceeds max_distance_m",
                    reason="distance_exceeded",
                    target_name=target_mesh.name,
                    target_face=triangle.face,
                    target_triangle=triangle.index,
                    source_name=source_mesh.name,
                    source_face=source_triangle.face,
                    source_triangle=source_triangle.index,
                    actual_max_distance_m=patch_vertex_distance,
                    threshold_max_distance_m=max_distance,
                    numerical_epsilon_m=distance_epsilon,
                    patch_area_m2=patch_area,
                )
            max_patch_vertex_distance = max(max_patch_vertex_distance, patch_vertex_distance)
            patch_uv = np.asarray(
                [source_uv[loop] for loop in source_triangle.loops], dtype=np.float64
            )
            patch = _Patch(
                source_triangle=source_triangle,
                source_triangle_2d=source_2d,
                source_uv=patch_uv,
                polygon=clipped,
                area=patch_area,
                max_vertex_distance=patch_vertex_distance,
            )
            patches.append(patch)
            candidate_stats["patches"] += 1

        if not patches:
            details: dict[str, Any] = {
                "reason": "missing_coverage",
                "target_name": target_mesh.name,
                "target_face": triangle.face,
                "target_triangle": triangle.index,
                "target_face_area_m2": target_area,
                "candidate_counts": candidate_stats,
                "region": region_index,
                "normal_degrees_threshold": normal_limit,
                "min_forward_angle_deg": min_forward_angle_deg,
                "max_failed_normal_angle_deg": max_failed_normal_angle_deg,
                "normal_fail_examples": normal_fail_examples,
            }
            if candidate_stats["total"] == 0:
                details["reason"] = "missing_coverage_no_candidate_triangles"
            elif candidate_stats["normal_rejected"] == candidate_stats["total"]:
                details["reason"] = "no_forward_normal_coverage"
            _fail(
                "VALIDATION_FAILED",
                "target triangle has no positive-area source patch",
                **details,
            )

        for first_index in range(len(patches)):
            for second_index in range(first_index + 1, len(patches)):
                first = patches[first_index]
                second = patches[second_index]
                budget.charge(
                    {
                        "phase": "patch_overlap",
                        "target_face": triangle.face,
                        "target_triangle": triangle.index,
                        "source_triangle_a": first.source_triangle.index,
                        "source_triangle_b": second.source_triangle.index,
                    }
                )
                intersection = _clip_polygon(first.polygon, second.polygon)
                overlap_area = _area(intersection)
                max_overlap_area = max(max_overlap_area, overlap_area)
                if intersection.shape[0] >= 3 and overlap_area > area_epsilon:
                    _fail(
                        "CONFLICT",
                        "positive-area source patches overlap",
                        reason="overlapping_patches",
                        target_name=target_mesh.name,
                        target_face=triangle.face,
                        target_triangle=triangle.index,
                        source_a={
                            "source_face": first.source_triangle.face,
                            "source_triangle": first.source_triangle.index,
                        },
                        source_b={
                            "source_face": second.source_triangle.face,
                            "source_triangle": second.source_triangle.index,
                        },
                        overlap_area_m2=overlap_area,
                        area_epsilon_m2=area_epsilon,
                    )

        patch_area_sum = float(sum(patch.area for patch in patches))
        total_patch_area += patch_area_sum
        total_patch_count += len(patches)
        missing_area = target_area - patch_area_sum
        max_missing_area = max(max_missing_area, max(0.0, missing_area))
        min_coverage_margin = min(min_coverage_margin, -missing_area)
        if missing_area > area_epsilon:
            _fail(
                "VALIDATION_FAILED",
                "source patches do not completely cover the target triangle",
                reason="missing_coverage",
                target_name=target_mesh.name,
                target_face=triangle.face,
                target_triangle=triangle.index,
                target_face_area_m2=target_area,
                covered_area_m2=patch_area_sum,
                missing_area_m2=missing_area,
                area_epsilon_m2=area_epsilon,
                candidate_counts=candidate_stats,
                normal_degrees_threshold=normal_limit,
                max_failed_normal_angle_deg=max_failed_normal_angle_deg,
            )
        if missing_area < -area_epsilon:
            _fail(
                "VALIDATION_FAILED",
                "covered area exceeds the target triangle after overlap rejection",
                reason="coverage_area_exceeds_target",
                target_face=triangle.face,
                target_triangle=triangle.index,
                target_face_area_m2=target_area,
                covered_area_m2=patch_area_sum,
                excess_area_m2=-missing_area,
                area_epsilon_m2=area_epsilon,
            )

        corner_uv = np.zeros((3, 2), dtype=np.float64)
        corner_sources: list[list[dict[str, Any]]] = [[], [], []]
        for corner_index in range(3):
            corner = target_2d[corner_index]
            values: list[tuple[np.ndarray, _Patch]] = []
            for patch in patches:
                if _point_in_convex(patch.polygon, corner):
                    values.append((patch.source_uv_at(corner), patch))
            if not values:
                _fail(
                    "VALIDATION_FAILED",
                    "a target triangle corner has no positive-area patch coverage",
                    reason="target_corner_not_covered",
                    target_face=triangle.face,
                    target_triangle=triangle.index,
                    corner=corner_index,
                    target_loop=triangle.loops[corner_index],
                    corner_2d=_vec(corner),
                    target_face_area_m2=target_area,
                    covered_area_m2=patch_area_sum,
                )
            base_uv = values[0][0]
            for other_uv, other_patch in values[1:]:
                delta_px, delta_uv = _pixel_delta(
                    other_uv, base_uv, width_value, height_value
                )
                max_corner_disagreement_px = max(max_corner_disagreement_px, delta_px)
                if delta_px > pixel_tolerance + uv_pixel_epsilon:
                    _fail(
                        "CONFLICT",
                        "a target triangle corner receives multiple incompatible UV values",
                        reason="target_corner_multivalued",
                        target_name=target_mesh.name,
                        target_face=triangle.face,
                        target_triangle=triangle.index,
                        corner=corner_index,
                        target_loop=triangle.loops[corner_index],
                        values=[
                            {"uv": _vec(other_uv), "source": _patch_source(other_patch)},
                            {"uv": _vec(base_uv), "source": _patch_source(values[0][1])},
                        ],
                        delta_px=delta_px,
                        delta_uv=delta_uv,
                        threshold_px=pixel_tolerance,
                    )
            corner_uv[corner_index] = base_uv
            for value, patch in values:
                corner_sources[corner_index].append(
                    {
                        "source_face": patch.source_triangle.face,
                        "source_triangle": patch.source_triangle.index,
                        "uv": _vec(value),
                    }
                )

        for patch in patches:
            for point_2d in patch.polygon:
                source_uv_at_point = patch.source_uv_at(point_2d)
                weights = _barycentric_2d(target_2d, point_2d)
                target_uv_at_point = weights @ corner_uv
                delta_px, delta_uv = _pixel_delta(
                    target_uv_at_point,
                    source_uv_at_point,
                    width_value,
                    height_value,
                )
                max_patch_uv_error_px = max(max_patch_uv_error_px, delta_px)
                max_patch_uv_error_uv = max(max_patch_uv_error_uv, delta_uv)
                if delta_px > pixel_tolerance + uv_pixel_epsilon:
                    target_corner_distance = min(
                        float(np.linalg.norm(point_2d - target_2d[index]))
                        for index in range(3)
                    )
                    _fail(
                        "UNSUPPORTED",
                        "source piecewise affine UV cannot be expressed by the target triangle",
                        reason="piecewise_uv_not_representable",
                        target_name=target_mesh.name,
                        target_face=triangle.face,
                        target_triangle=triangle.index,
                        source_face=patch.source_triangle.face,
                        source_triangle=patch.source_triangle.index,
                        patch_vertex_2d=_vec(point_2d),
                        patch_vertex_is_target_corner=target_corner_distance
                        <= float(NUMERIC_POLICY["polygon_point_epsilon_m"]),
                        target_interpolated_uv=_vec(target_uv_at_point),
                        source_piecewise_uv=_vec(source_uv_at_point),
                        delta_px=delta_px,
                        delta_uv=delta_uv,
                        threshold_px=pixel_tolerance,
                        target_corner_uv=[_vec(v) for v in corner_uv],
                        patch_area_m2=patch.area,
                    )

        for corner_index, target_loop in enumerate(triangle.loops):
            loop_values[target_loop].append((triangle.index, corner_uv[corner_index].copy()))
        coverage_records.append(
            {
                "target_face": triangle.face,
                "target_triangle": triangle.index,
                "region": region_index,
                "target_area_m2": target_area,
                "covered_area_m2": patch_area_sum,
                "area_epsilon_m2": area_epsilon,
                "patch_count": len(patches),
                "candidate_count": candidate_stats["total"],
                "source_faces": [
                    patch.source_triangle.face for patch in patches
                ],
                "source_triangles": [
                    patch.source_triangle.index for patch in patches
                ],
                "corner_source_counts": [len(v) for v in corner_sources],
            }
        )
        final_check_records.append(
            {
                "target_face": triangle.face,
                "target_triangle": triangle.index,
                "target_loops": triangle.loops,
                "target_2d": target_2d,
                "patches": patches,
            }
        )

    output = np.empty((target_mesh.loop_count, 2), dtype=np.float32)
    max_shared_loop_delta_px = 0.0
    max_shared_loop_delta_f32_px = 0.0
    for target_loop, values in enumerate(loop_values):
        if not values:
            _fail(
                "VALIDATION_FAILED",
                "a target loop received no target-triangle UV result",
                reason="missing_target_loop_uv",
                target_loop=target_loop,
            )
        base_uv = values[0][1]
        accumulated_raw_px = 0.0
        for triangle_index, value in values[1:]:
            delta_px, delta_uv = _pixel_delta(
                value, base_uv, width_value, height_value
            )
            accumulated_raw_px = max(accumulated_raw_px, delta_px)
            if delta_px > pixel_tolerance + uv_pixel_epsilon:
                _fail(
                    "CONFLICT",
                    "shared target loops receive incompatible UV values",
                    reason="shared_target_loop_conflict",
                    target_name=target_mesh.name,
                    target_loop=target_loop,
                    target_face=target_mesh.loop_face[target_loop],
                    first_triangle=values[0][0],
                    conflicting_triangle=triangle_index,
                    first_uv=_vec(base_uv),
                    other_uv=_vec(value),
                    delta_px=delta_px,
                    delta_uv=delta_uv,
                    threshold_px=pixel_tolerance,
                )
        max_shared_loop_delta_px = max(max_shared_loop_delta_px, accumulated_raw_px)
        f32_values = [
            np.asarray(value, dtype=np.float32) for _, value in values
        ]
        base_f32 = f32_values[0]
        for (triangle_index, _), value_f32 in zip(values[1:], f32_values[1:]):
            delta_px, delta_uv = _pixel_delta(
                value_f32.astype(np.float64),
                base_f32.astype(np.float64),
                width_value,
                height_value,
            )
            max_shared_loop_delta_f32_px = max(
                max_shared_loop_delta_f32_px, delta_px
            )
            if delta_px > pixel_tolerance + uv_pixel_epsilon:
                _fail(
                    "CONFLICT",
                    "shared target loops conflict after final float32 conversion",
                    reason="shared_target_loop_conflict_float32",
                    target_name=target_mesh.name,
                    target_loop=target_loop,
                    target_face=target_mesh.loop_face[target_loop],
                    first_triangle=values[0][0],
                    conflicting_triangle=triangle_index,
                    first_uv=_vec(base_f32),
                    other_uv=_vec(value_f32),
                    delta_px=delta_px,
                    delta_uv=delta_uv,
                    threshold_px=pixel_tolerance,
                )
        output[target_loop] = base_f32

    if not np.all(np.isfinite(output)):
        _fail(
            "VALIDATION_FAILED",
            "final float32 UV contains a non-finite value",
            reason="non_finite_output",
        )

    max_final_float32_patch_error_px = 0.0
    final_float32_patch_vertex_checks = 0
    for record in final_check_records:
        target_2d = record["target_2d"]
        target_loops = record["target_loops"]
        corner_uv_float32 = output[list(target_loops)]
        corner_uv = corner_uv_float32.astype(np.float64)
        for patch in record["patches"]:
            for point_2d in patch.polygon:
                weights = _barycentric_2d(target_2d, point_2d)
                target_uv_at_point = weights @ corner_uv
                source_uv_at_point = patch.source_uv_at(point_2d)
                delta_px, delta_uv = _pixel_delta(
                    target_uv_at_point,
                    source_uv_at_point,
                    width_value,
                    height_value,
                )
                max_final_float32_patch_error_px = max(
                    max_final_float32_patch_error_px, delta_px
                )
                final_float32_patch_vertex_checks += 1
                if delta_px > pixel_tolerance + uv_pixel_epsilon:
                    _fail(
                        "VALIDATION_FAILED",
                        "final float32 output fails patch-vertex re-verification",
                        reason="final_float32_patch_verification_failed",
                        target_name=target_mesh.name,
                        target_face=record["target_face"],
                        target_triangle=record["target_triangle"],
                        target_loops=list(target_loops),
                        target_corner_uv_float32=[
                            _vec(row) for row in corner_uv_float32
                        ],
                        patch_vertex_2d=_vec(point_2d),
                        target_interpolated_uv=_vec(target_uv_at_point),
                        source_piecewise_uv=_vec(source_uv_at_point),
                        source_face=patch.source_triangle.face,
                        source_triangle=patch.source_triangle.index,
                        patch_area_m2=patch.area,
                        delta_px=delta_px,
                        delta_uv=delta_uv,
                        threshold_px=pixel_tolerance,
                        numerical_epsilon_px=uv_pixel_epsilon,
                        pre_round_max_patch_error_px=max_patch_uv_error_px,
                        final_float32_output_checked=True,
                    )

    report = {
        "mode": "surface",
        "method": "explicit_domain_clipped_projection_v1",
        "source": {
            "name": source_mesh.name,
            "vertices": source_mesh.vertex_count,
            "faces": source_mesh.face_count,
            "loops": source_mesh.loop_count,
            "triangles": len(source_mesh.triangles),
        },
        "target": {
            "name": target_mesh.name,
            "vertices": target_mesh.vertex_count,
            "faces": target_mesh.face_count,
            "loops": target_mesh.loop_count,
            "triangles": len(target_mesh.triangles),
        },
        "parameters": {
            "width": width_value,
            "height": height_value,
            "max_distance_m": max_distance,
            "normal_degrees": normal_limit,
            "uv_tolerance_px": pixel_tolerance,
            "pair_budget": budget_value,
        },
        "thresholds": {
            **NUMERIC_POLICY,
            "per_triangle_area_epsilon_min_m2": min_area_epsilon,
            "per_triangle_area_epsilon_max_m2": max_area_epsilon,
            "max_area_epsilon_target_fraction_seen": max_area_epsilon_fraction,
        },
        "regions": [
            {
                "region": region["region"],
                "target_faces": region["target_faces"],
                "source_faces": region["source_faces"],
                "source_triangles": len(region["source_triangles"]),
            }
            for region in region_defs
        ],
        "coverage": {
            "verified_target_triangles": len(coverage_records),
            "target_area_m2": total_target_area,
            "covered_patch_area_m2": total_patch_area,
            "patch_count": total_patch_count,
            "max_overlap_area_m2": max_overlap_area,
            "max_missing_area_m2": max_missing_area,
            "min_signed_coverage_margin_m2": min_coverage_margin,
            "triangles": coverage_records,
        },
        "distance": {
            "max_patch_vertex_distance_m": max_patch_vertex_distance,
            "threshold_m": max_distance,
            "numerical_epsilon_m": distance_epsilon,
        },
        "normals": {
            "threshold_degrees": normal_limit,
            "min_forward_angle_deg_seen": min_forward_angle_deg,
            "max_failed_normal_angle_deg": max_failed_normal_angle_deg,
            "normal_rejected_candidates": normal_rejected_candidates,
        },
        "uv": {
            "output_dtype": "float32",
            "float32_tolerance_scope": "PROJECTED surface path only",
            "pixel_metric": NUMERIC_POLICY["uv_pixel_metric"],
            "threshold_px": pixel_tolerance,
            "numerical_epsilon_px": uv_pixel_epsilon,
            "max_target_corner_disagreement_px": max_corner_disagreement_px,
            "max_patch_interpolation_error_px": max_patch_uv_error_px,
            "max_patch_interpolation_error_uv": max_patch_uv_error_uv,
            "max_final_float32_patch_error_px": max_final_float32_patch_error_px,
            "final_float32_patch_vertex_checks": final_float32_patch_vertex_checks,
            "max_shared_target_loop_delta_px": max_shared_loop_delta_px,
            "max_shared_target_loop_float32_delta_px": max_shared_loop_delta_f32_px,
        },
        "pair_checks": {
            "used": budget.used,
            "consumed": budget.used,
            "budget": budget.budget,
            "remaining": budget.budget - budget.used,
            "scope": (
                "per surface() call; parent must deduct consumed from the shared "
                "manifest budget before the next target call"
            ),
            "per_call": True,
            "deduct_from_manifest": True,
            "definition": "one charge per source-triangle candidate and per patch-pair overlap test",
            "candidate_checks": total_candidates,
            "patch_pair_checks": budget.used - total_candidates,
            "candidate_count": total_candidates,
            "normal_rejected_candidates": normal_rejected_candidates,
            "degenerate_candidates": degenerate_candidates,
            "empty_candidates": empty_candidates,
        },
        "support": {
            "class": "explicit-domain, near-coplanar, single-layer, complete clipped-projection coverage",
            "interpolation_proof": (
                "on each clipped positive-area patch both the target-corner affine "
                "interpolation and the source triangle UV are affine; equality at "
                "all patch polygon vertices proves equality over the whole patch, "
                "and no-overlap plus equal total area proves target coverage; the "
                "final float32 target-corner values are rechecked at every patch "
                "vertex"
            ),
            "arbitrary_nearest_surface_transfer": False,
            "topology_modified": False,
            "source_face_split_allowed": False,
            "target_face_split_allowed": False,
            "source_purpose_approval_inherited": False,
            "out_of_scope": [
                "multiple overlapping surface layers",
                "holes or incomplete explicit domains",
                "source UV seams or non-affine variation that a target triangle cannot express",
                "target loops whose required UV is inconsistent across triangles",
                "high-density performance qualification",
            ],
        },
    }
    return output, report


def _patch_source(patch: _Patch) -> dict[str, Any]:
    return {
        "source_face": patch.source_triangle.face,
        "source_triangle": patch.source_triangle.index,
    }


__all__ = [
    "ERROR_CODES",
    "NUMERIC_POLICY",
    "TransferError",
    "indexed_map",
    "surface",
    "topology",
]
