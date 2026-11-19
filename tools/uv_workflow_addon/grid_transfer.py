"""Exact preflight and in-memory copying for level-0 bilinear atlas translations.

Coordinates are explicit triangle corners in texel units, bottom-left origin.
Source and destination corner order is part of the contract.
"""
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass

import numpy as np


DEFAULT_TEST_BUDGET = 150_000_000
DEFAULT_ATLAS_BUDGET = 16_777_216
SCHEMA = "continuous-bilinear-translation-preflight/1.0"


class Refusal(ValueError):
    def __init__(self, reason, *, report=None, **witness):
        super().__init__(reason)
        self.detail = {"reason": reason, **witness}
        self.report = report if report is not None else {
            "schema": SCHEMA, "status": "refused", "errors": [self.detail],
        }


def integer_pair(value, label, minimum=None, maximum=None):
    if isinstance(value, np.ndarray) and value.shape != (2,):
        raise Refusal("expected_integer_pair", field=label)
    if not isinstance(value, (list, tuple, np.ndarray)) or len(value) != 2:
        raise Refusal("expected_integer_pair", field=label)
    if not all(isinstance(part, (int, np.integer)) and not isinstance(part, bool) for part in value):
        raise Refusal("expected_integer_pair", field=label)
    result = tuple(int(x) for x in value)
    if minimum is not None and min(result) < minimum:
        raise Refusal("integer_below_minimum", field=label, value=result)
    if maximum is not None and max(result) > maximum:
        raise Refusal("integer_above_maximum", field=label, value=result)
    return result


def lattice(values, size, q, label):
    array = np.asarray(values)
    if array.ndim != 3 or array.shape[1:] != (3, 2) or not len(array):
        raise Refusal("expected_nonempty_triangles", field=label)
    if array.dtype.kind not in "fiu" or array.dtype.itemsize > 8:
        raise Refusal("non_numeric_corners", field=label)
    if not np.isfinite(array).all():
        raise Refusal("nonfinite_corners", field=label)
    if (array < 0).any() or (array > size).any():
        raise Refusal("triangle_corners_out_of_atlas", field=label)
    scaled = array.astype(np.float64) * q
    rounded = np.rint(scaled)
    if not np.array_equal(scaled, rounded):
        face, corner, axis = map(int, np.argwhere(scaled != rounded)[0])
        raise Refusal("corner_off_requested_lattice", field=label, face=face, corner=corner, axis=axis)
    return rounded.astype(np.int64)


def tap_box(points, q):
    """Inclusive texel-index box for closed support, before exact SAT."""
    lo = (points.min(axis=0) - 3*q//2 + q-1) // q
    hi = (points.max(axis=0) + q//2) // q
    return lo, hi


def supports(triangle, q, lower, upper):
    x = np.arange(lower[0], upper[0] + 1, dtype=np.int64)
    y = np.arange(lower[1], upper[1] + 1, dtype=np.int64)
    cx = (2*x + 1) * (q//2)
    cy = (2*y + 1) * (q//2)
    mins, maxs = triangle.min(axis=0), triangle.max(axis=0)
    px = (maxs[0] > cx-q) & (mins[0] < cx+q)
    py = (maxs[1] > cy-q) & (mins[1] < cy+q)
    positive = py[:, None] & px[None, :]
    bx = (maxs[0] >= cx-q) & (mins[0] <= cx+q)
    by = (maxs[1] >= cy-q) & (mins[1] <= cy+q)
    closed = by[:, None] & bx[None, :]
    for a, b in zip(triangle, np.roll(triangle, -1, axis=0), strict=True):
        ex, ey = map(int, b-a)
        projected = triangle[:, 0]*ey - triangle[:, 1]*ex
        center = cx[None, :]*ey - cy[:, None]*ex
        radius = (abs(ey) + abs(ex))*q
        left = center-radius
        right = center+radius
        minimum, maximum = projected.min(), projected.max()
        positive &= (minimum < right) & (maximum > left)
        closed &= (minimum <= right) & (maximum >= left)
    return positive, closed


def union_support(triangles, q, budget):
    lo, hi = tap_box(triangles.reshape(-1, 2), q)
    shape = tuple(int(v) for v in (hi-lo+1)[::-1])
    positive = np.zeros(shape, dtype=bool)
    closed = np.zeros(shape, dtype=bool)
    work = 0
    for triangle in triangles:
        low, high = tap_box(triangle, q)
        w, h = map(int, high-low+1)
        work += w*h
        if work > budget:
            raise Refusal("sat_work_budget_exceeded")
        x0, y0 = map(int, low-lo)
        block = np.s_[y0:y0+h, x0:x0+w]
        pos, box = supports(triangle, q, low, high)
        positive[block] |= pos
        closed[block] |= box
    return positive, closed, tuple(map(int, lo)), work


def tap_coordinates(mask, origin):
    y, x = np.nonzero(mask)
    return x+origin[0], y+origin[1]


def membership(x, y, mask, origin):
    ix, iy = x-origin[0], y-origin[1]
    h, w = mask.shape
    in_box = (ix >= 0) & (ix < w) & (iy >= 0) & (iy < h)
    present = np.zeros(len(x), dtype=bool)
    present[in_box] = mask[iy[in_box], ix[in_box]]
    return int(np.count_nonzero(~present))


def in_page(x, y, size):
    return (x >= 0) & (x < size[0]) & (y >= 0) & (y < size[1])


def destination_stats(x, y, translation, src_size, dst_size, mapping, src_pixels, dst_pixels):
    ox, oy = translation
    dx, dy = x+ox, y+oy
    valid_src = in_page(x, y, src_size)
    valid_dst = in_page(dx, dy, dst_size)
    valid = valid_src & valid_dst
    x, y, dx, dy = x[valid], y[valid], dx[valid], dy[valid]
    errors = {
        "source_out_of_bounds": int(np.count_nonzero(~valid_src)),
        "destination_out_of_bounds": int(np.count_nonzero(~valid_dst)),
    }
    if mapping is not None:
        previous = mapping[dy, dx]
        source_ids = y * src_size[0] + x
        repeated = previous >= 0
        errors["cross_patch_writes"] = int(np.count_nonzero(repeated))
        errors["different_source_writes"] = int(np.count_nonzero(repeated & (previous != source_ids)))
        mapping[dy, dx] = source_ids
    if src_pixels is not None:
        differences = src_pixels[y, x] != dst_pixels[dy, dx]
        if differences.ndim == 2:
            differences = np.any(differences, axis=1)
        errors["pixel_mismatches"] = int(np.count_nonzero(differences))
    return errors


def validate_images(source, destination, src_size, dst_size):
    if source is None and destination is None:
        return
    if source is None or destination is None:
        raise Refusal("expected_matching_integer_pixel_channels")
    source = _image_array(source, src_size, "source_pixels")
    destination = _image_array(destination, dst_size, "destination_pixels")
    if source.shape[2:] != destination.shape[2:] or source.dtype != destination.dtype:
        raise Refusal("expected_matching_integer_pixel_channels")


def _analyze(
    source_triangles, destination_triangles, patch_ids, translations, source_size, destination_size,
    *, subtexel_divisions, copy_masks=None, source_pixels=None, destination_pixels=None,
    test_budget=DEFAULT_TEST_BUDGET, atlas_budget=DEFAULT_ATLAS_BUDGET,
):
    """Check supplied part translations. Mask values are (origin_xy, bool HxW)."""
    for label, value in (("test_budget", test_budget), ("atlas_budget", atlas_budget)):
        if isinstance(value, bool) or not isinstance(value, (int, np.integer)) or value < 1:
            raise Refusal("expected_positive_integer_budget", field=label)
    src_size = integer_pair(source_size, "source_size", minimum=1)
    dst_size = integer_pair(destination_size, "destination_size", minimum=1)
    if max(src_size[0]*src_size[1], dst_size[0]*dst_size[1]) > min(atlas_budget, np.iinfo(np.int32).max):
        raise Refusal("atlas_budget_exceeded")
    q = subtexel_divisions
    if isinstance(q, bool) or not isinstance(q, (int, np.integer)):
        raise Refusal("expected_even_binary_lattice")
    q = int(q)
    if q < 2 or q & (q-1):
        raise Refusal("expected_even_binary_lattice")
    max_dimension = max(*src_size, *dst_size)
    if 8*((max_dimension+2)*q)**2 >= 2**63:
        raise Refusal("integer_sat_capacity_exceeded")
    source = lattice(source_triangles, src_size, q, "source_triangles")
    destination = lattice(destination_triangles, dst_size, q, "destination_triangles")
    if source.shape != destination.shape:
        raise Refusal("unpaired_correspondence")
    ids = np.asarray(patch_ids)
    if ids.shape != (len(source),) or ids.dtype.kind not in "iu":
        raise Refusal("expected_one_integer_patch_per_face")
    unique_ids = set(map(int, np.unique(ids)))
    def matching_patch_keys(mapping):
        return (isinstance(mapping, Mapping) and set(mapping) == unique_ids
                and all(isinstance(key, (int, np.integer)) and not isinstance(key, bool) for key in mapping))

    if not matching_patch_keys(translations) or (copy_masks is not None and not matching_patch_keys(copy_masks)):
        raise Refusal("patch_lists_disagree")
    shifts = {
        int(key): integer_pair(value, "translations", minimum=-max_dimension, maximum=max_dimension)
        for key, value in translations.items()
    }
    delta = destination-source
    for patch, shift in sorted(shifts.items()):
        member = np.flatnonzero(ids == patch)
        expected = np.asarray(shift, np.int64)*q
        disagreement = np.argwhere(delta[member] != expected)
        if len(disagreement):
            index, corner, axis = map(int, disagreement[0])
            face = int(member[index])
            actual = delta[face, corner].astype(float)/q
            raise Refusal("not_exact_integer_translation", patch=patch, face=face, corner=corner,
                          axis=axis, expected_texels=shift, actual_texels=actual.tolist())
    ab, ac = source[:, 1]-source[:, 0], source[:, 2]-source[:, 0]
    degenerate = np.flatnonzero(ab[:, 0]*ac[:, 1] == ab[:, 1]*ac[:, 0])
    if len(degenerate):
        raise Refusal("zero_area_triangle", face=int(degenerate[0]))
    if source_pixels is not None or destination_pixels is not None:
        source_pixels = None if source_pixels is None else np.asarray(source_pixels)
        destination_pixels = None if destination_pixels is None else np.asarray(destination_pixels)
    validate_images(source_pixels, destination_pixels, src_size, dst_size)

    support_mapping = np.full(dst_size[::-1], -1, dtype=np.int32)
    copy_mapping = np.full(dst_size[::-1], -1, dtype=np.int32)
    rows = []
    work_used = 0
    for patch, shift in sorted(shifts.items()):
        faces = source[ids == patch]
        positive, closed, origin, work = union_support(faces, q, test_budget-work_used)
        work_used += work
        if copy_masks is None:
            copy_origin, copy = origin, positive
        else:
            mask_entry = copy_masks[patch]
            if not isinstance(mask_entry, (list, tuple)) or len(mask_entry) != 2:
                raise Refusal("expected_copy_origin_and_mask", patch=patch)
            mask_origin, copy = mask_entry
            copy_origin = integer_pair(mask_origin, "copy_origin", minimum=-max_dimension, maximum=max_dimension)
            if (not isinstance(copy, np.ndarray) or copy.ndim != 2 or copy.dtype.kind != "b"
                    or not copy.size or copy.size > atlas_budget):
                raise Refusal("expected_bounded_boolean_copy_mask", patch=patch)
            copy = copy.copy()  # Audit and write plan reference the same mask snapshot.
        px, py = tap_coordinates(positive, origin)
        cx, cy = tap_coordinates(closed, origin)
        wx, wy = tap_coordinates(copy, copy_origin)
        common = (shift, src_size, dst_size)
        rows.append({
            "patch": patch, "triangles": int(len(faces)),
            "positive_taps": len(px), "closed_taps": len(cx), "copy_taps": len(wx),
            "closed_only_taps": len(cx)-len(px),
            "positive_missing_from_copy": membership(px, py, copy, copy_origin),
            "closed_missing_from_copy": membership(cx, cy, copy, copy_origin),
            "positive": destination_stats(px, py, *common, support_mapping, source_pixels, destination_pixels),
            "copy": destination_stats(wx, wy, *common, copy_mapping, source_pixels, destination_pixels),
            "closed": destination_stats(cx, cy, *common, None, source_pixels, destination_pixels),
        })

    counts = {key: sum(row[key] for row in rows) for key in (
        "triangles", "positive_taps", "closed_taps", "closed_only_taps", "copy_taps",
        "positive_missing_from_copy", "closed_missing_from_copy",
    )}
    for scope in ("positive", "copy", "closed"):
        counts[scope] = {key: sum(row[scope][key] for row in rows) for key in rows[0][scope]}
    errors = []
    if counts["positive_missing_from_copy"]:
        errors.append({"reason": "positive_support_missing_from_copy"})
    for scope in ("positive", "copy"):
        errors.extend(
            {"reason": f"{scope}_{key}", "count": count}
            for key, count in counts[scope].items() if count
        )
    report = {
        "schema": SCHEMA, "status": "refused" if errors else "pass", "errors": errors,
        "source_size": src_size, "destination_size": dst_size, "subtexel_divisions": q,
        "copy_mask_mode": "derived_positive" if copy_masks is None else "explicit",
        "pixel_values_compared": source_pixels is not None,
        "sat_bounding_box_taps_tested": work_used, "totals": counts, "patches": rows,
    }
    return report, copy_mapping


def refusal_report(exc):
    return deepcopy(exc.report)


def audit_footprint(
    source_triangles, destination_triangles, patch_ids, translations, source_size, destination_size,
    *, subtexel_divisions, copy_masks=None, source_pixels=None, destination_pixels=None,
    test_budget=DEFAULT_TEST_BUDGET, atlas_budget=DEFAULT_ATLAS_BUDGET,
):
    """Inspect patch translations, optional explicit copy masks and image values."""
    report, _ = _analyze(
        source_triangles, destination_triangles, patch_ids, translations, source_size, destination_size,
        subtexel_divisions=subtexel_divisions, copy_masks=copy_masks,
        source_pixels=source_pixels, destination_pixels=destination_pixels,
        test_budget=test_budget, atlas_budget=atlas_budget,
    )
    return report


@dataclass(frozen=True, eq=False)
class _TransferPlan:
    source_size: tuple[int, int]
    destination_size: tuple[int, int]
    _copy_mapping: np.ndarray
    _report: dict

    @property
    def report(self):
        return deepcopy(self._report)


def plan_transfer(
    source_triangles, destination_triangles, patch_ids, translations, source_size, destination_size,
    *, subtexel_divisions, copy_masks=None,
    test_budget=DEFAULT_TEST_BUDGET, atlas_budget=DEFAULT_ATLAS_BUDGET,
):
    """Build a reusable plan from explicit paired triangles and integer translations.

    Array origins are bottom-left. Mask entries are (origin_xy, bool HxW).
    Positive support and every planned write must be in bounds and conflict free.
    """
    report, mapping = _analyze(
        source_triangles, destination_triangles, patch_ids, translations, source_size, destination_size,
        subtexel_divisions=subtexel_divisions, copy_masks=copy_masks,
        test_budget=test_budget, atlas_budget=atlas_budget,
    )
    if report["status"] != "pass":
        first = report["errors"][0]
        raise Refusal(first["reason"], report=report,
                      **{key: value for key, value in first.items() if key != "reason"})
    # A bytes-backed NumPy view cannot be made writeable, even using setflags.
    sealed = np.frombuffer(mapping.tobytes(), dtype=mapping.dtype).reshape(mapping.shape)
    return _TransferPlan(report["source_size"], report["destination_size"], sealed, report)


def _image_array(pixels, size, field):
    image = np.asarray(pixels)
    if (image.ndim not in (2, 3) or image.shape[:2] != size[::-1]
            or image.dtype.kind not in "iu" or (image.ndim == 3 and not image.shape[2])):
        raise Refusal("expected_integer_image_with_matching_size", field=field)
    return image


def apply_transfer(plan, source_pixels, *, destination_pixels=None):
    """Apply a validated plan to an integer image, returning a fresh output.

    Pixel arrays are HxWxC, or HxW for grayscale. Unwritten texels are zero unless
    an explicit destination base was supplied. Input arrays remain untouched.
    """
    if not isinstance(plan, _TransferPlan):
        raise Refusal("expected_validated_transfer_plan")
    source = _image_array(source_pixels, plan.source_size, "source_pixels")
    shape = plan.destination_size[::-1] + source.shape[2:]
    if destination_pixels is None:
        output = np.zeros(shape, dtype=source.dtype)
    else:
        base = _image_array(destination_pixels, plan.destination_size, "destination_pixels")
        if base.shape[2:] != source.shape[2:] or base.dtype != source.dtype:
            raise Refusal("expected_matching_integer_pixel_channels")
        output = np.array(base, copy=True, order="C")

    tail = source.shape[2:]
    source_flat = source.reshape((-1,) + tail)
    output_flat = output.reshape((-1,) + tail)
    copy_mapping = plan._copy_mapping.ravel()
    copied = 0
    chunk_size = 1 << 18
    for start in range(0, len(copy_mapping), chunk_size):
        stop = start + chunk_size
        ids = copy_mapping[start:stop]
        selected = ids >= 0
        chunk = output_flat[start:stop]
        values = source_flat[ids[selected]]
        chunk[selected] = values
        if not np.array_equal(chunk[selected], values):
            raise Refusal("copied_values_changed")
        copied += len(values)
    report = plan.report
    if copied != report["totals"]["copy_taps"]:
        raise Refusal("copy_mapping_count_changed")
    report["write"] = {
        "copied_destination_texels": copied,
        "unwritten_destination_texels": len(copy_mapping) - copied,
        "unwritten_pixel_policy": "zero" if destination_pixels is None else "preserve_destination_base",
        "pixel_shape": list(output.shape),
        "pixel_dtype": output.dtype.str,
        "copied_values_exact": True,
    }
    return output, report


def transfer_pixels(
    source_pixels, source_triangles, destination_triangles, patch_ids, translations, source_size,
    destination_size, *, subtexel_divisions, copy_masks=None, destination_pixels=None,
    test_budget=DEFAULT_TEST_BUDGET, atlas_budget=DEFAULT_ATLAS_BUDGET,
):
    """Plan and apply an exact atlas copy. A refusal returns no candidate array."""
    plan = plan_transfer(
        source_triangles, destination_triangles, patch_ids, translations, source_size, destination_size,
        subtexel_divisions=subtexel_divisions, copy_masks=copy_masks,
        test_budget=test_budget, atlas_budget=atlas_budget,
    )
    return apply_transfer(plan, source_pixels, destination_pixels=destination_pixels)
