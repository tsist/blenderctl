"""Plain SRT to editable native TEXT specs, with explicit rational quantization."""
import re
from fractions import Fraction
from .contract import WorkflowError, validate_clip

TIME = re.compile(r'^(\d{2,}):(\d{2}):(\d{2})[,.](\d{3})$')


def _milliseconds(value):
    match = TIME.fullmatch(value.strip())
    if not match:
        raise WorkflowError('INVALID_REQUEST', 'Invalid SRT timestamp: ' + value)
    hours, minutes, seconds, millis = map(int, match.groups())
    if minutes > 59 or seconds > 59:
        raise WorkflowError('INVALID_REQUEST', 'Invalid SRT minutes or seconds')
    return ((hours * 60 + minutes) * 60 + seconds) * 1000 + millis


def import_srt(text, *, fps, timeline_start=1, channel=10, prefix='subtitle', font=None, font_size=48):
    """Cover the cue interval: floor start and ceil end, times relative to output.

    Overlapping cues use the first free channel at/above channel. The returned
    quantization rows make subframe rounding visible; callers review positioning.
    Rich-text/position markup is rejected instead of silently stripping it.
    """
    rate = Fraction(*fps)
    if rate <= 0 or not 1 <= channel <= 128:
        raise WorkflowError('INVALID_REQUEST', 'Positive FPS and channel 1..128 required')
    blocks = re.split(r'\n[ \t]*\n', text.lstrip('\ufeff').replace('\r\n', '\n').replace('\r', '\n').strip())
    clips, quantization, occupied, seen = [], [], {}, set()
    for block in blocks:
        if not block.strip():
            continue
        lines = block.splitlines()
        if len(lines) < 3 or not lines[0].strip().isdigit() or '-->' not in lines[1]:
            raise WorkflowError('INVALID_REQUEST', 'Expected SRT index, timing and nonempty text')
        index = int(lines[0].strip())
        if index in seen:
            raise WorkflowError('INVALID_REQUEST', 'Duplicate SRT index')
        seen.add(index)
        timings = lines[1].split('-->')
        if len(timings) != 2:
            raise WorkflowError('INVALID_REQUEST', 'Ambiguous SRT timing')
        start_ms, end_ms = map(_milliseconds, timings)
        if not start_ms < end_ms:
            raise WorkflowError('INVALID_REQUEST', 'SRT interval must be nonempty')
        body = '\n'.join(lines[2:]).strip()
        if re.search(r'<[^>]+>|\{\\', body):
            raise WorkflowError('UNSUPPORTED', 'SRT styling/position markup needs explicit conversion')
        left = Fraction(start_ms, 1000) * rate
        right = Fraction(end_ms, 1000) * rate
        start = timeline_start + left.numerator // left.denominator
        end = timeline_start - (-right.numerator // right.denominator)
        lane = channel
        while lane <= 128 and any(start < b and end > a for a, b in occupied.get(lane, [])):
            lane += 1
        if lane > 128:
            raise WorkflowError('RESOURCE_LIMIT', 'No free subtitle channel within native range')
        occupied.setdefault(lane, []).append((start, end))
        clip = {'id': prefix + '_' + str(index), 'kind': 'TEXT', 'name': prefix + '_' + str(index),
                'channel': lane, 'start': start, 'duration': end - start,
                'text': body, 'font_size': font_size}
        if font:
            clip['font'] = font
        clips.append(validate_clip(clip))
        quantization.append({'index': index, 'milliseconds': [start_ms, end_ms],
                             'frames': [start, end], 'rule': 'floor-start/ceil-end'})
    return {'clips': clips, 'quantization': quantization, 'fps': [rate.numerator, rate.denominator],
            'timeline_start': timeline_start, 'style_review': 'Native font, line breaks and placement require preview'}
