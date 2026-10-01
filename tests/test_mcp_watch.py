"""Exercise the line transport with a live input pipe, not direct handle calls."""

import io
import json
import os
import queue
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestrator.api import FIELDS, request
from orchestrator.mcp import MCPServer
from orchestrator.store import Store


class Responses(io.StringIO):
    def __init__(self):
        super().__init__()
        self.messages = queue.Queue()

    def write(self, text):
        self.messages.put(json.loads(text))
        return len(text)


class WatchTransportTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.home = Path(self.directory.name)
        self.store = Store(self.home)
        root = self.home / "project"
        root.mkdir()
        self.store.add_project("alpha", str(root))
        self.store.open_session("session", "test", "alpha")
        self.task = self.store.enqueue("alpha", "session", "planner", "real task", {})
        self.entered = threading.Event()
        self.release = threading.Event()
        self.detached = []
        self.polls = 0
        self.output = Responses()
        self.server = MCPServer(self.home, "session", output=self.output)
        reader, writer = os.pipe()
        self.reader = os.fdopen(reader)
        self.writer = os.fdopen(writer, "w")
        self.addCleanup(self.reader.close)
        self.addCleanup(self.writer.close)
        self.addCleanup(self.release.set)
        self.fields = patch.dict(FIELDS, watch_worker=({"watcher_id": "string"}, {"watcher_id"}))
        self.fields.start()
        self.addCleanup(self.fields.stop)
        self.requests = patch("orchestrator.mcp.request", side_effect=self.operation)
        self.request_mock = self.requests.start()
        self.addCleanup(self.requests.stop)
        self.detach_patch = patch.object(
            self.server,
            "_detach_watch",
            side_effect=lambda session, lease: self.detached.append((session, lease)),
        )
        self.detach_patch.start()
        self.addCleanup(self.detach_patch.stop)
        self.thread = threading.Thread(target=self.server.serve, args=(self.reader,))
        self.thread.start()
        self.addCleanup(self.stop)
        self.send("initialize", "init", {"protocolVersion": "2025-06-18"})
        self.assertEqual(self.receive()["id"], "init")

    def operation(self, home, session, action, payload):
        if action != "watch_worker":
            return request(home, session, action, payload)
        self.polls += 1
        self.entered.set()
        self.release.wait(2)
        return {"done": False, "detached": False, "worker": {"state": "running"}}

    def send(self, method, identifier=None, params=None):
        message = {"jsonrpc": "2.0", "method": method}
        if identifier is not None:
            message["id"] = identifier
        if params is not None:
            message["params"] = params
        self.writer.write(json.dumps(message) + "\n")
        self.writer.flush()

    def receive(self):
        return self.output.messages.get(timeout=1)

    def watch(self, identifier="watch", watcher_id="lease", **extra):
        self.send(
            "tools/call",
            identifier,
            {"name": "watch_worker", "arguments": {"watcher_id": watcher_id}, **extra},
        )

    def stop(self):
        self.release.set()
        self.writer.close()
        self.thread.join(5)
        self.assertFalse(self.thread.is_alive())

    def test_ping_and_status_during_pending_watch(self):
        self.watch()
        self.assertTrue(self.entered.wait(1))
        self.send("ping", "ping")
        self.assertEqual(self.receive()["id"], "ping")
        self.send("tools/call", "status", {"name": "status", "arguments": {}})
        self.assertEqual(self.receive()["id"], "status")
        self.stop()
        self.assertEqual(self.store.task(self.task["id"])["state"], "queued")
        self.assertEqual(self.detached, [("session", "lease")])
        self.assertTrue(self.output.messages.empty())

    def test_cancel_only_matching_wait_and_duplicate_id(self):
        self.watch()
        self.assertTrue(self.entered.wait(1))
        self.send("ping", "watch")
        duplicate = self.receive()
        self.assertIsNone(duplicate["id"])
        self.assertIn("error", duplicate)
        self.send("notifications/cancelled", params={"requestId": "other"})
        self.send("ping", "still-pending")
        self.assertEqual(self.receive()["id"], "still-pending")
        self.send("notifications/cancelled", params={"requestId": "watch"})
        self.send("ping", "cancel-processed")
        self.assertEqual(self.receive()["id"], "cancel-processed")
        self.release.set()
        cancelled = self.receive()
        self.assertEqual(cancelled["id"], "watch")
        self.assertEqual(cancelled["error"]["code"], -32800)
        self.assertEqual(self.detached, [("session", "lease")])
        self.assertEqual(self.store.task(self.task["id"])["state"], "queued")
        self.stop()
        self.assertTrue(self.output.messages.empty())

    def test_cancelling_one_watch_leaves_other_wait_pending(self):
        self.watch("first", "lease-one")
        self.watch("second", "lease-two")
        self.send("ping", "started")
        self.assertEqual(self.receive()["id"], "started")
        self.send("notifications/cancelled", params={"requestId": "first"})
        self.send("ping", "cancel-recorded")
        self.assertEqual(self.receive()["id"], "cancel-recorded")
        self.release.set()
        response = self.receive()
        self.assertEqual(response["id"], "first")
        self.assertEqual(response["error"]["code"], -32800)
        self.send("ping", "second-pending")
        self.assertEqual(self.receive()["id"], "second-pending")
        with self.server.watch_lock:
            self.assertIn("second", self.server.watches)
        self.stop()
        self.assertCountEqual(self.detached, [("session", "lease-one"), ("session", "lease-two")])

    def test_capacity_and_validation(self):
        with patch("orchestrator.mcp.MAX_WATCHES", 1):
            self.watch()
            self.assertTrue(self.entered.wait(1))
            self.watch("second")
            self.assertIn("error", self.receive())
        self.watch("invalid-token", _meta={"progressToken": True})
        self.assertIn("error", self.receive())
        self.send(
            "tools/call",
            "forged",
            {"name": "watch_worker", "arguments": {"watcher_id": "lease", "session_id": "other"}},
        )
        self.assertIn("error", self.receive())
        self.watch("duplicate-lease")
        self.assertIn("error", self.receive())
        for token in (None, [], {}, "x" * 257):
            self.watch("bad-token", _meta={"progressToken": token})
            self.assertIn("error", self.receive())
        self.assertEqual(self.polls, 1)

    def test_timeout_is_detachment_and_progress_is_safe(self):
        self.release.set()
        with (
            patch("orchestrator.mcp.WATCH_SECONDS", 0.04),
            patch("orchestrator.mcp.WATCH_POLL_SECONDS", 0.01),
        ):
            self.watch(_meta={"progressToken": 0})
            progress = []
            while True:
                response = self.receive()
                if "id" in response:
                    break
                progress.append(response)
            self.assertGreater(len(progress), 0)
            self.assertTrue(all(item["params"]["progressToken"] == 0 for item in progress))
            value = json.loads(response["result"]["content"][0]["text"])
            self.assertTrue(value["timed_out"])
            self.assertTrue(value["detached"])
            self.assertFalse(value["done"])
            self.assertGreater(self.polls, 1)
            self.assertEqual(self.detached, [("session", "lease")])

    def test_completion_detaches_and_no_progress_without_token(self):
        self.request_mock.side_effect = lambda *args: {
            "done": True,
            "detached": False,
            "worker": {"state": "candidate"},
        }
        self.watch()
        response = self.receive()
        self.assertTrue(json.loads(response["result"]["content"][0]["text"])["done"])
        self.assertEqual(self.detached, [("session", "lease")])
        self.assertTrue(self.output.messages.empty())

    def real_watch(self):
        from orchestrator.visibility import authorize_claude_agent, prepare_worker_watch

        self.requests.stop()
        self.detach_patch.stop()
        with self.store.transaction() as database:
            database.execute("UPDATE sessions SET frontend='claude' WHERE id='session'")
        origin = request(self.home, "session", "record_prompt", {"prompt": "Inspect files"})[
            "event_id"
        ]
        with patch("orchestrator.api._start_service"):
            worker = request(
                self.home,
                "session",
                "request_worker",
                {"brief": "Inspect files", "origin_event_id": origin},
            )
        with self.store.transaction() as database:
            database.execute(
                "UPDATE worker_requests SET state='running',task_id=? WHERE id=?",
                (self.task["id"], worker["id"]),
            )
        claimed = self.store.claim_next(1)
        self.assertEqual(claimed["id"], self.task["id"])
        prepared = prepare_worker_watch(self.store, "session", worker["id"])
        authorize_claude_agent(self.store, "session", prepared["agent"], "native-tool-use")
        self.send(
            "tools/call",
            "real-watch",
            {
                "name": "watch_worker",
                "arguments": {"watcher_id": prepared["watcher_id"]},
                "_meta": {"progressToken": "real-progress"},
            },
        )
        self.assertEqual(self.receive()["method"], "notifications/progress")
        return prepared

    def assert_real_detached(self, prepared):
        from orchestrator.store import StateError
        from orchestrator.visibility import watch_snapshot

        with self.assertRaisesRegex(StateError, "detached"):
            watch_snapshot(self.store, "session", prepared["watcher_id"])
        task = self.store.task(self.task["id"])
        self.assertEqual(task["state"], "starting")
        self.assertFalse(task["cancel_requested"])
        self.assertFalse(self.server.watches)

    def test_real_api_cancel_leaves_durable_task_active(self):
        prepared = self.real_watch()
        self.send("ping", "live-ping")
        self.assertEqual(self.receive()["id"], "live-ping")
        self.send("tools/call", "live-status", {"name": "status", "arguments": {}})
        self.assertEqual(self.receive()["id"], "live-status")
        self.send("notifications/cancelled", params={"requestId": "real-watch"})
        self.assertEqual(self.receive()["error"]["code"], -32800)
        self.stop()
        self.assert_real_detached(prepared)

    def test_real_api_revalidates_session_on_every_poll(self):
        with patch("orchestrator.mcp.WATCH_POLL_SECONDS", 0.05):
            prepared = self.real_watch()
            with self.store.transaction() as database:
                database.execute("UPDATE sessions SET active=0 WHERE id='session'")
            response = self.receive()
            self.assertTrue(response["result"]["isError"])
            self.stop()
            with self.store.transaction() as database:
                database.execute("UPDATE sessions SET active=1 WHERE id='session'")
            self.assert_real_detached(prepared)

    def test_output_bound_and_raw_worker_text_not_in_progress(self):
        self.request_mock.side_effect = lambda *args: {
            "done": False,
            "detached": False,
            "worker": {"state": "SECRET PROMPT", "stdout": "SECRET LOG"},
        }
        with patch("orchestrator.mcp.WATCH_SECONDS", 0):
            self.watch(_meta={"progressToken": "token"})
            progress = self.receive()
            self.assertNotIn("SECRET", json.dumps(progress))
            self.assertTrue(json.loads(self.receive()["result"]["content"][0]["text"])["timed_out"])
        self.request_mock.side_effect = lambda *args: {"done": True, "worker": "x" * 70000}
        self.watch("oversized")
        response = self.receive()
        self.assertTrue(response["result"]["isError"])
        self.assertLess(len(json.dumps(response)), 4096)

    def test_real_api_eof_detaches_without_cancelling_durable_task(self):
        prepared = self.real_watch()
        self.stop()
        self.assert_real_detached(prepared)
        self.assertTrue(self.output.messages.empty())


class WatchBackpressureTests(unittest.TestCase):
    def test_eof_interrupts_full_stdout_pipe_and_joins_watch(self):
        with tempfile.TemporaryDirectory() as temporary:
            output_reader, output_writer = os.pipe()
            input_reader, input_writer = os.pipe()
            with (
                os.fdopen(output_reader, "rb") as retained_output,
                os.fdopen(output_writer, "w") as output,
                os.fdopen(input_reader) as source,
                os.fdopen(input_writer, "w") as client,
            ):
                server = MCPServer(Path(temporary), "session", output=output)
                server.handle(
                    {
                        "jsonrpc": "2.0",
                        "id": 0,
                        "method": "initialize",
                        "params": {"protocolVersion": "2025-06-18"},
                    }
                )
                while True:
                    try:
                        os.write(output.fileno(), b"x" * 4096)
                    except BlockingIOError:
                        break
                sending = threading.Event()
                real_send = server.send

                def send(message):
                    sending.set()
                    return real_send(message)

                with (
                    patch(
                        "orchestrator.mcp.request",
                        return_value={"done": True, "worker": {"state": "candidate"}},
                    ),
                    patch.object(server, "_detach_watch"),
                    patch.object(server, "send", side_effect=send),
                ):
                    serving = threading.Thread(target=server.serve, args=(source,))
                    serving.start()
                    try:
                        client.write(
                            json.dumps(
                                {
                                    "jsonrpc": "2.0",
                                    "id": "watch",
                                    "method": "tools/call",
                                    "params": {
                                        "name": "watch_worker",
                                        "arguments": {"watcher_id": "lease"},
                                    },
                                }
                            )
                            + "\n"
                        )
                        client.flush()
                        self.assertTrue(sending.wait(2))
                        client.close()
                        serving.join(timeout=2)
                        self.assertFalse(
                            serving.is_alive(), "EOF must interrupt output backpressure"
                        )
                        self.assertTrue(server.closed.is_set())
                        self.assertFalse(any(thread.is_alive() for thread in server.watch_threads))
                        self.assertFalse(
                            retained_output.closed, "Client still retains the undrained output"
                        )
                    finally:
                        client.close()
                        server.closed.set()
                        serving.join(timeout=6)
