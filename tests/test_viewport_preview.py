# SPDX-License-Identifier: GPL-3.0-or-later
"""Host-only unit tests for tools/viewport_preview (standard library only, cross-platform)."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
VP_DIR = ROOT / "tools" / "viewport_preview"
VP_CLI = VP_DIR / "cli.py"

if str(VP_DIR) not in sys.path:
    sys.path.insert(0, str(VP_DIR))

import compositor_board  # noqa: E402
import desktop_runner  # noqa: E402
import framing  # noqa: E402
import session_manager  # noqa: E402


class ViewportPreviewHostTests(unittest.TestCase):
    def call(self, *args):
        return subprocess.run(
            [sys.executable, "-B", str(VP_CLI), *map(str, args)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )

    def test_front_convention_is_negative_y(self):
        self.assertEqual(framing.DIRECTION_VECTORS["FRONT"], (0.0, -1.0, 0.0))
        self.assertEqual(framing.DIRECTION_VECTORS["BACK"], (0.0, 1.0, 0.0))
        self.assertEqual(framing.DIRECTION_VECTORS["RIGHT"], (1.0, 0.0, 0.0))
        self.assertEqual(framing.DIRECTION_VECTORS["TOP"], (0.0, 0.0, 1.0))

    def test_layout_presets_and_projection_overrides(self):
        grid, views = framing.resolve_views("quad")
        self.assertEqual(grid, (2, 2))
        self.assertEqual([v["preset"] for v in views], ["TOP", "FRONT_3Q", "FRONT", "RIGHT"])
        self.assertEqual([v["projection"] for v in views], ["ORTHO", "PERSP", "ORTHO", "ORTHO"])

        grid_o, views_o = framing.resolve_views("orbit", projection_override="ortho")
        self.assertEqual(grid_o, (2, 2))
        self.assertTrue(all(v["projection"] == "ORTHO" for v in views_o))

        grid_c, views_c = framing.resolve_views("single", custom_views=["camera:MainCam"])
        self.assertEqual(grid_c, (1, 1))
        self.assertEqual(views_c[0]["camera"], "MainCam")
        self.assertEqual(views_c[0]["projection"], "CAMERA")

    def test_view_token_aliases_and_errors(self):
        tok = framing.parse_view_token("hero:ortho")
        self.assertEqual(tok["preset"], "FRONT_3Q")
        self.assertEqual(tok["projection"], "ORTHO")
        with self.assertRaises(ValueError):
            framing.parse_view_token("invalid_direction")
        with self.assertRaises(ValueError):
            framing.resolve_views("quad", custom_views=["front", "right"])

    def test_cli_help_and_session_help_work_without_blender(self):
        r1 = self.call("preview", "--help")
        self.assertEqual(r1.returncode, 0, r1.stderr)
        self.assertIn("solid_wire", r1.stdout)
        self.assertIn("--compare-modes", r1.stdout)
        self.assertIn("--strict-denoiser", r1.stdout)

        r2 = self.call("preview", "session", "--help")
        self.assertEqual(r2.returncode, 0, r2.stderr)
        self.assertIn("start", r2.stdout)
        self.assertIn("stop", r2.stdout)

    def test_cli_session_list_works_on_empty_dir(self):
        with tempfile.TemporaryDirectory() as td:
            res = self.call("preview", "session", "list", "--sessions-dir", td)
            self.assertEqual(res.returncode, 0, res.stderr)
            payload = json.loads(res.stdout)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["data"]["sessions"], [])

    def test_cli_rejects_missing_file_cleanly(self):
        with tempfile.TemporaryDirectory() as td:
            res = self.call("--jobs-dir", td, "preview")
            self.assertNotEqual(res.returncode, 0)
            payload = json.loads(res.stdout)
            self.assertFalse(payload["ok"])

    def test_modules_import_without_numpy_or_win32_call(self):
        self.assertTrue(hasattr(compositor_board, "compose_board"))
        self.assertTrue(hasattr(desktop_runner, "run_on_isolated_desktop"))
        self.assertTrue(hasattr(session_manager, "start_session"))


if __name__ == "__main__":
    unittest.main()
