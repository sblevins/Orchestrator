"""Real-process runtime tests. Fake harnesses never contact a model provider."""

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestrator.config import load_config
from orchestrator.runtime import (
    _process_results,
    _reconcile,
    _resume_session,
    _strict_json,
    ensure_supervisor,
    process_identity,
)
from orchestrator.store import Store

ROOT = Path(__file__).resolve().parent.parent


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.install = self.root / "install"
        shutil.copytree(ROOT / "orchestrator", self.install / "orchestrator")
        shutil.copytree(ROOT / "config", self.install / "config")
        shutil.copytree(ROOT / "roles", self.install / "roles")
        (self.install / "orchestrator/adapters.py").write_text("""
import json, sys
def build_command(config, role, prompt, cwd, output_path, session_id=None, *,
                  project_root=None, stdin_prompt=False):
    assert project_root is not None and stdin_prompt
    return [sys.executable, config['adapters']['claude']['command'][0]]
def parse_result(adapter, stdout, returncode):
    if returncode: raise ValueError('harness failed')
    return json.loads(stdout)
""")
        (self.install / "orchestrator/graphs.py").write_text(
            "def load_workflow(home, name): return {'name': name}\n"
        )
        self.harness = self.root / "harness.py"
        self.harness.write_text("""
import json, os, sys, time
prompt = sys.stdin.read()
assert os.environ['ORCHESTRATOR_CHILD'] == '1'
assert 'ORCHESTRATOR_SESSION_ID' not in os.environ
assert 'ORCHESTRATOR_FRONTEND' not in os.environ
assert 'CLAUDECODE' not in os.environ
assert prompt
if 'SLOW' in prompt: time.sleep(5)
if 'WAIT' in prompt: time.sleep(0.7)
if 'FLOOD' in prompt: print('x' * (9 * 1024 * 1024)); sys.exit(0)
if 'BAD_EXIT' in prompt: sys.exit(4)
print(json.dumps({'text': '{}', 'session_id': 'conversation', 'cost_usd': 0}))
""")
        self.registry = self.root / "machine-resources"
        self.registry.write_text("""#!/usr/bin/env python3
import os, sys
if sys.argv[1] == 'status': sys.exit(0)
if os.environ.get('REJECT_RESERVATION'): sys.exit(int(os.environ['REJECT_RESERVATION']))
arguments = sys.argv[sys.argv.index('--') + 1:]
os.execv(arguments[0], arguments)
""")
        self.registry.chmod(0o700)
        self.home = self.root / "home"
        self.store = Store(self.home)
        self.store.add_project("project", str(self.root))
        self.store.open_session("session", "test", "project")
        with self.store.transaction() as database:
            database.execute("UPDATE projects SET next_monitor_at=?", (time.time() + 3600,))
        self.config = load_config(self.home)
        self.config["adapters"]["claude"]["command"] = [str(self.harness)]
        self.config["roles"]["planner"]["timeout_seconds"] = 2
        self.config["supervisor"]["heartbeat_seconds"] = 0.1
        self.config["supervisor"]["stale_seconds"] = 0.3
        self.environment = {
            **os.environ,
            "PYTHONPATH": str(self.install),
            "PATH": str(self.root) + os.pathsep + os.environ["PATH"],
            "ORCHESTRATOR_SESSION_ID": "parent",
            "CLAUDECODE": "nested",
            "ORCHESTRATOR_FRONTEND": "pi",
        }
        self.children = []

    def tearDown(self):
        for child in self.children:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=5)
        self.temporary.cleanup()

    def enqueue(self, prompt="normal"):
        return self.store.enqueue("project", "session", "planner", prompt, self.config)

    def spawn(self, *arguments):
        child = subprocess.Popen(
            [sys.executable, "-m", "orchestrator.runtime", *arguments, "--home", str(self.home)],
            cwd=self.install,
            env=self.environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.children.append(child)
        return child

    def runner(self, prompt="normal"):
        task = self.enqueue(prompt)
        task = self.store.claim_next(2)
        return task, self.spawn("run-task", "--task-id", task["id"], "--token", task["token"])

    def wait_state(self, task_id, states, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            task = self.store.task(task_id)
            if task["state"] in states:
                return task
            time.sleep(0.03)
        self.fail(f"Task never reached {states}: {self.store.task(task_id)}")

    def test_identity(self):
        self.assertIsNotNone(process_identity(os.getpid()))
        self.assertIsNone(process_identity(999999999))

    def test_success_prompt_and_private_logs(self):
        task, child = self.runner()
        stdout, stderr = child.communicate(timeout=5)
        self.assertEqual(child.returncode, 0, (stdout, stderr))
        finished = self.store.task(task["id"])
        self.assertEqual(finished["state"], "succeeded")
        self.assertEqual(finished["harness_session"], "conversation")
        directory = self.store.data / "runs" / task["id"] / task["token"]
        self.assertIn(str(self.root), (directory / "prompt.txt").read_text())
        self.assertEqual((directory / "output.log").stat().st_mode & 0o777, 0o600)

    def test_large_prompt_reaches_real_adapter_over_file_stdin(self):
        shutil.copyfile(
            ROOT / "orchestrator/adapters.py", self.install / "orchestrator/adapters.py"
        )
        self.harness.write_text("""#!/usr/bin/env python3
import json, pathlib, stat, sys
assert stat.S_ISREG(pathlib.Path('/proc/self/fd/0').stat().st_mode)
prompt = sys.stdin.read()
assert prompt == pathlib.Path('prompt.txt').read_text()
assert 'x' * 200_000 in prompt
assert all(len(argument) < 10_000 for argument in sys.argv)
if '-p' in sys.argv:
    project = pathlib.Path(sys.argv[sys.argv.index('--add-dir') + 1])
    assert (project / 'source.txt').read_text() == 'project source'
    print(json.dumps({'type':'result', 'subtype':'success', 'is_error':False,
                      'result':'{}', 'session_id':'conversation', 'total_cost_usd':0}))
else:
    assert sys.argv[-2:] == ['--', '-']
    assert '--ignore-user-config' in sys.argv and '--ignore-rules' in sys.argv
    assert sys.argv[sys.argv.index('-s') + 1] == 'read-only'
    print(json.dumps({'type':'thread.started', 'thread_id':'conversation'}))
    print(json.dumps({'type':'item.completed', 'item':
                      {'id':'final', 'type':'agent_message', 'text':'{}'}}))
    print(json.dumps({'type':'turn.completed', 'usage':{}}))
""")
        self.harness.chmod(0o700)
        (self.root / "source.txt").write_text("project source")
        for adapter in ("claude", "codex"):
            with self.subTest(adapter=adapter):
                self.config["roles"]["planner"]["adapter"] = adapter
                self.config["adapters"][adapter]["command"] = [str(self.harness)]
                task, child = self.runner("x" * 200_000)
                stdout, stderr = child.communicate(timeout=5)
                finished = self.store.task(task["id"])
                self.assertEqual(child.returncode, 0, (stdout, stderr, finished["error"]))
                self.assertEqual(finished["state"], "succeeded")

    def test_timeout_and_output_bound(self):
        for prompt in ("SLOW", "FLOOD", "BAD_EXIT"):
            with self.subTest(prompt=prompt):
                task, child = self.runner(prompt)
                child.communicate(timeout=5)
                self.assertEqual(child.returncode, 1)
                self.assertEqual(self.store.task(task["id"])["state"], "failed")
                log = self.store.data / "runs" / task["id"] / task["token"] / "output.log"
                self.assertLessEqual(log.stat().st_size, 8 * 1024 * 1024)
                harness = json.loads(self.store.service_value("harness:" + task["id"]))
                self.assertIsNone(process_identity(harness["pid"]))

    def test_cancel(self):
        task, child = self.runner("SLOW")
        self.wait_state(task["id"], {"running"})
        self.store.cancel("session", task["id"])
        child.communicate(timeout=5)
        self.assertEqual(self.store.task(task["id"])["state"], "cancelled")

    def test_duplicate_attempt_does_not_launch(self):
        task, first = self.runner("WAIT")
        self.wait_state(task["id"], {"running"})
        duplicate = self.spawn("run-task", "--task-id", task["id"], "--token", task["token"])
        duplicate.communicate(timeout=5)
        self.assertEqual(duplicate.returncode, 1)
        first.communicate(timeout=5)
        self.assertEqual(self.store.task(task["id"])["state"], "succeeded")

    def test_supervisor_restart_preserves_runner(self):
        task = self.enqueue("WAIT")
        first = self.spawn("supervise")
        running = self.wait_state(task["id"], {"running"})
        first.kill()
        first.communicate(timeout=5)
        second = self.spawn("supervise", "--once")
        second.communicate(timeout=5)
        finished = self.wait_state(task["id"], {"succeeded"})
        self.assertEqual(running["token"], finished["token"])
        self.assertEqual(running["runner_pid"], finished["runner_pid"])

    def test_singleton_lock_rejects_second_supervisor(self):
        first = self.spawn("supervise")
        deadline = time.monotonic() + 3
        while not self.store.service_value("supervisor") and time.monotonic() < deadline:
            time.sleep(0.02)
        owner = json.loads(self.store.service_value("supervisor"))
        second = self.spawn("supervise", "--once")
        second.communicate(timeout=5)
        self.assertEqual(second.returncode, 0)
        self.assertEqual(json.loads(self.store.service_value("supervisor"))["pid"], owner["pid"])
        self.assertIsNone(first.poll())

    def test_rejected_reservation_is_bounded_and_cannot_start_late(self):
        self.environment["REJECT_RESERVATION"] = "75"
        task = self.enqueue()
        first = self.spawn("supervise", "--once")
        first.communicate(timeout=5)
        starting = self.store.task(task["id"])
        time.sleep(0.4)
        _reconcile(self.store, {})
        self.assertEqual(self.store.task(task["id"])["state"], "failed")
        late = self.spawn("run-task", "--task-id", task["id"], "--token", starting["token"])
        late.communicate(timeout=5)
        self.assertEqual(late.returncode, 1)

    def test_dead_runner_is_unknown_not_replayed(self):
        task, child = self.runner("SLOW")
        self.wait_state(task["id"], {"running"})
        deadline = time.monotonic() + 3
        while not self.store.service_value("harness:" + task["id"]) and time.monotonic() < deadline:
            time.sleep(0.02)
        child.send_signal(signal.SIGKILL)
        child.communicate(timeout=5)
        time.sleep(0.4)
        _reconcile(self.store, {})
        self.assertEqual(self.store.task(task["id"])["state"], "unknown")
        self.assertIsNone(self.store.claim_next(2))

    def test_missing_registry_fails_actionably(self):
        with (
            patch("orchestrator.runtime.shutil.which", return_value=None),
            self.assertRaisesRegex(RuntimeError, "Install machine-resources"),
        ):
            ensure_supervisor(self.home)

    def test_monitor_resume_requires_same_snapshot_and_rotates(self):
        current = {"role": "monitor", "project_id": "project", "config": self.config}
        self.assertIsNone(_resume_session(self.store, current))
        for turn in range(20):
            self.store.enqueue(
                "project", "session", "monitor", "snapshot", self.config, cursor=turn
            )
            task = self.store.claim_next(2)
            self.store.finish(task["id"], task["token"], text="{}", harness_session="same-session")
            self.store.processed(task["id"])
            expected = None if turn == 19 else "same-session"
            self.assertEqual(_resume_session(self.store, current), expected)
        changed = {**current, "config": {**self.config, "personalization": {"name": "different"}}}
        self.assertIsNone(_resume_session(self.store, changed))

    def test_strict_json_rejects_duplicate_and_nonfinite_values(self):
        for text in ('{"verdict":"approved","verdict":"changes_requested"}', '{"value":NaN}'):
            with self.assertRaises(ValueError):
                _strict_json(text)

    def test_invalid_monitor_cursor_backs_off_without_advancing(self):
        task = self.store.enqueue(
            "project", "session", "monitor", "snapshot", self.config, cursor=2
        )
        task = self.store.claim_next(2)
        self.store.finish(task["id"], task["token"], text='{"reviewed_through":3,"findings":[]}')
        _process_results(self.store)
        project = self.store.project("project")
        self.assertEqual(project["monitor_cursor"], 0)
        self.assertEqual(project["monitor_failures"], 1)
        self.assertGreater(project["next_monitor_at"], time.time())
        _process_results(self.store)
        self.assertEqual(self.store.project("project")["monitor_failures"], 1)


if __name__ == "__main__":
    unittest.main()
