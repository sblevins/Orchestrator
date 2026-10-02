"""Reproduce the user-facing bind/setup sequence through real MCP and hook processes."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from orchestrator.frontends import ROOT
from orchestrator.store import Store


class SetupTransportTests(unittest.TestCase):
    def test_bound_coordinator_finishes_initial_setup_without_shell_or_operator_cli(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            project = home / "Code/project"
            project.mkdir(parents=True)
            (project / "source.txt").write_text("unchanged source")
            store = Store(home)
            store.open_session("frontend", "claude")
            environment = {**os.environ, "PYTHONPATH": str(ROOT), "NO_MISTAKES_GATE": ""}
            for key in ("ORCHESTRATOR_CHILD", "ORCHESTRATOR_HOME", "ORCHESTRATOR_SESSION_ID"):
                environment.pop(key, None)
            # Exercise real transports/state/files. Starting unrelated paid monitor models
            # is intentionally replaced; worker execution is covered by lifecycle E2E tests.
            program = (
                "from pathlib import Path; import orchestrator.api as api; "
                "from orchestrator.mcp import MCPServer; "
                "api._start_service=lambda home: {'running': False, 'fixture': True}; "
                f"MCPServer(Path({str(home)!r}), 'frontend').serve()"
            )
            process = subprocess.Popen(
                [sys.executable, "-c", program],
                cwd=ROOT,
                env=environment,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            identifier = 0

            def rpc(method, parameters):
                nonlocal identifier
                identifier += 1
                process.stdin.write(
                    json.dumps(
                        {"jsonrpc": "2.0", "id": identifier, "method": method, "params": parameters}
                    )
                    + "\n"
                )
                process.stdin.flush()
                response = json.loads(process.stdout.readline())
                self.assertEqual(response["id"], identifier)
                self.assertNotIn("error", response)
                return response["result"]

            def operation(name, arguments=None, failure=False):
                result = rpc("tools/call", {"name": name, "arguments": arguments or {}})
                self.assertEqual(result["isError"], failure, result)
                return (
                    result["content"][0]["text"]
                    if failure
                    else json.loads(result["content"][0]["text"])
                )

            # Real hook process, but without discovering whichever Claude Code process
            # happens to be running this suite, which never owns the fixture session.
            hook_program = (
                "import sys; import orchestrator.bootstrap as bootstrap; "
                "import orchestrator.hooks as hooks; "
                "bootstrap.claude_parent = hooks.claude_parent = lambda: None; "
                "from orchestrator.cli import main; raise SystemExit(main(sys.argv[1:]))"
            )

            def hook(name, arguments):
                result = subprocess.run(
                    [
                        sys.executable,
                        "-c",
                        hook_program,
                        "--home",
                        str(home),
                        "hooks",
                        "PreToolUse",
                    ],
                    input=json.dumps(
                        {"session_id": "frontend", "tool_name": name, "tool_input": arguments}
                    ),
                    env=environment,
                    text=True,
                    capture_output=True,
                    timeout=10,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                return json.loads(result.stdout)

            try:
                rpc("initialize", {"protocolVersion": "2025-06-18"})
                tools = {tool["name"] for tool in rpc("tools/list", {})["tools"]}
                self.assertTrue({"project_setup", "setup_project"} <= tools)
                operation("register_project", {"project_id": "example", "root": str(project)})
                bound = operation("bind_project", {"project_id": "example"})
                self.assertEqual(bound["setup"]["phase"], "needs_configuration")
                self.assertFalse(operation("routing_policy")["available"])
                denied = hook(
                    "Bash", {"command": "./bin/orchestrator routing init --project example"}
                )
                self.assertEqual(denied["hookSpecificOutput"]["permissionDecision"], "deny")
                self.assertEqual(
                    hook("mcp__orchestrator__setup_project", {"session_id": "frontend"}), {}
                )
                initialized = operation("setup_project")
                self.assertTrue(initialized["changed"])
                current = operation("project_setup")
                policy = {"default": {"harness": "claude", "model": "Opus", "effort": "high"}}
                configured = operation(
                    "setup_project",
                    {"policy": policy, "expected_revision": current["policy_revision"]},
                )
                self.assertEqual(configured["phase"], "configured")
                self.assertTrue(configured["validation"]["routable"])
                self.assertFalse(configured["execution_authorized"])
                self.assertTrue(operation("routing_policy")["available"])
                self.assertEqual(operation("status")["setup"]["phase"], "configured")
                updated = operation(
                    "setup_project",
                    {"policy": policy, "expected_revision": configured["policy_revision"]},
                )
                self.assertTrue(updated["can_configure"])
                preferences = operation(
                    "configure_project",
                    {"settings": {"roles": {"monitor": {"model": "Opus", "effort": "max"}}}},
                )
                self.assertIn("revision", preferences)
                self.assertEqual((project / "source.txt").read_text(), "unchanged source")
                self.assertFalse((project / ".gitignore").exists())
                self.assertEqual(store.tasks(), [])
                self.assertEqual(
                    json.loads((project / ".orchestrator/crew-dispatch.json").read_text()), policy
                )
            finally:
                process.stdin.close()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)
                error = process.stderr.read()
                process.stdout.close()
                process.stderr.close()
            self.assertEqual(process.returncode, 0, error)


if __name__ == "__main__":
    unittest.main()
