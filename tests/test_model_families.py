"""Family selectors preserve exact pins, routing authority and harness boundaries."""

import copy
import json
import tempfile
import unittest
import uuid
from pathlib import Path

from orchestrator.adapters import AdapterError, build_command, parse_result
from orchestrator.config import ConfigurationError, load_config, validate_config, validate_executor
from orchestrator.frontends import build_frontend_command
from orchestrator.models import CLAUDE_FAMILIES, model_family, normalize_model
from orchestrator.routing import RoutingError, policy_digest, resolve_selection, validate_policy
from orchestrator.store import Store
from orchestrator.worker_execution import build_worker_command


class ModelFamilyTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.home = Path(self.directory.name)
        self.config = load_config(self.home)

    def test_only_known_family_names_are_normalized(self):
        for family in CLAUDE_FAMILIES:
            self.assertEqual(normalize_model(family.title()), family)
            self.assertEqual(model_family(family.upper()), family)
        for exact in (
            "claude-opus-4-6",
            "claude-fable-5",
            "claude-sonnet-4-5-20250929",
            "claude-future-8",
            "gpt-6-astra",
            "Opus-next",
            " opus",
            "best",
            "opusplan",
        ):
            self.assertEqual(normalize_model(exact), exact)
            self.assertIsNone(model_family(exact))

    def test_role_worker_and_claude_foreground_pass_canonical_family(self):
        for family in sorted(CLAUDE_FAMILIES):
            with self.subTest(family=family):
                model = family.title()
                for name in ("planner", "orchestrator", "monitor"):
                    self.config["roles"][name]["model"] = model
                original = copy.deepcopy(self.config)
                validate_config(self.config)
                specialist = build_command(
                    self.config, "planner", "Inspect", self.home, self.home / "out"
                )
                worker = build_worker_command(
                    self.config,
                    {"harness": "claude", "model": model, "effort": "high"},
                    "read",
                    "Inspect",
                    self.home,
                    self.home / "out",
                )
                foreground = build_frontend_command(
                    self.home, self.config, "claude", str(uuid.uuid4())
                )
                for argv in (specialist, worker, foreground):
                    self.assertEqual(argv[argv.index("--model") + 1], family)
                self.assertEqual(self.config, original)

    def test_pins_are_unchanged_and_pi_launcher_does_not_fuzzy_match_family(self):
        role = self.config["roles"]["orchestrator"]
        for model in ("Opus", "Fable", "claude-opus-4-6"):
            role["model"] = model
            command = build_frontend_command(self.home, self.config, "pi", str(uuid.uuid4()))
            self.assertEqual(command[command.index("--provider") + 1], "anthropic")
            if model_family(model):
                self.assertNotIn("--model", command)
            else:
                self.assertEqual(command[command.index("--model") + 1], model)
        for family in ("opus", "Fable"):
            with self.assertRaises(ConfigurationError):
                validate_executor(
                    {"adapter": "pi", "provider": "openai", "model": family, "effort": "high"}
                )
            with self.assertRaises(ConfigurationError):
                validate_executor(
                    {"adapter": "claude", "provider": "openai", "model": family, "effort": "high"}
                )
        for invalid in ("best", "opusplan", "future", " Opus", "Opus "):
            with self.assertRaises(ConfigurationError):
                validate_executor({"adapter": "claude", "model": invalid, "effort": "high"})

    def test_policy_alias_casing_matches_without_rewriting_policy_or_exact_pins(self):
        for family in CLAUDE_FAMILIES:
            policy = {"default": {"harness": "claude", "model": family.title(), "effort": "high"}}
            original = copy.deepcopy(policy)
            digest = policy_digest(policy)
            choice = {
                "rule": "default",
                "model": family,
                "effort": "high",
                "rationale": "Policy fits",
            }
            profile = resolve_selection(policy, choice)
            self.assertEqual(profile["model"], family)
            self.assertEqual(policy, original)
            self.assertEqual(policy_digest(policy), digest)
            with self.assertRaises(RoutingError):
                resolve_selection(policy, {**choice, "model": f"claude-{family}-99"})
            pinned = {"default": {**policy["default"], "model": f"claude-{family}-4-6"}}
            with self.assertRaises(RoutingError):
                resolve_selection(pinned, choice)
            override = resolve_selection(
                {}, {**choice, "harness": "claude"}, operator_override=True
            )
            self.assertTrue(override["requires_approval"])
            self.assertEqual(override["model"], family)
            with self.assertRaises(RoutingError):
                resolve_selection(
                    {}, {**choice, "harness": "pi", "provider": "openai"}, operator_override=True
                )

    def test_duplicate_family_case_candidates_are_ambiguous(self):
        with self.assertRaisesRegex(RoutingError, "duplicate"):
            validate_policy(
                {
                    "default": [
                        {"harness": "claude", "model": "Opus", "effort": "high"},
                        {"harness": "claude", "model": "opus", "effort": "high"},
                    ]
                }
            )

    def test_reported_selection_is_fenced_with_terminal_result(self):
        store = Store(self.home)
        store.add_project("project", str(self.home))
        store.open_session("session", "claude", "project")
        task = store.enqueue("project", "session", "planner", "Inspect", self.config)
        claimed = store.claim_next(1)
        selection = {
            "requested_model": "Opus",
            "family": "opus",
            "reported_models": ["claude-opus-9-2"],
        }
        self.assertFalse(
            store.finish(task["id"], "stale-token", text="stale", model_selection=selection)
        )
        self.assertNotIn("model_selection", store.task(task["id"]))
        self.assertTrue(
            store.finish(task["id"], claimed["token"], text="result", model_selection=selection)
        )
        self.assertEqual(store.task(task["id"])["model_selection"], selection)
        self.assertFalse(
            store.finish(task["id"], claimed["token"], text="replay", model_selection={})
        )
        self.assertEqual(store.task(task["id"])["model_selection"], selection)

    def test_reported_models_are_bounded_not_mistaken_for_single_primary(self):
        terminal = {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "session_id": "id",
            "result": "Answer",
        }
        baseline = parse_result("claude", json.dumps(terminal), 0)
        self.assertNotIn("reported_models", baseline)
        terminal["modelUsage"] = {"claude-opus-9-2": {"inputTokens": 10}, "claude-haiku-4-5": {}}
        result = parse_result("claude", json.dumps(terminal), 0)
        self.assertEqual(result["reported_models"], ["claude-haiku-4-5", "claude-opus-9-2"])
        self.assertNotIn("resolved_model", result)
        for usage in (
            [],
            None,
            {"bad\u001bname": {}},
            {"good": []},
            {"x" * 257: {}},
            {str(index): {} for index in range(33)},
        ):
            with self.subTest(usage=usage), self.assertRaises(AdapterError):
                parse_result("claude", json.dumps({**terminal, "modelUsage": usage}), 0)


if __name__ == "__main__":
    unittest.main()
