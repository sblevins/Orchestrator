import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from orchestrator.api import request
from orchestrator.store import StateError, Store, encode
from orchestrator.visibility import (
    authorize_claude_agent,
    detach_watch,
    prepare_worker_watch,
    watch_snapshot,
    worker_view,
)


class VisibilityTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.home = Path(self.directory.name)
        self.store = Store(self.home)
        for project in ("alpha", "beta"):
            root = self.home / project
            root.mkdir()
            self.store.add_project(project, str(root))
            self.store.open_session(project, "claude", project)
        self.store.open_session("pi", "pi", "alpha", observer=True)
        self.store.open_session("viewer", "claude", "alpha", observer=True)
        self.store.open_session("unbound", "claude")
        with patch("orchestrator.api._start_service", return_value={}):
            origin = request(self.home, "alpha", "record_prompt", {"prompt": "Inspect code"})[
                "event_id"
            ]
            self.worker = request(
                self.home,
                "alpha",
                "request_worker",
                {
                    "brief": "Inspect code",
                    "origin_event_id": origin,
                },
            )
        # Real durable task plus linked worker, without launching any paid model.
        self.task = self.store.enqueue("alpha", "alpha", "planner", "PRIVATE PROMPT", {})
        with self.store.transaction() as database:
            database.execute(
                "UPDATE worker_requests SET task_id=?,state='queued',profile_json=? WHERE id=?",
                (
                    self.task["id"],
                    encode(
                        {
                            "model": "actual-model",
                            "effort": "high",
                            "harness": "pi",
                            "provider": "vendor",
                            "secret": "PRIVATE PROFILE",
                        }
                    ),
                    self.worker["id"],
                ),
            )
            database.execute(
                "UPDATE tasks SET token='PRIVATE TOKEN',error='PRIVATE ERROR' WHERE id=?",
                (self.task["id"],),
            )

    def attach(self, session="alpha"):
        prepared = prepare_worker_watch(self.store, session, self.worker["id"])
        authorize_claude_agent(self.store, session, prepared["agent"], "tool-1")
        return prepared

    def test_frontend_not_worker_model_decides_watcher(self):
        prepared = self.attach()
        self.assertEqual(prepared["agent"]["model"], "haiku")
        self.assertIn("actual-model", prepared["agent"]["description"])
        self.assertEqual(worker_view(self.store, "pi")["frontend"], "pi")
        for operation, argument in (
            (prepare_worker_watch, self.worker["id"]),
            (watch_snapshot, prepared["watcher_id"]),
        ):
            with self.assertRaisesRegex(StateError, "only in a Claude"):
                operation(self.store, "pi", argument)
        self.assertEqual(self.store.task(self.task["id"])["state"], "queued")

    def test_strict_invocation_and_idempotent_hook_replay(self):
        prepared = prepare_worker_watch(self.store, "alpha", self.worker["id"])
        with self.assertRaisesRegex(StateError, "Start the prepared"):
            watch_snapshot(self.store, "alpha", prepared["watcher_id"])
        for field, value in (
            ("model", "opus"),
            ("prompt", "Do the work"),
            ("run_in_background", False),
            ("extra", True),
        ):
            with self.subTest(field=field), self.assertRaises(StateError):
                authorize_claude_agent(
                    self.store, "alpha", {**prepared["agent"], field: value}, "tool-1"
                )
        for _ in range(2):
            self.assertTrue(
                authorize_claude_agent(self.store, "alpha", prepared["agent"], "tool-1")[
                    "authorized"
                ]
            )
        with self.assertRaises(StateError):
            authorize_claude_agent(self.store, "alpha", prepared["agent"], "tool-2")
        again = prepare_worker_watch(self.store, "alpha", self.worker["id"])
        self.assertTrue(again["already_attached"])
        self.assertIsNone(again["agent"])

    def test_concurrent_prepare_and_launch_are_deduplicated(self):
        with ThreadPoolExecutor(max_workers=4) as executor:
            prepared = list(
                executor.map(
                    lambda _: prepare_worker_watch(self.store, "alpha", self.worker["id"]), range(8)
                )
            )
        self.assertEqual(len({item["watcher_id"] for item in prepared}), 1)

        def launch(index):
            try:
                authorize_claude_agent(self.store, "alpha", prepared[0]["agent"], f"tool-{index}")
                return True
            except StateError:
                return False

        with ThreadPoolExecutor(max_workers=4) as executor:
            self.assertEqual(sum(executor.map(launch, range(8))), 1)

    def test_detach_and_expiry_do_not_cancel_execution(self):
        first = self.attach()
        detach_watch(self.store, "alpha", first["watcher_id"])
        with self.assertRaises(StateError):
            watch_snapshot(self.store, "alpha", first["watcher_id"])
        second = prepare_worker_watch(self.store, "alpha", self.worker["id"])
        self.assertNotEqual(first["watcher_id"], second["watcher_id"])
        with patch("orchestrator.visibility.now", return_value=10**12):
            third = prepare_worker_watch(self.store, "alpha", self.worker["id"])
        self.assertNotEqual(second["watcher_id"], third["watcher_id"])
        self.assertFalse(self.store.task(self.task["id"])["cancel_requested"])
        self.assertEqual(self.store.task(self.task["id"])["state"], "queued")

    def test_boundaries_and_takeover_invalidate_capability(self):
        prepared = self.attach()
        for session in ("viewer", "beta", "unbound"):
            with self.subTest(session=session), self.assertRaises(StateError):
                watch_snapshot(self.store, session, prepared["watcher_id"])
        with self.assertRaises(StateError):
            worker_view(self.store, "beta", self.worker["id"])
        observer = self.attach("viewer")
        self.assertFalse(
            watch_snapshot(self.store, "viewer", observer["watcher_id"])["worker"][
                "can_request_task_cancel"
            ]
        )
        self.store.open_session("replacement", "claude", "alpha", takeover=True)
        with self.assertRaises(StateError):
            watch_snapshot(self.store, "alpha", prepared["watcher_id"])
        detach_watch(self.store, "alpha", prepared["watcher_id"])

    def test_candidate_done_is_not_accepted_and_private_fields_omitted(self):
        prepared = self.attach()
        snapshot = watch_snapshot(self.store, "alpha", prepared["watcher_id"])
        self.assertFalse(snapshot["done"])
        self.assertNotIn("PRIVATE", json.dumps(snapshot))
        with self.store.transaction() as database:
            database.execute(
                "UPDATE worker_requests SET state='candidate',result_json=? WHERE id=?",
                (
                    encode(
                        {
                            "summary": "a" * 10000,
                            "changes": ["b" * 10000] * 256,
                            "checks": [],
                            "remaining_issues": [],
                            "secret": "PRIVATE",
                        }
                    ),
                    self.worker["id"],
                ),
            )
        snapshot = watch_snapshot(self.store, "alpha", prepared["watcher_id"])
        self.assertTrue(snapshot["done"])
        self.assertFalse(snapshot["worker"]["accepted"])
        self.assertLess(len(json.dumps(snapshot)), 14000)
        self.assertNotIn("PRIVATE", json.dumps(snapshot))

    def test_api_validates_arguments_and_permits_readonly_observer(self):
        self.assertEqual(
            request(self.home, "viewer", "worker_view", {"request_id": self.worker["id"]})[
                "worker"
            ]["request_id"],
            self.worker["id"],
        )
        for payload in ({"offset": True}, {"offset": -1}, {"extra": "wrong"}, {"request_id": []}):
            with self.subTest(payload=payload), self.assertRaises(StateError):
                request(self.home, "viewer", "worker_view", payload)
        with self.assertRaises(StateError):
            request(self.home, "pi", "prepare_worker_watch", {"request_id": self.worker["id"]})

    def test_pagination_is_bounded_and_stable(self):
        with patch("orchestrator.visibility.PAGE_SIZE", 1):
            page = worker_view(self.store, "alpha")
            self.assertEqual(len(page["workers"]), 1)
            self.assertIsNone(page["next_offset"])
            self.assertEqual(worker_view(self.store, "alpha", offset=1)["workers"], [])


if __name__ == "__main__":
    unittest.main()
