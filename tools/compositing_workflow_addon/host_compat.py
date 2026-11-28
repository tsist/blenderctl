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
