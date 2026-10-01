"""Restart and stale-result regressions through the supervisor's Store API."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestrator.config import load_config
from orchestrator.runtime import _process_results
from orchestrator.store import StateError, Store, encode

GRAPH = {
    "summary": "OLD SCOPE",
    "assumptions": [],
    "risks": [],
    "questions": [],
    "nodes": [
        {
            "id": "old",
            "title": "Old work",
            "description": "Implement old scope",
            "depends_on": [],
            "acceptance_criteria": ["Old work done"],
            "kind": "work",
        }
    ],
}


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = Store(Path(temporary.name))
        self.store.add_project("project", temporary.name)
        self.store.open_session("session", "test", "project")
        self.config = load_config(self.store.home)

    def complete(self, value):
        task = self.store.claim_next(2)
        self.assertIsNotNone(task)
        self.store.finish(task["id"], task["token"], text=encode(value))
        return self.store.task(task["id"])

    def reviewing(self):
        plan = self.store.create_plan("session", "OLD SCOPE", self.config)
        self.complete(GRAPH)
        _process_results(self.store)
        self.assertEqual(self.store.plan(plan["id"])["status"], "reviewing")
        return plan

    def inspect_monitor(self):
        candidate = self.store.monitor_candidate("project")
        self.store.schedule_monitor("project", candidate, "inspect", self.config, 0)
        self.complete({"reviewed_through": candidate["cursor"], "findings": []})
        _process_results(self.store)

    def test_scope_change_fences_pending_planner(self):
        plan = self.store.create_plan("session", "OLD SCOPE", self.config)
        self.store.record("session", "scope.changed", {"summary": "NEW SCOPE"})
        planner = self.complete(GRAPH)
        _process_results(self.store)
        self.store.attach_critic(planner, "obsolete review", self.config)
        self.inspect_monitor()
        self.assertEqual(self.store.plan(plan["id"])["status"], "needs_revision")
        self.assertIsNone(self.store.plan(plan["id"])["graph_json"])
        self.assertFalse(any(task["role"] == "critic" for task in self.store.tasks()))
        with self.assertRaises(StateError):
            self.store.approve_plan(plan["id"])

    def test_scope_change_between_graph_install_and_critic_enqueue(self):
        plan = self.store.create_plan("session", "OLD SCOPE", self.config)
        planner = self.complete(GRAPH)
        self.assertTrue(self.store.install_graph(plan["id"], planner["id"], GRAPH))
        self.store.record("session", "scope.changed", {"summary": "NEW SCOPE"})
        self.store.attach_critic(planner, "obsolete review", self.config)
        self.assertEqual(self.store.plan(plan["id"])["status"], "needs_revision")
        self.assertEqual(len(self.store.tasks()), 1)
        self.assertEqual(self.store.task(planner["id"])["processed"], 1)

    def test_scope_change_fences_pending_critic_for_both_verdicts(self):
        for verdict in ("approved", "changes_requested"):
            with self.subTest(verdict=verdict):
                plan = self.reviewing()
                self.store.record("session", "scope.changed", {"summary": "NEW SCOPE"})
                self.complete({"verdict": verdict, "findings": []})
                _process_results(self.store)
                self.inspect_monitor()
                self.assertEqual(self.store.plan(plan["id"])["status"], "needs_revision")
                self.assertFalse(self.store.tasks(states=("queued",)))
                with self.assertRaises(StateError):
                    self.store.approve_plan(plan["id"])

    def test_crash_after_critique_commit_has_exactly_one_revision(self):
        plan = self.reviewing()
        critic = self.complete({"verdict": "changes_requested", "findings": ["Fix it"]})
        with (
            patch.object(self.store, "revise_plan", side_effect=KeyboardInterrupt),
            self.assertRaises(KeyboardInterrupt),
        ):
            _process_results(self.store)
        self.store = Store(self.store.home)
        _process_results(self.store)
        self.store.apply_critique(critic, "changes_requested", ["Fix it"])
        self.assertIsNone(self.store.revise_plan(plan["id"], "duplicate", self.config))
        self.assertEqual(self.store.task(critic["id"])["processed"], 1)
        self.assert_one_revision(plan)

    def assert_one_revision(self, plan):
        queued = self.store.tasks(states=("queued",))
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0]["role"], "planner")
        revision = self.store.plan(queued[0]["plan_id"])
        self.assertEqual(revision["review_round"], 2)
        self.assertEqual(self.store.plan(plan["id"])["status"], "superseded")
        self.assertEqual(json.loads(queued[0]["prompt"])["critique"]["findings"], ["Fix it"])
        events = self.store.events("project")
        self.assertEqual(sum(event["kind"] == "plan.revision_started" for event in events), 1)
        self.assertEqual(sum(event["kind"] == "plan.reviewed" for event in events), 1)

    def test_crash_inside_revision_transaction_rolls_back_and_recovers(self):
        plan = self.reviewing()
        critic = self.complete({"verdict": "changes_requested", "findings": ["Fix it"]})
        enqueue = self.store._enqueue

        def crash_after_enqueue(*args, **kwargs):
            enqueue(*args, **kwargs)
            raise KeyboardInterrupt("death before commit")

        with (
            patch.object(self.store, "_enqueue", side_effect=crash_after_enqueue),
            self.assertRaises(KeyboardInterrupt),
        ):
            _process_results(self.store)
        self.store = Store(self.store.home)
        self.assertEqual(self.store.plan(plan["id"])["status"], "reviewing")
        self.assertEqual(self.store.task(critic["id"])["processed"], 0)
        self.assertEqual(len(self.store.tasks()), 2)
        _process_results(self.store)
        _process_results(self.store)
        self.assert_one_revision(plan)

    def test_review_round_limit_still_stops_revisions(self):
        self.config["planning"]["max_review_rounds"] = 2
        self.reviewing()
        self.complete({"verdict": "changes_requested", "findings": ["Fix it"]})
        _process_results(self.store)
        revision_task = self.complete(GRAPH)
        _process_results(self.store)
        critic = self.complete({"verdict": "changes_requested", "findings": ["Still wrong"]})
        _process_results(self.store)
        self.store = Store(self.store.home)
        _process_results(self.store)
        self.assertEqual(len(self.store.tasks()), 4)
        self.assertEqual(self.store.task(critic["id"])["processed"], 1)
        self.assertEqual(self.store.plan(revision_task["plan_id"])["status"], "needs_revision")
