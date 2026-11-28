"""CLI and worker input contract for read-only saved-file QA."""

from pathlib import Path

from model_contract import normalize_qa
from node_contract import FILE
from protocol import Failure, SCHEMA, read_json
from render_contract import file_input
from scene_contract import BOOL, array, integer, obj, validate


TILES = {**array(integer(1001, 1099), 1, 99), "uniqueItems": True}
PIXELS = integer(1, 32768)
TILE_SIZE = array(PIXELS, 2, 2)
UDIM = obj({
    "enabled": BOOL,
    "tiles": TILES,
    "tile_resolution": PIXELS,
    "per_object_tiles": {"type": "object", "additionalProperties": TILES},
}, ["enabled"])


def normalize_params(params):
    if not isinstance(params, dict) or set(params) - {
        "file", "expected_sha256", "options", "udim", "udim_measurements", "profile_scene", "probe_file",
    }:
        raise Failure("INVALID_REQUEST", "Invalid QA request fields")
    source = {key: value for key, value in params.items() if key in FILE["properties"]}
    validate(source, FILE)
    source["file"] = str(Path(source["file"]).resolve())
    if Path(source["file"]).suffix.lower() != ".blend":
        raise Failure("INVALID_REQUEST", "QA accepts a saved .blend source")
    file_input(source)
    options = normalize_qa(params.get("options", {"profile": "game"}))
    result = {**source, "options": options}
    if "profile_scene" in params:
        validate(params["profile_scene"], BOOL)
        result["profile_scene"] = params["profile_scene"]
    if "probe_file" in params:
        validate(params["probe_file"], FILE)
        result["probe_file"] = file_input(params["probe_file"])
    if "udim" in params:
        udim = params["udim"]
        if not isinstance(udim, dict):
            raise Failure("INVALID_REQUEST", "UDIM declaration must be an object")
        resolved = dict(udim)
        per_object = resolved.pop("per_object_tiles", {})
        validate(resolved, {**UDIM, "required": ["enabled", "tiles", "tile_resolution"]
                            if udim.get("enabled") is True else ["enabled"]})
        if not isinstance(per_object, dict) or len(per_object) > 1000:
            raise Failure("INVALID_REQUEST", "UDIM per_object_tiles must be an object with up to 1000 entries")
        for name, tiles in per_object.items():
            if not isinstance(name, str) or not name or "\x00" in name:
                raise Failure("INVALID_REQUEST", "UDIM object names must be nonempty")
            validate(tiles, TILES)
            if not set(tiles) <= set(resolved.get("tiles", [])):
                raise Failure("INVALID_REQUEST", f"UDIM tile assignment exceeds declared tiles for {name!r}")
        if resolved["enabled"] and options.get("uv") is False:
            raise Failure("INVALID_REQUEST", "Declared UDIM requires UV inspection")
        result["udim"] = {**resolved, "per_object_tiles": per_object}
    if "udim_measurements" in params:
        measured = params["udim_measurements"]
        validate(measured, obj({"tile_size_px": TILE_SIZE}, ["tile_size_px"]))
        declaration = result.get("udim", {})
        if measured["tile_size_px"][0] != declaration.get("tile_resolution"):
            raise Failure("INVALID_REQUEST", "C1 tile_resolution must equal the declared width in pixels")
        result["udim_measurements"] = measured
    return result


def add_arguments(sub):
    parser = sub.add_parser("qa", help="cold-open a saved blend and run engineering, UV and department probes")
    parser.add_argument("file")
    parser.add_argument("--expected-sha256", required=True)
    parser.add_argument("--profile", choices=("game", "render", "mobile_strict", "print"), default="game")
    parser.add_argument("--objects", nargs="+", help="inspect only these named meshes")
    parser.add_argument("--uv-layer")
    parser.add_argument("--uv-report", type=Path, help="UV workflow JSON containing the C1 udim declaration")
    parser.add_argument("--no-uv", action="store_true")
    parser.add_argument("--base-only", action="store_true")
    parser.add_argument("--include-hidden", action="store_true")
    parser.add_argument("--max-tris", type=int, help="cap spatial BVH checks by total triangle count")
    parser.add_argument("--profile-scene", action="store_true", help="write evaluation timings and a texture memory estimate")
    parser.add_argument("--media", type=Path, help="optional finished movie for black-frame and EBU R128 probes")
    parser.add_argument("--media-sha256", help="SHA-256 required with --media")


def request_from_args(args):
    options = {"profile": args.profile}
    for key in ("objects", "uv_layer", "max_tris"):
        value = getattr(args, key)
        if value is not None:
            options[key] = value
    if args.no_uv:
        options["uv"] = False
    if args.base_only:
        options["evaluated"] = False
    if args.include_hidden:
        options["include_hidden"] = True
    params = {"file": args.file, "expected_sha256": args.expected_sha256,
              "options": options, "profile_scene": args.profile_scene}
    if args.uv_report:
        document = read_json(args.uv_report)
        if not isinstance(document, dict) or "udim" not in document:
            raise Failure("INVALID_REQUEST", "The UV report must contain a C1 udim declaration")
        params["udim"] = document["udim"]
        measured = document.get("udim_measurements", {})
        if isinstance(measured, dict) and "tile_size_px" in measured:
            params["udim_measurements"] = {"tile_size_px": measured["tile_size_px"]}
    if bool(args.media) != bool(args.media_sha256):
        raise Failure("INVALID_REQUEST", "--media and --media-sha256 must be specified together")
    if args.media:
        params["probe_file"] = {"file": str(args.media.resolve()), "expected_sha256": args.media_sha256}
    return {"schema_version": SCHEMA, "command": "qa.inspect", "params": params}
