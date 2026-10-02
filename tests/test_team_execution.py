"""Public tools, real Git workspaces and real offline harness processes for peer teams."""

import json
import unittest
from unittest.mock import patch

from orchestrator.api import request
from orchestrator.runtime import run_task
from orchestrator.store import StateError, Store
from orchestrator.workers import WorkerService
from tests import test_worker_execution as workspace_tests


class TeamExecutionTests(unittest.TestCase):
    def setUp(self):
        workspace_tests.WorkspaceTests.setUp(self)
        self.home = self.root / "home"
        self.store = Store(self.home)
        self.store.add_project("alpha", str(self.source))
        self.store.open_session("coordinator", "claude", "alpha")
        start = patch("orchestrator.api._start_service", return_value={"fixture": True})
        start.start()
        self.addCleanup(start.stop)
        harness = self.root / "offline-harness"
        harness.write_text("""#!/usr/bin/env python3
import json, sys
from pathlib import Path
prompt = sys.stdin.read()
comparison = "UNTRUSTED PEER REPORT BUNDLE:" in prompt
assert Path('file.txt').read_text() == 'original\\n'
if comparison:
    bundle = json.loads(prompt.split('UNTRUSTED PEER REPORT BUNDLE:', 1)[1].split('\\n\\nAuthorized worker workspace', 1)[0])
    assert len(bundle['reports']) == 4
report = {'summary': 'Compared all four reports' if comparison else 'Independent finding',
          'changes': [], 'checks': ['Read pinned source'], 'remaining_issues': ['Unverified hypothesis']}
print(json.dumps({'session_id':'fixture-session','type':'result','subtype':'success','is_error':False,'result':json.dumps(report)}))
""")
        harness.chmod(0o700)
        (self.home / "config").mkdir(exist_ok=True)
        (self.home / "config/local.toml").write_text(
            f"[adapters.claude]\ncommand=[{json.dumps(str(harness))}]\n"
        )
        self.operation("configure_project", {"settings": {"execution": {"max_parallel": 4}}})
        self.operation(
            "setup_project",
            {
                "policy": {
                    "classifications": {
                        "audit": {
                            "team": [
                                {"harness": "claude", "model": f"claude-fixture-{index}"}
                                for index in range(4)
                            ]
                        }
                    }
                }
            },
        )
        origin = self.operation("record_prompt", {"prompt": "Audit this project"})["event_id"]
        self.parent = self.operation(
            "request_worker", {"brief": "Audit file.txt", "origin_event_id": origin}
        )
        self.parent = self.operation(
            "select_worker",
            {
                "request_id": self.parent["id"],
                "choice": {
                    "classification": "audit",
                    "difficulty": "very hard",
                    "rationale": "Four independent audits",
                },
            },
        )
        self.workers = WorkerService(self.store)

    def operation(self, action, payload=None):
        return request(self.home, "coordinator", action, payload)

    def test_four_models_two_rounds_restart_baseline_and_parent_acceptance(self):
        self.workers.dispatch_ready()
        self.assertEqual(self.workers.get(self.parent["id"])["state"], "group_running")
        # Moving the source HEAD after dispatch cannot change the team's captured baseline.
        (self.source / "file.txt").write_text("new source version\n")
        workspace_tests.git(self.source, "add", "file.txt")
        workspace_tests.git(self.source, "commit", "-qm", "Move source after team capture")
        for round_number in (1, 2):
            self.workers = WorkerService(Store(self.home))
            self.workers.dispatch_ready()
            tasks = []
            while task := self.store.claim_next(4):
                tasks.append(task)
                self.assertEqual(task["config"]["worker"]["baseline_commit"], self.original)
                self.assertEqual(task["config"]["worker"]["profile"]["effort"], "max")
                self.assertEqual(
                    run_task(self.home, task["id"], task["token"]),
                    0,
                    self.store.task(task["id"])["error"],
                )
                self.workers.process_result(self.store.task(task["id"]))
            self.assertEqual(len(tasks), 4, f"round {round_number}")
        self.workers.dispatch_ready()
        parent = self.operation("worker", {"request_id": self.parent["id"]})
        self.assertEqual(parent["state"], "candidate")
        self.assertIsNone(parent["task_id"])
        reports = parent["result"]["team_reports"]
        self.assertEqual(len(reports["independent"]["reports"]), 4)
        self.assertEqual(len(reports["comparisons"]["reports"]), 4)
        self.assertEqual(len(self.store.tasks()), 8)
        child = next(
            worker for worker in self.workers.list("alpha") if worker["id"] != parent["id"]
        )
        with self.assertRaisesRegex(StateError, "parent"):
            self.operation(
                "accept_worker", {"request_id": child["id"], "reason": "Cannot accept child"}
            )
        accepted = self.operation(
            "accept_worker", {"request_id": parent["id"], "reason": "Reviewed all disagreements"}
        )
        self.assertEqual(accepted["state"], "accepted")
        self.assertEqual((self.source / "file.txt").read_text(), "new source version\n")

    def test_child_authority_and_cancellation_are_parent_scoped(self):
        self.workers.dispatch_ready()
        child = next(
            worker for worker in self.workers.list("alpha") if worker["id"] != self.parent["id"]
        )
        for action, extra in (
            ("select_worker", {"choice": {}}),
            ("refresh_worker_policy", {}),
            ("approve_worker", {"reason": "No child approval"}),
        ):
            with self.subTest(action=action), self.assertRaisesRegex(StateError, "parent"):
                self.operation(action, {"request_id": child["id"], **extra})
        cancelled = self.operation(
            "cancel_worker", {"request_id": self.parent["id"], "reason": "Stop audit"}
        )
        self.assertEqual(cancelled["state"], "cancelled")
        self.workers.dispatch_ready()
        self.assertEqual(self.store.tasks(), [])
        self.assertTrue(
            all(worker["state"] == "cancelled" for worker in self.workers.list("alpha"))
        )

    def test_forged_child_profile_and_baseline_are_rejected(self):
        self.workers.dispatch_ready()
        self.workers.dispatch_ready()
        task = self.store.claim_next(4)
        with self.store.transaction() as database:
            config = task["config"]
            config["worker"]["baseline_commit"] = "b" * 40
            database.execute(
                "UPDATE tasks SET config_json=? WHERE id=?", (json.dumps(config), task["id"])
            )
        with self.assertRaisesRegex(StateError, "baseline"):
            self.workers.start_check(task)
        with self.store.transaction() as database:
            profile = task["config"]["worker"]["profile"]
            profile["model"] = "claude-different"
            database.execute(
                "UPDATE worker_requests SET profile_json=? WHERE id=?",
                (json.dumps(profile), task["worker_request_id"]),
            )
        with self.assertRaisesRegex(StateError, "parent grant"):
            self.workers.start_check(task)
