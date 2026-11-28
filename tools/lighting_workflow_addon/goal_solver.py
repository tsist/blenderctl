"""Compile shot measurements into a bounded light-power adjustment."""

import math

import numpy as np

from . import core


EPS = 1e-9


class Constraints:
    def __init__(self, request):
        self.regions = []
        self.rows = []
        for item in request['exposure']:
            self._exposure(item['subject'], item['stops'], item['tolerance_stops'], 'subject')
            for protected in item['protect']:
                self._exposure(protected['object'], 0, protected['tolerance_stops'], 'protect')
        for item in request['key_fill']:
            self.rows.append({'kind': 'key_fill', 'region_id': self._region(item['region']),
                              'target': item['ratio'], 'tolerance': item['tolerance_stops'],
                              'key': tuple(sorted(item['key'])), 'fill': tuple(sorted(item['fill']))})
        for item in request['brightness']:
            self.rows.append({'kind': 'brightness', 'region_id': self._region({'roi': item['roi']}),
                              'min': item['min'], 'max': item['max']})
        self.groups = sorted({row[key] for row in self.rows if row['kind'] == 'key_fill' for key in ('key', 'fill')})

    def _exposure(self, name, stops, tolerance, kind):
        self.rows.append({'kind': kind, 'region_id': self._region({'object': name}),
                          'target': stops, 'tolerance': tolerance})

    def _region(self, region):
        if region not in self.regions:
            self.regions.append(region)
        return self.regions.index(region)

    def validate_source(self, before):
        for row in self.rows:
            index = row['region_id']
            if row['kind'] in ('subject', 'protect') and before[index] <= EPS:
                core._fail('UNMEASURABLE', 'Relative exposure needs positive baseline luminance: ' +
                           self.regions[index]['object'])

    @staticmethod
    def limits(row, inset=0):
        if row['kind'] == 'brightness':
            margin = (row['max'] - row['min']) * inset
            return row['min'] + margin, row['max'] - margin
        tolerance = row['tolerance'] * (1 - 2 * inset)
        if row['kind'] == 'key_fill':
            return row['target'] / 2**tolerance, row['target'] * 2**tolerance
        return row['target'] - tolerance, row['target'] + tolerance

    def report(self, before, combined, groups):
        results = []
        for row in self.rows:
            index = row['region_id']
            kind = row['kind']
            result = {'kind': kind, 'region': self.regions[index]}
            lower, upper = self.limits(row)
            if kind == 'key_fill':
                key, fill = groups[row['key']][index], groups[row['fill']][index]
                actual = float(key / fill) if min(key, fill) > EPS else None
                target = row['target']
                result.update(key_lights=list(row['key']), fill_lights=list(row['fill']),
                              key_luminance=float(key), fill_luminance=float(fill), unit='ratio',
                              deviation_stops=math.log2(actual / target) if actual is not None else None)
            elif kind == 'brightness':
                actual = float(combined[index])
                result.update(source=float(before[index]), unit='linear_luminance')
            else:
                actual = math.log2(combined[index] / before[index]) if combined[index] > EPS else None
                result.update(source_luminance=float(before[index]), measured_luminance=float(combined[index]),
                              unit='stops', deviation_stops=actual - row['target'] if actual is not None else None)
            delta = None if actual is None else actual - min(upper, max(lower, actual))
            result.update(actual=actual, minimum=lower, maximum=upper,
                          deviation_from_range=delta, passed=delta is not None and abs(delta) <= 1e-8)
            if 'target' in row:
                result['target'] = row['target']
            results.append(result)
        return results

    def solve(self, before, dark, response, names, current, minimum, maximum):
        span = maximum - minimum
        scaled = response * span
        offset = dark + response @ minimum
        group_responses = {group: response * np.isin(names, group) for group in self.groups}

        def build(inset):
            size = len(names)
            directions = np.identity(size)
            matrix = [*directions, *-directions]
            limits = [*[1.] * size, *[0.] * size]
            for row in self.rows:
                index = row['region_id']
                lower, upper = self.limits(row, inset)
                if row['kind'] == 'key_fill':
                    key = group_responses[row['key']][index]
                    fill = group_responses[row['fill']][index]
                    for constraint, limit in ((key - upper * fill, 0.), (lower * fill - key, 0.),
                                              (-key, -2 * EPS), (-fill, -2 * EPS)):
                        matrix.append(constraint * span)
                        limits.append(limit - constraint @ minimum)
                else:
                    if row['kind'] in ('subject', 'protect'):
                        lower = before[index] * 2**lower
                        upper = before[index] * 2**upper
                    matrix.extend((scaled[index], -scaled[index]))
                    limits.extend((upper - offset[index], offset[index] - lower))
            return np.asarray(matrix), np.asarray(limits)

        # First leave slack for finite-sample rendering. Retry the full requested
        # intervals when protection bounds or light limits leave no slack.
        fraction = np.divide(current - minimum, span, out=np.zeros_like(span), where=span > 0)
        for inset in (.20, 0):
            scale, consistent = _project(*build(inset), fraction)
            energy = minimum + span * scale
            if consistent:
                break
        predicted = self.report(before, dark + response @ energy,
                                {group: values @ energy for group, values in group_responses.items()})
        return energy, predicted


def _project(matrix, limits, start):
    """Dykstra projection onto linear half-spaces, anchored at current energies."""
    length = np.linalg.norm(matrix, axis=1)
    nonzero = length > 1e-15
    if (limits[~nonzero] < -1e-12).any():
        return np.clip(start, 0, 1), False
    rows = matrix[nonzero] / length[nonzero, None]
    bounds = limits[nonzero] / length[nonzero]
    corrections = np.zeros(len(rows))
    point = np.clip(start, 0, 1)
    best = point.copy()
    best_error = max(0., float((rows @ best - bounds).max()))
    for _ in range(1500):
        if best_error <= 1e-10:
            return best, True
        for i, (normal, bound) in enumerate(zip(rows, bounds)):
            trial = point + corrections[i] * normal
            corrections[i] = max(0., float(normal @ trial - bound))
            point = trial - corrections[i] * normal
        bounded = np.clip(point, 0, 1)
        error = max(0., float((rows @ bounded - bounds).max()))
        if error < best_error:
            best_error, best = error, bounded
    return best, best_error <= 1e-10
