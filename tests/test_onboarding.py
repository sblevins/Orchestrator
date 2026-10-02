"""Policy preferences stay editable throughout an active project's life."""

import hashlib
import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from orchestrator.api import request
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
        service = patch("orchestrator.api._start_service", return_value={"running": False})
        service.start()
        self.addCleanup(service.stop)

    def test_public_backend_accepts_second_edit_without_revision(self):
        request(self.home, "alpha", "setup_project", {"policy": POLICY})
        changed = {"default": {**POLICY["default"], "effort": "low"}}
        result = request(self.home, "alpha", "setup_project", {"policy": changed})
        self.assertTrue(result["changed"])
        self.assertTrue(result["can_configure"])
        self.assertFalse(result["initial_setup_open"])
        self.assertFalse(result["execution_authorized"])
        self.assertEqual(result["policy"], changed)
        self.assertEqual(self.store.tasks(), [])
        self.assertFalse((self.home / "beta/.orchestrator").exists())

    def test_bootstrap_is_private_and_idempotent(self):
        result = setup_project(self.store, "alpha")
        self.assertTrue(result["changed"])
        self.assertEqual(result["policy"], {"rules": []})
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(
            result["policy_revision"], hashlib.sha256(self.path.read_bytes()).hexdigest()
        )
        self.assertFalse(setup_project(self.store, "alpha")["changed"])

    def test_bootstrap_materializes_inherited_policy(self):
        (self.home / "config").mkdir()
        inherited = self.home / "config/crew-dispatch.json"
        inherited.write_text(json.dumps(POLICY))
        self.assertEqual(project_setup(self.store, "alpha")["policy_source"], str(inherited))
        self.assertTrue(setup_project(self.store, "alpha")["changed"])
        self.assertEqual(json.loads(self.path.read_text()), POLICY)
        self.assertEqual(json.loads(inherited.read_text()), POLICY)
        self.assertFalse(setup_project(self.store, "alpha")["changed"])

    def test_deleted_and_malformed_policies_can_be_repaired(self):
        setup_project(self.store, "alpha", POLICY)
        self.path.unlink()
        self.assertTrue(project_setup(self.store, "alpha")["can_configure"])
        setup_project(self.store, "alpha", POLICY, "missing")
        self.path.write_text('{"broken":')
        state = project_setup(self.store, "alpha")
        self.assertIsNotNone(state["policy_error"])
        self.assertTrue(
            setup_project(self.store, "alpha", POLICY, state["policy_revision"])["changed"]
        )

    def test_incomplete_policy_is_saved_with_blockers(self):
        draft = {"default": {"harness": "claude"}}
        result = setup_project(self.store, "alpha", draft)
        self.assertFalse(result["validation"]["routable"])
        self.assertTrue(result["validation"]["blockers"])
        self.assertEqual(result["policy"], draft)

    def test_stale_revision_does_not_overwrite(self):
        initial = setup_project(self.store, "alpha")
        setup_project(self.store, "alpha", POLICY)
        with self.assertRaisesRegex(StateError, "changed"):
            setup_project(self.store, "alpha", {"rules": []}, initial["policy_revision"])
        self.assertEqual(json.loads(self.path.read_text()), POLICY)

    def test_unbound_observer_and_displaced_sessions_cannot_write(self):
        self.assertFalse(project_setup(self.store, "observer")["can_configure"])
        for session in ("observer", "unbound"):
            with self.subTest(session=session), self.assertRaises(StateError):
                setup_project(self.store, session, POLICY)
        self.store.open_session("replacement", "claude", "alpha", takeover=True)
        with self.assertRaises(StateError):
            setup_project(self.store, "alpha", POLICY)
        self.store.close_session("replacement")
        with self.assertRaises(StateError):
            setup_project(self.store, "replacement", POLICY)

    def test_symlink_directory_policy_and_fifo_are_rejected(self):
        elsewhere = self.home / "elsewhere"
        elsewhere.mkdir()
        self.path.parent.symlink_to(elsewhere, target_is_directory=True)
        with self.assertRaises((OSError, ValueError)):
            setup_project(self.store, "alpha", POLICY)
        self.assertEqual(list(elsewhere.iterdir()), [])
        self.path.parent.unlink()
        self.path.parent.mkdir()
        target = elsewhere / "file"
        target.write_text("untouched")
        self.path.symlink_to(target)
        with self.assertRaises((OSError, ValueError)):
            setup_project(self.store, "alpha", POLICY)
        self.assertEqual(target.read_text(), "untouched")
        self.path.unlink()
        os.mkfifo(self.path)
        with self.assertRaises((OSError, ValueError)):
            setup_project(self.store, "alpha", POLICY)

    def test_hardlink_replacement_does_not_modify_shared_inode(self):
        shared = self.home / "shared-policy.json"
        shared.write_text(json.dumps(POLICY))
        original = shared.read_bytes()
        self.path.parent.mkdir()
        os.link(shared, self.path)
        setup_project(self.store, "alpha", {"rules": []})
        self.assertEqual(shared.read_bytes(), original)
        self.assertNotEqual(shared.stat().st_ino, self.path.stat().st_ino)

    def test_cooperative_writers_reject_stale_revisions(self):
        initial = setup_project(self.store, "alpha")

        def save(index):
            try:
                setup_project(self.store, "alpha", POLICY, initial["policy_revision"])
                return True
            except StateError:
                return False

        with ThreadPoolExecutor(max_workers=2) as executor:
            self.assertEqual(sum(executor.map(save, range(2))), 1)
        self.assertEqual(len(list(self.path.parent.iterdir())), 1)

    def test_invalid_policies_and_duplicate_keys(self):
        for policy in ({}, {"default": []}, {"default": {"harness": []}}):
            with self.subTest(policy=policy), self.assertRaises(ValueError):
                setup_project(self.store, "alpha", policy)
        self.assertFalse(self.path.exists())
        self.path.parent.mkdir()
        self.path.write_text('{"rules": [], "rules": []}')
        state = project_setup(self.store, "alpha")
        self.assertIn("duplicate", state["policy_error"].lower())
        self.assertTrue(state["can_configure"])

    def test_external_edit_before_install_is_rejected(self):
        from orchestrator import onboarding

        initial = setup_project(self.store, "alpha")
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

    def test_edits_after_dispatch(self):
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
        self.assertTrue(setup_project(self.store, "alpha", POLICY)["changed"])
        self.assertTrue(project_setup(self.store, "alpha")["can_configure"])


if __name__ == "__main__":
    unittest.main()
