"""Absolute selection dimensions for the Blender 5.3 mesh control cage."""
import math

from mathutils import Vector

from model_contract import ABSOLUTE_SCALE_AXES
from protocol import Failure


def bounds(points):
    columns = tuple(zip(*points))
    return [min(axis) for axis in columns], [max(axis) for axis in columns]


def scale_selection(item, vertices, op, scene):
    if len(vertices) < 2:
        raise Failure('INVALID_REQUEST', 'Absolute scale requires at least two vertices')

    world = op['space'] == 'world'
    matrix = item.matrix_world
    basis = matrix.to_3x3()
    inverse = None
    if world or op['pivot'] == 'cursor':
        try:
            inverse = basis.inverted()
        except ValueError as exc:
            raise Failure('INVALID_REQUEST', 'World or cursor absolute scale requires an invertible world transform') from exc

    # A translation cancels in both the bounds and the scale about a pivot.
    # Keeping the origin here preserves small dimensions far from world zero.
    def measure(co):
        return basis @ co if world else co

    points = [measure(vertex.co.copy()) for vertex in vertices]
    if any(not math.isfinite(value) for point in points for value in point):
        raise Failure('INVALID_REQUEST', 'Absolute scale source coordinates must be finite')
    lower, upper = bounds(points)
    before = [high - low for low, high in zip(lower, upper)]
    units = op.get('units', 'blender_units')
    unit_to_meters = float(scene.unit_settings.scale_length)
    if not math.isfinite(unit_to_meters) or unit_to_meters <= 0:
        raise Failure('INVALID_REQUEST', 'Scene unit scale must be finite and positive')
    divisor = unit_to_meters if units == 'meters' else 1.0
    target = before.copy()
    factors = [1.0, 1.0, 1.0]
    for index, axis in enumerate(ABSOLUTE_SCALE_AXES):
        if axis not in op['dimensions']:
            continue
        target[index] = op['dimensions'][axis] / divisor
        if before[index] == 0 and target[index] != 0:
            raise Failure('INVALID_REQUEST', f'Cannot give a flat selection nonzero {axis} dimensions')
        if before[index] != 0:
            factors[index] = target[index] / before[index]

    if op['pivot'] == 'cursor':
        cursor = scene.cursor.location - matrix.translation
        pivot = cursor if world else inverse @ cursor
    elif op['pivot'] == 'bounds_center':
        pivot = [(low + high) * 0.5 for low, high in zip(lower, upper)]
    else:
        pivot = [math.fsum(column) / len(points) for column in zip(*points)]

    if factors != [1.0, 1.0, 1.0]:
        def scaled(point):
            new = Vector(tuple(
                value if factor == 1 else center + (value - center) * factor
                for value, center, factor in zip(point, pivot, factors)
            ))
            return inverse @ new if world else new

        coordinates = [scaled(point) for point in points]
        if any(not math.isfinite(value) for co in coordinates for value in co):
            raise Failure('VALIDATION_FAILED', 'Absolute scale exceeds mesh coordinate precision')
        after_lower, after_upper = bounds([measure(co) for co in coordinates])
        after = [high - low for low, high in zip(after_lower, after_upper)]
        for axis, expected, actual in zip(ABSOLUTE_SCALE_AXES, target, after):
            if (expected > 0 and actual == 0) or not math.isclose(
                actual, expected, rel_tol=2e-6, abs_tol=1e-6
            ):
                raise Failure('VALIDATION_FAILED', f'Absolute scale {axis} dimension cannot be represented at mesh precision')
        for vertex, co in zip(vertices, coordinates):
            vertex.co = co
        item.data.update()
    else:
        after = before

    return {
        'op': op['op'],
        'object': item.name,
        'space': op['space'],
        'pivot': op['pivot'],
        'units': units,
        'blender_unit_to_meters': unit_to_meters,
        'vertex_count': len(vertices),
        'before_dimensions_bu': before,
        'target_dimensions_bu': target,
        'after_dimensions_bu': after,
    }
