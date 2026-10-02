"""User-facing project rules must describe this checkout, not an older deployment."""

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class CurrentPolicyDocumentationTests(unittest.TestCase):
    def test_status_describes_editable_project_configuration(self):
        status = (ROOT / "docs/status.md").read_text()
        implemented = status.split("## Implemented\n", 1)[1].split("## Validation\n", 1)[0]
        for current_rule in (
            "configure_project",
            "setup_project",
            "classifications",
            "execution.mode",
            "execution.base_ref",
            "execution.unattended",
            "remaining_issues",
            "retry_review",
            "monitoring.quiet_seconds",
            "send_team_message",
            "read_team_messages",
            "max-supported",
            "OPENAI_API_KEY",
            "images.enabled",
        ):
            self.assertIn(current_rule, implemented)
        self.assertIn("project-local", implemented.lower())
        for obsolete_rule in (
            "Initial setup closes after configuration or dispatched work",
            "acceptance remains an operator action",
            "unrelated writes and policy-required approvals require worker approval",
            "The slow monitor selects plan work; the foreground selects unrelated work",
            "source changes and tracked policy edits still do",
        ):
            self.assertNotIn(obsolete_rule, status)

    def test_status_separates_validation_from_deployment(self):
        status = (ROOT / "docs/status.md").read_text()
        self.assertIn("## Deployment activation", status)
        self.assertIn("validation is pending", status.lower())
        self.assertIn("No paid provider calls", status)
        self.assertNotIn("PID `", status)
        self.assertNotIn("/home/", status)
        self.assertNotIn("~/Agents/", status)

    def test_current_guides_do_not_restore_retired_absolute_restrictions(self):
        for name in ("README.md", "AGENTS.md", "docs/configuration.md", "docs/project-routing.md"):
            with self.subTest(path=name):
                text = (ROOT / name).read_text()
                for contract in (
                    "execution.unattended",
                    "remaining_issues",
                    "execution.mode",
                    "run_command",
                ):
                    self.assertIn(contract, text)
                self.assertNotIn("Candidate results are never automatically accepted", text)
                self.assertNotIn("Workers have file tools only, no shell or test execution", text)
                self.assertNotIn("\u2014", text)

    def test_loaded_instructions_and_user_guide_agree_on_local_configuration(self):
        for name in ("AGENTS.md", "CLAUDE.md", "roles/orchestrator.md", "docs/project-routing.md"):
            with self.subTest(path=name):
                text = (ROOT / name).read_text()
                self.assertIn("configure_project", text)
                self.assertIn("setup_project", text)
                self.assertIn("another project", text)
                self.assertNotIn(
                    "Existing-policy maintenance and closed-setup repairs remain operator work",
                    text,
                )
                self.assertNotIn("policy maintenance or repair remains operator work", text)
