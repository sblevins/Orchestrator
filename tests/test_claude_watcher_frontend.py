"""Native watcher frontend contracts, without hosted model calls."""

import json
import os
import re
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock, patch

from orchestrator.bootstrap import bootstrap
from orchestrator.config import load_config
from orchestrator.frontends import ROOT, build_frontend_command
from orchestrator.hooks import handle_hook
from orchestrator.store import StateError, Store


class ClaudeWatcherFrontendTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.store = Store(self.home)
        self.store.add_project("project", str(self.home))
        self.store.open_session("frontend", "claude", "project")
        environment = patch.dict(os.environ, {}, clear=False)
        environment.start()
        self.addCleanup(environment.stop)
        for name in ("ORCHESTRATOR_CHILD", "ORCHESTRATOR_SESSION_ID"):
            os.environ.pop(name, None)
        owner_process = patch("orchestrator.bootstrap.claude_parent", return_value=None)
        owner_process.start()
        self.addCleanup(owner_process.stop)
        visibility = ModuleType("orchestrator.visibility")
        self.authorize = Mock(return_value={"watcher_id": "watcher"})
        visibility.authorize_claude_agent = self.authorize
        self.backend = patch.dict(sys.modules, {"orchestrator.visibility": visibility})
        self.backend.start()
        self.addCleanup(self.backend.stop)
        self.agent = {
            "subagent_type": "orchestrator-watcher",
            "model": "haiku",
            "run_in_background": True,
            "description": "Watch existing worker",
            "prompt": "Canonical backend-generated prompt",
        }

    def hook(self, **changes):
        payload = {
            "session_id": "frontend",
            "tool_name": "Agent",
            "tool_input": self.agent,
            "tool_use_id": "native-tool-use",
        }
        payload.update(changes)
        return handle_hook(self.home, "PreToolUse", payload)

    def assert_denied(self, result):
        self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_exact_input_and_id_pass_to_authority_without_rewriting(self):
        self.assertEqual(self.hook(), {})
        arguments = self.authorize.call_args.args
        self.assertIsInstance(arguments[0], Store)
        self.assertEqual(arguments[1:], ("frontend", self.agent, "native-tool-use"))
        self.assertIs(arguments[2], self.agent)
        # Idempotence belongs to the atomic service, not a hook-side cache.
        self.assertEqual(self.hook(), {})
        self.assertEqual(self.authorize.call_count, 2)

    def test_backend_rejections_always_deny(self):
        for error in (StateError("unprepared or modified"), ValueError("invalid"), TypeError()):
            with self.subTest(error=error):
                self.authorize.side_effect = error
                self.assert_denied(self.hook())

    def test_arbitrary_agent_is_never_allowed_without_backend_authorization(self):
        self.authorize.side_effect = StateError("Not the prepared invocation")
        for input_value in (
            {},
            {"subagent_type": "general-purpose"},
            {**self.agent, "resume": "x"},
        ):
            self.assert_denied(self.hook(tool_input=input_value))
        self.assertEqual(self.authorize.call_count, 3)

    def test_malformed_agent_calls_deny_before_backend(self):
        for input_value in (None, [], "prompt"):
            self.assert_denied(self.hook(tool_input=input_value))
        for tool_use_id in (None, 1, "", " "):
            self.assert_denied(self.hook(tool_use_id=tool_use_id))
        for session_id in (None, "", 1, "unknown-session"):
            self.assert_denied(self.hook(session_id=session_id))
        self.authorize.assert_not_called()

    def test_owner_rejection_precedes_authorization(self):
        with patch("orchestrator.hooks.verify_claude_owner", side_effect=StateError("displaced")):
            self.assert_denied(self.hook())
        self.authorize.assert_not_called()

    def test_inactive_unbound_and_pi_sessions_deny(self):
        self.store.close_session("frontend")
        self.assert_denied(self.hook())
        self.store.open_session("unbound", "claude")
        self.assert_denied(self.hook(session_id="unbound"))
        self.store.open_session("pi-session", "pi", "project")
        self.assert_denied(self.hook(session_id="pi-session"))
        self.authorize.assert_not_called()

    def test_taskstop_and_other_execution_remain_denied(self):
        for tool_name in ("TaskStop", "Task", "Bash", "Write", "Edit"):
            self.assert_denied(self.hook(tool_name=tool_name))
        self.assert_denied(
            self.hook(
                tool_name="mcp__orchestrator__watch_worker", tool_input={"session_id": "other"}
            )
        )
        self.authorize.assert_not_called()

    def test_only_claude_startup_receives_native_watcher_instructions(self):
        for frontend in ("claude", "pi"):
            with self.subTest(frontend=frontend):
                result = bootstrap(self.home, frontend, "new-" + frontend, reserve=False)
                instructions = result["instructions"]
                if frontend == "claude":
                    self.assertIn("prepare_worker_watch", instructions)
                    self.assertIn("exactly the returned agent arguments", instructions)
                    self.assertIn("until Agent actually launches", instructions)
                    self.assertIn("If agent is null (already_attached)", instructions)
                    self.assertIn("do not launch a duplicate watcher", instructions)
                    self.assertIn("Haiku does not support an effort setting", instructions)
                else:
                    self.assertNotIn("prepare_worker_watch", instructions)
                    self.assertNotIn("Haiku", instructions)

    def test_launcher_exposes_agent_only_for_claude(self):
        config = load_config(self.home)
        for frontend in ("claude", "pi"):
            command = build_frontend_command(self.home, config, frontend, str(uuid.uuid4()))
            tools = command[command.index("--tools") + 1].split(",")
            self.assertEqual("Agent" in tools, frontend == "claude")
            self.assertNotIn("TaskStop", tools)
            if frontend == "claude":
                definition = json.loads(command[command.index("--agents") + 1])[
                    "orchestrator-watcher"
                ]
                self.assertEqual(definition["tools"], ["mcp__orchestrator__watch_worker"])
                self.assertEqual(definition["model"], "haiku")

    def test_real_authority_consumes_only_exact_prepared_invocation(self):
        self.backend.stop()
        from orchestrator.api import request
        from orchestrator.visibility import prepare_worker_watch

        with patch("orchestrator.api._start_service", return_value={"running": True}):
            origin = request(self.home, "frontend", "record_prompt", {"prompt": "Inspect files"})
            worker = request(
                self.home,
                "frontend",
                "request_worker",
                {
                    "brief": "Inspect files",
                    "origin_event_id": origin["event_id"],
                },
            )
        # Represent an already dispatched worker without launching a paid model.
        task = self.store.enqueue("project", "frontend", "planner", "Fixture worker", {})
        with self.store.transaction() as database:
            database.execute(
                "UPDATE worker_requests SET task_id=? WHERE id=?", (task["id"], worker["id"])
            )
        prepared = prepare_worker_watch(self.store, "frontend", worker["id"])
        invocation = prepared["agent"]
        for changes in ({"model": "sonnet"}, {"run_in_background": False}, {"resume": "x"}):
            self.assert_denied(self.hook(tool_input={**invocation, **changes}))
        self.assertEqual(self.hook(tool_input=invocation), {})
        self.assertEqual(self.hook(tool_input=invocation), {})
        self.assert_denied(self.hook(tool_input=invocation, tool_use_id="duplicate-launch"))
        self.assertIsNone(prepare_worker_watch(self.store, "frontend", worker["id"])["agent"])
        self.assertEqual(self.store.task(task["id"])["state"], "queued")
        # Follow the exact native prompt, including session_id, through launcher-bound MCP.
        from orchestrator.mcp import MCPServer, ProtocolError

        prompted_arguments = json.loads(re.search(r"\{[^{}]+\}", invocation["prompt"])[0])
        for bound in (None, "frontend"):
            server = MCPServer(self.home, bound)
            session, payload = server._validate_arguments("watch_worker", prompted_arguments)
            self.assertEqual(session, "frontend")
            self.assertFalse(request(self.home, session, "watch_worker", payload)["done"])
        with self.assertRaises(ProtocolError):
            MCPServer(self.home, "other")._validate_arguments("watch_worker", prompted_arguments)

    def test_native_watcher_cannot_mutate_or_trigger_monitor(self):
        for name in (
            "Agent",
            "Read",
            "mcp__orchestrator__cancel_task",
            "mcp__orchestrator__record_decision",
        ):
            self.assert_denied(self.hook(agent_type="orchestrator-watcher", tool_name=name))
        with patch.object(Store, "record") as record:
            handle_hook(
                self.home,
                "PostToolUse",
                {
                    "session_id": "frontend",
                    "tool_name": "Agent",
                    "tool_input": self.agent,
                },
            )
            record.assert_not_called()

    def test_definition_has_only_watch_tool_and_no_unsupported_effort(self):
        definition = (ROOT / ".claude/agents/orchestrator-watcher.md").read_text()
        frontmatter = definition.split("---", 2)[1]
        fields = dict(line.split(": ", 1) for line in frontmatter.strip().splitlines())
        self.assertEqual(fields["name"], "orchestrator-watcher")
        self.assertEqual(fields["model"], "haiku")
        self.assertEqual(fields["background"], "true")
        self.assertEqual(fields["tools"], "mcp__orchestrator__watch_worker")
        self.assertNotIn("effort", fields)
        self.assertIn("exactly once", definition)
        self.assertIn("not the model doing the implementation", definition)
