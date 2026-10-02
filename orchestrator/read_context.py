"""Explicit specialist read roots, shared by settings, launchers and the file broker."""

import os
import re
from pathlib import Path

MAX_READ_ROOTS = 16
ALIAS = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
CONTROL_DIRECTORIES = {
    ".git",
    ".orchestrator",
    ".ssh",
    ".aws",
    ".azure",
    ".gnupg",
    ".docker",
    ".kube",
    ".pi",
    ".claude",
    "credentials",
    "secrets",
    "auth.json",
    "credentials.json",
    "token",
    "tokens",
    "secrets.json",
    "id_rsa",
    "id_ed25519",
    "id_ecdsa",
    "id_dsa",
    ".npmrc",
    ".pypirc",
    ".netrc",
    "_netrc",
    ".pgpass",
    ".git-credentials",
}
# These are host-wide locations, not individually selected project directories.
HOST_ROOTS = {
    Path(path)
    for path in ("/", "/home", "/Users", "/tmp", "/var", "/usr", "/opt", "/srv", "/mnt", "/media")
}
HOST_CONTROL_ROOTS = tuple(
    Path(path) for path in ("/etc", "/proc", "/sys", "/dev", "/run", "/boot")
)


def overlaps(left, right):
    return left == right or left in right.parents or right in left.parents


def validate_read_roots(value, *, normalize=False, require_available=False, excluded_paths=()):
    """Validate captured path strings without requiring the directories to remain present.

    Explicit settings edits normalize existing directories; specialist launchers
    require availability and reject paths that now resolve somewhere else.
    Unrelated settings and worker dispatch only need structural/policy validation.
    Exclusions identify private supervisor/auth paths known to each caller.
    """
    if not isinstance(value, list) or len(value) > MAX_READ_ROOTS:
        raise ValueError(
            f"context.read_roots must be a list of at most {MAX_READ_ROOTS} {{alias, path}} entries"
        )
    if not value:
        return []

    def policy_path(path):
        supplied = Path(path).expanduser()
        return (
            supplied.resolve()
            if normalize or require_available
            else Path(os.path.abspath(supplied))
        )

    aliases = set()
    directories = set()
    result = []
    home = policy_path(Path.home())
    auth = policy_path(os.environ.get("PI_CODING_AGENT_DIR", str(home / ".pi/agent")))
    config_home = policy_path(os.environ.get("XDG_CONFIG_HOME", str(home / ".config")))
    exclusions = [
        auth,
        policy_path(os.environ.get("CLAUDE_CONFIG_DIR", str(home / ".claude"))),
        policy_path(os.environ.get("GH_CONFIG_DIR", str(config_home / "gh"))),
        policy_path(config_home / "gcloud"),
        policy_path(home / ".local/share/keyrings"),
        *(policy_path(path) for path in excluded_paths),
    ]
    for index, entry in enumerate(value):
        label = f"context.read_roots[{index}]"
        if not isinstance(entry, dict) or set(entry) != {"alias", "path"}:
            raise ValueError(f"{label} requires exactly alias and path")
        alias = entry["alias"]
        if not isinstance(alias, str) or not ALIAS.fullmatch(alias) or alias.lower() == "project":
            raise ValueError(
                f"{label}.alias must be 1-64 ASCII letters, digits, underscores or hyphens, starting with a letter or digit; project is reserved"
            )
        if alias in aliases:
            raise ValueError(
                f"{label}.alias duplicates {alias}; give each read root a unique alias"
            )
        path = entry["path"]
        if (
            not isinstance(path, str)
            or not path
            or len(path) > 4096
            or any(ord(character) < 32 or ord(character) == 127 for character in path)
        ):
            raise ValueError(
                f"{label}.path must be an absolute directory path without control characters"
            )
        try:
            path.encode("utf-8", "strict")
            supplied = Path(path).expanduser() if normalize else Path(path)
            if not supplied.is_absolute():
                raise ValueError(
                    f"{label}.path must be absolute; select the specific reference directory"
                )
            canonical = Path(os.path.normpath(supplied))
            if normalize or require_available:
                canonical = supplied.resolve(strict=True)
                if not canonical.is_dir():
                    raise ValueError(
                        f"{label}.path must be an existing directory; repair or remove this read root"
                    )
        except (OSError, RuntimeError, UnicodeError) as error:
            raise ValueError(
                f"{label}.path cannot be opened as an existing directory; repair or remove this read root"
            ) from error
        if not normalize and (path != str(canonical) or path.startswith("//")):
            raise ValueError(
                f"{label}.path must be canonical; save its absolute resolved directory through configure_project"
            )
        if (
            canonical in HOST_ROOTS
            or canonical == home
            or any(canonical == root or root in canonical.parents for root in HOST_CONTROL_ROOTS)
            or any(
                part.lower() in CONTROL_DIRECTORIES
                or part.lower().startswith((".env", "credentials.", "secrets."))
                or part.lower().endswith((".pem", ".key", ".p12", ".pfx"))
                for part in canonical.parts
            )
            or any(overlaps(canonical, private) for private in exclusions)
        ):
            raise ValueError(
                f"{label}.path includes host-wide, control, authentication or private supervisor files; select a specific project reference directory"
            )
        if canonical in directories:
            raise ValueError(
                f"{label}.path duplicates another read root; use one alias per directory"
            )
        aliases.add(alias)
        directories.add(canonical)
        result.append({"alias": alias, "path": str(canonical)})
    return result


def supervisor_exclusions(home):
    """Private state/configuration must not become model-readable reference material."""
    return (Path(home) / "data", Path(home) / "config")
