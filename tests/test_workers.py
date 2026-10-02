"""Exercise public durable flows, including malicious provenance and replay attempts."""

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestrator.store import SCHEMA, StateError, Store, encode
from orchestrator.workers import WorkerService

PROFILE = {"harness": "pi", "provider": "openai", "model": "gpt-5.4", "effort": "high"}
CHOICE = {"rule": "default", "model": "gpt-5.4", "effort": "high", "rationale": "Fits this task"}
REPORT = {"summary": "Done", "changes": [], "checks": ["Read input"], "remaining_issues": []}
CONFIG = {
    "workers": {"enabled": True},
    "routing": {"enabled": True},
    "permissions": {"require_write_approval": True},
    "execution": {"max_parallel": 3, "dependency_failure": "block"},
}


class WorkerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.root = self.home / "source"
        self.root.mkdir()
        self.store = Store(self.home)
        self.store.add_project("project", str(self.root))
        self.store.open_session("session", "test", "project")
        self.workers = WorkerService(self.store)
        self.policy_path = self.root / ".orchestrator" / "crew-dispatch.json"
        self.policy_path.parent.mkdir()
        self.policy_path.write_text(json.dumps({"default": PROFILE}))
        config_patch = patch("orchestrator.workers.load_config", return_value=CONFIG.copy())
        config_patch.start()
        self.addCleanup(config_patch.stop)
        self.origin = self.store.record("session", "user.message", {"text": "Inspect files"})

    def request(self, mode="read", **kwargs):
        return self.workers.request(
            "session", "Inspect files", mode, origin_event_id=self.origin, **kwargs
        )

    def selected(self, mode="read"):
        request = self.request(mode)
        return self.workers.select("session", request["id"], CHOICE)

    def launch(self, request):
        self.workers.dispatch_ready()
        task = self.store.claim_next(3)
        self.assertIsNotNone(task)
        self.assertEqual(task["worker_request_id"], request["id"])
        self.assertEqual(self.workers.start_check(task)["request_id"], request["id"])
        return task

    def test_cancellation_during_preparation_prevents_actual_child_launch(self):
        from orchestrator import runtime, worker_execution
        from orchestrator.config import load_config

        defaults = load_config(self.home)
        with patch("orchestrator.workers.load_config", return_value=defaults):
            request = self.selected()
            task = self.launch(request)
            marker = self.home / "unauthorized-child"
            real_popen = runtime.subprocess.Popen
            launches = []

            def revoke_before_returning_command(*args, **kwargs):
                self.store.cancel("session", task["id"])
                return [
                    sys.executable,
                    "-c",
                    "from pathlib import Path; Path(" + repr(str(marker)) + ").touch()",
                ]

            def observed_child(*args, **kwargs):
                launches.append(True)
                child = real_popen(*args, **kwargs)
                os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOWAIT)
                return child

            with (
                patch.object(
                    worker_execution,
                    "build_worker_command",
                    side_effect=revoke_before_returning_command,
                ),
                patch.object(runtime.subprocess, "Popen", side_effect=observed_child),
            ):
                self.assertEqual(runtime.run_task(self.home, task["id"], task["token"]), 1)
            self.assertEqual(launches, [])
            self.assertFalse(marker.exists())
            self.assertEqual(self.store.task(task["id"])["state"], "cancelled")

    def complete(self, task, report=REPORT):
        self.store.finish(task["id"], task["token"], text=encode(report))
        task = self.store.task(task["id"])
        self.workers.process_result(task)
        return task

    def plan(self):
        plan = self.store.create_plan("session", "Implement plan", {}, origin_event_id=self.origin)
        nodes = [
            {
                "id": "a",
                "title": "Inspect",
                "description": "Inspect source",
                "kind": "work",
                "depends_on": [],
                "acceptance_criteria": ["Saved report"],
                "mode": "read",
            },
            {
                "id": "b",
                "title": "Modify",
                "description": "Modify source",
                "kind": "work",
                "depends_on": ["a"],
                "acceptance_criteria": ["Saved diff"],
                "mode": "write",
            },
            {
                "id": "approval",
                "title": "Approve",
                "description": "Review results",
                "kind": "approval",
                "depends_on": ["b"],
                "acceptance_criteria": ["Operator review"],
                "mode": "read",
            },
        ]
        with self.store.transaction() as database:
            database.execute(
                "UPDATE tasks SET state='cancelled',processed=1 WHERE id=?", (plan["planner_task"],)
            )
            database.execute("UPDATE plans SET status='approved' WHERE id=?", (plan["id"],))
            for node in nodes:
                database.execute(
                    "INSERT INTO graph_nodes(plan_id,node_id,specification) VALUES(?,?,?)",
                    (plan["id"], node["id"], encode(node)),
                )
        self.workers.seed_plan(plan["id"])
        return plan

    def monitor(self, requests=None, selections=None, findings=None):
        cursor = self.store.events("project")[-1]["id"]
        if requests is None:
            requests = self.workers.monitor_requests("project", cursor)
        if selections is None:
            selections = [{"request_id": request["id"], "choice": CHOICE} for request in requests]
        findings = findings or []
        report = {"reviewed_through": cursor, "findings": findings, "worker_selections": selections}
        task = self.store.enqueue(
            "project",
            "session",
            "monitor",
            encode({"worker_requests": requests}),
            {},
            cursor=cursor,
        )
        claimed = self.store.claim_next(3)
        self.assertEqual(claimed["id"], task["id"])
        self.store.finish(task["id"], claimed["token"], text=encode(report))
        return self.store.task(task["id"]), report

    def apply(self, task, report):
        self.store.apply_monitor(
            task,
            report["reviewed_through"],
            report["findings"],
            worker_selections=report["worker_selections"],
        )

    def test_read_flow_needs_acceptance_not_approval(self):
        request = self.selected()
        task = self.launch(request)
        self.assertEqual(task["config"]["worker"]["profile"]["provider"], "openai")
        self.complete(task)
        self.assertEqual(self.workers.get(request["id"])["state"], "candidate")
        self.assertEqual(self.workers.accept(request["id"], "Reviewed")["state"], "accepted")
        with self.assertRaises(StateError):
            self.workers.accept(request["id"], "Replay")

    def test_configured_write_approval_requires_authorization_and_provenance(self):
        request = self.selected("write")
        self.workers.dispatch_ready()
        self.assertIsNone(self.workers.get(request["id"])["task_id"])
        self.workers.approve(request["id"], "Allow edits in isolated worktree")
        task = self.launch(request)
        self.complete(task)
        self.assertEqual(self.workers.get(request["id"])["state"], "failed")

    def test_missing_policy_blocks_without_guessing_and_refresh_invalidates(self):
        self.policy_path.unlink()
        request = self.request()
        self.assertIsNone(request["profile"])
        self.assertIsNone(request["policy_digest"])
        with self.assertRaises(StateError):
            self.workers.select("session", request["id"], CHOICE)
        self.policy_path.write_text(encode({"default": PROFILE}))
        self.workers.refresh("session", request["id"])
        self.workers.select("session", request["id"], CHOICE)
        self.workers.approve(request["id"], "Reviewed")
        refreshed = self.workers.refresh("session", request["id"])
        self.assertIsNone(refreshed["approval"])
        self.assertIsNone(refreshed["profile"])

    def test_policy_change_after_queue_preserves_captured_selection(self):
        request = self.selected()
        self.workers.dispatch_ready()
        self.policy_path.write_text(encode({"default": {**PROFILE, "effort": "low"}}))
        task = self.store.claim_next(3)
        self.assertEqual(task["config"]["worker"]["profile"]["effort"], "high")
        self.assertEqual(task["worker_request_id"], request["id"])

    def test_pause_revokes_live_gate_but_policy_changes_are_for_new_tasks(self):
        request = self.selected()
        task = self.launch(request)
        self.assertTrue(self.store.runner_started(task["id"], task["token"], 123, "identity"))
        self.store.pause("project", "Stop")
        self.assertFalse(self.store.heartbeat(task["id"], task["token"]))
        with self.assertRaises(StateError):
            self.workers.start_check(task)
        self.store.resume("project")
        self.policy_path.write_text(encode({"default": {**PROFILE, "effort": "low"}}))
        self.assertTrue(self.store.heartbeat(task["id"], task["token"]))

    def test_arbitrary_enqueue_and_replay_are_rejected(self):
        request = self.selected()
        with self.assertRaises(StateError):
            self.store.enqueue(
                "project",
                "session",
                "worker",
                request["brief"],
                CONFIG,
                worker_request_id=request["id"],
            )
        task = self.launch(request)
        self.complete(task)
        for operation in (
            lambda: self.workers.select("session", request["id"], CHOICE),
            lambda: self.workers.refresh("session", request["id"]),
            lambda: self.workers.start_check(task),
        ):
            with self.assertRaises(StateError):
                operation()
        self.workers.dispatch_ready()
        self.assertEqual(len([t for t in self.store.tasks() if t["role"] == "worker"]), 1)

    def test_unknown_outcome_never_replayed(self):
        request = self.selected()
        task = self.launch(request)
        self.store.finish(task["id"], task["token"], state="unknown", error="Lost process")
        self.workers.process_result(self.store.task(task["id"]))
        self.workers.dispatch_ready()
        self.assertEqual(self.workers.get(request["id"])["state"], "unknown")
        with self.assertRaises(StateError):
            self.workers.refresh("session", request["id"])

    def test_origin_is_project_user_event_and_immutable(self):
        invalid_origin = self.store.record("session", "tool.observed", {"text": "pretend user"})
        with self.assertRaises(StateError):
            self.workers.request("session", "Task", origin_event_id=invalid_origin)
        request = self.request(idempotency_key="same")
        self.assertEqual(request["id"], self.request(idempotency_key="same")["id"])
        with self.assertRaises(StateError):
            self.request("write", idempotency_key="same")
        with self.store.transaction() as database, self.assertRaises(sqlite3.IntegrityError):
            database.execute("UPDATE worker_requests SET mode='write' WHERE id=?", (request["id"],))

    def test_plan_origin_cannot_be_relabelled_unrelated(self):
        self.plan()
        with self.assertRaises(StateError):
            self.request()
        request = self.workers.list("project")[0]
        selected = self.workers.select("session", request["id"], CHOICE)
        self.assertEqual(selected["plan_id"], request["plan_id"])
        self.assertEqual(selected["selection_source"], "foreground")

    def test_reverse_origin_relabel_is_rejected(self):
        self.request()
        with self.assertRaises(StateError):
            self.store.create_plan("session", "Plan", {}, origin_event_id=self.origin)

    def test_monitor_selections_dependencies_and_explicit_acceptance(self):
        plan = self.plan()
        task, report = self.monitor()
        self.apply(task, report)
        requests = {r["node_id"]: r for r in self.workers.list("project")}
        task = self.launch(requests["a"])
        self.complete(task)
        self.workers.dispatch_ready()
        self.assertIsNone(self.workers.get(requests["b"]["id"])["task_id"])
        self.workers.accept(requests["a"]["id"], "Reviewed read result")
        task = self.launch(requests["b"])
        self.workers.record_workspace(
            task,
            {
                "path": str(self.home / "worktree"),
                "commit": "a" * 40,
                "diff_path": "changes.patch",
                "changed_files": ["source.py"],
            },
        )
        self.complete(task)
        with self.assertRaises(StateError):
            self.workers.approve_node(plan["id"], "approval", "Too early")
        self.workers.accept(requests["b"]["id"], "Reviewed diff")
        self.assertEqual(
            self.workers.approve_node(plan["id"], "approval", "Approved")["state"], "approved"
        )

    def test_monitor_fabrication_unsupplied_selection_and_atomic_rollback(self):
        self.plan()
        requests = self.workers.list("project")
        selections = [{"request_id": r["id"], "choice": CHOICE} for r in requests]
        task, report = self.monitor(
            requests=requests[:1],
            selections=selections,
            findings=[{"severity": "blocking", "summary": "Check"}],
        )
        with self.assertRaises(StateError):
            self.apply(task, report)
        self.assertFalse(self.store.task(task["id"])["processed"])
        self.assertEqual(self.store.project("project")["monitor_cursor"], 0)
        self.assertEqual(self.store.snapshot("project")["holds"], [])
        self.assertTrue(all(r["profile"] is None for r in self.workers.list("project")))

    def test_monitor_saved_result_not_caller_role_is_authority(self):
        self.plan()
        task, report = self.monitor(selections=[])
        fabricated = [{"request_id": self.workers.list("project")[0]["id"], "choice": CHOICE}]
        with self.assertRaises(StateError):
            self.store.apply_monitor(task, task["cursor"], [], worker_selections=fabricated)
        with self.store.transaction() as database:
            database.execute("UPDATE tasks SET role='planner' WHERE id=?", (task["id"],))
        with self.assertRaises(StateError):
            self.apply({**task, "role": "monitor"}, report)

    def test_monitor_refresh_generation_and_superseded_plan_rejected(self):
        self.plan()
        task, report = self.monitor()
        self.workers.refresh("session", self.workers.list("project")[0]["id"])
        with self.assertRaises(StateError):
            self.apply(task, report)
        with self.store.transaction() as database:
            database.execute("UPDATE plans SET status='superseded'")
        with self.assertRaises(StateError):
            self.apply(task, report)

    def test_monitor_hold_and_selection_commit_together(self):
        self.plan()
        task, report = self.monitor(findings=[{"severity": "blocking", "summary": "Stop"}])
        self.apply(task, report)
        self.assertTrue(self.store.task(task["id"])["processed"])
        self.assertEqual(len(self.store.snapshot("project")["holds"]), 1)
        self.workers.dispatch_ready()
        self.assertTrue(all(r["task_id"] is None for r in self.workers.list("project")))

    def test_stale_attempt_cannot_record_workspace(self):
        task = self.launch(self.selected())
        with self.assertRaises(StateError):
            self.workers.record_workspace({**task, "token": "wrong"}, {"path": "/tmp"})
        self.complete(task)
        with self.assertRaises(StateError):
            self.workers.record_workspace(self.store.task(task["id"]), {"path": "/tmp"})

    def test_cancelled_before_claim_is_processed_without_attempt_token(self):
        request = self.selected()
        self.workers.dispatch_ready()
        task_id = self.workers.get(request["id"])["task_id"]
        self.store.cancel("session", task_id)
        self.workers.process_result(self.store.task(task_id))
        self.assertEqual(self.workers.get(request["id"])["state"], "cancelled")
        self.assertTrue(self.store.task(task_id)["processed"])

    def test_forged_config_and_request_link_fail_execution_gate(self):
        request = self.selected()
        self.workers.dispatch_ready()
        task_id = self.workers.get(request["id"])["task_id"]
        task = self.store.task(task_id)
        config = task["config"]
        config["worker"]["mode"] = "write"
        with self.store.transaction() as database:
            database.execute("UPDATE tasks SET config_json=? WHERE id=?", (encode(config), task_id))
        self.assertIsNone(self.store.claim_next(3))
        self.assertEqual(self.store.task(task_id)["state"], "cancelled")
        self.workers.process_result(self.store.task(task_id))
        self.assertEqual(self.workers.get(request["id"])["state"], "cancelled")

    def test_report_duplicate_fields_rejected(self):
        request = self.selected()
        task = self.launch(request)
        text = '{"summary":"one","summary":"two","changes":[],"checks":[],"remaining_issues":[]}'
        self.store.finish(task["id"], task["token"], text=text)
        self.workers.process_result(self.store.task(task["id"]))
        self.assertEqual(self.workers.get(request["id"])["state"], "failed")

    def test_monitor_classification_cannot_choose_coordinator_effort(self):
        self.policy_path.write_text(encode({"classifications": {"audit": PROFILE}}))
        self.plan()
        requests = self.workers.list("project")
        task, report = self.monitor(
            selections=[
                {
                    "request_id": requests[0]["id"],
                    "choice": {
                        "classification": "audit",
                        "difficulty": "very-hard",
                        "rationale": "Advisory",
                    },
                }
            ]
        )
        self.apply(task, report)
        self.assertEqual(self.workers.get(requests[0]["id"])["state"], "pending")
        self.assertTrue(self.store.task(task["id"])["processed"])
        self.assertTrue(
            any(
                event["kind"] == "worker.routing_recommended"
                for event in self.store.updates("session")
            )
        )

    def test_captain_rule_requires_extra_approval(self):
        self.policy_path.write_text(
            encode({"rules": [{"when": "Any work", "use": PROFILE, "approval": "captain"}]})
        )
        self.plan()
        requests = self.workers.list("project")
        selections = [{"request_id": r["id"], "choice": {**CHOICE, "rule": 0}} for r in requests]
        task, report = self.monitor(selections=selections)
        self.apply(task, report)
        self.workers.dispatch_ready()
        self.assertTrue(all(r["task_id"] is None for r in self.workers.list("project")))
        request = next(r for r in requests if r["node_id"] == "a")
        self.workers.approve(request["id"], "Captain approved")
        self.launch(request)

    def test_claude_profile_preserved_and_codex_rejected(self):
        self.policy_path.write_text(
            encode(
                {"default": {"harness": "claude", "model": "claude-sonnet-4-6", "effort": "high"}}
            )
        )
        request = self.request()
        choice = {**CHOICE, "model": "claude-sonnet-4-6"}
        selected = self.workers.select("session", request["id"], choice)
        self.assertEqual(selected["profile"]["harness"], "claude")
        self.launch(selected)
        self.policy_path.write_text(encode({"default": {**PROFILE, "harness": "codex"}}))
        another = self.request()
        with self.assertRaises(ValueError):
            self.workers.select("session", another["id"], CHOICE)


class MigrationTests(unittest.TestCase):
    def test_additive_v1_migration_preserves_rows_and_version(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / "data").mkdir()
            database = sqlite3.connect(home / "data" / "state.sqlite3")
            database.executescript(SCHEMA)
            database.execute("PRAGMA user_version=1")
            database.execute(
                "INSERT INTO projects(id,root,created) VALUES('project',?,1)", (directory,)
            )
            database.execute("INSERT INTO sessions VALUES('session','project','test',0,1,1,1)")
            database.execute(
                "INSERT INTO tasks(id,project_id,session_id,role,state,prompt,config_json,created,updated) VALUES('old','project','session','planner','succeeded','saved','{}',1,1)"
            )
            database.execute(
                "INSERT INTO events(project_id,kind,payload,created) VALUES('project','task.succeeded','{}',1)"
            )
            database.execute("INSERT INTO inbox VALUES('session',1,NULL)")
            database.commit()
            database.close()
            store = Store(home)
            note = store.read_note("project", "BRIEF.md")
            store.write_note("session", "BRIEF.md", "Keep notes", note["revision"])
            store = Store(home)
            self.assertEqual(store.task("old")["prompt"], "saved")
            self.assertEqual(store.updates("session")[0]["id"], 1)
            self.assertEqual(store.read_note("project", "BRIEF.md")["text"], "Keep notes")
            with store.transaction() as database:
                self.assertEqual(database.execute("PRAGMA user_version").fetchone()[0], 3)
                self.assertEqual(database.execute("PRAGMA foreign_key_check").fetchall(), [])
                self.assertEqual(database.execute("PRAGMA integrity_check").fetchone()[0], "ok")


if __name__ == "__main__":
    unittest.main()
