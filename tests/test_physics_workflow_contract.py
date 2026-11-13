"""Contract, validation bounds, and hash determinism unit tests for Physics Nodes Workflow."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest

TOOLS_DIR = Path(__file__).resolve().parent.parent / "tools"
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

from physics_nodes_workflow_addon import contract  # noqa: E402
from physics_workflow_cli_v1 import protocol, runner  # noqa: E402


class TestPhysicsWorkflowContract(unittest.TestCase):
    def test_describe_contract_has_three_domains_and_six_presets(self) -> None:
        info = contract.describe_contract()
        self.assertEqual(info["contract_id"], contract.CONTRACT_ID)
        self.assertEqual(tuple(info["domains"]), contract.DOMAINS)
        self.assertGreaterEqual(len(info["presets"]), 6)
        self.assertIn("rigid_drop_pack", info["presets"])
        self.assertIn("cloth_banner_wind", info["presets"])
        self.assertIn("fluid_fountain_sdf", info["presets"])

    def test_normalize_request_defaults_and_hash(self) -> None:
        req1 = contract.normalize_request(
            {
                "operation": "build",
                "preset_id": "cloth_banner_wind",
                "target_object": "BannerMesh",
            }
        )
        req2 = contract.normalize_request(
            {
                "operation": "build",
                "preset_id": "cloth_banner_wind",
                "target_object": "BannerMesh",
            }
        )
        self.assertEqual(req1["domain"], "cloth_rope")
        self.assertEqual(req1["parameters_hash"], req2["parameters_hash"])

        req3 = contract.normalize_request(
            {
                "operation": "build",
                "preset_id": "cloth_banner_wind",
                "target_object": "BannerMesh",
                "parameters": {"wind_turbulence": 4.5},
            }
        )
        self.assertNotEqual(req1["parameters_hash"], req3["parameters_hash"])

    def test_validation_rejects_invalid_bounds_and_mismatched_domain(self) -> None:
        bad_cases = [
            {"preset_id": "rigid_drop_pack", "substeps": 0},
            {"preset_id": "rigid_drop_pack", "substeps": 9},
            {"preset_id": "rigid_drop_pack", "frame_start": 50, "frame_end": 10},
            {"preset_id": "rigid_drop_pack", "domain": "cloth_rope"},
            {"preset_id": "rigid_drop_pack", "bridge_mode": "UNKNOWN_SOLVER"},
            {"preset_id": "rigid_drop_pack", "parameters": {"nonexistent_param": 1}},
            {"preset_id": "rigid_drop_pack", "parameters": {"restitution": 1.5}},
            {"preset_id": "rigid_drop_pack", "parameters": {"gravity": [0.0, -9.81]}},
        ]
        for case in bad_cases:
            with self.assertRaises(contract.PhysicsContractError) as ctx:
                contract.normalize_request(case)
            self.assertEqual(ctx.exception.code, "VALIDATION_ERROR")

    def test_cli_describe_envelope(self) -> None:
        resp = runner.describe()
        self.assertTrue(resp.ok)
        self.assertEqual(resp.command, "describe")
        self.assertEqual(resp.data["contract_id"], contract.CONTRACT_ID)
        # Ensure serializable to UTF-8 JSON
        dumped = json.dumps(resp.to_dict(), ensure_ascii=False)
        self.assertIn("rigid_drop_pack", dumped)


if __name__ == "__main__":
    unittest.main()
