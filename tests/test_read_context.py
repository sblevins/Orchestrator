"""Project-local specialist context, exercised with real Git and owned subprocesses."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestrator import runtime
from orchestrator.adapters import build_command, build_pi_command
from orchestrator.config import ConfigurationError, load_config, role_config, validate_config
from orchestrator.pi_tools import FileBroker, PolicyError, launch
from orchestrator.project_settings import configure_project, project_settings
from orchestrator.runtime import _prompt
from orchestrator.store import Store
from orchestrator.worker_execution import build_worker_command
from tests import test_pi_adapter as fixtures


class ContextFixture:
    def setup_context(self, root):
        self.home = root / "supervisor"
        self.project = root / "registered-main"
        self.reference = root / "design-worktree"
        self.project.mkdir()
        self.git("init", "-b", "main")
        # Import synthetic history only into the temporary fixture repository.
        self.git(
            "fast-import",
            "--quiet",
            input=(
                "commit refs/heads/main\nmark :1\n"
                "committer Fixture <fixture@example.invalid> 1700000000 +0000\n"
                "data 7\nfixture\nM 100644 inline README.md\ndata 12\nMain source\n\n"
                "commit refs/heads/design\n"
                "committer Fixture <fixture@example.invalid> 1700000001 +0000\n"
                "data 6\ndesign\nfrom :1\nM 100644 inline docs/design.md\n"
                "data 23\nBRANCH_ONLY_DESIGN_123\n\n"
            ),
        )
        self.git("reset", "--hard", "main")
        self.git("worktree", "add", str(self.reference), "design")
        self.store = Store(self.home)
        self.store.add_project("alpha", str(self.project))
        self.store.open_session("alpha", "test", "alpha")
        self.store.add_project("beta", str(self.reference))
        self.store.open_session("beta", "test", "beta")
        self.read_roots = [{"alias": "design", "path": str(self.reference)}]

    def git(self, *arguments, input=None):
        return subprocess.run(
            ["git", "-C", str(self.project), *arguments],
            input=input,
            text=True,
            capture_output=True,
            check=True,
        ).stdout

    def configure(self, roots=None):
        return configure_project(
            self.store,
            "alpha",
            {"context": {"read_roots": self.read_roots if roots is None else roots}},
        )

    def task(self, role="critic"):
        return self.store.enqueue(
            "alpha", "alpha", role, "Review the branch design", load_config(self.home, "alpha")
        )

    def command(self, task):
        run = self.home / "data" / "runs" / task["id"]
        run.mkdir(parents=True, exist_ok=True)
        return build_command(
            task["config"],
            task["role"],
            _prompt(self.store, task, role_config(task["config"], task["role"])),
            run,
            run / "result.txt",
            project_root=self.project,
            read_roots=task["config"]["context"]["read_roots"],
            stdin_prompt=True,
        )


class ReadContextTests(ContextFixture, unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.setup_context(self.root)

    def test_settings_are_private_canonical_and_captured_for_new_tasks(self):
        old = self.task()
        beta_before = load_config(self.home, "beta")
        global_before = load_config(self.home)
        with patch.dict(os.environ, {"HOME": str(self.root)}):
            saved = self.configure([{"alias": "design", "path": "~/design-worktree"}])
        self.assertEqual(saved["settings"]["context"]["read_roots"], self.read_roots)
        override = json.loads((self.home / "config/projects/alpha.json").read_text())
        self.assertEqual(override["context"]["read_roots"], self.read_roots)
        self.assertEqual(load_config(self.home, "beta"), beta_before)
        self.assertEqual(load_config(self.home), global_before)
        self.assertEqual(self.store.task(old["id"])["config"]["context"]["read_roots"], [])
        captured = self.task()
        self.configure([])
        self.assertEqual(
            self.store.task(captured["id"])["config"]["context"]["read_roots"], self.read_roots
        )
        self.assertEqual(self.task()["config"]["context"]["read_roots"], [])

    def test_missing_reference_only_blocks_specialist_launch(self):
        self.configure()
        captured = self.task()
        config = captured["config"]
        profile = role_config(config, "critic")
        profile["harness"] = "pi"
        beta_before = load_config(self.home, "beta")
        shutil.rmtree(self.reference)
        self.assertEqual(load_config(self.home, "alpha")["context"]["read_roots"], self.read_roots)
        self.assertNotIn("error", project_settings(self.store, "alpha"))
        workspace = self.root / "ordinary-worker"
        workspace.mkdir()
        command = build_worker_command(
            config,
            profile,
            "read",
            "ordinary work",
            workspace,
            self.root / "worker-result",
            project_root=self.project,
        )
        self.assertEqual(json.loads(command[-1])["read_roots"], [])
        updated = configure_project(self.store, "alpha", {"execution": {"max_parallel": 2}})
        self.assertEqual(updated["settings"]["execution"]["max_parallel"], 2)
        self.assertEqual(updated["settings"]["context"]["read_roots"], self.read_roots)
        for task in (captured, self.task()):
            with self.assertRaisesRegex(ValueError, "repair or remove this read root"):
                self.command(task)
        with self.assertRaisesRegex(ValueError, "repair or remove this read root"):
            build_command(
                config,
                "critic",
                "Inspect reference",
                workspace,
                self.root / "specialist-result",
                project_root=self.project,
                read_roots=self.read_roots,
            )
        claimed = self.store.claim_next(1)
        self.assertEqual(claimed["id"], captured["id"])
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "orchestrator.runtime",
                "run-task",
                "--home",
                str(self.home),
                "--task-id",
                claimed["id"],
                "--token",
                claimed["token"],
            ],
            cwd=fixtures.BROKER.parent.parent,
            text=True,
            capture_output=True,
            timeout=15,
            check=False,
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        failed = self.store.task(captured["id"])
        self.assertEqual(failed["state"], "failed")
        self.assertIn("repair or remove this read root", failed["error"])
        with self.assertRaisesRegex(ConfigurationError, "repair or remove this read root"):
            self.configure()
        repaired = configure_project(self.store, "alpha", {"context": {"read_roots": None}})
        self.assertEqual(repaired["settings"]["context"]["read_roots"], [])
        self.command(self.task())
        self.assertEqual(load_config(self.home, "beta"), beta_before)

    def test_retargeted_reference_only_blocks_specialist_launch(self):
        self.configure()
        captured = self.task()
        moved = self.root / "moved-reference"
        self.reference.rename(moved)
        self.reference.symlink_to(moved, target_is_directory=True)
        self.assertEqual(load_config(self.home, "alpha")["context"]["read_roots"], self.read_roots)
        configure_project(self.store, "alpha", {"execution": {"max_parallel": 2}})
        with self.assertRaisesRegex(ValueError, "canonical"):
            self.command(captured)
        self.configure([])
        self.command(self.task())

    def test_primary_root_containing_private_runtime_paths_remains_readable(self):
        run = self.project / "private-run"
        agent = self.project / "private-agent"
        run.mkdir()
        agent.mkdir()
        auth = self.project / "login.json"
        for path in (run / "prompt.txt", agent / "state.txt", auth):
            path.write_text("PRIVATE_SENTINEL")
        (self.reference / ".l.html").write_text("ordinary hidden page")
        options = {
            "cwd": str(run),
            "project_root": str(self.project),
            "mode": "read",
            "auth_path": str(auth),
            "agent_dir": str(agent),
            "tools": ["read", "ls", "find", "grep"],
            "trusted": True,
        }
        requests = [
            {"name": "read", "arguments": {"path": "README.md"}},
            {"name": "read", "arguments": {"path": str(run / "prompt.txt")}},
            {"name": "read", "arguments": {"path": str(auth)}},
            {"name": "read", "arguments": {"path": str(agent / "state.txt")}},
            {"name": "ls", "arguments": {"path": "."}},
            {"name": "find", "arguments": {"path": ".", "pattern": "**/*"}},
            {"name": "grep", "arguments": {"path": ".", "pattern": "PRIVATE_SENTINEL"}},
        ]
        for roots in ([], self.read_roots):
            options["read_roots"] = roots
            process = subprocess.run(
                [sys.executable, "-I", str(fixtures.BROKER), "serve", json.dumps(options)],
                input="".join(json.dumps(request) + "\n" for request in requests),
                text=True,
                capture_output=True,
                check=True,
            )
            results = [json.loads(line) for line in process.stdout.splitlines()]
            self.assertEqual(
                [result["ok"] for result in results], [True, False, False, False, True, True, True]
            )
            self.assertIn("Main source", results[0]["text"])
            for result in results[4:]:
                for private_name in (
                    "private-run",
                    "private-agent",
                    "login.json",
                    "PRIVATE_SENTINEL",
                ):
                    self.assertNotIn(private_name, result["text"])
        broker = FileBroker(options)
        self.addCleanup(broker.close)
        hidden_page = self.reference / ".l.html"
        self.assertEqual(broker.execute("read", {"path": str(hidden_page)}), "ordinary hidden page")
        self.assertIn(".l.html", broker.execute("ls", {"path": str(self.reference)}))
        self.assertIn(
            ".l.html", broker.execute("find", {"path": str(self.reference), "pattern": "*.html"})
        )
        self.assertIn(
            "ordinary hidden page",
            broker.execute("grep", {"path": str(self.reference), "pattern": "ordinary"}),
        )
        with self.assertRaises(PolicyError), broker.directory(self.project, ("private-run",)):
            pass
        for root in (run, self.project, agent):
            with self.assertRaises(PolicyError):
                FileBroker({**options, "read_roots": [{"alias": "private", "path": str(root)}]})

    def test_self_hosted_runtime_hides_supervisor_private_state_from_broker(self):
        home = self.root / "orchestrator-checkout"
        (home / "src").mkdir(parents=True)
        (home / "config").mkdir()
        (home / "src/app.py").write_text("PUBLIC_SOURCE = True\n")
        (home / "config/default.toml").write_text("# PUBLIC_DEFAULT_CONFIG\n")
        store = Store(home)
        store.add_project("self", str(home))
        store.open_session("self", "test", "self")
        other_root = self.root / "other-project"
        other_root.mkdir()
        store.add_project("other", str(other_root))
        store.open_session("other", "test", "other")
        configure_project(store, "other", {"execution": {"max_parallel": 3}})
        sibling_run = home / "data/runs/sibling-task/sibling-token"
        sibling_run.mkdir(parents=True)
        (sibling_run / "prompt.txt").write_text("PRIVATE_SENTINEL sibling prompt")
        notes = home / "data/projects/other/notes"
        notes.mkdir(parents=True, exist_ok=True)
        (notes / "BRIEF").write_text("PRIVATE_SENTINEL other brief")
        (home / "config/workflows").mkdir()
        (home / "config/workflows/private.json").write_text('{"PRIVATE_SENTINEL": true}')
        (home / "config/crew-dispatch.json").write_text('{"PRIVATE_SENTINEL": true}')
        (home / "config/local.toml").write_text("# PRIVATE_SENTINEL local\n")
        other_settings = (home / "config/projects/other.json").read_text()
        (home / "config/projects/other.json").write_text(
            other_settings.replace("{", '{"_comment": "PRIVATE_SENTINEL",', 1)
        )
        reference = self.root / "secrets/tokens/reference"
        (reference / "docs").mkdir(parents=True)
        (reference / "docs/design.md").write_text("REFERENCE_SOURCE design\n")
        (reference / ".env").write_text("CREDENTIAL_SENTINEL=1\n")
        (reference / "credentials.json").write_text('{"CREDENTIAL_SENTINEL": 1}')
        subprocess.run(["git", "init", "-q", str(reference)], check=True)
        (reference / ".git/description").write_text("CREDENTIAL_SENTINEL metadata\n")
        claude_worktree = self.root / "main-repo/.claude/worktrees/design"
        claude_worktree.mkdir(parents=True)
        (claude_worktree / "plan.md").write_text("CLAUDE_WORKTREE_SOURCE\n")
        roots = [
            {"alias": "reference", "path": str(reference)},
            {"alias": "claude-worktree", "path": str(claude_worktree)},
        ]
        saved = configure_project(store, "self", {"context": {"read_roots": roots}})
        self.assertEqual(saved["settings"]["context"]["read_roots"], roots)
        queued = store.enqueue(
            "self", "self", "critic", "Review self-hosted source", load_config(home, "self")
        )
        claimed = store.claim_next(1)
        self.assertEqual(claimed["id"], queued["id"])
        launched = []
        real_popen = subprocess.Popen

        def capture(command, *arguments, **keywords):
            launched.append(command)
            return real_popen([sys.executable, "-c", "raise SystemExit(1)"], *arguments, **keywords)

        with patch.object(runtime.subprocess, "Popen", side_effect=capture):
            self.assertEqual(runtime.run_task(home, claimed["id"], claimed["token"]), 1)
        self.assertEqual(len(launched), 1)
        options = json.loads(launched[0][-1])
        requests = [
            {"name": "read", "arguments": {"path": "src/app.py"}},
            {"name": "read", "arguments": {"path": "config/default.toml"}},
            {"name": "read", "arguments": {"path": str(reference / "docs/design.md")}},
            {"name": "read", "arguments": {"path": str(claude_worktree / "plan.md")}},
            {"name": "ls", "arguments": {"path": "."}},
            {"name": "ls", "arguments": {"path": "config"}},
            {"name": "find", "arguments": {"path": ".", "pattern": "**/*"}},
            {"name": "grep", "arguments": {"path": ".", "pattern": "SENTINEL"}},
            {"name": "find", "arguments": {"path": str(reference), "pattern": "**/*"}},
            {"name": "grep", "arguments": {"path": str(reference), "pattern": "SENTINEL"}},
            {"name": "read", "arguments": {"path": str(sibling_run / "prompt.txt")}},
            {"name": "read", "arguments": {"path": "data/projects/other/notes/BRIEF"}},
            {"name": "read", "arguments": {"path": "config/projects/other.json"}},
            {"name": "read", "arguments": {"path": "config/local.toml"}},
            {"name": "read", "arguments": {"path": "config/workflows/private.json"}},
            {"name": "read", "arguments": {"path": "config/crew-dispatch.json"}},
            {"name": "ls", "arguments": {"path": "data/runs"}},
            {"name": "grep", "arguments": {"path": "config/projects", "pattern": "SENTINEL"}},
            {"name": "read", "arguments": {"path": str(reference / ".env")}},
            {"name": "read", "arguments": {"path": str(reference / "credentials.json")}},
            {"name": "read", "arguments": {"path": str(reference / ".git/description")}},
        ]
        for trusted in (False, True):
            process = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    str(fixtures.BROKER),
                    "serve",
                    json.dumps({**options, "trusted": trusted}),
                ],
                input="".join(json.dumps(request) + "\n" for request in requests),
                text=True,
                capture_output=True,
                check=True,
            )
            results = [json.loads(line) for line in process.stdout.splitlines()]
            self.assertEqual([result["ok"] for result in results], [True] * 10 + [False] * 11)
            self.assertIn("PUBLIC_SOURCE", results[0]["text"])
            self.assertIn("PUBLIC_DEFAULT_CONFIG", results[1]["text"])
            self.assertIn("REFERENCE_SOURCE", results[2]["text"])
            self.assertIn("CLAUDE_WORKTREE_SOURCE", results[3]["text"])
            self.assertIn("default.toml", results[5]["text"])
            self.assertIn("src/app.py", results[6]["text"])
            self.assertIn("docs/design.md", results[8]["text"])
            for result in results:
                for private_name in (
                    "SENTINEL",
                    "data",
                    "local.toml",
                    "projects",
                    "workflows",
                    "crew-dispatch",
                    "sibling",
                    "credentials",
                    ".env",
                ):
                    self.assertNotIn(private_name, result.get("text", ""))
        for changes in (
            {"mode": "write", "cwd": str(self.root / "worker-worktree")},
            {"worker_context": {"task_id": "malicious"}},
        ):
            with self.assertRaisesRegex(PolicyError, "Private supervisor paths"):
                FileBroker({**options, "read_roots": [], **changes})

    def test_claude_args_and_specialist_prompts_discover_roots(self):
        self.configure()
        for role in ("planner", "critic", "monitor"):
            task = self.task(role)
            prompt = _prompt(self.store, task, role_config(task["config"], role))
            self.assertIn(json.dumps(self.read_roots[0], sort_keys=True), prompt)
            self.assertIn("absolute", prompt)
            self.assertIn("mutable", prompt)
            command = self.command(task)
            if role != "critic":
                directories = [
                    command[index + 1]
                    for index, value in enumerate(command)
                    if value == "--add-dir"
                ]
                self.assertEqual(directories, [str(self.project), str(self.reference)])
                self.assertEqual(command[command.index("--tools") + 1], "Read,Glob,Grep")
            else:
                self.assertEqual(json.loads(command[-1])["read_roots"], self.read_roots)

    def test_multiple_named_roots_are_disclosed_and_readable(self):
        notes = self.root / "reference-notes"
        notes.mkdir()
        (notes / "notes.md").write_text("Reference notes")
        self.read_roots.append({"alias": "notes", "path": str(notes)})
        self.configure()
        task = self.task()
        broker = FileBroker(json.loads(self.command(task)[-1]))
        self.addCleanup(broker.close)
        self.assertEqual(
            broker.execute("read", {"path": str(notes / "notes.md")}), "Reference notes"
        )
        self.assertIn(
            "BRANCH_ONLY", broker.execute("read", {"path": str(self.reference / "docs/design.md")})
        )
        command = self.command(self.task("planner"))
        directories = [
            command[index + 1] for index, value in enumerate(command) if value == "--add-dir"
        ]
        self.assertEqual(directories, [str(self.project), str(self.reference), str(notes)])
        prompt = _prompt(self.store, task, role_config(task["config"], "critic"))
        for entry in self.read_roots:
            self.assertIn(json.dumps(entry, sort_keys=True), prompt)

    def test_broker_child_default_denial_configured_reads_and_no_writes(self):
        default_options = json.loads(self.command(self.task())[-1])
        self.configure()
        options = json.loads(self.command(self.task())[-1])
        requests = [
            {"name": "read", "arguments": {"path": "README.md"}},
            {"name": "read", "arguments": {"path": str(self.reference / "docs/design.md")}},
            {"name": "ls", "arguments": {"path": str(self.reference / "docs")}},
            {"name": "find", "arguments": {"path": str(self.reference), "pattern": "**/*.md"}},
            {"name": "grep", "arguments": {"path": str(self.reference), "pattern": "BRANCH_ONLY"}},
        ]
        for current, expected in ((default_options, False), (options, True)):
            process = subprocess.run(
                [sys.executable, "-I", str(fixtures.BROKER), "serve", json.dumps(current)],
                input="".join(json.dumps(request) + "\n" for request in requests),
                capture_output=True,
                text=True,
                check=True,
            )
            results = [json.loads(line) for line in process.stdout.splitlines()]
            self.assertEqual(len(results), len(requests))
            self.assertTrue(results[0]["ok"])
            self.assertEqual([result["ok"] for result in results[1:]], [expected] * 4)
            if expected:
                self.assertIn("BRANCH_ONLY_DESIGN_123", results[1]["text"])
            else:
                self.assertTrue(
                    all("outside allowed roots" in result["error"] for result in results[1:])
                )
        broker = FileBroker(options)
        self.addCleanup(broker.close)
        for name, arguments in (
            ("write", {"path": str(self.reference / "docs/design.md"), "content": "changed"}),
            (
                "edit",
                {
                    "path": str(self.reference / "docs/design.md"),
                    "edits": [{"oldText": "BRANCH", "newText": "changed"}],
                },
            ),
        ):
            with self.assertRaises(PolicyError):
                broker.execute(name, arguments)
        self.assertFalse((self.project / "docs/design.md").exists())
        self.assertEqual(
            (self.reference / "docs/design.md").read_text(), "BRANCH_ONLY_DESIGN_123\n"
        )
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_broker_child_rejects_forged_write_tools_without_mutation(self):
        self.configure()
        options = json.loads(self.command(self.task())[-1])
        options["tool_names"] += ["write", "edit"]
        target = self.reference / "docs/design.md"
        original = target.read_text()
        requests = [
            {"name": "write", "arguments": {"path": str(target), "content": "changed"}},
            {
                "name": "edit",
                "arguments": {
                    "path": str(target),
                    "edits": [{"oldText": original, "newText": "changed"}],
                },
            },
            {"name": "read", "arguments": {"path": str(target)}},
        ]
        process = subprocess.run(
            [sys.executable, "-I", str(fixtures.BROKER), "serve", json.dumps(options)],
            input="".join(json.dumps(request) + "\n" for request in requests),
            text=True,
            capture_output=True,
            check=True,
        )
        results = [json.loads(line) for line in process.stdout.splitlines()]
        self.assertEqual([result["ok"] for result in results], [False, False, True])
        self.assertEqual(target.read_text(), original)
        self.assertEqual(results[-1]["text"], original)

    def test_credentials_escapes_private_runs_and_worker_authority(self):
        self.configure()
        options = json.loads(self.command(self.task())[-1])
        (self.reference / "credentials.json").write_text("secret")
        outside = self.root / "outside.txt"
        outside.write_text("private")
        (self.reference / "escape.md").symlink_to(outside)
        broker = FileBroker(options)
        self.addCleanup(broker.close)
        for target in (
            self.reference / "credentials.json",
            self.reference / "escape.md",
            Path(options["cwd"]),
        ):
            with self.assertRaises(PolicyError):
                broker.execute("read", {"path": str(target)})
        for changes in ({"mode": "write"}, {"worker_context": {"task_id": "malicious"}}):
            with self.assertRaises(PolicyError):
                FileBroker({**options, **changes})
        with self.assertRaises(PolicyError):
            FileBroker({**options, "read_roots": [{"alias": "private", "path": options["cwd"]}]})

    def test_real_runtime_uses_captured_context_after_settings_change(self):
        self.configure()
        harness = self.root / "claude-fixture"
        harness.write_text(
            f"#!{sys.executable}\n"
            "import json, pathlib, sys\n"
            "directories = [sys.argv[index + 1] for index, value in enumerate(sys.argv) if value == '--add-dir']\n"
            f"assert directories == {[str(self.project), str(self.reference)]!r}\n"
            "assert 'BRANCH_ONLY' in (pathlib.Path(directories[1]) / 'docs/design.md').read_text()\n"
            "prompt = sys.stdin.read()\n"
            f"assert {str(self.reference)!r} in prompt\n"
            "print(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False, 'result': '{}', 'session_id': 'fixture'}))\n"
        )
        harness.chmod(0o700)
        config = load_config(self.home, "alpha")
        config["adapters"]["claude"]["command"] = [str(harness)]
        queued = self.store.enqueue("alpha", "alpha", "planner", "Inspect design", config)
        self.configure([])
        claimed = self.store.claim_next(1)
        self.assertEqual(claimed["id"], queued["id"])
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "orchestrator.runtime",
                "run-task",
                "--home",
                str(self.home),
                "--task-id",
                claimed["id"],
                "--token",
                claimed["token"],
            ],
            cwd=fixtures.BROKER.parent.parent,
            text=True,
            capture_output=True,
            timeout=15,
            check=False,
        )
        finished = self.store.task(claimed["id"])
        self.assertEqual(result.returncode, 0, result.stderr + str(finished["error"]))
        self.assertEqual(finished["state"], "succeeded")
        self.assertEqual(finished["config"]["context"]["read_roots"], self.read_roots)

    def test_worker_builder_does_not_inherit_specialist_roots(self):
        self.configure()
        config = load_config(self.home, "alpha")
        for mode in ("read", "write"):
            command = build_pi_command(
                config,
                role_config(config, "critic"),
                "worker task",
                self.reference,
                self.root / "output",
                project_root=self.project,
                mode=mode,
            )
            self.assertEqual(json.loads(command[-1])["read_roots"], [])
        workspace = self.root / "worker-workspace"
        workspace.mkdir()
        for role in ("planner", "critic"):
            profile = role_config(config, role)
            profile["harness"] = profile["adapter"]
            for mode in ("read", "write"):
                command = build_worker_command(
                    config,
                    profile,
                    mode,
                    "worker task",
                    workspace,
                    self.root / "result",
                    project_root=self.project,
                )
                self.assertNotIn(str(self.reference), json.dumps(command))
                if role == "critic":
                    self.assertEqual(json.loads(command[-1])["read_roots"], [])
        worker = self.task()
        worker["role"] = "worker"
        prompt = _prompt(self.store, worker, {"prompt_path": "roles/worker.md"})
        self.assertNotIn(str(self.reference), prompt)

    def test_launcher_excludes_installation_inside_any_read_root(self):
        self.configure()
        options = json.loads(self.command(self.task())[-1])
        executable = self.reference / "pi"
        executable.write_text("#!/bin/sh\nexit 99\n")
        executable.chmod(0o700)
        options["executable"] = str(executable)
        with (
            patch.dict(os.environ, {"PI_CODING_AGENT_DIR": str(self.root / "isolated-auth")}),
            self.assertRaisesRegex(PolicyError, "installation must be outside"),
        ):
            launch(options)

    def test_control_auth_roots_and_noncanonical_saved_paths_are_rejected(self):
        credential_directory = self.reference / ".ssh"
        credential_directory.mkdir()
        auth_directory = self.root / "custom-login"
        auth_directory.mkdir()
        for path in (
            credential_directory,
            self.reference / "docs/..",
            self.project / ".git",
            self.project / ".git/refs",
        ):
            config = load_config(self.home)
            config["context"]["read_roots"] = [{"alias": "invalid", "path": str(path)}]
            with self.assertRaises(ConfigurationError):
                validate_config(config)
        for variable in ("PI_CODING_AGENT_DIR", "CLAUDE_CONFIG_DIR", "GH_CONFIG_DIR"):
            with (
                patch.dict(os.environ, {variable: str(auth_directory)}),
                self.assertRaises(ConfigurationError),
            ):
                self.configure([{"alias": "login", "path": str(auth_directory)}])
        self.configure()
        options = json.loads(self.command(self.task())[-1])
        for private in ("auth_path", "agent_dir"):
            with self.assertRaises(PolicyError):
                FileBroker({**options, private: str(self.reference / "private-auth")})
        roots = [{"alias": f"directory{index}", "path": str(self.reference)} for index in range(17)]
        with self.assertRaisesRegex(ConfigurationError, "at most 16"):
            self.configure(roots)
        renamed = self.root / "moved-design"
        self.reference.rename(renamed)
        self.reference.symlink_to(renamed, target_is_directory=True)
        with self.assertRaises(PolicyError):
            FileBroker(options)
        repaired = configure_project(self.store, "alpha", {"context": {"read_roots": None}})
        self.assertEqual(repaired["settings"]["context"]["read_roots"], [])

    def test_invalid_shapes_paths_aliases_and_supervisor_roots(self):
        (self.home / "config").mkdir(exist_ok=True)
        for roots in (
            {},
            ["wrong"],
            [{"alias": "design"}],
            [{"alias": "design", "path": str(self.reference), "write": True}],
            [{"alias": "bad alias", "path": str(self.reference)}],
            self.read_roots * 2,
            [{"alias": "design", "path": "relative"}],
            [{"alias": "design", "path": str(self.root / "missing")}],
            [{"alias": "design", "path": str(self.reference / "docs/design.md")}],
            [{"alias": "all", "path": "/"}],
            [{"alias": "private", "path": str(self.home / "data")}],
            [{"alias": "private", "path": str(self.home)}],
            [{"alias": "private", "path": str(self.home / "config")}],
        ):
            with self.subTest(roots=roots), self.assertRaises(ConfigurationError):
                self.configure(roots)
        config = load_config(self.home)
        config["context"] = {"read_roots": [], "write_roots": []}
        with self.assertRaises(ConfigurationError):
            validate_config(config)


class ReadContextInstalledTests(ContextFixture, unittest.TestCase):
    setUpClass = classmethod(fixtures.PiInstalledE2ETests.setUpClass.__func__)
    server = fixtures.PiInstalledE2ETests.server
    run_bridge = fixtures.PiInstalledE2ETests.run_bridge

    def setUp(self):
        fixtures.PiInstalledE2ETests.setUp(self)
        self.setup_context(self.root)

    def run_operations(self, operations):
        task = self.task()
        generated = json.loads(self.command(task)[-1])
        self.options.update(generated)
        self.options.update(provider="fixture", model="fixture-model", effort="off")
        self.cwd = Path(generated["cwd"])
        responses = [
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": str(index),
                        "type": "function",
                        "function": {"name": name, "arguments": json.dumps(arguments)},
                    }
                ]
            }
            for index, (name, arguments) in enumerate(operations)
        ] + [{"content": "branch design reviewed"}]
        with self.server(responses) as models:
            result = self.run_bridge(models)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(
            fixtures.parse_result("pi", result.stdout, 0)["text"], "branch design reviewed"
        )
        events = [json.loads(line) for line in result.stdout.splitlines()]
        return [event["isError"] for event in events if event["type"] == "tool_execution_end"]

    def test_captured_context_through_installed_sdk_and_owned_broker(self):
        self.configure()
        (self.reference / "credentials.json").write_text("credential-sentinel")
        (self.reference / "escape.md").symlink_to(self.auth)
        results = self.run_operations(
            [
                ("read", {"path": "README.md"}),
                ("read", {"path": str(self.reference / "docs/design.md")}),
                ("ls", {"path": str(self.reference / "docs")}),
                ("find", {"path": str(self.reference), "pattern": "**/*.md"}),
                ("grep", {"path": str(self.reference), "pattern": "BRANCH_ONLY"}),
                ("read", {"path": str(self.auth)}),
                ("read", {"path": str(self.reference / "credentials.json")}),
                ("read", {"path": str(self.reference / "escape.md")}),
                ("read", {"path": "README.md"}),
            ]
        )
        self.assertEqual(results, [False] * 5 + [True] * 3 + [False])
        self.assertNotIn("credential-sentinel", json.dumps(self.requests))
        self.assertIn("BRANCH_ONLY_DESIGN_123", json.dumps(self.requests[2]))
        self.assertIn(str(self.reference), json.dumps(self.requests[0]))
        self.assertIn("design", json.dumps(self.requests[0]))
        self.assertEqual(
            (self.reference / "docs/design.md").read_text(), "BRANCH_ONLY_DESIGN_123\n"
        )
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_empty_context_denies_sibling_and_recovers_through_installed_sdk(self):
        results = self.run_operations(
            [
                ("read", {"path": str(self.reference / "docs/design.md")}),
                ("ls", {"path": str(self.reference)}),
                ("find", {"path": str(self.reference), "pattern": "**/*.md"}),
                ("grep", {"path": str(self.reference), "pattern": "BRANCH_ONLY"}),
                ("read", {"path": "README.md"}),
            ]
        )
        self.assertEqual(results, [True] * 4 + [False])
        self.assertIn("outside allowed roots", json.dumps(self.requests[-1]))
        self.assertNotIn("BRANCH_ONLY_DESIGN_123", json.dumps(self.requests))
