"""Routing policy tests never launch harnesses or access private quota accounts."""

import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from orchestrator.routing import (
    RoutingError,
    capture_quota_evidence,
    load_policy,
    policy_digest,
    resolve_selection,
    validate_policy,
)


class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.profile = {
            "harness": "pi",
            "provider": "test-provider",
            "model": "test-model",
            "effort": "high",
        }
        self.policy = {"default": deepcopy(self.profile)}
        self.choice = {
            "rule": "default",
            "model": "test-model",
            "effort": "high",
            "rationale": "Fits the task",
        }
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.home = Path(self.directory.name) / "home"
        self.project = Path(self.directory.name) / "project"

    def write_policy(self, project, value):
        path = (
            self.project / ".orchestrator" if project else self.home / "config"
        ) / "crew-dispatch.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def test_loading_precedence_and_digest(self):
        home_path = self.write_policy(False, self.policy)
        self.assertEqual(load_policy(self.home, self.project)["source"], str(home_path))
        project_policy = {"default": {**self.profile, "effort": "low"}}
        project_path = self.write_policy(True, project_policy)
        loaded = load_policy(self.home, self.project)
        self.assertEqual(loaded["source"], str(project_path))
        self.assertEqual(loaded["policy"], project_policy)
        self.assertEqual(loaded["digest"], policy_digest(project_policy))
        self.assertNotEqual(loaded["digest"], policy_digest(self.policy))
        reordered = {"default": dict(reversed(list(self.profile.items())))}
        self.assertEqual(policy_digest(self.policy), policy_digest(reordered))

    def test_no_policy_has_no_guessed_default(self):
        with self.assertRaisesRegex(RoutingError, "No worker policy configured"):
            load_policy(self.home, self.project)

    def test_invalid_project_never_falls_back(self):
        self.write_policy(False, self.policy)
        path = self.write_policy(True, self.policy)
        for content in ("{", "{}", '{"default": {}, "default": {}}', '{"default": NaN}', "\udcff"):
            with self.subTest(content=repr(content)):
                path.write_bytes(content.encode("utf-8", errors="surrogatepass"))
                with self.assertRaises(RoutingError):
                    load_policy(self.home, self.project)
        path.unlink()
        path.symlink_to(path.parent / "missing")
        with self.assertRaisesRegex(RoutingError, "symlink"):
            load_policy(self.home, self.project)

    def test_oversized_policy_is_bounded(self):
        path = self.write_policy(True, self.policy)
        path.write_bytes(b" " * (1024 * 1024 + 1))
        with self.assertRaisesRegex(RoutingError, "limit"):
            load_policy(self.home, self.project)

    def test_defensive_copy_preserves_extensions(self):
        original = {**self.policy, "extension": {"nested": [1]}}
        validated = validate_policy(original)
        validated["default"]["model"] = "changed"
        validated["extension"]["nested"].append(2)
        self.assertEqual(original["default"]["model"], "test-model")
        self.assertEqual(original["extension"]["nested"], [1])

    def test_empty_project_policy_is_valid_but_cannot_select_workers(self):
        self.assertEqual(validate_policy({"rules": []}), {"rules": []})
        self.write_policy(True, {"rules": []})
        self.assertEqual(load_policy(self.home, self.project)["policy"], {"rules": []})
        with self.assertRaisesRegex(RoutingError, "no default"):
            resolve_selection(
                {"rules": []},
                {
                    "rule": "default",
                    "model": "gpt-5",
                    "effort": "high",
                    "rationale": "Inspect",
                },
            )

    def test_invalid_schema(self):
        invalid = [None, [], {}, {"rules": {}}, {"default": []}, {"default": None}]
        invalid.extend(
            {"default": {**self.profile, field: value}}
            for field, value in (
                ("harness", ""),
                ("model", " "),
                ("model", "--flag"),
                ("effort", "invented"),
                ("provider", "Provider"),
                ("provider", "has/slash"),
                ("floor", {"scope": "weekly", "min_percent": True}),
                ("floor", {"scope": "weekly", "min_percent": float("nan")}),
                ("floor", {"scope": "weekly", "min_percent": 101}),
            )
        )
        invalid.extend(
            {"rules": [{"when": "a task", "use": self.profile, field: value}]}
            for field, value in (
                ("when", ""),
                ("approval", "automatic"),
                ("min_confidence", True),
                ("min_confidence", 1.1),
                ("select", "first-match"),
                ("floor", {"scope": "weekly", "min_percent": 20}),
            )
        )
        invalid.extend(
            [
                {"default": [self.profile, self.profile]},
                {**self.policy, "extension": (1, 2)},
                {**self.policy, "extension": {1: "not a JSON key"}},
                {
                    "default": {
                        **self.profile,
                        "floor": {"scope": "weekly", "min_percent": 10**1000},
                    }
                },
            ]
        )
        for policy in invalid:
            with self.subTest(policy=policy), self.assertRaises(RoutingError):
                validate_policy(policy)

    def test_best_fit_is_callers_choice_not_first_match(self):
        policy = {
            "rules": [
                {"when": "all coding", "use": {**self.profile, "effort": "low"}},
                {"when": "careful coding", "use": self.profile, "approval": "captain"},
            ]
        }
        result = resolve_selection(policy, {**self.choice, "rule": 1})
        self.assertEqual(result["rule"], 1)
        self.assertEqual(result["effort"], "high")
        self.assertTrue(result["requires_approval"])
        self.assertEqual(result["candidate"], 0)

    def test_selection_is_defensive_and_explicit(self):
        original_policy, original_choice = deepcopy(self.policy), deepcopy(self.choice)
        result = resolve_selection(self.policy, self.choice)
        result["model"] = "changed"
        self.assertEqual(self.policy, original_policy)
        self.assertEqual(self.choice, original_choice)
        for field in ("rule", "rationale"):
            choice = {key: value for key, value in self.choice.items() if key != field}
            with self.subTest(field=field), self.assertRaises(RoutingError):
                resolve_selection(self.policy, choice)
        for field, value in (
            ("rule", True),
            ("rule", -1),
            ("candidate", True),
            ("candidate", -1),
            ("model", "other"),
            ("provider", "other"),
            ("harness", "claude"),
        ):
            with self.subTest(field=field, value=value), self.assertRaises(RoutingError):
                resolve_selection(self.policy, {**self.choice, field: value})

    def test_omitted_axes_are_explicit_never_inherited(self):
        policy = {"default": {"harness": "pi"}}
        with self.assertRaisesRegex(RoutingError, "explicit provider"):
            resolve_selection(policy, self.choice)
        result = resolve_selection(policy, {**self.choice, "provider": "test-provider"})
        self.assertEqual(result["provider"], "test-provider")
        self.assertEqual(result["model"], "test-model")

    def test_supported_pi_efforts_and_no_downgrade(self):
        for effort in ("off", "minimal", "low", "medium", "high", "xhigh"):
            profile = {**self.profile, "effort": effort}
            self.assertEqual(
                resolve_selection({"default": profile}, {**self.choice, "effort": effort})[
                    "effort"
                ],
                effort,
            )
        for effort in ("max", "ultra"):
            with self.subTest(effort=effort), self.assertRaisesRegex(RoutingError, "unsupported"):
                resolve_selection(
                    {"default": {**self.profile, "effort": effort}},
                    {**self.choice, "effort": effort},
                )

    def test_claude_max_is_coordinator_controlled(self):
        profile = {"harness": "claude", "model": "claude-test"}
        choice = {**self.choice, "model": "claude-test", "effort": "max"}
        self.assertEqual(resolve_selection({"default": profile}, choice)["effort"], "max")
        self.assertEqual(
            resolve_selection({"default": {**profile, "effort": "max"}}, choice)["effort"], "max"
        )

    def test_executor_and_provider_compatibility(self):
        for harness, model, provider in (
            ("codex", "test-model", "test-provider"),
            ("opencode", "test-model", "test-provider"),
            ("pi", "claude-test", "test-provider"),
            ("pi", "test-model", "anthropic"),
            ("pi", "anthropic/hidden-model", "test-provider"),
            ("claude", "test-model", "anthropic"),
            ("claude", "claude-test", "test-provider"),
        ):
            profile = {"harness": harness, "model": model, "provider": provider, "effort": "high"}
            validate_policy({"default": profile})
            with self.subTest(profile=profile), self.assertRaises(RoutingError):
                resolve_selection({"default": profile}, {**self.choice, "model": model})

    def test_confidence_gate(self):
        policy = {"rules": [{"when": "specific task", "use": self.profile, "min_confidence": 0.8}]}
        for confidence in (None, 0.79, True, float("nan"), 2):
            choice = {**self.choice, "rule": 0}
            if confidence is not None:
                choice["confidence"] = confidence
            with self.subTest(confidence=confidence), self.assertRaises(RoutingError):
                resolve_selection(policy, choice)
        self.assertEqual(
            resolve_selection(policy, {**self.choice, "rule": 0, "confidence": 0.8})["rule"], 0
        )

    def test_arrays_and_floors_fail_closed_even_with_fabricated_metrics(self):
        policies = [
            {"default": [self.profile]},
            {"default": [self.profile, {**self.profile, "model": "second-model"}]},
            {"default": {**self.profile, "floor": {"scope": "weekly", "min_percent": 10}}},
            {
                "rules": [
                    {
                        "when": "task",
                        "use": self.profile,
                        "floor": {
                            "scope": "weekly",
                            "min_percent": 10,
                            "provider": "test-provider",
                        },
                    }
                ]
            },
            {"rules": [{"when": "task", "use": self.profile, "select": "quota-balanced"}]},
        ]
        for policy in policies:
            for evidence in (
                None,
                capture_quota_evidence(),
                {"status": "available", "source": "program", "remaining": 100},
            ):
                choice = {
                    **self.choice,
                    "rule": 0 if "rules" in policy else "default",
                    "candidate": 0,
                }
                with (
                    self.subTest(policy=policy, evidence=evidence),
                    self.assertRaisesRegex(RoutingError, "Quota-dependent"),
                ):
                    resolve_selection(policy, choice, evidence=evidence)

    def test_override_bypasses_policy_not_execution_or_approval(self):
        choice = {**self.choice, "harness": "pi", "provider": "test-provider"}
        result = resolve_selection(None, choice, operator_override=True)
        self.assertEqual(result["rule"], "override")
        self.assertTrue(result["requires_approval"])
        for changes in ({"harness": "codex"}, {"effort": "max"}, {"model": "claude-test"}):
            with self.subTest(changes=changes), self.assertRaises(RoutingError):
                resolve_selection(None, {**choice, **changes}, operator_override=True)
        with self.assertRaises(RoutingError):
            resolve_selection(None, choice, operator_override="operator")

    def test_quota_capture_is_sanitized_unavailable_and_never_probes_auth(self):
        with patch("subprocess.run", side_effect=AssertionError("must not run commands")):
            evidence = capture_quota_evidence()
        self.assertEqual(evidence["status"], "unavailable")
        self.assertEqual(evidence["source"], "program")
        self.assertNotIn("accounts", evidence)
        self.assertNotIn("credentials", evidence)
        result = resolve_selection(
            self.policy, self.choice, evidence={"token": "PRIVATE", "account": "PRIVATE"}
        )
        self.assertNotIn("PRIVATE", json.dumps(result))
        self.assertFalse(result["requires_approval"])


if __name__ == "__main__":
    unittest.main()
