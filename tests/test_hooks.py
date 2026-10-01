import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock, patch

from orchestrator.hooks import handle_hook
from orchestrator.store import Store


class HookTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)
        self.store = Store(self.home)
        self.store.add_project("project", str(self.home))
        self.store.open_session("frontend", "claude", "project")
        self.environment = patch.dict(os.environ, {"ORCHESTRATOR_CHILD": "0"})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def hook(self, event, **values):
        return handle_hook(self.home, event, {"session_id": "frontend", **values})

    def test_start_context_and_takeover_not_reactivated(self):
        output = self.hook("SessionStart")
        self.assertEqual(set(output), {"hookSpecificOutput"})
        self.assertEqual(output["hookSpecificOutput"]["hookEventName"], "SessionStart")
        self.assertIn("frontend", output["hookSpecificOutput"]["additionalContext"])
        self.assertIn("BRIEF.md", output["hookSpecificOutput"]["additionalContext"])
        self.store.open_session("replacement", "claude", "project", takeover=True)
        self.hook("SessionStart")
        self.assertFalse(self.store.session("frontend")["active"])
        self.assertTrue(self.store.session("replacement")["active"])

    def test_unknown_initialized_only_once(self):
        handle_hook(self.home, "SessionStart", {"session_id": "new"})
        self.store.close_session("new")
        handle_hook(self.home, "SessionStart", {"session_id": "new"})
        self.assertFalse(self.store.session("new")["active"])

    def test_all_prompts_delegate_intact_including_unbound(self):
        api = ModuleType("orchestrator.api")
        api.request = Mock(return_value={})
        prompt = "oversized " * 10000
        with patch.dict(sys.modules, {"orchestrator.api": api}):
            self.hook("UserPromptSubmit", prompt=prompt)
            api.request.assert_called_once_with(
                self.home, "frontend", "record_prompt", {"prompt": prompt}
            )
            self.store.open_session("unbound", "claude")
            handle_hook(
                self.home, "UserPromptSubmit", {"session_id": "unbound", "prompt": "/status"}
            )
            self.assertEqual(api.request.call_count, 2)

    def test_readonly_and_own_tools_do_not_trigger_monitor(self):
        before = self.store.events("project")
        for name in [
            "Read",
            "Grep",
            "mcp__orchestrator__status",
            "mcp__orchestrator__acknowledge",
            "mcp__orchestrator__request",
        ]:
            self.hook("PostToolUse", tool_name=name, tool_input={"action": "updates"})
        self.hook(
            "PostToolUse",
            tool_name="Bash",
            tool_input={
                "command": "/repo/bin/orchestrator request --session frontend --action status --payload '{}'"
            },
        )
        self.assertEqual(before, self.store.events("project"))
        self.hook("PostToolUseFailure", tool_name="Bash", tool_input={"command": "x" * 50000})
        event = self.store.events("project")[-1]
        self.assertEqual(event["kind"], "tool.observed")
        self.assertTrue(event["payload"]["failed"])
        self.assertTrue(event["payload"]["truncated"])

    def test_stop_routine_reply_quiet_substantive_once(self):
        self.store.record("frontend", "user.message", {"prompt": "status"}, review_required=False)
        count = len(self.store.events("project"))
        self.assertEqual(self.hook("Stop", last_assistant_message="All running"), {})
        self.assertEqual(len(self.store.events("project")), count)
        self.store.record("frontend", "user.message", {"prompt": "Change the plan"})
        self.hook("Stop", last_assistant_message="I will revise it")
        count = len(self.store.events("project"))
        self.hook("Stop", last_assistant_message="I will revise it")
        self.assertEqual(len(self.store.events("project")), count)
        self.assertEqual(self.store.events("project")[-1]["kind"], "decision.recorded")

    def test_pending_context_guard_and_no_implicit_ack(self):
        with self.store.transaction() as database:
            event_id = self.store._event(
                database, "project", "monitor.findings", {"findings": []}, "frontend", notify=True
            )
        output = self.hook("Stop")
        self.assertEqual(output["hookSpecificOutput"]["hookEventName"], "Stop")
        self.assertLessEqual(len(output["hookSpecificOutput"]["additionalContext"]), 9000)
        self.assertEqual(self.hook("Stop", stop_hook_active=True), {})
        self.assertEqual(self.store.updates("frontend")[0]["id"], event_id)

    def test_child_ignored_and_end_closes_only_session(self):
        with patch.dict(os.environ, {"ORCHESTRATOR_CHILD": "1"}):
            self.assertEqual(self.hook("SessionEnd"), {})
            self.assertTrue(self.store.session("frontend")["active"])
        task = self.store.enqueue("project", "frontend", "planner", "plan", {})
        self.assertEqual(self.hook("SessionEnd"), {})
        self.assertFalse(self.store.session("frontend")["active"])
        self.assertEqual(self.store.task(task["id"])["state"], "queued")
