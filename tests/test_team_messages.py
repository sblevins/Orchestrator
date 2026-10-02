"""Offline end-to-end team messaging with real dispatch and active task grants."""

import contextlib
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestrator import team_mcp, team_messages
from orchestrator.store import StateError, Store
from orchestrator.workers import WorkerService

PROFILE = {"harness": "pi", "provider": "openai", "model": "gpt-5.4", "effort": "high"}


class TeamMessageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        (self.home / "config").mkdir()
        (self.home / "config/local.toml").write_text(
            "[workers]\nenabled = true\n[routing]\nenabled = true\n[execution]\nmax_parallel = 8\n"
        )
        self.store = Store(self.home)
        # Also allows running these tests before the parent's additive migration lands.
        with contextlib.closing(self.store.connect()) as database:
            database.executescript(team_messages.SCHEMA)
        self.service = WorkerService(self.store)
        self.tasks, self.parent = self.create_team("project")

    def create_team(self, project):
        root = self.home / project
        (root / ".orchestrator").mkdir(parents=True)
        (root / ".orchestrator/crew-dispatch.json").write_text(
            json.dumps(
                {
                    "classifications": {
                        "audit": {
                            "team": [
                                {**PROFILE, "model": model}
                                for model in ("gpt-5.4", "gpt-5.3-codex", "gpt-5.2")
                            ]
                        }
                    }
                }
            )
        )
        self.store.add_project(project, str(root))
        session = project + "-session"
        self.store.open_session(session, "test", project)
        origin = self.store.record(session, "user.message", {"text": "Audit source"})
        request = self.service.request(session, "Audit source", "read", origin_event_id=origin)
        parent = self.service.select(
            session, request["id"], {"classification": "audit", "rationale": "Independent peers"}
        )
        with patch("orchestrator.worker_execution._git", return_value="a" * 40):
            self.service.dispatch_ready()
        self.service.dispatch_ready()
        tasks = [self.store.claim_next(8) for _ in range(3)]
        self.assertTrue(all(tasks))
        return tasks, parent

    def call(self, peer, name, **arguments):
        task = self.tasks[peer]
        return team_messages.execute(self.home, task["id"], task["token"], name, arguments)

    def test_exchange_restart_cursor_privacy_and_idempotency(self):
        first = self.call(
            0, "send_team_message", message="Shared finding", idempotency_key="finding"
        )
        again = self.call(
            0, "send_team_message", message="Shared finding", idempotency_key="finding"
        )
        self.assertEqual(first, again)
        private = self.call(1, "send_team_message", message="Private evidence", recipient=0)
        self.assertEqual(first["sender_peer"], 0)
        self.assertEqual(len(first["peers"]), 3)
        self.assertIn("untrusted", first["instructions"])
        self.store = Store(self.home)
        visible = self.call(0, "read_team_messages", limit=1)
        self.assertEqual(visible["messages"], [first["message"]])
        remaining = self.call(0, "read_team_messages", after=visible["cursor"])
        self.assertEqual(remaining["messages"], [private["message"]])
        self.assertEqual(self.call(2, "read_team_messages")["messages"], [first["message"]])
        self.assertEqual(len(self.call(1, "read_team_messages")["messages"]), 2)
        self.assertEqual(
            self.call(0, "read_team_messages", after=remaining["cursor"])["messages"], []
        )
        with self.assertRaisesRegex(StateError, "different"):
            self.call(0, "send_team_message", message="Changed", idempotency_key="finding")
        events = [
            event
            for event in self.store.events("project")
            if event["kind"] == "worker.team_message"
        ]
        self.assertEqual(len(events), 2)
        self.assertNotIn("Shared finding", json.dumps(events))
        with contextlib.closing(self.store.connect()) as database:
            self.assertEqual(
                database.execute(
                    "SELECT COUNT(*) FROM events WHERE kind='worker.team_message' AND review_required=1"
                ).fetchone()[0],
                0,
            )
            with self.assertRaises(sqlite3.IntegrityError):
                database.execute("UPDATE worker_team_messages SET body='replacement'")

    def test_cross_project_and_identity_overrides(self):
        self.call(0, "send_team_message", message="project secret")
        other_tasks, _ = self.create_team("other")
        other = other_tasks[0]
        result = team_messages.execute(
            self.home, other["id"], other["token"], "read_team_messages", {}
        )
        self.assertEqual(result["messages"], [])
        with self.assertRaises(StateError):
            team_messages.execute(
                self.home, other["id"], self.tasks[0]["token"], "read_team_messages", {}
            )
        for field in ("project_id", "home", "task_id", "token", "sender_peer", "parent_request_id"):
            with self.subTest(field=field), self.assertRaisesRegex(StateError, "Unknown fields"):
                self.call(0, "read_team_messages", **{field: "override"})

    def test_revoked_attempts(self):
        mutations = [
            ("UPDATE tasks SET token='replacement' WHERE id=?", self.tasks[0]["id"]),
            ("UPDATE tasks SET cancel_requested=1 WHERE id=?", self.tasks[0]["id"]),
            ("UPDATE tasks SET state='succeeded' WHERE id=?", self.tasks[0]["id"]),
            ("UPDATE worker_requests SET state='cancelled' WHERE id=?", self.parent["id"]),
            ("UPDATE worker_requests SET generation=generation+1 WHERE id=?", self.parent["id"]),
            (
                "UPDATE worker_groups SET state='candidate' WHERE parent_request_id=?",
                self.parent["id"],
            ),
        ]
        for statement, identifier in mutations:
            with self.subTest(statement=statement):
                with self.store.transaction() as database:
                    database.execute(statement, (identifier,))
                for tool in ("read_team_messages", "send_team_message"):
                    with self.assertRaises(StateError):
                        self.call(
                            0, tool, **({"message": "denied"} if tool.startswith("send") else {})
                        )
                with self.store.transaction() as database:
                    database.execute(
                        "UPDATE tasks SET token=?,cancel_requested=0,state='starting' WHERE id=?",
                        (self.tasks[0]["token"], self.tasks[0]["id"]),
                    )
                    database.execute(
                        "UPDATE worker_requests SET state='group_running',generation=? WHERE id=?",
                        (self.parent["generation"], self.parent["id"]),
                    )
                    database.execute(
                        "UPDATE worker_groups SET state='round1' WHERE parent_request_id=?",
                        (self.parent["id"],),
                    )

    def test_running_workers_and_monitor_hold(self):
        for task in self.tasks:
            self.assertTrue(
                self.store.runner_started(task["id"], task["token"], 12345, "test-runner")
            )
        self.call(0, "send_team_message", message="Running peer finding")
        self.assertEqual(len(self.call(1, "read_team_messages")["messages"]), 1)
        with self.store.transaction() as database:
            database.execute(
                "INSERT INTO holds(id,project_id,source_task,detail,created) VALUES('hold','project',?,'Review required',0)",
                (self.tasks[0]["id"],),
            )
        for name, arguments in (
            ("read_team_messages", {}),
            ("send_team_message", {"message": "denied"}),
        ):
            with self.assertRaisesRegex(StateError, "holds"):
                self.call(0, name, **arguments)

    def test_validation_and_budget(self):
        for arguments in (
            {"message": ""},
            {"message": "a" * 16001},
            {"message": "a", "recipient": True},
            {"message": "a", "recipient": 3},
            {"message": "a", "idempotency_key": "../bad"},
        ):
            with self.subTest(arguments=str(arguments)[:80]), self.assertRaises(StateError):
                self.call(0, "send_team_message", **arguments)
        for arguments in ({"after": -1}, {"limit": 51}, {"after": True}):
            with self.assertRaises(StateError):
                self.call(0, "read_team_messages", **arguments)
        self.call(0, "send_team_message", message="one", idempotency_key="one")
        with patch.object(team_messages, "MAX_MESSAGES", 1):
            self.call(0, "send_team_message", message="one", idempotency_key="one")
            with self.assertRaisesRegex(StateError, "finish your report"):
                self.call(0, "send_team_message", message="two")
        with (
            patch.object(team_messages, "MAX_BODY_BYTES", 3),
            self.assertRaisesRegex(StateError, "budget"),
        ):
            self.call(0, "send_team_message", message="two")

    def rpc(self, messages, token=None):
        task = self.tasks[0]
        process = subprocess.run(
            [
                sys.executable,
                "-I",
                str(Path(team_mcp.__file__).resolve()),
                str(self.home),
                task["id"],
                token or task["token"],
            ],
            input=messages,
            capture_output=True,
            timeout=10,
            cwd=self.home,
            check=False,
        )
        self.assertEqual(process.returncode, 0, process.stderr.decode())
        self.assertEqual(process.stderr, b"")
        return [json.loads(line) for line in process.stdout.splitlines()]

    def test_private_mcp_subprocess(self):
        requests = [
            {"id": 0, "method": "tools/list"},
            {"id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}},
            {"method": "notifications/initialized"},
            {"id": 2, "method": "tools/list"},
            {
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "send_team_message",
                    "arguments": {"message": "live coordination"},
                },
            },
            {
                "id": 4,
                "method": "tools/call",
                "params": {"name": "read_team_messages", "arguments": {"task_id": "override"}},
            },
            {"id": 5, "method": "tools/call", "params": {"name": "read_team_messages"}},
            {"id": 6, "method": "request_worker"},
        ]
        output = self.rpc(
            b"".join(
                (json.dumps({"jsonrpc": "2.0", **request}) + "\n").encode() for request in requests
            )
        )
        self.assertIn("error", output[0])
        self.assertEqual(
            {tool["name"] for tool in output[2]["result"]["tools"]},
            {"send_team_message", "read_team_messages"},
        )
        self.assertFalse(output[3]["result"]["isError"])
        self.assertTrue(output[4]["result"]["isError"])
        messages = json.loads(output[5]["result"]["content"][0]["text"])["messages"]
        self.assertEqual(messages[0]["body"], "live coordination")
        self.assertIn("error", output[6])
        self.assertNotIn(self.tasks[0]["token"], json.dumps(output))

    def test_mcp_malformed_framing(self):
        for payload in (
            b"\xff\n",
            b"{}",
            b'{"jsonrpc":"2.0","jsonrpc":"2.0"}\n',
            b"a" * (128 * 1024 + 1),
        ):
            with self.subTest(payload=payload[:40]):
                self.assertEqual(self.rpc(payload)[0]["error"]["code"], -32700)
