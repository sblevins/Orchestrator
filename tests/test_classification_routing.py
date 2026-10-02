"""Classification routing and caller-controlled effort, without launching workers."""

import unittest
from copy import deepcopy

from orchestrator.routing import RoutingError, policy_readiness, resolve_selection, validate_policy


class ClassificationRoutingTests(unittest.TestCase):
    def setUp(self):
        self.pi = {"harness": "pi", "provider": "openai", "model": "gpt-exact-123"}
        self.claude = {"harness": "claude", "model": "Opus"}
        self.policy = {"classifications": {"coding": self.pi, "review": self.claude}}
        self.choice = {"classification": "coding", "difficulty": "hard", "rationale": "Fits"}

    def test_single_profile_approval_requires_confirmation(self):
        for approval in ("user", "captain"):
            with self.subTest(approval=approval):
                policy = {"classifications": {"audit": {**self.claude, "approval": approval}}}
                selection = resolve_selection(policy, {**self.choice, "classification": "audit"})
                self.assertTrue(selection["requires_approval"])
                self.assertEqual(selection["model"], "opus")
        self.assertFalse(resolve_selection(self.policy, self.choice)["requires_approval"])
        with self.assertRaisesRegex(RoutingError, "approval must be user or captain"):
            validate_policy({"classifications": {"audit": {**self.claude, "approval": "auto"}}})

    def test_unknown_profile_keys_are_rejected_with_guidance(self):
        team = {"team": [self.claude, {**self.pi, "approval": "user"}]}
        for policy in (
            {"classifications": {"audit": {**self.claude, "aproval": "user"}}},
            {"classifications": {"audit": team}},
            {"rules": [{"when": "Any", "use": {**self.claude, "efort": "max"}}]},
            {"default": {**self.claude, "approval": "captain"}},
        ):
            with (
                self.subTest(policy=policy),
                self.assertRaisesRegex(RoutingError, "unsupported keys .*approval on the rule"),
            ):
                validate_policy(policy)

    def test_fallback_difficulty_fills_only_missing_effort_and_choice_overrides(self):
        pinned = {"harness": "pi", "provider": "openai-codex", "effort": "max-supported"}
        policy = {
            "classifications": {
                "audit": {
                    "team": [
                        {**pinned, "model": "Astra"},
                        {**pinned, "model": "Sol"},
                        {"harness": "claude", "model": "Opus"},
                        {"harness": "claude", "model": "Fable", "effort": "xhigh"},
                    ]
                }
            }
        }
        choice = {"classification": "audit", "rationale": "Audit"}
        standing = resolve_selection(policy, choice, fallback_difficulty="hard")
        self.assertEqual(
            [peer["effort"] for peer in standing["team"]],
            ["max-supported", "max-supported", "high", "xhigh"],
        )
        self.assertNotIn("difficulty", standing)
        explicit = resolve_selection(
            policy, {**choice, "difficulty": "easy"}, fallback_difficulty="hard"
        )
        self.assertEqual([peer["effort"] for peer in explicit["team"]], ["low"] * 4)
        exact = resolve_selection(policy, {**choice, "effort": "high"}, fallback_difficulty="hard")
        self.assertEqual([peer["effort"] for peer in exact["team"]], ["high"] * 4)

    def test_defaults_and_normalization(self):
        for classification, expected_model in (("coding", "gpt-exact-123"), ("review", "opus")):
            for difficulty, effort in (
                ("easy", "low"),
                ("hard", "high"),
                ("very hard", "max-supported" if classification == "coding" else "max"),
            ):
                with self.subTest(classification=classification, difficulty=difficulty):
                    result = resolve_selection(
                        self.policy,
                        {**self.choice, "classification": classification, "difficulty": difficulty},
                    )
                    self.assertEqual(result["model"], expected_model)
                    self.assertEqual(result["effort"], effort)
                    self.assertEqual(result["difficulty"], difficulty.replace(" ", "-"))
                    self.assertEqual(result["rule"], classification)
                    self.assertIsNone(result["candidate"])
                    self.assertFalse(result["requires_approval"])
                    self.assertIn("unverified", result["evidence"]["uncertainty"][0])

    def test_custom_mapping_and_explicit_effort(self):
        self.policy["difficulty_levels"] = {"VERY HARD": {"pi": "medium"}}
        choice = {**self.choice, "difficulty": "very_hard"}
        self.assertEqual(resolve_selection(self.policy, choice)["effort"], "medium")
        self.assertEqual(
            resolve_selection(self.policy, {**choice, "classification": "review"})["effort"], "max"
        )
        choice.pop("difficulty")
        choice["effort"] = "off"
        self.assertEqual(resolve_selection(self.policy, choice)["effort"], "off")
        self.pi["effort"] = "high"
        self.assertEqual(resolve_selection(self.policy, choice)["effort"], "off")
        choice.pop("effort")
        self.assertEqual(resolve_selection(self.policy, choice)["effort"], "high")

    def test_exact_ids_remain_exact_and_family_ids_normalize(self):
        for model in ("claude-opus-4-6", "claude-opus-4-20250514", "SONNET"):
            self.claude["model"] = model
            result = resolve_selection(self.policy, {**self.choice, "classification": "review"})
            self.assertEqual(result["model"], "sonnet" if model == "SONNET" else model)

    def test_model_only_readiness(self):
        for policy in (
            self.policy,
            {"default": self.pi},
            {"rules": [{"when": "coding", "use": self.pi}]},
        ):
            result = policy_readiness(policy)
            self.assertTrue(result["routable"])
            self.assertEqual(result["blockers"], [])
            self.assertIn("catalog support and access have not been verified", result["message"])

    def test_legacy_inheritance_and_effort_override(self):
        profile = {**self.claude, "effort": "low"}
        policy = {"default": profile}
        choice = {"rule": "default", "rationale": "Fits"}
        self.assertEqual(resolve_selection(policy, choice)["effort"], "low")
        self.assertEqual(resolve_selection(policy, {**choice, "effort": "max"})["effort"], "max")
        self.assertEqual(
            resolve_selection(policy, {**choice, "difficulty": "very-hard"})["effort"], "max"
        )
        with self.assertRaisesRegex(RoutingError, "choice.model conflicts"):
            resolve_selection(policy, {**choice, "model": "sonnet"})
        self.assertEqual(resolve_selection(policy, {**choice, "model": "OPUS"})["model"], "opus")

    def test_invalid_choices_have_field_errors(self):
        for field, value in (
            ("classification", "unknown"),
            ("classification", []),
            ("difficulty", "impossible"),
            ("effort", "low"),
            ("model", "replacement"),
            ("provider", "other"),
            ("harness", "claude"),
            ("candidate", 0),
            ("rule", "default"),
            ("team", []),
        ):
            with self.subTest(field=field), self.assertRaisesRegex(RoutingError, "choice\\."):
                resolve_selection(self.policy, {**self.choice, field: value})
        choice = {key: value for key, value in self.choice.items() if key != "difficulty"}
        with self.assertRaisesRegex(RoutingError, "effort"):
            resolve_selection(self.policy, choice)

    def test_malformed_classification_policies(self):
        invalid = [
            [],
            None,
            {"coding": [self.pi]},
            {"coding": {}},
            {"coding": {"harness": "pi", "model": "test"}},
            {"coding": {"harness": "claude"}},
            {"": self.pi},
        ]
        for classifications in invalid:
            with (
                self.subTest(classifications=classifications),
                self.assertRaisesRegex(RoutingError, "classifications"),
            ):
                validate_policy({"classifications": classifications})
        for levels in (
            [],
            {"unknown": {"pi": "low"}},
            {"hard": "high"},
            {"hard": {}},
            {"hard": {"pi": "ultra"}},
            {"hard": {"codex": "high"}},
            {"hard": {"pi": []}},
            {"very hard": {"pi": "high"}, "very-hard": {"pi": "low"}},
        ):
            with (
                self.subTest(levels=levels),
                self.assertRaisesRegex(RoutingError, "difficulty_levels"),
            ):
                validate_policy({**self.policy, "difficulty_levels": levels})

    def test_team_resolution_and_defensive_copy(self):
        policy = {"classifications": {"coding": {"team": [self.pi, self.claude]}}}
        original = deepcopy(policy)
        result = resolve_selection(policy, {**self.choice, "difficulty": "very-hard"})
        self.assertEqual([peer["effort"] for peer in result["team"]], ["max-supported", "max"])
        self.assertEqual(result["rule"], "coding")
        self.assertIsNone(result["candidate"])
        self.assertFalse(result["requires_approval"])
        result["team"][0]["model"] = "changed"
        self.assertEqual(policy, original)
        readiness = policy_readiness(policy)
        self.assertTrue(readiness["routable"])
        self.assertEqual(readiness["executable_profiles"], 2)

    def test_team_effort_and_mapping_apply_to_every_peer(self):
        policy = {
            "classifications": {"coding": {"team": [self.pi, self.claude]}},
            "difficulty_levels": {"hard": {"pi": "medium", "claude": "low"}},
        }
        result = resolve_selection(policy, self.choice)
        self.assertEqual([peer["effort"] for peer in result["team"]], ["medium", "low"])
        choice = {"classification": "coding", "effort": "high", "rationale": "Fits"}
        result = resolve_selection(policy, choice)
        self.assertEqual(result["effort"], "high")
        self.assertEqual([peer["effort"] for peer in result["team"]], ["high", "high"])
        with self.assertRaisesRegex(RoutingError, "unsupported by claude"):
            resolve_selection(policy, {**choice, "effort": "off"})
        distinct_providers = [self.pi, {**self.pi, "provider": "other-provider"}]
        result = resolve_selection(
            {"classifications": {"coding": {"team": distinct_providers}}}, self.choice
        )
        self.assertEqual(len(result["team"]), 2)

    def test_team_boundaries_and_distinctness(self):
        invalid = [
            [],
            [self.pi],
            [self.pi] * 9,
            [self.pi, {**self.pi, "effort": "low"}],
            [self.claude, {**self.claude, "model": "opus"}],
            [self.pi, {"team": [self.pi, self.claude]}],
            "not-array",
            [self.pi, [self.claude]],
        ]
        for team in invalid:
            with self.subTest(team=team), self.assertRaises(RoutingError):
                validate_policy({"classifications": {"coding": {"team": team}}})
        with self.assertRaisesRegex(RoutingError, "supports team"):
            validate_policy(
                {
                    "classifications": {
                        "coding": {"team": [self.pi, self.claude], "model": "ignored"}
                    }
                }
            )
        team = [{**self.pi, "model": f"exact-{index}"} for index in range(8)]
        result = resolve_selection({"classifications": {"coding": {"team": team}}}, self.choice)
        self.assertEqual(len(result["team"]), 8)

    def test_teams_do_not_bypass_executor_or_quota_gates(self):
        for invalid_peer in (
            {**self.pi, "provider": "anthropic"},
            {**self.pi, "floor": {"scope": "weekly", "min_percent": 10}},
        ):
            policy = {"classifications": {"coding": {"team": [self.claude, invalid_peer]}}}
            with self.assertRaises(RoutingError):
                resolve_selection(policy, self.choice, evidence={"status": "available"})
            self.assertFalse(policy_readiness(policy)["routable"])
        with self.assertRaisesRegex(RoutingError, "Quota-dependent"):
            resolve_selection(
                {"default": [self.pi, self.claude]},
                {"rule": "default", "candidate": 0, "difficulty": "hard", "rationale": "Fits"},
            )


if __name__ == "__main__":
    unittest.main()
