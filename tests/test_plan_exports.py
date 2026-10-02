"""Real saved plans and transports produce private, standardized offline artifacts."""

import contextlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from orchestrator.api import request, tool_definitions
from orchestrator.config import load_config
from orchestrator.plan_exports import export_plan, plan_snapshot
from orchestrator.store import StateError, Store, encode

ROOT = Path(__file__).resolve().parents[1]


def graph_node(name, dependencies=(), kind="work"):
    return {
        "id": name,
        "title": name.title(),
        "description": f"Perform {name}",
        "depends_on": list(dependencies),
        "acceptance_criteria": [f"Evidence for {name}"],
        "kind": kind,
        "mode": "read",
    }


class PlanExportTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.store = Store(self.home)
        for project in ("alpha", "beta"):
            root = self.home / project
            root.mkdir()
            self.store.add_project(project, str(root))
            self.store.open_session(project, "test", project)
        self.store.open_session("observer", "test", "alpha", observer=True)
        self.store.open_session("unbound", "test")
        self.plan = self.store.create_plan("alpha", "Plan the work", load_config(self.home))
        self.graph = {
            "summary": "Inspect, compare, then review",
            "assumptions": ["Existing project"],
            "risks": ["Unverified routing"],
            "questions": ["Which model?"],
            "nodes": [
                graph_node("review", ("left", "right"), "review"),
                graph_node("left", ("inspect",)),
                graph_node("right", ("inspect",)),
                graph_node("inspect"),
            ],
        }
        self.store.install_graph(self.plan["id"], self.plan["planner_task"], self.graph)

    def durable_state(self):
        with contextlib.closing(self.store.connect()) as database:
            return list(database.iterdump())

    def cli(self, *arguments, input=None):
        return subprocess.run(
            [sys.executable, str(ROOT / "bin/orchestrator"), "--home", str(self.home), *arguments],
            input=input,
            capture_output=True,
            check=False,
            text=True,
            timeout=30,
        )

    def add_worker(self, identifier="worker", node="left", profile=None, **fields):
        values = {
            "id": identifier,
            "project_id": "alpha",
            "session_id": "alpha",
            "origin_event_id": self.store.record("alpha", "user.message", {"prompt": "Work"}),
            "plan_id": self.plan["id"],
            "node_id": node,
            "plan_version": self.plan["version"],
            "brief": "Assigned work",
            "mode": "read",
            "state": "selected",
            "evidence_json": "{}",
            "profile_json": encode(profile) if profile else None,
            "created": 0,
            **fields,
        }
        with self.store.transaction() as database:
            database.execute(
                f"INSERT INTO worker_requests ({','.join(values)}) "
                f"VALUES ({','.join('?' for _ in values)})",
                tuple(values.values()),
            )

    def test_export_preserves_state_and_generates_complete_private_bundle(self):
        before = self.durable_state()
        result = request(self.home, "alpha", "export_plan", {"plan_id": self.plan["id"]})
        self.assertEqual(self.durable_state(), before)
        bundle = Path(result["html_path"]).parent
        self.assertEqual(
            {path.name for path in bundle.iterdir()}, {"index.html", "plan.mmd", "snapshot.json"}
        )
        self.assertTrue(bundle.is_relative_to(self.home / "data/projects/alpha/plan-exports"))
        self.assertEqual(result["html_uri"], Path(result["html_path"]).as_uri())
        self.assertEqual(result["node_count"], 4)
        self.assertEqual(result["wave_count"], 3)
        snapshot = json.loads(Path(result["snapshot_path"]).read_text())
        self.assertEqual(snapshot["plan_id"], self.plan["id"])
        self.assertEqual(
            {node["id"]: node["wave"] for node in snapshot["nodes"]},
            {"inspect": 1, "left": 2, "right": 2, "review": 3},
        )
        self.assertEqual(snapshot["readiness"]["ready"], ["inspect"])
        self.assertTrue(all(not node["workers"] for node in snapshot["nodes"]))
        self.assertEqual(bundle.stat().st_mode & 0o777, 0o700)
        for path in bundle.iterdir():
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertFalse((self.home / "alpha/.orchestrator").exists())

    def test_concurrent_exports_publish_separate_complete_bundles(self):
        before = self.durable_state()
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(
                executor.map(lambda _: export_plan(self.store, self.plan["id"]), range(4))
            )
        self.assertEqual(len({result["html_path"] for result in results}), 4)
        for result in results:
            snapshot = json.loads(Path(result["snapshot_path"]).read_text())
            self.assertEqual(snapshot["plan_id"], self.plan["id"])
            self.assertTrue(Path(result["html_path"]).read_text().endswith("</html>"))
        self.assertEqual(self.durable_state(), before)

    def test_refresh_is_immutable_and_paused_does_not_mean_authorized(self):
        first = export_plan(self.store, self.plan["id"])
        original = Path(first["snapshot_path"]).read_bytes()
        with self.store.transaction() as database:
            database.execute(
                "UPDATE graph_nodes SET state='completed' WHERE plan_id=? AND node_id='inspect'",
                (self.plan["id"],),
            )
        self.store.pause("alpha", "Pause")
        second = export_plan(self.store, self.plan["id"])
        self.assertNotEqual(first["html_path"], second["html_path"])
        self.assertEqual(Path(first["snapshot_path"]).read_bytes(), original)
        updated = json.loads(Path(second["snapshot_path"]).read_text())
        self.assertTrue(updated["paused"])
        self.assertEqual(updated["readiness"]["ready"], ["left", "right"])
        self.assertEqual(updated["plan_status"], self.store.plan(self.plan["id"])["status"])

    def test_observer_allowed_but_other_project_unbound_and_inactive_denied(self):
        result = request(self.home, "observer", "export_plan", {"plan_id": self.plan["id"]})
        self.assertTrue(Path(result["html_path"]).exists())
        for session in ("beta", "unbound"):
            with self.subTest(session=session), self.assertRaises(StateError):
                request(self.home, session, "export_plan", {"plan_id": self.plan["id"]})
        with self.store.transaction() as database:
            database.execute("UPDATE sessions SET active=0 WHERE id='alpha'")
        with self.assertRaises(StateError):
            request(self.home, "alpha", "export_plan", {"plan_id": self.plan["id"]})
        self.assertFalse((self.home / "data/projects/beta/plan-exports").exists())

    def test_input_cannot_supply_html_css_paths_or_foreign_content(self):
        for extra in (
            {"html": "<script>alert(1)</script>"},
            {"output": "/tmp/escape"},
            {"project_id": "beta"},
            {"css": "x"},
        ):
            with self.subTest(extra=extra), self.assertRaises(StateError):
                request(self.home, "alpha", "export_plan", {"plan_id": self.plan["id"], **extra})
        for plan_id in ("../beta", "not-found", ""):
            with self.subTest(plan_id=plan_id), self.assertRaises(StateError):
                export_plan(self.store, plan_id)
        pending = self.store.create_plan("alpha", "Not drafted", load_config(self.home))
        with self.assertRaisesRegex(StateError, "no validated graph"):
            export_plan(self.store, pending["id"])

    def test_symlinked_artifact_directory_cannot_write_another_project(self):
        parent = self.home / "data/projects/alpha"
        parent.mkdir(parents=True, exist_ok=True)
        destination = self.home / "beta"
        (parent / "plan-exports").symlink_to(destination, target_is_directory=True)
        with self.assertRaisesRegex(StateError, "Cannot publish private plan export"):
            export_plan(self.store, self.plan["id"])
        self.assertEqual(list(destination.iterdir()), [])

    def test_failed_publication_leaves_no_partial_bundle(self):
        actual_fsync = os.fsync
        calls = 0

        def failing_fsync(descriptor):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError(28, "No space left")
            return actual_fsync(descriptor)

        with (
            patch("orchestrator.plan_exports.os.fsync", side_effect=failing_fsync),
            self.assertRaises(StateError),
        ):
            export_plan(self.store, self.plan["id"])
        directory = self.home / "data/projects/alpha/plan-exports" / self.plan["id"]
        self.assertEqual(list(directory.iterdir()), [])

    def test_saved_team_profiles_rounds_reports_and_unknown_are_truthful(self):
        profiles = [
            {"harness": "claude", "model": "opus", "effort": "max"},
            {"harness": "pi", "provider": "openai", "model": "astra", "effort": "max-supported"},
        ]
        self.add_worker(profile={"team": profiles}, state="group_running")
        report = {
            "summary": "Untrusted review",
            "changes": [],
            "checks": ["Read source"],
            "remaining_issues": ["Open finding"],
            "secret": "NEVER_EXPORT_THIS",
        }
        self.add_worker(
            "child",
            None,
            profiles[1],
            plan_id=None,
            plan_version=None,
            state="candidate",
            result_json=encode(report),
            error="SECRET_RAW_ERROR",
        )
        with self.store.transaction() as database:
            database.execute(
                "INSERT INTO worker_groups(parent_request_id,state,generation,created,baseline_commit) "
                "VALUES('worker','round2',1,0,?)",
                ("a" * 40,),
            )
            database.execute(
                "INSERT INTO worker_group_members(child_request_id,parent_request_id,peer_index,round) VALUES('child','worker',1,2)"
            )
            database.execute(
                "UPDATE graph_nodes SET state='unknown' WHERE plan_id=? AND node_id='left'",
                (self.plan["id"],),
            )
        snapshot = plan_snapshot(self.store, self.plan["id"])
        node = next(node for node in snapshot["nodes"] if node["id"] == "left")
        self.assertEqual(node["state"], "unknown")
        self.assertEqual(node["workers"][0]["profiles"], profiles)
        child = node["workers"][1]
        self.assertEqual((child["team_round"], child["team_peer"]), (2, 1))
        self.assertEqual(child["report"]["remaining_issues"], ["Open finding"])
        self.assertTrue(child["report_is_untrusted_data"])
        self.assertNotIn("NEVER_EXPORT_THIS", encode(snapshot))
        self.assertNotIn("SECRET_RAW_ERROR", encode(snapshot))
        self.assertNotIn("prompt", child)
        self.assertNotIn("config", child)

    def test_invalid_settings_do_not_hide_saved_graph(self):
        settings = self.home / "config/projects"
        settings.mkdir(parents=True)
        (settings / "alpha.json").write_text('{"execution":{"max_parallel":0}}')
        snapshot = plan_snapshot(self.store, self.plan["id"])
        self.assertEqual(snapshot["summary"], self.graph["summary"])
        self.assertIsNone(snapshot["max_parallel"])
        self.assertTrue(snapshot["readiness_notice"])
        self.assertTrue(all(not nodes for nodes in snapshot["readiness"].values()))
        self.assertEqual(len(snapshot["nodes"]), 4)

    def test_current_policy_cannot_invent_or_change_displayed_assignment(self):
        self.add_worker(profile={"harness": "claude", "model": "opus", "effort": "high"})
        policy = self.home / "alpha/.orchestrator"
        policy.mkdir()
        (policy / "crew-dispatch.json").write_text(
            '{"default":{"harness":"claude","model":"new-model"}}'
        )
        snapshot = plan_snapshot(self.store, self.plan["id"])
        assigned = next(node for node in snapshot["nodes"] if node["id"] == "left")
        self.assertEqual(assigned["workers"][0]["profiles"][0]["model"], "opus")
        self.assertEqual(
            next(node for node in snapshot["nodes"] if node["id"] == "right")["workers"], []
        )

    def test_real_cli_exports_and_default_graph_remains_json(self):
        before = self.durable_state()
        response = self.cli("plan-view", self.plan["id"])
        self.assertEqual(response.returncode, 0, response.stderr + response.stdout)
        result = json.loads(response.stdout)
        self.assertTrue(Path(result["html_path"]).is_file())
        mermaid = self.cli("graph", self.plan["id"], "--format", "mermaid")
        self.assertEqual(mermaid.returncode, 0, mermaid.stdout + mermaid.stderr)
        self.assertEqual(mermaid.stdout, Path(result["mermaid_path"]).read_text())
        graph = self.cli("graph", self.plan["id"])
        self.assertEqual(json.loads(graph.stdout)["graph"], self.graph)
        self.assertEqual(before, self.durable_state())

    def test_cli_opens_browser_only_when_requested(self):
        browser = self.home / "fake_browser.py"
        browser.write_text(
            "import os, sys\nfrom pathlib import Path\n"
            "Path(os.environ['PLAN_BROWSER_LOG']).write_text(sys.argv[1])\n"
        )
        log = self.home / "browser.log"
        with patch.dict(
            os.environ,
            {
                "BROWSER": f"{sys.executable} {browser} %s",
                "PLAN_BROWSER_LOG": str(log),
            },
        ):
            closed = self.cli("plan-view", self.plan["id"])
            self.assertEqual(closed.returncode, 0, closed.stdout + closed.stderr)
            self.assertFalse(log.exists())
            opened = self.cli("plan-view", self.plan["id"], "--open")
        self.assertEqual(opened.returncode, 0, opened.stdout + opened.stderr)
        result = json.loads(opened.stdout)
        self.assertTrue(result["browser_opened"])
        self.assertEqual(log.read_text(), result["html_uri"])

    def test_real_mcp_uses_plan_id_only_and_cannot_read_other_project(self):
        messages = [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-11-25"},
            },
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "export_plan", "arguments": {"plan_id": self.plan["id"]}},
            },
        ]
        serialized = "\n".join(json.dumps(message) for message in messages) + "\n"
        response = self.cli("mcp", "--session", "alpha", input=serialized)
        self.assertEqual(response.returncode, 0, response.stderr)
        exported = json.loads(response.stdout.splitlines()[-1])["result"]
        self.assertFalse(exported["isError"], exported)
        result = json.loads(exported["content"][0]["text"])
        self.assertTrue(Path(result["html_path"]).is_file())
        response = self.cli("mcp", "--session", "beta", input=serialized)
        denied = json.loads(response.stdout.splitlines()[-1])["result"]
        self.assertTrue(denied["isError"])
        self.assertNotIn(self.graph["summary"], response.stdout)
        tool = next(tool for tool in tool_definitions(False) if tool["name"] == "export_plan")
        self.assertEqual(tool["inputSchema"]["required"], ["plan_id"])
        self.assertEqual(set(tool["inputSchema"]["properties"]), {"plan_id"})
        self.assertFalse(tool["annotations"]["readOnlyHint"])
        self.assertFalse(tool["annotations"]["destructiveHint"])
