# SPDX-License-Identifier: GPL-3.0-or-later
"""Host-only coverage for the portable Win53 launcher and ZIP validation."""
import json
from pathlib import Path
import subprocess
import sys
import unittest


ENTRY = Path(__file__).resolve().parents[1] / "tools" / "rig_phase3_handoff" / "cli.py"


class RigPhase3Contract(unittest.TestCase):
    def test_describe_requires_no_local_package(self):
        proc = subprocess.run([sys.executable, "-B", str(ENTRY), "rigging-workflow", "phase3", "describe"],
                              capture_output=True, text=True, check=True)
        data = json.loads(proc.stdout)["data"]
        self.assertTrue(data["package_required_for_execution"])
        self.assertEqual(8, len(data["operations"]))
        self.assertEqual(5, len(data["live"]))

    def test_aliases_and_escapes_are_rejected_before_extracting(self):
        code = """
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from zipfile import ZipFile
sys.path.insert(0, sys.argv[1])
from runtime_pin import extract_package, REQUIRED_MEMBERS
from protocol import Failure
with TemporaryDirectory() as tmp:
    root = Path(tmp)
    names = ('rigging_workflow/../escape.py',
             'rigging_workflow/./alias.py',
             'rigging_workflow/CON.py',
             'rigging_workflow/__INIT__.py')
    for index, bad_name in enumerate(names):
        bundle = root / f'{index}.zip'
        with ZipFile(bundle, 'w') as output:
            for name in REQUIRED_MEMBERS | {bad_name}:
                output.writestr(name, '# test')
        extracted = root / f'out{index}'
        try:
            extract_package(bundle, extracted)
        except Failure as exc:
            assert exc.code == 'INVALID_REQUEST'
        else:
            raise AssertionError(bad_name)
        assert not extracted.exists()
"""
        proc = subprocess.run([sys.executable, "-B", "-c", code, str(ENTRY.parent)],
                              capture_output=True, text=True)
        self.assertEqual(0, proc.returncode, proc.stderr)


if __name__ == "__main__":
    unittest.main()
