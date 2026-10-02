"""Conversational settings edits confined to the active session's project."""

import contextlib
import difflib
import hashlib
import os
import secrets
from copy import deepcopy

from .config import (
    DEFAULT_CONFIG,
    PROJECT_SETTINGS_LIMIT,
    ConfigurationError,
    _read,
    load_config,
    project_config_directory,
    read_project_override,
    unvalidated_config,
    validate_config,
)
from .onboarding import _session
from .read_context import supervisor_exclusions, validate_read_roots
from .store import StateError, encode

MESSAGE = (
    "Role/model settings and context.read_roots apply to NEW tasks, not already captured tasks. "
    "Project permissions, worker enablement and worker concurrency are live controls. "
    "To change the native foreground model or effort, use /model and /effort."
)
OPEN_SETTINGS = {("planning", "templates"), ("commands", "tool_paths")}
PROCESS_WIDE_SETTINGS = {
    ("supervisor", "max_parallel"): (
        "supervisor.max_parallel is shared by all projects; "
        "use execution.max_parallel to limit this project's concurrent workers"
    ),
    ("supervisor", "poll_seconds"): (
        "supervisor.poll_seconds is shared by all projects and has no project setting; "
        "the operator sets it in config/local.toml"
    ),
}


def _schema():
    schema = _read(DEFAULT_CONFIG)
    role_fields = {field for role in schema["roles"].values() for field in role}
    for role in schema["roles"].values():
        for field in role_fields:
            role.setdefault(field, None)
    return schema


def _check_names(settings, schema, override, location=()):
    for key, value in settings.items():
        path = (*location, key)
        name = ".".join(path)
        if value is None and key in override:
            continue
        if path in PROCESS_WIDE_SETTINGS:
            if value is None:
                continue
            raise ConfigurationError(PROCESS_WIDE_SETTINGS[path])
        if path in OPEN_SETTINGS:
            continue
        if not isinstance(schema, dict) or key not in schema:
            choices = sorted(schema) if isinstance(schema, dict) else []
            suggestion = difflib.get_close_matches(key, choices, n=1)
            raise ConfigurationError(
                f"Unknown project setting {name}"
                + (f"; did you mean {'.'.join((*location, suggestion[0]))}?" if suggestion else "")
                + (
                    f" Supported {'.'.join(location) or 'sections'}: {', '.join(choices)}"
                    if choices
                    else f" {'.'.join(location)} has no nested settings"
                )
            )
        if isinstance(value, dict):
            nested = override.get(key)
            _check_names(value, schema[key], nested if isinstance(nested, dict) else {}, path)


def _apply(override, settings):
    result = deepcopy(override)
    for key, value in settings.items():
        if value is None:
            result.pop(key, None)
        elif isinstance(value, dict):
            nested = result.get(key)
            merged = _apply(nested if isinstance(nested, dict) else {}, value)
            if merged or not value:
                result[key] = merged
            else:
                result.pop(key, None)
        else:
            result[key] = deepcopy(value)
    return result


def _command(config, section, name):
    table = config.get(section)
    entry = table.get(name) if isinstance(table, dict) else None
    return entry.get("command") if isinstance(entry, dict) else None


def _snapshot(store, session, project):
    override, revision = read_project_override(store.home, project["id"])
    snapshot = {
        "project_id": project["id"],
        "revision": revision,
        "can_configure": bool(session["active"]) and not bool(session["observer"]),
        "message": MESSAGE,
    }
    try:
        snapshot["settings"] = load_config(store.home, project["id"])
    except ConfigurationError as error:
        snapshot["settings"] = unvalidated_config(store.home, project["id"], override)
        snapshot["error"] = (
            f"{error}. Repair this project's settings with configure_project; "
            "a null value removes this project's override and restores the inherited setting."
        )
    return snapshot


def project_settings(store, session_id):
    """Return effective bound-project settings and the private override revision."""
    with store.transaction() as database:
        session, project = _session(database, session_id)
        return _snapshot(store, session, project)


def configure_project(store, session_id, settings, expected_revision=None):
    """Deep-merge supported settings into only this project's private JSON override.

    A null value removes only this project's override, revealing the inherited setting.
    """
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
        _check_names(settings, _schema(), override)
        settings = deepcopy(settings)
        context = settings.get("context")
        if isinstance(context, dict) and context.get("read_roots") is not None:
            try:
                context["read_roots"] = validate_read_roots(
                    context["read_roots"],
                    normalize=True,
                    excluded_paths=supervisor_exclusions(store.home),
                )
            except ValueError as error:
                raise ConfigurationError(str(error)) from error
        updated = _apply(override, settings)
        current = unvalidated_config(store.home, project_id, override)
        effective = unvalidated_config(store.home, project_id, updated)
        validate_config(effective)
        try:
            validate_read_roots(
                effective.get("context", {}).get("read_roots", []),
                excluded_paths=supervisor_exclusions(store.home),
            )
        except ValueError as error:
            raise ConfigurationError(str(error)) from error
        # A replacement executable can ignore all harness safety flags. Chat settings
        # can tune roles and resources, but cannot install executable entry points.
        for section in ("adapters", "frontends"):
            for name, value in effective.get(section, {}).items():
                if (
                    isinstance(value, dict)
                    and "command" in value
                    and value["command"] != _command(current, section, name)
                ):
                    raise StateError("Executable commands cannot be changed conversationally")
        if updated == override:
            return {
                "project_id": project_id,
                "settings": effective,
                "revision": revision,
                "can_configure": True,
                "changed": False,
                "message": MESSAGE,
            }
        text = encode(updated) + "\n"
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
