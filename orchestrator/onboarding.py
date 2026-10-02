"""Project policy configuration without granting a coordinator shell or source writes."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import secrets
import stat
from pathlib import Path

from .routing import (
    RoutingError,
    _read_policy_file,
    _unique_object,
    load_policy,
    policy_readiness,
    validate_policy,
)
from .store import StateError, encode

POLICY_LIMIT = 1024 * 1024
POLICY_NAME = "crew-dispatch.json"


def _session(database, session_id, *, writer=False):
    session = database.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
    if not session or not session["active"] or not session["project_id"]:
        raise StateError("Project setup requires an active, bound session")
    if writer and session["observer"]:
        raise StateError("Only the active project coordinator may configure the project")
    project = database.execute(
        "SELECT * FROM projects WHERE id=?", (session["project_id"],)
    ).fetchone()
    return session, project


def _key(project_id):
    return f"project-setup-complete:{project_id}"


def _complete(database, project_id):
    database.execute("INSERT OR IGNORE INTO service VALUES(?,?)", (_key(project_id), "true"))


def _local_policy(root):
    path = Path(root) / ".orchestrator" / POLICY_NAME
    try:
        raw = _read_policy_file(path)
    except FileNotFoundError:
        # A dangling symlink is not an absent policy or an authorization to replace it.
        if path.is_symlink() or path.parent.is_symlink():
            raise StateError("Repair the symlinked worker policy before project setup")
        return path, "missing", None, None
    if len(raw) > POLICY_LIMIT:
        raise StateError("Worker policy exceeds 1 MiB; inspect it before setup")
    revision = hashlib.sha256(raw).hexdigest()
    try:
        policy = validate_policy(json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object))
    except (ValueError, UnicodeError, RecursionError) as error:
        return path, revision, None, str(error)
    return path, revision, policy, None


def _snapshot(database, store, session, project):
    path, revision, policy, error = _local_policy(project["root"])
    source = str(path)
    if revision == "missing":
        try:
            inherited = load_policy(store.home, Path(project["root"]))
            policy, source = inherited["policy"], inherited["source"]
        except RoutingError as failure:
            error = str(failure)
    configured = policy is not None and bool(
        policy.get("rules") or policy.get("classifications") or "default" in policy
    )
    closed = bool(
        database.execute("SELECT 1 FROM service WHERE key=?", (_key(project["id"]),)).fetchone()
    )
    dispatched = bool(
        database.execute(
            "SELECT 1 FROM worker_requests WHERE project_id=? AND task_id IS NOT NULL LIMIT 1",
            (project["id"],),
        ).fetchone()
    )
    # Keep historical bootstrap status informational, never an authorization seal.
    if configured or dispatched:
        _complete(database, project["id"])
        closed = True
    return {
        "project_id": project["id"],
        "phase": "configured"
        if configured
        else "repair_required"
        if closed
        else "needs_configuration",
        "initial_setup_open": not closed,
        "can_configure": bool(session["active"]) and not bool(session["observer"]),
        "policy_path": str(path),
        "policy_source": source,
        "policy_revision": revision,
        "policy": policy,
        "policy_error": error,
        "validation": policy_readiness(policy) if policy is not None else None,
        "execution_authorized": False,
        "next_step": (
            "Use setup_project to save or repair worker preferences at any time. "
            "Optionally pass expected_revision=policy_revision to detect stale edits. "
            "Incomplete preferences are saved as drafts; inspect validation blockers before dispatch. "
            "Changes apply to new tasks, not running work. Do not invent worker profiles."
        ),
    }


def project_setup(store, session_id):
    """Read readiness and remember previously configured projects, without changing files."""
    with store.transaction() as database:
        session, project = _session(database, session_id)
        return _snapshot(database, store, session, project)


def _write_policy(root, text, *, expected_revision):
    """Descriptor-relative writes, no symlink traversal, no arbitrary caller-supplied path."""
    with contextlib.ExitStack() as stack:
        path = Path(root)
        directory = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
        stack.callback(os.close, directory)
        for component in path.parts[1:]:
            directory = os.open(
                component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory
            )
            stack.callback(os.close, directory)
        try:
            os.mkdir(".orchestrator", mode=0o700, dir_fd=directory)
        except FileExistsError:
            pass
        directory = os.open(
            ".orchestrator", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory
        )
        stack.callback(os.close, directory)
        temporary = ".setup-" + secrets.token_hex(16)
        descriptor = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                output.write(text)
                output.flush()
                os.fsync(output.fileno())
            if expected_revision == "missing":
                # Atomic no-clobber installation, even if another process creates the file.
                os.link(
                    temporary,
                    POLICY_NAME,
                    src_dir_fd=directory,
                    dst_dir_fd=directory,
                    follow_symlinks=False,
                )
                os.unlink(temporary, dir_fd=directory)
            else:
                # Recheck the actual target directory immediately before replacement too.
                # SQLite serializes supported setup writers. Arbitrary manual editors do
                # not share that lock: they must not edit concurrently with chat setup.
                # This is optimistic revision checking, not filesystem compare-and-swap.
                target = os.open(
                    POLICY_NAME, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
                )
                with os.fdopen(target, "rb") as existing:
                    if not stat.S_ISREG(os.fstat(existing.fileno()).st_mode):
                        raise StateError("Worker policy must be a regular file")
                    current = existing.read(POLICY_LIMIT + 1)
                if (
                    len(current) > POLICY_LIMIT
                    or hashlib.sha256(current).hexdigest() != expected_revision
                ):
                    raise StateError("Worker policy changed; reread project_setup before saving")
                os.replace(temporary, POLICY_NAME, src_dir_fd=directory, dst_dir_fd=directory)
            os.fsync(directory)
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temporary, dir_fd=directory)


def setup_project(store, session_id, policy=None, expected_revision=None):
    """Save a project-local policy or draft; never approve, execute, or edit source."""
    if expected_revision is not None and (
        not isinstance(expected_revision, str) or not expected_revision
    ):
        raise StateError("expected_revision must be a nonempty string")
    if policy is not None:
        policy = validate_policy(policy)
    with store.transaction() as database:
        session, project = _session(database, session_id, writer=True)
        before = _snapshot(database, store, session, project)
        if expected_revision is not None and expected_revision != before["policy_revision"]:
            raise StateError("Worker policy changed; reread project_setup before saving")
        if policy is None and before["policy_revision"] != "missing":
            result = {**before, "changed": False}
        else:
            saved_policy = policy if policy is not None else before["policy"]
            if saved_policy is None:
                saved_policy = {"rules": []}
            text = encode(saved_policy) + "\n"
            if len(text.encode("utf-8")) > POLICY_LIMIT:
                raise StateError("Worker policy exceeds 1 MiB")
            _write_policy(project["root"], text, expected_revision=before["policy_revision"])
            result = _snapshot(database, store, session, project)
            store._event(
                database,
                project["id"],
                "project.setup",
                {
                    "phase": result["phase"],
                    "policy_path": result["policy_path"],
                    "policy_revision": result["policy_revision"],
                    "execution_authorized": False,
                },
                session_id,
                review_required=result["phase"] == "configured",
            )
            result = {**result, "changed": True}
    return result
