"""Conversational execution choices remain private to the bound project."""

import unittest
from copy import deepcopy

from orchestrator.config import ConfigurationError, load_config, validate_config
from orchestrator.project_settings import configure_project
from tests import test_project_settings as settings_tests


class ExecutionPreferenceTests(unittest.TestCase):
    def setUp(self):
        settings_tests.ProjectSettingsTests.setUp(self)

    def test_trusted_unattended_setup_does_not_change_other_projects(self):
        beta = load_config(self.home, "beta")
        shared = load_config(self.home)
        settings = {
            "execution": {
                "mode": "trusted",
                "unattended": True,
                "base_ref": "crosschain/integration",
                "worker_difficulty": "very-hard",
            },
            "planning": {"clarification": "material"},
            "monitoring": {"quiet_seconds": 30},
            "commands": {"enabled": True, "network": True},
        }
        configured = configure_project(self.store, "alpha", settings)
        self.assertTrue(configured["settings"]["execution"]["unattended"])
        self.assertEqual(configured["settings"]["execution"]["mode"], "trusted")
        self.assertEqual(load_config(self.home, "beta"), beta)
        self.assertEqual(load_config(self.home), shared)
        reverted = configure_project(self.store, "alpha", {"execution": {"mode": "restricted"}})
        self.assertEqual(reverted["settings"]["execution"]["mode"], "restricted")
        self.assertEqual(reverted["settings"]["execution"]["base_ref"], "crosschain/integration")

    def test_malformed_new_settings_return_actionable_configuration_errors(self):
        defaults = load_config(self.home)
        for section, key, bad in (
            ("execution", "mode", []),
            ("execution", "unattended", "yes"),
            ("execution", "worker_difficulty", {}),
            ("execution", "base_ref", "--all"),
            ("execution", "base_ref", "main\nother"),
            ("planning", "clarification", []),
            ("monitoring", "quiet_seconds", -1),
            ("commands", "enabled", 1),
            ("commands", "timeout_seconds", 0),
            ("commands", "tool_paths", {"python": "relative"}),
            ("images", "enabled", "yes"),
            ("images", "output_format", "exe"),
        ):
            with self.subTest(section=section, key=key, value=bad):
                configuration = deepcopy(defaults)
                configuration[section][key] = bad
                with self.assertRaisesRegex(ConfigurationError, section):
                    validate_config(configuration)
