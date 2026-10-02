"""One project's invalid configuration never stops the shared supervisor for another."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from orchestrator.bootstrap import bootstrap
from orchestrator.config import ConfigurationError, load_config
from orchestrator.hooks import handle_hook
from orchestrator.project_settings import configure_project, project_settings
from orchestrator.runtime import process_identity, supervise
from orchestrator.store import Store, encode

GRAPH = {
    "summary": "Alpha work",
    "assumptions": [],
    "risks": [],
    "questions": [],
    "nodes": [
        {
            "id": "inspect",
            "title": "Inspect",
            "description": "Inspect the project",
            "depends_on": [],
            "acceptance_criteria": ["Inspected"],
            "kind": "work",
        }
    ],
}


class ProjectConfigurationIsolationTests(unittest.TestCase):
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
        configure_project(self.store, "alpha", {"supervisor": {"heartbeat_seconds": 20}})
        (self.home / "config/local.toml").write_text("[supervisor]\nstale_seconds = 15.0\n")
        self.launched = []
        launcher = Mock()
        launcher.poll.return_value = None

        def launch(command, **_options):
            self.launched.append(command)
            return launcher

        for target, replacement in (
            ("orchestrator.runtime.subprocess.Popen", launch),
            ("orchestrator.runtime._resource_command", lambda *arguments: list(arguments)),
        ):
            patcher = patch(target, side_effect=replacement)
            patcher.start()
            self.addCleanup(patcher.stop)

    def monitor_tasks(self, project_id):
        return [
            task
            for task in self.store.tasks()
            if task["project_id"] == project_id and task["role"] == "monitor"
        ]

    def invalid_events(self, project_id):
        return [
            event
            for event in self.store.events(project_id)
            if event["kind"] == "project.configuration_invalid"
        ]

    def test_invalid_project_does_not_stop_others_and_is_repaired_conversationally(self):
        with self.assertRaises(ConfigurationError):
            load_config(self.home, "alpha")
        plan = self.store.create_plan("alpha", "Alpha work", load_config(self.home))
        with self.store.transaction() as database:
            database.execute(
                "UPDATE plans SET status='approved',graph_json=? WHERE id=?",
                (encode(GRAPH), plan["id"]),
            )
        for name in ("alpha", "beta"):
            self.store.record(name, "user.message", {"prompt": f"{name} needs review"})

        supervise(self.home, once=True)
        supervise(self.home, once=True)

        self.assertEqual(len(self.monitor_tasks("beta")), 1)
        self.assertEqual(self.monitor_tasks("beta")[0]["state"], "starting")
        self.assertEqual(self.monitor_tasks("alpha"), [])
        self.assertEqual(len(self.invalid_events("alpha")), 1)
        self.assertIn(
            "stale_seconds must exceed", self.invalid_events("alpha")[0]["payload"]["error"]
        )
        self.assertIn(
            self.invalid_events("alpha")[0]["id"],
            [update["id"] for update in self.store.updates("alpha")],
        )
        self.assertEqual(self.invalid_events("beta"), [])

        snapshot = project_settings(self.store, "alpha")
        self.assertIn("stale_seconds must exceed", snapshot["error"])
        configure_project(
            self.store,
            "alpha",
            {"supervisor": {"heartbeat_seconds": None}},
            snapshot["revision"],
        )
        supervise(self.home, once=True)

        self.assertEqual(len(self.monitor_tasks("alpha")), 1)
        self.assertEqual(len(self.invalid_events("alpha")), 1)
        self.assertNotIn("error", project_settings(self.store, "alpha"))

    def test_invalid_project_plan_does_not_stop_other_worker_dispatch(self):
        from orchestrator.workers import WorkerService

        (self.home / "config/local.toml").write_text(
            "[supervisor]\nstale_seconds = 15.0\n"
            "[workers]\nenabled = true\n[routing]\nenabled = true\n"
        )
        plan = self.store.create_plan("alpha", "Alpha work", load_config(self.home))
        with self.store.transaction() as database:
            database.execute(
                "UPDATE plans SET status='approved',graph_json=? WHERE id=?",
                (encode(GRAPH), plan["id"]),
            )
        policy = self.home / "beta/.orchestrator/crew-dispatch.json"
        policy.parent.mkdir()
        policy.write_text(
            json.dumps(
                {
                    "classifications": {
                        "review": {
                            "harness": "pi",
                            "provider": "openai",
                            "model": "gpt-5.4",
                            "effort": "high",
                        }
                    }
                }
            )
        )
        service = WorkerService(self.store)
        origin = self.store.record("beta", "user.message", {"text": "Review beta"})
        request = service.request("beta", "Review beta", "read", origin_event_id=origin)
        service.select("beta", request["id"], {"classification": "review", "rationale": "Review"})

        supervise(self.home, once=True)

        workers = [
            task
            for task in self.store.tasks()
            if task["project_id"] == "beta" and task["role"] == "worker"
        ]
        self.assertEqual(len(workers), 1)


class InvalidProjectCoordinatorTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {"NO_MISTAKES_GATE": ""})
        environment.start()
        self.addCleanup(environment.stop)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.home = Path(directory.name)
        self.store = Store(self.home)
        self.store.add_project("project", str(self.home))
        self.store.open_session("writer", "claude", "project")
        live_owner = {"pid": os.getpid(), "identity": process_identity(os.getpid())}
        self.store.set_service_value("native-owner:writer", json.dumps(live_owner))
        for target in (
            "orchestrator.hooks.resolve_claude_session",
            "orchestrator.hooks.verify_claude_owner",
        ):
            owner_patch = patch(target, return_value="writer")
            owner_patch.start()
            self.addCleanup(owner_patch.stop)
        configure_project(self.store, "writer", {"supervisor": {"heartbeat_seconds": 20}})
        (self.home / "config/local.toml").write_text("[supervisor]\nstale_seconds = 15.0\n")

    def hook(self, event, **fields):
        return handle_hook(self.home, event, {"session_id": "native", **fields})

    def decision(self, name, **tool_input):
        output = self.hook("PreToolUse", tool_name=name, tool_input=tool_input, tool_use_id="t")
        return output.get("hookSpecificOutput", {}).get("permissionDecision", "allow")

    def test_coordinator_can_still_converse_and_repair_its_project(self):
        with self.assertRaises(ConfigurationError):
            load_config(self.home, "project")
        self.hook("UserPromptSubmit", prompt="Please repair the heartbeat setting")
        self.assertIn("user.message", [event["kind"] for event in self.store.events("project")])
        self.assertEqual(self.decision("Read", file_path="README.md"), "deny")
        self.assertIn(
            "configure_project",
            self.hook("PreToolUse", tool_name="Bash", tool_input={"command": "true"})[
                "hookSpecificOutput"
            ]["permissionDecisionReason"],
        )
        self.assertEqual(
            self.decision("mcp__orchestrator__configure_project", session_id="writer"), "allow"
        )
        self.assertEqual(
            self.decision("mcp__orchestrator__configure_project", session_id="other"), "deny"
        )
        configure_project(self.store, "writer", {"supervisor": {"heartbeat_seconds": None}})
        self.assertEqual(self.decision("Read", file_path="README.md"), "allow")
        self.assertEqual(self.decision("Bash", command="true"), "deny")
        self.hook("Stop", last_assistant_message="Repaired")

    def test_resumed_frontend_starts_restricted_with_repair_guidance(self):
        (self.home / "config/local.toml").write_text(
            '[supervisor]\nstale_seconds = 15.0\n[execution]\nmode = "trusted"\n'
        )
        self.store.set_service_value("native-owner:writer", "{}")
        initialized = bootstrap(self.home, "claude", "writer", None, continuation=True)
        self.assertEqual(initialized["config"]["execution"]["mode"], "restricted")
        self.assertIn("configuration is invalid", initialized["instructions"])


if __name__ == "__main__":
    unittest.main()
