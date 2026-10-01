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
role = 'monitor' if prompt.startswith('# Monitor') else 'critic' if prompt.startswith('# Critic') else 'planner'
with open(os.environ['ORCH_TEST_CALLS'], 'a') as log:
    log.write(json.dumps({'role': role, 'cwd': os.getcwd(), 'argv': sys.argv[1:-1]}) + '\\n')
if role == 'monitor':
    evidence = json.loads(prompt.split('Task evidence/request:\\n', 1)[1])
    output = {'reviewed_through': evidence['reviewed_through'], 'findings': []}
elif role == 'critic':
    output = {'verdict': 'approved', 'findings': []}
else:
    output = json.loads(Path(os.environ['ORCH_TEST_WORKFLOW']).read_text())
if '--output-format' in sys.argv:
    print(json.dumps({'type':'result','subtype':'success','is_error':False,'session_id':str(uuid.uuid4()),'result':json.dumps(output),'total_cost_usd':0.001}))
else:
    print(json.dumps({'type':'thread.started','thread_id':str(uuid.uuid4())}))
    print(json.dumps({'type':'item.completed','item':{'id':'answer','type':'agent_message','text':json.dumps(output)}}))
    print(json.dumps({'type':'turn.completed','usage':{'input_tokens':1,'output_tokens':1}}))
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
            f"[adapters.codex]\ncommand=[{json.dumps(str(executable))}]\n"
            "[supervisor]\npoll_seconds=0.1\nheartbeat_seconds=0.1\nstale_seconds=3\nmonitor_interval_seconds=0.1\n"
            "[roles.planner]\ntimeout_seconds=10\n[roles.critic]\ntimeout_seconds=10\n[roles.monitor]\ntimeout_seconds=10\n"
        )

    def cleanup_service(self):
        # Only identity-checked processes recorded in this test's private home are ours.
        for task in self.store.tasks():
            record = json.loads(self.store.service_value("harness:" + task["id"], "{}"))
            if record and process_identity(record["pid"]) == record["identity"]:
                try:
                    os.killpg(record["pid"], signal.SIGKILL)
                except ProcessLookupError:
                    pass
            if (
                task["runner_pid"]
                and process_identity(task["runner_pid"]) == task["runner_identity"]
            ):
                try:
                    os.kill(task["runner_pid"], signal.SIGKILL)
                except ProcessLookupError:
                    pass
        record = json.loads(self.store.service_value("supervisor", "{}"))
        if record and process_identity(record["pid"]) == record["identity"]:
            try:
                os.kill(record["pid"], signal.SIGTERM)
            except ProcessLookupError:
                pass
            deadline = time.monotonic() + 3
            while (
                process_identity(record["pid"]) == record["identity"]
                and time.monotonic() < deadline
            ):
                time.sleep(0.02)

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
        self.assertFalse(graph["dispatch_enabled"])
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
