"""Editable project guidance and foreground permission boundaries, without model calls."""

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
            "can_configure",
            "project_settings",
            "configure_project",
            "config/projects/<bound-id>.json",
            "planning.templates",
            "no permanent setup seal",
            "easy, hard, or very-hard",
            "planned and on-demand",
            "same frozen Git commit",
            "eight jobs",
            "coordinator_approvals=true",
            "require_write_approval=false",
            "enforce_monitor_holds=true",
            "approve_plan",
            "resolve_hold",
            "approve_worker",
            "accept_worker",
            "approve_node",
            "cancel_worker",
            "not automatically accepted",
            "/model and /effort",
            "validation.blockers",
            "never invent preferences or defaults",
            "never force an overwrite",
            "including after deletion or an incomplete draft",
            "Workers currently have file tools only",
        ):
            self.assertIn(text, instructions)
        for obsolete_claim in (
            "permanently closes initial setup",
            "maintenance or repair remains operator work",
            "require operator authorization through the CLI",
        ):
            self.assertNotIn(obsolete_claim, instructions)

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
        for action in (
            "project_setup",
            "setup_project",
            "project_settings",
            "configure_project",
            "approve_plan",
            "resolve_hold",
            "approve_worker",
            "accept_worker",
            "approve_node",
            "cancel_worker",
        ):
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
            ("mcp__orchestrator__configure_project", {"session_id": "another-instance"}),
            ("mcp__other__configure_project", {}),
            ("mcp__orchestrator__accept_worker", {"session_id": "another-instance"}),
            ("mcp__other__approve_plan", {}),
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
        self.assertIn(
            "bind_project, project_setup, setup_project, project_settings, configure_project",
            extension,
        )
        for guidance in (
            "On stale revision reread project_setup",
            "no permanent seal or operator CLI handoff",
            "config/projects/<bound-id>.json",
            "planned and on-demand work while preserving plan origins",
            "easy/hard/very-hard",
            "complete untrusted reports",
            "accept only the parent",
            "Candidates are not automatically accepted",
            "cancel_worker requires request_id and reason",
            "file tools without shell remain mandatory",
        ):
            self.assertIn(guidance, extension)
        self.assertNotIn("Approvals remain operator-only", extension)
        self.assertNotIn("closed setup maintenance remains operator-only", extension)
        self.assertIn("!allowedTools.has(event.toolName)", extension)
        self.assertIn('readTools[name]), "orchestrator"]', extension)
        self.assertIn("Session identity is provided by the bridge, never by payload.", extension)
