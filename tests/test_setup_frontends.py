"""Initial setup guidance and foreground permission boundaries, without model calls."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestrator.bootstrap import ROOT, bootstrap
from orchestrator.hooks import handle_hook


class SetupFrontendTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)
        self.environment = patch.dict("os.environ", {}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.parent = patch("orchestrator.hooks.claude_parent", return_value=None)
        self.parent.start()
        self.addCleanup(self.parent.stop)

    def assert_setup_guidance(self, instructions):
        for text in (
            "project_setup",
            "setup_project",
            "expected_revision",
            "policy_revision",
            "initial_setup_open",
            "can_configure",
            "validation.blockers",
            "never invent preferences or defaults",
            "never force an overwrite",
            "deleting the policy does not reopen it",
            "Workers currently have file tools only",
        ):
            self.assertIn(text, instructions)

    def test_both_frontend_boot_instructions_explain_conversational_setup(self):
        for frontend in ("claude", "pi"):
            with self.subTest(frontend=frontend):
                initialized = bootstrap(self.home, frontend, f"setup-{frontend}", reserve=False)
                self.assert_setup_guidance(initialized["instructions"])

    def test_claude_startup_context_preserves_setup_guidance(self):
        response = handle_hook(self.home, "SessionStart", {"session_id": "setup-claude"})
        self.assert_setup_guidance(response["hookSpecificOutput"]["additionalContext"])

    def test_bash_stays_denied_even_for_policy_initialization(self):
        bootstrap(self.home, "claude", "setup-claude", reserve=False)
        for command in ("pwd", "mkdir -p .orchestrator", "git submodule update --init"):
            with self.subTest(command=command):
                response = handle_hook(
                    self.home,
                    "PreToolUse",
                    {
                        "session_id": "setup-claude",
                        "tool_name": "Bash",
                        "tool_input": {"command": command},
                    },
                )["hookSpecificOutput"]
                self.assertEqual(response["permissionDecision"], "deny")
                self.assertIn("setup_project", response["permissionDecisionReason"])

    def test_owned_setup_tool_allowed_but_not_cross_session_or_foreign_namespace(self):
        bootstrap(self.home, "claude", "setup-claude", reserve=False)
        for action in ("project_setup", "setup_project"):
            for payload in ({}, {"session_id": "setup-claude"}):
                self.assertEqual(
                    handle_hook(
                        self.home,
                        "PreToolUse",
                        {
                            "session_id": "setup-claude",
                            "tool_name": f"mcp__orchestrator__{action}",
                            "tool_input": payload,
                        },
                    ),
                    {},
                )
        for tool_name, payload in (
            ("mcp__orchestrator__setup_project", {"session_id": "another-instance"}),
            ("mcp__other__setup_project", {}),
            ("Write", {"file_path": ".orchestrator/crew-dispatch.json", "content": "{}"}),
        ):
            response = handle_hook(
                self.home,
                "PreToolUse",
                {
                    "session_id": "setup-claude",
                    "tool_name": tool_name,
                    "tool_input": payload,
                },
            )
            self.assertEqual(response["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_pi_description_exposes_setup_without_widening_tool_gate(self):
        extension = (ROOT / ".pi/extensions/orchestrator.ts").read_text()
        self.assertIn("bind_project, project_setup, setup_project, status", extension)
        self.assertIn("On stale revision reread project_setup", extension)
        self.assertIn("!allowedTools.has(event.toolName)", extension)
        self.assertIn('readTools[name]), "orchestrator"]', extension)
        self.assertIn("Session identity is provided by the bridge, never by payload.", extension)
