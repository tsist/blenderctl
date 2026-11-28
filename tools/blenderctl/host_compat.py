"""Version policy shared by launchers and Blender worker entry points."""

MINIMUM_BLENDER_VERSION = (5, 3, 0)


def is_supported_host(version):
    """Accept a release tuple, bpy.app.version, or Windows product version."""
    if isinstance(version, str):
        try:
            version = tuple(int(part) for part in version.split("."))
        except ValueError:
            return False
    return (isinstance(version, (tuple, list)) and len(version) >= 3
            and all(type(part) is int for part in version[:3])
            and tuple(version[:3]) >= MINIMUM_BLENDER_VERSION)


def blender_identity(app):
    """Return the observed host identity stored in job and session receipts."""
    return {"build": app.build_hash.decode(), "blender_version": ".".join(map(str, app.version))}


def valid_host_identity(record):
    import re

    return (isinstance(record.get("build"), str) and re.fullmatch(r"[0-9a-f]{7,64}", record["build"])
            and is_supported_host(record.get("blender_version")))


def probe_blender_identity(binary):
    import re
    import subprocess

    result = subprocess.run([str(binary), "--version"], capture_output=True, text=True, timeout=15,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    version = re.search(r"^Blender (\d+\.\d+\.\d+)", result.stdout, re.MULTILINE)
    build = re.search(r"^\s*build hash:\s*([0-9a-f]+)", result.stdout, re.MULTILINE)
    record = {"blender_version": version[1] if version else None, "build": build[1] if build else None}
    if result.returncode or not valid_host_identity(record):
        raise ValueError("Expected Windows Blender 5.3.0 or later")
    return record
