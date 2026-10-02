"""Conversational preferences and approvals never cross the bound project boundary."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestrator.api import request
from orchestrator.config import load_config
from orchestrator.graphs import configured_workflow
from orchestrator.store import StateError, Store
from orchestrator.workers import WorkerService


class ProjectControlTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.store = Store(self.home)
        for name in ("alpha", "beta"):
            root = self.home / name
            root.mkdir()
            self.store.add_project(name, str(root))
            self.store.open_session(name, "claude", name)
        service = patch("orchestrator.api._start_service", return_value={})
        service.start()
        self.addCleanup(service.stop)

    def call(self, action, payload=None, session="alpha"):
        return request(self.home, session, action, payload)

    def test_config_and_workflow_edits_never_change_beta_or_defaults(self):
        defaults = load_config(self.home)
        beta = load_config(self.home, "beta")
        template = json.loads(
            (Path(__file__).parents[1] / "workflows/research-review.json").read_text()
        )
        template["summary"] = "Alpha private workflow"
        self.call(
            "configure_project",
            {
                "settings": {
                    "roles": {"monitor": {"model": "Opus", "effort": "max"}},
                    "planning": {
                        "workflow": "alpha-workflow",
                        "templates": {"alpha-workflow": template},
                    },
                    "permissions": {"enforce_monitor_holds": False, "require_write_approval": True},
                }
            },
        )
        self.assertEqual(load_config(self.home), defaults)
        self.assertEqual(load_config(self.home, "beta"), beta)
        self.assertEqual(configured_workflow(self.home, load_config(self.home, "alpha")), template)
        self.assertEqual(self.call("workflows")["workflows"]["alpha-workflow"], template)
        self.assertNotIn("alpha-workflow", self.call("workflows", session="beta")["workflows"])
        with self.assertRaisesRegex(StateError, "accepts"):
            self.call("configure_project", {"project_id": "beta", "settings": {}})
        self.assertFalse((self.home / "config/local.toml").exists())
        self.assertFalse((self.home / "config/projects/beta.json").exists())

    def test_approval_targets_are_checked_against_binding(self):
        workers = WorkerService(self.store)
        origin = self.store.record("beta", "user.message", {"prompt": "Beta work"})
        worker = workers.request("beta", "Beta task", origin_event_id=origin)
        self.store.record("beta", "user.message", {"prompt": "Separate beta planning"})
        plan = self.store.create_plan("beta", "Beta plan", load_config(self.home, "beta"))
        with self.store.transaction() as database:
            database.execute(
                "INSERT INTO holds(id,project_id,source_task,detail,created) VALUES('beta-hold','beta',?,'Hold',0)",
                (plan["planner_task"],),
            )
        for action, fields in (
            ("approve_worker", {"request_id": worker["id"]}),
            ("accept_worker", {"request_id": worker["id"]}),
            ("cancel_worker", {"request_id": worker["id"]}),
            ("approve_plan", {"plan_id": plan["id"]}),
            ("approve_node", {"plan_id": plan["id"], "node_id": "node"}),
            ("resolve_hold", {"hold_id": "beta-hold"}),
        ):
            with self.subTest(action=action), self.assertRaises(StateError):
                self.call(action, {**fields, "reason": "Cannot cross project"})
        self.assertEqual(workers.get(worker["id"])["state"], "pending")
        self.assertEqual(self.store.plan(plan["id"])["status"], "drafting")

    def test_conversational_approval_preference_is_reversible_without_operator(self):
        self.call("setup_project", {"policy": {"default": {"harness": "claude", "model": "Opus"}}})
        origin = self.call("record_prompt", {"prompt": "Inspect project"})["event_id"]
        worker = self.call("request_worker", {"brief": "Inspect", "origin_event_id": origin})
        self.call(
            "select_worker",
            {
                "request_id": worker["id"],
                "choice": {
                    "rule": "default",
                    "difficulty": "easy",
                    "rationale": "Small inspection",
                },
            },
        )
        self.call(
            "configure_project", {"settings": {"permissions": {"coordinator_approvals": False}}}
        )
        with self.assertRaisesRegex(StateError, "configure_project"):
            self.call("approve_worker", {"request_id": worker["id"], "reason": "Approved"})
        self.call(
            "configure_project", {"settings": {"permissions": {"coordinator_approvals": True}}}
        )
        result = self.call(
            "approve_worker", {"request_id": worker["id"], "reason": "Approved here"}
        )
        self.assertEqual(result["approval"], "Approved here")
        self.store.open_session("observer", "pi", "alpha", observer=True)
        for action, payload in (
            ("configure_project", {"settings": {}}),
            ("setup_project", {"policy": {"rules": []}}),
            ("approve_worker", {"request_id": worker["id"], "reason": "No"}),
        ):
            with self.subTest(action=action), self.assertRaises(StateError):
                self.call(action, payload, session="observer")

    def test_reviewed_plan_approval_returns_success_and_records_reason(self):
        plan = self.store.create_plan("alpha", "Reviewed plan", load_config(self.home, "alpha"))
        with self.store.transaction() as database:
            database.execute(
                "UPDATE plans SET status='reviewed',critic_task=? WHERE id=?",
                (plan["planner_task"], plan["id"]),
            )
            cursor = self.store._event(
                database,
                "alpha",
                "plan.reviewed",
                {"plan_id": plan["id"]},
                task_id=plan["planner_task"],
            )
            database.execute("UPDATE projects SET monitor_cursor=? WHERE id='alpha'", (cursor,))
        with patch("orchestrator.api._start_service", return_value={}) as start:
            result = self.call(
                "approve_plan", {"plan_id": plan["id"], "reason": "User approved the reviewed plan"}
            )
            start.assert_called_once_with(self.home)
        self.assertTrue(result["approved"])
        self.assertEqual(self.store.plan(plan["id"])["status"], "approved")
        events = [event for event in self.store.events("alpha") if event["kind"] == "plan.approved"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["payload"]["reason"], "User approved the reviewed plan")
