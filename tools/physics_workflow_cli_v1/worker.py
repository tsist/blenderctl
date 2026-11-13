"""Headless Blender worker invoked by physics_workflow_cli_v1."""

from __future__ import annotations

import json
from pathlib import Path
import sys

import bpy


def _read_args() -> dict[str, str]:
    argv = sys.argv
    if "--" not in argv:
        raise RuntimeError("Worker requires '-- <request_json> <output_json>'")
    rest = argv[argv.index("--") + 1 :]
    if len(rest) < 2:
        raise RuntimeError("Worker requires <request_json> and <output_json>")
    return {"request_path": rest[0], "result_path": rest[1]}


def main() -> None:
    args = _read_args()
    req_path = Path(args["request_path"]).resolve()
    res_path = Path(args["result_path"]).resolve()
    payload = json.loads(req_path.read_text(encoding="utf-8"))

    tools_dir = str(Path(payload["tools_dir"]).resolve())
    if tools_dir not in sys.path:
        sys.path.insert(0, tools_dir)

    import physics_nodes_workflow_addon as pnw

    input_blend = Path(payload["input_blend"]).resolve()
    output_blend = Path(payload["output_blend"]).resolve()
    output_blend.parent.mkdir(parents=True, exist_ok=True)

    bpy.ops.wm.open_mainfile(filepath=str(input_blend), use_scripts=False)
    res = pnw.execute_request(payload["request"])
    if res.get("ok"):
        bpy.ops.wm.save_as_mainfile(filepath=str(output_blend))
        res["output_blend"] = str(output_blend)

    res_path.write_text(json.dumps(res, indent=2, ensure_ascii=False), encoding="utf-8")
    if not res.get("ok"):
        sys.exit(1)


if __name__ == "__main__":
    main()
