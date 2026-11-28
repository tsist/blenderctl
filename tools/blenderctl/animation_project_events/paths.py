"""Fixed path resolution for animation_project_events.

Production layout (the only accepted production location):

    tools/blenderctl/animation_project_events/project_events.py

The project root is derived only from this file's formal location.  There is
no upward search and no env/request runtime selection.  A test-only importable
configuration may provide PROJECT_ROOT when the formal layout is not present.
"""
# action: PF-01 formal-in-place paths and importable test layout only.
import importlib.util
import sys
from pathlib import Path


HERE = Path(__file__).resolve().parent


def _formal_root():
    if (HERE.name == "animation_project_events"
            and HERE.parent.name == "blenderctl"
            and HERE.parent.parent.name == "tools"):
        return HERE.parent.parent.parent
    return None


def _test_root():
    config = HERE / "tests" / "test_layout.py"
    if not config.is_file():
        raise RuntimeError(
            "install at tools/blenderctl/animation_project_events "
            "or provide tests/test_layout.py")
    spec = importlib.util.spec_from_file_location(
        "animation_project_events_test_layout", config)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    root = Path(module.PROJECT_ROOT).resolve()
    if not root.is_dir():
        raise RuntimeError("test PROJECT_ROOT is not a directory: " + str(root))
    return root


ROOT = _formal_root() or _test_root()
CORE = ROOT / "tools" / "blenderctl"
RUNTIME = ROOT / "tools"
BLENDER_53 = ROOT / "runtime/blender/releases/5.3.0/blender-5.3.0-windows-x64/blender.exe"
if not CORE.is_dir():
    raise RuntimeError("common blenderctl core is missing: " + str(CORE))
sys.path.insert(0, str(CORE))
