"""End-user build confinement, with no paid calls or network dependencies."""

import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestrator.commands import (
    BWRAP,
    OUTPUT_LIMIT,
    RESULT_OUTPUT_LIMIT,
    _prepare,
    run_command,
)


class CommandConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "workspace"
        self.source = self.root / "source"
        self.workspace.mkdir()
        self.source.mkdir()
        self.options = {
            "cwd": str(self.workspace),
            "project_root": str(self.source),
            "mode": "write",
            "artifact_directory": str(self.root / "artifacts"),
        }

    def test_argv_environment_and_immutable_options(self):
        original = dict(self.options)
        argv, environment, timeout, _ = _prepare(self.options, {"command": "true"})
        self.assertEqual(self.options, original)
        self.assertIn("--unshare-all", argv)
        self.assertNotIn("--share-net", argv)
        self.assertEqual(argv[-3:], ["/bin/sh", "-c", "true"])
        self.assertEqual(timeout, 120)
        self.assertEqual(
            set(environment),
            {"PATH", "HOME", "TMPDIR", "LANG", "CARGO_BUILD_JOBS", "OMP_NUM_THREADS", "MAKEFLAGS"},
        )
        self.assertNotIn("/etc", argv)

    def test_trusted_host_toolchain_paths_work_without_copying_api_keys(self):
        tools = self.root / "installed-tools"
        tools.mkdir()
        tool = tools / "project-build"
        tool.write_text('#!/bin/sh\nprintf "installed:%s:%s" "$HOME" "${OPENAI_API_KEY-unset}"\n')
        tool.chmod(0o700)
        result = run_command(
            {
                **self.options,
                "sandbox": False,
                "host_environment": {
                    "PATH": str(tools) + ":/usr/bin:/bin",
                    "HOME": str(self.root / "user-home"),
                    "OPENAI_API_KEY": "never-copy-this",
                },
            },
            {"command": "project-build"},
        )
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["output"], "installed:" + str(self.root / "user-home") + ":unset")

    def test_reject_hostile_arguments(self):
        for arguments in (
            {"command": ""},
            {"command": "x" * 32001},
            {"command": "true", "cwd": "../source"},
            {"command": "true", "cwd": "/tmp"},
            {"command": "true", "timeout_seconds": 121},
            {"command": "true", "timeout_seconds": True},
            {"command": "true", "env": {"PATH": "/evil"}},
        ):
            with self.subTest(arguments=str(arguments)[:80]), self.assertRaises(ValueError):
                _prepare(self.options, arguments)

    def test_symlink_cwd_and_tools(self):
        (self.workspace / "escape").symlink_to(self.source, target_is_directory=True)
        with self.assertRaises(ValueError):
            _prepare(self.options, {"command": "true", "cwd": "escape"})
        tool = self.root / "tool"
        tool.symlink_to("/usr/bin/true")
        with self.assertRaises(ValueError):
            _prepare({**self.options, "tool_paths": {"tool": str(tool)}}, {"command": "true"})
        with self.assertRaises(ValueError):
            _prepare(
                {**self.options, "tool_paths": {"../evil": "/usr/bin/true"}}, {"command": "true"}
            )

    def test_invalid_options_and_artifact_paths(self):
        for change in (
            {"sandbox": "false"},
            {"sandbox": 0},
            {"mode": "anything"},
            {"network": "yes"},
            {"cpus": 0},
            {"timeout_seconds": 901},
            {"cwd": "/usr"},
            {"artifact_directory": str(self.workspace / "logs")},
        ):
            with self.subTest(change=change), self.assertRaises(ValueError):
                _prepare({**self.options, **change}, {"command": "true"})
        private = self.root / "private"
        private.mkdir(mode=0o700)
        (self.root / "linked").symlink_to(private, target_is_directory=True)
        with self.assertRaises(OSError):
            run_command(
                {**self.options, "artifact_directory": str(self.root / "linked")},
                {"command": "true"},
            )
        public = self.root / "public"
        public.mkdir(mode=0o755)
        with self.assertRaises(ValueError):
            run_command({**self.options, "artifact_directory": str(public)}, {"command": "true"})

    def test_unavailable_never_falls_back(self):
        with patch("orchestrator.commands.BWRAP", "/nonexistent/bwrap"):
            result = run_command(self.options, {"command": "touch must-not-exist"})
        self.assertIsNone(result["exit_code"])
        self.assertTrue(result["sandboxed"])
        self.assertIn("Sandbox unavailable", result["error"])
        self.assertFalse((self.workspace / "must-not-exist").exists())


class TrustedCommandTests(unittest.TestCase):
    setUp = CommandConfigurationTests.setUp

    def run_script(self, script, **options):
        with patch("orchestrator.commands.BWRAP", "/nonexistent/bwrap"):
            return run_command({**self.options, "sandbox": False, **options}, {"command": script})

    def test_build_is_explicitly_not_a_sandbox_even_in_read_mode(self):
        sibling = self.root / "other-project"
        sibling.mkdir()
        marker = sibling / "marker"
        marker.write_text("original")
        (self.workspace / "build.sh").write_text(
            "#!/bin/sh\nset -eu\nprintf built > result\n"
            f"printf changed > {shlex.quote(str(marker))}\n"
            "printf 'build complete\\n'\n"
        )
        real_popen = subprocess.Popen
        with patch("orchestrator.commands.subprocess.Popen", wraps=real_popen) as launch:
            result = self.run_script("sh build.sh", mode="read")
        self.assertEqual(launch.call_count, 1)
        self.assertEqual(launch.call_args.args[0][-3:], ["/bin/sh", "-c", "sh build.sh"])
        self.assertIn("--guard-command", launch.call_args.args[0])
        self.assertEqual(launch.call_args.kwargs["cwd"], str(self.workspace))
        self.assertEqual(result["exit_code"], 0, result)
        self.assertFalse(result["sandboxed"])
        self.assertNotIn("error", result)
        self.assertEqual((self.workspace / "result").read_text(), "built")
        self.assertEqual(marker.read_text(), "changed")
        self.assertIn("build complete", result["output"])

    def test_compile_and_test_temporary_checkout(self):
        subprocess.run(["git", "init", "-q", str(self.workspace)], check=True)
        (self.workspace / "main.c").write_text("int main(void) { return 0; }\n")
        (self.workspace / "Makefile").write_text(
            "all:\n\tcc -Wall -Werror main.c -o program\n"
            "test: all\n\t./program\n\tgit status --short\n"
        )
        result = self.run_script("make test")
        self.assertEqual(result["exit_code"], 0, result)
        self.assertTrue((self.workspace / "program").is_file())
        (self.workspace / "main.c").write_text("invalid C syntax\n")
        failure = self.run_script("make test")
        self.assertNotEqual(failure["exit_code"], 0, failure)
        self.assertIn("main.c", failure["output"])
        self.assertIn("error:", failure["output"])
        self.assertEqual(Path(failure["log_path"]).read_text(), failure["output"])

    def test_host_tool_alias_and_no_inherited_credentials_or_path(self):
        tool = self.root / "actual-tool-name"
        tool.write_text("#!/bin/sh\nprintf approved-tool")
        tool.chmod(0o700)
        with patch.dict(
            os.environ,
            {"PROVIDER_SECRET": "secret-value", "GIT_SSH_COMMAND": "evil", "PATH": "/evil"},
        ):
            result = self.run_script("env; approved", tool_paths={"approved": str(tool)})
        self.assertEqual(result["exit_code"], 0, result)
        self.assertIn("approved-tool", result["output"])
        self.assertNotIn("secret-value", result["output"])
        self.assertNotIn("GIT_SSH", result["output"])
        self.assertNotIn("/evil", result["output"])

    def test_routing_and_host_only_authority(self):
        nested = self.workspace / "nested"
        nested.mkdir()
        result = run_command(
            {**self.options, "sandbox": False}, {"command": "pwd", "cwd": "nested"}
        )
        self.assertEqual(result["output"].strip(), str(nested))
        (self.workspace / "escape").symlink_to(self.source, target_is_directory=True)
        for extra in (
            {"cwd": "../source"},
            {"cwd": str(self.source)},
            {"cwd": "escape"},
            {"env": {"SECRET": "injected"}},
            {"sandbox": False},
            {"tool_paths": {"evil": "/usr/bin/true"}},
        ):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                run_command({**self.options, "sandbox": False}, {"command": "true", **extra})

    def test_artifacts_and_exit_status(self):
        result = self.run_script("printf failed; exit 7")
        self.assertEqual(result["exit_code"], 7)
        self.assertFalse(result["sandboxed"])
        log = Path(result["log_path"])
        self.assertEqual(log.read_text(), "failed")
        self.assertEqual(log.stat().st_mode & 0o777, 0o600)
        self.assertEqual(log.parent.stat().st_mode & 0o777, 0o700)
        metadata_path = Path(result["metadata_path"])
        self.assertEqual(metadata_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(
            json.loads(metadata_path.read_text()),
            {key: value for key, value in result.items() if key != "output"},
        )

    def test_launch_failure_does_not_claim_confinement(self):
        with patch("orchestrator.commands.subprocess.Popen", side_effect=OSError("unavailable")):
            result = self.run_script("true")
        self.assertFalse(result["sandboxed"])
        self.assertIsNone(result["exit_code"])
        self.assertIn("Trusted command execution failed", result["error"])
        self.assertNotIn("confinement", result["error"])

    def test_timeout_and_process_group_cleanup(self):
        result = self.run_script(
            "(sleep 2; touch survived) & echo child-started; wait", timeout_seconds=1
        )
        self.assertTrue(result["timed_out"], result)
        self.assertFalse(result["sandboxed"])
        second = self.run_script("sleep 2; test ! -e survived")
        self.assertEqual(second["exit_code"], 0, second)

    def test_parent_death_cleans_owned_group_not_unrelated_process(self):
        options = {**self.options, "sandbox": False}
        script = "(sleep 2; touch survived) & printf ready > ready; wait"
        parent = subprocess.Popen(
            [
                sys.executable,
                "-c",
                (
                    "from orchestrator.commands import run_command; "
                    f"run_command({options!r}, {{'command': {script!r}}})"
                ),
            ],
            cwd=str(Path(__file__).resolve().parents[1]),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        unrelated = subprocess.Popen(["sleep", "30"])
        try:
            deadline = time.monotonic() + 10
            while not (self.workspace / "ready").exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue((self.workspace / "ready").exists())
            parent.kill()
            parent.wait(timeout=5)
            time.sleep(2.3)
            self.assertFalse((self.workspace / "survived").exists())
            self.assertIsNone(unrelated.poll())
        finally:
            if parent.poll() is None:
                parent.kill()
            parent.wait()
            unrelated.terminate()
            unrelated.wait()

    def test_verbose_output_runs_to_completion_with_head_and_tail(self):
        result = self.run_script(
            "printf FIRST-LINE; yes verbose-build-line | head -c 3000000; "
            "printf '\\nFINAL SUMMARY: 2 failed'; exit 4"
        )
        self.assertEqual(result["exit_code"], 4, result)
        self.assertFalse(result["timed_out"])
        self.assertEqual(result["output_bytes"], len("FIRST-LINE") + 3000000 + 24)
        self.assertTrue(result["output_truncated"])
        self.assertTrue(result["log_truncated"])
        self.assertLessEqual(len(result["output"].encode()), RESULT_OUTPUT_LIMIT + 100)
        self.assertTrue(result["output"].startswith("FIRST-LINE"))
        self.assertTrue(result["output"].endswith("FINAL SUMMARY: 2 failed"))
        self.assertIn("output bytes omitted", result["output"])
        log = Path(result["log_path"]).read_bytes()
        self.assertLessEqual(len(log), OUTPUT_LIMIT + 100)
        self.assertTrue(log.startswith(b"FIRST-LINE"))
        self.assertTrue(log.endswith(b"FINAL SUMMARY: 2 failed"))
        metadata = json.loads(Path(result["metadata_path"]).read_text())
        self.assertEqual(metadata["output_bytes"], result["output_bytes"])
        self.assertTrue(metadata["log_truncated"])

    def test_small_output_is_complete(self):
        result = self.run_script("printf complete-output")
        self.assertEqual(result["output"], "complete-output")
        self.assertEqual(result["output_bytes"], len("complete-output"))
        self.assertFalse(result["output_truncated"])
        self.assertEqual(Path(result["log_path"]).read_text(), "complete-output")


class SandboxTests(CommandConfigurationTests):
    test_compile_and_test_temporary_checkout = (
        TrustedCommandTests.test_compile_and_test_temporary_checkout
    )

    @classmethod
    def setUpClass(cls):
        if not Path(BWRAP).is_file():
            raise unittest.SkipTest("bubblewrap unavailable")
        probe = subprocess.run(
            [
                BWRAP,
                "--unshare-all",
                "--die-with-parent",
                "--ro-bind",
                "/usr",
                "/usr",
                "--symlink",
                "usr/bin",
                "/bin",
                "--symlink",
                "usr/lib",
                "/lib",
                "--symlink",
                "usr/lib64",
                "/lib64",
                "--proc",
                "/proc",
                "--dev",
                "/dev",
                "--tmpfs",
                "/tmp",
                "/usr/bin/true",
            ],
            capture_output=True,
            timeout=10,
            check=False,
        )
        if probe.returncode:
            raise unittest.SkipTest("bubblewrap not permitted: " + probe.stderr.decode())

    def run_script(self, script, **options):
        return run_command({**self.options, **options}, {"command": script})

    def test_real_build_denies_other_projects_source_and_git(self):
        subprocess.run(["git", "init", "-q", str(self.workspace)], check=True)
        sibling = self.root / "other-project"
        sibling.mkdir()
        global_directory = self.root / "global-config"
        global_directory.mkdir()
        protected = [
            sibling / "marker",
            global_directory / "marker",
            self.source / "marker",
            self.workspace / ".git" / "config",
        ]
        for path in protected[:3]:
            path.write_text("original")
        originals = [path.read_bytes() for path in protected]
        script = "#!/bin/sh\nset -eu\nprintf built > result\n"
        for path in protected:
            script += (
                f"if (printf hacked > {shlex.quote(str(path))}) 2>/dev/null; then exit 90; fi\n"
            )
        script += "git branch --list\nprintf 'build complete\\n'\n"
        (self.workspace / "build.sh").write_text(script)
        result = self.run_script("sh build.sh")
        self.assertEqual(result["exit_code"], 0, result)
        self.assertEqual((self.workspace / "result").read_text(), "built")
        self.assertEqual([path.read_bytes() for path in protected], originals)
        self.assertIn("build complete", result["output"])

    def test_read_only_and_private_tmp(self):
        result = self.run_script("touch /tmp/allowed && ! touch forbidden", mode="read")
        self.assertEqual(result["exit_code"], 0, result)
        self.assertFalse((self.workspace / "forbidden").exists())

    def test_environment_and_approved_tool(self):
        tool = self.root / "approved"
        tool.write_text("#!/bin/sh\nprintf approved-tool")
        tool.chmod(0o700)
        with patch.dict(os.environ, {"PROVIDER_SECRET": "secret-value", "GIT_SSH_COMMAND": "evil"}):
            result = self.run_script("env; approved", tool_paths={"approved": str(tool)})
        self.assertEqual(result["exit_code"], 0, result)
        self.assertNotIn("secret-value", result["output"])
        self.assertNotIn("GIT_SSH", result["output"])
        self.assertIn("approved-tool", result["output"])

    def test_timeout_and_descendants_killed(self):
        result = self.run_script(
            "(sleep 2; touch survived) & echo child-started; wait", timeout_seconds=1
        )
        self.assertTrue(result["timed_out"], result)
        # A second real sandbox waits long enough to catch any escaped writer.
        second = self.run_script("sleep 2; test ! -e survived")
        self.assertEqual(second["exit_code"], 0, second)

    def test_output_bound_and_artifact_correlation(self):
        result = self.run_script("yes overflow | head -c 2000000; printf SANDBOX-TAIL")
        self.assertEqual(result["exit_code"], 0, result)
        self.assertTrue(result["log_truncated"], result)
        log = Path(result["log_path"])
        self.assertLessEqual(log.stat().st_size, OUTPUT_LIMIT + 100)
        self.assertTrue(log.read_text().endswith("SANDBOX-TAIL"))
        self.assertTrue(result["output"].endswith("SANDBOX-TAIL"))
        self.assertEqual(log.stat().st_mode & 0o777, 0o600)
        metadata = json.loads(Path(result["metadata_path"]).read_text())
        self.assertEqual(metadata["log_path"], str(log))
        self.assertEqual(metadata["verification"], "program-captured")
        failure = self.run_script("printf failed; exit 7")
        self.assertEqual(failure["exit_code"], 7)
        self.assertNotEqual(failure["log_path"], result["log_path"])

    def test_worktree_git_metadata_readonly(self):
        subprocess.run(["git", "init", "-q", str(self.source)], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(self.source),
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.invalid",
                "-c",
                "commit.gpgsign=false",
                "commit",
                "-q",
                "--allow-empty",
                "-m",
                "initial",
            ],
            check=True,
        )
        self.workspace.rmdir()
        subprocess.run(
            [
                "git",
                "-C",
                str(self.source),
                "worktree",
                "add",
                "-q",
                "--detach",
                str(self.workspace),
            ],
            check=True,
        )
        result = self.run_script("git branch --list && ! git update-ref refs/heads/evil HEAD")
        self.assertEqual(result["exit_code"], 0, result)


if __name__ == "__main__":
    unittest.main()
