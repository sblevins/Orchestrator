import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestrator.api import request
from orchestrator.mcp import MCPServer
from orchestrator.store import StateError, Store


class APITests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.home = Path(self.directory.name)
        self.store = Store(self.home)
        self.store.open_session("instance", "test")
        (self.home / "project").mkdir()

    def bind(self):
        request(
            self.home,
            "instance",
            "register_project",
            {"project_id": "alpha", "root": str(self.home / "project")},
        )
        with patch("orchestrator.api._start_service", return_value={"running": True}):
            return request(self.home, "instance", "bind_project", {"project_id": "alpha"})

    def test_unbound_prompts_saved_and_binding_fixed(self):
        result = request(
            self.home, "instance", "record_prompt", {"prompt": "Which project shall we use?"}
        )
        self.assertEqual(
            json.loads(Path(result["path"]).read_text())["prompt"], "Which project shall we use?"
        )
        self.bind()
        result = request(self.home, "instance", "record_prompt", {"prompt": "What's the status?"})
        self.assertFalse(result["review_required"])
        request(self.home, "instance", "record_prompt", {"prompt": "Change the goal"})
        self.assertTrue(self.store.monitor_candidate("alpha"))
        with self.assertRaises(StateError):
            request(
                self.home, "instance", "bind_project", {"project_id": "alpha", "takeover": True}
            )

    def test_binding_mirrors_earlier_prompts_once(self):
        request(self.home, "instance", "record_prompt", {"prompt": "Plan a new database for alpha"})
        self.bind()
        with patch("orchestrator.api._start_service", return_value={"running": True}):
            request(self.home, "instance", "bind_project", {"project_id": "alpha"})
        messages = [
            event for event in self.store.events("alpha") if event["kind"] == "user.message"
        ]
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["payload"]["prompt"], "Plan a new database for alpha")
        self.assertTrue(messages[0]["review_required"])

    def test_long_prompt_preserves_full_original(self):
        self.bind()
        prompt = "x" * 50000 + " important change"
        result = request(self.home, "instance", "record_prompt", {"prompt": prompt})
        self.assertEqual(json.loads(Path(result["path"]).read_text())["prompt"], prompt)
        event = self.store.events("alpha")[-1]
        self.assertTrue(event["payload"]["truncated"])
        self.assertTrue(event["review_required"])

    def test_scope_change_invalidates_reviewed_plan(self):
        self.bind()
        plan = self.store.create_plan("instance", "test", {})
        with self.store.transaction() as database:
            database.execute("UPDATE plans SET status='reviewed' WHERE id=?", (plan["id"],))
        request(
            self.home,
            "instance",
            "record_decision",
            {"summary": "Scope changed", "scope_change": True},
        )
        self.assertEqual(self.store.plan(plan["id"])["status"], "needs_revision")

    def test_cross_project_read_and_mutation_denied(self):
        self.bind()
        other_path = self.home / "other"
        other_path.mkdir()
        self.store.add_project("beta", str(other_path))
        self.store.open_session("other", "test", "beta")
        task = self.store.enqueue("beta", "other", "planner", "other", {})
        for action in ("task", "cancel_task"):
            with self.assertRaises(StateError):
                request(self.home, "instance", action, {"task_id": task["id"]})
        with self.assertRaises(StateError):
            request(self.home, "instance", "spawn_worker", {"model": "anything"})

    def test_mcp_initialization_schema_and_result(self):
        server = MCPServer(self.home, "instance")
        early = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        self.assertIn("error", early)
        initialized = server.handle(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "initialize",
                "params": {"protocolVersion": "2025-06-18"},
            }
        )
        self.assertEqual(initialized["result"]["protocolVersion"], "2025-06-18")
        tools = server.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/list"})["result"][
            "tools"
        ]
        self.assertNotIn("session_id", tools[0]["inputSchema"]["properties"])
        self.assertNotIn("approve", [tool["name"] for tool in tools])
        result = server.handle(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "projects", "arguments": {}},
            }
        )
        self.assertFalse(result["result"]["isError"])
        forgery = server.handle(
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/call",
                "params": {"name": "projects", "arguments": {"session_id": "other"}},
            }
        )
        self.assertIn("error", forgery)

    def test_mcp_duplicate_keys_and_parse_errors(self):
        output = io.StringIO()
        server = MCPServer(self.home, "instance", output=output)
        server.serve(io.StringIO('{"jsonrpc":"2.0","id":1,"id":2,"method":"ping"}\nnot-json\n'))
        records = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual([record["error"]["code"] for record in records], [-32700, -32700])

    def test_cli_mcp_real_subprocess(self):
        code_root = Path(__file__).resolve().parents[1]
        messages = [
            {
                "jsonrpc": "2.0",
                "id": "init",
                "method": "initialize",
                "params": {"protocolVersion": "2025-11-25"},
            },
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": "list", "method": "tools/list"},
            {
                "jsonrpc": "2.0",
                "id": "call",
                "method": "tools/call",
                "params": {"name": "projects", "arguments": {}},
            },
        ]
        result = subprocess.run(
            [
                sys.executable,
                str(code_root / "bin/orchestrator"),
                "--home",
                str(self.home),
                "mcp",
                "--session",
                "instance",
            ],
            input="".join(json.dumps(message) + "\n" for message in messages),
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        replies = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual([reply["id"] for reply in replies], ["init", "list", "call"])
        self.assertFalse(replies[-1]["result"]["isError"])

    def test_cli_watch_does_not_consume_notification(self):
        self.bind()
        task = self.store.enqueue("alpha", "instance", "planner", "read", {})
        claimed = self.store.claim_next(1)
        self.store.finish(task["id"], claimed["token"], text="done")
        code_root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            [
                sys.executable,
                str(code_root / "bin/orchestrator"),
                "--home",
                str(self.home),
                "watch",
                "--session",
                "instance",
                "--seconds",
                "0.1",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("saved feedback", result.stderr)
        self.assertEqual(len(self.store.updates("instance")), 1)


if __name__ == "__main__":
    unittest.main()
