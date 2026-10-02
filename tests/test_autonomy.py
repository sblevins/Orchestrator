"""Standing authorization uses real persisted worker gates and project settings."""

import contextlib
import tempfile
import unittest
from pathlib import Path

from orchestrator import autonomy, teams
from orchestrator.store import Store, encode
from orchestrator.workers import WorkerService

PROFILE = {"harness": "pi", "provider": "openai", "model": "gpt-5.4", "effort": "high"}
CHOICE = {"rule": "default", "model": "gpt-5.4", "effort": "high", "rationale": "Fits"}
REPORT = {"summary": "Done", "changes": [], "checks": ["Read source"], "remaining_issues": []}


class AutonomyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.store = Store(self.home)
        self.workers = WorkerService(self.store)
        self.origins = {}
        for project_id in ("a", "b"):
            root = self.home / project_id
            (root / ".orchestrator").mkdir(parents=True)
            (root / ".orchestrator/crew-dispatch.json").write_text(encode({"default": PROFILE}))
            self.store.add_project(project_id, str(root))
            self.store.open_session(project_id, "test", project_id)
            self.origins[project_id] = self.store.record(
                project_id, "user.message", {"text": "Inspect"}
            )
        self.configure("a", True)
        self.configure("b", False)

    def configure(self, project_id, enabled):
        path = self.home / "config/projects" / (project_id + ".toml")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("[execution]\nunattended = " + str(enabled).lower() + "\n")

    def selected(self, project_id="a"):
        request = self.workers.request(
            project_id, "Inspect", "read", origin_event_id=self.origins[project_id]
        )
        return self.workers.select(project_id, request["id"], CHOICE)

    def candidate(self, project_id="a", issues=None):
        request = self.selected(project_id)
        self.workers.dispatch_ready()
        task = self.store.claim_next(3)
        self.assertEqual(task["worker_request_id"], request["id"])
        self.store.finish(
            task["id"], task["token"], text=encode({**REPORT, "remaining_issues": issues or []})
        )
        self.workers.process_result(self.store.task(task["id"]))
        self.assertEqual(self.workers.get(request["id"])["state"], "candidate")
        return request

    def waits(self, project_id="a"):
        return [
            event for event in self.store.events(project_id) if event["kind"] == "autonomy.waiting"
        ]

    def test_only_authorized_project_accepts_and_config_revocation_stops(self):
        authorized = self.candidate("a")
        manual = self.candidate("b")
        autonomy.advance(self.store)
        self.assertEqual(self.workers.get(authorized["id"])["accepted"], autonomy.REASON)
        self.assertEqual(self.workers.get(manual["id"])["state"], "candidate")
        next_request = self.candidate("a")
        self.configure("a", False)
        autonomy.advance(self.store)
        self.assertEqual(self.workers.get(next_request["id"])["state"], "candidate")

    def test_acceptance_respects_pause_and_does_not_change_source(self):
        source_file = self.home / "a" / "source.txt"
        source_file.write_text("uncommitted source remains untouched\n")
        request = self.candidate()
        self.store.pause("a", "Operator paused")
        autonomy.advance(self.store)
        self.assertEqual(self.workers.get(request["id"])["state"], "candidate")
        self.store.resume("a")
        autonomy.advance(self.store)
        self.assertEqual(self.workers.get(request["id"])["state"], "accepted")
        self.assertEqual(source_file.read_text(), "uncommitted source remains untouched\n")
        self.assertFalse((self.home / "a" / ".git").exists())

    def test_remaining_issues_notify_once_without_claiming_verified(self):
        request = self.candidate(issues=["Untested change"])
        autonomy.advance(self.store)
        autonomy.advance(Store(self.home))
        self.assertEqual(self.workers.get(request["id"])["state"], "candidate")
        waits = self.waits()
        self.assertEqual(len(waits), 1)
        self.assertFalse(waits[0]["review_required"])
        self.assertIn("Needs attention", waits[0]["payload"]["reason"])
        with contextlib.closing(self.store.connect()) as database:
            self.assertEqual(
                database.execute(
                    "SELECT COUNT(*) FROM inbox WHERE event_id=?", (waits[0]["id"],)
                ).fetchone()[0],
                1,
            )

    def test_unknown_failed_and_cancelled_are_never_replayed(self):
        for state in ("unknown", "failed", "cancelled"):
            request = self.selected()
            with self.store.transaction() as database:
                database.execute(
                    "UPDATE worker_requests SET state=? WHERE id=?", (state, request["id"])
                )
            autonomy.advance(self.store)
            current = self.workers.get(request["id"])
            self.assertEqual(current["state"], state)
            self.assertIsNone(current["task_id"])

    def test_bad_project_configuration_does_not_stop_other_project(self):
        request = self.candidate("b")
        self.configure("b", True)
        (self.home / "config/projects/a.toml").write_text("[invalid")
        autonomy.advance(self.store)
        autonomy.advance(self.store)
        self.assertEqual(self.workers.get(request["id"])["state"], "accepted")
        self.assertEqual(len(self.waits("a")), 1)

    def reviewed_plan(self):
        plan = self.store.create_plan("a", "Inspect", {}, origin_event_id=self.origins["a"])
        with self.store.transaction() as database:
            database.execute(
                "UPDATE tasks SET state='cancelled',processed=1 WHERE id=?", (plan["planner_task"],)
            )
            database.execute(
                "UPDATE plans SET status='reviewed',critic_task=? WHERE id=?",
                (plan["planner_task"], plan["id"]),
            )
            event_id = self.store._event(
                database,
                "a",
                "plan.reviewed",
                {"verdict": "approved"},
                task_id=plan["planner_task"],
            )
        return plan, event_id

    def test_reviewed_plan_needs_monitor_evidence_and_holds_resolved(self):
        plan, review_event = self.reviewed_plan()
        autonomy.advance(self.store)
        autonomy.advance(self.store)
        self.assertEqual(self.store.plan(plan["id"])["status"], "reviewed")
        self.assertEqual(len(self.waits()), 1)
        with self.store.transaction() as database:
            database.execute("UPDATE projects SET monitor_cursor=? WHERE id='a'", (review_event,))
        self.store.pause("a", "Review required")
        autonomy.advance(self.store)
        self.assertEqual(self.store.plan(plan["id"])["status"], "reviewed")
        self.store.resume("a")
        with self.store.transaction() as database:
            database.execute(
                "UPDATE projects SET monitor_cursor=(SELECT MAX(id) FROM events) WHERE id='a'"
            )
            database.execute(
                "INSERT INTO holds VALUES('hold','a',?,'Blocking finding',0,NULL,NULL)",
                (plan["planner_task"],),
            )
        autonomy.advance(self.store)
        self.assertEqual(self.store.plan(plan["id"])["status"], "reviewed")
        self.store.resolve_hold("hold", "Checked")
        with self.store.transaction() as database:
            database.execute(
                "UPDATE projects SET monitor_cursor=(SELECT MAX(id) FROM events) WHERE id='a'"
            )
        autonomy.advance(self.store)
        self.assertEqual(self.store.plan(plan["id"])["status"], "approved")
        self.assertEqual(
            next(event for event in self.store.events("a") if event["kind"] == "plan.approved")[
                "payload"
            ]["reason"],
            autonomy.REASON,
        )

    def test_acceptance_unlocks_dependent_plan_work(self):
        plan, _ = self.reviewed_plan()
        with self.store.transaction() as database:
            database.execute("UPDATE plans SET status='approved' WHERE id=?", (plan["id"],))
            for node_id, dependencies in (("first", []), ("next", ["first"])):
                node = {
                    "id": node_id,
                    "title": "Inspect",
                    "description": "Inspect source",
                    "kind": "work",
                    "depends_on": dependencies,
                    "acceptance_criteria": ["Report saved"],
                    "mode": "read",
                }
                database.execute(
                    "INSERT INTO graph_nodes(plan_id,node_id,specification) VALUES(?,?,?)",
                    (plan["id"], node_id, encode(node)),
                )
        self.workers.seed_plan(plan["id"])
        requests = {request["node_id"]: request for request in self.workers.list("a")}
        for request in requests.values():
            self.workers.select("a", request["id"], CHOICE)
        self.workers.dispatch_ready()
        task = self.store.claim_next(3)
        self.assertEqual(task["worker_request_id"], requests["first"]["id"])
        self.store.finish(task["id"], task["token"], text=encode(REPORT))
        self.workers.process_result(self.store.task(task["id"]))
        self.workers.dispatch_ready()
        self.assertIsNone(self.workers.get(requests["next"]["id"])["task_id"])
        autonomy.advance(self.store)
        self.workers.dispatch_ready()
        self.assertIsNotNone(self.workers.get(requests["next"]["id"])["task_id"])

    def test_explicit_confirmation_never_written_even_for_selected_team(self):
        request = self.selected()
        profile = {"team": [PROFILE, PROFILE], "requires_approval": True}
        with self.store.transaction() as database:
            database.execute(
                "UPDATE worker_requests SET profile_json=? WHERE id=?",
                (encode(profile), request["id"]),
            )
        autonomy.advance(self.store)
        self.assertIsNone(self.workers.get(request["id"])["approval"])
        with self.store.transaction() as database:
            database.execute(
                "UPDATE worker_requests SET state='candidate',result_json=? WHERE id=?",
                (encode(REPORT), request["id"]),
            )
        autonomy.advance(self.store)
        current = self.workers.get(request["id"])
        self.assertEqual(current["state"], "candidate")
        self.assertIsNone(current["approval"])
        self.assertIn("approval", self.waits()[0]["payload"]["reason"].lower())

    def test_team_children_are_not_accepted(self):
        parent = self.selected()
        with self.store.transaction() as database:
            database.execute(
                "UPDATE worker_requests SET profile_json=? WHERE id=?",
                (encode({"team": [PROFILE, PROFILE]}), parent["id"]),
            )
            teams.start(
                self.workers,
                database,
                self.workers._request(database, parent["id"]),
                baseline_commit="a" * 40,
            )
            children = teams.members(database, parent["id"])
            for child in children:
                database.execute(
                    "UPDATE worker_requests SET state='candidate',result_json=? WHERE id=?",
                    (encode(REPORT), child["child_request_id"]),
                )
        autonomy.advance(self.store)
        for child in children:
            self.assertEqual(self.workers.get(child["child_request_id"])["state"], "candidate")
        self.assertEqual(self.waits(), [])


if __name__ == "__main__":
    unittest.main()
