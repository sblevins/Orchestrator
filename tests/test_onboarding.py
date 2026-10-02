"""Conversational policy setup stays inside the bound project's initial configuration."""

import hashlib
import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from orchestrator.api import request, tool_definitions
from orchestrator.onboarding import project_setup, setup_project
from orchestrator.store import StateError, Store

POLICY = {"default": {"harness": "claude", "model": "Opus", "effort": "high"}}


class OnboardingTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.home = Path(self.directory.name)
        self.store = Store(self.home)
        for project, frontend in (("alpha", "claude"), ("beta", "pi")):
            root = self.home / project
            root.mkdir()
            self.store.add_project(project, str(root))
            self.store.open_session(project, frontend, project)
        self.store.open_session("observer", "claude", "alpha", observer=True)
        self.store.open_session("unbound", "claude")
        self.path = self.home / "alpha/.orchestrator/crew-dispatch.json"
        self.service = patch("orchestrator.api._start_service", return_value={"running": False})
        self.service.start()
        self.addCleanup(self.service.stop)

    def test_initialize_then_save_chosen_policy_without_execution(self):
        initial = project_setup(self.store, "alpha")
        self.assertEqual(initial["phase"], "needs_configuration")
        self.assertEqual(initial["policy_revision"], "missing")
        self.assertFalse(self.path.exists())
        initialized = setup_project(self.store, "alpha")
        self.assertTrue(initialized["changed"])
        self.assertTrue(initialized["can_configure"])
        self.assertEqual(json.loads(self.path.read_text()), {"rules": []})
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(
            initialized["policy_revision"], hashlib.sha256(self.path.read_bytes()).hexdigest()
        )
        self.assertFalse(setup_project(self.store, "alpha")["changed"])
        configured = setup_project(self.store, "alpha", POLICY, initialized["policy_revision"])
        self.assertEqual(configured["phase"], "configured")
        self.assertTrue(configured["validation"]["routable"])
        self.assertFalse(configured["initial_setup_open"])
        self.assertFalse(configured["execution_authorized"])
        self.assertEqual(json.loads(self.path.read_text()), POLICY)
        self.assertEqual(self.store.tasks(), [])
        self.assertFalse((self.home / "beta/.orchestrator").exists())

    def test_direct_save_works_in_pi_and_never_changes_source(self):
        source = self.home / "beta/source.py"
        source.write_text("unchanged")
        result = request(
            self.home, "beta", "setup_project", {"policy": POLICY, "expected_revision": "missing"}
        )
        self.assertEqual(result["phase"], "configured")
        self.assertEqual(source.read_text(), "unchanged")
        self.assertFalse((self.home / "beta/.gitignore").exists())
        self.assertEqual(
            result["policy_path"], str(self.home / "beta/.orchestrator/crew-dispatch.json")
        )

    def test_complete_cannot_be_reopened_by_deleting_policy(self):
        setup_project(self.store, "alpha", POLICY, "missing")
        self.path.unlink()
        result = project_setup(self.store, "alpha")
        self.assertEqual(result["phase"], "repair_required")
        self.assertFalse(result["can_configure"])
        with self.assertRaisesRegex(StateError, "setup is complete"):
            setup_project(self.store, "alpha", POLICY, "missing")
        self.assertFalse(setup_project(self.store, "alpha")["changed"])
        self.assertFalse(self.path.exists())

    def test_existing_configured_policy_is_preserved_and_sealed(self):
        self.path.parent.mkdir()
        self.path.write_text(json.dumps(POLICY))
        raw = self.path.read_bytes()
        self.assertEqual(project_setup(self.store, "alpha")["phase"], "configured")
        with self.assertRaises(StateError):
            setup_project(
                self.store,
                "alpha",
                {"default": {**POLICY["default"], "model": "Fable"}},
                hashlib.sha256(raw).hexdigest(),
            )
        self.assertEqual(self.path.read_bytes(), raw)
        self.path.unlink()
        self.assertFalse(project_setup(self.store, "alpha")["initial_setup_open"])

    def test_rejected_save_commits_newly_observed_permanent_seal(self):
        # A configured policy appeared after binding, without a prior setup read.
        self.path.parent.mkdir()
        self.path.write_text(json.dumps(POLICY))
        with self.assertRaisesRegex(StateError, "setup is complete"):
            setup_project(self.store, "alpha", POLICY, "missing")
        self.path.unlink()
        self.assertFalse(project_setup(self.store, "alpha")["initial_setup_open"])
        with self.assertRaisesRegex(StateError, "setup is complete"):
            setup_project(self.store, "alpha", POLICY, "missing")
        self.assertFalse(self.path.exists())

    def test_external_edits_before_install_recheck_are_rejected(self):
        initial = setup_project(self.store, "alpha")
        from orchestrator import onboarding

        original_write = onboarding._write_policy

        def edit_before_recheck(*args, **kwargs):
            self.path.write_text('{"rules": [], "manual-note": "keep"}')
            return original_write(*args, **kwargs)

        with (
            patch("orchestrator.onboarding._write_policy", side_effect=edit_before_recheck),
            self.assertRaisesRegex(StateError, "changed"),
        ):
            setup_project(self.store, "alpha", POLICY, initial["policy_revision"])
        self.assertIn("keep", self.path.read_text())
        self.assertEqual(len(list(self.path.parent.iterdir())), 1)

    def test_manual_editors_are_outside_the_shared_writer_protocol(self):
        # Explicitly document the unsupported boundary: an arbitrary editor can
        # change the file after the final check. Users must not mix manual editing
        # with conversational setup. Cooperative setup writers are tested separately.
        initial = setup_project(self.store, "alpha")
        original_replace = os.replace

        def external_edit_then_replace(*args, **kwargs):
            self.path.write_text('{"rules": [], "manual-note": "concurrent external edit"}')
            return original_replace(*args, **kwargs)

        with patch("orchestrator.onboarding.os.replace", side_effect=external_edit_then_replace):
            result = setup_project(self.store, "alpha", POLICY, initial["policy_revision"])
        self.assertEqual(result["phase"], "configured")
        self.assertEqual(json.loads(self.path.read_text()), POLICY)

    def test_inherited_policy_does_not_get_shadowed_by_setup(self):
        (self.home / "config").mkdir()
        inherited = self.home / "config/crew-dispatch.json"
        inherited.write_text(json.dumps(POLICY))
        result = project_setup(self.store, "alpha")
        self.assertEqual(result["policy_source"], str(inherited))
        self.assertEqual(result["phase"], "configured")
        self.assertFalse(setup_project(self.store, "alpha")["changed"])
        self.assertFalse(self.path.exists())

    def test_unbound_observer_and_displaced_sessions_cannot_write(self):
        self.assertFalse(project_setup(self.store, "observer")["can_configure"])
        for session in ("observer", "unbound"):
            with self.subTest(session=session), self.assertRaises(StateError):
                request(self.home, session, "setup_project")
        self.store.open_session("replacement", "claude", "alpha", takeover=True)
        with self.assertRaises(StateError):
            setup_project(self.store, "alpha")
        self.assertTrue(setup_project(self.store, "replacement")["changed"])
        self.store.close_session("replacement")
        with self.assertRaises(StateError):
            project_setup(self.store, "replacement")

    def test_invalid_or_unexecutable_policy_does_not_close_or_write(self):
        for policy in (
            {},
            {"default": {"harness": "codex", "model": "x", "effort": "high"}},
            {"default": {"harness": "claude", "model": "Opus"}},
            {"default": [{"harness": "claude", "model": "Opus", "effort": "high"}]},
        ):
            with self.subTest(policy=policy), self.assertRaises(ValueError):
                setup_project(self.store, "alpha", policy, "missing")
            self.assertFalse(self.path.exists())
            self.assertTrue(project_setup(self.store, "alpha")["can_configure"])

    def test_missing_revision_and_stale_revision_do_not_overwrite(self):
        with self.assertRaisesRegex(StateError, "expected_revision"):
            setup_project(self.store, "alpha", POLICY)
        initial = setup_project(self.store, "alpha")
        self.path.write_text('{"rules": [], "user-note": "preserve"}')
        with self.assertRaisesRegex(StateError, "changed"):
            setup_project(self.store, "alpha", POLICY, initial["policy_revision"])
        self.assertIn("preserve", self.path.read_text())
        current = project_setup(self.store, "alpha")
        self.assertTrue(
            setup_project(self.store, "alpha", POLICY, current["policy_revision"])["changed"]
        )

    def test_initial_malformed_file_can_be_repaired_with_current_revision(self):
        self.path.parent.mkdir()
        self.path.write_text('{"broken":')
        state = project_setup(self.store, "alpha")
        self.assertIsNotNone(state["policy_error"])
        self.assertTrue(state["initial_setup_open"])
        self.assertEqual(
            setup_project(self.store, "alpha", POLICY, state["policy_revision"])["phase"],
            "configured",
        )

    def test_duplicate_keys_require_repair_and_are_not_silently_accepted(self):
        self.path.parent.mkdir()
        self.path.write_text('{"rules": [], "rules": []}')
        state = project_setup(self.store, "alpha")
        self.assertIn("duplicate", state["policy_error"].lower())
        self.assertTrue(state["can_configure"])

    def test_symlink_directory_policy_and_fifo_fail_without_touching_target(self):
        elsewhere = self.home / "elsewhere"
        elsewhere.mkdir()
        self.path.parent.symlink_to(elsewhere, target_is_directory=True)
        with self.assertRaises((OSError, ValueError)):
            setup_project(self.store, "alpha", POLICY, "missing")
        self.assertEqual(list(elsewhere.iterdir()), [])
        self.path.parent.unlink()
        self.path.parent.mkdir()
        target = elsewhere / "file"
        target.write_text("untouched")
        self.path.symlink_to(target)
        with self.assertRaises((OSError, ValueError)):
            setup_project(self.store, "alpha", POLICY, "missing")
        self.assertEqual(target.read_text(), "untouched")
        self.path.unlink()
        os.mkfifo(self.path)
        with self.assertRaises((OSError, ValueError)):
            setup_project(self.store, "alpha", POLICY, "missing")

    def test_concurrent_initialization_and_configuration_do_not_duplicate_or_clobber(self):
        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(lambda _: setup_project(self.store, "alpha"), range(4)))
        self.assertEqual(sum(result["changed"] for result in results), 1)
        revision = results[0]["policy_revision"]

        def save(index):
            try:
                setup_project(
                    self.store,
                    "alpha",
                    {"default": {**POLICY["default"], "model": "Opus" if index else "Fable"}},
                    revision,
                )
                return True
            except StateError:
                return False

        with ThreadPoolExecutor(max_workers=4) as executor:
            self.assertEqual(sum(executor.map(save, range(4))), 1)
        self.assertEqual(len(list(self.path.parent.iterdir())), 1)

    def test_dispatched_work_prevents_reentering_initial_setup(self):
        from orchestrator.workers import WorkerService

        origin = request(self.home, "alpha", "record_prompt", {"prompt": "Inspect code"})[
            "event_id"
        ]
        worker = WorkerService(self.store).request(
            "alpha", "Inspect", "read", origin_event_id=origin
        )
        task = self.store.enqueue("alpha", "alpha", "planner", "fixture", {})
        with self.store.transaction() as database:
            database.execute(
                "UPDATE worker_requests SET task_id=? WHERE id=?", (task["id"], worker["id"])
            )
        self.assertFalse(project_setup(self.store, "alpha")["initial_setup_open"])
        with self.assertRaises(StateError):
            setup_project(self.store, "alpha", POLICY, "missing")

    def test_api_fields_and_status_expose_setup_without_execution_authority(self):
        tools = {tool["name"]: tool for tool in tool_definitions(False)}
        self.assertIn("setup_project", tools)
        self.assertTrue(tools["project_setup"]["annotations"]["readOnlyHint"])
        self.assertFalse(tools["setup_project"]["annotations"]["readOnlyHint"])
        self.assertIn("setup", request(self.home, "alpha", "status"))
        for payload in (
            {"policy": []},
            {"policy": None},
            {"project_id": "beta"},
            {"path": "/tmp/outside"},
            {"expected_revision": 1},
            {"expected_revision": "missing"},
        ):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                request(self.home, "alpha", "setup_project", payload)
        self.assertFalse(self.path.exists())


if __name__ == "__main__":
    unittest.main()
