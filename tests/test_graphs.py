"""Graph validation and scheduling tests require only the standard library."""

from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from orchestrator.graphs import (
    GraphError, MAX_ITEMS, MAX_NODES, MAX_TEXT, MAX_WORKFLOW_BYTES, STATES,
    load_workflow, planning_schema, ready_nodes, validate_plan,
)


def node(identifier, dependencies=(), kind="work"):
    return {"id": identifier, "title": identifier, "description": "Do this task.",
            "depends_on": list(dependencies), "acceptance_criteria": ["Verified."], "kind": kind}


def plan(*nodes):
    return {"summary": "A plan", "assumptions": [], "risks": [], "questions": [],
            "nodes": list(nodes) or [node("root")]}


def diamond():
    return plan(node("root"), node("left", ["root"]), node("right", ["root"]),
                node("join", ["left", "right"]))


class ValidationTests(unittest.TestCase):
    def test_valid_copy_and_no_input_mutation(self):
        source = diamond()
        original = deepcopy(source)
        validated = validate_plan(source)
        self.assertEqual(source, original)
        self.assertEqual(validated, source)
        validated["nodes"][1]["depends_on"].clear()
        validated["nodes"][0]["acceptance_criteria"].append("Changed")
        self.assertEqual(source, original)

    def test_duplicates_missing_dependencies_and_cycles(self):
        invalid = [plan(node("a"), node("a")), plan(node("a", ["missing"])),
                   plan(node("a", ["a"])), plan(node("a", ["b"]), node("b", ["a"])),
                   plan(node("a"), node("b", ["a", "a"])),
                   plan(node("a"), node("b", ["c"]), node("c", ["b"]))]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(GraphError):
                validate_plan(value)

    def test_exact_object_shapes(self):
        for value in (None, [], True, "plan", {}, {**plan(), "extra": True}):
            with self.subTest(value=value), self.assertRaises(GraphError):
                validate_plan(value)
        for target in ("plan", "node"):
            source = plan()
            mapping = source if target == "plan" else source["nodes"][0]
            for key in list(mapping):
                invalid = deepcopy(source)
                del (invalid if target == "plan" else invalid["nodes"][0])[key]
                with self.subTest(target=target, missing=key), self.assertRaises(GraphError):
                    validate_plan(invalid)
        source = plan()
        source["nodes"][0]["extra"] = "no"
        with self.assertRaises(GraphError):
            validate_plan(source)

    def test_invalid_fields(self):
        cases = {
            "id": [None, True, [], "", "../bad", "a/b", "a\\b", "é", "a\n", "a" * 129],
            "title": [None, True, [], "", "  ", "a\x00b", "a" * 257],
            "description": [None, 1, "", "\t", "a" * (MAX_TEXT + 1)],
            "kind": [None, True, [], "critic", "worker"],
            "depends_on": [None, (), "root", [1], [[]], ["bad/path"], ["x"] * MAX_NODES],
            "acceptance_criteria": [None, (), "test", [], [True], [""], ["\x00"],
                                    ["a"] * (MAX_ITEMS + 1)],
        }
        for field, values in cases.items():
            for value in values:
                invalid = plan()
                invalid["nodes"][0][field] = value
                with self.subTest(field=field, value=value), self.assertRaises(GraphError):
                    validate_plan(invalid)
        for field in ("summary", "assumptions", "risks", "questions", "nodes"):
            values = ([None, [], True, "", "\n", "\x00", "a" * (MAX_TEXT + 1)]
                      if field == "summary" else [None, (), True, "a"])
            if field == "nodes":
                values += [[], [None], [node(str(index)) for index in range(MAX_NODES + 1)]]
            elif field != "summary":
                values += [[True], [""], ["a"] * (MAX_ITEMS + 1)]
            for value in values:
                invalid = plan()
                invalid[field] = value
                with self.subTest(field=field), self.assertRaises(GraphError):
                    validate_plan(invalid)

    def test_boundaries_and_large_chain_without_recursion(self):
        source = plan(*(node(str(index), [str(index - 1)] if index else [])
                        for index in range(MAX_NODES)))
        source["summary"] = "a" * MAX_TEXT
        source["nodes"][0]["title"] = "a" * 256
        source["assumptions"] = ["a" * MAX_TEXT] * MAX_ITEMS
        validate_plan(source)
        self.assertEqual(ready_nodes(source, {})["ready"], ["0"])
        source = plan(node("a" * 128))
        validate_plan(source)

    def test_schema_is_independent_and_has_exact_required_fields(self):
        schema = planning_schema()
        self.assertEqual(set(schema["required"]), set(plan()))
        self.assertFalse(schema["additionalProperties"])
        item = schema["properties"]["nodes"]["items"]
        self.assertEqual(set(item["required"]), set(node("a")))
        self.assertEqual(item["properties"]["kind"]["enum"], ["work", "review", "approval"])
        schema["properties"]["nodes"]["maxItems"] = 0
        self.assertEqual(planning_schema()["properties"]["nodes"]["maxItems"], MAX_NODES)


class ReadinessTests(unittest.TestCase):
    def test_diamond_fan_out_and_join(self):
        source = diamond()
        self.assertEqual(ready_nodes(source, {}), {
            "ready": ["root"], "waiting": ["left", "right", "join"],
            "blocked": [], "cancelled": [],
        })
        states = {"root": "completed"}
        self.assertEqual(ready_nodes(source, states)["ready"], ["left", "right"])
        states["left"] = "succeeded"
        self.assertEqual(ready_nodes(source, states)["ready"], ["right"])
        self.assertIn("join", ready_nodes(source, states)["waiting"])
        states["right"] = "completed"
        self.assertEqual(ready_nodes(source, states)["ready"], ["join"])
        states["join"] = "succeeded"
        self.assertTrue(all(not value for value in ready_nodes(source, states).values()))

    def test_out_of_order_nodes_and_deterministic_ties(self):
        source = plan(node("join", ["z", "a"]), node("z", ["root"]),
                      node("a", ["root"]), node("root"))
        for _ in range(5):
            self.assertEqual(ready_nodes(source, {"root": "completed"})["ready"], ["z", "a"])
        self.assertEqual(ready_nodes(source, {"root": "failed"})["blocked"], ["z", "a", "join"])
        self.assertEqual([item["id"] for item in validate_plan(source)["nodes"]],
                         ["join", "z", "a", "root"])

    def test_capacity_counts_running_and_never_releases_selected_dependencies(self):
        source = plan(node("a"), node("b"), node("c"), node("d", ["a"]))
        for limit, expected in ((1, []), (2, ["b"]), (3, ["b", "c"]), (64, ["b", "c"])):
            with self.subTest(limit=limit):
                result = ready_nodes(source, {"a": "running"}, limit)
                self.assertEqual(result["ready"], expected)
                self.assertIn("d", result["waiting"])
        result = ready_nodes(source, {"a": "running", "b": "running"}, 1)
        self.assertEqual(result["ready"], [])
        self.assertEqual(result["waiting"], ["c", "d"])
        self.assertEqual(ready_nodes(source, {}, 64)["ready"], ["a", "b", "c"])

    def test_failure_is_transitive_for_both_policies(self):
        for policy, output in (("block", "blocked"), ("cancel", "cancelled")):
            for failure in ("failed", "cancelled", "blocked"):
                with self.subTest(policy=policy, failure=failure):
                    result = ready_nodes(diamond(), {"root": failure}, dependency_failure=policy)
                    self.assertTrue(set(["left", "right", "join"]).issubset(result[output]))
                    self.assertEqual(result["ready"], [])
                    self.assertEqual(result["waiting"], [])
        source = plan(*diamond()["nodes"], node("independent"))
        self.assertEqual(ready_nodes(source, {"root": "failed"})["ready"], ["independent"])

    def test_failure_overrides_inconsistent_descendant_success(self):
        states = {"root": "failed", "left": "completed", "right": "running", "join": "approved"}
        self.assertEqual(ready_nodes(diamond(), states)["blocked"], ["left", "right", "join"])

    def test_approval_never_auto_succeeds_or_launches(self):
        source = plan(node("approve", kind="approval"), node("after", ["approve"]))
        for state in STATES - {"approved", "failed", "blocked", "cancelled"}:
            with self.subTest(state=state):
                result = ready_nodes(source, {"approve": state})
                self.assertEqual(result["ready"], [])
                self.assertEqual(result["waiting"], ["approve", "after"])
        self.assertEqual(ready_nodes(source, {})["ready"], [])
        self.assertEqual(ready_nodes(source, {"approve": "approved"})["ready"], ["after"])

    def test_approval_and_completed_states_cannot_skip_dependencies(self):
        source = plan(node("before"), node("approve", ["before"], "approval"),
                      node("after", ["approve"]))
        result = ready_nodes(source, {"approve": "approved"})
        self.assertEqual(result["ready"], ["before"])
        self.assertEqual(result["waiting"], ["approve", "after"])
        result = ready_nodes(diamond(), {"left": "completed", "right": "succeeded"})
        self.assertEqual(result["ready"], ["root"])
        self.assertEqual(result["waiting"], ["left", "right", "join"])

    def test_unknown_and_work_approved_are_not_success(self):
        for state in ("unknown", "approved"):
            result = ready_nodes(diamond(), {"root": state})
            self.assertEqual(result["ready"], [])
            self.assertEqual(result["waiting"], ["root", "left", "right", "join"])

    def test_does_not_mutate_plan_or_states(self):
        source = diamond()
        states = {"root": "failed"}
        original = deepcopy((source, states))
        ready_nodes(source, states)
        self.assertEqual((source, states), original)

    def test_invalid_state_keys_values_and_scheduler_options(self):
        for states in (None, [], {"missing": "completed"}, {1: "completed"},
                       {"root": []}, {"root": True}, {"root": "typo"}, {"root": None}):
            with self.subTest(states=states), self.assertRaises(GraphError):
                ready_nodes(diamond(), states)
        for maximum in (True, 0, -1, 65, 1.5, "3", None):
            with self.subTest(maximum=maximum), self.assertRaises(GraphError):
                ready_nodes(diamond(), {}, maximum)
        for policy in (None, [], True, "ignore", "continue"):
            with self.subTest(policy=policy), self.assertRaises(GraphError):
                ready_nodes(diamond(), {}, dependency_failure=policy)


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.home = Path(self.directory.name)

    def write(self, name, value):
        path = self.home / "config" / "workflows" / (name + ".json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def test_tracked_templates_are_schedulable(self):
        for name in ("plan-review", "research-review"):
            source = load_workflow(self.home, name)
            self.assertEqual(ready_nodes(source, {})["ready"], ["research"])
            states = {}
            while True:
                result = ready_nodes(source, states)
                if not result["ready"]:
                    break
                states.update(dict.fromkeys(result["ready"], "completed"))
            self.assertEqual(result["waiting"], ["approval"])
            states["approval"] = "approved"
            self.assertTrue(all(not values for values in ready_nodes(source, states).values()))
        source = load_workflow(self.home, "research-review")
        self.assertEqual(ready_nodes(source, {"research": "completed"})["ready"],
                         ["evidence-review", "risk-review"])

    def test_custom_template_and_full_override(self):
        custom = diamond()
        self.write("custom", custom)
        self.assertEqual(load_workflow(self.home, "custom"), custom)
        self.write("plan-review", custom)
        self.assertEqual(load_workflow(self.home, "plan-review"), custom)
        self.assertEqual(ready_nodes(load_workflow(self.home, "plan-review"),
                                     {"root": "completed"})["ready"], ["left", "right"])
        with tempfile.TemporaryDirectory() as other:
            self.assertNotEqual(load_workflow(Path(other), "plan-review"), custom)

    def test_invalid_override_does_not_fall_back(self):
        self.write("plan-review", {"summary": "invalid"})
        with self.assertRaises(GraphError):
            load_workflow(self.home, "plan-review")

    def test_path_traversal_and_invalid_names(self):
        for name in ("../escape", "/tmp/escape", "a/b", "a\\b", ".", "..", "a.json", "",
                     "a\n", "a\x00", "é", "a" * 129, None, [], True):
            with self.subTest(name=name), self.assertRaises(GraphError):
                load_workflow(self.home, name)

    def test_malicious_file_and_directory_symlinks(self):
        with tempfile.TemporaryDirectory() as outside:
            external = Path(outside)
            (external / "plan-review.json").write_text(json.dumps(plan()), encoding="utf-8")
            target = self.write("plan-review", plan())
            for destination in (external / "plan-review.json", external / "missing", target):
                target.unlink()
                target.symlink_to(destination)
                with self.subTest(destination=destination), self.assertRaises(GraphError):
                    load_workflow(self.home, "plan-review")
            target.unlink()
            target.parent.rmdir()
            target.parent.symlink_to(external, target_is_directory=True)
            with self.assertRaises(GraphError):
                load_workflow(self.home, "plan-review")
            target.parent.unlink()
            target.parent.parent.rmdir()
            target.parent.parent.symlink_to(external, target_is_directory=True)
            with self.assertRaises(GraphError):
                load_workflow(self.home, "plan-review")

    def test_tracked_symlink_rejected(self):
        directory = self.home / "tracked"
        directory.mkdir()
        (directory / "custom.json").symlink_to(self.write("other", plan()))
        with patch("orchestrator.graphs.WORKFLOW_DIRECTORY", directory):
            with self.assertRaises(GraphError):
                load_workflow(self.home, "custom")

    def test_bad_json_encoding_duplicate_keys_and_oversized_files(self):
        target = self.write("plan-review", plan())
        for content in (b"{", b"\xff", b'{"summary":"a","summary":"b"}',
                        b"[" * 2000, b"x" * (MAX_WORKFLOW_BYTES + 1)):
            target.write_bytes(content)
            with self.subTest(content=content[:20]), self.assertRaises(GraphError):
                load_workflow(self.home, "plan-review")

    def test_missing_directory_and_nonregular_files(self):
        with self.assertRaisesRegex(GraphError, "missing workflow"):
            load_workflow(self.home, "missing")
        target = self.write("plan-review", plan())
        target.unlink()
        target.mkdir()
        with self.assertRaises(GraphError):
            load_workflow(self.home, "plan-review")
        target.rmdir()
        os.mkfifo(target)
        with self.assertRaises(GraphError):
            load_workflow(self.home, "plan-review")


if __name__ == "__main__":
    unittest.main()
