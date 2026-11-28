"""Render the configured frame in an isolated copy of the active scene."""

import json
import os
from pathlib import Path
import sys
import tempfile

import bpy


def _write(path, payload):
    scratch = path.with_suffix(".tmp")
    scratch.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    scratch.replace(path)


def main():
    scene_name, frame, fmt, output_name, result_name = sys.argv[sys.argv.index("--") + 1:]
    output, result = Path(output_name), Path(result_name)
    status = result.with_name("status.json")
    with result.with_name("blender-render.log").open("wb", buffering=0) as log:
        for descriptor in (1, 2):
            os.dup2(log.fileno(), descriptor)
        try:
            scene = bpy.data.scenes[scene_name]
            scene.frame_set(int(frame))
            scene.render.image_settings.file_format = fmt
            if fmt == "OPEN_EXR":
                scene.render.image_settings.color_mode = "RGBA"
                scene.render.image_settings.color_depth = "32"
            scene.render.use_file_extension = False
            _write(status, {"detail": "正在渲染当前帧并计算合成效果", "progress": 0.35})
            with tempfile.TemporaryDirectory(prefix=".cwf-", dir=output.parent) as directory:
                staged = Path(directory) / output.name
                scene.render.filepath = str(staged)
                if "FINISHED" not in bpy.ops.render.render(write_still=True, scene=scene.name) or not staged.is_file():
                    raise RuntimeError("Blender 未生成完整的合成输出")
                _write(status, {"detail": "正在保存合成结果", "progress": 0.95})
                # On the supported Windows host this fails if the destination exists.
                staged.rename(output)
            _write(result, {"ok": True, "file": str(output)})
        except Exception as error:
            _write(result, {"ok": False, "error": str(error), "log": log.name})
            return 1
    return 0


if __name__ == "__main__":
    main()
