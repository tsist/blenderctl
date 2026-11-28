"""Load the selected Animation Curves release in an isolated Blender worker."""
import os
from pathlib import Path
import sys

from host_compat import is_supported_host
from protocol import Failure

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))
from animation_support_handoff.editions import package


def load_author(bpy, edition, *, allow_53=False):
    author = package(edition)
    if not bpy.app.background or bpy.app.online_access or bpy.context.preferences.filepaths.use_scripts_auto_execute:
        raise Failure('UNSUPPORTED', 'Animation authoring requires an offline background worker with script auto-execution disabled')
    if os.name != "nt" or not is_supported_host(bpy.app.version):
        raise Failure("UNSUPPORTED", "Animation authoring requires Windows Blender 5.3.0 or later")
    sys.path.insert(0, str(author.parent))
    import animation_curves_workflow_addon as addon

    if '.'.join(map(str, addon.bl_info['version'])) != edition or Path(addon.__file__).resolve().parent != author:
        raise Failure('UNSUPPORTED', f'Expected the bundled {edition} animation author')
    return addon
