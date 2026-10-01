"""Real signed Git candidates, with an ephemeral per-command signing identity."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestrator import adapters
from orchestrator.worker_execution import (
    WorkspaceError,
    build_worker_command,
    capture_workspace,
    prepare_workspace,
)


def git(path, *arguments):
    return (
        subprocess.check_output(
            ["git", "-c", "core.hooksPath=/dev/null", *arguments], cwd=path, stderr=subprocess.PIPE
        )
        .decode()
        .strip()
    )


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        key = self.root / "signing"
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True)
        allowed = self.root / "allowed_signers"
        allowed.write_text("worker@example.test " + key.with_suffix(".pub").read_text())
        settings = {
            "user.name": "Worker Test",
            "user.email": "worker@example.test",
            "gpg.format": "ssh",
            "user.signingkey": str(key),
            "commit.gpgsign": "true",
            "gpg.ssh.allowedSignersFile": str(allowed),
        }
        environment = {"GIT_CONFIG_COUNT": str(len(settings))}
        for index, (name, value) in enumerate(settings.items()):
            environment[f"GIT_CONFIG_KEY_{index}"] = name
            environment[f"GIT_CONFIG_VALUE_{index}"] = value
        self.environment = patch.dict(os.environ, environment)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.source = self.root / "source"
        self.source.mkdir()
        git(self.source, "init", "-q")
        (self.source / "file.txt").write_text("original\n")
        git(self.source, "add", ".")
        git(self.source, "commit", "-qm", "initial")
        self.original = git(self.source, "rev-parse", "HEAD")

    def prepare(self, **kwargs):
        return prepare_workspace(
            self.root / "home", {"id": "request-1", "mode": "write"}, self.source, **kwargs
        )

    def assert_source_untouched(self):
        self.assertEqual(git(self.source, "rev-parse", "HEAD"), self.original)
        self.assertEqual(git(self.source, "status", "--porcelain"), "")
        self.assertEqual((self.source / "file.txt").read_text(), "original\n")

    def test_write_candidate_signed_and_source_untouched(self):
        workspace = self.prepare()
        path = Path(workspace["path"])
        self.assertTrue(path.is_relative_to(self.root / "home/data/workspaces/request-1"))
        (path / "file.txt").write_text("worker change\n")
        (path / "new.txt").write_text("new\n")
        result = capture_workspace(workspace)
        git(self.source, "verify-commit", result["commit"])
        self.assertEqual(result["changed_files"], ["file.txt", "new.txt"])
        self.assertIn("worker change", Path(result["diff_path"]).read_text())
        self.assert_source_untouched()

    def test_dirty_source_not_stashed(self):
        (self.source / "file.txt").write_text("user edits")
        with self.assertRaisesRegex(WorkspaceError, "clean"):
            self.prepare()
        self.assertEqual((self.source / "file.txt").read_text(), "user edits")
        self.assertEqual(git(self.source, "stash", "list"), "")

    def test_dependencies_only_apply_in_workspace(self):
        git(self.source, "checkout", "-qb", "dependency")
        (self.source / "dependency.txt").write_text("accepted")
        git(self.source, "add", ".")
        git(self.source, "commit", "-qm", "accepted dependency")
        dependency = git(self.source, "rev-parse", "HEAD")
        git(self.source, "checkout", "--detach", self.original)
        workspace = self.prepare(dependency_commits=[dependency])
        self.assertEqual((Path(workspace["path"]) / "dependency.txt").read_text(), "accepted")
        self.assertFalse((self.source / "dependency.txt").exists())
        self.assertEqual(capture_workspace(workspace)["changed_files"], ["dependency.txt"])
        self.assert_source_untouched()

    def test_read_worker_observes_accepted_changes_not_stale_source(self):
        first = self.prepare()
        (Path(first["path"]) / "file.txt").write_text("accepted implementation\n")
        result = capture_workspace(first)
        review = prepare_workspace(
            self.root / "home",
            {"id": "review", "mode": "read"},
            self.source,
            [result["commit"]],
        )
        self.assertEqual(
            (Path(review["path"]) / "file.txt").read_text(), "accepted implementation\n"
        )
        self.assertEqual(review["mode"], "read")
        self.assert_source_untouched()

    def test_transitive_accepted_dependencies(self):
        first = self.prepare()
        (Path(first["path"]) / "first.txt").write_text("first")
        first_result = capture_workspace(first)
        second = prepare_workspace(
            self.root / "home",
            {"id": "second", "mode": "write"},
            self.source,
            [first_result["commit"]],
        )
        (Path(second["path"]) / "second.txt").write_text("second")
        second_result = capture_workspace(second)
        third = prepare_workspace(
            self.root / "home",
            {"id": "third", "mode": "write"},
            self.source,
            [second_result["commit"]],
        )
        self.assertEqual((Path(third["path"]) / "first.txt").read_text(), "first")
        self.assertEqual((Path(third["path"]) / "second.txt").read_text(), "second")
        self.assert_source_untouched()

    def test_dependency_conflict_leaves_source_untouched(self):
        git(self.source, "checkout", "-qb", "dependency")
        (self.source / "file.txt").write_text("dependency change\n")
        git(self.source, "commit", "-qam", "dependency")
        dependency = git(self.source, "rev-parse", "HEAD")
        git(self.source, "checkout", "--detach", self.original)
        (self.source / "file.txt").write_text("source change\n")
        git(self.source, "commit", "-qam", "source change")
        source_head = git(self.source, "rev-parse", "HEAD")
        with self.assertRaisesRegex(WorkspaceError, "merge failed"):
            self.prepare(dependency_commits=[dependency])
        self.assertEqual(git(self.source, "rev-parse", "HEAD"), source_head)
        self.assertEqual(git(self.source, "status", "--porcelain"), "")
        self.assertEqual((self.source / "file.txt").read_text(), "source change\n")

    def test_index_symlink_redirection(self):
        workspace = self.prepare()
        path = Path(workspace["path"])
        administration = Path(git(path, "rev-parse", "--absolute-git-dir"))
        (administration / "index").unlink()
        (administration / "index").symlink_to(self.source / ".git/index")
        with self.assertRaisesRegex(WorkspaceError, "symlink"):
            capture_workspace(workspace)
        self.assert_source_untouched()

    def test_git_pointer_redirection(self):
        workspace = self.prepare()
        (Path(workspace["path"]) / ".git").write_text(f"gitdir: {self.source / '.git'}\n")
        with self.assertRaises(WorkspaceError):
            capture_workspace(workspace)
        self.assert_source_untouched()

    def test_git_symlink_redirection(self):
        workspace = self.prepare()
        pointer = Path(workspace["path"]) / ".git"
        pointer.unlink()
        pointer.symlink_to(self.source / ".git")
        with self.assertRaises(WorkspaceError):
            capture_workspace(workspace)
        self.assert_source_untouched()

    def test_file_symlink(self):
        workspace = self.prepare()
        (Path(workspace["path"]) / "outside").symlink_to(self.source / "file.txt")
        with self.assertRaises(WorkspaceError):
            capture_workspace(workspace)
        self.assert_source_untouched()

    def test_hardlink(self):
        workspace = self.prepare()
        os.link(self.source / "file.txt", Path(workspace["path"]) / "outside")
        with self.assertRaises(WorkspaceError):
            capture_workspace(workspace)

    def test_changed_provenance(self):
        workspace = self.prepare()
        workspace["base_commit"] = "0" * 40
        with self.assertRaisesRegex(WorkspaceError, "provenance changed"):
            capture_workspace(workspace)

    def test_artifact_symlink(self):
        workspace = self.prepare()
        (Path(workspace["path"]).parent / "changes.patch").symlink_to(self.source / "file.txt")
        with self.assertRaisesRegex(WorkspaceError, "symlink"):
            capture_workspace(workspace)
        self.assert_source_untouched()

    def test_bounded_artifact(self):
        workspace = self.prepare()
        (Path(workspace["path"]) / "large").write_text("x" * (5 * 1024 * 1024))
        with self.assertRaisesRegex(WorkspaceError, "output exceeds"):
            capture_workspace(workspace)
        self.assert_source_untouched()

    def test_read_non_git_and_invalid_id(self):
        result = prepare_workspace(self.root, {"mode": "read"}, self.root)
        self.assertEqual(result["path"], str(self.root))
        with self.assertRaisesRegex(WorkspaceError, "request ID"):
            prepare_workspace(self.root, {"mode": "write", "id": "../escape"}, self.root)


class CommandTests(unittest.TestCase):
    def test_claude_stdin_restricted(self):
        command = build_worker_command(
            {"adapters": {"claude": {"command": ["claude"]}}},
            {"harness": "claude", "model": "claude-sonnet-4-6", "effort": "high"},
            "write",
            "secret prompt",
            Path("/owned"),
            Path("/result"),
            project_root=Path("/source"),
        )
        self.assertIn("--restricted", command)
        self.assertNotIn("secret prompt", command)
        self.assertNotIn("--add-dir", command)
        self.assertNotIn("Bash", ",".join(command))
        self.assertEqual(command[command.index("--tools") + 1], "Read,Glob,Grep,Edit,Write")

    def test_pi_delegates_controlled_tools(self):
        with patch.object(
            adapters, "build_pi_command", create=True, return_value=["pi"]
        ) as builder:
            profile = {"harness": "pi", "provider": "openai", "model": "gpt-5", "effort": "high"}
            command = build_worker_command(
                {}, profile, "read", "secret", Path("/owned"), Path("/output")
            )
        self.assertEqual(command, ["pi"])
        self.assertEqual(builder.call_args.args[1]["allowed_tools"], ["Read", "Glob", "Grep"])
        self.assertTrue(builder.call_args.kwargs["stdin_prompt"])
        self.assertEqual(builder.call_args.kwargs["mode"], "read")

    def test_reject_wrong_executors(self):
        for profile in (
            {"harness": "codex", "model": "gpt-5", "effort": "high"},
            {"harness": "pi", "provider": "anthropic", "model": "claude-sonnet", "effort": "high"},
            {"harness": "claude", "model": "gpt-5", "effort": "high"},
        ):
            with self.subTest(profile=profile), self.assertRaises(adapters.AdapterError):
                build_worker_command({}, profile, "read", "prompt", Path("/owned"), Path("/out"))
