"""Pi protocol, file broker, and real SDK E2E tests using only a loopback fake provider."""

import contextlib
import http.server
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from copy import deepcopy
from pathlib import Path

from orchestrator.adapters import AdapterError, build_pi_command, parse_result
from orchestrator.pi_tools import FileBroker, PolicyError, trusted_installation

BRIDGE = Path(__file__).resolve().parents[1] / "orchestrator" / "pi_bridge.mjs"
BROKER = BRIDGE.with_name("pi_tools.py")


def jsonl(events):
    return "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events)


def transcript(text="done"):
    message = {
        "role": "assistant",
        "provider": "openai",
        "model": "gpt-4o",
        "stopReason": "stop",
        "content": [{"type": "text", "text": text}],
    }
    return [
        {
            "type": "orchestrator_pi_preflight",
            "version": "0.99.2",
            "verified": True,
            "provider": "openai",
            "model": "gpt-4o",
            "effort": "off",
            "mode": "read",
            "tools": ["read", "ls", "find", "grep"],
        },
        {"type": "session", "version": 3, "id": "diagnostic-only", "cwd": "/fixture"},
        {"type": "agent_start"},
        {"type": "turn_start"},
        {
            "type": "message_start",
            "message": {"role": "assistant", "content": [], "stopReason": "pending"},
        },
        {"type": "message_end", "message": message},
        {"type": "turn_end", "message": message, "toolResults": []},
        {"type": "agent_end", "messages": [message], "willRetry": False},
        {"type": "agent_settled"},
    ]


class PiParserTests(unittest.TestCase):
    def test_success_unicode_and_ephemeral(self):
        text = "one\u2028two\u2029three"
        self.assertEqual(
            parse_result("pi", jsonl(transcript(text)), 0),
            {"text": text, "session_id": None, "cost_usd": None},
        )
        self.assertEqual(
            parse_result("pi", jsonl(transcript()).replace("\n", "\r\n"), 0)["text"], "done"
        )

    def test_rejects_incomplete_failed_or_late_success(self):
        cases = [
            transcript()[:-1],
            transcript()[1:],
            transcript() + [{"type": "agent_start"}],
            transcript() + [{"type": "orchestrator_pi_error"}],
            transcript()[:2] + [{"type": "agent_settled"}],
            transcript()[:5]
            + [{"type": "tool_execution_start", "toolCallId": "x", "toolName": "bash"}]
            + transcript()[5:],
        ]
        for reason in ("error", "aborted", "length", "toolUse", "pending", "deferred"):
            events = deepcopy(transcript())
            events[5]["message"]["stopReason"] = reason
            cases.append(events)
        for events in cases:
            with self.subTest(events=events), self.assertRaises(AdapterError):
                parse_result("pi", jsonl(events), 0)
        for code in (1, -15, True, None, "0"):
            with self.assertRaises(AdapterError):
                parse_result("pi", jsonl(transcript()), code)

    def test_malformed_event_shapes_are_adapter_errors(self):
        for event in (
            {"type": "message_end", "message": {}},
            {"type": "tool_execution_end", "toolCallId": []},
            {"type": "tool_execution_update", "toolCallId": {}},
            {"type": "turn_end", "message": None},
        ):
            with self.subTest(event=event), self.assertRaises(AdapterError):
                parse_result("pi", jsonl(transcript()[:2] + [event] + transcript()[2:]), 0)
        with self.assertRaises(AdapterError):
            parse_result("pi", jsonl(transcript("bad\ud800text")), 0)

    def test_strict_framing_and_preflight(self):
        for source in (
            jsonl(transcript()).rstrip("\n"),
            "noise\n" + jsonl(transcript()),
            jsonl(transcript()) + "\n",
            '{"type":"x","type":"y"}\n',
            '{"type":"x","number":NaN}\n',
        ):
            with self.assertRaises(AdapterError):
                parse_result("pi", source, 0)
        for key, value in (
            ("verified", False),
            ("version", "unknown"),
            ("tools", ["bash"]),
            ("provider", "anthropic"),
            ("effort", "invented"),
        ):
            events = deepcopy(transcript())
            events[0][key] = value
            with self.assertRaises(AdapterError):
                parse_result("pi", jsonl(events), 0)


class BrokerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.worktree = self.root / "worktree"
        self.source.mkdir()
        self.worktree.mkdir()
        self.broker = FileBroker(
            {"cwd": str(self.worktree), "project_root": str(self.source), "mode": "write"}
        )
        self.addCleanup(self.broker.close)

    def test_real_file_operations(self):
        self.broker.execute("write", {"path": "folder/file.txt", "content": "hello\nworld"})
        self.assertEqual(self.broker.execute("read", {"path": "folder/file.txt"}), "hello\nworld")
        self.broker.execute(
            "edit", {"path": "folder/file.txt", "edits": [{"oldText": "hello", "newText": "hi"}]}
        )
        self.assertIn("folder/file.txt", self.broker.execute("find", {"pattern": "**/*.txt"}))
        self.assertIn("2:world", self.broker.execute("grep", {"pattern": "world"}))
        self.assertEqual(self.broker.execute("ls", {}), "folder/")
        (self.worktree / "binary.png").write_bytes(b"\xff\x00\x01")
        self.assertIn("2:world", self.broker.execute("grep", {"pattern": "world"}))
        (self.source / "reference.txt").write_text("reference")
        self.assertEqual(
            self.broker.execute("read", {"path": str(self.source / "reference.txt")}), "reference"
        )

    def test_forbidden_paths_and_tools(self):
        (self.root / "outside").write_text("secret")
        (self.worktree / "link").symlink_to(self.root / "outside")
        (self.worktree / "linked-dir").symlink_to(self.source, target_is_directory=True)
        os.link(self.root / "outside", self.worktree / "hardlink")
        os.mkfifo(self.worktree / "pipe")
        for target in (
            "../outside",
            str(self.root / "outside"),
            "link",
            "hardlink",
            "pipe",
            "linked-dir/new",
            ".git/config",
            ".pi/settings.json",
            "AGENTS.md",
            "CLAUDE.MD",
            "auth.json",
            ".env",
            "/proc/self/environ",
        ):
            for name in ("read", "write"):
                with (
                    self.subTest(target=target, tool=name),
                    self.assertRaises((PolicyError, OSError)),
                ):
                    self.broker.execute(name, {"path": target, "content": "overwrite"})
        self.assertEqual((self.root / "outside").read_text(), "secret")
        for name in ("bash", "powershell", "codemode", "tool_search"):
            with self.assertRaises(PolicyError):
                self.broker.execute(name, {})
        with self.assertRaises(PolicyError):
            self.broker.execute("write", {"path": str(self.source / "new"), "content": "no"})

    def test_read_private_run_directory_is_not_exposed(self):
        (self.worktree / "task-metadata.json").write_text("private")
        broker = FileBroker(
            {"cwd": str(self.worktree), "project_root": str(self.source), "mode": "read"}
        )
        try:
            with self.assertRaises(PolicyError):
                broker.execute("read", {"path": str(self.worktree / "task-metadata.json")})
            self.assertNotIn("task-metadata", broker.execute("ls", {}))
        finally:
            broker.close()

    def test_read_only_and_source_overlap(self):
        with self.assertRaises(PolicyError):
            FileBroker({"cwd": str(self.source), "project_root": str(self.source), "mode": "write"})
        broker = FileBroker({"cwd": str(self.worktree), "mode": "read"})
        try:
            with self.assertRaises(PolicyError):
                broker.execute("write", {"path": "new", "content": "no"})
        finally:
            broker.close()


class PiBuilderTests(unittest.TestCase):
    def test_owned_child_and_allowlist(self):
        settings = {
            "adapter": "pi",
            "provider": "openai-codex",
            "model": "gpt-6-astra",
            "effort": "high",
            "allowed_tools": ["Read", "Glob", "Grep"],
        }
        command = build_pi_command(
            {"adapters": {"pi": {"command": ["pi"]}}},
            settings,
            "hello",
            Path("/work"),
            Path("/out"),
            project_root=Path("/source"),
        )
        self.assertEqual(command[1], "-I")
        self.assertEqual(command[2], str(BROKER))
        options = json.loads(command[-1])
        self.assertNotIn("prompt", options)
        self.assertEqual(options["tools"], ["read", "ls", "find", "grep"])
        self.assertEqual(options["model"], "gpt-6-astra")
        self.assertNotIn("codex", command)
        for mode, tools in (("read", ["Write"]), ("write", ["Bash"])):
            with self.assertRaises(AdapterError):
                build_pi_command(
                    {"adapters": {"pi": {"command": ["pi"]}}},
                    {**settings, "allowed_tools": tools},
                    "hello",
                    Path("/work"),
                    Path("/out"),
                    project_root=Path("/source"),
                    mode=mode,
                )


class PiInstalledE2ETests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        executable = shutil.which("pi")
        if not executable or not shutil.which("node"):
            raise unittest.SkipTest("installed Pi/Node unavailable")
        try:
            cls.package = trusted_installation(executable, set())
        except PolicyError as error:
            raise unittest.SkipTest(str(error)) from error

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.cwd = self.root / "worktree"
        self.source = self.root / "source"
        self.agent = self.root / "agent"
        for directory in (self.cwd, self.source, self.agent):
            directory.mkdir()
        self.auth = self.root / "auth.json"
        self.auth.write_text("{}")
        self.options = {
            "package": str(self.package),
            "auth_path": str(self.auth),
            "agent_dir": str(self.agent),
            "python": sys.executable,
            "broker": str(BROKER),
            "cwd": str(self.cwd),
            "project_root": str(self.source),
            "mode": "read",
            "tools": ["read", "ls", "find", "grep"],
            "provider": "fixture",
            "model": "fixture-model",
            "effort": "off",
        }
        self.environment = {
            "PATH": "/usr/bin:/bin",
            "HOME": str(self.root),
            "PI_CODING_AGENT_DIR": str(self.agent),
            "PI_OFFLINE": "1",
            "PI_SKIP_VERSION_CHECK": "1",
            "PI_TELEMETRY": "0",
        }
        self.requests = []

    @contextlib.contextmanager
    def server(self, responses, status=200):
        requests = self.requests

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                requests.append(json.loads(body))
                self.send_response(status)
                self.send_header(
                    "Content-Type", "text/event-stream" if status == 200 else "application/json"
                )
                self.end_headers()
                if status != 200:
                    self.wfile.write(b'{"error":{"message":"fixture denied"}}')
                    return
                index = min(len(requests) - 1, len(responses) - 1)
                delta = responses[index]
                reason = "tool_calls" if "tool_calls" in delta else "stop"
                for content, finish in (({"role": "assistant", **delta}, None), ({}, reason)):
                    record = {
                        "id": "fixture",
                        "object": "chat.completion.chunk",
                        "created": 1,
                        "model": "fixture-model",
                        "choices": [{"index": 0, "delta": content, "finish_reason": finish}],
                    }
                    self.wfile.write(("data: " + json.dumps(record) + "\n\n").encode())
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        models = self.root / "models.json"
        models.write_text(
            json.dumps(
                {
                    "providers": {
                        "fixture": {
                            "baseUrl": f"http://127.0.0.1:{server.server_port}/v1",
                            "api": "openai-completions",
                            "apiKey": "dummy-fixture-key",
                            "models": [
                                {
                                    "id": "fixture-model",
                                    "reasoning": False,
                                    "input": ["text"],
                                    "contextWindow": 10000,
                                    "maxTokens": 1000,
                                }
                            ],
                        }
                    }
                }
            )
        )
        try:
            yield models
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def run_bridge(self, models=None):
        script = self.root / "fixture.mjs"
        script.write_text(
            f"import {{ runBridge }} from {json.dumps(BRIDGE.as_uri())};\n"
            f"process.exitCode = await runBridge({json.dumps(self.options)}, "
            f"{{modelsPath: {json.dumps(str(models) if models else None)}}});\n"
        )
        return subprocess.run(
            ["/usr/bin/node", str(script)],
            input="Perform the fixture task",
            cwd=self.cwd,
            env=self.environment,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

    def test_owned_launcher_no_credentials_and_environment_isolation(self):
        settings = {
            "adapter": "pi",
            "provider": "openai",
            "model": "gpt-4o",
            "effort": "off",
            "allowed_tools": ["Read", "Glob", "Grep"],
        }
        command = build_pi_command(
            {"adapters": {"pi": {"command": [str(self.package / "dist/bundle/cli.js")]}}},
            settings,
            "fixture",
            self.cwd,
            self.root / "result",
            project_root=self.source,
        )
        injected = self.root / "injected.cjs"
        marker = self.root / "injection-ran"
        injected.write_text(f"require('fs').writeFileSync({json.dumps(str(marker))}, 'bad')")
        environment = {
            **self.environment,
            "NODE_OPTIONS": "--require=" + str(injected),
            "PYTHONPATH": str(self.root),
        }
        result = subprocess.run(
            command,
            input="fixture",
            cwd=self.cwd,
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("authentication", result.stderr)
        self.assertFalse(marker.exists())
        self.assertFalse((self.root / "result").exists())

    def test_real_sdk_success_and_discovery_disabled(self):
        (self.cwd / ".pi").mkdir()
        (self.cwd / ".pi" / "SYSTEM.md").write_text("INJECTED_SENTINEL")
        (self.cwd / "AGENTS.md").write_text("INJECTED_SENTINEL")
        with self.server([{"content": "fixture complete"}]) as models:
            result = self.run_bridge(models)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(
            parse_result("pi", result.stdout, result.returncode)["text"], "fixture complete"
        )
        self.assertEqual(len(self.requests), 1)
        request = self.requests[0]
        self.assertNotIn("INJECTED_SENTINEL", json.dumps(request))
        self.assertEqual(
            {tool["function"]["name"] for tool in request["tools"]}, set(self.options["tools"])
        )

    def test_real_sdk_read_and_write_tools(self):
        self.options.update(mode="write", tools=["read", "ls", "find", "grep", "edit", "write"])
        responses = [
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call-write",
                        "type": "function",
                        "function": {
                            "name": "write",
                            "arguments": json.dumps({"path": "result.txt", "content": "safe"}),
                        },
                    }
                ]
            },
            {"content": "written"},
        ]
        with self.server(responses) as models:
            result = self.run_bridge(models)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual((self.cwd / "result.txt").read_text(), "safe")
        self.assertEqual(parse_result("pi", result.stdout, 0)["text"], "written")
        self.assertFalse((self.source / "result.txt").exists())

    def test_unoffered_operation_fails_even_if_model_recovers(self):
        for tool, arguments in (("bash", {"command": "touch forbidden"}),):
            self.requests.clear()
            responses = [
                {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call-denied",
                            "type": "function",
                            "function": {"name": tool, "arguments": json.dumps(arguments)},
                        }
                    ]
                },
                {"content": "I recovered"},
            ]
            with self.server(responses) as models:
                result = self.run_bridge(models)
            self.assertNotEqual(result.returncode, 0)
            with self.assertRaises(AdapterError):
                parse_result("pi", result.stdout, result.returncode)
            self.assertFalse((self.cwd / "forbidden").exists())
            self.assertEqual(
                len(self.requests), 1, "forbidden operation must stop further inference"
            )

    def test_exact_model_effort_and_credentials_before_prompt(self):
        for changes in ({"model": "unknown-fixture"}, {"effort": "high"}):
            original = dict(self.options)
            self.options.update(changes)
            with self.server([{"content": "must not be called"}]) as models:
                result = self.run_bridge(models)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(self.requests, [])
            self.options = original
        self.options.update(provider="openai", model="gpt-4o", effort="off")
        result = self.run_bridge()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("authentication", result.stderr)
        self.auth.write_text(json.dumps({"openai": {"type": "api_key", "key": "!touch forbidden"}}))
        result = self.run_bridge()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.cwd / "forbidden").exists())

    def test_http_failure_is_not_success(self):
        with self.server([{}], status=401) as models:
            result = self.run_bridge(models)
        self.assertNotEqual(result.returncode, 0)
        with self.assertRaises(AdapterError):
            parse_result("pi", result.stdout, result.returncode)


if __name__ == "__main__":
    unittest.main()
