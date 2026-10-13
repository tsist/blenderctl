"""Three starting directions, not universal aesthetic or photometric claims."""
from copy import deepcopy
from .contract import WorkflowError

PRESETS = {
    'natural': [
        {'id': 'key', 'role': 'key', 'type': 'AREA', 'location': [-1.5, -2, 2], 'energy': 500, 'color': [1, .94, .85], 'size': 2},
        {'id': 'fill', 'role': 'fill', 'type': 'AREA', 'location': [2, -1, 1], 'energy': 160, 'color': [.82, .9, 1], 'size': 2},
        {'id': 'rim', 'role': 'rim', 'type': 'AREA', 'location': [1, 1.5, 2], 'energy': 350, 'color': [1, 1, 1], 'shape': 'RECTANGLE', 'size': .8, 'size_y': 2}],
    'dramatic': [
        {'id': 'key', 'role': 'key', 'type': 'AREA', 'location': [-2, -.8, 1.6], 'energy': 600, 'color': [1, .78, .52], 'size': .8},
        {'id': 'fill', 'role': 'fill', 'type': 'AREA', 'location': [2, -1, .8], 'energy': 40, 'color': [.4, .65, 1], 'size': 1.5},
        {'id': 'rim', 'role': 'rim', 'type': 'AREA', 'location': [1, 1.5, 1.5], 'energy': 450, 'color': [.5, .75, 1], 'shape': 'RECTANGLE', 'size': .4, 'size_y': 2}],
    'soft': [
        {'id': 'key', 'role': 'key', 'type': 'AREA', 'location': [-1, -1.5, 2], 'energy': 500, 'color': [1, .96, .9], 'size': 3},
        {'id': 'fill', 'role': 'fill', 'type': 'AREA', 'location': [1.5, -1, 1.5], 'energy': 280, 'color': [.9, .95, 1], 'size': 3},
        {'id': 'rim', 'role': 'rim', 'type': 'AREA', 'location': [0, 1.5, 2], 'energy': 250, 'color': [1, 1, 1], 'size': 2}],
}


def lights(name):
    if name not in PRESETS: raise WorkflowError('INVALID_REQUEST', 'Unknown lighting preset: ' + str(name))
    return deepcopy(PRESETS[name])


def get(name):
    return lights(name)
