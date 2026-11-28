"""Deprecated thin forwarder to the qualified add-on key-library module.

No key-library logic is copied here. Attribute access lazily imports
``<qualified-prefix>.exchange_runtime.key_library`` through the bridge's
already-verified loader. The add-on root is never added to ``sys.path`` and no
generic ``key_library``/``exchange_runtime`` modules are created.
"""
from __future__ import annotations

import importlib

import rig_exchange_bridge


def _module():
    core, _, _, _, _ = rig_exchange_bridge._load()
    prefix = str(core.__package__).split(".")[0]
    return importlib.import_module(prefix + ".exchange_runtime.key_library")


def __getattr__(name):
    return getattr(_module(), name)
