"""Host-safe, explicit source/target correspondence for new UV candidates."""
import math
import re
from .contract import fail


def _sha(value):
    return isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value) is not None


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def normalize(v, names):
    rows = v.get('transfer')
    if not isinstance(rows, dict) or set(rows) != set(names):
        fail('transfer must cover the exact target objects')
    if not _number(v.get('transfer_unit_scale')) or v['transfer_unit_scale'] <= 0:
        fail('transfer_unit_scale must bind the positive scene unit scale')
    base = {'source_object', 'source_uv', 'source_binding', 'target_binding', 'mode'}
    for name, row in rows.items():
        if not isinstance(row, dict):
            fail('Transfer entry must be an object: ' + name)
        mode = row.get('mode')
        extra = {'INDEXED': set(), 'EXPLICIT': {'corner_map', 'winding'},
                 'PROJECTED': {'regions', 'max_distance_m', 'normal_degrees', 'uv_tolerance_px'}}
        fields = set(row)
        if mode == 'EXPLICIT' and 'vertex_domain' in row:
            fields = fields - {'vertex_domain'}
        if not isinstance(mode, str) or mode not in extra or fields != base | extra[mode]:
            fail('Transfer mode fields must be exact: ' + name)
        if any(not isinstance(row[k], str) or not row[k] for k in ('source_object', 'source_uv')):
            fail('Transfer source object and UV must be explicit names')
        if row['source_object'] in names:
            fail('Transfer sources and targets must be disjoint; use copy_uv within one object')
        for key, keys in [('source_binding', {'geometry', 'mesh', 'uv_hash'}),
                          ('target_binding', {'geometry', 'mesh', 'protection', 'active_uv'})]:
            b = row[key]
            if not isinstance(b, dict) or set(b) != keys or not _sha(b.get('geometry')) or not isinstance(b.get('mesh'), str) or not b['mesh']:
                fail('Invalid transfer identity: ' + name + '/' + key)
            if not _sha(b['uv_hash' if key == 'source_binding' else 'protection']):
                fail('Invalid transfer content identity')
            if key == 'target_binding' and b['active_uv'] is not None and (not isinstance(b['active_uv'], str) or not b['active_uv']):
                fail('Invalid target active UV identity')
        if mode == 'EXPLICIT':
            if row.get('vertex_domain', 'ALL') not in ('ALL', 'FACE_REFERENCED'):
                fail('Explicit vertex_domain must be ALL or FACE_REFERENCED')
            ids = row['corner_map']
            if not isinstance(ids, list) or not ids or len(ids) > 1000000 or any(type(i) is not int or i < 0 for i in ids) or len(set(ids)) != len(ids):
                fail('corner_map must be a complete unique source-loop mapping')
            if row['winding'] not in ('PRESERVE', 'REVERSE'):
                fail('Explicit correspondence requires PRESERVE or REVERSE winding')
        elif mode == 'PROJECTED':
            for key, low, high in [('max_distance_m', 0, None), ('normal_degrees', 0, 30), ('uv_tolerance_px', 0, 1)]:
                x = row[key]
                if not _number(x) or x < low or high is not None and x > high or key == 'uv_tolerance_px' and x == 0:
                    fail('Invalid projected transfer limit: ' + key)
            regions = row['regions']
            if not isinstance(regions, list) or not regions or len(regions) > 10000:
                fail('Projected transfer requires explicit bounded source/target face regions')
            seen = set()
            for region in regions:
                if not isinstance(region, dict) or set(region) != {'target_faces', 'source_faces'}:
                    fail('Transfer region requires exact target_faces/source_faces')
                for key in ('target_faces', 'source_faces'):
                    ids = region[key]
                    if not isinstance(ids, list) or not ids or len(ids) > 1000000 or any(type(i) is not int or i < 0 for i in ids) or len(set(ids)) != len(ids):
                        fail('Transfer face regions require unique nonnegative indices')
                if seen.intersection(region['target_faces']):
                    fail('Target faces cannot occur in more than one transfer region')
                seen.update(region['target_faces'])
    return v
