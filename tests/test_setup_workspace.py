"""Real Git workspace preparation with local onboarding policy metadata."""

import os
import socket
import unittest
from pathlib import Path

from orchestrator.worker_execution import WorkspaceError, capture_workspace
from tests import test_worker_execution as worker_tests

git = worker_tests.git


class SetupWorkspaceTests(unittest.TestCase):
    setUp = worker_tests.WorkspaceTests.setUp
    prepare = worker_tests.WorkspaceTests.prepare

    def make_policy(self):
        policy = self.source / ".orchestrator" / "crew-dispatch.json"
        policy.parent.mkdir(exist_ok=True)
        policy.write_text("{}\n")
        return policy

    def assert_rejected(self):
        with self.assertRaises(WorkspaceError):
            self.prepare()
        self.assertFalse((self.root / "home/data/workspaces/request-1").exists())
        self.assertEqual(git(self.source, "stash", "list"), "")

    def test_other_untracked_paths_are_not_exempt(self):
        self.make_policy()
        for name in (
            "new.txt",
            ".orchestrator/other.json",
            ".orchestrator/crew-dispatch.json.backup",
            ".orchestrator/crew-dispatch.json\n",
            '.orchestrator/crew-dispatch.json"',
            ".orchestrator/crew-dispatch.json\t",
            '".orchestrator/crew-dispatch.json"',
        ):
            with self.subTest(name=name):
                other = self.source / name
                other.parent.mkdir(parents=True, exist_ok=True)
                other.write_text("untracked")
                self.assert_rejected()
                other.unlink()

    def test_tracked_policy_edits_do_not_block_isolated_workers(self):
        policy = self.make_policy()
        git(self.source, "add", ".")
        git(self.source, "commit", "-qm", "track policy")
        for index, (staged, deleted) in enumerate(
            ((False, False), (True, False), (False, True), (True, True))
        ):
            with self.subTest(staged=staged, deleted=deleted):
                git(self.source, "reset", "--hard", "HEAD")
                if deleted:
                    policy.unlink()
                else:
                    policy.write_text("changed policy")
                if staged:
                    git(self.source, "add", "--all")
                from orchestrator.worker_execution import prepare_workspace

                workspace = prepare_workspace(
                    self.root / "home", {"id": f"policy-{index}", "mode": "write"}, self.source
                )
                self.assertEqual(
                    (Path(workspace["path"]) / ".orchestrator/crew-dispatch.json").read_text(),
                    "{}\n",
                )
                self.assertEqual(policy.exists(), not deleted)

    def test_staged_policy_addition_does_not_block_workers(self):
        self.make_policy()
        git(self.source, "add", ".")
        workspace = self.prepare()
        self.assertFalse((Path(workspace["path"]) / ".orchestrator/crew-dispatch.json").exists())
        self.assertEqual(
            git(self.source, "status", "--porcelain"), "A  .orchestrator/crew-dispatch.json"
        )

    def test_source_changes_with_policy_are_not_exempt(self):
        self.make_policy()
        (self.source / "file.txt").write_text("user changes")
        self.assert_rejected()
        git(self.source, "add", "file.txt")
        self.assert_rejected()
        self.assertEqual((self.source / "file.txt").read_text(), "user changes")

    def test_nonordinary_policy_is_rejected(self):
        policy = self.make_policy()
        target = self.root / "target"
        target.write_text("{}")
        for kind in ("symlink", "dangling", "directory", "fifo", "socket", "hardlink"):
            with self.subTest(kind=kind):
                policy.unlink()
                listener = None
                if kind == "symlink":
                    policy.symlink_to(target)
                elif kind == "dangling":
                    policy.symlink_to(self.root / "missing")
                elif kind == "directory":
                    policy.mkdir()
                elif kind == "fifo":
                    os.mkfifo(policy)
                elif kind == "socket":
                    listener = socket.socket(socket.AF_UNIX)
                    listener.bind(str(policy))
                else:
                    os.link(target, policy)
                try:
                    self.assert_rejected()
                finally:
                    if listener:
                        listener.close()
                    if kind == "directory":
                        policy.rmdir()
                    else:
                        policy.unlink()
                    policy.write_text("{}")

    def test_symlinked_policy_directory_is_rejected(self):
        target = self.root / "policies"
        target.mkdir()
        (target / "crew-dispatch.json").write_text("{}")
        (self.source / ".orchestrator").symlink_to(target, target_is_directory=True)
        self.assert_rejected()

    def test_untracked_policy_allows_signed_candidate_without_source_changes(self):
        policy = self.source / ".orchestrator" / "crew-dispatch.json"
        policy.parent.mkdir()
        policy.write_text('{"version": 1}\n')
        original_status = git(self.source, "status", "--porcelain=v1", "--untracked-files=all")
        self.assertEqual(original_status, "?? .orchestrator/crew-dispatch.json")
        original_config = (self.source / ".git/config").read_bytes()
        original_exclude = (self.source / ".git/info/exclude").read_bytes()
        workspace = self.prepare()
        self.assertFalse((Path(workspace["path"]) / ".orchestrator").exists())
        (Path(workspace["path"]) / "file.txt").write_text("candidate\n")
        candidate = capture_workspace(workspace)
        git(self.source, "verify-commit", candidate["commit"])
        self.assertEqual(candidate["changed_files"], ["file.txt"])
        self.assertEqual(git(self.source, "rev-parse", "HEAD"), self.original)
        self.assertEqual(
            git(self.source, "status", "--porcelain=v1", "--untracked-files=all"),
            original_status,
        )
        self.assertEqual(policy.read_text(), '{"version": 1}\n')
        self.assertEqual((self.source / "file.txt").read_text(), "original\n")
        self.assertEqual((self.source / ".git/config").read_bytes(), original_config)
        self.assertEqual((self.source / ".git/info/exclude").read_bytes(), original_exclude)
        self.assertFalse((self.source / ".gitignore").exists())
        self.assertEqual(git(self.source, "stash", "list"), "")
