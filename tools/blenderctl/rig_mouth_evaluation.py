"""Supply the existing shared evaluator for releases missing the mouth-core alias."""
from contextlib import contextmanager


@contextmanager
def mouth_update_callback(mouth):
    missing = not hasattr(mouth, "_update")
    if missing:
        mouth._update = mouth.core._update
    try:
        yield missing
    finally:
        if missing:
            del mouth._update
