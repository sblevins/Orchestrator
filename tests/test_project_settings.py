"""Project settings never mutate another project's settings or shared defaults."""

import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from orchestrator.config import ConfigurationError, load_config
from orchestrator.project_settings import configure_project, project_settings
from orchestrator.store import StateError, Store


class ProjectSettingsTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.home = Path(directory.name)
        self.store = Store(self.home)
        for name in ("alpha", "beta"):
            root = self.home / name
            root.mkdir()
            self.store.add_project(name, str(root))
            self.store.open_session(name, "claude", name)
        self.store.open_session("observer", "claude", "alpha", observer=True)
        self.store.open_session("unbound", "claude")
        self.path = self.home / "config/projects/alpha.json"

    def test_patch_is_private_and_layers_after_toml(self):
        baseline = load_config(self.home)
        beta = load_config(self.home, "beta")
        self.path.parent.mkdir(parents=True)
        toml = self.path.with_suffix(".toml")
        toml.write_text('[personalization]\nname="TOML"\n')
        result = configure_project(self.store, "alpha", {"personalization": {"name": "Alpha"}})
        self.assertEqual(result["settings"]["personalization"]["name"], "Alpha")
        self.assertEqual(load_config(self.home, "alpha"), result["settings"])
        self.assertEqual(load_config(self.home, "beta"), beta)
        self.assertEqual(load_config(self.home), baseline)
        self.assertEqual(toml.read_text(), '[personalization]\nname="TOML"\n')
        self.assertEqual(json.loads(self.path.read_text()), {"personalization": {"name": "Alpha"}})
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertIn("NEW tasks", result["message"])
        self.assertIn("/model", result["message"])

    def test_partial_edits_preserve_previous_settings_and_enforce_supplied_revision(self):
        first = configure_project(self.store, "alpha", {"execution": {"max_parallel": 2}})
        second = configure_project(
            self.store, "alpha", {"workers": {"enabled": False}}, first["revision"]
        )
        self.assertEqual(second["settings"]["execution"]["max_parallel"], 2)
        self.assertFalse(second["settings"]["workers"]["enabled"])
        with self.assertRaisesRegex(StateError, "changed"):
            configure_project(
                self.store, "alpha", {"workers": {"enabled": True}}, first["revision"]
            )
        self.assertEqual(project_settings(self.store, "alpha")["revision"], second["revision"])

    def test_permissions_are_optional_boolean_and_project_local(self):
        baseline = load_config(self.home)
        permissions = {
            "coordinator_approvals": False,
            "require_write_approval": True,
            "enforce_monitor_holds": False,
        }
        result = configure_project(self.store, "alpha", {"permissions": permissions})
        self.assertEqual(result["settings"]["permissions"], permissions)
        self.assertEqual(load_config(self.home), baseline)
        for invalid in ({"unknown": True}, {"coordinator_approvals": 1}, []):
            with self.subTest(invalid=invalid), self.assertRaises(ConfigurationError):
                configure_project(self.store, "alpha", {"permissions": invalid})

    def test_private_workflow_templates_validate_and_replace_node_lists(self):
        template = {
            "summary": "Review",
            "assumptions": [],
            "risks": [],
            "questions": [],
            "nodes": [
                {
                    "id": "read",
                    "title": "Read",
                    "description": "Inspect",
                    "depends_on": [],
                    "acceptance_criteria": ["Read"],
                    "kind": "work",
                }
            ],
        }
        configure_project(
            self.store,
            "alpha",
            {"planning": {"templates": {"custom": template}, "workflow": "custom"}},
        )
        replacement = [{**template["nodes"][0], "id": "inspect"}]
        result = configure_project(
            self.store, "alpha", {"planning": {"templates": {"custom": {"nodes": replacement}}}}
        )
        self.assertEqual(
            result["settings"]["planning"]["templates"]["custom"]["nodes"], replacement
        )
        self.assertNotIn("custom", load_config(self.home, "beta")["planning"].get("templates", {}))
        for templates in (
            {"../bad": template},
            {"bad": {}},
            {str(index): template for index in range(65)},
        ):
            with self.assertRaisesRegex(ConfigurationError, "planning.templates"):
                configure_project(self.store, "alpha", {"planning": {"templates": templates}})

    def test_observer_unbound_inactive_rejected(self):
        self.assertFalse(project_settings(self.store, "observer")["can_configure"])
        for session in ("observer", "unbound"):
            with self.assertRaises(StateError):
                configure_project(self.store, session, {})
        self.store.close_session("alpha")
        with self.assertRaises(StateError):
            configure_project(self.store, "alpha", {})

    def test_symlink_directories_targets_and_fifo_are_rejected(self):
        target = self.home / "shared"
        target.mkdir()
        (self.home / "config").symlink_to(target, target_is_directory=True)
        with self.assertRaises((OSError, ValueError)):
            configure_project(self.store, "alpha", {})
        self.assertEqual(list(target.iterdir()), [])
        (self.home / "config").unlink()
        self.path.parent.mkdir(parents=True)
        shared = target / "file"
        shared.write_text("{}")
        self.path.symlink_to(shared)
        with self.assertRaises((OSError, ValueError)):
            configure_project(self.store, "alpha", {})
        self.assertEqual(shared.read_text(), "{}")
        self.path.unlink()
        os.mkfifo(self.path)
        with self.assertRaises((OSError, ValueError)):
            configure_project(self.store, "alpha", {})

    def test_hardlink_replacement_preserves_shared_file(self):
        self.path.parent.mkdir(parents=True)
        shared = self.home / "shared.json"
        shared.write_text("{}")
        os.link(shared, self.path)
        configure_project(self.store, "alpha", {"workers": {"enabled": False}})
        self.assertEqual(shared.read_text(), "{}")
        self.assertNotEqual(shared.stat().st_ino, self.path.stat().st_ino)

    def test_no_executable_privilege_expansion_or_unknown_sections(self):
        for settings in (
            {"adapters": {"claude": {"command": ["/tmp/unsafe"]}}},
            {"frontends": {"claude": {"command": ["/tmp/unsafe"]}}},
            {"roles": {"planner": {"allowed_tools": ["Bash"]}}},
            {"project_id": "beta"},
            {"execution": {"max_parallel": 0}},
        ):
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                configure_project(self.store, "alpha", settings)
        self.assertFalse(self.path.exists())

    def test_concurrent_partial_patches_do_not_lose_updates(self):
        patches = [{"workers": {"enabled": False}}, {"execution": {"max_parallel": 2}}]
        with ThreadPoolExecutor(max_workers=2) as executor:
            list(
                executor.map(
                    lambda settings: configure_project(self.store, "alpha", settings), patches
                )
            )
        settings = project_settings(self.store, "alpha")["settings"]
        self.assertFalse(settings["workers"]["enabled"])
        self.assertEqual(settings["execution"]["max_parallel"], 2)

    def test_bounded_payload_and_invalid_bound_id(self):
        with self.assertRaisesRegex(StateError, "1 MiB"):
            configure_project(
                self.store, "alpha", {"personalization": {"name": "a" * (1024 * 1024)}}
            )
        with self.assertRaises(ConfigurationError):
            load_config(self.home, "../beta")
        self.assertFalse(self.path.exists())


if __name__ == "__main__":
    unittest.main()
