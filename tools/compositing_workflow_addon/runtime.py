"""Public compositing host capability policy."""

from .host_compat import is_supported_host


def supported(app):
    return is_supported_host(app.version)
