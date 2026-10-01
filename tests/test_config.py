"""Configuration tests use private temporary homes and no external harnesses."""

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from orchestrator.config import (
    ConfigurationError, DEFAULT_CONFIG, load_config, role_config, validate_config,
)


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.home = Path(self.directory.name)
        self.defaults = load_config(self.home)

    def write(self, relative, content):
        path = self.home / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def test_defaults(self):
        validate_config(self.defaults)
        expected = {
            "orchestrator": ("claude", "claude-sonnet-5-5", "low"),
            "planner": ("claude", "claude-opus-5-5", "high"),
            "critic": ("codex", "gpt-6-astra", "high"),
            "monitor": ("claude", "claude-opus-5-5", "high"),
        }
        for name, values in expected.items():
            role = role_config(self.defaults, name)
            self.assertEqual(tuple(role[key] for key in ("adapter", "model", "effort")), values)
        self.assertEqual(self.defaults["frontends"]["preferred"], "claude")
        self.assertEqual(self.defaults["workers"], {"enabled": False})
        self.assertEqual(self.defaults["routing"],
                         {"enabled": False, "rules": [], "first_mate": {}})

    def test_layer_order_and_project_isolation(self):
        self.write("config/local.toml", '[roles.planner]\nmodel="future-model"\n'
                   'allowed_tools=["Read"]\ntimeout_seconds=400\n')
        self.write("config/projects/alpha.toml", '[roles.planner]\n'
                   'model="project-model"\nallowed_tools=[]\n')
        alpha = load_config(self.home, "alpha")["roles"]["planner"]
        beta = load_config(self.home, "beta")["roles"]["planner"]
        self.assertEqual(alpha["model"], "project-model")
        self.assertEqual(alpha["allowed_tools"], [])
        self.assertEqual(alpha["timeout_seconds"], 400)
        self.assertEqual(beta["model"], "future-model")
        self.assertEqual(beta["allowed_tools"], ["Read"])
        self.assertEqual(beta["effort"], "high")

    def test_invalid_intermediate_value_can_be_overridden(self):
        self.write("config/local.toml", '[roles.planner]\neffort="xhigh"\n')
        self.write("config/projects/fixed.toml", '[roles.planner]\neffort="high"\n')
        load_config(self.home, "fixed")
        with self.assertRaises(ConfigurationError):
            load_config(self.home)

    def test_future_models_and_pi_frontend(self):
        self.write("config/local.toml", '[roles.critic]\nmodel="future-vendor/model-99"\n'
                   'effort="xhigh"\n[frontends]\npreferred="pi"\n')
        config = load_config(self.home)
        self.assertEqual(config["roles"]["critic"]["model"], "future-vendor/model-99")
        self.assertEqual(config["frontends"]["preferred"], "pi")

    def test_role_copy_is_independent(self):
        role = role_config(self.defaults, "planner")
        role["allowed_tools"].clear()
        self.assertEqual(self.defaults["roles"]["planner"]["allowed_tools"],
                         ["Read", "Glob", "Grep"])
        for name in ("worker", "", [], None):
            with self.subTest(name=name), self.assertRaises(ConfigurationError):
                role_config(self.defaults, name)

    def test_project_id_rejects_traversal_and_bad_types(self):
        for project_id in ("../outside", "/tmp/outside", "a/b", "a\\b", ".", "..", "",
                           "a.toml", "a\x00", "a\n", "a" * 129, True, 1, [], "é"):
            with self.subTest(project_id=project_id), self.assertRaises(ConfigurationError):
                load_config(self.home, project_id)
        load_config(self.home, "Project_1-abc")

    def test_symlink_escape_rejected(self):
        with tempfile.TemporaryDirectory() as outside:
            self.write("config/projects/inside.toml", "")
            (self.home / "config/projects/escape.toml").symlink_to(Path(outside) / "missing")
            with self.assertRaises(ConfigurationError):
                load_config(self.home, "escape")
            (self.home / "config/local.toml").symlink_to(Path(outside) / "missing")
            with self.assertRaises(ConfigurationError):
                load_config(self.home)

    def test_errors_identify_bad_files(self):
        path = self.write("config/local.toml", "[broken")
        with self.assertRaisesRegex(ConfigurationError, "local.toml"):
            load_config(self.home)
        path.write_bytes(b"\xff")
        with self.assertRaises(ConfigurationError):
            load_config(self.home)
        path.unlink()
        path.mkdir()
        with self.assertRaises(ConfigurationError):
            load_config(self.home)

    def test_missing_tracked_defaults_are_an_error(self):
        with patch("orchestrator.config.DEFAULT_CONFIG", self.home / "missing.toml"):
            with self.assertRaises(ConfigurationError):
                load_config(self.home)

    def test_bad_role_values(self):
        bad_values = {
            "adapter": ["pi", "unknown", [], True],
            "model": ["", "  ", "a\x00", 5, True],
            "effort": ["xhigh", "unknown", [], True],
            "timeout_seconds": [0, -1, True, float("nan"), float("inf"), 86401, "60"],
            "max_budget_usd": [0, True, float("nan"), float("inf"), 1001, "5"],
            "memory": [2, True, "0G", "2GB", "-1G", "1G;evil", "63M", "1025G",
                       "9" * 5000 + "G"],
            "cpus": [True, 0, -1, 65, 1.5, "1"],
            "allowed_tools": ["Read", ["Bash"], ["Write"], ["Read", "Read"], [[]]],
            "prompt_path": ["../roles/planner.md", "/tmp/planner.md", "roles/critic.md", []],
        }
        for key, values in bad_values.items():
            for value in values:
                with self.subTest(key=key, value=str(value)[:50]):
                    config = deepcopy(self.defaults)
                    config["roles"]["planner"][key] = value
                    with self.assertRaises(ConfigurationError):
                        validate_config(config)

    def test_bad_supervisor_values(self):
        for key in self.defaults["supervisor"]:
            for value in (True, "1", 0, float("nan"), float("inf"), 100000):
                with self.subTest(key=key, value=value):
                    config = deepcopy(self.defaults)
                    config["supervisor"][key] = value
                    with self.assertRaises(ConfigurationError):
                        validate_config(config)
        config = deepcopy(self.defaults)
        config["supervisor"]["stale_seconds"] = config["supervisor"]["heartbeat_seconds"]
        with self.assertRaises(ConfigurationError):
            validate_config(config)

    def test_no_workers_or_routing(self):
        for section, key, value in (
            ("workers", "enabled", True), ("workers", "enabled", 0),
            ("workers", "model", "anything"), ("routing", "enabled", True),
            ("routing", "rules", [{}]), ("routing", "first_mate", {"model": "x"}),
            ("routing", "default_model", "anything"),
        ):
            with self.subTest(section=section, key=key):
                config = deepcopy(self.defaults)
                config[section][key] = value
                with self.assertRaises(ConfigurationError):
                    validate_config(config)

    def test_commands_cannot_override_security_flags(self):
        for command in ("claude", [], [True], [""], ["--bad"], ["claude", "--version"],
                        ["claude", "--dangerously-skip-permissions"],
                        ["codex", "--dangerously-bypass-approvals-and-sandbox"],
                        ["codex", "-c", "sandbox_mode=\"danger-full-access\""],
                        ["dangerously-skip-permissions"]):
            with self.subTest(command=command):
                config = deepcopy(self.defaults)
                config["adapters"]["claude"]["command"] = command
                with self.assertRaises(ConfigurationError):
                    validate_config(config)

    def test_required_tables_and_fields(self):
        for key in ("supervisor", "personalization", "workers", "routing", "roles", "adapters"):
            for replacement in (None, [], "table"):
                with self.subTest(key=key, replacement=replacement):
                    config = deepcopy(self.defaults)
                    config[key] = replacement
                    with self.assertRaises(ConfigurationError):
                        validate_config(config)
        for key in ("adapter", "model", "effort", "timeout_seconds", "max_budget_usd",
                    "memory", "cpus", "allowed_tools"):
            config = deepcopy(self.defaults)
            del config["roles"]["planner"][key]
            with self.subTest(key=key), self.assertRaises(ConfigurationError):
                validate_config(config)

    def test_templates_and_prompts(self):
        root = DEFAULT_CONFIG.parent.parent
        self.write("config/local.toml", (root / "config/local.example.toml").read_text())
        self.write("config/projects/example.toml",
                   (root / "config/project.example.toml").read_text())
        config = load_config(self.home, "example")
        for role in config["roles"].values():
            self.assertTrue((root / role["prompt_path"]).is_file())


if __name__ == "__main__":
    unittest.main()
