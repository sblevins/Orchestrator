"""Optional timing and unrolled-round metadata never change scheduling."""

import unittest
from copy import deepcopy

from orchestrator.graphs import GraphError, planning_schema, ready_nodes, validate_plan
from orchestrator.plan_presentation import presentation_metadata
from tests.test_graphs import node, plan


def estimate(minimum=10, maximum=20):
    return {
        "min_minutes": minimum,
        "max_minutes": maximum,
        "basis": "Source inspected; active work only.",
    }


def cycle(iteration=1, maximum=3):
    return {
        "id": "quality",
        "label": "Review / revise",
        "iteration": iteration,
        "max_iterations": maximum,
    }


class PlanMetadataTests(unittest.TestCase):
    def test_optional_fields_preserved_without_changing_scheduling(self):
        legacy = plan(node("review1"), node("fix1", ["review1"]), node("review2", ["fix1"]))
        graph = deepcopy(legacy)
        for index, task in enumerate(graph["nodes"]):
            task.update(estimate=estimate(), cycle=cycle(2 if index == 2 else 1))
        self.assertEqual(validate_plan(graph), graph)
        self.assertEqual(validate_plan(legacy), legacy)
        for states in ({}, {"review1": "completed"}, {"review1": "completed", "fix1": "completed"}):
            self.assertEqual(ready_nodes(graph, states), ready_nodes(legacy, states))
        schema = planning_schema()["properties"]["nodes"]["items"]
        self.assertNotIn("estimate", schema["required"])
        self.assertNotIn("cycle", schema["required"])
        self.assertIn("estimate", schema["properties"])
        self.assertIn("cycle", schema["properties"])
        copied = validate_plan(graph)
        copied["nodes"][0]["estimate"]["basis"] = "Changed"
        self.assertNotEqual(copied, graph)

    def test_invalid_metadata_has_actionable_errors(self):
        invalid_estimates = [
            None,
            {},
            {**estimate(), "other": 1},
            estimate(21, 20),
            {**estimate(), "basis": " "},
        ]
        for field in ("min_minutes", "max_minutes"):
            invalid_estimates.extend(
                {**estimate(), field: value} for value in (True, 0, -1, 1.5, "10", 525601)
            )
        invalid_cycles = [
            None,
            {},
            cycle(4, 3),
            {**cycle(), "label": " "},
            {**cycle(), "id": "../bad"},
            {**cycle(), "extra": 1},
        ]
        for field in ("iteration", "max_iterations"):
            invalid_cycles.extend(
                {**cycle(), field: value} for value in (True, 0, -1, 1.5, "2", 257)
            )
        for field, values in (("estimate", invalid_estimates), ("cycle", invalid_cycles)):
            for value in values:
                with (
                    self.subTest(field=field, value=value),
                    self.assertRaisesRegex(GraphError, field),
                ):
                    validate_plan(plan({**node("task"), field: value}))

    def test_cycle_group_consistency_and_real_dependency_cycles(self):
        for field, value in (("label", "Different"), ("max_iterations", 4)):
            with self.subTest(field=field), self.assertRaisesRegex(GraphError, "same label"):
                validate_plan(
                    plan(
                        {**node("a"), "cycle": cycle()},
                        {**node("b"), "cycle": {**cycle(), field: value}},
                    )
                )
        with self.assertRaisesRegex(GraphError, "dependency cycle"):
            validate_plan(
                plan(
                    {**node("a", ["b"]), "cycle": cycle()}, {**node("b", ["a"]), "cycle": cycle(2)}
                )
            )

    def test_wave_ranges_are_parallel_not_sums_and_unknowns_remain_unknown(self):
        nodes = [
            {**node("a"), "wave": 1, "estimate": estimate(5, 15)},
            {**node("b"), "wave": 1, "estimate": estimate(10, 12)},
            {**node("c"), "wave": 2, "estimate": estimate()},
            {**node("d"), "wave": 2},
            {**node("e"), "wave": 3},
        ]
        before = deepcopy(nodes)
        metadata = presentation_metadata(nodes)
        self.assertEqual(metadata["waves"][0]["estimate"], {"min_minutes": 10, "max_minutes": 15})
        self.assertEqual(metadata["waves"][0]["estimated_tasks"], 2)
        self.assertIsNone(metadata["waves"][1]["estimate"])
        self.assertEqual(metadata["waves"][1]["estimated_tasks"], 1)
        self.assertIsNone(metadata["waves"][2]["estimate"])
        self.assertEqual(nodes, before)

    def test_unrolled_group_collects_multiple_tasks_per_round_across_waves(self):
        nodes = [
            {**node("review1"), "wave": 1, "cycle": cycle()},
            {**node("fix1"), "wave": 2, "cycle": cycle()},
            {**node("review2"), "wave": 3, "cycle": cycle(2)},
            {**node("ordinary"), "wave": 4},
        ]
        group = presentation_metadata(nodes)["cycles"][0]
        self.assertEqual(
            group,
            {
                "id": "quality",
                "label": "Review / revise",
                "max_iterations": 3,
                "iterations": [1, 2],
                "node_ids": ["review1", "fix1", "review2"],
            },
        )
        self.assertEqual(
            presentation_metadata([{**node("review-round-3"), "wave": 1}])["cycles"], []
        )
        self.assertEqual(presentation_metadata([]), {"waves": [], "cycles": []})
