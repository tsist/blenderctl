"""Public, source-bound operations for native Hair Curves and static hair cards."""
from pathlib import Path

from node_contract import FILE
from protocol import Failure
from render_contract import file_input
from scene_contract import NAME, integer, number, obj, validate


def register(sub):
    actions = sub.add_parser("hair", help="Native Blender Hair Essentials").add_subparsers(dest="action", required=True)
    run = actions.add_parser("run", help="Assemble a groom or export static hair cards into the job directory")
    run.add_argument("--request", required=True, type=Path)


def normalize(params):
    if not isinstance(params, dict):
        raise Failure("INVALID_REQUEST", "Hair request must be an object")
    operation = params.get("operation")
    common = {**FILE["properties"], "operation": {"const": operation}, "name": NAME}
    if operation == "assemble":
        fields = {**common, "guide": NAME, "material": NAME, "settings": {"type": "object"}}
        required = [*common, "guide"]
    elif operation == "export_cards":
        fields = {**common, "groom": NAME, "frame": integer(1, 100000), "card_count": integer(1, 10000),
                  "card_width": number(.000001, 1)}
        required = [*common, "groom"]
    else:
        raise Failure("INVALID_REQUEST", "Choose assemble or export_cards")
    validate(params, obj(fields, required))
    source = file_input({k: params[k] for k in FILE["properties"]})
    if Path(source["file"]).suffix.lower() != ".blend":
        raise Failure("INVALID_REQUEST", "Hair operations require a saved .blend file")
    return {**params, **source}
