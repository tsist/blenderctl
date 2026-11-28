"""Exercise a plugin in one disposable factory Blender process."""
import importlib
import json
from pathlib import Path
import sys
import time
import traceback

import bpy


def probe(source):
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(source.parent))
    module = importlib.import_module(source.name)
    if Path(module.__file__).resolve() != source / "__init__.py":
        raise RuntimeError("Blender imported a different plugin")
    started = time.perf_counter()
    module.register()
    duration = time.perf_counter() - started
    module.unregister()
    return {"ok": True, "registered": True, "unregistered": True, "registration_seconds": duration}


if __name__ == "__main__":
    source, receipt = map(Path, sys.argv[sys.argv.index("--") + 1:])
    try:
        result = probe(source.resolve())
    except Exception:
        result = {"ok": False, "error": traceback.format_exc(limit=8)}
    result.update(source=str(source), blender=bpy.app.version_string, build=bpy.app.build_hash.decode())
    receipt.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    if not result["ok"]:
        raise RuntimeError("Plugin registration failed; see receipt")
