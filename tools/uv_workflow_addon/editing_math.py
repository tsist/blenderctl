"""Pure-NumPy UV editing math.

Pixel space is ``(U * width, V * height)``.  A positive rotation angle is
counter-clockwise in that space.  The public function validates one explicit
selection and action, copies the complete float32 UV array, changes only the
selected entries, and returns compact diagnostics.  It does not import ``bpy``
or the production contract and never modifies its inputs.
"""

from collections.abc import Mapping

import numpy as np

__all__ = ["apply_coordinates"]


_ACTION_FIELDS = {
    "translate": frozenset(("kind", "delta_px")),
    "rotate": frozenset(("kind", "angle_degrees", "pivot_px")),
    "scale": frozenset(("kind", "factors", "pivot_px")),
    "mirror": frozenset(("kind", "axis", "pivot_px")),
    "align": frozenset(("kind", "axis", "value_px")),
    "straighten": frozenset(("kind",)),
}

_REQUIRED_FIELDS = {
    "translate": frozenset(("kind", "delta_px")),
    "rotate": frozenset(("kind", "angle_degrees")),
    "scale": frozenset(("kind", "factors")),
    "mirror": frozenset(("kind", "axis")),
    "align": frozenset(("kind", "axis")),
    "straighten": frozenset(("kind",)),
}

_QUARTER_TURN_COSINES = (
    (1.0, 0.0),
    (0.0, 1.0),
    (-1.0, 0.0),
    (0.0, -1.0),
)


def _validate_uv(uv):
    try:
        source = np.asarray(uv)
    except Exception as exc:
        raise ValueError("uv must be an Nx2 numeric array") from exc
    if source.ndim != 2 or source.shape[1] != 2:
        raise ValueError("uv must have shape (N, 2)")
    if source.dtype.kind not in "iuf":
        raise ValueError("uv must contain real numeric values")
    if not bool(np.isfinite(source).all()):
        raise ValueError("uv values must all be finite")
    try:
        with np.errstate(all="ignore"):
            result = np.array(source, dtype=np.float32, copy=True, order="C")
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("uv cannot be represented as float32") from exc
    if not bool(np.isfinite(result).all()):
        raise ValueError("uv values must fit finite float32")
    return result


def _validate_indices(indices, count):
    try:
        items = list(indices)
    except TypeError as exc:
        raise ValueError("indices must be a non-empty sequence of integers") from exc
    if not items:
        raise ValueError("indices must not be empty")

    selected = []
    seen = set()
    for item in items:
        if isinstance(item, (bool, np.bool_)) or not isinstance(item, (int, np.integer)):
            raise ValueError("each index must be a real, non-boolean integer")
        index = int(item)
        if index < 0 or index >= count:
            raise ValueError("index outside the UV array")
        if index in seen:
            raise ValueError("indices must be unique")
        seen.add(index)
        selected.append(index)
    return np.asarray(selected, dtype=np.intp)


def _positive_dimension(value, name):
    if isinstance(value, (bool, np.bool_)) or not isinstance(value, (int, np.integer)):
        raise ValueError(name + " must be a positive integer")
    integer = int(value)
    if integer <= 0:
        raise ValueError(name + " must be a positive integer")
    try:
        as_float = float(integer)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(name + " is not finite") from exc
    if not np.isfinite(as_float):
        raise ValueError(name + " is not finite")
    return as_float


def _finite_number(value, name):
    if isinstance(value, (bool, np.bool_)) or not isinstance(
            value, (int, float, np.integer, np.floating)):
        raise ValueError(name + " must be a finite number")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(name + " must be a finite number") from exc
    if not np.isfinite(number):
        raise ValueError(name + " must be finite")
    return number


def _finite_pair(value, name):
    try:
        array = np.asarray(value)
    except Exception as exc:
        raise ValueError(name + " must contain two finite numbers") from exc
    if array.shape != (2,) or array.dtype.kind not in "iuf":
        raise ValueError(name + " must contain two finite numbers")
    try:
        with np.errstate(all="ignore"):
            pair = array.astype(np.float64, copy=True)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(name + " must contain two finite numbers") from exc
    if not bool(np.isfinite(pair).all()):
        raise ValueError(name + " must contain two finite numbers")
    return pair


def _axis(value, name="axis"):
    if not isinstance(value, str) or value not in ("U", "V"):
        raise ValueError(name + " must be 'U' or 'V'")
    return value


def _action_kind(action):
    if not isinstance(action, Mapping):
        raise ValueError("action must be a mapping")
    keys = set(action.keys())
    if "kind" not in keys:
        raise ValueError("action is missing kind")
    kind = action["kind"]
    if not isinstance(kind, str) or kind not in _ACTION_FIELDS:
        raise ValueError("unsupported action kind")

    allowed = _ACTION_FIELDS[kind]
    required = _REQUIRED_FIELDS[kind]
    missing = required - keys
    if missing:
        raise ValueError("action is missing field(s): " + ", ".join(sorted(missing)))
    unknown = keys - allowed
    if unknown:
        raise ValueError("action has unknown field(s): "
                         + ", ".join(sorted(repr(key) for key in unknown)))
    return kind


def _details(kind, selected_count):
    return {"kind": kind, "selected_count": int(selected_count)}


def _set_pivot(details, pivot):
    details["pivot_px"] = [float(pivot[0]), float(pivot[1])]


def _checked(values, message):
    array = np.asarray(values, dtype=np.float64)
    if not bool(np.isfinite(array).all()):
        raise ValueError(message)
    return array


def _selected_pixels(original, indices, dimensions):
    selected = original[indices].astype(np.float64, copy=False)
    with np.errstate(over="ignore", invalid="ignore"):
        pixels = selected * dimensions
    return _checked(pixels, "selected pixel coordinates overflow")


def _bbox_center(pixels):
    low = np.min(pixels, axis=0)
    high = np.max(pixels, axis=0)
    with np.errstate(over="ignore", invalid="ignore"):
        center = low * 0.5 + high * 0.5
    return _checked(center, "selected pixel bounds overflow")


def _resolve_pivot(action, pixels):
    if "pivot_px" in action:
        return _finite_pair(action["pivot_px"], "pivot_px")
    return _bbox_center(pixels)


def _to_float32(values, name):
    try:
        with np.errstate(all="ignore"):
            array = np.array(values, dtype=np.float32, copy=True, order="C")
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(name + " cannot be represented as float32") from exc
    if not bool(np.isfinite(array).all()):
        raise ValueError(name + " overflows finite float32")
    return array


def _pixels_to_uv(pixels, divisor, name):
    with np.errstate(over="ignore", invalid="ignore"):
        values = np.asarray(pixels, dtype=np.float64) / divisor
    values = _checked(values, name + " overflow")
    return _to_float32(values, name)


def _finish(original, indices, result, details):
    if result.dtype != np.float32 or result.shape != original.shape:
        raise ValueError("internal output shape or dtype mismatch")
    if not bool(np.isfinite(result).all()):
        raise ValueError("output must contain only finite float32 values")
    changed = np.any(result[indices] != original[indices], axis=1)
    details["changed_count"] = int(np.count_nonzero(changed))
    return result, details


def _rotation_terms(angle_mod):
    quarter_turns = angle_mod / 90.0
    if quarter_turns.is_integer():
        cosine, sine = _QUARTER_TURN_COSINES[int(quarter_turns) % 4]
        return cosine, sine
    radians = float(np.deg2rad(angle_mod))
    with np.errstate(over="ignore", invalid="ignore"):
        cosine = float(np.cos(radians))
        sine = float(np.sin(radians))
    if not np.isfinite(cosine) or not np.isfinite(sine):
        raise ValueError("rotation angle cannot be represented")
    return cosine, sine


def apply_coordinates(uv, indices, action, width, height):
    """Apply one action to selected loop UVs and return ``(new_uv, details)``.

    ``uv`` is an Nx2 real array (normally float32), ``indices`` is a non-empty,
    unique, ordered sequence of loop indices, and width/height are positive
    integers.  The returned array is always a complete float32 copy.  Invalid
    input and finite-float32 output overflow raise :class:`ValueError`.
    """

    original = _validate_uv(uv)
    selected_indices = _validate_indices(indices, len(original))
    width_float = _positive_dimension(width, "width")
    height_float = _positive_dimension(height, "height")
    dimensions = np.asarray((width_float, height_float), dtype=np.float64)

    kind = _action_kind(action)
    pixels = _selected_pixels(original, selected_indices, dimensions)
    details = _details(kind, len(selected_indices))

    if kind == "translate":
        delta = _finite_pair(action["delta_px"], "delta_px")
        if delta[0] == 0.0 and delta[1] == 0.0:
            return _finish(original, selected_indices, original.copy(), details)
        with np.errstate(over="ignore", invalid="ignore"):
            translated = pixels + delta
        translated = _checked(translated, "translated pixel coordinates overflow")
        rows = _pixels_to_uv(translated, dimensions, "translate output")
        if delta[0] == 0.0:
            rows[:, 0] = original[selected_indices, 0]
        if delta[1] == 0.0:
            rows[:, 1] = original[selected_indices, 1]
        result = original.copy()
        result[selected_indices] = rows
        return _finish(original, selected_indices, result, details)

    if kind == "rotate":
        angle = _finite_number(action["angle_degrees"], "angle_degrees")
        angle_mod = float(np.fmod(angle, 360.0))
        pivot = _resolve_pivot(action, pixels)
        _set_pivot(details, pivot)
        if angle_mod == 0.0:
            return _finish(original, selected_indices, original.copy(), details)

        cosine, sine = _rotation_terms(angle_mod)
        with np.errstate(over="ignore", invalid="ignore"):
            offsets = pixels - pivot
        offsets = _checked(offsets, "rotation offsets overflow")
        rotated = np.empty_like(pixels)
        with np.errstate(over="ignore", invalid="ignore"):
            rotated[:, 0] = (
                pivot[0] + cosine * offsets[:, 0] - sine * offsets[:, 1])
            rotated[:, 1] = (
                pivot[1] + sine * offsets[:, 0] + cosine * offsets[:, 1])
        rotated = _checked(rotated, "rotated pixel coordinates overflow")
        rows = _pixels_to_uv(rotated, dimensions, "rotate output")
        at_pivot = (offsets[:, 0] == 0.0) & (offsets[:, 1] == 0.0)
        if bool(np.any(at_pivot)):
            rows[at_pivot] = original[selected_indices][at_pivot]
        result = original.copy()
        result[selected_indices] = rows
        return _finish(original, selected_indices, result, details)

    if kind == "scale":
        factors = _finite_pair(action["factors"], "factors")
        if bool(np.any(factors <= 0.0)):
            raise ValueError("scale factors must both be strictly positive")
        pivot = _resolve_pivot(action, pixels)
        _set_pivot(details, pivot)
        if factors[0] == 1.0 and factors[1] == 1.0:
            return _finish(original, selected_indices, original.copy(), details)

        with np.errstate(over="ignore", invalid="ignore"):
            offsets = pixels - pivot
        offsets = _checked(offsets, "scale offsets overflow")
        with np.errstate(over="ignore", invalid="ignore"):
            scaled = pivot + offsets * factors
        scaled = _checked(scaled, "scaled pixel coordinates overflow")
        rows = _pixels_to_uv(scaled, dimensions, "scale output")
        original_rows = original[selected_indices]
        for axis in (0, 1):
            if factors[axis] == 1.0:
                rows[:, axis] = original_rows[:, axis]
            else:
                fixed = offsets[:, axis] == 0.0
                if bool(np.any(fixed)):
                    rows[fixed, axis] = original_rows[fixed, axis]
        result = original.copy()
        result[selected_indices] = rows
        return _finish(original, selected_indices, result, details)

    if kind == "mirror":
        axis = _axis(action["axis"])
        pivot = _resolve_pivot(action, pixels)
        _set_pivot(details, pivot)
        component = 0 if axis == "U" else 1
        with np.errstate(over="ignore", invalid="ignore"):
            reflected = 2.0 * pivot[component] - pixels[:, component]
        reflected = _checked(reflected, "mirrored pixel coordinates overflow")
        values = _pixels_to_uv(
            reflected, dimensions[component], "mirror output")
        fixed = pixels[:, component] == pivot[component]
        if bool(np.any(fixed)):
            values[fixed] = original[selected_indices[fixed], component]
        result = original.copy()
        result[selected_indices, component] = values
        return _finish(original, selected_indices, result, details)

    if kind == "align":
        axis = _axis(action["axis"])
        component = 0 if axis == "U" else 1
        if "value_px" in action:
            value = _finite_number(action["value_px"], "value_px")
        else:
            with np.errstate(over="ignore", invalid="ignore"):
                value = float(np.mean(pixels[:, component], dtype=np.float64))
            if not np.isfinite(value):
                raise ValueError("selected axis mean is not finite")

        target = float(_pixels_to_uv(
            np.asarray(value, dtype=np.float64),
            dimensions[component],
            "align output",
        ))
        original_rows = original[selected_indices]
        values = np.full(len(selected_indices), target, dtype=np.float32)
        unchanged = pixels[:, component] == value
        if bool(np.any(unchanged)):
            values[unchanged] = original_rows[unchanged, component]
        result = original.copy()
        result[selected_indices, component] = values
        return _finish(original, selected_indices, result, details)

    if kind == "straighten":
        if len(selected_indices) < 3:
            raise ValueError("straighten requires at least three selected points")
        if bool(np.array_equal(pixels[0], pixels[-1])):
            raise ValueError("straighten endpoints must be distinct")

        with np.errstate(over="ignore", invalid="ignore"):
            differences = np.diff(pixels, axis=0)
        differences = _checked(differences, "straighten segment overflow")
        with np.errstate(over="ignore", invalid="ignore"):
            lengths = np.linalg.norm(differences, axis=1)
        lengths = _checked(lengths, "straighten segment length overflow")
        cumulative = np.empty(len(pixels), dtype=np.float64)
        cumulative[0] = 0.0
        with np.errstate(over="ignore", invalid="ignore"):
            cumulative[1:] = np.cumsum(lengths, dtype=np.float64)
        cumulative = _checked(cumulative, "straighten total length overflow")
        total_length = float(cumulative[-1])
        if total_length <= 0.0:
            raise ValueError("straighten total length must be positive")

        parameters = cumulative / total_length
        parameters[0] = 0.0
        parameters[-1] = 1.0
        with np.errstate(over="ignore", invalid="ignore"):
            end_delta = pixels[-1] - pixels[0]
            straightened = pixels[0] + parameters[:, None] * end_delta
        straightened = _checked(
            straightened, "straightened pixel coordinates overflow")
        rows = _pixels_to_uv(straightened, dimensions, "straighten output")
        rows[0] = original[selected_indices[0]]
        rows[-1] = original[selected_indices[-1]]
        result = original.copy()
        result[selected_indices] = rows
        return _finish(original, selected_indices, result, details)

    raise ValueError("unsupported action kind")
