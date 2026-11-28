"""Goal-based adjustment for a saved scene on the calling worker."""

from contextlib import ExitStack
from copy import deepcopy
import json
from time import perf_counter
from uuid import uuid4

import numpy as np

from . import advanced, core
from .goal_contract import LIMIT, normalize_request
from .goal_measure import measurement
from .goal_solver import Constraints


def _energy(obj):
    return core._light_values(obj)['energy']


def _set_energy(obj, energy):
    core._set_light_values(obj, {'energy': float(energy)})
    if not core._close(_energy(obj), energy):
        core._fail('VALIDATION_FAILED', 'Blender clamped light power: ' + obj.name)


class LightEdit:
    def __init__(self, obj, spec, managed):
        self.obj = obj
        self.data = obj.data
        self.source = _energy(obj)
        self.minimum = spec.get('min_energy', 0.)
        default = 4. if self.data.type == 'SUN' else 250.
        self.maximum = spec.get('max_energy', min(LIMIT, max(default, self.source * 8)))
        if not self.minimum <= self.maximum or self.source < 0:
            core._fail('INVALID_REQUEST', 'Invalid nonnegative power limits: ' + obj.name)
        if self.maximum > self.data.bl_rna.properties['energy'].hard_max:
            core._fail('INVALID_REQUEST', 'Power exceeds native range: ' + obj.name)
        self.managed = managed
        self.copy = None
        self.shared = self.data.users - int(self.data.use_fake_user) > 1 or bool(self.data.library)

    def prepare(self, undo):
        undo.callback(self.undo)
        if self.shared:
            self.copy = self.data.copy()
            self.copy.use_fake_user = False
            self.obj.data = self.copy

    def discard_copy(self):
        self.obj.data = self.data
        core._bpy().data.lights.remove(self.copy)
        self.copy = None

    def undo(self):
        if self.copy is not None:
            self.discard_copy()
        elif not self.shared:
            _set_energy(self.obj, self.source)

    def read(self):
        return {'object': self.obj.name, 'source_energy': self.source, 'final_energy': _energy(self.obj),
                'source_data': self.data.name, 'final_data': self.obj.data.name, 'copied_data': self.copy is not None}


def _preflight(request, scene, layer, states):
    bpy = core._bpy()
    managed = {row['object']: (rig, id_) for rig, state in states.items() for id_, row in state['lights'].items()}
    instance_collections = {obj.instance_collection for obj in bpy.data.objects if obj.instance_collection}
    edits = {}
    checked = set()
    for spec in request['lights']:
        name = spec['name']
        obj = layer.objects.get(name)
        if obj is None or obj.type != 'LIGHT':
            core._fail('NOT_FOUND', 'Named light is absent from the view layer: ' + name)
        core._exclusive(obj, scene)
        if obj.hide_render or obj.data.override_library or obj.data.animation_data:
            core._fail('UNSUPPORTED', 'Light needs visible, static native power: ' + name)
        if any(name in collection.all_objects for collection in instance_collections):
            core._fail('CONFLICT', 'Named light is used by a collection instance: ' + name)
        rig_id, light_id = managed.get(name, (None, None))
        if rig_id is not None:
            rig = states[rig_id]
            if rig_id not in checked:
                core._check_structure(scene, rig)
                checked.add(rig_id)
            row = rig['lights'][light_id]
            core._conflict(_energy(obj), row['baseline']['energy'], 'Manual power edit: ' + name, 'error')
        edits[name] = LightEdit(obj, spec, managed.get(name))
    for spec in request['add_lights']:
        if len((spec['name'] + '.Data').encode()) > 63:
            core._fail('INVALID_REQUEST', 'New fill name exceeds Blender ID length: ' + spec['name'])
        core._fresh(bpy.data.objects, spec['name'])
        core._fresh(bpy.data.lights, spec['name'] + '.Data')
        if spec.get('min_energy', 0.) > spec['max_energy']:
            core._fail('INVALID_REQUEST', 'Invalid power limits: ' + spec['name'])
    for link in request['light_links']:
        advanced.validate_links(scene, {'receivers': link['receivers'], 'receivers_mode': link['mode']})
        edit = edits.get(link['light'])
        if edit and edit.managed:
            rig_id, light_id = edit.managed
            baseline = states[rig_id]['lights'][light_id]['baseline']
            current = advanced.link_values(edit.obj)
            if 'receivers_members' in current:
                core._fail('UNSUPPORTED', 'Goal link edits cannot replace explicit object/collection members: ' + edit.obj.name)
            for field, default in (('receivers', []), ('receivers_mode', 'INCLUDE')):
                core._conflict(current[field], baseline.get(field, default),
                               'Manual receiver edit: ' + edit.obj.name, 'error')
    return edits


def _add_fill(scene, spec, undo):
    from mathutils import Vector

    bpy = core._bpy()
    name = spec['name']
    data = bpy.data.lights.new(name + '.Data', 'AREA')
    undo.callback(bpy.data.lights.remove, data)
    data.shape, data.size, data.energy = 'DISK', spec['size'], 0
    obj = bpy.data.objects.new(name, data)
    undo.callback(bpy.data.objects.remove, obj, do_unlink=True)
    scene.collection.objects.link(obj)
    core._transform(obj, Vector(spec['location']), Vector(spec['aim']))
    return LightEdit(obj, spec, None)


def _set_receiver(scene, edit, spec, undo):
    binding = edit.obj.light_linking
    previous = binding.receiver_collection
    owner = edit.managed[0] if edit.managed else None
    collection = None
    if spec['receivers']:
        collection = advanced.new_link_collection(scene, 'LW.Goal.Link.' + uuid4().hex[:16],
                                                  spec['receivers'], spec['mode'], owner)
        undo.callback(core._bpy().data.collections.remove, collection)
    undo.callback(setattr, binding, 'receiver_collection', previous)
    binding.receiver_collection = collection
    if binding.receiver_collection != collection:
        core._fail('VALIDATION_FAILED', 'Receiver collection assignment did not persist: ' + edit.obj.name)
    if collection is not None:
        advanced._check_consumers(collection, edit.obj)
    return previous, collection


def _identity(item):
    return item.name_full if item else None


def _snapshot(scene):
    bpy = core._bpy()
    result = core.snapshot_protection(scene, include_managed=True)
    result['goal_scene'] = {'world': _identity(scene.world), 'exposure': float(scene.view_settings.exposure),
                            'frame': [scene.frame_current, scene.frame_subframe],
                            'camera': _identity(scene.camera), 'render_path': scene.render.filepath}
    result['goal_light_links'] = {obj.name: {key: _identity(getattr(obj.light_linking, prop))
                                           for key, prop in advanced.LINK_PROPERTIES.items()}
                                  for obj in bpy.data.objects if obj.type == 'LIGHT'}
    result['goal_link_members'] = {c.name: sorted((obj.name, member.light_linking.link_state)
                                                for obj, member in zip(c.objects, c.collection_objects, strict=True))
                                   for c in bpy.data.collections}
    return result


def _record_key(item):
    bpy = core._bpy()
    return json.dumps({'type': item.bl_rna.identifier, 'name': item.name,
                       'library': bpy.path.abspath(item.library.filepath) if item.library else None},
                      sort_keys=True, ensure_ascii=False)


def _compare(scene, before, edits, new, links, retired=(), retained=()):
    before = deepcopy(before)
    after = _snapshot(scene)
    after_record = after['records']
    before_record = before['records']
    removed_collections = set(collection.name for collection in retired)
    added_collections = set(collection.name for _, collection in links.values() if collection)
    for collection in retired:
        before_record.pop(_record_key(collection))
        after_record.pop(_record_key(collection))
        before['goal_link_members'].pop(collection.name)
        after['goal_link_members'].pop(collection.name)
    for collection in retained:
        key = _record_key(collection)
        before_record[key]['fake_user'] = after_record[key]['fake_user']
    for name, (_, collection) in links.items():
        if name not in new:
            before['goal_light_links'][name].pop('receivers')
            after['goal_light_links'][name].pop('receivers')
        if collection:
            after_record.pop(_record_key(collection))
            after['goal_link_members'].pop(collection.name)
    for name, edit in edits.items():
        data_key = _record_key(edit.obj.data)
        if name in new:
            after_record.pop(_record_key(edit.obj))
            after_record.pop(data_key)
            after['goal_light_links'].pop(name)
        elif edit.copy is not None:
            old_data = deepcopy(before_record[_record_key(edit.data)])
            new_data = after_record.pop(data_key)
            for record in (old_data, new_data):
                record.pop('name')
                record.pop('fake_user')
                record['settings'].pop('use_fake_user', None)
                record['settings'].pop('name', None)
                record['settings'].pop('energy')
            if not core._close(old_data, new_data):
                delta = core.verify_protection(old_data, new_data)['difference']
                core._fail('VALIDATION_FAILED', 'Copied light lost a protected data setting: ' + name + ' at ' + delta)
            obj_key = _record_key(edit.obj)
            before_record[obj_key]['object'].pop('data')
            after_record[obj_key]['object'].pop('data')
        elif not edit.shared:
            before_record[data_key]['settings'].pop('energy')
            after_record[data_key]['settings'].pop('energy')
    for records, ignored in ((before_record, removed_collections), (after_record, removed_collections | added_collections)):
        for row in records.values():
            if 'collections' in row:
                row['collections'] = [name for name in row['collections'] if name not in ignored]
            if records is after_record and 'members' in row:
                row['members'] = [name for name in row['members'] if name not in new]
    after['scene_memberships'][scene.name]['objects'] = [name for name in after['scene_memberships'][scene.name]['objects']
                                                           if name not in new]
    proof = core.verify_protection(before, after)
    if proof['status'] != 'pass':
        core._fail('VALIDATION_FAILED', 'Protected scene changed at ' + proof['difference'])
    return proof


def _update_managed(scene, states, edits, links, undo):
    updated = deepcopy(states)
    for name, edit in edits.items():
        if edit.managed is None:
            continue
        rig_id, light_id = edit.managed
        rig = updated[rig_id]
        row = rig['lights'][light_id]
        values = {'energy': _energy(edit.obj)}
        if name in links:
            _, collection = links[name]
            observed = advanced.link_values(edit.obj)
            fields = {key: observed[key] for key in ('receivers', 'receivers_mode')}
            values.update(fields)
            rig.setdefault('links', {}).setdefault(light_id, {})['receivers'] = {
                'collection': _identity(collection), 'refs': fields['receivers'], 'mode': fields['receivers_mode'],
                'witness': advanced._link_witness(collection) if collection else None}
        row['baseline'].update(values)
        row['recipe'].update(values)
        for spec in rig['manifest']['lights']:
            if spec['id'] == light_id:
                spec.update(values)
    if updated != states:
        undo.callback(core._save_states, scene, states)
        core._save_states(scene, updated)


def _run_measurement(constraints, sample, stage, edits):
    render_count = 0

    def read():
        nonlocal render_count
        render_count += 1
        result = np.asarray(sample(), dtype=float)
        if result.shape != (len(constraints.regions),) or not np.isfinite(result).all():
            core._fail('VALIDATION_FAILED', 'Measurement must return one finite value per region')
        return result

    before = read()
    constraints.validate_source(before)
    stage()
    names = list(edits)
    current = np.array([edits[name].source for name in names])
    minimum = np.array([edits[name].minimum for name in names])
    maximum = np.array([edits[name].maximum for name in names])
    objects = [edits[name].obj for name in names]

    def write(energies):
        for obj, energy in zip(objects, energies, strict=True):
            _set_energy(obj, energy)

    write(np.zeros(len(objects)))
    dark = read()
    responses = np.zeros((len(before), len(objects)))
    for i, (obj, energy) in enumerate(zip(objects, maximum, strict=True)):
        if energy > 0:
            _set_energy(obj, energy)
            responses[:, i] = (read() - dark) / energy
            _set_energy(obj, 0)
    predicted_energy, predicted = constraints.solve(before, dark, responses, names, current, minimum, maximum)
    write(predicted_energy)
    combined = read()
    groups = {}
    for group in constraints.groups:
        write(np.where(np.isin(names, group), predicted_energy, 0.))
        groups[group] = read() - dark
    write(predicted_energy)
    measured = constraints.report(before, combined, groups)
    return {'render_count': render_count, 'predicted': predicted, 'constraints': measured,
            'lights': [edit.read() for edit in edits.values()]}


def _run_group_measurement(scene, layer, constraints, sampling, edits, stage):
    started = perf_counter()
    stage()
    names = list(edits)
    current = np.array([edit.source for edit in edits.values()])
    minimum = np.array([edit.minimum for edit in edits.values()])
    maximum = np.array([edit.maximum for edit in edits.values()])
    reference = np.where(current > 0, current, maximum)
    objects = [edit.obj for edit in edits.values()]
    for obj, energy in zip(objects, reference, strict=True):
        _set_energy(obj, energy)
    with measurement(scene, layer, constraints.regions, sampling, objects) as (sample, size, device):
        render_started = perf_counter()
        prepare_seconds = render_started - started
        combined, group_values = sample()
        render_seconds = perf_counter() - render_started
    responses = np.asarray(group_values, dtype=float).T
    total = np.asarray(combined, dtype=float)
    if not np.isfinite(responses).all() or not np.isfinite(total).all():
        core._fail('UNMEASURABLE', 'Light Group samples must be finite')
    dark = total - responses.sum(axis=1)
    if (dark < -1e-5 - 1e-5 * np.abs(total)).any():
        core._fail('VALIDATION_FAILED', 'Light Group contribution exceeds the Combined pass')
    responses = np.divide(responses, reference, out=np.zeros_like(responses), where=reference > 0)
    before = dark + responses @ current
    constraints.validate_source(before)
    energies, predicted = constraints.solve(before, dark, responses, names, current, minimum, maximum)
    for obj, energy in zip(objects, energies, strict=True):
        _set_energy(obj, energy)
    actual = np.asarray([_energy(obj) for obj in objects])
    groups = {group: responses @ np.where(np.isin(names, group), actual, 0) for group in constraints.groups}
    values = constraints.report(before, dark + responses @ actual, groups)
    return {'render_count': 1, 'predicted': predicted, 'constraints': values,
            'lights': [edit.read() for edit in edits.values()], 'dimensions': size, 'device': device,
            'prepare_seconds': prepare_seconds, 'render_seconds': render_seconds,
            'solve_seconds': perf_counter() - started,
            'measured_baseline_luminance': before.tolist(), 'inferred_fixed_luminance': dark.tolist(),
            'measurement_method': 'single_linear_light_group_render'}


def tune(request):
    """Tune native powers. Return measured constraints and persist only a passing result."""
    request = normalize_request(request)
    scene = core._scene(request['scene'])
    constraints = Constraints(request)
    with core._context(scene, request['view_layer']) as layer, ExitStack() as undo:
        states = core._states(scene)
        edits = _preflight(request, scene, layer, states)
        new = {spec['name'] for spec in request['add_lights']}
        links = {}
        before = _snapshot(scene)
        source_engine = scene.render.engine

        def stage():
            for edit in edits.values():
                edit.prepare(undo)
            for spec in request['add_lights']:
                edit = _add_fill(scene, spec, undo)
                edits[spec['name']] = edit
            for spec in request['light_links']:
                links[spec['light']] = _set_receiver(scene, edits[spec['light']], spec, undo)

        sampling = request['sampling']
        backend = sampling['device']['backend']
        if backend == 'CPU':
            with measurement(scene, layer, constraints.regions, sampling) as (sample, dimensions):
                result = _run_measurement(constraints, sample, stage, edits)
            result.update(dimensions=dimensions, device={'backend': 'CPU'}, measurement_method='successive_cpu_renders')
        else:
            result = _run_group_measurement(scene, layer, constraints, sampling, edits, stage)
        result.update(scene=scene.name, view_layer=layer.name, frame=scene.frame_current, camera=scene.camera.name,
                      source_engine=source_engine, measurement_engine='CYCLES', measurement_device=backend,
                      measurement_space='scene_linear_raw', sampling=sampling,
                      added_lights=sorted(new),
                      receiver_changes=[{'object': name, 'source_collection': _identity(prior),
                                         'final_collection': _identity(final)} for name, (prior, final) in links.items()])
        passed = all(row['passed'] for row in result['constraints'])
        if not passed or not request['apply']:
            undo.close()
            layer.update()
            result['protection'] = _compare(scene, before, {}, set(), {})
            result['status'] = 'measured' if passed else 'unsatisfied'
            result['applied'] = False
            return result
        for edit in edits.values():
            if edit.copy is not None and _energy(edit.obj) == edit.source:
                edit.discard_copy()
        retired, retained = set(), set()
        for name, (prior, _) in links.items():
            if prior is None:
                continue
            if edits[name].managed:
                retired.add(prior)
            elif not prior.users and prior.library is None:
                undo.callback(setattr, prior, 'use_fake_user', False)
                prior.use_fake_user = True
                retained.add(prior)
        _update_managed(scene, states, edits, links, undo)
        result['protection'] = _compare(scene, before, edits, new, links, retired, retained)
        for collection in retired:
            core._bpy().data.collections.remove(collection)
        undo.pop_all()
        result['status'], result['applied'] = 'applied', True
        result['lights'] = [edit.read() for edit in edits.values()]
        result['retained_previous_link_collections'] = sorted(collection.name for collection in retained)
        return result
