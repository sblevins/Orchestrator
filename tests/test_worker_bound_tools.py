"""Host tool grants and configured-branch workspace regressions (no paid calls)."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestrator.adapters import worker_tool_options
from orchestrator.worker_execution import build_worker_command, prepare_workspace
from orchestrator.worker_mcp import WorkerMCPServer

CONTEXT = {"home": "/private", "task_id": "task", "token": "secret"}


def test_host_options_trusted_and_team():
    options = worker_tool_options(
        {
            "execution": {"mode": "trusted"},
            "commands": {"sandbox": True},
            "worker": {"team_parent": "parent"},
        },
        "read",
        Path("/work"),
        Path("/source"),
        Path("/private/result"),
        CONTEXT,
    )
    assert options["commands"]["sandbox"] is False
    assert options["commands"]["artifact_directory"] == "/private/command-artifacts"
    assert set(options["tool_names"]) == {"run_command", "send_team_message", "read_team_messages"}
    assert (
        worker_tool_options({}, "read", "/work", "/source", "/private/result", CONTEXT)[
            "tool_names"
        ]
        == []
    )


def test_harness_owned_tool_configuration(harness, provider, model):
    command = build_worker_command(
        {"execution": {"mode": "trusted"}, "adapters": {harness: {"command": [harness]}}},
        {"harness": harness, "provider": provider, "model": model, "effort": "high"},
        "read",
        "inspect",
        Path("/workspace"),
        Path("/private/result"),
        project_root=Path("/source"),
        worker_context=CONTEXT,
    )
    if harness == "pi":
        options = json.loads(command[-1])
        assert options["worker_context"] == CONTEXT
        assert "run_command" in options["tool_names"]
    else:
        assert "--strict-mcp-config" in command
        assert (
            "mcp__orchestrator_worker__run_command" in command[command.index("--allowedTools") + 1]
        )
        options = json.loads(
            json.loads(command[-1])["mcpServers"]["orchestrator_worker"]["args"][-1]
        )
    assert options["commands"]["sandbox"] is False


def test_mcp_rechecks_attempt_and_notifications_never_execute():
    server = WorkerMCPServer(
        {"worker_context": CONTEXT, "tool_names": ["run_command"], "commands": {}}
    )
    server.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-03-26"},
        }
    )
    call = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {"name": "run_command", "arguments": {"command": "true"}},
    }
    with patch("orchestrator.worker_mcp.check_worker", side_effect=ValueError("revoked")) as check:
        assert server.handle(call)["result"]["isError"] is True
        assert check.call_count == 1
        del call["id"]
        assert server.handle(call) is None
        assert check.call_count == 1


def test_configured_branch_workspace_leaves_source_main_untouched(tmp_path, mode):
    from tests import test_worker_execution as fixtures

    fixture = fixtures.WorkspaceTests()
    fixture.setUp()
    try:
        source = fixture.source

        def git(*arguments):
            return fixtures.git(source, *arguments)

        git("branch", "-M", "main")
        main_commit = git("rev-parse", "HEAD")
        git("checkout", "-qb", "design")
        (source / "design.md").write_text("Configured branch design\n")
        git("add", ".")
        git("commit", "-qm", "design")
        design_commit = git("rev-parse", "HEAD")
        git("checkout", "-q", "main")
        workspace = prepare_workspace(
            tmp_path / "home", {"id": "branch-test", "mode": mode}, source, base_ref="design"
        )
        assert Path(workspace["path"], "design.md").read_text() == "Configured branch design\n"
        assert workspace["base_commit"] == design_commit
        assert git("rev-parse", "HEAD") == main_commit
        assert git("branch", "--show-current") == "main"
        assert git("status", "--porcelain") == ""
        assert not (source / "design.md").exists()
    finally:
        fixture.doCleanups()


class WorkerBoundToolsTests(unittest.TestCase):
    def test_host_options(self):
        test_host_options_trusted_and_team()

    def test_harnesses(self):
        for harness, provider, model in (
            ("claude", "anthropic", "claude-sonnet-4-6"),
            ("pi", "openai", "gpt-5.4"),
        ):
            with self.subTest(harness=harness):
                test_harness_owned_tool_configuration(harness, provider, model)

    def test_recoverable_matched_tool_error(self):
        from orchestrator.adapters import AdapterError, parse_result
        from tests.test_pi_adapter import transcript

        records = transcript()
        request = {
            "role": "assistant",
            "provider": "openai",
            "model": "gpt-4o",
            "stopReason": "toolUse",
            "content": [{"type": "toolCall", "id": "call", "name": "read", "arguments": {}}],
        }
        tool_result = {
            "role": "toolResult",
            "toolCallId": "call",
            "toolName": "read",
            "isError": True,
        }
        records[3:3] = [
            {"type": "turn_start"},
            {"type": "message_start", "message": request},
            {"type": "message_end", "message": request},
            {"type": "tool_execution_start", "toolCallId": "call", "toolName": "read"},
            {
                "type": "tool_execution_end",
                "toolCallId": "call",
                "toolName": "read",
                "isError": True,
            },
            {"type": "message_start", "message": tool_result},
            {"type": "message_end", "message": tool_result},
            {"type": "turn_end", "message": request},
        ]
        output = lambda: "".join(json.dumps(record) + "\n" for record in records)
        self.assertEqual(parse_result("pi", output(), 0)["text"], "done")
        tool_result["isError"] = False
        with self.assertRaises(AdapterError):
            parse_result("pi", output(), 0)

    def test_revocation(self):
        test_mcp_rechecks_attempt_and_notifications_never_execute()

    def test_detached_command_cleanup(self):
        from orchestrator.runtime import _kill_descendants, _remember_descendants, process_identity

        process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                (
                    "import subprocess,time; "
                    "child=subprocess.Popen(['sleep','60'],start_new_session=True); "
                    "print(child.pid,flush=True); child.wait()"
                ),
            ],
            stdout=subprocess.PIPE,
            text=True,
        )
        descendants = {}
        try:
            child_pid = int(process.stdout.readline())
            _remember_descendants(process.pid, descendants)
            self.assertIn(child_pid, descendants)
            _kill_descendants(descendants)
            process.wait(timeout=5)
            self.assertIsNone(process_identity(child_pid))
        finally:
            _kill_descendants(descendants)
            if process.poll() is None:
                process.kill()
            process.wait()
            process.stdout.close()

    def test_branch_base(self):
        for mode in ("read", "write"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                test_configured_branch_workspace_leaves_source_main_untouched(Path(directory), mode)
