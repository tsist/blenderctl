# Local installation launcher; implementation remains in the qualified runtime.
import runpy
from pathlib import Path

if __name__ == "__main__":
    runpy.run_path(str(Path('<PROJECT_ROOT>\\runtime\\video-editing\\0.1.3\\skills\\blender-video-editing\\scripts\\prepare_media.py')), run_name="__main__")
