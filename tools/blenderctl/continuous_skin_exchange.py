"""Deprecated compatibility shim for the shared continuous-rig adapter.

This module intentionally contains no exchange algorithm. It forwards the
legacy ``export_shared`` entry point to the single qualified add-on bridge.
The current CLI dispatch uses ``rig_exchange_bridge`` directly; this shim only
keeps older local imports working during the 0.84 transition.
"""
from __future__ import annotations

import rig_exchange_bridge

ADAPTER = "BBONE_SHARED_CONTINUOUS_V1"


def export_shared(params, spec, job, started, hashes=None):
    return rig_exchange_bridge.export_shared(
        params, spec, job, started, hashes=hashes
    )
