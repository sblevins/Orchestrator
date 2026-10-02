"""Restricted worker invocations and local, reviewable Git candidates."""

import hashlib
import json
import os
import re
import selectors
import stat
import subprocess
import sys
import time
from pathlib import Path

from orchestrator import adapters
from orchestrator.config import ConfigurationError, validate_executor
from orchestrator.models import model_family, normalize_model
from orchestrator.pi_tools import forbidden, plain_directory

MAX_OUTPUT = 4 * 1024 * 1024
MAX_PATCH_BYTES = 64 * 1024 * 1024
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
    environment.update(GIT_TERMINAL_PROMPT="0", GIT_NO_REPLACE_OBJECTS="1", GIT_OPTIONAL_LOCKS="0")
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


def build_worker_command(
    config, profile, mode, prompt, cwd, output_path, *, project_root=None, worker_context=None
):
    """Build stdin-only commands with explicitly granted, task-bound tools."""
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
        validate_executor(profile, allow_effort_selector=True)
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
            worker_context=worker_context,
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
    options = (
        adapters.worker_tool_options(config, mode, cwd, project_root, output_path, worker_context)
        if worker_context is not None
        else {"tool_names": []}
    )
    mcp_tools = ["mcp__orchestrator_worker__" + name for name in options["tool_names"]]
    mcp_arguments = []
    if mcp_tools:
        mcp_arguments = [
            "--mcp-config",
            json.dumps(
                {
                    "mcpServers": {
                        "orchestrator_worker": {
                            "command": sys.executable,
                            "args": [
                                "-I",
                                str(Path(__file__).with_name("worker_mcp.py")),
                                json.dumps(options),
                            ],
                        }
                    }
                }
            ),
        ]
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
        ",".join(tools + mcp_tools),
        "--settings",
        "{}",
        "--setting-sources",
        "",
        "--strict-mcp-config",
        "--disable-slash-commands",
        *mcp_arguments,
    ]


def _fingerprint(path):
    path = _plain_path(path)
    if not path.is_file() or path.stat().st_size > MAX_OUTPUT:
        raise WorkspaceError(f"invalid metadata file: {path}")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _metadata_fingerprint(path, *, trusted=False):
    digest = _fingerprint(path)
    if not trusted or Path(path).name not in {"config", "config.worktree"}:
        return digest
    # Init/sync legitimately update submodule registration in the shared config.
    # Retain every other setting, especially includes and ownership/redirection.
    records = _run(
        ["config", "--no-includes", "--file", str(path), "--null", "--list"],
        Path(path).parent,
    ).split(b"\0")
    protected = [record for record in records if not record.startswith(b"submodule.")]
    return hashlib.sha256(b"\0".join(protected)).hexdigest()


def _metadata(path, *, trusted=False):
    git_file = _plain_path(path / ".git")
    git_dir = _plain_path(_git(path, "rev-parse", "--absolute-git-dir"))
    common_dir = _plain_path(_git(path, "rev-parse", "--path-format=absolute", "--git-common-dir"))
    files = [git_file, git_dir / "commondir", git_dir / "gitdir", common_dir / "config"]
    if (git_dir / "config.worktree").exists():
        files.append(git_dir / "config.worktree")
    return {str(file): _metadata_fingerprint(file, trusted=trusted) for file in files}


def _gitlinks(path):
    links = {}
    for record in _run(["ls-files", "--stage", "-z"], path).split(b"\0"):
        if not record:
            continue
        entry, name = record.split(b"\t", 1)
        mode, commit, stage = entry.split()
        if mode == b"160000" and stage == b"0":
            links[name] = commit.decode("ascii")
    return links


def _check_submodule(path, relative, commit, *, snapshot=False):
    candidate = _plain_path(path / relative)
    pointer = _plain_path(candidate / ".git")
    if not pointer.exists():
        if any(candidate.iterdir()):
            raise WorkspaceError(
                f"Submodule {relative} is not initialized; initialize it before capture"
            )
        return
    if _plain_path(_git(candidate, "rev-parse", "--show-toplevel")) != candidate:
        raise WorkspaceError(f"Submodule {relative} has redirected metadata")
    # A parent commit records only the submodule commit, never its dirty files.
    if _run(
        ["status", "--porcelain=v1", "--untracked-files=all", "--ignore-submodules=none", "-z"],
        candidate,
    ):
        raise WorkspaceError(
            f"Submodule {relative} has uncommitted changes; commit or remove them "
            "inside the submodule before preparing or capturing the workspace"
        )
    if snapshot and _git(candidate, "rev-parse", "HEAD") != commit:
        raise WorkspaceError(
            f"Submodule {relative} has a changed checkout; commit its updated pointer in the "
            "parent repository before preparing the workspace"
        )


def _project_symlink(path, root):
    """Preserve relative project links without pointing a worker back at its source."""
    target = os.readlink(path)
    if os.path.isabs(target):
        raise WorkspaceError(
            "Project symlinks must be relative to remain inside the worker checkout"
        )
    try:
        resolved = path.resolve(strict=False)
        relative = resolved.relative_to(root)
    except (OSError, RuntimeError, ValueError) as error:
        raise WorkspaceError("Project symlink leaves the checkout or contains a loop") from error
    if any(forbidden(part, write=True, trusted=True) for part in relative.parts):
        raise WorkspaceError("Project symlink points to excluded credential or control files")
    return target


def _snapshot_source(source, destination):
    """Copy bounded ordinary project files using no-follow directory descriptors.

    Git supplies tracked and non-ignored untracked names; the broker's filters
    exclude credential and control files. The source index and ref stay untouched.
    """
    names = set(
        _run(["ls-files", "-z", "--cached", "--others", "--exclude-standard"], source).split(b"\0")
    ) - {b""}
    tracked = set(_run(["ls-files", "-z", "--cached"], destination).split(b"\0")) - {b""}
    # Clear the owned checkout first so file-to-directory changes also work.
    for name in sorted(tracked, reverse=True):
        relative = Path(os.fsdecode(name))
        if relative.is_absolute() or ".." in relative.parts or ".git" in relative.parts:
            raise WorkspaceError("unsafe snapshot file path")
        target = _plain_path(destination / relative.parent) / relative.name
        if target.is_symlink() or target.is_file():
            target.unlink()
            directory = target.parent
            while directory != destination:
                try:
                    directory.rmdir()
                except OSError:
                    break
                directory = directory.parent
    links = _gitlinks(source)
    destination_links = _gitlinks(destination)
    if links != destination_links:
        raise WorkspaceError(
            "Changed submodule commits cannot be snapshotted; commit the parent submodule changes first"
        )
    total_bytes = 0
    for name in sorted(names):
        relative = Path(os.fsdecode(name))
        if relative.is_absolute() or ".." in relative.parts or ".git" in relative.parts:
            raise WorkspaceError("unsafe snapshot file path")
        target = _plain_path(destination / relative)
        if any(forbidden(part, write=True, trusted=True) for part in relative.parts):
            continue
        source_file = _plain_path(source / relative.parent) / relative.name
        if source_file.is_symlink():
            link = _project_symlink(source_file, source)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.symlink_to(link)
            continue
        _plain_path(source_file)
        try:
            directory = plain_directory(source / relative.parent)
            try:
                descriptor = os.open(
                    relative.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
                )
            finally:
                os.close(directory)
        except (FileNotFoundError, NotADirectoryError):
            continue
        except OSError as error:
            raise WorkspaceError("snapshot contains an unsafe path or symlink") from error
        metadata = os.fstat(descriptor)
        if stat.S_ISDIR(metadata.st_mode) and name in tracked:
            os.close(descriptor)
            if name in links:
                _check_submodule(source, relative, links[name], snapshot=True)
            # Ordinary tracked files replaced by directories are copied through
            # their separately enumerated children, not as directory entries.
            continue
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            os.close(descriptor)
            raise WorkspaceError("snapshot supports only ordinary non-linked files")
        with os.fdopen(descriptor, "rb") as stream:
            total_bytes += metadata.st_size
            if metadata.st_size > MAX_FILE_BYTES or total_bytes > 64 * MAX_FILE_BYTES:
                raise WorkspaceError("snapshot files exceed capture limit")
            content = stream.read(MAX_FILE_BYTES + 1)
            if len(content) > MAX_FILE_BYTES:
                raise WorkspaceError("snapshot file exceeds capture limit")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        target.chmod(0o755 if metadata.st_mode & stat.S_IXUSR else 0o644)


def prepare_workspace(
    home,
    request,
    project_root,
    dependency_commits=(),
    *,
    baseline_commit=None,
    base_ref="HEAD",
    isolated=False,
    trusted=False,
):
    """Never stash, merge, push, or modify source files or its checked-out branch."""
    source = _plain_path(project_root)
    mode = request.get("mode")
    if (
        mode == "read"
        and not dependency_commits
        and baseline_commit is None
        and not isolated
        and base_ref == "HEAD"
    ):
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
    # Local onboarding metadata need not be committed or hidden with Git excludes.
    # Validate even when Git omits special files (for example, FIFOs) from status.
    policy = _plain_path(source / ".orchestrator" / "crew-dispatch.json")
    try:
        policy_metadata = policy.lstat()
    except FileNotFoundError:
        policy_metadata = None
    if policy_metadata is not None and (
        not stat.S_ISREG(policy_metadata.st_mode) or policy_metadata.st_nlink != 1
    ):
        raise WorkspaceError("routing policy must be an ordinary non-linked file")
    # Restricted workers require committed source except routing metadata.
    # Authorized trusted HEAD workers snapshot source into their owned checkout.
    # Match the complete NUL-delimited output for the restricted policy exception.
    status = _run(["status", "--porcelain=v1", "--untracked-files=all", "-z"], source)
    metadata_only = status[2:] == b" .orchestrator/crew-dispatch.json\0" and status[:2] in {
        b"??",
        b" M",
        b"M ",
        b"MM",
        b"A ",
        b"AM",
        b" D",
        b"D ",
        b"AD",
    }
    if status and not metadata_only and not trusted:
        raise WorkspaceError(
            "Source code has uncommitted changes; commit those changes before creating "
            "an isolated worker snapshot. Routing policy edits do not need a commit."
        )
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
    if baseline_commit is not None and (
        not isinstance(baseline_commit, str) or not COMMIT_ID.fullmatch(baseline_commit)
    ):
        raise WorkspaceError("Team baseline must be a full Git commit ID")
    if (
        not isinstance(base_ref, str)
        or not base_ref
        or base_ref.startswith("-")
        or "\x00" in base_ref
    ):
        raise WorkspaceError("base_ref must name an existing Git ref or commit")
    snapshot_source = trusted and baseline_commit is None and base_ref == "HEAD"
    unborn = False
    try:
        base = baseline_commit or _git(
            source, "rev-parse", "--verify", "--end-of-options", base_ref + "^{commit}"
        )
    except WorkspaceError:
        if not snapshot_source:
            raise
        # Only an actually unborn branch is eligible, not a broken existing ref.
        head_ref = _git(source, "symbolic-ref", "HEAD")
        if _git(source, "for-each-ref", "--format=%(refname)", head_ref):
            raise WorkspaceError("source HEAD does not resolve to a commit")
        unborn = True
        tree = _git(source, "hash-object", "-w", "-t", "tree", os.devnull)
        base = _git(source, "commit-tree", tree, "-S", "-m", "Worker empty snapshot baseline")
    parent = _plain_path(Path(home) / "data" / "workspaces" / request_id)
    parent.parent.mkdir(parents=True, exist_ok=True)
    parent.mkdir(mode=0o700)  # Exclusive ownership; never reuse another attempt.
    path = parent / "worktree"
    branch = "orchestrator/worker-" + request_id
    _git(source, "worktree", "add", "-b", branch, str(path), base)
    if snapshot_source:
        _snapshot_source(source, path)
        _git(path, "add", "--all", "--", ".")
        _git(path, "commit", "--allow-empty", "-S", "-m", "Worker source snapshot " + request_id)
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
        "base_ref": base_ref,
        "trusted": trusted,
        "source_changes": "snapshotted" if snapshot_source else "excluded" if status else "none",
        "source_unborn": unborn,
        "branch": branch,
        "dependency_commits": commits,
        "prepared_commit": _git(path, "rev-parse", "HEAD"),
        "metadata": _metadata(path, trusted=trusted),
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
        if _metadata_fingerprint(filename, trusted=workspace.get("trusted", False)) != digest:
            raise WorkspaceError("workspace Git metadata changed")
    for filename in workspace["metadata"]:
        if Path(filename).name == "commondir":
            administration = Path(filename).parent
            for directory, folders, files in os.walk(administration, followlinks=False):
                for name in folders + files:
                    _plain_path(Path(directory) / name)
    if _metadata(path, trusted=workspace.get("trusted", False)) != workspace["metadata"]:
        raise WorkspaceError("workspace metadata redirected")
    if (
        not workspace.get("trusted", False)
        and _git(path, "rev-parse", "HEAD") != workspace["prepared_commit"]
    ):
        raise WorkspaceError("worker changed Git history")
    if (
        not workspace.get("trusted", False)
        and _git(path, "symbolic-ref", "--short", "HEAD") != workspace["branch"]
    ):
        raise WorkspaceError("worker changed workspace branch")
    # Bound all additions, including untracked files, before staging anything.
    names = _run(["ls-files", "-z", "--cached", "--others", "--exclude-standard"], path)
    links = _gitlinks(path) if workspace.get("trusted", False) else {}
    total_bytes = 0
    for name in set(names.split(b"\0")) - {b""}:
        relative = Path(os.fsdecode(name))
        if relative.is_absolute() or ".." in relative.parts or ".git" in relative.parts:
            raise WorkspaceError("unsafe workspace file path")
        candidate = _plain_path(path / relative.parent) / relative.name
        if candidate.is_symlink() and workspace.get("trusted", False):
            _project_symlink(candidate, path)
            continue
        candidate = _plain_path(candidate)
        if candidate.exists():
            metadata = candidate.stat()
            if stat.S_ISDIR(metadata.st_mode) and name in links:
                _check_submodule(path, relative, links[name])
                continue
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
        limit=MAX_PATCH_BYTES,
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
