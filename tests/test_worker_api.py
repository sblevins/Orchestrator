import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestrator.api import request, tool_definitions
from orchestrator.cli import main
from orchestrator.mcp import MCPServer, ProtocolError
from orchestrator.store import StateError, Store


class WorkerAPITests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.home = Path(self.directory.name)
        self.store = Store(self.home)
        for project_id in ("alpha", "beta"):
            root = self.home / project_id
            root.mkdir()
            self.store.add_project(project_id, str(root))
            self.store.open_session(project_id, "test", project_id)
        self.store.open_session("observer", "test", "alpha", observer=True)
        self.store.open_session("unbound", "test")
        self.origin = request(self.home, "alpha", "record_prompt", {"prompt": "Inspect files"})[
            "event_id"
        ]
        self.service = patch("orchestrator.api._start_service", return_value={"running": True})
        self.start_service = self.service.start()
        self.addCleanup(self.service.stop)

    def create_worker(self, session="alpha", **extra):
        origin = (
            self.origin
            if session == "alpha"
            else request(self.home, session, "record_prompt", {"prompt": "Inspect other files"})[
                "event_id"
            ]
        )
        return request(
            self.home,
            session,
            "request_worker",
            {
                "brief": "Inspect files",
                "origin_event_id": origin,
                **extra,
            },
        )

    def cli(self, *arguments):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            status = main(["--home", str(self.home), *arguments])
        return status, json.loads(output.getvalue())

    def test_request_get_list_and_missing_policy(self):
        result = self.create_worker()
        self.assertEqual(result["mode"], "read")
        self.assertEqual(result["origin_event_id"], self.origin)
        self.assertIsNone(result["profile"])
        self.assertEqual(
            request(self.home, "alpha", "worker", {"request_id": result["id"]})["id"], result["id"]
        )
        self.assertEqual(len(request(self.home, "alpha", "workers")["workers"]), 1)
        self.assertFalse(request(self.home, "alpha", "routing_policy")["available"])
        self.start_service.assert_called()

    def test_access_checks_precede_service_construction(self):
        other = self.create_worker("beta")
        with patch("orchestrator.workers.WorkerService") as workers:
            for action in ("worker", "select_worker", "refresh_worker_policy"):
                payload = {"request_id": other["id"]}
                if action == "select_worker":
                    payload["choice"] = {}
                with self.assertRaises(StateError):
                    request(self.home, "alpha", action, payload)
            for session in ("observer", "unbound"):
                with self.assertRaises(StateError):
                    request(
                        self.home,
                        session,
                        "request_worker",
                        {
                            "brief": "Inspect",
                            "origin_event_id": self.origin,
                        },
                    )
            workers.assert_not_called()

    def test_cross_project_origin_is_rejected_before_service(self):
        origin = request(self.home, "beta", "record_prompt", {"prompt": "Other work"})["event_id"]
        with patch("orchestrator.workers.WorkerService") as workers:
            with self.assertRaises(StateError):
                self.create_worker(origin_event_id=origin)
            workers.assert_not_called()

    def test_request_requires_integer_origin_and_valid_mode(self):
        for extra in (
            {"origin_event_id": True},
            {"origin_event_id": "1"},
            {"mode": "execute"},
            {"role": "monitor"},
            {"plan_id": "some-plan"},
        ):
            with self.subTest(extra=extra), self.assertRaises(StateError):
                self.create_worker(**extra)
        with self.assertRaises(StateError):
            request(self.home, "alpha", "request_worker", {"brief": "Missing origin"})

    def test_observer_can_inspect_but_inactive_cannot(self):
        self.assertIn("workers", request(self.home, "observer", "workers"))
        self.store.close_session("observer")
        with self.assertRaises(StateError):
            request(self.home, "observer", "routing_policy")

    def test_model_tools_offer_project_approvals_and_preserve_choice(self):
        tools = {tool["name"]: tool for tool in tool_definitions(False)}
        for name in ("approve_worker", "accept_worker", "approve_node", "configure_project"):
            self.assertIn(name, tools)
        self.assertNotIn("override_worker", tools)
        self.assertEqual(
            tools["request_worker"]["inputSchema"]["properties"]["origin_event_id"]["type"],
            "integer",
        )
        self.assertEqual(
            tools["select_worker"]["inputSchema"]["properties"]["choice"]["type"], "object"
        )
        server = MCPServer(self.home, "alpha")
        choice = {
            "rule": 0,
            "candidate": 1,
            "model": "explicit",
            "effort": "high",
            "rationale": "Reviewed policy",
        }
        self.assertEqual(
            server._validate_arguments("select_worker", {"request_id": "id", "choice": choice})[1][
                "choice"
            ],
            choice,
        )
        with self.assertRaises(ProtocolError):
            server._validate_arguments(
                "request_worker", {"brief": "Inspect", "origin_event_id": True}
            )
        with self.assertRaises(ProtocolError):
            server._validate_arguments("select_worker", {"request_id": "id", "choice": []})

    def test_selection_starts_supervisor_and_returns_service_result(self):
        result = self.create_worker()
        with patch(
            "orchestrator.workers.WorkerService.select", return_value={"id": result["id"]}
        ) as select:
            choice = {
                "rule": "default",
                "model": "explicit",
                "effort": "high",
                "rationale": "Reason",
            }
            self.start_service.reset_mock()
            self.assertEqual(
                request(
                    self.home,
                    "alpha",
                    "select_worker",
                    {"request_id": result["id"], "choice": choice},
                ),
                {"id": result["id"]},
            )
            select.assert_called_once_with("alpha", result["id"], choice)
            self.start_service.assert_called_once()

    def test_cli_init_exclusive_and_no_defaults_real_process(self):
        command = [
            sys.executable,
            "-m",
            "orchestrator.cli",
            "--home",
            str(self.home),
            "routing",
            "init",
            "--project",
            "alpha",
        ]
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        policy = self.home / "alpha" / ".orchestrator" / "crew-dispatch.json"
        self.assertEqual(json.loads(policy.read_text()), {"rules": []})
        self.assertFalse(json.loads(result.stdout)["routable"])
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(policy.read_text()), {"rules": []})

    def test_cli_validates_schema_without_an_executable_profile(self):
        policy = self.home / "alpha" / ".orchestrator" / "crew-dispatch.json"
        policy.parent.mkdir()
        document = {
            "rules": [
                {
                    "when": "Inspect files",
                    "use": {
                        "harness": "unimplemented",
                        "model": "explicit",
                        "effort": "high",
                    },
                }
            ]
        }
        policy.write_text(json.dumps(document))
        status, result = self.cli("routing", "validate", "--project", "alpha")
        self.assertEqual(status, 0, result)
        self.assertTrue(result["valid"])
        self.assertFalse(result["routable"])
        self.assertTrue(result["blockers"])
        self.assertEqual(self.cli("routing", "show", "--project", "alpha")[1]["policy"], document)
        policy.write_text('{"rules": "invalid"}')
        self.assertEqual(self.cli("routing", "validate", "--project", "alpha")[0], 1)

    def test_cli_policy_fifo_cannot_hang_supervisor(self):
        policy = self.home / "alpha" / ".orchestrator" / "crew-dispatch.json"
        policy.parent.mkdir()
        os.mkfifo(policy)
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "orchestrator.cli",
                "--home",
                str(self.home),
                "routing",
                "validate",
                "--project",
                "alpha",
            ],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("regular file", result.stdout)

    def test_cli_init_rejects_symlink_directory_and_file(self):
        target = self.home / "target"
        target.mkdir()
        directory = self.home / "alpha" / ".orchestrator"
        directory.symlink_to(target, target_is_directory=True)
        self.assertEqual(self.cli("routing", "init", "--project", "alpha")[0], 1)
        self.assertEqual(list(target.iterdir()), [])
        directory.unlink()
        directory.mkdir()
        (directory / "crew-dispatch.json").symlink_to(target / "missing")
        self.assertEqual(self.cli("routing", "init", "--project", "alpha")[0], 1)
        self.assertFalse((target / "missing").exists())

    def test_cli_operator_actions_start_supervisor(self):
        for command, method, arguments in (
            ("approve-worker", "approve", ["request", "--reason", "Reviewed"]),
            ("accept-worker", "accept", ["request", "--reason", "Verified"]),
            ("approve-node", "approve_node", ["plan", "node", "--reason", "Reviewed"]),
            ("override-worker", "override", ["request", "--choice", '{"rationale":"Reviewed"}']),
        ):
            with (
                self.subTest(command=command),
                patch(
                    f"orchestrator.workers.WorkerService.{method}", return_value={"ok": True}
                ) as action,
                patch(
                    "orchestrator.runtime.ensure_supervisor", return_value={"running": True}
                ) as supervisor,
            ):
                status, result = self.cli(command, *arguments)
                self.assertEqual(status, 0, result)
                action.assert_called_once()
                supervisor.assert_called_once_with(self.home)

    def test_override_private_file_permissions_and_symlinks(self):
        choice = self.home / "choice.json"
        choice.write_text('{"rationale":"Reviewed"}')
        choice.chmod(0o644)
        with (
            patch("orchestrator.workers.WorkerService.override", return_value={}) as override,
            patch("orchestrator.runtime.ensure_supervisor", return_value={}),
        ):
            self.assertEqual(
                self.cli("override-worker", "request", "--choice-file", str(choice))[0], 1
            )
            override.assert_not_called()
            choice.chmod(0o600)
            self.assertEqual(
                self.cli("override-worker", "request", "--choice-file", str(choice))[0], 0
            )
            link = self.home / "link.json"
            link.symlink_to(choice)
            self.assertEqual(
                self.cli("override-worker", "request", "--choice-file", str(link))[0], 1
            )


if __name__ == "__main__":
    unittest.main()
