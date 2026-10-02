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

    def test_unknown_nested_settings_are_rejected_with_spelling_guidance(self):
        for settings, guidance in (
            ({"execution": {"unatended": True}}, "did you mean execution.unattended"),
            ({"exection": {"unattended": True}}, "did you mean execution"),
            ({"monitoring": {"quiet_secs": 5}}, "did you mean monitoring.quiet_seconds"),
            ({"roles": {"planer": {"effort": "low"}}}, "did you mean roles.planner"),
            ({"roles": {"planner": {"efort": "low"}}}, "did you mean roles.planner.effort"),
            ({"execution": {"mode": {"trusted": True}}}, "execution.mode has no nested"),
        ):
            with (
                self.subTest(settings=settings),
                self.assertRaisesRegex(ConfigurationError, guidance),
            ):
                configure_project(self.store, "alpha", settings)
        self.assertFalse(self.path.exists())
        result = configure_project(
            self.store,
            "alpha",
            {
                "roles": {
                    "planner": {
                        "adapter": "pi",
                        "provider": "openai-codex",
                        "model": "gpt-6-astra",
                    }
                },
                "monitoring": {"quiet_seconds": 5},
                "commands": {"tool_paths": {"forge": "/usr/local/bin/forge"}},
                "supervisor": {"task_timeout_seconds": 120, "task_memory": "4G"},
            },
        )
        settings = result["settings"]
        self.assertEqual(settings["roles"]["planner"]["provider"], "openai-codex")
        self.assertEqual(settings["monitoring"]["quiet_seconds"], 5)
        self.assertEqual(settings["commands"]["tool_paths"], {"forge": "/usr/local/bin/forge"})
        self.assertEqual(settings["supervisor"]["task_timeout_seconds"], 120)
        self.assertEqual(load_config(self.home, "beta")["roles"]["planner"]["adapter"], "claude")

    def test_process_wide_supervisor_settings_point_to_project_settings(self):
        baseline = load_config(self.home)
        with self.assertRaisesRegex(ConfigurationError, "use execution.max_parallel"):
            configure_project(self.store, "alpha", {"supervisor": {"max_parallel": 8}})
        with self.assertRaisesRegex(ConfigurationError, "config/local.toml"):
            configure_project(self.store, "alpha", {"supervisor": {"poll_seconds": 0.5}})
        self.assertFalse(self.path.exists())
        self.assertEqual(load_config(self.home), baseline)
        result = configure_project(self.store, "alpha", {"execution": {"max_parallel": 1}})
        self.assertEqual(result["settings"]["execution"]["max_parallel"], 1)

    def test_null_removes_only_this_project_override(self):
        self.path.parent.mkdir(parents=True)
        (self.home / "config/local.toml").write_text('[execution]\nworker_difficulty = "easy"\n')
        self.path.write_text(
            json.dumps({"execution": {"unatended": True}, "supervisor": {"max_parallel": 8}})
        )
        configure_project(
            self.store,
            "alpha",
            {"execution": {"worker_difficulty": "very-hard", "unattended": True}},
        )
        configure_project(self.store, "beta", {"execution": {"unattended": True}})
        result = configure_project(
            self.store,
            "alpha",
            {
                "execution": {"worker_difficulty": None, "unattended": None, "unatended": None},
                "supervisor": {"max_parallel": None},
            },
        )
        self.assertTrue(result["changed"])
        self.assertEqual(result["settings"]["execution"]["worker_difficulty"], "easy")
        self.assertFalse(result["settings"]["execution"]["unattended"])
        self.assertEqual(json.loads(self.path.read_text()), {})
        self.assertTrue(load_config(self.home, "beta")["execution"]["unattended"])
        self.assertEqual(load_config(self.home)["execution"]["worker_difficulty"], "easy")
        unchanged = configure_project(self.store, "alpha", {"execution": {"unattended": None}})
        self.assertFalse(unchanged["changed"])
        self.assertEqual(unchanged["revision"], result["revision"])
        with self.assertRaisesRegex(ConfigurationError, "did you mean execution.unattended"):
            configure_project(self.store, "alpha", {"execution": {"unatended": None}})

    def test_invalid_project_is_reported_and_repaired_from_its_unvalidated_layers(self):
        configure_project(self.store, "alpha", {"supervisor": {"heartbeat_seconds": 20}})
        (self.home / "config/local.toml").write_text("[supervisor]\nstale_seconds = 15.0\n")
        with self.assertRaisesRegex(ConfigurationError, "stale_seconds must exceed"):
            load_config(self.home, "alpha")
        self.assertEqual(load_config(self.home, "beta")["supervisor"]["stale_seconds"], 15.0)
        snapshot = project_settings(self.store, "alpha")
        self.assertIn("stale_seconds must exceed", snapshot["error"])
        self.assertEqual(snapshot["settings"]["supervisor"]["heartbeat_seconds"], 20)
        with self.assertRaisesRegex(ConfigurationError, "stale_seconds must exceed"):
            configure_project(self.store, "alpha", {"workers": {"enabled": False}})
        repaired = configure_project(
            self.store,
            "alpha",
            {"supervisor": {"heartbeat_seconds": None}},
            snapshot["revision"],
        )
        self.assertEqual(repaired["settings"]["supervisor"]["heartbeat_seconds"], 5.0)
        self.assertEqual(load_config(self.home, "alpha"), repaired["settings"])
        self.assertNotIn("error", project_settings(self.store, "alpha"))

    def test_image_model_with_control_character_is_not_saved(self):
        with self.assertRaisesRegex(ConfigurationError, "images.model"):
            configure_project(self.store, "alpha", {"images": {"model": "gpt\x7fimage"}})
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
