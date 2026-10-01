"""Restricted worker invocations and local, reviewable Git candidates."""

import hashlib
import json
import os
import re
import selectors
import stat
import subprocess
import time
from pathlib import Path

from orchestrator import adapters
from orchestrator.config import ConfigurationError, validate_executor
from orchestrator.models import model_family, normalize_model

MAX_OUTPUT = 4 * 1024 * 1024
MAX_FILES = 1000
MAX_FILE_BYTES = 16 * 1024 * 1024
SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
COMMIT_ID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")


class WorkspaceError(ValueError):
    """Workspace preparation or capture failed closed."""


def _plain_path(value):
    path = Path(os.path.abspath(value))
    for part in (path, *path.parents):
        if part.is_symlink():
            raise WorkspaceError(f"symlink path is forbidden: {part}")
    return path


def _run(arguments, cwd, *, limit=MAX_OUTPUT):
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("GIT_") or key.startswith("GIT_CONFIG_")
    }
    environment.update(GIT_TERMINAL_PROMPT="0", GIT_NO_REPLACE_OBJECTS="1")
    command = [
        "git",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "diff.external=",
        "-c",
        "core.attributesFile=/dev/null",
        *arguments,
    ]
    with subprocess.Popen(
        command, cwd=cwd, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    ) as process:
        selector = selectors.DefaultSelector()
        streams = {process.stdout: bytearray(), process.stderr: bytearray()}
        for stream in streams:
            selector.register(stream, selectors.EVENT_READ)
        deadline = time.monotonic() + 120
        try:
            while selector.get_map():
                if time.monotonic() > deadline:
                    raise WorkspaceError("Git operation exceeded 120 seconds")
                for key, _ in selector.select(0.1):
                    chunk = os.read(key.fileobj.fileno(), 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                    else:
                        streams[key.fileobj].extend(chunk)
                        if sum(map(len, streams.values())) > limit:
                            raise WorkspaceError("Git output exceeds artifact limit")
            process.wait(timeout=max(0.1, deadline - time.monotonic()))
        except BaseException:
            process.kill()
            process.wait()
            raise
        finally:
            selector.close()
        if process.returncode:
            reason = bytes(streams[process.stderr]).decode("utf-8", "replace")[:2000]
            raise WorkspaceError(f"Git {arguments[0]} failed: {reason}")
        return bytes(streams[process.stdout])


def _git(cwd, *arguments):
    return _run(list(arguments), cwd).decode("utf-8", "strict").strip()


def build_worker_command(config, profile, mode, prompt, cwd, output_path, *, project_root=None):
    """Build stdin-only commands; no execution tool is granted to workers."""
    if mode not in ("read", "write"):
        raise adapters.AdapterError("worker mode must be read or write")
    for name, value in (
        ("prompt", prompt),
        ("model", profile.get("model")),
        ("effort", profile.get("effort")),
    ):
        if not isinstance(value, str) or not value.strip() or "\x00" in value:
            raise adapters.AdapterError(f"invalid worker {name}")
    try:
        validate_executor(profile)
    except ConfigurationError as error:
        raise adapters.AdapterError(str(error)) from error
    tools = ["Read", "Glob", "Grep"] + (["Edit", "Write"] if mode == "write" else [])
    harness = profile.get("harness")
    model = normalize_model(profile["model"])
    provider = profile.get("provider")
    anthropic = provider == "anthropic" or "claude" in model.lower() or model_family(model)
    if harness == "pi":
        if anthropic or not provider:
            raise adapters.AdapterError("Pi requires an explicit non-Anthropic provider")
        settings = {
            "adapter": "pi",
            "provider": provider,
            "model": model,
            "effort": profile["effort"],
            "allowed_tools": tools,
        }
        return adapters.build_pi_command(
            config,
            settings,
            prompt,
            cwd,
            output_path,
            project_root=project_root,
            stdin_prompt=True,
            mode=mode,
        )
    if harness != "claude" or not anthropic or provider not in (None, "anthropic"):
        raise adapters.AdapterError(
            "only Claude Anthropic and Pi non-Anthropic workers are supported"
        )
    if profile["effort"] not in {"low", "medium", "high", "xhigh", "max"}:
        raise adapters.AdapterError("unsupported Claude effort")
    command = config["adapters"]["claude"]["command"]
    if (
        not isinstance(command, list)
        or len(command) != 1
        or not isinstance(command[0], str)
        or not command[0]
        or command[0].startswith("-")
    ):
        raise adapters.AdapterError("Claude command must contain one executable only")
    # Do not add the source checkout as an allowed directory for a write worker.
    return [
        *command,
        "-p",
        "--restricted",
        "--output-format",
        "json",
        "--model",
        model,
        "--effort",
        profile["effort"],
        "--permission-mode",
        "dontAsk",
        "--tools",
        ",".join(tools),
        "--allowedTools",
        ",".join(tools),
        "--settings",
        "{}",
        "--setting-sources",
        "",
        "--strict-mcp-config",
        "--disable-slash-commands",
    ]


def _fingerprint(path):
    path = _plain_path(path)
    if not path.is_file() or path.stat().st_size > MAX_OUTPUT:
        raise WorkspaceError(f"invalid metadata file: {path}")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _metadata(path):
    git_file = _plain_path(path / ".git")
    git_dir = _plain_path(_git(path, "rev-parse", "--absolute-git-dir"))
    common_dir = _plain_path(_git(path, "rev-parse", "--path-format=absolute", "--git-common-dir"))
    files = [git_file, git_dir / "commondir", git_dir / "gitdir", common_dir / "config"]
    if (git_dir / "config.worktree").exists():
        files.append(git_dir / "config.worktree")
    return {str(file): _fingerprint(file) for file in files}


def prepare_workspace(home, request, project_root, dependency_commits=()):
    """Never stash, merge, push, or modify source files or its checked-out branch."""
    source = _plain_path(project_root)
    mode = request.get("mode")
    if mode == "read" and not dependency_commits:
        return {"mode": "read", "path": str(source), "source": str(source)}
    if mode not in ("read", "write"):
        raise WorkspaceError("worker mode must be read or write")
    request_id = request.get("id", request.get("request_id"))
    if not isinstance(request_id, str) or not SAFE_ID.fullmatch(request_id):
        raise WorkspaceError("request ID is not a safe path component")
    _plain_path(source / ".git")
    repository = _plain_path(_git(source, "rev-parse", "--show-toplevel"))
    if repository != source:
        raise WorkspaceError("write project must be the Git repository root")
    if _git(source, "status", "--porcelain=v1", "--untracked-files=all"):
        raise WorkspaceError("source checkout must be clean; no automatic stash is permitted")
    commits = list(dependency_commits)
    if len(commits) > 100 or any(
        not isinstance(commit, str) or not COMMIT_ID.fullmatch(commit) for commit in commits
    ):
        raise WorkspaceError("dependencies must be bounded full commit IDs")
    for commit in commits:
        _git(source, "verify-commit", commit)
    # Checkout itself can invoke smudge filters, so check before worktree add.
    configuration = _git(source, "config", "--list", "--name-only")
    if any(
        name.startswith("filter.") or (name.startswith("merge.") and name.endswith(".driver"))
        for name in configuration.splitlines()
    ):
        raise WorkspaceError(
            "workspace repositories with Git filters or merge drivers are unsupported"
        )
    base = _git(source, "rev-parse", "HEAD")
    parent = _plain_path(Path(home) / "data" / "workspaces" / request_id)
    parent.parent.mkdir(parents=True, exist_ok=True)
    parent.mkdir(mode=0o700)  # Exclusive ownership; never reuse another attempt.
    path = parent / "worktree"
    branch = "orchestrator/worker-" + request_id
    _git(source, "worktree", "add", "-b", branch, str(path), base)
    for commit in commits:
        if _git(path, "rev-list", "HEAD.." + commit, "--max-count=1"):
            # Merge in this isolated checkout only, preserving transitive accepted
            # prerequisites and ancestry so shared dependencies are not replayed.
            _git(path, "merge", "--no-ff", "--no-edit", "--gpg-sign", commit)
    workspace = {
        "mode": mode,
        "request_id": request_id,
        "path": str(path),
        "source": str(source),
        "repository": str(repository),
        "base_commit": base,
        "branch": branch,
        "dependency_commits": commits,
        "prepared_commit": _git(path, "rev-parse", "HEAD"),
        "metadata": _metadata(path),
    }
    manifest = parent / "provenance.json"
    with manifest.open("x") as stream:
        json.dump(workspace, stream, sort_keys=True)
    manifest.chmod(0o400)
    return workspace


def capture_workspace(workspace):
    """Capture a bounded candidate and signed local commit, not accepted completion."""
    if workspace.get("mode") == "read":
        return {**workspace, "changed_files": [], "commit": None, "diff_path": None}
    path = _plain_path(workspace["path"])
    manifest = _plain_path(path.parent / "provenance.json")
    if manifest.stat().st_size > MAX_OUTPUT:
        raise WorkspaceError("oversized provenance")
    if json.loads(manifest.read_text()) != workspace:
        raise WorkspaceError("workspace provenance changed")
    # Check saved files BEFORE asking Git to follow the worktree's .git pointer.
    for filename, digest in workspace["metadata"].items():
        if _fingerprint(filename) != digest:
            raise WorkspaceError("workspace Git metadata changed")
    for filename in workspace["metadata"]:
        if Path(filename).name == "commondir":
            administration = Path(filename).parent
            for directory, folders, files in os.walk(administration, followlinks=False):
                for name in folders + files:
                    _plain_path(Path(directory) / name)
    if _metadata(path) != workspace["metadata"]:
        raise WorkspaceError("workspace metadata redirected")
    if _git(path, "rev-parse", "HEAD") != workspace["prepared_commit"]:
        raise WorkspaceError("worker changed Git history")
    if _git(path, "symbolic-ref", "--short", "HEAD") != workspace["branch"]:
        raise WorkspaceError("worker changed workspace branch")
    # Bound all additions, including untracked files, before staging anything.
    names = _run(["ls-files", "-z", "--cached", "--others", "--exclude-standard"], path)
    total_bytes = 0
    for name in set(names.split(b"\0")) - {b""}:
        relative = Path(os.fsdecode(name))
        if relative.is_absolute() or ".." in relative.parts or ".git" in relative.parts:
            raise WorkspaceError("unsafe workspace file path")
        candidate = _plain_path(path / relative)
        if candidate.exists():
            metadata = candidate.stat()
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise WorkspaceError("only ordinary non-linked files are supported")
            total_bytes += metadata.st_size
            if metadata.st_size > MAX_FILE_BYTES or total_bytes > 64 * MAX_FILE_BYTES:
                raise WorkspaceError("workspace files exceed capture limit")
    _git(path, "add", "--all", "--", ".")
    changed = _run(
        [
            "diff",
            "--cached",
            "--no-ext-diff",
            "--no-textconv",
            "--name-only",
            "-z",
            workspace["base_commit"],
            "--",
        ],
        path,
    ).split(b"\0")
    changed_files = [os.fsdecode(name) for name in changed if name]
    if len(changed_files) > MAX_FILES:
        raise WorkspaceError("changed file count exceeds capture limit")
    diff = _run(
        [
            "diff",
            "--cached",
            "--no-ext-diff",
            "--no-textconv",
            "--binary",
            workspace["base_commit"],
            "--",
        ],
        path,
    )
    artifact = _plain_path(path.parent / "changes.patch")
    with artifact.open("xb") as stream:
        stream.write(diff)
    _git(path, "commit", "--allow-empty", "-S", "-m", "Worker candidate " + workspace["request_id"])
    commit = _git(path, "rev-parse", "HEAD")
    _git(path, "verify-commit", commit)
    return {
        **workspace,
        "changed_files": changed_files,
        "commit": commit,
        "result_commit": commit,
        "diff_path": str(artifact),
        "diff_bytes": len(diff),
    }
