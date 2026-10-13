"""Import plain UTF-8 SRT to native TEXT clips without mutating a Blender file."""
import argparse
import hashlib
import json
from pathlib import Path
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True, type=Path, help='Project/development root containing tools')
    parser.add_argument('--srt', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--font', required=True, type=Path)
    parser.add_argument('--fps', required=True, help='Rational rate such as 24/1 or 30000/1001')
    parser.add_argument('--timeline-start', type=int, default=1)
    parser.add_argument('--channel', type=int, default=10)
    parser.add_argument('--font-size', type=int, default=48)
    parser.add_argument('--prefix', default='subtitle')
    args = parser.parse_args()
    sys.path.insert(0, str(args.root.resolve() / 'tools'))
    from video_editing_workflow_addon.subtitles import import_srt
    def desc(path):
        path = path.resolve()
        return {'file': str(path), 'expected_sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
    font = desc(args.font)
    result = import_srt(args.srt.read_text(encoding='utf-8-sig'), fps=list(map(int, args.fps.split('/'))),
                        timeline_start=args.timeline_start, channel=args.channel, prefix=args.prefix,
                        font=font, font_size=args.font_size)
    result['resources'] = [desc(args.srt), font]
    with args.output.open('x', encoding='utf-8', newline='\n') as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
    print(json.dumps({'output': str(args.output.resolve()), 'cue_count': len(result['clips'])}))


if __name__ == '__main__':
    main()
