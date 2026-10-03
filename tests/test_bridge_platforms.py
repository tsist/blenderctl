# SPDX-License-Identifier: GPL-3.0-or-later
"""Host-only Windows/Linux backend discovery and GUI argument regression tests."""
import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
ADDON = ROOT / "tools/material_workflow_addon"
spec = importlib.util.spec_from_file_location("platform_job_bridge", ADDON / "job_bridge.py")
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)


class PlatformDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / "project with spaces ; $(not-a-command)"
        self.installation = self.base / "Blender installation"
        self.cli = self.make_file(self.root / "tools/blenderctl/cli.py")
        self.blender = self.make_file(self.installation / "blender")
        self.version_result = subprocess.CompletedProcess([], 0, "0.51.0\n", "")
        bridge._CONFIGS.clear()

    def tearDown(self):
        self.temp.cleanup()

    def make_file(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
        return path

    def python(self, name="python3.13", version="5.2"):
        return self.make_file(self.installation / version / "python/bin" / name)

    def configure(self, python_binary=None):
        return bridge.configure(self.root, self.blender, python_binary)

    def assert_refused_before_launch(self, expression="specify python_binary"):
        with patch.object(bridge, "_process") as process:
            with self.assertRaisesRegex(bridge.BridgeError, expression):
                self.configure()
            process.assert_not_called()

    def test_linux_versioned_interpreter(self):
        python = self.python()
        with patch.object(bridge, "_process", return_value=self.version_result) as process:
            config = self.configure()
        self.assertEqual(config["python_binary"], str(python))
        self.assertEqual(config["blender_binary"], str(self.blender))
        process.assert_called_once_with([str(python), str(self.cli), "--version"], self.root, 10)

    def test_linux_python3_name(self):
        python = self.python("python3")
        with patch.object(bridge, "_process", return_value=self.version_result):
            self.assertEqual(self.configure()["python_binary"], str(python))

    def test_windows_layout_remains_supported(self):
        self.blender = self.make_file(self.installation / "blender.exe")
        python = self.python("python.exe")
        with patch.object(bridge, "_process", return_value=self.version_result):
            self.assertEqual(self.configure()["python_binary"], str(python))

    def test_aliases_deduplicated_by_resolved_path(self):
        python = self.python()
        (python.parent / "python3").symlink_to(python.name)
        with patch.object(bridge, "_process", return_value=self.version_result):
            self.assertEqual(self.configure()["python_binary"], str(python))

    def test_multiple_distinct_interpreters_refused(self):
        self.python("python3.12")
        self.python("python3.13")
        self.assert_refused_before_launch("found 2 interpreters")

    def test_multiple_blender_versions_refused_not_newest(self):
        self.python(version="4.5")
        self.python(version="5.2")
        self.assert_refused_before_launch("found 2 interpreters")

    def test_only_exact_version_directories_and_interpreter_names(self):
        python = self.python()
        for version in ("5.2-backup", "5.2.1", "5x2", "x5.2", "5.2x"):
            self.python(version=version)
        for name in ("python3.13-config", "python3.13.bak", "python3.13m", "python3x13", "python.exe.bak", "python", "python2.7"):
            self.python(name)
        (python.parent / "python3.11").mkdir()
        with patch.object(bridge, "_process", return_value=self.version_result):
            self.assertEqual(self.configure()["python_binary"], str(python))

    def test_no_bundle_never_falls_back_to_path(self):
        foreign = self.make_file(self.base / "path-interpreter/python3")
        with patch.dict("os.environ", {"PATH": str(foreign.parent)}):
            self.assert_refused_before_launch("found 0 interpreters")

    def test_explicit_python_disambiguates(self):
        self.python("python3.12")
        python = self.python("python3.13")
        with patch.object(bridge, "_process", return_value=self.version_result):
            self.assertEqual(self.configure(python)["python_binary"], str(python))

    def test_explicit_external_python_is_not_silently_replaced(self):
        self.python()
        python = self.make_file(self.base / "chosen Python/python3")
        with patch.object(bridge, "_process", return_value=self.version_result):
            self.assertEqual(self.configure(python)["python_binary"], str(python))

    def test_explicit_relative_missing_and_directory_python_refused(self):
        for python in ("python3", self.base / "missing-python", self.root):
            with self.subTest(python=python), patch.object(bridge, "_process") as process:
                with self.assertRaises(bridge.BridgeError):
                    self.configure(python)
                process.assert_not_called()

    def test_python_symlink_outside_installation_refused(self):
        foreign = self.make_file(self.base / "foreign/python3.13")
        path = self.installation / "5.2/python/bin/python3.13"
        path.parent.mkdir(parents=True)
        path.symlink_to(foreign)
        self.assert_refused_before_launch("executable escapes")

    def test_python_directory_symlink_outside_installation_refused(self):
        foreign = self.make_file(self.base / "foreign/python/bin/python3.13")
        (self.installation / "5.2").symlink_to(foreign.parents[2], target_is_directory=True)
        self.assert_refused_before_launch("directory escapes")

    def test_blender_symlink_uses_real_installation(self):
        python = self.python()
        alias = self.base / "bin/blender"
        alias.parent.mkdir()
        alias.symlink_to(self.blender)
        with patch.object(bridge, "_process", return_value=self.version_result):
            config = bridge.configure(self.root, alias)
        self.assertEqual(config["python_binary"], str(python))
        self.assertEqual(config["blender_binary"], str(self.blender))

    def test_probe_failure_is_not_cached(self):
        self.python()
        failure = subprocess.CompletedProcess([], 1, "", "cannot start")
        with patch.object(bridge, "_process", return_value=failure):
            with self.assertRaisesRegex(bridge.BridgeError, "CLI version check failed"):
                self.configure()
        self.assertEqual(bridge._CONFIGS, {})

    def test_cli_and_blender_must_exist_before_launch(self):
        self.python()
        for path in (self.cli, self.blender):
            with self.subTest(path=path):
                path.unlink()
                self.assert_refused_before_launch("Required file does not exist")
                path.touch()

    def test_process_preserves_argv_without_shell(self):
        python = self.python()
        with patch.object(subprocess, "run", return_value=self.version_result) as run:
            self.configure()
        self.assertEqual(run.call_args.args[0], [str(python), str(self.cli), "--version"])
        self.assertFalse(run.call_args.kwargs["shell"])
        self.assertEqual(run.call_args.kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(run.call_args.kwargs["cwd"], str(self.root))

    def test_job_call_passes_selected_blender_explicitly(self):
        python = self.python()
        jobs = self.root / "jobs with spaces ; $(no)"
        request = self.root / "request with spaces.json"
        with patch.object(bridge, "_process", return_value=self.version_result):
            config = self.configure()
        response = subprocess.CompletedProcess([], 0, '{"ok": true}', "")
        with patch.object(subprocess, "run", return_value=response) as run:
            bridge._call(config, jobs, ["request", str(request)], 123)
        self.assertEqual(run.call_args.args[0], [str(python), str(self.cli), "--blender", str(self.blender),
                                               "--jobs-dir", str(jobs), "--timeout", "123", "request", str(request)])
        self.assertFalse(run.call_args.kwargs["shell"])


class GuiBackendTests(unittest.TestCase):
    def setUp(self):
        self.package = types.ModuleType("platform_test_addon")
        self.package.__path__ = [str(ADDON)]
        self.bpy = types.ModuleType("bpy")
        self.bpy.app = types.SimpleNamespace(binary_path="/actual Blender/blender")
        self.bpy.path = types.SimpleNamespace(abspath=Mock(side_effect=lambda value: "/resolved/" + value))
        self.bpy.types = types.SimpleNamespace(Operator=type("Operator", (), {}), Panel=type("Panel", (), {}))
        props = types.ModuleType("bpy.props")
        props.StringProperty = props.EnumProperty = props.IntProperty = lambda **kwargs: kwargs
        self.backend = types.SimpleNamespace(configure=Mock(return_value={"validated": True}))
        self.package.ui = types.SimpleNamespace(MWOperator=type("MWOperator", (), {}))
        self.package.ui_batch = types.SimpleNamespace()
        self.package.handoff = types.SimpleNamespace()
        self.package.job_bridge = self.backend
        modules = {"bpy": self.bpy, "bpy.props": props, "platform_test_addon": self.package}
        self.module_patch = patch.dict(sys.modules, modules)
        self.module_patch.start()
        self.addCleanup(self.module_patch.stop)
        self.ui = self.load_ui()

    def load_ui(self):
        spec = importlib.util.spec_from_file_location("platform_test_addon.ui_handoff", ADDON / "ui_handoff.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_empty_python_uses_bundled_discovery_and_actual_blender(self):
        scene = types.SimpleNamespace(mw_cli_root="project", mw_cli_python="")
        self.assertEqual(self.ui._backend_config(scene), {"validated": True})
        self.backend.configure.assert_called_once_with("/resolved/project", "/actual Blender/blender", python_binary=None)
        self.bpy.path.abspath.assert_called_once_with("project")

    def test_explicit_gui_python_passed_separately(self):
        scene = types.SimpleNamespace(mw_cli_root="project", mw_cli_python="custom Python/python3.13")
        self.ui._backend_config(scene)
        self.backend.configure.assert_called_once_with("/resolved/project", "/actual Blender/blender",
                                                       python_binary="/resolved/custom Python/python3.13")
        self.assertEqual(self.ui.PROPS["mw_cli_python"]["subtype"], "FILE_PATH")

    def test_handoff_validates_backend_before_creating_snapshot(self):
        scene = types.SimpleNamespace()
        self.package.handoff.prepare = Mock()
        with patch.object(self.ui, "_backend_config", side_effect=bridge.BridgeError("invalid backend")) as configure:
            with self.assertRaisesRegex(bridge.BridgeError, "invalid backend"):
                self.ui.MW_OT_handoff().run(types.SimpleNamespace(scene=scene))
        configure.assert_called_once_with(scene)
        self.package.handoff.prepare.assert_not_called()

    def test_python_override_is_visible_in_panel(self):
        scene = types.SimpleNamespace(mw_handoff_state="", mw_handoff_info="", mw_handoff_candidate="",
                                      mw_handoff_sheet="", mw_last_error="")
        panel = self.ui.MW_PT_handoff()
        panel.layout = Mock()
        panel.draw(types.SimpleNamespace(scene=scene))
        self.assertIn("mw_cli_python", [call.args[1] for call in panel.layout.prop.call_args_list])

    def test_linux_panel_warns_to_archive_temporary_snapshots(self):
        scene = types.SimpleNamespace(mw_handoff_state="", mw_handoff_info="", mw_handoff_candidate="",
                                      mw_handoff_sheet="", mw_last_error="")
        for platform in ("linux", "win32"):
            with self.subTest(platform=platform), patch.object(self.ui.sys, "platform", platform):
                panel = self.ui.MW_PT_handoff()
                panel.layout = Mock()
                panel.draw(types.SimpleNamespace(scene=scene))
                labels = [call.kwargs.get("text", "") for call in panel.layout.label.call_args_list]
            self.assertEqual(any("临时文件系统快照需及时归档" in label for label in labels), platform == "linux")
        self.assertEqual(self.ui.PROPS["mw_handoff_root"]["name"], "快照根目录")

    def test_valid_explicit_environment_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve() / "custom project"
            cli = root / "tools/blenderctl/cli.py"
            cli.parent.mkdir(parents=True)
            cli.touch()
            snapshots = Path(directory).resolve() / "not-created/snapshots"
            with patch.dict("os.environ", {"BLENDERCTL_ROOT": str(root), "BLENDERCTL_SNAPSHOT_ROOT": str(snapshots)}):
                module = self.load_ui()
            self.assertEqual(module.PROPS["mw_cli_root"]["default"], str(root))
            self.assertEqual(module.PROPS["mw_handoff_root"]["default"], str(snapshots))
            self.assertFalse(snapshots.parent.exists())

    def test_relative_environment_defaults_are_ignored(self):
        for value in ("relative/path", "~/project", "$HOME/project", ""):
            with self.subTest(value=value), patch.dict("os.environ", {"BLENDERCTL_ROOT": value, "BLENDERCTL_SNAPSHOT_ROOT": value}):
                module = self.load_ui()
            self.assertEqual(module.PROPS["mw_cli_root"]["default"], str(ROOT))
            self.assertEqual(module.PROPS["mw_handoff_root"]["default"], "")

    def test_backend_environment_requires_existing_project(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            file = root / "file-not-directory"
            file.touch()
            for value in (root, root / "missing", file):
                with self.subTest(value=value), patch.dict("os.environ", {"BLENDERCTL_ROOT": str(value)}):
                    module = self.load_ui()
                self.assertEqual(module.PROPS["mw_cli_root"]["default"], str(ROOT))

    def test_malformed_environment_defaults_are_ignored(self):
        for name in ("BLENDERCTL_ROOT", "BLENDERCTL_SNAPSHOT_ROOT"):
            with self.subTest(name=name), patch.object(self.ui.os.environ, "get", return_value="/invalid\x00path"):
                self.assertEqual(self.ui._environment_default(name, project=name == "BLENDERCTL_ROOT"), "")

    def test_filesystem_error_in_environment_default_is_ignored(self):
        with patch.dict("os.environ", {"BLENDERCTL_ROOT": "/unreadable-project"}), patch.object(Path, "resolve", side_effect=OSError("unreadable")):
            self.assertEqual(self.ui._environment_default("BLENDERCTL_ROOT", project=True), "")


if __name__ == "__main__":
    unittest.main()
