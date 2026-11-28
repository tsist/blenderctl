"""Inspect active animation and NLA playback ranges against the saved scene."""

from qa import finding


def run_probe(context, profile):
    scene = context["scene"]
    begin, end = scene.frame_start, scene.frame_end
    findings = []
    for obj in scene.objects:
        data = obj.animation_data
        if data is None:
            continue
        spans = []
        if data.action:
            spans.append((data.action.name, *data.action.frame_range))
        for track in data.nla_tracks:
            if track.mute:
                continue
            spans.extend((strip.name, strip.frame_start, strip.frame_end) for strip in track.strips if not strip.mute)
        for name, start, finish in spans:
            if start < begin or finish > end:
                findings.append(finding("animation_outside_scene_range", None, object=obj.name, clip=name,
                                        clip_range=[start, finish], scene_range=[begin, end]))
    return findings
