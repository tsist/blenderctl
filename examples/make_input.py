"""Bind an empty create_fixture.py source to an eight-frame SDR template example."""
import argparse
import hashlib
import json
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument('--source', type=Path, required=True)
parser.add_argument('--output', type=Path, required=True)
args = parser.parse_args()
source, target = args.source.resolve(), args.output.resolve()
value = {'context': {'file': str(source), 'expected_sha256': hashlib.sha256(source.read_bytes()).hexdigest(), 'resources': []},
         'parameters': {'workflow_id': 'PublicVideoExample', 'scene': 'PublicVideoExample', 'fps': [24, 1],
                        'width': 160, 'height': 90, 'frame_start': 1,
                        'shots': [{'id': 'red', 'kind': 'COLOR', 'duration': 4, 'color': [1, 0, 0, 1]},
                                  {'id': 'blue', 'kind': 'COLOR', 'duration': 4, 'color': [0, 0, 1, 1]}],
                        'output': {'audio': False, 'color_mode': 'SDR', 'format': 'PNG', 'shots': True,
                                   'shot_ids': ['red', 'blue'], 'subtitles': False, 'max_pixel_frames': 230400}}}
target.parent.mkdir(parents=True, exist_ok=True)
with target.open('x', encoding='utf-8') as stream:
    json.dump(value, stream, indent=2)
print(json.dumps({'file': str(target), 'expected_sha256': hashlib.sha256(target.read_bytes()).hexdigest()}))
