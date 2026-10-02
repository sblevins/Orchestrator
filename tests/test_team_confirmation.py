"""One user confirmation gates the entire audit, even with standing authorization."""

import unittest

from orchestrator.autonomy import advance
from orchestrator.routing import resolve_selection
from tests import test_team_execution as team_tests


class TeamConfirmationTests(unittest.TestCase):
    operation = team_tests.TeamExecutionTests.operation

    def setUp(self):
        team_tests.TeamExecutionTests.setUp(self)
        self.operation(
            "cancel_worker",
            {"request_id": self.parent["id"], "reason": "Use confirmed audit fixture instead"},
        )

    def test_single_confirmation_releases_all_peers(self):
        self.operation("configure_project", {"settings": {"execution": {"unattended": True}}})
        policy = {
            "classifications": {
                "security_audit": {
                    "description": "In-depth entire codebase security audit; compare all findings.",
                    "approval": "user",
                    "team": [
                        {"harness": "claude", "model": f"claude-fixture-{index}", "effort": "max"}
                        for index in range(4)
                    ],
                }
            }
        }
        self.operation("setup_project", {"policy": policy})
        origin = self.operation("record_prompt", {"prompt": "Audit the whole codebase"})["event_id"]
        parent = self.operation(
            "request_worker", {"brief": "Full audit", "origin_event_id": origin}
        )
        selected = self.operation(
            "select_worker",
            {
                "request_id": parent["id"],
                "choice": {
                    "classification": "security_audit",
                    "rationale": "Requested audit team",
                },
            },
        )
        self.assertTrue(selected["profile"]["requires_approval"])
        advance(self.store)
        self.workers.dispatch_ready()
        self.assertEqual(self.workers.get(parent["id"])["state"], "selected")
        self.assertEqual(self.store.tasks(), [])
        self.operation(
            "approve_worker", {"request_id": parent["id"], "reason": "User confirmed audit"}
        )
        self.workers.dispatch_ready()
        self.workers.dispatch_ready()
        children = [
            child
            for child in self.workers.list("alpha")
            if child["selection_source"] == "team:" + parent["id"]
        ]
        self.assertEqual(len(children), 4)
        self.assertTrue(all(child["state"] == "queued" for child in children))
        self.assertTrue(all("send_team_message" in child["brief"] for child in children))
        self.assertTrue(all("In-depth entire codebase" in child["brief"] for child in children))

    def test_clean_confirmed_team_accepts_without_another_user_turn(self):
        from orchestrator.runtime import run_task

        self.operation("configure_project", {"settings": {"execution": {"unattended": True}}})
        harness = self.root / "offline-harness"
        harness.write_text(harness.read_text().replace("['Unverified hypothesis']", "[]"))
        policy = {
            "classifications": {
                "audit": {
                    "approval": "user",
                    "team": [
                        {"harness": "claude", "model": f"claude-fixture-{index}", "effort": "max"}
                        for index in range(4)
                    ],
                }
            }
        }
        self.operation("setup_project", {"policy": policy})
        origin = self.operation("record_prompt", {"prompt": "Audit"})["event_id"]
        parent = self.operation("request_worker", {"brief": "Audit", "origin_event_id": origin})
        self.operation(
            "select_worker",
            {
                "request_id": parent["id"],
                "choice": {
                    "classification": "audit",
                    "rationale": "Requested",
                },
            },
        )
        self.operation("approve_worker", {"request_id": parent["id"], "reason": "User confirmed"})
        self.workers.dispatch_ready()
        for _round in range(2):
            self.workers.dispatch_ready()
            count = 0
            while task := self.store.claim_next(4):
                self.assertEqual(run_task(self.home, task["id"], task["token"]), 0)
                self.workers.process_result(self.store.task(task["id"]))
                count += 1
            self.assertEqual(count, 4)
        self.workers.dispatch_ready()
        self.assertEqual(self.workers.get(parent["id"])["result"]["remaining_issues"], [])
        advance(self.store)
        self.assertEqual(self.workers.get(parent["id"])["state"], "accepted")

    def test_round_one_finding_waits_for_foreground_acceptance(self):
        from orchestrator.runtime import run_task

        self.operation("configure_project", {"settings": {"execution": {"unattended": True}}})
        harness = self.root / "offline-harness"
        harness.write_text(
            harness.read_text().replace(
                "['Unverified hypothesis']",
                "[] if comparison else ['Possible SQL injection in X']",
            )
        )
        origin = self.operation("record_prompt", {"prompt": "Audit"})["event_id"]
        parent = self.operation("request_worker", {"brief": "Audit", "origin_event_id": origin})
        self.operation(
            "select_worker",
            {
                "request_id": parent["id"],
                "choice": {"classification": "audit", "difficulty": "hard", "rationale": "Audit"},
            },
        )
        self.workers.dispatch_ready()
        for _round in range(2):
            self.workers.dispatch_ready()
            while task := self.store.claim_next(4):
                self.assertEqual(run_task(self.home, task["id"], task["token"]), 0)
                self.workers.process_result(self.store.task(task["id"]))
        self.workers.dispatch_ready()
        issues = self.workers.get(parent["id"])["result"]["remaining_issues"]
        self.assertEqual(
            issues, [f"Round 1 peer {index}: Possible SQL injection in X" for index in range(4)]
        )
        advance(self.store)
        self.assertEqual(self.workers.get(parent["id"])["state"], "candidate")
        accepted = self.operation(
            "accept_worker", {"request_id": parent["id"], "reason": "Reviewed audit findings"}
        )
        self.assertEqual(accepted["state"], "accepted")

    def test_latest_families_and_explicit_research_effort(self):
        # Provider is explicitly supplied here as a fixture, not guessed for a user project.
        profiles = [
            {
                "harness": "pi",
                "provider": "openai-codex",
                "model": "Astra",
                "effort": "max-supported",
            },
            {
                "harness": "pi",
                "provider": "openai-codex",
                "model": "Sol",
                "effort": "max-supported",
            },
            {"harness": "claude", "model": "Opus", "effort": "max"},
            {"harness": "claude", "model": "Fable", "effort": "max"},
        ]
        policy = {"classifications": {"audit": {"team": profiles, "approval": "user"}}}
        audit = resolve_selection(policy, {"classification": "audit", "rationale": "Requested"})
        self.assertEqual(
            [peer["model"] for peer in audit["team"]], ["astra", "sol", "opus", "fable"]
        )
        self.assertEqual(
            [peer["effort"] for peer in audit["team"]],
            ["max-supported", "max-supported", "max", "max"],
        )
        research_profiles = [profiles[index] | {"effort": "xhigh"} for index in (2, 0, 3)]
        research = resolve_selection(
            {"classifications": {"research": {"team": research_profiles}}},
            {"classification": "research", "rationale": "Requested"},
        )
        self.assertFalse(research["requires_approval"])
        self.assertEqual([peer["effort"] for peer in research["team"]], ["xhigh"] * 3)
