"""Offline review recovery exercises the public helper against durable state."""

import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from orchestrator.config import load_config
from orchestrator.project_settings import configure_project
from orchestrator.reviews import retry_review
from orchestrator.store import StateError, Store


class ReviewRetryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.home = Path(directory.name)
        self.store = Store(self.home)
        for project in ("alpha", "beta"):
            root = self.home / project
            root.mkdir()
            self.store.add_project(project, str(root))
            self.store.open_session(project, "claude", project)
        self.store.open_session("observer", "claude", "alpha", observer=True)
        self.store.open_session("unbound", "claude")
        self.config = load_config(self.home, "alpha")
        self.plan = self.store.create_plan("alpha", "Keep the paid draft", self.config)
        self.planner_id = self.plan["planner_task"]
        self.graph = {
            "summary": "Saved draft",
            "assumptions": [],
            "risks": [],
            "questions": [],
            "nodes": [
                {
                    "id": "inspect",
                    "title": "Inspect source",
                    "description": "Read project source",
                    "depends_on": [],
                    "acceptance_criteria": ["Report source findings"],
                    "kind": "work",
                }
            ],
        }
        with self.store.transaction() as database:
            database.execute(
                "UPDATE tasks SET state='succeeded',result='planner evidence' WHERE id=?",
                (self.planner_id,),
            )
        self.store.install_graph(self.plan["id"], self.planner_id, self.graph)
        self.prompt = 'Exact saved graph and evidence context: {"draft": "unchanged"}\n'
        self.store.attach_critic(self.store.task(self.planner_id), self.prompt, self.config)
        self.critic_id = self.store.plan(self.plan["id"])["critic_task"]
        with self.store.transaction() as database:
            database.execute(
                "UPDATE tasks SET state='failed',result='original output',error='AGENTS.md read' "
                "WHERE id=?",
                (self.critic_id,),
            )

    def retry(self, session="alpha", reason="Fixed critic settings"):
        return retry_review(self.store, session, self.plan["id"], reason)

    def test_reuses_draft_prompt_dependency_and_current_configuration(self):
        old_plan = self.store.plan(self.plan["id"])
        old_critic = self.store.task(self.critic_id)
        old_planner = self.store.task(self.planner_id)
        configure_project(self.store, "alpha", {"roles": {"critic": {"model": "fixed-model"}}})
        result = self.retry()
        critic = self.store.task(result["task_id"])
        self.assertTrue(result["reused_draft"])
        self.assertFalse(result["new_planner_started"])
        self.assertEqual(result["state"], "queued")
        self.assertEqual(critic["prompt"], self.prompt)
        self.assertEqual(critic["config"], load_config(self.home, "alpha"))
        self.assertEqual(critic["idempotency_key"], f"critic-retry:{self.critic_id}")
        self.assertEqual(self.store.task(self.critic_id), old_critic)
        self.assertEqual(self.store.task(self.planner_id), old_planner)
        updated = self.store.plan(self.plan["id"])
        for key in old_plan.keys() - {"status", "critic_task"}:
            self.assertEqual(updated[key], old_plan[key])
        self.assertEqual(updated["status"], "reviewing")
        self.assertEqual(len([t for t in self.store.tasks() if t["role"] == "planner"]), 1)
        with self.store.transaction() as database:
            dependency = database.execute(
                "SELECT * FROM dependencies WHERE task_id=?", (critic["id"],)
            ).fetchone()
            self.assertEqual(tuple(dependency), (critic["id"], self.planner_id))
            event = database.execute(
                "SELECT * FROM events WHERE kind='critic.retry_requested'"
            ).fetchone()
            self.assertEqual(event["review_required"], 1)
            payload = json.loads(event["payload"])
            self.assertEqual(payload["previous_critic_id"], self.critic_id)
            self.assertEqual(payload["critic_task_id"], critic["id"])
            self.assertEqual(payload["reason"], "Fixed critic settings")
            self.assertIsNone(
                database.execute("SELECT 1 FROM inbox WHERE event_id=?", (event["id"],)).fetchone()
            )

    def test_concurrent_requests_enqueue_only_one_critic(self):
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: self.retry(), range(2)))
        self.assertEqual(results[0], results[1])
        self.assertEqual(len([t for t in self.store.tasks() if t["role"] == "critic"]), 2)

    def test_session_authorization_and_cross_project(self):
        for session in ("observer", "unbound", "beta", "missing"):
            with self.subTest(session=session), self.assertRaises(StateError):
                self.retry(session)
        self.store.close_session("alpha")
        with self.assertRaises(StateError):
            self.retry()
        self.store.open_session("new-writer", "claude", "alpha")
        result = self.retry("new-writer")
        self.assertEqual(self.store.task(result["task_id"])["session_id"], "new-writer")
        self.assertEqual(self.store.plan(self.plan["id"])["session_id"], "alpha")

    def test_superseded_plan_cannot_reuse_another_plans_review(self):
        new_plan = self.store.create_plan("alpha", "Replacement", self.config)
        self.store.attach_critic(self.store.task(new_plan["planner_task"]), "Other", self.config)
        with self.assertRaisesRegex(StateError, "current|superseded"):
            self.retry()
        with self.store.transaction() as database:
            database.execute("UPDATE plans SET status='reviewing' WHERE id=?", (self.plan["id"],))
        with self.assertRaisesRegex(StateError, "current|superseded"):
            self.retry()

    def test_unknown_never_replayed(self):
        with self.store.transaction() as database:
            database.execute("UPDATE tasks SET state='unknown' WHERE id=?", (self.critic_id,))
        with self.assertRaisesRegex(StateError, "manual|explicit"):
            self.retry()
        self.assertEqual(len(self.store.tasks()), 2)

    def test_failed_planner_or_missing_graph_refused(self):
        with self.store.transaction() as database:
            database.execute("UPDATE tasks SET state='failed' WHERE id=?", (self.planner_id,))
        with self.assertRaisesRegex(StateError, "planner"):
            self.retry()
        with self.store.transaction() as database:
            database.execute("UPDATE tasks SET state='succeeded' WHERE id=?", (self.planner_id,))
            database.execute("UPDATE plans SET graph_json=NULL WHERE id=?", (self.plan["id"],))
        with self.assertRaisesRegex(StateError, "draft|graph"):
            self.retry()

    def test_cancelled_and_processing_failure_can_retry_needs_revision(self):
        for state in ("cancelled", "succeeded"):
            with self.subTest(state=state):
                with self.store.transaction() as database:
                    database.execute(
                        "UPDATE plans SET status='needs_revision',critic_task=? WHERE id=?",
                        (self.critic_id, self.plan["id"]),
                    )
                    database.execute("UPDATE tasks SET state=? WHERE id=?", (state, self.critic_id))
                self.store.fail_processing(self.store.task(self.critic_id), "Invalid critic output")
                result = self.retry()
                self.assertEqual(result["state"], "queued")
                self.critic_id = result["task_id"]

    def test_valid_success_is_not_retried(self):
        with self.store.transaction() as database:
            database.execute(
                "UPDATE tasks SET state='succeeded',error=NULL WHERE id=?", (self.critic_id,)
            )
        with self.assertRaises(StateError):
            self.retry()

    def test_live_current_critic_is_returned_without_enqueue(self):
        for state in ("queued", "starting", "running"):
            with self.subTest(state=state):
                with self.store.transaction() as database:
                    database.execute("UPDATE tasks SET state=? WHERE id=?", (state, self.critic_id))
                result = self.retry()
                self.assertEqual(result["task_id"], self.critic_id)
                self.assertEqual(result["state"], state)
                self.assertEqual(len(self.store.tasks()), 2)

    def test_mislinked_tasks_and_nonreview_states_refused(self):
        for column, task_id in (("planner_task", self.critic_id), ("critic_task", self.planner_id)):
            with self.subTest(column=column):
                with self.store.transaction() as database:
                    database.execute(
                        f"UPDATE plans SET {column}=? WHERE id=?", (task_id, self.plan["id"])
                    )
                with self.assertRaises(StateError):
                    self.retry()
                with self.store.transaction() as database:
                    database.execute(
                        "UPDATE plans SET planner_task=?,critic_task=? WHERE id=?",
                        (self.planner_id, self.critic_id, self.plan["id"]),
                    )
        for status in ("approved", "reviewed", "drafting"):
            with self.subTest(status=status):
                with self.store.transaction() as database:
                    database.execute(
                        "UPDATE plans SET status=? WHERE id=?", (status, self.plan["id"])
                    )
                with self.assertRaises(StateError):
                    self.retry()
        self.assertEqual(len(self.store.tasks()), 2)

    def test_live_critic_with_needs_revision_does_not_overlap(self):
        with self.store.transaction() as database:
            database.execute(
                "UPDATE plans SET status='needs_revision' WHERE id=?", (self.plan["id"],)
            )
            database.execute("UPDATE tasks SET state='running' WHERE id=?", (self.critic_id,))
        with self.assertRaises(StateError):
            self.retry()
        self.assertEqual(len(self.store.tasks()), 2)

    def test_second_failed_attempt_can_be_retried_without_replanning(self):
        first = self.retry()
        with self.store.transaction() as database:
            database.execute("UPDATE tasks SET state='failed' WHERE id=?", (first["task_id"],))
        second = self.retry()
        self.assertNotEqual(first["task_id"], second["task_id"])
        self.assertEqual(
            self.store.task(second["task_id"])["idempotency_key"],
            f"critic-retry:{first['task_id']}",
        )
        self.assertEqual(len([t for t in self.store.tasks() if t["role"] == "planner"]), 1)

    def test_invalid_reason_and_ids(self):
        for reason in (None, "", " ", "x" * 12001, "nul\x00"):
            with self.subTest(reason=repr(reason)[:30]), self.assertRaises(StateError):
                self.retry(reason=reason)
        for plan_id in (None, "../escape", [], "missing"):
            with self.subTest(plan_id=plan_id), self.assertRaises(StateError):
                retry_review(self.store, "alpha", plan_id, "Retry")
