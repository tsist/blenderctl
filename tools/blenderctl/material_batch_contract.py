# SPDX-License-Identifier: GPL-3.0-or-later
"""Host adapter; no new branch files read before dependency isolation."""
from pathlib import Path
import sys
from protocol import Failure
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from material_workflow_addon.batch_contract import (BATCH_MANIFEST_SCHEMA, BATCH_PARAMS_SCHEMA,
    topological_order, assignment_manifest, input_documents)
from material_workflow_addon.batch_contract import normalize_params as _normalize_params
from material_workflow_addon.contract import WorkflowError
def normalize_params(value):
    try: return _normalize_params(value)
    except WorkflowError as error: raise Failure(error.code, str(error)) from error
