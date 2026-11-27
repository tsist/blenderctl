"""Host-side contracts for the public hard-surface production adapter."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "blenderctl"))
from production_contract import HARD_SURFACE_RELEASE, package_root
from protocol import Failure, validate


class HardSurfaceProductionContractTest(unittest.TestCase):
    def setUp(self):
        self.request = {
            "schema_version": "1.0",
            "command": "production.run",
            "params": {"file": str(Path("source.blend").resolve()), "expected_sha256": "a" * 64,
                       "domain": "hard-surface", "request": {"operation": "describe"}},
        }

    def test_selects_hard_surface_release_only_for_hard_surface(self):
        self.assertEqual(validate(self.request)["params"]["release"], HARD_SURFACE_RELEASE)
        self.request["params"]["domain"] = "compositing"
        self.assertNotIn("release", validate(self.request)["params"])

    def test_requires_preview_for_commit(self):
        params = self.request["params"]
        for op in ("execute", "commit-sweep"):
            edit = {"operation": op, "object": "Shell"}
            if op == "execute":
                edit["params"] = {}
            params["request"] = edit
            with self.subTest(operation=op), self.assertRaisesRegex(Failure, "expected_preview_signature"):
                validate(self.request)

    def test_rejects_other_release_or_invalid_coordinates(self):
        params = self.request["params"]
        params["release"] = "1.0.0"
        with self.assertRaisesRegex(Failure, "requires release"):
            validate(self.request)
        params.pop("release")
        params["request"] = {"operation": "inspect-sweep", "object": "Rail",
                             "station_positions_mm": {"mid": [0, 0, 1e100]}}
        with self.assertRaisesRegex(Failure, "finite world-millimetre"):
            validate(self.request)

    def test_hard_surface_manifest_must_match_published_inventory(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root)
            release = directory / HARD_SURFACE_RELEASE
            release.mkdir()
            (release / "manifest.json").write_text("{}", encoding="utf-8")
            with patch("production_contract.RELEASES", directory):
                with self.assertRaisesRegex(Failure, "published inventory"):
                    package_root("hard-surface", HARD_SURFACE_RELEASE)


if __name__ == "__main__":
    unittest.main()
