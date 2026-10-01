"""Actual installed Pi/plugin, production observer provider, no credentials or network."""

import json
import os
import queue
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

from orchestrator.frontends import ROOT

PLUGIN = Path.home() / ".pi/agent/npm/node_modules/@tintinweb/pi-subagents/src/index.ts"
FIXTURES = ROOT / "tests/fixtures/pi_observer"


@unittest.skipUnless(shutil.which("pi") and shutil.which("node"), "Installed Pi required")
class PiObserverTests(unittest.TestCase):
    def test_bounded_lifecycle_and_generation_guards(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / ".pi/agents").mkdir(parents=True)
            shutil.copy(ROOT / ".pi/agents/orchestrator-observer.md", directory / ".pi/agents")
            completed = subprocess.run(
                [
                    "node",
                    "--require",
                    str(FIXTURES / "deny-network.cjs"),
                    str(FIXTURES / "unit.mjs"),
                    str(ROOT / ".pi/lib/orchestrator-observer.ts"),
                    str(Path(shutil.which("pi")).resolve()),
                ],
                cwd=directory,
                env={
                    "PATH": os.environ["PATH"],
                    "HOME": str(directory),
                    "PI_CODING_AGENT_DIR": str(directory / "agent"),
                    "PI_OFFLINE": "1",
                    "OBSERVER_FIXTURE": str(directory),
                },
                text=True,
                capture_output=True,
                timeout=30,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("observer lifecycle unit checks passed", completed.stdout)
            self.assertFalse((directory / "network-attempts.log").exists())

    @unittest.skipUnless(PLUGIN.is_file(), "Installed pi-subagents required")
    def test_production_provider_native_record_detach_and_definition_guard(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            project = directory / "project"
            (project / ".pi/agents").mkdir(parents=True)
            shutil.copy(ROOT / ".pi/agents/orchestrator-observer.md", project / ".pi/agents")
            for name in ("home", "agent", "tmp"):
                (directory / name).mkdir()
            extension = directory / "fixture.ts"
            extension.write_text(
                (FIXTURES / "extension.ts")
                .read_text()
                .replace(
                    "PRODUCTION_OBSERVER_MODULE", str(ROOT / ".pi/lib/orchestrator-observer.ts")
                )
            )
            worker = {
                "request_id": "fixture-worker",
                "task_id": "task-1",
                "state": "queued",
                "task_state": "running",
                "done": False,
                "accepted": False,
                "label": "Independent fixture worker",
                "profile": {
                    "harness": "pi",
                    "provider": "actual-worker-provider",
                    "model": "actual-worker-model",
                    "effort": "low",
                },
            }

            def save_worker():
                replacement = directory / "worker.tmp"
                replacement.write_text(json.dumps(worker))
                replacement.replace(directory / "worker.json")

            save_worker()
            environment = {
                "PATH": os.environ["PATH"],
                "HOME": str(directory / "home"),
                "PI_CODING_AGENT_DIR": str(directory / "agent"),
                "TMPDIR": str(directory / "tmp"),
                "PI_OFFLINE": "1",
                "OBSERVER_FIXTURE": str(directory),
                "LANG": "C.UTF-8",
            }
            process = subprocess.Popen(
                [
                    "node",
                    "--require",
                    str(FIXTURES / "deny-network.cjs"),
                    str(Path(shutil.which("pi")).resolve()),
                    "--mode",
                    "rpc",
                    "--offline",
                    "--no-session",
                    "--no-extensions",
                    "--no-skills",
                    "--no-prompt-templates",
                    "--no-context-files",
                    "--extension",
                    str(extension),
                    "--extension",
                    str(PLUGIN),
                    "--provider",
                    "orchestrator-local-observer",
                    "--model",
                    "worker-status",
                ],
                cwd=project,
                env=environment,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            records = queue.Queue()

            def read_records():
                for line in process.stdout:
                    try:
                        records.put(json.loads(line))
                    except json.JSONDecodeError:
                        records.put({"invalid": line})

            reader = threading.Thread(target=read_records, daemon=True)
            reader.start()

            def command(kind, **arguments):
                identifier = str(time.monotonic_ns())
                process.stdin.write(
                    json.dumps({"id": identifier, "type": kind, **arguments}) + "\n"
                )
                process.stdin.flush()
                while True:
                    record = records.get(timeout=20)
                    self.assertNotIn("invalid", record)
                    if record.get("id") == identifier:
                        self.assertTrue(record.get("success"), record)
                        return record.get("data")

            def events():
                path = directory / "events.jsonl"
                return (
                    [json.loads(line) for line in path.read_text().splitlines()]
                    if path.exists()
                    else []
                )

            def wait_event(name, count=1):
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    found = [event for event in events() if event["event"] == name]
                    if len(found) >= count:
                        return found[-1]
                    time.sleep(0.05)
                self.fail(f"Missing {name}: {events()}")

            try:
                command("get_state")
                command("prompt", message="/fixture-sync")
                wait_event("subagents:started")
                wait_event("worker-detail-read")
                command("prompt", message="/fixture-detach")
                stopped = wait_event("subagents:failed")
                self.assertEqual(stopped["data"]["status"], "stopped")
                self.assertFalse(worker["done"])
                command("prompt", message="/fixture-sync")
                self.assertEqual(
                    sum(event["event"] == "subagents:started" for event in events()), 1
                )
                # Explicit attachment, not automatic respawn after the native Stop operation.
                command("prompt", message="/fixture-attach")
                wait_event("subagents:started", 2)
                worker.update(
                    done=True,
                    state="candidate",
                    task_state="succeeded",
                    report="FINAL SAFE RESULT: 42.\u001b[31m unaccepted\u0007",
                )
                save_worker()
                completed = wait_event("subagents:completed")
                self.assertIn("FINAL SAFE RESULT: 42.", completed["data"]["result"])
                self.assertNotIn("\u001b", completed["data"]["result"])
                self.assertIn("accepted: no", completed["data"]["result"])
                self.assertEqual(completed["data"]["toolUses"], 0)
                state = command("get_state")
                self.assertEqual(state["messageCount"], 0)
                self.assertEqual(state["pendingMessageCount"], 0)
                entries = command("get_entries")["entries"]
                self.assertEqual(
                    sum(entry.get("customType") == "subagents:record" for entry in entries), 2
                )
                # Exact customized definition must be rejected before native spawn.
                definition = project / ".pi/agents/orchestrator-observer.md"
                definition.write_text(definition.read_text().replace("tools: none", "tools: bash"))
                command("prompt", message="/fixture-attach")
                wait_event("fixture-error")
                self.assertEqual(
                    sum(event["event"] == "subagents:started" for event in events()), 2
                )
                self.assertFalse((directory / "network-attempts.log").exists())
                self.assertFalse(any(event["event"] == "parent-agent-start" for event in events()))
                command("prompt", message="/fixture-quit")
                process.wait(timeout=10)
                self.assertEqual(process.returncode, 0)
                self.assertEqual(process.stderr.read(), "")
            finally:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=10)
                for pipe in (process.stdin, process.stdout, process.stderr):
                    pipe.close()
                reader.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
