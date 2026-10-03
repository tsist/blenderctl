# SPDX-License-Identifier: GPL-3.0-or-later
"""Pure Python protocol tests; no GUI or GPU required."""
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("job_bridge", (Path(__file__).resolve().parents[1] / "tools/material_workflow_addon/job_bridge.py"))
b = importlib.util.module_from_spec(spec)
spec.loader.exec_module(b)


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        for name in ("tools/blenderctl/cli.py", "blender.exe", "5.2/python/bin/python.exe"):
            p = self.root / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.touch()
        b._CONFIGS.clear()
        with patch.object(b, "_process", return_value=subprocess.CompletedProcess([], 0, "0.51.0\n", "")):
            self.config = b.configure(str(self.root), str(self.root / "blender.exe"))
        self.request = {"schema_version": "1.0", "command": "doctor", "params": {}}
        self.request_file = self.root / "request.json"
        self.request_file.write_text(json.dumps(self.request))
        self.jobs = self.root / "jobs"
        self.job = self.jobs / ("a" * 32)
        self.job.mkdir(parents=True)
        (self.job / "request.json").write_text(json.dumps(self.request))
        (self.job / "launch.json").write_text(json.dumps({"blender": self.config["blender_binary"]}))
        self.ref = dict(job_id=self.job.name, jobs_dir=str(self.jobs), command="doctor",
                        request_sha256=b._sha(self.request), project_root=str(self.root),
                        blender_binary=self.config["blender_binary"])

    def tearDown(self):
        self.temp.cleanup()

    def response(self, value, code=0):
        return subprocess.CompletedProcess([], code, json.dumps(value), "")

    def test_configuration_cached(self):
        with patch.object(b, "_process") as mock:
            self.assertEqual(b.configure(str(self.root), self.config["blender_binary"]), self.config)
            mock.assert_not_called()

    def test_submit_and_reload(self):
        envelope = dict(ok=True, command="job.submit", job_id=self.job.name, data={"state": "queued"})
        with patch.object(b, "_process", return_value=self.response(envelope)) as mock:
            value = b.submit(str(self.request_file), str(self.jobs), self.config)
            self.assertEqual(value["job_ref"]["job_id"], self.job.name)
            self.assertIn("--async", mock.call_args.args[0])
        with patch.object(b, "_process", return_value=self.response(dict(ok=True, command="job.status", job_id=self.job.name, data={"state": "running", "cancel_requested": True}))):
            s = b.summarize(b.status(json.loads(json.dumps(value["job_ref"])), self.config))
            self.assertFalse(s["terminal"])
            self.assertTrue(s["cancel_requested"])

    def test_failed_result_is_preserved(self):
        value = dict(ok=False, command="doctor", job_id=self.job.name, error={"code": "CANCELLED"})
        with patch.object(b, "_process", return_value=self.response(value, 7)):
            self.assertEqual(b.result(self.ref, self.config), value)

    def test_normalized_request_owned(self):
        normalized = dict(self.request, params={"default_added": True})
        (self.job / "request.json").write_text(json.dumps(normalized))
        envelope = dict(ok=True, command="job.submit", job_id=self.job.name, data={"state": "queued"})
        with patch.object(b, "_process", return_value=self.response(envelope)):
            value = b.submit(str(self.request_file), str(self.jobs), self.config)
        self.assertEqual(value["job_ref"]["request_sha256"], b._sha(normalized))
        self.assertEqual(value["job_ref"]["submitted_request_sha256"], b._sha(self.request))

    def test_invalid_ownership_never_calls_cli(self):
        for key, value in (("job_id", "../other"), ("request_sha256", "0" * 64), ("command", "material.batch"), ("project_root", "other")):
            ref = dict(self.ref, **{key: value})
            with patch.object(b, "_process") as mock, self.assertRaises(b.BridgeError):
                b.cancel(ref, self.config)
            mock.assert_not_called()

    def test_invalid_json(self):
        with patch.object(b, "_process", return_value=subprocess.CompletedProcess([], 2, "oops", "bad")), self.assertRaises(b.BridgeError):
            b.status(self.ref, self.config)

    def test_submit_error_retained(self):
        value = dict(ok=False, error={"code": "INVALID_REQUEST"})
        with patch.object(b, "_process", return_value=self.response(value, 2)), self.assertRaises(b.BridgeError) as caught:
            b.submit(str(self.request_file), str(self.jobs), self.config)
        self.assertEqual(caught.exception.envelope, value)

    def test_timeout_and_no_shell(self):
        with patch.object(subprocess, "run", side_effect=subprocess.TimeoutExpired([], 15)) as mock, self.assertRaises(b.BridgeError):
            b._process(["python", "cli"], self.root, 15)
        self.assertFalse(mock.call_args.kwargs["shell"])
        self.assertEqual(mock.call_args.kwargs["timeout"], 15)

    def test_invalid_timeout_and_relative_paths(self):
        for seconds in (True, 0, float("nan")):
            with self.assertRaises(b.BridgeError):
                b.submit(str(self.request_file), str(self.jobs), self.config, seconds)
        with self.assertRaises(b.BridgeError):
            b.configure("relative", self.config["blender_binary"])

    def test_recover_unique_never_submits(self):
        with patch.object(b, "_process") as mocked:
            ref = b.recover(str(self.request_file), str(self.jobs), self.config)
            self.assertEqual(ref["job_id"], self.job.name)
            mocked.assert_not_called()

    def test_recover_no_match(self):
        (self.job / "request.json").write_text(json.dumps(dict(self.request, params={"unexpected": 1})))
        with self.assertRaises(b.BridgeError):
            b.recover(str(self.request_file), str(self.jobs), self.config)

    def test_recover_multiple_rejected(self):
        second = self.jobs / ("b" * 32)
        second.mkdir()
        for name in ("request.json", "launch.json"):
            (second / name).write_bytes((self.job / name).read_bytes())
        with self.assertRaises(b.BridgeError):
            b.recover(str(self.request_file), str(self.jobs), self.config)

    def test_recover_excludes_verified_previous_id(self):
        second = self.jobs / ("b" * 32)
        second.mkdir()
        for name in ("request.json", "launch.json"):
            (second / name).write_bytes((self.job / name).read_bytes())
        with patch.object(b, "_process") as mocked:
            ref = b.recover(str(self.request_file), str(self.jobs), self.config,
                            exclude_job_ids=[self.job.name])
            self.assertEqual(ref["job_id"], second.name)
            mocked.assert_not_called()

    def test_recover_all_excluded_refuses(self):
        with self.assertRaises(b.BridgeError):
            b.recover(str(self.request_file), str(self.jobs), self.config,
                      exclude_job_ids=[self.job.name])

    def test_recover_invalid_exclude_refuses(self):
        for excluded in (["../foreign"], ["A" * 32], [42]):
            with self.assertRaises(b.BridgeError):
                b.recover(str(self.request_file), str(self.jobs), self.config,
                          exclude_job_ids=excluded)

    def test_recover_matches_all_layers_not_just_file(self):
        normalized = dict(schema_version="1.0", command="material.batch",
                          params={"file": "same.blend", "manifest": {"layers": [{"color": "red"}]}})
        self.request_file.write_text(json.dumps(normalized))
        changed = json.loads(json.dumps(normalized))
        changed["params"]["manifest"]["layers"][0]["color"] = "blue"
        (self.job / "request.json").write_text(json.dumps(changed))
        with patch.object(b, "_normalized_request", return_value=normalized), self.assertRaises(b.BridgeError):
            b.recover(str(self.request_file), str(self.jobs), self.config)

    def test_recover_different_backend_rejected(self):
        (self.job / "launch.json").write_text(json.dumps({"blender": str(self.root / "other.exe")}))
        with self.assertRaises(b.BridgeError):
            b.recover(str(self.request_file), str(self.jobs), self.config)

    def test_recover_incomplete_metadata_unknown(self):
        (self.job / "launch.json").write_text("not-json")
        with self.assertRaises(b.BridgeError):
            b.recover(str(self.request_file), str(self.jobs), self.config)


if __name__ == "__main__":
    unittest.main()
