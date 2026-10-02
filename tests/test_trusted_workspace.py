"""Trusted setup uses an owned snapshot, never a user commit or stash."""

import os
import subprocess
import sys
import unittest
from pathlib import Path

from orchestrator.worker_execution import WorkspaceError, capture_workspace
from tests import test_worker_execution as fixtures

git = fixtures.git


class TrustedWorkspaceTests(unittest.TestCase):
    setUp = fixtures.WorkspaceTests.setUp
    prepare = fixtures.WorkspaceTests.prepare

    def test_dirty_source_starts_without_manual_commit(self):
        (self.source / "file.txt").write_text("staged user edit\n")
        git(self.source, "add", "file.txt")
        (self.source / "file.txt").write_text("current user edit\n")
        (self.source / "new.txt").write_text("untracked user file\n")
        index = (self.source / ".git/index").read_bytes()
        workspace = self.prepare(trusted=True)
        path = Path(workspace["path"])
        self.assertEqual((path / "file.txt").read_text(), "current user edit\n")
        self.assertEqual((path / "new.txt").read_text(), "untracked user file\n")
        (path / "worker.txt").write_text("worker result\n")
        result = capture_workspace(workspace)
        git(self.source, "verify-commit", result["commit"])
        self.assertEqual((self.source / ".git/index").read_bytes(), index)
        self.assertEqual(git(self.source, "rev-parse", "HEAD"), self.original)
        self.assertEqual((self.source / "file.txt").read_text(), "current user edit\n")
        self.assertEqual((self.source / "new.txt").read_text(), "untracked user file\n")
        self.assertFalse((self.source / "worker.txt").exists())
        self.assertEqual(git(self.source, "stash", "list"), "")

    def test_explicit_branch_excludes_unrelated_source_edits(self):
        git(self.source, "branch", "selected", self.original)
        (self.source / "file.txt").write_text("root edits")
        (self.source / "untracked.txt").write_text("root new file")
        workspace = self.prepare(trusted=True, base_ref="selected")
        self.assertEqual(workspace["source_changes"], "excluded")
        self.assertEqual((Path(workspace["path"]) / "file.txt").read_text(), "original\n")
        self.assertFalse((Path(workspace["path"]) / "untracked.txt").exists())
        self.assertEqual(capture_workspace(workspace)["changed_files"], [])
        self.assertEqual((self.source / "file.txt").read_text(), "root edits")

    def test_unborn_repository_snapshot_does_not_commit_source(self):
        self.source = self.root / "unborn"
        self.source.mkdir()
        git(self.source, "init", "-q")
        (self.source / "draft.txt").write_text("draft")
        git(self.source, "add", "draft.txt")
        index = (self.source / ".git/index").read_bytes()
        source_head = (self.source / ".git/HEAD").read_bytes()
        workspace = self.prepare(trusted=True)
        self.assertTrue(workspace["source_unborn"])
        self.assertEqual((Path(workspace["path"]) / "draft.txt").read_text(), "draft")
        result = capture_workspace(workspace)
        git(self.source, "verify-commit", result["commit"])
        self.assertEqual((self.source / ".git/index").read_bytes(), index)
        self.assertEqual((self.source / ".git/HEAD").read_bytes(), source_head)
        self.assertEqual(
            git(
                self.source,
                "for-each-ref",
                "--format=%(refname)",
                git(self.source, "symbolic-ref", "HEAD"),
            ),
            "",
        )

    def test_snapshot_filters_sensitive_files_and_ignored_files(self):
        for name in ("credentials.json", "private.key", "AGENTS.md", ".env", ".gitignore"):
            (self.source / name).write_text("ignored.txt\n")
        (self.source / "ignored.txt").write_text("ignored")
        (self.source / ".orchestrator").mkdir()
        (self.source / ".orchestrator/crew-dispatch.json").write_text("{}")
        workspace = self.prepare(trusted=True)
        self.assertEqual(
            sorted(path.name for path in Path(workspace["path"]).iterdir()),
            [".git", ".gitignore", "AGENTS.md", "file.txt"],
        )
        self.assertEqual((self.source / "credentials.json").read_text(), "ignored.txt\n")

    def test_normal_instruction_symlink_survives_snapshot_and_capture(self):
        (self.source / "AGENTS.md").write_text("Project instructions")
        (self.source / "CLAUDE.md").symlink_to("AGENTS.md")
        git(self.source, "add", "AGENTS.md", "CLAUDE.md")
        git(self.source, "commit", "-qm", "Project instruction alias")
        workspace = self.prepare(trusted=True)
        path = Path(workspace["path"])
        self.assertTrue((path / "CLAUDE.md").is_symlink())
        self.assertEqual((path / "CLAUDE.md").read_text(), "Project instructions")
        (path / "AGENTS.md").write_text("Updated instructions")
        captured = capture_workspace(workspace)
        self.assertIn("AGENTS.md", captured["changed_files"])
        self.assertEqual((self.source / "CLAUDE.md").read_text(), "Project instructions")
        self.assertTrue((self.source / "CLAUDE.md").is_symlink())

    def test_unsafe_symlink_is_not_followed(self):
        (self.source / "linked").symlink_to(self.root)
        with self.assertRaisesRegex(WorkspaceError, "symlink"):
            self.prepare(trusted=True)
        self.assertTrue((self.source / "linked").is_symlink())

    def test_hardlink_is_not_copied(self):
        os.link(self.source / "file.txt", self.source / "linked")
        with self.assertRaisesRegex(WorkspaceError, "non-linked"):
            self.prepare(trusted=True)

    def test_dirty_snapshot_and_signed_dependency_are_combined(self):
        git(self.source, "checkout", "-qb", "dependency")
        (self.source / "dependency.txt").write_text("accepted")
        git(self.source, "add", ".")
        git(self.source, "commit", "-qm", "dependency")
        dependency = git(self.source, "rev-parse", "HEAD")
        git(self.source, "checkout", "--detach", self.original)
        (self.source / "file.txt").write_text("user edits")
        workspace = self.prepare(trusted=True, dependency_commits=[dependency])
        path = Path(workspace["path"])
        self.assertEqual((path / "dependency.txt").read_text(), "accepted")
        self.assertEqual((path / "file.txt").read_text(), "user edits")
        result = capture_workspace(workspace)
        self.assertEqual(result["changed_files"], ["dependency.txt", "file.txt"])
        git(self.source, "verify-commit", result["commit"])
        self.assertEqual(git(self.source, "rev-parse", "HEAD"), self.original)

    def test_deleted_and_file_directory_replacement(self):
        (self.source / "file.txt").unlink()
        (self.source / "file.txt").mkdir()
        (self.source / "file.txt/child.txt").write_text("child")
        workspace = self.prepare(trusted=True)
        self.assertEqual((Path(workspace["path"]) / "file.txt/child.txt").read_text(), "child")
        self.assertEqual(
            capture_workspace(workspace)["changed_files"], ["file.txt", "file.txt/child.txt"]
        )

    def test_deleted_file_and_executable_untracked_file(self):
        (self.source / "file.txt").unlink()
        (self.source / "script.sh").write_text("#!/bin/sh\nexit 0\n")
        (self.source / "script.sh").chmod(0o755)
        workspace = self.prepare(trusted=True)
        path = Path(workspace["path"])
        self.assertFalse((path / "file.txt").exists())
        self.assertTrue((path / "script.sh").stat().st_mode & 0o100)
        self.assertEqual(capture_workspace(workspace)["changed_files"], ["file.txt", "script.sh"])

    def test_explicit_baseline_does_not_reapply_root_changes(self):
        (self.source / "file.txt").write_text("unrelated source changes")
        workspace = self.prepare(trusted=True, baseline_commit=self.original)
        self.assertEqual(workspace["source_changes"], "excluded")
        self.assertEqual((Path(workspace["path"]) / "file.txt").read_text(), "original\n")

    def test_trusted_worker_commit_can_be_captured(self):
        (self.source / "file.txt").write_text("user edit")
        workspace = self.prepare(trusted=True)
        path = Path(workspace["path"])
        (path / "worker.txt").write_text("worker commit")
        git(path, "add", ".")
        git(path, "commit", "-qm", "worker's own commit")
        result = capture_workspace(workspace)
        git(self.source, "verify-commit", result["commit"])
        self.assertEqual(result["changed_files"], ["file.txt", "worker.txt"])

    def add_submodule(self):
        dependency = self.root / "dependency"
        dependency.mkdir()
        git(dependency, "init", "-q")
        (dependency / "library.txt").write_text("library\n")
        (dependency / "library.py").write_text("VALUE = 42\n")
        (dependency / ".gitignore").write_text("build/\n")
        git(dependency, "add", ".")
        git(dependency, "commit", "-qm", "library")
        git(
            self.source,
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "add",
            str(dependency),
            "vendor/library",
        )
        git(self.source, "commit", "-qam", "submodule")
        git(self.source, "config", "--remove-section", "submodule.vendor/library")

    def initialize_submodule(self, path):
        git(
            path, "-c", "protocol.file.allow=always", "submodule", "update", "--init", "--recursive"
        )

    def test_trusted_submodule_init_and_build_capture_root_change(self):
        self.add_submodule()
        workspace = self.prepare(trusted=True)
        path = Path(workspace["path"])
        self.initialize_submodule(path)
        build = path / "vendor/library/build"
        build.mkdir()
        subprocess.run(
            [
                sys.executable,
                "-c",
                "import py_compile; py_compile.compile('library.py', cfile='build/library.pyc', doraise=True)",
            ],
            cwd=build.parent,
            check=True,
        )
        self.assertTrue((build / "library.pyc").is_file())
        (path / "file.txt").write_text("root change\n")
        result = capture_workspace(workspace)
        self.assertEqual(result["changed_files"], ["file.txt"])
        git(self.source, "verify-commit", result["commit"])

    def test_dirty_source_submodule_is_not_silently_dropped(self):
        self.add_submodule()
        (self.source / "vendor/library/library.txt").write_text("uncommitted")
        with self.assertRaisesRegex(WorkspaceError, "Submodule.*uncommitted changes"):
            self.prepare(trusted=True)

    def test_dirty_worker_submodule_is_not_silently_dropped(self):
        self.add_submodule()
        workspace = self.prepare(trusted=True)
        path = Path(workspace["path"])
        self.initialize_submodule(path)
        (path / "vendor/library/new.txt").write_text("untracked")
        with self.assertRaisesRegex(WorkspaceError, "Submodule.*uncommitted changes"):
            capture_workspace(workspace)

    def test_dirty_tracked_worker_submodule_is_rejected(self):
        self.add_submodule()
        workspace = self.prepare(trusted=True)
        path = Path(workspace["path"])
        self.initialize_submodule(path)
        (path / "vendor/library/library.txt").write_text("dirty")
        with self.assertRaisesRegex(WorkspaceError, "Submodule.*uncommitted changes"):
            capture_workspace(workspace)

    def test_changed_source_submodule_checkout_is_not_dropped(self):
        self.add_submodule()
        module = self.source / "vendor/library"
        (module / "library.txt").write_text("changed")
        git(module, "commit", "-qam", "changed library")
        with self.assertRaisesRegex(WorkspaceError, "changed checkout"):
            self.prepare(trusted=True)

    def test_committed_worker_submodule_pointer_is_captured(self):
        self.add_submodule()
        workspace = self.prepare(trusted=True)
        path = Path(workspace["path"])
        self.initialize_submodule(path)
        module = path / "vendor/library"
        (module / "library.txt").write_text("changed")
        git(module, "commit", "-qam", "changed library")
        commit = git(module, "rev-parse", "HEAD")
        result = capture_workspace(workspace)
        self.assertEqual(result["changed_files"], ["vendor/library"])
        self.assertIn(commit, Path(result["diff_path"]).read_text())

    def test_uninitialized_submodule_capture(self):
        self.add_submodule()
        workspace = self.prepare(trusted=True)
        self.assertEqual(capture_workspace(workspace)["changed_files"], [])

    def test_nonempty_uninitialized_submodule_is_rejected(self):
        self.add_submodule()
        workspace = self.prepare(trusted=True)
        (Path(workspace["path"]) / "vendor/library/arbitrary").write_text("not a checkout")
        with self.assertRaisesRegex(WorkspaceError, "not initialized"):
            capture_workspace(workspace)

    def test_trusted_submodule_symlink_is_rejected(self):
        self.add_submodule()
        workspace = self.prepare(trusted=True)
        directory = Path(workspace["path"]) / "vendor/library"
        directory.rmdir()
        directory.symlink_to(self.source / "vendor/library")
        with self.assertRaisesRegex(WorkspaceError, "symlink"):
            capture_workspace(workspace)

    def test_trusted_config_redirection_is_rejected(self):
        workspace = self.prepare(trusted=True)
        path = Path(workspace["path"])
        git(path, "config", "core.worktree", str(self.source))
        with self.assertRaisesRegex(WorkspaceError, "metadata changed"):
            capture_workspace(workspace)

    def test_restricted_submodule_init_still_rejects_config_changes(self):
        self.add_submodule()
        workspace = self.prepare()
        self.initialize_submodule(Path(workspace["path"]))
        with self.assertRaisesRegex(WorkspaceError, "metadata changed"):
            capture_workspace(workspace)

    def test_trusted_normal_file_replaced_by_directory_is_rejected(self):
        workspace = self.prepare(trusted=True)
        path = Path(workspace["path"]) / "file.txt"
        path.unlink()
        path.mkdir()
        with self.assertRaisesRegex(WorkspaceError, "ordinary"):
            capture_workspace(workspace)

    def test_subdirectory_cannot_expand_project_boundary(self):
        nested = self.source / "nested"
        nested.mkdir()
        self.source = nested
        with self.assertRaisesRegex(WorkspaceError, "repository root"):
            self.prepare(trusted=True)
