import json
import tempfile
import unittest
import uuid
from pathlib import Path

from orchestrator.config import ConfigurationError, load_config
from orchestrator.frontends import ROOT, build_frontend_command


class FrontendTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)
        self.config = load_config(self.home)
        self.session = str(uuid.uuid4())

    def test_claude_native_explicit_private_mcp(self):
        command = build_frontend_command(self.home, self.config, "claude", self.session)
        self.assertEqual(
            command[command.index("--model") + 1], self.config["roles"]["orchestrator"]["model"]
        )
        self.assertEqual(command[command.index("--session-id") + 1], self.session)
        self.assertIn("--strict-mcp-config", command)
        self.assertFalse(
            {"--print", "--bare", "--safe-mode", "--dangerously-skip-permissions"} & set(command)
        )
        path = Path(command[command.index("--mcp-config") + 1])
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        server = json.loads(path.read_text())["mcpServers"]["orchestrator"]
        self.assertEqual(
            server,
            {
                "command": str(ROOT / "bin/orchestrator"),
                "args": ["--home", str(self.home), "mcp", "--session", self.session],
            },
        )

    def test_configured_read_tools_are_not_widened(self):
        self.config["roles"]["orchestrator"]["allowed_tools"] = []
        for frontend, expected in [("claude", "AskUserQuestion,Agent"), ("pi", "orchestrator")]:
            command = build_frontend_command(self.home, self.config, frontend, self.session)
            self.assertEqual(command[command.index("--tools") + 1], expected)

    def test_channels_explicit_only(self):
        command = build_frontend_command(self.home, self.config, "claude", self.session, True)
        self.assertIn("--dangerously-load-development-channels", command)
        path = Path(command[command.index("--mcp-config") + 1])
        self.assertIn(
            "--channels", json.loads(path.read_text())["mcpServers"]["orchestrator"]["args"]
        )
        with self.assertRaises(ConfigurationError):
            build_frontend_command(self.home, self.config, "pi", self.session, True)

    def test_pi_provider_and_separate_storage(self):
        for adapter, provider, effort in [
            ("claude", "anthropic", "high"),
            ("pi", "openai-codex", "xhigh"),
        ]:
            self.config["roles"]["orchestrator"].update(
                adapter=adapter,
                effort=effort,
                provider=provider,
                model="claude-sonnet-5-5" if adapter == "claude" else "gpt-6-astra",
            )
            command = build_frontend_command(self.home, self.config, "pi", self.session)
            self.assertEqual(command[command.index("--provider") + 1], provider)
            self.assertEqual(command[command.index("--thinking") + 1], effort)
            self.assertEqual(
                command[command.index("-e") + 1], str(ROOT / ".pi/extensions/orchestrator.ts")
            )
            self.assertTrue(Path(command[command.index("--session-dir") + 1]).is_dir())
        with self.assertRaises(ConfigurationError):
            build_frontend_command(self.home, self.config, "claude", self.session)

    def test_private_prompt_file_for_each_frontend(self):
        for frontend, flag in (
            ("claude", "--append-system-prompt-file"),
            ("pi", "--append-system-prompt"),
        ):
            command = build_frontend_command(self.home, self.config, frontend, self.session)
            path = Path(command[command.index(flag) + 1])
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertIn((ROOT / "roles/orchestrator.md").read_text(), path.read_text())
            self.assertIn(self.config["personalization"]["communication_style"], path.read_text())

    def test_invalid_session_and_symlinks(self):
        with self.assertRaises(ConfigurationError):
            build_frontend_command(self.home, self.config, "claude", "../escape")
        (self.home / "data").symlink_to(self.home, target_is_directory=True)
        with self.assertRaises(ConfigurationError):
            build_frontend_command(self.home, self.config, "claude", self.session)

    def test_hook_settings_contract(self):
        command = build_frontend_command(self.home, self.config, "claude", self.session)
        settings_path = Path(command[command.index("--settings") + 1])
        self.assertEqual(settings_path.stat().st_mode & 0o777, 0o600)
        hooks = json.loads(settings_path.read_text())["hooks"]
        for event, matchers in hooks.items():
            for matcher in matchers:
                for hook in matcher["hooks"]:
                    self.assertIn(str(ROOT / "bin/orchestrator"), hook["command"])
                    self.assertNotIn("$CLAUDE_PROJECT_DIR", hook["command"])
                    if hook.get("asyncRewake"):
                        self.assertEqual(event, "Stop")
                        self.assertNotIn("async", hook)
                        self.assertEqual(hook["timeout"], 28800)
                        self.assertIn("27000", hook["command"])
                    else:
                        self.assertIn(hook["timeout"], (2, 5, 30))
