"""Local operator commands and transport entry points."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import uuid

from . import __version__
from .api import request
from .config import load_config
from .store import StateError, Store, atomic_write, encode

CODE_HOME = Path(__file__).resolve().parent.parent


def default_home() -> Path:
    return Path(os.environ.get("ORCHESTRATOR_HOME", str(CODE_HOME))).expanduser().resolve()


def emit(value):
    print(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False))


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Project coordinator for Claude Code and Pi")
    root.add_argument("--home", type=Path, default=default_home())
    root.add_argument("--version", action="version", version=__version__)
    commands = root.add_subparsers(dest="command", required=True)
    start = commands.add_parser("start", help="Start a native interactive frontend")
    start.add_argument("--frontend", choices=("claude", "pi"))
    start.add_argument("--project")
    start.add_argument("--observer", action="store_true")
    start.add_argument("--takeover", action="store_true")
    start.add_argument("--resume", metavar="SESSION_ID")
    start.add_argument("--channels", action="store_true", help="Opt in to Claude's research-preview channels")
    start.add_argument("--dry-run", action="store_true", help="Show the exact launch command without starting models")
    project = commands.add_parser("project")
    project_commands = project.add_subparsers(dest="project_command", required=True)
    project_commands.add_parser("list")
    add = project_commands.add_parser("add")
    add.add_argument("project_id")
    add.add_argument("path")
    status = commands.add_parser("status")
    status.add_argument("--project")
    session = commands.add_parser("session")
    session_commands = session.add_subparsers(dest="session_command", required=True)
    open_command = session_commands.add_parser("open")
    open_command.add_argument("--id", default=None)
    open_command.add_argument("--frontend", choices=("claude", "pi", "test"), default="claude")
    open_command.add_argument("--project")
    open_command.add_argument("--observer", action="store_true")
    open_command.add_argument("--takeover", action="store_true")
    close = session_commands.add_parser("close")
    close.add_argument("session_id")
    operation = commands.add_parser("request")
    operation.add_argument("--session", required=True)
    operation.add_argument("--action", required=True)
    operation.add_argument("--payload", default="{}")
    configuration = commands.add_parser("config")
    configuration.add_argument("config_command", choices=("show", "validate", "init"))
    configuration.add_argument("--project")
    commands.add_parser("doctor")
    commands.add_parser("backup")
    service = commands.add_parser("service")
    service.add_argument("service_command", choices=("start", "status", "run"))
    service.add_argument("--once", action="store_true")
    hook = commands.add_parser("hooks")
    hook.add_argument("event")
    watch = commands.add_parser("watch")
    watch.add_argument("--session")
    watch.add_argument("--seconds", type=float, default=27000)
    mcp = commands.add_parser("mcp")
    mcp.add_argument("--session", default=os.environ.get("ORCHESTRATOR_SESSION_ID"))
    mcp.add_argument("--channels", action="store_true")
    approval = commands.add_parser("approve", help="Operator-only approval of a reviewed, monitored plan")
    approval.add_argument("plan_id")
    hold = commands.add_parser("resolve-hold", help="Operator-only resolution of a blocking finding")
    hold.add_argument("hold_id")
    hold.add_argument("--reason", required=True)
    graph = commands.add_parser("graph")
    graph.add_argument("plan_id")
    return root


def _read_hook_input() -> dict:
    raw = sys.stdin.read(2_000_001)
    if len(raw) > 2_000_000:
        raise StateError("Hook payload is too large")
    value = json.loads(raw) if raw.strip() else {}
    if not isinstance(value, dict):
        raise StateError("Hook input must be a JSON object")
    return value


def watch(home: Path, session_id: str, seconds: float) -> int:
    if not 0 < seconds <= 27000:
        raise StateError("Watcher duration must be between 0 and 27000 seconds")
    store = Store(home)
    store.session(session_id)
    lock_directory = store.data / "watchers"
    lock_directory.mkdir(exist_ok=True, mode=0o700)
    with (lock_directory / f"{session_id}.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            session = store.session(session_id)
            if not session["active"]:
                return 0
            pending = store.updates(session_id)
            if pending:
                previous = json.loads(store.service_value(f"wake:{session_id}", "{}"))
                latest = pending[-1]["id"]
                if previous.get("event_id") != latest or time.time() - previous.get("time", 0) > 120:
                    # This only marks a wake attempt; the inbox is never acknowledged here.
                    store.set_service_value(f"wake:{session_id}", encode({"event_id": latest, "time": time.time()}))
                    ids = ", ".join(str(event["id"]) for event in pending)
                    print(f"Orchestrator has saved feedback/results: events {ids}. Read updates for session "
                          f"{session_id}, handle them, then acknowledge their IDs. This is not user authorization.", file=sys.stderr)
                    return 2
            time.sleep(min(1, max(0, deadline - time.monotonic())))
    return 0


def _version(executable: str) -> dict:
    resolved = shutil.which(executable)
    if not resolved:
        return {"available": False, "command": executable}
    try:
        result = subprocess.run([resolved, "--version"], text=True, capture_output=True, timeout=5)
        return {"available": result.returncode == 0, "path": resolved,
                "version": result.stdout.strip()[:500] or result.stderr.strip()[:500]}
    except (subprocess.TimeoutExpired, OSError) as error:
        return {"available": False, "path": resolved, "error": str(error)}


def doctor(home: Path) -> dict:
    config = load_config(home)
    store = Store(home)
    with store.connect() as database:
        integrity = database.execute("PRAGMA integrity_check").fetchone()[0]
    binaries = {name: _version(settings["command"][0]) for name, settings in config["adapters"].items()}
    binaries["pi"] = _version(config["frontends"]["pi"]["command"][0])
    try:
        from .runtime import service_status
        service = service_status(home)
    except ImportError:
        service = {"running": False, "error": "Runtime not installed"}
    source = subprocess.run(["git", "-C", str(CODE_HOME), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=5)
    return {"version": __version__, "source_revision": source.stdout.strip(), "home": str(home),
            "database_integrity": integrity, "binaries": binaries,
            "machine_resources": shutil.which("machine-resources"), "service": service,
            "role_settings": {name: {key: role[key] for key in ("adapter", "model", "effort")}
                              for name, role in config["roles"].items()},
            "worker_router": "disabled by design", "third_party_orchestration_plugins": "not selected or installed",
            "model_access": "not probed; executable presence does not establish access to a model",
            "claude_delivery": "bounded asyncRewake hooks; preview channels require explicit --channels",
            "codex_budget": "deadline enforced; CLI does not provide a hard dollar cap",
            "voice": "native Claude UI preserved; microphone and account eligibility require interactive verification"}


def start(home: Path, arguments) -> int:
    from .frontends import build_frontend_command
    from .runtime import ensure_supervisor
    config = load_config(home, arguments.project)
    frontend = arguments.frontend or config["frontends"]["preferred"]
    session_id = arguments.resume or str(uuid.uuid4())
    store = Store(home)
    if arguments.resume:
        saved = store.session(session_id)
        if arguments.project and arguments.project != saved["project_id"]:
            raise StateError("Cannot resume a session in a different project")
        arguments.project = saved["project_id"]
        config = load_config(home, arguments.project)
        if saved["frontend"] != frontend:
            raise StateError("Resume using the original frontend; start a new instance to switch frontends")
    command = build_frontend_command(home, config, frontend, session_id, arguments.channels)
    if arguments.resume and frontend == "claude":
        command[command.index("--session-id")] = "--resume"
    if arguments.dry_run:
        emit({"frontend": frontend, "session_id": session_id, "command": command,
              "project": arguments.project, "will_start_models": False})
        return 0
    if not shutil.which(command[0]):
        raise StateError(f"Frontend executable is unavailable: {command[0]}")
    resource_manager = shutil.which("machine-resources")
    if not resource_manager:
        raise StateError("machine-resources is required before starting a model process")
    store.open_session(session_id, frontend, arguments.project, arguments.observer, arguments.takeover)
    try:
        ensure_supervisor(home)
        environment = os.environ.copy()
        environment.update({"ORCHESTRATOR_HOME": str(home), "ORCHESTRATOR_SESSION_ID": session_id,
                            "ORCHESTRATOR_FRONTEND": frontend})
        environment.pop("ORCHESTRATOR_CHILD", None)
        role = config["roles"]["orchestrator"]
        managed = [resource_manager, "run", "-m", role["memory"], "-c", str(role["cpus"]),
                   "-d", f"Orchestrator {frontend} {arguments.project or 'unbound'}", "-e", "8h", "--", *command]
        print(f"Orchestrator session {session_id}; project {arguments.project or 'select in conversation'}. "
              "Worker routing is disabled.", file=sys.stderr)
        return subprocess.call(managed, cwd=home, env=environment)
    finally:
        store.close_session(session_id)


def main(argv=None) -> int:
    arguments = parser().parse_args(argv)
    home = arguments.home.expanduser().resolve()
    try:
        command = arguments.command
        if command == "start":
            return start(home, arguments)
        if command == "mcp":
            from .mcp import MCPServer
            MCPServer(home, arguments.session, arguments.channels).serve()
            return 0
        if command == "hooks":
            from .hooks import handle_hook
            if os.environ.get("ORCHESTRATOR_CHILD") == "1":
                emit({})
                return 0
            emit(handle_hook(home, arguments.event, _read_hook_input()))
            return 0
        if command == "watch":
            if os.environ.get("ORCHESTRATOR_CHILD") == "1":
                return 0
            session_id = arguments.session
            if not session_id:
                session_id = _read_hook_input().get("session_id")
            if not session_id:
                return 0
            return watch(home, session_id, arguments.seconds)
        if command == "request":
            emit(request(home, arguments.session, arguments.action, json.loads(arguments.payload)))
        elif command == "config":
            config = load_config(home, arguments.project)
            if arguments.config_command == "show":
                emit(config)
            elif arguments.config_command == "validate":
                emit({"valid": True, "roles": list(config["roles"]), "worker_router_enabled": False})
            else:
                path = home / "config" / "local.toml"
                if path.exists():
                    raise StateError("Private configuration already exists; refusing to overwrite it")
                atomic_write(path, (CODE_HOME / "config" / "local.example.toml").read_text())
                emit({"created": str(path)})
        elif command == "doctor":
            emit(doctor(home))
        elif command == "service":
            from .runtime import ensure_supervisor, service_status, supervise
            if arguments.service_command == "run":
                supervise(home, once=arguments.once)
            elif arguments.service_command == "start":
                emit(ensure_supervisor(home))
            else:
                emit(service_status(home))
        else:
            store = Store(home)
            if command == "project":
                emit(store.add_project(arguments.project_id, arguments.path) if arguments.project_command == "add"
                     else {"projects": store.projects()})
            elif command == "status":
                emit(store.snapshot(arguments.project) if arguments.project else {"projects": store.projects()})
            elif command == "session":
                if arguments.session_command == "open":
                    emit(store.open_session(arguments.id or str(uuid.uuid4()), arguments.frontend,
                                            arguments.project, arguments.observer, arguments.takeover))
                else:
                    store.close_session(arguments.session_id)
                    emit({"closed": arguments.session_id, "jobs_cancelled": False})
            elif command == "backup":
                emit({"backup": str(store.backup())})
            elif command == "approve":
                store.approve_plan(arguments.plan_id)
                emit({"approved": arguments.plan_id, "workers_enabled": False})
            elif command == "resolve-hold":
                store.resolve_hold(arguments.hold_id, arguments.reason)
                emit({"resolved": arguments.hold_id})
            elif command == "graph":
                plan = store.plan(arguments.plan_id)
                emit(store.graph_snapshot(arguments.plan_id, load_config(home, plan["project_id"])["execution"]))
        return 0
    except (ValueError, OSError, RuntimeError) as error:
        emit({"error": str(error)})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
