"""Regression coverage using the installed SDK and a loopback fake provider."""

import json
import os
import subprocess
import sys
import time
import unittest

from tests import test_pi_adapter as fixtures


class ToolRecoveryTests(unittest.TestCase):
    setUpClass = classmethod(fixtures.PiInstalledE2ETests.setUpClass.__func__)
    setUp = fixtures.PiInstalledE2ETests.setUp
    server = fixtures.PiInstalledE2ETests.server
    run_bridge = fixtures.PiInstalledE2ETests.run_bridge

    def test_instruction_reads_and_missing_file_recovery(self):
        (self.source / "AGENTS.md").write_text("Project instructions")
        (self.source / "CLAUDE.md").write_text("Additional instructions")
        responses = []
        for index, name in enumerate(("AGENTS.md", "missing.txt", "CLAUDE.md")):
            responses.append(
                {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": f"read-{index}",
                            "type": "function",
                            "function": {"name": "read", "arguments": json.dumps({"path": name})},
                        }
                    ]
                }
            )
        responses.append({"content": "review complete"})
        with self.server(responses) as models:
            result = self.run_bridge(models)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        events = [json.loads(line) for line in result.stdout.splitlines()]
        tool_results = [event for event in events if event["type"] == "tool_execution_end"]
        self.assertEqual([event["isError"] for event in tool_results], [False, True, False])
        self.assertIn("Project instructions", json.dumps(self.requests[1]))
        self.assertIn("Additional instructions", json.dumps(self.requests[3]))
        self.assertEqual(len(self.requests), 4)
        self.assertEqual(fixtures.parse_result("pi", result.stdout, 0)["text"], "review complete")

    def test_denied_path_does_not_leak_or_kill_review(self):
        self.auth.write_text(
            json.dumps({"other-provider": {"type": "api_key", "key": "private-sentinel"}})
        )
        responses = [
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "outside",
                        "type": "function",
                        "function": {
                            "name": "read",
                            "arguments": json.dumps({"path": str(self.auth)}),
                        },
                    }
                ]
            },
            {"content": "review continued without that file"},
        ]
        with self.server(responses) as models:
            result = self.run_bridge(models)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(len(self.requests), 2)
        self.assertNotIn("private-sentinel", result.stdout + json.dumps(self.requests))
        self.assertIn("outside allowed roots", json.dumps(self.requests[1]))

    def test_read_worker_uses_selected_worktree_not_source_branch(self):
        (self.cwd / "design.md").write_text("selected branch design")
        (self.source / "design.md").write_text("wrong source branch")
        self.options["worker_context"] = {"task_id": "host-captured-read-fixture"}
        responses = [
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "design",
                        "type": "function",
                        "function": {"name": "read", "arguments": '{"path":"design.md"}'},
                    }
                ]
            },
            {"content": "correct design reviewed"},
        ]
        with self.server(responses) as models:
            result = self.run_bridge(models)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn("selected branch design", json.dumps(self.requests[1]))
        self.assertNotIn("wrong source branch", json.dumps(self.requests[1]))

    def test_trusted_worker_can_write_normal_project_configuration(self):
        self.options.update(mode="write", tools=["read", "write"], trusted=True)
        responses = []
        for index, name in enumerate((".gitignore", ".github/workflows/test.yml", "AGENTS.md")):
            responses.append(
                {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": str(index),
                            "type": "function",
                            "function": {
                                "name": "write",
                                "arguments": json.dumps(
                                    {"path": name, "content": "project configuration"}
                                ),
                            },
                        }
                    ]
                }
            )
        responses.append({"content": "configuration written"})
        with self.server(responses) as models:
            result = self.run_bridge(models)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        for name in (".gitignore", ".github/workflows/test.yml", "AGENTS.md"):
            self.assertEqual((self.cwd / name).read_text(), "project configuration")
            self.assertFalse((self.source / name).exists())

    def test_trusted_internal_instruction_alias_can_be_read_and_edited(self):
        (self.cwd / "AGENTS.md").write_text("Original project instructions")
        (self.cwd / "CLAUDE.md").symlink_to("AGENTS.md")
        self.options.update(mode="write", tools=["read", "write"], trusted=True)
        responses = [
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "alias-read",
                        "type": "function",
                        "function": {"name": "read", "arguments": '{"path":"CLAUDE.md"}'},
                    }
                ]
            },
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "alias-write",
                        "type": "function",
                        "function": {
                            "name": "write",
                            "arguments": json.dumps(
                                {"path": "CLAUDE.md", "content": "Updated instructions"}
                            ),
                        },
                    }
                ]
            },
            {"content": "instruction alias updated"},
        ]
        with self.server(responses) as models:
            result = self.run_bridge(models)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn("Original project instructions", json.dumps(self.requests[1]))
        self.assertTrue((self.cwd / "CLAUDE.md").is_symlink())
        self.assertEqual((self.cwd / "AGENTS.md").read_text(), "Updated instructions")
        self.assertFalse((self.source / "AGENTS.md").exists())

    def test_family_authentication_uses_resolved_model(self):
        self.options["model"] = "Astra"
        with self.server([{"content": "family authenticated"}]) as models:
            configuration = json.loads(models.read_text())
            configuration["providers"]["fixture"]["models"][0]["id"] = "gpt-6-astra"
            models.write_text(json.dumps(configuration))
            result = self.run_bridge(models)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        parsed = fixtures.parse_result("pi", result.stdout, 0)
        self.assertEqual(parsed["model_selection"]["requested_model"], "Astra")
        self.assertEqual(parsed["model_selection"]["reported_models"], ["gpt-6-astra"])

    def command_options(self):
        from tests.test_team_messages import TeamMessageTests

        team = TeamMessageTests()
        team.setUp()
        self.addCleanup(team.doCleanups)
        task = team.tasks[0]
        self.options["worker_context"] = {
            "home": str(team.home),
            "task_id": task["id"],
            "token": task["token"],
        }
        return {
            "cwd": str(self.cwd),
            "project_root": str(self.source),
            "mode": "read",
            "artifact_directory": str(self.root / "artifacts"),
            "sandbox": False,
            "network": False,
            "timeout_seconds": 20,
            "cpus": 1,
            "tool_paths": {},
        }

    def test_owned_command_real_sdk(self):
        self.options.update(tool_names=["run_command"], commands=self.command_options())
        responses = [
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "command-1",
                        "type": "function",
                        "function": {
                            "name": "run_command",
                            "arguments": json.dumps({"command": "printf captured-command; exit 3"}),
                        },
                    }
                ]
            },
            {"content": "command checked"},
        ]
        with self.server(responses) as models:
            result = self.run_bridge(models)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn("captured-command", json.dumps(self.requests[1]))
        metadata = list((self.root / "artifacts").glob("*.json"))
        self.assertEqual(len(metadata), 1)
        self.assertEqual(json.loads(metadata[0].read_text())["exit_code"], 3)
        self.assertEqual(
            {tool["function"]["name"] for tool in self.requests[0]["tools"]}, {"run_command"}
        )

    def test_isolated_owned_package_imports(self):
        script = (
            "import importlib.util; "
            f"s=importlib.util.spec_from_file_location('broker', {str(fixtures.BROKER)!r}); "
            "m=importlib.util.module_from_spec(s); s.loader.exec_module(m); "
            "assert callable(m.owned_module('team_messages').execute); "
            "assert callable(m.owned_module('commands').run_command); "
            "assert callable(m.owned_module('images').generate_image)"
        )
        result = subprocess.run(
            [sys.executable, "-I", "-c", script],
            cwd=self.cwd,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_broker_cancellation_kills_command_group(self):
        command_options = self.command_options()
        options = {**self.options, "tool_names": ["run_command"], "commands": command_options}
        marker = self.cwd / "command.pid"
        process = subprocess.Popen(
            [sys.executable, "-I", str(fixtures.BROKER), "serve", json.dumps(options)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            process.stdin.write(
                json.dumps(
                    {
                        "name": "run_command",
                        "arguments": {"command": "printf '%s' $$ > command.pid; exec sleep 20"},
                    }
                )
                + "\n"
            )
            process.stdin.flush()
            deadline = time.monotonic() + 5
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(marker.exists())
            command_pid = int(marker.read_text())
            process.terminate()
            process.wait(timeout=5)
            with self.assertRaises(ProcessLookupError):
                os.kill(command_pid, 0)
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)
            for stream in (process.stdin, process.stdout, process.stderr):
                stream.close()

    def test_team_messages_real_sdk(self):
        from tests.test_team_messages import TeamMessageTests

        team = TeamMessageTests()
        team.setUp()
        self.addCleanup(team.doCleanups)
        task = team.tasks[0]
        self.options.update(
            tool_names=["send_team_message", "read_team_messages"],
            worker_context={"home": str(team.home), "task_id": task["id"], "token": task["token"]},
        )
        responses = []
        for index, (name, arguments) in enumerate(
            (("send_team_message", {"message": "Shared SDK finding"}), ("read_team_messages", {}))
        ):
            responses.append(
                {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": f"team-{index}",
                            "type": "function",
                            "function": {"name": name, "arguments": json.dumps(arguments)},
                        }
                    ]
                }
            )
        responses.append({"content": "team complete"})
        with self.server(responses) as models:
            result = self.run_bridge(models)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(
            team.call(1, "read_team_messages")["messages"][0]["body"], "Shared SDK finding"
        )
        self.assertNotIn(task["token"], result.stdout)
        self.assertIn("Shared SDK finding", json.dumps(self.requests[2]))

    def test_malformed_broker_response_is_fatal(self):
        fake = self.root / "broken-broker.py"
        fake.write_text(
            "import sys\nfor line in sys.stdin:\n print('{\"ok\":false}', flush=True)\n"
        )
        self.options["broker"] = str(fake)
        with self.server(
            [
                {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "read",
                            "type": "function",
                            "function": {"name": "read", "arguments": '{"path":"anything"}'},
                        }
                    ]
                },
                {"content": "must not recover"},
            ]
        ) as models:
            result = self.run_bridge(models)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(self.requests), 1)

    def test_unoffered_tool_still_aborts(self):
        with self.server(
            [
                {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "bad",
                            "type": "function",
                            "function": {"name": "run_command", "arguments": '{"command":"true"}'},
                        }
                    ]
                },
                {"content": "must not recover"},
            ]
        ) as models:
            result = self.run_bridge(models)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(self.requests), 1)
