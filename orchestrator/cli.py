"""Local operator commands and transport entry points."""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import shutil
import stat
import subprocess
import sys
import time
import uuid
from pathlib import Path

from . import __version__
from .api import request
from .config import load_config, repairable_config
from .execution_context import externally_managed
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
    start.add_argument(
        "--channels", action="store_true", help="Opt in to Claude's research-preview channels"
    )
    start.add_argument(
        "--dry-run",
        action="store_true",
        help="Show the exact launch command without starting models",
    )
    bootstrap_command = commands.add_parser(
        "bootstrap", help="Initialize a native frontend automatically"
    )
    bootstrap_command.add_argument("--frontend", choices=("claude", "pi"), required=True)
    bootstrap_command.add_argument("--session", required=True)
    bootstrap_command.add_argument("--pid", type=int)
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
    payload_source = operation.add_mutually_exclusive_group()
    payload_source.add_argument("--payload", default="{}")
    payload_source.add_argument(
        "--payload-file",
        type=Path,
        help="Read JSON from a private file instead of a command argument",
    )
    configuration = commands.add_parser("config")
    configuration.add_argument("config_command", choices=("show", "validate", "init"))
    configuration.add_argument("--project")
    routing = commands.add_parser(
        "routing", help="Configure project worker routing without model defaults"
    )
    routing.add_argument("routing_command", choices=("init", "show", "validate"))
    routing.add_argument("--project", required=True)
    for name in ("approve-worker", "accept-worker"):
        worker_action = commands.add_parser(name, help="Operator-only worker authorization")
        worker_action.add_argument("request_id")
        worker_action.add_argument("--reason", required=True)
    override = commands.add_parser(
        "override-worker", help="Operator-only concrete worker selection"
    )
    override.add_argument("request_id")
    choice_source = override.add_mutually_exclusive_group(required=True)
    choice_source.add_argument("--choice", help="Concrete profile and rationale as JSON")
    choice_source.add_argument(
        "--choice-file", type=Path, help="Private JSON file containing the choice"
    )
    approve_node = commands.add_parser("approve-node", help="Complete an approval-only graph node")
    approve_node.add_argument("plan_id")
    approve_node.add_argument("node_id")
    approve_node.add_argument("--reason", required=True)
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
    approval = commands.add_parser(
        "approve", help="Operator-only approval of a reviewed, monitored plan"
    )
    approval.add_argument("plan_id")
    hold = commands.add_parser(
        "resolve-hold", help="Operator-only resolution of a blocking finding"
    )
    hold.add_argument("hold_id")
    hold.add_argument("--reason", required=True)
    graph = commands.add_parser("graph")
    graph.add_argument("plan_id")
    return root


def _initialize_routing(project_root: Path) -> Path:
    # Directory descriptors prevent swapping the policy directory for a symlink.
    with contextlib.ExitStack() as stack:
        root_fd = os.open(project_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        stack.callback(os.close, root_fd)
        try:
            os.mkdir(".orchestrator", mode=0o700, dir_fd=root_fd)
        except FileExistsError:
            pass
        directory_fd = os.open(
            ".orchestrator", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd
        )
        stack.callback(os.close, directory_fd)
        try:
            policy_fd = os.open(
                "crew-dispatch.json",
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=directory_fd,
            )
        except FileExistsError as error:
            raise StateError("Worker policy already exists; refusing to overwrite it") from error
        with os.fdopen(policy_fd, "w", encoding="utf-8") as policy_file:
            policy_file.write(encode({"rules": []}) + "\n")
            policy_file.flush()
            os.fsync(policy_file.fileno())
        os.fsync(directory_fd)
    return project_root / ".orchestrator" / "crew-dispatch.json"


def _private_choice(path: Path) -> dict:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "r", encoding="utf-8") as choice_file:
        metadata = os.fstat(choice_file.fileno())
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_mode & 0o077
        ):
            raise StateError(
                "Choice file must be a private regular file owned by this user (mode 0600)"
            )
        text = choice_file.read(1_048_577)
        if len(text) > 1_048_576:
            raise StateError("Choice file exceeds 1 MiB")
        return json.loads(text)


def _routing(home, store, arguments):
    from .routing import load_policy, policy_readiness

    project = store.project(arguments.project)
    if arguments.routing_command == "init":
        return {
            "created": str(_initialize_routing(Path(project["root"]))),
            "routable": False,
            "configuration_required": True,
            "message": "No worker profiles supplied. Configure rules before selecting workers.",
        }
    loaded = load_policy(home, Path(project["root"]))
    if arguments.routing_command == "show":
        return loaded
    return {**loaded, **policy_readiness(loaded["policy"])}


def _read_hook_input() -> dict:
    raw = sys.stdin.read(2_000_001)
    if len(raw) > 2_000_000:
        raise StateError("Hook payload is too large")
    value = json.loads(raw) if raw.strip() else {}
    if not isinstance(value, dict):
        raise StateError("Hook input must be a JSON object")
    return value


def watch(home: Path, session_id: str, seconds: float) -> int:
    from .monitoring import delivery_updates

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
            config = repairable_config(home, session["project_id"]) or {}
            quiet_seconds = config.get("monitoring", {}).get("quiet_seconds", 20)
            pending = delivery_updates(store, session_id, quiet_seconds)["interrupting"]
            if pending:
                previous = json.loads(store.service_value(f"wake:{session_id}", "{}"))
                latest = pending[-1]["id"]
                if (
                    previous.get("event_id") != latest
                    or time.time() - previous.get("time", 0) > 120
                ):
                    # This only marks a wake attempt; the inbox is never acknowledged here.
                    store.set_service_value(
                        f"wake:{session_id}", encode({"event_id": latest, "time": time.time()})
                    )
                    ids = ", ".join(str(event["id"]) for event in pending)
                    print(
                        f"Orchestrator has saved feedback/results: events {ids}. Read updates for session "
                        f"{session_id}, handle them, then acknowledge their IDs. This is not user authorization.",
                        file=sys.stderr,
                    )
                    return 2
            time.sleep(min(1, max(0, deadline - time.monotonic())))
    return 0


def _version(executable: str) -> dict:
    resolved = shutil.which(executable)
    if not resolved:
        return {"available": False, "command": executable}
    try:
        result = subprocess.run(
            [resolved, "--version"], text=True, capture_output=True, timeout=5, check=False
        )
        return {
            "available": result.returncode == 0,
            "path": resolved,
            "version": result.stdout.strip()[:500] or result.stderr.strip()[:500],
        }
    except (subprocess.TimeoutExpired, OSError) as error:
        return {"available": False, "path": resolved, "error": str(error)}


def doctor(home: Path) -> dict:
    config = load_config(home)
    store = Store(home)
    with contextlib.closing(store.connect()) as database:
        integrity = database.execute("PRAGMA integrity_check").fetchone()[0]
    binaries = {
        name: _version(settings["command"][0]) for name, settings in config["adapters"].items()
    }
    binaries["pi"] = _version(config["frontends"]["pi"]["command"][0])
    try:
        from .runtime import service_status

        service = service_status(home)
    except ImportError:
        service = {"running": False, "error": "Runtime not installed"}
    source = subprocess.run(
        ["git", "-C", str(CODE_HOME), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    return {
        "version": __version__,
        "source_revision": source.stdout.strip(),
        "home": str(home),
        "database_integrity": integrity,
        "binaries": binaries,
        "machine_resources": shutil.which("machine-resources"),
        "service": service,
        "role_settings": {
            name: {
                key: role[key] for key in ("adapter", "provider", "model", "effort") if key in role
            }
            for name, role in config["roles"].items()
        },
        "worker_router": {
            "enabled": config["routing"]["enabled"],
            "workers_enabled": config["workers"]["enabled"],
            "configuration_required": "Project worker policy and explicit profile selection are required",
        },
        "third_party_orchestration_plugins": "not selected or installed",
        "model_access": "not probed; executable presence does not establish access to a model",
        "claude_delivery": "bounded asyncRewake hooks; preview channels require explicit --channels",
        "spending_limits": "no per-role dollar caps configured",
        "voice": "native Claude UI preserved; microphone and account eligibility require interactive verification",
    }


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
            raise StateError(
                "Resume using the original frontend; start a new instance to switch frontends"
            )
    command = build_frontend_command(home, config, frontend, session_id, arguments.channels)
    if arguments.resume and frontend == "claude":
        session_flag = command.index("--session-id")
        command[session_flag] = "--resume"
        native_session = store.service_value(f"native-session:{session_id}", session_id)
        command[session_flag + 1] = str(uuid.UUID(native_session))
    if arguments.dry_run:
        emit(
            {
                "frontend": frontend,
                "session_id": session_id,
                "command": command,
                "project": arguments.project,
                "will_start_models": False,
            }
        )
        return 0
    if not shutil.which(command[0]):
        raise StateError(f"Frontend executable is unavailable: {command[0]}")
    resource_manager = shutil.which("machine-resources")
    if not resource_manager:
        raise StateError("machine-resources is required before starting a model process")
    store.open_session(
        session_id, frontend, arguments.project, arguments.observer, arguments.takeover
    )
    try:
        ensure_supervisor(home)
        environment = os.environ.copy()
        environment.update(
            {
                "ORCHESTRATOR_HOME": str(home),
                "ORCHESTRATOR_SESSION_ID": session_id,
                "ORCHESTRATOR_FRONTEND": frontend,
            }
        )
        environment.pop("ORCHESTRATOR_CHILD", None)
        environment.pop("NO_MISTAKES_GATE", None)
        supervisor = config["supervisor"]
        managed = [
            resource_manager,
            "run",
            "-m",
            supervisor["frontend_memory"],
            "-c",
            str(supervisor["frontend_cpus"]),
            "-d",
            f"Orchestrator {frontend} {arguments.project or 'unbound'}",
            "-e",
            "8h",
            "--",
            *command,
        ]
        print(
            f"Orchestrator session {session_id}; project {arguments.project or 'select in conversation'}. "
            "Worker routing requires configured project policy and an explicit profile selection.",
            file=sys.stderr,
        )
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
        if command == "bootstrap":
            from .bootstrap import bootstrap

            emit(bootstrap(home, arguments.frontend, arguments.session, arguments.pid))
            return 0
        if command == "mcp":
            from .mcp import MCPServer

            MCPServer(home, arguments.session, arguments.channels).serve()
            return 0
        if command == "hooks":
            from .hooks import handle_hook

            if externally_managed():
                emit({})
                return 0
            try:
                emit(handle_hook(home, arguments.event, _read_hook_input()))
            except (ValueError, OSError, RuntimeError) as error:
                if arguments.event in {"PreToolUse", "UserPromptSubmit"}:
                    print(f"Orchestrator blocked: {error}", file=sys.stderr)
                    return 2  # Native Claude's blocking hook error contract.
                raise
            return 0
        if command == "watch":
            if externally_managed():
                return 0
            session_id = arguments.session
            if not session_id:
                hook_input = _read_hook_input()
                from .bootstrap import verify_claude_owner

                native_id = hook_input.get("session_id")
                session_id = verify_claude_owner(home, native_id) if native_id else None
            if not session_id:
                return 0
            return watch(home, session_id, arguments.seconds)
        if command == "request":
            if arguments.payload_file:
                with arguments.payload_file.open(encoding="utf-8") as payload_file:
                    payload_text = payload_file.read(4_000_001)
                if len(payload_text) > 4_000_000:
                    raise StateError("Request payload exceeds 4 million characters")
            else:
                payload_text = arguments.payload
            emit(request(home, arguments.session, arguments.action, json.loads(payload_text)))
        elif command == "config":
            config = load_config(home, arguments.project)
            if arguments.config_command == "show":
                emit(config)
            elif arguments.config_command == "validate":
                emit(
                    {
                        "valid": True,
                        "roles": list(config["roles"]),
                        "worker_router_enabled": config["routing"]["enabled"],
                        "workers_enabled": config["workers"]["enabled"],
                        "worker_policy": "Project policy configuration and explicit selection required",
                    }
                )
            else:
                path = home / "config" / "local.toml"
                if path.exists():
                    raise StateError(
                        "Private configuration already exists; refusing to overwrite it"
                    )
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
            if command == "routing":
                emit(_routing(home, store, arguments))
            elif command in {"approve-worker", "accept-worker", "override-worker", "approve-node"}:
                from .runtime import ensure_supervisor
                from .workers import WorkerService

                workers = WorkerService(store)
                if command == "approve-worker":
                    result = workers.approve(arguments.request_id, arguments.reason)
                elif command == "accept-worker":
                    result = workers.accept(arguments.request_id, arguments.reason)
                elif command == "approve-node":
                    result = workers.approve_node(
                        arguments.plan_id, arguments.node_id, arguments.reason
                    )
                else:
                    choice = (
                        _private_choice(arguments.choice_file)
                        if arguments.choice_file
                        else json.loads(arguments.choice)
                    )
                    if not isinstance(choice, dict):
                        raise StateError("Worker choice must be a JSON object")
                    result = workers.override(arguments.request_id, choice)
                emit({"result": result, "service": ensure_supervisor(home)})
            elif command == "project":
                emit(
                    store.add_project(arguments.project_id, arguments.path)
                    if arguments.project_command == "add"
                    else {"projects": store.projects()}
                )
            elif command == "status":
                emit(
                    store.snapshot(arguments.project)
                    if arguments.project
                    else {"projects": store.projects()}
                )
            elif command == "session":
                if arguments.session_command == "open":
                    emit(
                        store.open_session(
                            arguments.id or str(uuid.uuid4()),
                            arguments.frontend,
                            arguments.project,
                            arguments.observer,
                            arguments.takeover,
                        )
                    )
                else:
                    store.close_session(arguments.session_id)
                    emit({"closed": arguments.session_id, "jobs_cancelled": False})
            elif command == "backup":
                emit({"backup": str(store.backup())})
            elif command == "approve":
                store.approve_plan(arguments.plan_id)
                from .runtime import ensure_supervisor

                plan = store.plan(arguments.plan_id)
                config = load_config(home, plan["project_id"])
                emit(
                    {
                        "approved": arguments.plan_id,
                        "workers_enabled": config["workers"]["enabled"],
                        "worker_policy": "Configured policy and monitor selection required; captain rules need separate approval",
                        "service": ensure_supervisor(home),
                    }
                )
            elif command == "resolve-hold":
                store.resolve_hold(arguments.hold_id, arguments.reason)
                emit({"resolved": arguments.hold_id})
            elif command == "graph":
                plan = store.plan(arguments.plan_id)
                emit(
                    store.graph_snapshot(
                        arguments.plan_id, load_config(home, plan["project_id"])["execution"]
                    )
                )
        return 0
    except (ValueError, OSError, RuntimeError) as error:
        emit({"error": str(error)})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
