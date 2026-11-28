"""Select the installed Toon implementation and verify versioned recovery bundles."""

import os
from pathlib import Path

from protocol import Failure, ROOT, digest, read_json


def select_toon_bundle(registry, edition=None):
    override = os.environ.get("BLENDERCTL_TOON_BUNDLE") if not edition else None
    edition = edition or registry["new_jobs"]["toon-workflow"]
    bundle = registry["toon_bundles"][edition]
    directory = Path(override) if override else ROOT / bundle["directory"]
    if not directory.is_absolute():
        raise Failure("INVALID_REQUEST", "Toon bundle must be an absolute directory")
    entry = directory / "toon_workflow_cli_v1/cli.py"
    if override or bundle.get("workspace"):
        return entry
    manifest = directory / "bundle.json"
    if digest(manifest) != bundle["manifest_sha256"]:
        raise Failure("CONFLICT", f"Toon {edition} bundle manifest has changed")
    if digest(entry) != read_json(manifest)["files"]["toon_workflow_cli_v1/cli.py"]:
        raise Failure("CONFLICT", f"The Toon {edition} launcher differs from the verified package")
    return entry
