"""Emitted coordinator and worker interfaces describe the effective execution authority."""

import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestrator.adapters import worker_tool_options
from orchestrator.api import request
from orchestrator.bootstrap import bootstrap
from orchestrator.hooks import handle_hook
from orchestrator.mcp import MCPServer
from orchestrator.worker_mcp import WorkerMCPServer

APPROVAL_TOOLS = ("approve_plan", "resolve_hold", "approve_worker", "accept_worker", "approve_node")
SANDBOXED = "enabled in the OS sandbox"
HOST = "enabled without an OS sandbox (host access)"
CONFIGURATIONS = (
    ({"execution": {"mode": "restricted"}, "commands": {"enabled": False}}, "disabled"),
    ({"execution": {"mode": "restricted"}, "commands": {"enabled": True}}, SANDBOXED),
    (
        {"execution": {"mode": "restricted"}, "commands": {"enabled": True, "sandbox": False}},
        HOST,
    ),
    ({"execution": {"mode": "trusted"}, "commands": {"enabled": False, "sandbox": True}}, HOST),
)


class CoordinatorContextTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        for target, value in (
            ("os.environ", {}),
            ("orchestrator.bootstrap.claude_parent", None),
            ("orchestrator.hooks.claude_parent", None),
            ("orchestrator.api._start_service", {"started": False}),
        ):
            patcher = (
                patch.dict(target, value, clear=True)
                if target == "os.environ"
                else patch(target, return_value=value)
            )
            patcher.start()
            self.addCleanup(patcher.stop)

    def initialize(self):
        server = MCPServer(self.home, "instance")
        initialized = server.handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-06-18"},
            }
        )
        listed = server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        return initialized["result"]["instructions"], {
            tool["name"] for tool in listed["result"]["tools"]
        }

    def test_mcp_instructions_offer_the_bound_approval_tools_it_lists(self):
        instructions, tools = self.initialize()
        for name in APPROVAL_TOOLS:
            with self.subTest(tool=name):
                self.assertIn(name, instructions)
                self.assertIn(name, tools)
        self.assertNotIn(
            "Approval, overrides, and result acceptance are operator-only", instructions
        )
        self.assertIn("execution.unattended=true", instructions)
        self.assertIn("remaining_issues", instructions)

    def test_startup_context_reports_effective_worker_commands(self):
        bootstrap(self.home, "claude", "writer", reserve=False)
        request(
            self.home,
            "writer",
            "register_project",
            {"project_id": "project", "root": str(self.home)},
        )
        request(self.home, "writer", "bind_project", {"project_id": "project"})
        request(self.home, "writer", "setup_project", {})
        for settings, expected in CONFIGURATIONS:
            with self.subTest(settings=settings):
                request(self.home, "writer", "configure_project", {"settings": settings})
                context = handle_hook(
                    self.home, "SessionStart", {"session_id": "writer", "source": "resume"}
                )["hookSpecificOutput"]["additionalContext"]
                preferences = json.loads(
                    re.search(r"Planning preferences:\n(\{.*\})", context).group(1)
                )
                self.assertEqual(preferences["execution_mode"], settings["execution"]["mode"])
                self.assertEqual(preferences["worker_run_command"], expected)
                bash = handle_hook(
                    self.home,
                    "PreToolUse",
                    {"session_id": "writer", "tool_name": "Bash", "tool_input": {}},
                )
                self.assertEqual(
                    bash.get("hookSpecificOutput", {}).get("permissionDecision"),
                    None if settings["execution"]["mode"] == "trusted" else "deny",
                )


class WorkerCommandDescriptionTests(unittest.TestCase):
    def test_run_command_description_follows_effective_sandbox(self):
        context = {"home": "/private", "task_id": "task", "token": "secret"}
        for settings, expected in CONFIGURATIONS[1:]:
            with self.subTest(settings=settings):
                options = worker_tool_options(
                    settings, "write", "/work", "/source", "/private/result", context
                )
                server = WorkerMCPServer(options)
                server.handle(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {"protocolVersion": "2025-03-26"},
                    }
                )
                with patch("orchestrator.worker_mcp.check_worker"):
                    listed = server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
                (tool,) = listed["result"]["tools"]
                self.assertEqual(tool["name"], "run_command")
                self.assertEqual(options["commands"]["sandbox"], expected == SANDBOXED)
                if expected == SANDBOXED:
                    self.assertIn("inside the OS sandbox", tool["description"])
                else:
                    self.assertIn("without an OS sandbox", tool["description"])


if __name__ == "__main__":
    unittest.main()
