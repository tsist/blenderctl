"""Runtime builds admitted by the shared compositor apply and verification paths."""

SUPPORTED_BUILDS = {
    (5, 2, 1): b'9e2066aef7ef',
    (5, 3, 0): b'02f0899d78e3',
}


def supported(app):
    return SUPPORTED_BUILDS.get(tuple(app.version)) == app.build_hash
