"""Exercise the public CLI and real supervisor/runner processes without provider calls."""

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path

from orchestrator.runtime import process_identity
from orchestrator.store import Store

ROOT = Path(__file__).resolve().parents[1]


class LifecycleE2ETests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)
        self.store = Store(self.home)
        self.project = self.home / "source-project"
        self.project.mkdir()
        self.store.add_project("example", str(self.project))
        self.session_id = str(uuid.uuid4())
        self.store.open_session(self.session_id, "claude", "example")
        self.environment = {
            **os.environ,
            "ORCHESTRATOR_HOME": str(self.home),
            "ORCHESTRATOR_SESSION_ID": self.session_id,
            "PYTHONPATH": str(ROOT),
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
        }
        self.cli = [sys.executable, str(ROOT / "bin/orchestrator"), "--home", str(self.home)]

    def command(self, *arguments, payload=None, check=True):
        result = subprocess.run(
            [*self.cli, *arguments],
            input=json.dumps(payload) if payload is not None else None,
            capture_output=True,
            text=True,
            timeout=20,
            env=self.environment,
            check=False,
        )
        if check:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def test_native_clear_keeps_durable_project_binding(self):
        self.command(
            "hooks", "SessionEnd", payload={"session_id": self.session_id, "reason": "clear"}
        )
        self.assertTrue(self.store.session(self.session_id)["active"])
        native_session = str(uuid.uuid4())
        result = self.command(
            "hooks", "SessionStart", payload={"session_id": native_session, "source": "clear"}
        )
        context = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertIn(self.session_id, context)
        self.assertIn("example", context)
        self.command(
            "hooks",
            "UserPromptSubmit",
            payload={"session_id": native_session, "prompt": "Change the acceptance criteria"},
        )
        events = self.store.events("example")
        self.assertEqual(events[-1]["session_id"], self.session_id)
        self.assertEqual(events[-1]["kind"], "user.message")

    def test_large_prompt_file_transport(self):
        prompt = "Please inspect this requirement: " + "x" * 200_000
        payload_path = self.home / "large-payload.json"
        payload_path.write_text(json.dumps({"prompt": prompt}))
        result = self.command(
            "request",
            "--session",
            self.session_id,
            "--action",
            "record_prompt",
            "--payload-file",
            str(payload_path),
        )
        self.assertTrue(json.loads(result.stdout)["review_required"])
        saved_files = list(
            (self.store.data / "sessions" / self.session_id / "prompts").glob("*.json")
        )
        self.assertEqual(json.loads(saved_files[0].read_text())["prompt"], prompt)

    def install_fake_harnesses(self):
        executable = self.home / "harness"
        executable.write_text("""#!/usr/bin/env python3
import json, os, sys, uuid
from pathlib import Path
prompt = sys.stdin.read()
role = 'worker' if prompt.startswith('# Worker') else 'monitor' if prompt.startswith('# Monitor') else 'critic' if prompt.startswith('# Critic') else 'planner'
with open(os.environ['ORCH_TEST_CALLS'], 'a') as log:
    log.write(json.dumps({'role': role, 'cwd': os.getcwd(), 'argv': sys.argv[1:-1]}) + '\\n')
if role == 'worker':
    if os.environ.get('ORCH_TEST_WORKER_WRITE') == '1':
        Path('file.txt').write_text('candidate implementation')
    output = {'summary': 'Inspected fixture', 'changes': [], 'checks': ['Read fixture task'], 'remaining_issues': []}
elif role == 'monitor':
    evidence = json.loads(prompt.split('Task evidence/request:\\n', 1)[1])
    output = {'reviewed_through': evidence['reviewed_through'], 'findings': []}
    selections = []
    for request in evidence.get('worker_requests', []):
        profile = request.get('policy', {}).get('policy', {}).get('default')
        if profile:
            selections.append({'request_id': request['id'], 'choice': {
                'rule': 'default', 'model': profile['model'], 'effort': profile['effort'],
                'rationale': 'Configured profile for this planned fixture task'}})
    if selections: output['worker_selections'] = selections
elif role == 'critic':
    output = {'verdict': 'approved', 'findings': []}
else:
    output = json.loads(Path(os.environ['ORCH_TEST_WORKFLOW']).read_text())
print(json.dumps({'type':'result','subtype':'success','is_error':False,'session_id':str(uuid.uuid4()),'result':json.dumps(output),'total_cost_usd':0.001}))
""")
        executable.chmod(0o700)
        registry = self.home / "machine-resources"
        registry.write_text("""#!/usr/bin/env python3
import os, sys
if sys.argv[1]=='status': sys.exit(0)
args=sys.argv[sys.argv.index('--')+1:]
os.execv(args[0],args)
""")
        registry.chmod(0o700)
        self.environment.update(
            {
                "PATH": str(self.home) + os.pathsep + os.environ["PATH"],
                "ORCH_TEST_CALLS": str(self.home / "calls.jsonl"),
                "ORCH_TEST_WORKFLOW": str(ROOT / "workflows/research-review.json"),
            }
        )
        (self.home / "config").mkdir()
        (self.home / "config/local.toml").write_text(
            f"[adapters.claude]\ncommand=[{json.dumps(str(executable))}]\n"
            '[roles.critic]\nadapter="claude"\nprovider="anthropic"\nmodel="claude-fixture-critic"\n'
            "[supervisor]\npoll_seconds=0.1\nheartbeat_seconds=0.1\nstale_seconds=3\nmonitor_interval_seconds=0.1\n"
            "task_timeout_seconds=10\n"
        )

    def cleanup_service(self):
        # Read durable identities before freezing: a runner may hold the launch
        # transaction, and freezing it while opening SQLite would deadlock cleanup.
        # Freezing the supervisor first below still prevents fresh child launches;
        # recursively capturing its children covers jobs newer than this snapshot.
        record = json.loads(self.store.service_value("supervisor", "{}"))
        tasks = self.store.tasks()
        harnesses = {
            task["id"]: json.loads(self.store.service_value("harness:" + task["id"], "{}"))
            for task in tasks
        }
        owned_processes = {}

        def freeze_tree(pid, identity):
            if not identity or pid in owned_processes or process_identity(pid) != identity:
                return
            try:
                os.kill(pid, signal.SIGSTOP)
            except ProcessLookupError:
                return
            owned_processes[pid] = identity
            try:
                children = Path(f"/proc/{pid}/task/{pid}/children").read_text().split()
            except FileNotFoundError:
                children = []
            for child in children:
                child_pid = int(child)
                freeze_tree(child_pid, process_identity(child_pid))

        if record:
            freeze_tree(record["pid"], record["identity"])
        # Also recover this test's detached runners if its supervisor already died.
        for task in tasks:
            if task["runner_pid"]:
                freeze_tree(task["runner_pid"], task["runner_identity"])
            harness = harnesses[task["id"]]
            if harness:
                freeze_tree(harness["pid"], harness["identity"])
        for pid, identity in reversed(list(owned_processes.items())):
            if process_identity(pid) == identity:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        deadline = time.monotonic() + 5
        while any(process_identity(pid) == identity for pid, identity in owned_processes.items()):
            if time.monotonic() >= deadline:
                self.fail("Test subprocesses did not exit before removing their private state")
            time.sleep(0.02)

    def test_worker_runs_as_background_task_and_requires_acceptance(self):
        self.install_fake_harnesses()
        self.addCleanup(self.cleanup_service)
        policy = self.project / ".orchestrator" / "crew-dispatch.json"
        policy.parent.mkdir()
        policy.write_text(
            json.dumps(
                {
                    "default": {
                        "harness": "claude",
                        "model": "claude-fixture-worker",
                        "effort": "low",
                    }
                }
            )
        )

        def operation(action, payload):
            return json.loads(
                self.command(
                    "request",
                    "--session",
                    self.session_id,
                    "--action",
                    action,
                    "--payload",
                    json.dumps(payload),
                ).stdout
            )

        origin = operation("record_prompt", {"prompt": "Inspect the project independently"})[
            "event_id"
        ]
        worker = operation(
            "request_worker", {"brief": "Inspect project files", "origin_event_id": origin}
        )
        selected = operation(
            "select_worker",
            {
                "request_id": worker["id"],
                "choice": {
                    "rule": "default",
                    "model": "claude-fixture-worker",
                    "effort": "low",
                    "rationale": "The configured default covers unrelated inspection",
                },
            },
        )
        self.assertEqual(selected["profile"]["model"], "claude-fixture-worker")
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            worker = operation("worker", {"request_id": worker["id"]})
            if worker["state"] in {"candidate", "failed", "unknown", "cancelled"}:
                break
            time.sleep(0.1)
        self.assertEqual(worker["state"], "candidate", worker)
        task = self.store.task(worker["task_id"])
        self.assertEqual(task["state"], "succeeded")
        self.assertIsNotNone(task["runner_pid"])
        self.assertEqual(task["role"], "worker")
        self.assertEqual(len([item for item in self.store.tasks() if item["role"] == "worker"]), 1)
        self.command("accept-worker", worker["id"], "--reason", "Reviewed the saved fixture result")
        accepted = operation("worker", {"request_id": worker["id"]})
        self.assertEqual(accepted["state"], "accepted")

    def test_authorized_write_creates_signed_candidate_without_modifying_source(self):
        self.install_fake_harnesses()
        self.addCleanup(self.cleanup_service)
        self.environment["ORCH_TEST_WORKER_WRITE"] = "1"
        key = self.home / "signing"
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True)
        signers = self.home / "allowed_signers"
        signers.write_text("worker@example.test " + key.with_suffix(".pub").read_text())
        settings = {
            "user.name": "Worker Test",
            "user.email": "worker@example.test",
            "gpg.format": "ssh",
            "user.signingkey": str(key),
            "commit.gpgsign": "true",
            "gpg.ssh.allowedSignersFile": str(signers),
        }
        self.environment["GIT_CONFIG_COUNT"] = str(len(settings))
        for index, (name, value) in enumerate(settings.items()):
            self.environment[f"GIT_CONFIG_KEY_{index}"] = name
            self.environment[f"GIT_CONFIG_VALUE_{index}"] = value

        def git(*arguments):
            return (
                subprocess.check_output(
                    ["git", "-c", "core.hooksPath=/dev/null", *arguments],
                    cwd=self.project,
                    env=self.environment,
                    stderr=subprocess.PIPE,
                )
                .decode()
                .strip()
            )

        git("init", "-q")
        (self.project / "file.txt").write_text("original")
        policy = self.project / ".orchestrator" / "crew-dispatch.json"
        policy.parent.mkdir()
        policy.write_text(
            json.dumps(
                {
                    "default": {
                        "harness": "claude",
                        "model": "claude-fixture-worker",
                        "effort": "high",
                    }
                }
            )
        )
        git("add", ".")
        git("commit", "-qm", "Initial fixture")
        source_head = git("rev-parse", "HEAD")

        def operation(action, payload):
            return json.loads(
                self.command(
                    "request",
                    "--session",
                    self.session_id,
                    "--action",
                    action,
                    "--payload",
                    json.dumps(payload),
                ).stdout
            )

        origin = operation("record_prompt", {"prompt": "Make an unrelated file change"})["event_id"]
        worker = operation(
            "request_worker",
            {"brief": "Edit the fixture", "mode": "write", "origin_event_id": origin},
        )
        worker = operation(
            "select_worker",
            {
                "request_id": worker["id"],
                "choice": {
                    "rule": "default",
                    "model": "claude-fixture-worker",
                    "effort": "high",
                    "rationale": "Use configured fixture",
                },
            },
        )
        self.assertIsNone(operation("worker", {"request_id": worker["id"]})["task_id"])
        self.command("approve-worker", worker["id"], "--reason", "Authorize isolated fixture edit")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            worker = operation("worker", {"request_id": worker["id"]})
            if worker["state"] in {"candidate", "failed", "unknown"}:
                break
            time.sleep(0.1)
        self.assertEqual(worker["state"], "candidate", worker)
        self.assertEqual((self.project / "file.txt").read_text(), "original")
        self.assertEqual(git("rev-parse", "HEAD"), source_head)
        self.assertEqual(git("status", "--porcelain"), "")
        workspace = worker["workspace"]
        self.assertEqual(
            (Path(workspace["path"]) / "file.txt").read_text(), "candidate implementation"
        )
        git("verify-commit", workspace["commit"])
        self.assertIn("candidate implementation", Path(workspace["diff_path"]).read_text())

    def test_monitor_selects_planned_workers_and_acceptance_releases_dependencies(self):
        self.install_fake_harnesses()
        self.addCleanup(self.cleanup_service)
        policy = self.project / ".orchestrator" / "crew-dispatch.json"
        policy.parent.mkdir()
        policy.write_text(
            json.dumps(
                {
                    "default": {
                        "harness": "claude",
                        "model": "claude-fixture-worker",
                        "effort": "high",
                    }
                }
            )
        )
        graph = {
            "summary": "Two dependent inspections",
            "assumptions": [],
            "risks": [],
            "questions": [],
            "nodes": [
                {
                    "id": node,
                    "title": node,
                    "description": "Inspect files",
                    "kind": "work",
                    "depends_on": dependencies,
                    "acceptance_criteria": ["Review saved report"],
                }
                for node, dependencies in [("first", []), ("second", ["first"])]
            ],
        }
        fixture = self.home / "graph.json"
        fixture.write_text(json.dumps(graph))
        self.environment["ORCH_TEST_WORKFLOW"] = str(fixture)

        def operation(action, payload):
            return json.loads(
                self.command(
                    "request",
                    "--session",
                    self.session_id,
                    "--action",
                    action,
                    "--payload",
                    json.dumps(payload),
                ).stdout
            )

        operation("record_prompt", {"prompt": "Plan two dependent inspections"})
        plan = operation("start_plan", {"request": "Plan two dependent inspections"})["plan"]
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if (
                self.store.plan(plan["id"])["status"] == "reviewed"
                and self.command("approve", plan["id"], check=False).returncode == 0
            ):
                break
            time.sleep(0.1)
        else:
            self.fail("Plan was not approved")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            workers = {worker["node_id"]: worker for worker in operation("workers", {})["workers"]}
            if workers.get("first", {}).get("state") == "candidate":
                break
            time.sleep(0.1)
        self.assertEqual(workers["first"]["state"], "candidate", workers)
        self.assertTrue(workers["first"]["selection_source"].startswith("monitor:"))
        self.assertIsNone(workers["second"]["task_id"], workers)
        self.command("accept-worker", workers["first"]["id"], "--reason", "Checked first result")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            second = operation("worker", {"request_id": workers["second"]["id"]})
            if second["state"] == "candidate":
                break
            time.sleep(0.1)
        self.assertEqual(second["state"], "candidate", second)
        self.assertTrue(second["selection_source"].startswith("monitor:"))
        self.assertEqual(
            self.store.graph_snapshot(plan["id"])["states"],
            {"first": "completed", "second": "awaiting_review"},
        )

    def test_large_valid_plan_node_does_not_stop_shared_supervisor(self):
        self.install_fake_harnesses()
        self.addCleanup(self.cleanup_service)
        graph = {
            "summary": "Large valid task",
            "assumptions": [],
            "risks": [],
            "questions": [],
            "nodes": [
                {
                    "id": "large",
                    "title": "Inspect",
                    "description": "Inspect source",
                    "kind": "work",
                    "depends_on": [],
                    "acceptance_criteria": ["x" * 8192] * 13,
                }
            ],
        }
        fixture = self.home / "large-workflow.json"
        fixture.write_text(json.dumps(graph))
        self.environment["ORCH_TEST_WORKFLOW"] = str(fixture)
        self.command(
            "request",
            "--session",
            self.session_id,
            "--action",
            "record_prompt",
            "--payload",
            json.dumps({"prompt": "Plan the large inspection task"}),
        )
        plan = json.loads(
            self.command(
                "request",
                "--session",
                self.session_id,
                "--action",
                "start_plan",
                "--payload",
                json.dumps({"request": "Plan the large inspection task"}),
            ).stdout
        )["plan"]
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if (
                self.store.plan(plan["id"])["status"] == "reviewed"
                and self.command("approve", plan["id"], check=False).returncode == 0
            ):
                break
            time.sleep(0.1)
        else:
            self.fail("Large plan never became approvable")
        from orchestrator.workers import WorkerService

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not WorkerService(self.store).list("example"):
            time.sleep(0.1)
        requests = WorkerService(self.store).list("example")
        self.assertEqual(len(requests), 1, self.store.snapshot("example"))
        self.assertGreater(len(requests[0]["brief"]), 100000)
        self.assertTrue(json.loads(self.command("service", "status").stdout)["running"])

    def test_full_planning_review_monitor_and_restart_recovery(self):
        self.install_fake_harnesses()
        self.addCleanup(self.cleanup_service)
        response = self.command(
            "request",
            "--session",
            self.session_id,
            "--action",
            "start_plan",
            "--payload",
            json.dumps({"request": "Plan a small research task with a graph."}),
        )
        plan_id = json.loads(response.stdout)["plan"]["id"]
        # Closing the frontend does not close or cancel its independent jobs.
        self.command("session", "close", self.session_id)
        deadline = time.monotonic() + 15
        approved = False
        while time.monotonic() < deadline:
            if self.store.plan(plan_id)["status"] == "reviewed":
                result = self.command("approve", plan_id, check=False)
                if result.returncode == 0:
                    approved = True
                    break
            time.sleep(0.1)
        self.assertTrue(approved, json.dumps(self.store.snapshot("example"), indent=2))
        graph = json.loads(self.command("graph", plan_id).stdout)
        self.assertTrue(graph["dispatch_enabled"])
        self.assertTrue(graph["readiness"]["ready"])
        calls = [json.loads(line) for line in (self.home / "calls.jsonl").read_text().splitlines()]
        self.assertEqual(sum(call["role"] == "planner" for call in calls), 1)
        self.assertEqual(sum(call["role"] == "critic" for call in calls), 1)
        self.assertGreaterEqual(sum(call["role"] == "monitor" for call in calls), 1)
        self.assertEqual(
            {task["role"] for task in self.store.tasks()}, {"planner", "critic", "monitor"}
        )
        # A new frontend gets unhandled results; reading never acknowledges them.
        replacement = str(uuid.uuid4())
        self.command("session", "open", "--id", replacement, "--project", "example")
        result = json.loads(
            self.command("request", "--session", replacement, "--action", "updates").stdout
        )
        self.assertTrue(result["updates"])
        self.assertEqual(len(result["updates"]), len(self.store.updates(replacement)))


if __name__ == "__main__":
    unittest.main()
