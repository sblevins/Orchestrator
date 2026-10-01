"""Offline protocol fixtures and actual subprocess tests; never call paid models."""

import json
import subprocess
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from orchestrator.adapters import AdapterError, build_command, parse_result
from orchestrator.config import load_config

CLAUDE_RESULT = {
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "result": "review complete",
    "session_id": "claude-session",
    "total_cost_usd": 0.12,
}
CODEX_EVENTS = [
    {"type": "thread.started", "thread_id": "codex-thread"},
    {"type": "turn.started"},
    {
        "type": "item.completed",
        "item": {"id": "item_0", "type": "agent_message", "text": "checking"},
    },
    {
        "type": "item.completed",
        "item": {"id": "item_1", "type": "agent_message", "text": '{"approved":true}'},
    },
    {"type": "turn.completed", "usage": {"input_tokens": 123, "output_tokens": 45}},
]


def jsonl(events):
    return "\n".join(json.dumps(event) for event in events)


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.home = Path(self.directory.name)
        self.config = load_config(self.home)
        self.output = self.home / "result with spaces.json"

    def command(self, role="planner", prompt="review", session_id=None):
        return build_command(self.config, role, prompt, self.home, self.output, session_id)

    def test_claude_explicit_policy(self):
        command = self.command()
        self.assertEqual(command[0], "claude")
        for flag, value in {
            "--model": "claude-opus-5-5",
            "--effort": "high",
            "--output-format": "json",
            "--permission-mode": "dontAsk",
            "--tools": "Read,Glob,Grep",
            "--allowedTools": "Read,Glob,Grep",
            "--settings": "{}",
            "--setting-sources": "",
        }.items():
            self.assertEqual(command[command.index(flag) + 1], value)
        self.assertIn("-p", command)
        self.assertIn("--strict-mcp-config", command)
        self.assertIn("--disable-slash-commands", command)
        self.assertEqual(command[-2:], ["--", "review"])
        self.assertNotIn("--resume", command)

    def test_all_role_commands_have_no_spending_cap(self):
        for role in self.config["roles"]:
            sessions = (
                (None,)
                if self.config["roles"][role]["adapter"] == "pi"
                else (None, "saved-session")
            )
            for session in sessions:
                for stdin_prompt in (False, True):
                    with self.subTest(role=role, session=session, stdin=stdin_prompt):
                        command = build_command(
                            self.config,
                            role,
                            "review",
                            self.home,
                            self.output,
                            session,
                            stdin_prompt=stdin_prompt,
                        )
                        self.assertFalse(any("budget" in argument for argument in command))

    def test_obsolete_budget_cannot_be_silently_ignored(self):
        for role in self.config["roles"]:
            with self.subTest(role=role):
                config = deepcopy(self.config)
                config["roles"][role]["max_budget_usd"] = 5.0
                with self.assertRaisesRegex(AdapterError, "removed.*Remove"):
                    build_command(config, role, "review", self.home, self.output)

    def test_empty_and_restricted_claude_tools(self):
        for tools in ([], ["Read"]):
            self.config["roles"]["planner"]["allowed_tools"] = tools
            command = self.command()
            self.assertEqual(command[command.index("--tools") + 1], ",".join(tools))
            self.assertEqual(command[command.index("--allowedTools") + 1], ",".join(tools))

    def test_pi_explicit_policy(self):
        command = self.command("critic", "--hostile prompt")
        self.assertEqual(command[1], "-I")
        self.assertTrue(command[2].endswith("/orchestrator/pi_tools.py"))
        self.assertEqual(command[3], "launch")
        options = json.loads(command[4])
        self.assertEqual(options["provider"], "openai-codex")
        self.assertEqual(options["model"], "gpt-6-astra")
        self.assertEqual(options["effort"], "high")
        self.assertEqual(options["cwd"], str(self.home))
        self.assertEqual(options["prompt"], "--hostile prompt")
        self.assertEqual(set(options["tools"]), {"read", "ls", "find", "grep"})
        self.assertNotIn("codex", command)

    def test_stdin_prompt_and_project_access_preserve_policy(self):
        project = self.home / "project with spaces"
        for session in (None, "saved-session"):
            for role in ("planner", "critic"):
                with self.subTest(role=role, session=session):
                    if role == "critic" and session is not None:
                        with self.assertRaisesRegex(AdapterError, "ephemeral"):
                            build_command(
                                self.config,
                                role,
                                "large prompt",
                                self.home,
                                self.output,
                                session,
                                stdin_prompt=True,
                            )
                        continue
                    command = build_command(
                        self.config,
                        role,
                        "large prompt",
                        self.home,
                        self.output,
                        session,
                        project_root=project,
                        stdin_prompt=True,
                    )
                    self.assertNotIn("large prompt", command)
                    if role == "planner":
                        self.assertIn("-p", command)
                        self.assertNotIn("--", command)
                        self.assertEqual(command[command.index("--add-dir") + 1], str(project))
                        self.assertEqual(command[command.index("--tools") + 1], "Read,Glob,Grep")
                        if session:
                            self.assertEqual(command[-2:], ["--resume", session])
                    else:
                        options = json.loads(command[-1])
                        self.assertNotIn("prompt", options)
                        self.assertEqual(options["mode"], "read")
                        self.assertEqual(options["project_root"], str(project))
        self.assertNotIn("--add-dir", self.command())
        with self.assertRaises(AdapterError):
            build_command(
                self.config,
                "planner",
                "review",
                self.home,
                self.output,
                project_root=Path("bad\x00path"),
            )

    def test_resume_keeps_policy_and_exact_session(self):
        claude = self.command(session_id="saved-claude")
        self.assertEqual(claude[claude.index("--resume") + 1], "saved-claude")
        with self.assertRaisesRegex(AdapterError, "ephemeral"):
            self.command("critic", session_id="saved-pi")

    def test_validation_no_fallback_or_mutation(self):
        original = deepcopy(self.config)
        self.command()
        self.assertEqual(self.config, original)
        for key, value in (("effort", "unsupported"), ("allowed_tools", ["Bash"])):
            with self.subTest(key=key):
                self.config = deepcopy(original)
                self.config["roles"]["planner"][key] = value
                with self.assertRaises(AdapterError):
                    self.command()
        self.config = deepcopy(original)
        self.config["adapters"]["claude"]["command"] += ["--dangerously-skip-permissions"]
        with self.assertRaises(AdapterError):
            self.command()

    def test_invalid_arguments(self):
        for prompt in ("", "\x00", None):
            with self.subTest(prompt=prompt), self.assertRaises(AdapterError):
                self.command(prompt=prompt)
        for session in ("", "-last", "\x00"):
            with self.subTest(session=session), self.assertRaises(AdapterError):
                self.command(session_id=session)
        self.assertEqual(json.loads(self.command("critic", "-")[-1])["prompt"], "-")
        with self.assertRaises(AdapterError):
            self.command("critic", "-", "saved")
        with self.assertRaises(AdapterError):
            self.command("worker")

    def test_real_subprocess_argv_and_normalization(self):
        # A real executable receives the same argv/cwd/stdin as the harness.
        # It echoes argv as the terminal payload, exposing shell interpolation.
        executable = self.home / "fake harness"
        executable.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "payload = json.dumps({'argv': sys.argv[1:], 'cwd': os.getcwd(), "
            "'stdin': sys.stdin.read()})\n"
            "if '-p' in sys.argv:\n"
            " print(json.dumps({'type':'result', 'subtype':'success', 'is_error':False, "
            "'session_id':'fake-claude', 'structured_output':json.loads(payload)}))\n"
            "else:\n"
            " print(json.dumps({'type':'thread.started','thread_id':'fake-codex'}))\n"
            " print(json.dumps({'type':'item.completed', 'item': "
            "{'id':'final','type':'agent_message','text':payload}}))\n"
            " print(json.dumps({'type':'turn.completed','usage':{}}))\n",
            encoding="utf-8",
        )
        executable.chmod(0o700)
        prompt = '--dangerously-skip-permissions; $(touch SHOULD_NOT_EXIST)\n"quotes"'
        for role, adapter in (("planner", "claude"),):
            self.config["adapters"][adapter]["command"] = [str(executable)]
            command = self.command(role, prompt)
            result = subprocess.run(
                command,
                cwd=self.home,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            parsed = parse_result(adapter, result.stdout, result.returncode)
            payload = json.loads(parsed["text"])
            self.assertEqual(payload["argv"], command[1:])
            self.assertEqual(payload["argv"][-1], prompt)
            self.assertEqual(payload["cwd"], str(self.home))
            self.assertEqual(payload["stdin"], "")
            self.assertEqual(parsed["session_id"], "fake-" + adapter)
        self.assertFalse((self.home / "SHOULD_NOT_EXIST").exists())
        self.assertFalse(self.output.exists())


class ParserTests(unittest.TestCase):
    def test_claude_success_and_structured_priority(self):
        self.assertEqual(
            parse_result("claude", json.dumps(CLAUDE_RESULT, indent=2), 0),
            {"text": "review complete", "session_id": "claude-session", "cost_usd": 0.12},
        )
        for structured in ({"nodes": [], "summary": "café"}, [], "text", None):
            event = {**CLAUDE_RESULT, "structured_output": structured}
            result = parse_result("claude", json.dumps(event), 0)
            self.assertEqual(json.loads(result["text"]), structured)

    def test_codex_final_completed_message_and_unknown_cost(self):
        result = parse_result("codex", jsonl(CODEX_EVENTS), 0)
        self.assertEqual(
            result, {"text": '{"approved":true}', "session_id": "codex-thread", "cost_usd": None}
        )

    def test_forward_compatible_events_and_identical_terminal_replay(self):
        for adapter, events in (("claude", [CLAUDE_RESULT]), ("codex", CODEX_EVENTS)):
            transcript = [
                {"type": "future.telemetry", "extra": True},
                *events,
                events[-1],
                {"type": "future.telemetry"},
            ]
            self.assertIsInstance(parse_result(adapter, jsonl(transcript), 0)["text"], str)

    def test_replayed_completed_item_does_not_replace_final_message(self):
        events = [*CODEX_EVENTS[:-1], CODEX_EVENTS[2], CODEX_EVENTS[-1]]
        result = parse_result("codex", jsonl(events), 0)
        self.assertEqual(result["text"], '{"approved":true}')

    def test_claude_activity_after_terminal_is_not_success(self):
        for event_type in ("assistant", "user", "stream_event"):
            with self.assertRaises(AdapterError):
                parse_result("claude", jsonl([CLAUDE_RESULT, {"type": event_type}]), 0)

    def test_nonzero_and_invalid_exit_status(self):
        for adapter, events in (("claude", [CLAUDE_RESULT]), ("codex", CODEX_EVENTS)):
            for status in (1, 143, -15, False, "0", None):
                with self.subTest(adapter=adapter, status=status), self.assertRaises(AdapterError):
                    parse_result(adapter, jsonl(events), status)

    def test_malformed_or_missing_success(self):
        for adapter in ("claude", "codex"):
            for output in (
                "",
                "noise",
                "{}",
                "[]",
                "null",
                '{"type":1}',
                '{"type":"result", "type":"result"}',
                '{"type":"future","cost":NaN}',
                '{"type":"future"}',
            ):
                with self.subTest(adapter=adapter, output=output), self.assertRaises(AdapterError):
                    parse_result(adapter, output, 0)
            valid = json.dumps(CLAUDE_RESULT) if adapter == "claude" else jsonl(CODEX_EVENTS)
            for output in (valid + "\nnoise", "noise\n" + valid, valid + '\n{"type":'):
                with self.assertRaises(AdapterError):
                    parse_result(adapter, output, 0)
        with self.assertRaises(AdapterError):
            parse_result("other", json.dumps(CLAUDE_RESULT), 0)

    def test_claude_terminal_errors(self):
        for changes in (
            {"is_error": True},
            {"is_error": 0},
            {"is_error": None},
            {"subtype": "error_max_budget_usd"},
            {"subtype": "error_max_turns"},
            {"subtype": "error_during_execution"},
            {"subtype": "unknown"},
            {"stop_reason": "interrupted"},
            {"result": ""},
            {"result": {}},
            {"session_id": ""},
            {"session_id": None},
        ):
            with self.subTest(changes=changes), self.assertRaises(AdapterError):
                parse_result("claude", json.dumps({**CLAUDE_RESULT, **changes}), 0)
        for key in ("type", "subtype", "is_error", "result", "session_id"):
            event = dict(CLAUDE_RESULT)
            del event[key]
            with self.assertRaises(AdapterError):
                parse_result("claude", json.dumps(event), 0)

    def test_cost_validation(self):
        for cost in (True, "1.2", -1, float("nan"), float("inf"), 10**400):
            with self.subTest(cost=cost), self.assertRaises(AdapterError):
                parse_result("claude", json.dumps({**CLAUDE_RESULT, "total_cost_usd": cost}), 0)
        for cost in (None, 0, 2, 0.5):
            output = json.dumps({**CLAUDE_RESULT, "total_cost_usd": cost})
            result = parse_result("claude", output, 0)
            self.assertEqual(result["cost_usd"], cost)
        event = dict(CLAUDE_RESULT)
        del event["total_cost_usd"]
        self.assertIsNone(parse_result("claude", json.dumps(event), 0)["cost_usd"])

    def test_conflicting_terminals_and_sessions(self):
        invalid = [
            ("claude", [CLAUDE_RESULT, {**CLAUDE_RESULT, "result": "different"}]),
            ("claude", [{"type": "system", "session_id": "other"}, CLAUDE_RESULT]),
            ("codex", [*CODEX_EVENTS, {"type": "turn.completed", "usage": {"a": 1}}]),
            ("codex", [*CODEX_EVENTS, {"type": "thread.started", "thread_id": "other"}]),
        ]
        for adapter, events in invalid:
            with self.assertRaises(AdapterError):
                parse_result(adapter, jsonl(events), 0)

    def test_failure_or_interruption_even_after_success(self):
        for adapter, events in (("claude", [CLAUDE_RESULT]), ("codex", CODEX_EVENTS)):
            for failure in ("error", "turn.failed", "turn.interrupted", "turn.cancelled"):
                with (
                    self.subTest(adapter=adapter, failure=failure),
                    self.assertRaises(AdapterError),
                ):
                    parse_result(adapter, jsonl([*events, {"type": failure}]), 0)

    def test_incomplete_and_invalid_codex_sequences(self):
        invalid = [
            CODEX_EVENTS[:-1],
            CODEX_EVENTS[1:],
            [CODEX_EVENTS[0], CODEX_EVENTS[-1]],
            [*CODEX_EVENTS, {"type": "turn.started"}],
            [*CODEX_EVENTS, CODEX_EVENTS[-2]],
            [CODEX_EVENTS[0], {"type": "item.completed", "item": None}, CODEX_EVENTS[-1]],
            [
                CODEX_EVENTS[0],
                {
                    "type": "item.started",
                    "item": {"id": "x", "type": "agent_message", "text": "partial"},
                },
                CODEX_EVENTS[-1],
            ],
            [
                *CODEX_EVENTS[:-1],
                {
                    "type": "item.completed",
                    "item": {"id": "item_1", "type": "agent_message", "text": "conflict"},
                },
                CODEX_EVENTS[-1],
            ],
            [
                *CODEX_EVENTS[:-1],
                {
                    "type": "item.completed",
                    "item": {
                        "id": "failed",
                        "type": "agent_message",
                        "status": "interrupted",
                        "text": "partial",
                    },
                },
                CODEX_EVENTS[-1],
            ],
        ]
        for events in invalid:
            with self.subTest(events=events), self.assertRaises(AdapterError):
                parse_result("codex", jsonl(events), 0)


if __name__ == "__main__":
    unittest.main()
