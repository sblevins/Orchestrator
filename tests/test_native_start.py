"""Launcher-free frontend lifecycle using real native-style parent processes."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestrator.api import request
from orchestrator.bootstrap import bootstrap
from orchestrator.hooks import handle_hook
from orchestrator.store import StateError, Store

ROOT = Path(__file__).resolve().parents[1]


class NativeStartTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)
        self.store = Store(self.home)
        self.environment = patch.dict(os.environ, {}, clear=False)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        for key in ("ORCHESTRATOR_SESSION_ID", "ORCHESTRATOR_HOME", "ORCHESTRATOR_CHILD"):
            os.environ.pop(key, None)

    def test_native_frontend_claim_uses_supervisor_limits_once(self):
        (self.home / "config").mkdir(exist_ok=True)
        (self.home / "config/local.toml").write_text(
            '[supervisor]\nfrontend_memory="5G"\nfrontend_cpus=3\n'
        )
        with (
            patch("orchestrator.bootstrap.shutil.which", return_value="/fake/machine-resources"),
            patch("orchestrator.bootstrap.subprocess.run") as run,
        ):
            run.return_value.returncode = 0
            bootstrap(self.home, "pi", "native-reserved", os.getpid())
            bootstrap(self.home, "pi", "native-reserved", os.getpid())
        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args_list[0].args[0], ["/fake/machine-resources", "status"])
        arguments = run.call_args_list[1].args[0]
        self.assertEqual(arguments[1], "claim")
        self.assertEqual(arguments[arguments.index("-p") + 1], str(os.getpid()))
        self.assertEqual(arguments[arguments.index("-m") + 1], "5G")
        self.assertEqual(arguments[arguments.index("-c") + 1], "3")

    def test_native_pi_bootstrap_reconnects_but_cannot_reclaim_takeover(self):
        first = bootstrap(self.home, "pi", "native-pi", reserve=False)
        self.assertEqual(first["session"]["id"], "native-pi")
        self.assertIn("User preferences", first["instructions"])
        self.store.add_project("example", str(self.home))
        self.store.open_session("native-pi", "pi", "example")
        self.store.close_session("native-pi")
        reopened = bootstrap(self.home, "pi", "native-pi", reserve=False)
        self.assertEqual(reopened["session"]["project_id"], "example")
        self.store.open_session("replacement", "pi", "example", takeover=True)
        self.store.close_session("replacement")
        with self.assertRaisesRegex(StateError, "replaced"):
            bootstrap(self.home, "pi", "native-pi", reserve=False)
        self.assertFalse(self.store.session("native-pi")["active"])
        with self.assertRaisesRegex(StateError, "inactive"):
            request(self.home, "native-pi", "bind_project", {"project_id": "example"})

    def test_rejected_duplicate_cannot_use_or_close_original(self):
        bootstrap(self.home, "claude", "shared-native", os.getpid(), reserve=False)
        other = subprocess.Popen(
            [sys.executable, "-c", "import sys; sys.stdin.read()"], stdin=subprocess.PIPE
        )
        try:
            with self.assertRaisesRegex(StateError, "already open"):
                bootstrap(self.home, "claude", "shared-native", other.pid, reserve=False)
            with patch("orchestrator.bootstrap.claude_parent", return_value=other.pid):
                denied = handle_hook(
                    self.home,
                    "PreToolUse",
                    {
                        "session_id": "shared-native",
                        "tool_name": "mcp__orchestrator__projects",
                        "tool_input": {"session_id": "shared-native"},
                    },
                )
                self.assertEqual(denied["hookSpecificOutput"]["permissionDecision"], "deny")
                with self.assertRaises(StateError):
                    handle_hook(
                        self.home,
                        "UserPromptSubmit",
                        {"session_id": "shared-native", "prompt": "Change it"},
                    )
                handle_hook(
                    self.home,
                    "SessionEnd",
                    {"session_id": "shared-native", "reason": "prompt_input_exit"},
                )
            self.assertTrue(self.store.session("shared-native")["active"])
        finally:
            other.stdin.close()
            other.wait(timeout=5)

    def test_in_process_resume_selects_target_not_previous_project(self):
        for name in ("old-project", "target-project"):
            directory = self.home / name
            directory.mkdir()
            self.store.add_project(name, str(directory))
        bootstrap(self.home, "claude", "old-native", os.getpid(), reserve=False)
        self.store.open_session("old-native", "claude", "old-project")
        self.store.open_session("target-native", "claude", "target-project")
        self.store.close_session("target-native")
        with patch("orchestrator.runtime.ensure_supervisor", return_value={}):
            resumed = bootstrap(
                self.home, "claude", "target-native", os.getpid(), reserve=False, resume=True
            )
            cleared = bootstrap(
                self.home,
                "claude",
                "cleared-native",
                os.getpid(),
                reserve=False,
                resume=False,
                continuation=True,
            )
        self.assertEqual(resumed["session"]["id"], "target-native")
        self.assertEqual(resumed["session"]["project_id"], "target-project")
        self.assertEqual(cleared["session"]["id"], "target-native")
        self.assertFalse(self.store.session("old-native")["active"])
        self.assertEqual(self.store.service_value("native-alias:claude:old-native"), "old-native")
        self.assertEqual(
            self.store.service_value("native-alias:claude:target-native"), "target-native"
        )
        with (
            patch("orchestrator.bootstrap.claude_parent", return_value=os.getpid()),
            self.assertRaises(StateError),
        ):
            handle_hook(
                self.home,
                "UserPromptSubmit",
                {"session_id": "old-native", "prompt": "stale prompt"},
            )
        self.assertEqual(json.loads(self.store.service_value("native-owner:old-native")), {})
        reopened = bootstrap(self.home, "claude", "old-native", reserve=False, resume=True)
        self.assertEqual(reopened["session"]["project_id"], "old-project")
        self.assertTrue(reopened["session"]["active"])

    def test_native_claude_guards_workers_and_cross_session_tools(self):
        handle_hook(self.home, "SessionStart", {"session_id": "native-claude"})
        for name in ("Bash", "Edit", "Write", "Agent", "mcp__other__execute"):
            result = handle_hook(
                self.home, "PreToolUse", {"session_id": "native-claude", "tool_name": name}
            )
            self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(
            handle_hook(
                self.home, "PreToolUse", {"session_id": "native-claude", "tool_name": "Read"}
            ),
            {},
        )
        result = handle_hook(
            self.home,
            "PreToolUse",
            {
                "session_id": "native-claude",
                "tool_name": "mcp__orchestrator__status",
                "tool_input": {"session_id": "other"},
            },
        )
        self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_plain_claude_parent_preserves_clear_and_mcp_identity(self):
        # The script is named claude, as the real native executable is. No launcher
        # ORCHESTRATOR_* variables or generated settings are supplied.
        native = self.home / "claude"
        native.write_text("""import json, os, subprocess, sys
from pathlib import Path
root, home = map(Path, sys.argv[1:])
cli = [sys.executable, str(root/'bin/orchestrator'), '--home', str(home)]
def hook(event, session, **extra):
    command = cli+['hooks',event]
    result = subprocess.run(command,input=json.dumps({'session_id':session,**extra}),text=True,capture_output=True,check=True)
    return json.loads(result.stdout)
first=hook('SessionStart','native-first',source='startup')
hook('UserPromptSubmit','native-first',prompt='Plan the original requirement')
hook('SessionEnd','native-first',reason='clear')
cleared=hook('SessionStart','native-cleared',source='clear')
hook('UserPromptSubmit','native-cleared',prompt='Refine the requirement')
messages=[{'jsonrpc':'2.0','id':0,'method':'initialize','params':{'protocolVersion':'2025-11-25','capabilities':{},'clientInfo':{'name':'native-test','version':'1'}}}, {'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':'projects','arguments':{'session_id':'native-first'}}}]
reply=subprocess.run(cli+['mcp'],input=''.join(json.dumps(message)+'\\n' for message in messages),text=True,capture_output=True,check=True)
hook('SessionEnd','native-cleared',reason='prompt_input_exit')
print(json.dumps({'first':first,'cleared':cleared,'mcp':json.loads(reply.stdout.splitlines()[-1])}))
""")
        registry = self.home / "machine-resources"
        registry.write_text("#!/bin/sh\nexit 0\n")
        registry.chmod(0o700)
        environment = {**os.environ, "PATH": str(self.home) + os.pathsep + os.environ["PATH"]}
        result = subprocess.run(
            [sys.executable, str(native), str(ROOT), str(self.home)],
            env=environment,
            text=True,
            capture_output=True,
            check=False,
            timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        self.assertIn("native-first", output["cleared"]["hookSpecificOutput"]["additionalContext"])
        self.assertNotIn("error", output["mcp"])
        session = self.store.session("native-first")
        self.assertFalse(session["active"])
        saved = list((self.store.data / "sessions/native-first/prompts").glob("*.json"))
        self.assertEqual(len(saved), 2)
        self.assertEqual(
            self.store.service_value("native-alias:claude:native-cleared"), "native-first"
        )
        resumed = bootstrap(self.home, "claude", "native-cleared", resume=True, reserve=False)
        self.assertEqual(resumed["session"]["id"], "native-first")
        self.assertTrue(resumed["session"]["active"])


if __name__ == "__main__":
    unittest.main()
