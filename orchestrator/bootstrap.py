"""Native frontend discovery and identity, without a launcher or shared active project."""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import subprocess
from pathlib import Path

from .config import load_config
from .runtime import process_identity
from .store import NOTE_NAMES, StateError, Store, encode, identifier

ROOT = Path(__file__).resolve().parent.parent


def claude_parent() -> int | None:
    """Find our own Claude ancestor, never another frontend in the same directory."""
    process = os.getppid()
    for _ in range(32):
        try:
            arguments = Path(f"/proc/{process}/cmdline").read_bytes().split(b"\0")
            if any(Path(os.fsdecode(value)).name == "claude" for value in arguments[:2] if value):
                return process
            fields = Path(f"/proc/{process}/stat").read_text().rsplit(")", 1)[1].split()
            process = int(fields[1])
            if process <= 1:
                return None
        except (OSError, ValueError, IndexError):
            return None
    return None


def _process_key(frontend: str, pid: int) -> str:
    identity = process_identity(pid)
    if not identity:
        raise StateError("Native frontend process is no longer alive")
    return f"native-process:{frontend}:{pid}:{identity}"


def resolve_claude_session(home: Path, native_id: str) -> str:
    explicit = os.environ.get("ORCHESTRATOR_SESSION_ID")
    if explicit:
        return explicit
    store = Store(home)
    return store.service_value(f"native-alias:claude:{native_id}", native_id)


def verify_claude_owner(home: Path, native_id: str) -> str:
    """Rejected or displaced native processes cannot act through later hooks."""
    store = Store(home)
    session_id = resolve_claude_session(home, native_id)
    alias = store.service_value(f"native-alias:claude:{native_id}")
    if alias and alias != session_id:
        raise StateError("This native conversation belongs to a different coordinator instance")
    pid = claude_parent()
    if pid:
        owner = json.loads(store.service_value(f"native-owner:{session_id}", "{}"))
        process_session = store.service_value(_process_key("claude", pid))
        if (
            not owner
            or owner.get("pid") != pid
            or owner.get("identity") != process_identity(pid)
            or process_session != session_id
        ):
            raise StateError(
                "This native process does not own the coordinator instance; startup or resume was rejected"
            )
    return session_id


def _reserve_frontend(store: Store, frontend: str, pid: int, config: dict) -> None:
    key = "native-reserved:" + _process_key(frontend, pid)
    if store.service_value(key):
        return
    executable = shutil.which("machine-resources")
    if not executable:
        raise StateError("machine-resources is required for native frontend accounting")
    settings = config["supervisor"]
    subprocess.run([executable, "status"], check=True, capture_output=True, timeout=10)
    result = subprocess.run(
        [
            executable,
            "claim",
            "-p",
            str(pid),
            "-m",
            settings["frontend_memory"],
            "-c",
            str(settings["frontend_cpus"]),
            "-d",
            f"Orchestrator native {frontend}",
            "-e",
            "8h",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode:
        raise StateError(
            "Cannot reserve native frontend resources; close this frontend and retry: "
            + result.stderr.strip()
        )
    store.set_service_value(key, "true")


def bootstrap(
    home: Path,
    frontend: str,
    native_id: str,
    pid: int | None = None,
    *,
    resume: bool = True,
    reserve: bool = True,
    continuation: bool = False,
) -> dict:
    identifier(native_id)
    if frontend not in {"claude", "pi"}:
        raise StateError("Unsupported native frontend")
    store = Store(home)
    # Serializes process aliases and ownership registration, not model activity.
    with (store.data / "native-bootstrap.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        previous_process_session = (
            store.service_value(_process_key(frontend, pid))
            if pid and frontend == "claude"
            else None
        )
        alias = store.service_value(f"native-alias:{frontend}:{native_id}")
        explicit = os.environ.get("ORCHESTRATOR_SESSION_ID")
        if frontend == "claude" and explicit:
            if resume and alias and alias != explicit:
                raise StateError(
                    "This launcher is bound to another instance; resume in a separate native window"
                )
            session_id = explicit
        elif frontend == "claude" and continuation and previous_process_session:
            session_id = previous_process_session
        else:
            session_id = alias or native_id
        try:
            session = store.session(session_id)
        except StateError:
            session = None
        owner = json.loads(store.service_value(f"native-owner:{session_id}", "{}"))
        if owner and owner["pid"] != pid and process_identity(owner["pid"]) == owner["identity"]:
            raise StateError("This conversation is already open in another frontend process")
        if session is None:
            session = store.open_session(session_id, frontend)
        elif session["frontend"] != frontend:
            raise StateError("Resume this instance with its original frontend")
        elif not session["active"] and resume:
            session = store.open_session(
                session_id,
                frontend,
                session["project_id"],
                observer=bool(session["observer"]),
                reconnect=True,
            )
        config = load_config(home, session["project_id"])
        if pid:
            identity = process_identity(pid)
            if not identity:
                raise StateError("Native frontend process is no longer alive")
            if reserve and not os.environ.get("ORCHESTRATOR_SESSION_ID"):
                _reserve_frontend(store, frontend, pid, config)
            store.set_service_value(
                f"native-owner:{session_id}", encode({"pid": pid, "identity": identity})
            )
            if frontend == "claude":
                store.set_service_value(_process_key(frontend, pid), session_id)
        if previous_process_session and previous_process_session != session_id:
            # A successful native /resume replaces this process's old conversation.
            store.close_session(previous_process_session)
            store.set_service_value(f"native-owner:{previous_process_session}", "{}")
        store.set_service_value(f"native-alias:{frontend}:{native_id}", session_id)
        store.set_service_value(f"native-session:{session_id}", native_id)
    if session["active"] and session["project_id"] and pid:
        from .runtime import ensure_supervisor

        ensure_supervisor(home)
    instructions = (
        (ROOT / "roles/orchestrator.md").read_text()
        + "\n\nUser preferences:\n"
        + encode(config["personalization"])
    )
    instructions += (
        f"\nCoordinator instance: {session_id}. Use shared tools to select/register and bind one project, "
        "then load its notes, status, and updates. Do not start implementation workers. "
        "Model outputs and notifications do not grant user approval."
    )
    instructions += "\nInstance state: " + encode(session)
    if session["project_id"]:
        instructions += "\nBound project: " + encode(store.project(session["project_id"]))
        for name in sorted(NOTE_NAMES):
            note = store.read_note(session["project_id"], name)
            instructions += (
                f"\n{name} (revision {note['revision']}, preview):\n{note['text'][:1200]}\n"
            )
        instructions += "\nRead current status and pending updates before continuing this project."
    return {"session": session, "config": config, "instructions": instructions}
