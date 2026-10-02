"""Configuration tests use private temporary homes and no external harnesses."""

import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from orchestrator.config import (
    DEFAULT_CONFIG,
    ConfigurationError,
    load_config,
    role_config,
    validate_config,
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
            "critic": ("pi", "gpt-6-astra", "high"),
            "monitor": ("claude", "claude-opus-5-5", "high"),
        }
        for name, values in expected.items():
            role = role_config(self.defaults, name)
            self.assertEqual(tuple(role[key] for key in ("adapter", "model", "effort")), values)
        self.assertEqual(self.defaults["frontends"]["preferred"], "claude")
        self.assertEqual(self.defaults["workers"], {"enabled": True})
        self.assertEqual(self.defaults["routing"], {"enabled": True, "rules": [], "first_mate": {}})

    def test_provider_harness_and_effort_are_not_silently_changed(self):
        critic = self.defaults["roles"]["critic"]
        self.assertEqual(critic["provider"], "openai-codex")
        for changes in (
            {"provider": "anthropic"},
            {"model": "claude-opus-5-5"},
            {"provider": ""},
            {"effort": "ultra"},
            {"adapter": "codex"},
        ):
            config = deepcopy(self.defaults)
            config["roles"]["critic"].update(changes)
            with self.subTest(changes=changes), self.assertRaises(ConfigurationError):
                validate_config(config)
        config = deepcopy(self.defaults)
        config["roles"]["critic"].update(provider="new-vendor", model="future-model", effort="off")
        validate_config(config)
        for changes in ({"provider": "openai"}, {"model": "gpt-6-astra"}):
            config = deepcopy(self.defaults)
            config["roles"]["planner"].update(changes)
            with self.assertRaises(ConfigurationError):
                validate_config(config)

    def test_no_role_has_a_dollar_budget(self):
        for name in self.defaults["roles"]:
            with self.subTest(role=name):
                self.assertNotIn("max_budget_usd", role_config(self.defaults, name))

    def test_obsolete_budget_overrides_require_migration(self):
        for relative, project in (
            ("config/local.toml", None),
            ("config/projects/example.toml", "example"),
        ):
            for name in self.defaults["roles"]:
                with self.subTest(path=relative, role=name):
                    path = self.write(relative, f"[roles.{name}]\nmax_budget_usd=5.0\n")
                    try:
                        with self.assertRaisesRegex(
                            ConfigurationError,
                            rf"roles\.{name}\.max_budget_usd.*removed.*Remove.*no per-role dollar caps",
                        ):
                            load_config(self.home, project)
                    finally:
                        path.unlink()

    def test_shared_execution_limits_and_simple_roles(self):
        self.assertEqual(self.defaults["supervisor"]["task_timeout_seconds"], 900)
        for role in self.defaults["roles"].values():
            self.assertTrue({"timeout_seconds", "memory", "cpus"}.isdisjoint(role))
        for field, value in (("timeout_seconds", 10), ("memory", "2G"), ("cpus", 1)):
            config = deepcopy(self.defaults)
            config["roles"]["planner"][field] = value
            with self.assertRaisesRegex(ConfigurationError, "shared supervisor"):
                validate_config(config)
        for field in ("task_memory", "frontend_memory"):
            for value in ("63M", "1025G", "2GB", "0G", "1G;evil"):
                config = deepcopy(self.defaults)
                config["supervisor"][field] = value
                with self.assertRaises(ConfigurationError):
                    validate_config(config)
        self.write("config/local.toml", '[supervisor]\ntask_memory="4G"\ntask_cpus=2\n')
        configured = load_config(self.home)
        self.assertEqual(configured["supervisor"]["task_memory"], "4G")
        self.assertEqual(configured["supervisor"]["task_cpus"], 2)

    def test_graph_and_execution_defaults(self):
        self.assertEqual(
            self.defaults["planning"],
            {
                "structure": "graph",
                "workflow": "plan-review",
                "clarification": "material",
                "max_review_rounds": 3,
            },
        )
        self.assertEqual(
            self.defaults["execution"],
            {
                "mode": "restricted",
                "unattended": False,
                "worker_difficulty": "hard",
                "base_ref": "HEAD",
                "max_parallel": 3,
                "dependency_failure": "block",
            },
        )

    def test_custom_workflow_and_execution_overrides(self):
        self.write(
            "config/local.toml",
            '[planning]\nworkflow="custom-review_2"\n'
            "max_review_rounds=5\n[execution]\nmax_parallel=4\n"
            'dependency_failure="cancel"\n',
        )
        self.write(
            "config/projects/alpha.toml",
            '[planning]\nworkflow="project-review"\n[execution]\nmax_parallel=2\n',
        )
        local = load_config(self.home)
        project = load_config(self.home, "alpha")
        self.assertEqual(local["planning"]["workflow"], "custom-review_2")
        self.assertEqual(project["planning"]["workflow"], "project-review")
        self.assertEqual(project["planning"]["max_review_rounds"], 5)
        self.assertEqual(project["planning"]["structure"], "graph")
        self.assertEqual(
            project["execution"],
            {**self.defaults["execution"], "max_parallel": 2, "dependency_failure": "cancel"},
        )
        self.assertEqual(local["execution"]["max_parallel"], 4)
        # Config selects a name. The graph module owns template loading and validation.
        self.assertFalse((self.home / "config/workflows").exists())

    def test_invalid_planning_and_execution_settings(self):
        cases = {
            ("planning", "structure"): [None, "list", "", [], True],
            ("planning", "workflow"): [
                None,
                "",
                "../escape",
                "/tmp/escape",
                "a/b",
                "a\\\\b",
                "a.json",
                "a\n",
                "a\x00",
                "é",
                "a" * 129,
                True,
                1,
                [],
            ],
            ("planning", "max_review_rounds"): [
                None,
                True,
                0,
                -1,
                101,
                1.5,
                "3",
                float("nan"),
                float("inf"),
            ],
            ("execution", "max_parallel"): [
                None,
                True,
                0,
                -1,
                65,
                1.5,
                "3",
                float("nan"),
                float("inf"),
            ],
            ("execution", "dependency_failure"): [None, "ignore", "continue", "", [], True],
        }
        for (section, key), values in cases.items():
            for value in values:
                with self.subTest(section=section, key=key, value=value):
                    config = deepcopy(self.defaults)
                    config[section][key] = value
                    with self.assertRaises(ConfigurationError):
                        validate_config(config)
            config = deepcopy(self.defaults)
            del config[section][key]
            with self.subTest(section=section, missing=key), self.assertRaises(ConfigurationError):
                validate_config(config)

    def test_planning_execution_boundaries(self):
        for rounds, parallel, workflow in ((1, 1, "a"), (100, 64, "A" * 128)):
            config = deepcopy(self.defaults)
            config["planning"].update(max_review_rounds=rounds, workflow=workflow)
            config["execution"]["max_parallel"] = parallel
            validate_config(config)

    def test_frontend_launcher_executables(self):
        for name in ("claude", "pi"):
            config = deepcopy(self.defaults)
            executable = f"/private/tools/{name}-launcher"
            config["frontends"][name]["command"] = [executable]
            validate_config(config)
            for command in (
                [executable, "--flag"],
                [executable, "--dangerously-skip-permissions"],
                executable,
                [],
            ):
                with self.subTest(frontend=name, command=command):
                    config["frontends"][name]["command"] = command
                    with self.assertRaises(ConfigurationError):
                        validate_config(config)

    def test_layer_order_and_project_isolation(self):
        self.write(
            "config/local.toml",
            '[roles.planner]\nmodel="claude-future-model"\nallowed_tools=["Read"]\n[supervisor]\ntask_timeout_seconds=400\n',
        )
        self.write(
            "config/projects/alpha.toml",
            '[roles.planner]\nmodel="claude-project-model"\nallowed_tools=[]\n',
        )
        alpha = load_config(self.home, "alpha")["roles"]["planner"]
        beta = load_config(self.home, "beta")["roles"]["planner"]
        self.assertEqual(alpha["model"], "claude-project-model")
        self.assertEqual(alpha["allowed_tools"], [])
        self.assertEqual(load_config(self.home, "alpha")["supervisor"]["task_timeout_seconds"], 400)
        self.assertEqual(beta["model"], "claude-future-model")
        self.assertEqual(beta["allowed_tools"], ["Read"])
        self.assertEqual(beta["effort"], "high")

    def test_invalid_intermediate_value_can_be_overridden(self):
        self.write("config/local.toml", '[roles.planner]\neffort="invalid"\n')
        self.write("config/projects/fixed.toml", '[roles.planner]\neffort="high"\n')
        load_config(self.home, "fixed")
        with self.assertRaises(ConfigurationError):
            load_config(self.home)

    def test_future_models_and_pi_frontend(self):
        self.write(
            "config/local.toml",
            '[roles.critic]\nmodel="future-vendor/model-99"\n'
            'effort="xhigh"\n[frontends]\npreferred="pi"\n',
        )
        config = load_config(self.home)
        self.assertEqual(config["roles"]["critic"]["model"], "future-vendor/model-99")
        self.assertEqual(config["frontends"]["preferred"], "pi")

    def test_role_copy_is_independent(self):
        role = role_config(self.defaults, "planner")
        role["allowed_tools"].clear()
        self.assertEqual(
            self.defaults["roles"]["planner"]["allowed_tools"], ["Read", "Glob", "Grep"]
        )
        for name in ("worker", "", [], None):
            with self.subTest(name=name), self.assertRaises(ConfigurationError):
                role_config(self.defaults, name)

    def test_project_id_rejects_traversal_and_bad_types(self):
        for project_id in (
            "../outside",
            "/tmp/outside",
            "a/b",
            "a\\b",
            ".",
            "..",
            "",
            "a.toml",
            "a\x00",
            "a\n",
            "a" * 129,
            True,
            1,
            [],
            "é",
        ):
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
        with (
            patch("orchestrator.config.DEFAULT_CONFIG", self.home / "missing.toml"),
            self.assertRaises(ConfigurationError),
        ):
            load_config(self.home)

    def test_bad_role_values(self):
        bad_values = {
            "adapter": ["pi", "unknown", [], True],
            "model": ["", "  ", "a\x00", 5, True],
            "effort": ["unknown", [], True],
            "timeout_seconds": [0, -1, True, float("nan"), float("inf"), 86401, "60"],
            "memory": [2, True, "0G", "2GB", "-1G", "1G;evil", "63M", "1025G", "9" * 5000 + "G"],
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

    def test_routing_requires_project_policy_not_inline_models(self):
        for section, key, value in (
            ("workers", "enabled", 0),
            ("workers", "model", "anything"),
            ("routing", "rules", [{}]),
            ("routing", "first_mate", {"model": "x"}),
            ("routing", "default_model", "anything"),
        ):
            with self.subTest(section=section, key=key):
                config = deepcopy(self.defaults)
                config[section][key] = value
                with self.assertRaises(ConfigurationError):
                    validate_config(config)

    def test_commands_cannot_override_security_flags(self):
        for command in (
            "claude",
            [],
            [True],
            [""],
            ["--bad"],
            ["claude", "--version"],
            ["claude", "--dangerously-skip-permissions"],
            ["codex", "--dangerously-bypass-approvals-and-sandbox"],
            ["codex", "-c", 'sandbox_mode="danger-full-access"'],
            ["dangerously-skip-permissions"],
        ):
            with self.subTest(command=command):
                config = deepcopy(self.defaults)
                config["adapters"]["claude"]["command"] = command
                with self.assertRaises(ConfigurationError):
                    validate_config(config)

    def test_required_tables_and_fields(self):
        for key in (
            "supervisor",
            "planning",
            "execution",
            "personalization",
            "workers",
            "routing",
            "roles",
            "adapters",
        ):
            for replacement in (None, [], "table"):
                with self.subTest(key=key, replacement=replacement):
                    config = deepcopy(self.defaults)
                    config[key] = replacement
                    with self.assertRaises(ConfigurationError):
                        validate_config(config)
        for key in (
            "adapter",
            "model",
            "effort",
            "allowed_tools",
        ):
            config = deepcopy(self.defaults)
            del config["roles"]["planner"][key]
            with self.subTest(key=key), self.assertRaises(ConfigurationError):
                validate_config(config)

    def test_templates_and_prompts(self):
        root = DEFAULT_CONFIG.parent.parent
        self.write("config/local.toml", (root / "config/local.example.toml").read_text())
        self.write(
            "config/projects/example.toml", (root / "config/project.example.toml").read_text()
        )
        config = load_config(self.home, "example")
        for role in config["roles"].values():
            self.assertTrue((root / role["prompt_path"]).is_file())


if __name__ == "__main__":
    unittest.main()
