"""Conversational settings edits confined to the active session's project."""

import contextlib
import hashlib
import os
import secrets

from .config import (
    PROJECT_SETTINGS_LIMIT,
    _merge,
    load_config,
    project_config_directory,
    read_project_override,
    validate_config,
)
from .onboarding import _session
from .store import StateError, encode

MESSAGE = (
    "Role/model settings apply to NEW tasks, not running tasks. "
    "Project permissions, worker enablement and worker concurrency are live controls. "
    "To change the native foreground model or effort, use /model and /effort."
)


def _snapshot(store, session, project):
    _, revision = read_project_override(store.home, project["id"])
    return {
        "project_id": project["id"],
        "settings": load_config(store.home, project["id"]),
        "revision": revision,
        "can_configure": bool(session["active"]) and not bool(session["observer"]),
        "message": MESSAGE,
    }


def project_settings(store, session_id):
    """Return effective bound-project settings and the private override revision."""
    with store.transaction() as database:
        session, project = _session(database, session_id)
        return _snapshot(store, session, project)


def configure_project(store, session_id, settings, expected_revision=None):
    """Deep-merge supported settings into only this project's private JSON override."""
    if not isinstance(settings, dict):
        raise StateError("settings must be an object")
    if expected_revision is not None and (
        not isinstance(expected_revision, str) or not expected_revision
    ):
        raise StateError("expected_revision must be a nonempty string")
    with store.transaction() as database:
        _session_state, project = _session(database, session_id, writer=True)
        project_id = project["id"]
        override, revision = read_project_override(store.home, project_id)
        if expected_revision is not None and expected_revision != revision:
            raise StateError("Project settings changed; reread project_settings before saving")
        current = load_config(store.home, project_id)
        supported = set(current) | {"permissions"}
        if set(settings) - supported:
            raise StateError("Supported settings sections: " + ", ".join(sorted(supported)))
        effective = _merge(current, settings)
        validate_config(effective)
        # A replacement executable can ignore all harness safety flags. Chat settings
        # can tune roles and resources, but cannot install executable entry points.
        for section in ("adapters", "frontends"):
            for name, value in effective.get(section, {}).items():
                if (
                    isinstance(value, dict)
                    and "command" in value
                    and value["command"] != current.get(section, {}).get(name, {}).get("command")
                ):
                    raise StateError("Executable commands cannot be changed conversationally")
        text = encode(_merge(override, settings)) + "\n"
        raw = text.encode("utf-8")
        if len(raw) > PROJECT_SETTINGS_LIMIT:
            raise StateError("Project settings exceed 1 MiB")
        with project_config_directory(store.home, project_id, create=True) as directory:
            temporary = ".settings-" + secrets.token_hex(16)
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=directory,
            )
            try:
                with os.fdopen(descriptor, "wb") as output:
                    output.write(raw)
                    output.flush()
                    os.fsync(output.fileno())
                _, latest_revision = read_project_override(store.home, project_id)
                if latest_revision != revision:
                    raise StateError(
                        "Project settings changed; reread project_settings before saving"
                    )
                # Replace the directory entry, never mutate an existing hardlinked inode.
                os.replace(
                    temporary, project_id + ".json", src_dir_fd=directory, dst_dir_fd=directory
                )
                os.fsync(directory)
            finally:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(temporary, dir_fd=directory)
        saved_revision = hashlib.sha256(raw).hexdigest()
        store._event(
            database,
            project_id,
            "project.configured",
            {"sections": sorted(settings), "revision": saved_revision},
            session_id,
        )
        return {
            "project_id": project_id,
            "settings": effective,
            "revision": saved_revision,
            "can_configure": True,
            "changed": saved_revision != revision,
            "message": MESSAGE,
        }
