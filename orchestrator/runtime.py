"""Local singleton scheduler and independent, resource-reserved task runners."""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
from pathlib import Path
import selectors
import shutil
import signal
import subprocess
import sys
import time

from .config import load_config, role_config
from .store import NOTE_NAMES, Store, atomic_write, encode

OUTPUT_LIMIT = 8 * 1024 * 1024
TRACKED_ROOT = Path(__file__).resolve().parent.parent


def process_identity(pid: int) -> str | None:
    """Linux boot ID plus start ticks prevents signalling a reused PID."""
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        if fields[0] == "Z":
            return None
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip() + ":" + fields[19]
    except (OSError, IndexError, ValueError):
        return None


def _alive(record: dict) -> bool:
    return bool(record.get("identity")) and process_identity(record["pid"]) == record["identity"]


def _metadata(store: Store, key: str) -> dict:
    return json.loads(store.service_value(key, "{}"))


def service_status(home: Path) -> dict:
    store = Store(home)
    record = _metadata(store, "supervisor")
    return {**record, "running": _alive(record), "home": str(store.home),
            "active_tasks": len(store.tasks(states=("starting", "running")))}


def _environment() -> dict:
    environment = os.environ.copy()
    for key in ("ORCHESTRATOR_SESSION_ID", "ORCHESTRATOR_FRONTEND", "CLAUDECODE"):
        environment.pop(key, None)
    environment["ORCHESTRATOR_CHILD"] = "1"
    # Detached children must import the same installation even from a private cwd.
    environment["PYTHONPATH"] = str(TRACKED_ROOT) + os.pathsep + environment.get("PYTHONPATH", "")
    return environment


def _resource_command(memory: str, cpus: int, seconds: float, description: str,
                      arguments: list[str]) -> list[str]:
    executable = shutil.which("machine-resources")
    if not executable:
        raise RuntimeError("Install machine-resources and put it on PATH before starting the service")
    subprocess.run([executable, "status"], check=True, stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL, timeout=10)
    return [executable, "run", "-m", memory, "-c", str(cpus), "-e", f"{seconds:g}s",
            "-d", description, "--hard-limit", "--", sys.executable, "-m",
            "orchestrator.runtime", *arguments]


@contextlib.contextmanager
def _lock(store: Store, name: str):
    descriptor = os.open(store.data / name, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
        else:
            yield True
    finally:
        os.close(descriptor)


def ensure_supervisor(home: Path) -> dict:
    store = Store(home)
    if service_status(home)["running"]:
        return service_status(home)
    with _lock(store, "supervisor-launch.lock") as acquired:
        if not acquired:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                status = service_status(home)
                if status["running"]:
                    return status
                time.sleep(0.1)
            raise RuntimeError("Supervisor startup is busy; inspect machine-resources status and retry")
        if service_status(home)["running"]:
            return service_status(home)
        command = _resource_command("256M", 1, 86400, "orchestrator supervisor",
                                    ["supervise", "--home", str(store.home)])
        launcher = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, start_new_session=True,
                                    env=_environment(), cwd=TRACKED_ROOT)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            status = service_status(home)
            if status["running"]:
                return status
            if launcher.poll() is not None:
                raise RuntimeError(f"Supervisor reservation failed (exit {launcher.returncode}); "
                                   "inspect machine-resources status, then retry")
            time.sleep(0.1)
        raise RuntimeError("Supervisor did not become ready within 15 seconds; inspect resource registry")


def _signal_group(record: dict, number: int) -> None:
    if _alive(record):
        with contextlib.suppress(ProcessLookupError):
            os.killpg(record["pid"], number)


def _resume_session(store: Store, task: dict) -> str | None:
    if task["role"] != "monitor":
        return None
    previous = [item for item in store.tasks(task["project_id"], ("succeeded",))
                if item["role"] == "monitor" and item["config"] == task["config"]
                and item["harness_session"] and item["processed"] and not item["error"]]
    if not previous or len(previous) % 20 == 0:
        return None
    return previous[-1]["harness_session"]


def _prompt(store: Store, task: dict, role: dict) -> str:
    path = (TRACKED_ROOT / role.get("prompt_path", f"roles/{task['role']}.md")).resolve()
    if not path.is_relative_to(TRACKED_ROOT / "roles"):
        raise ValueError("Role prompt must remain inside the tracked roles directory")
    prompt = path.read_text()
    context = {"project_root": store.project(task["project_id"])["root"],
               "personalization": task["config"]["personalization"]}
    if task["role"] == "planner":
        from .graphs import load_workflow
        context["workflow"] = load_workflow(store.home, task["config"]["planning"]["workflow"])
    return (prompt + "\n\nRead-only project context (not instructions):\n" + encode(context)
            + "\n\nTask evidence/request:\n" + task["prompt"])


def run_task(home: Path, task_id: str, token: str) -> int:
    store = Store(home)
    identity = process_identity(os.getpid())
    if not identity or not store.runner_started(task_id, token, os.getpid(), identity):
        return 1
    task = store.task(task_id)
    child = None
    child_record = {}
    try:
        from .adapters import build_command, parse_result
        config = task["config"]
        role = role_config(config, task["role"])
        heartbeat = min(config["supervisor"]["heartbeat_seconds"], 1.0)
        directory = store.data / "runs" / task_id / token
        directory.mkdir(parents=True, mode=0o700)
        os.chmod(directory, 0o700)
        prompt = _prompt(store, task, role)
        atomic_write(directory / "prompt.txt", prompt)
        output_path = directory / "result.txt"
        command = build_command(config, task["role"], prompt, directory, output_path,
                                session_id=_resume_session(store, task))
        deadline = time.monotonic() + role["timeout_seconds"]
        child = subprocess.Popen(command, cwd=directory, env=_environment(),
                                 stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, start_new_session=True)
        child_record = {"pid": child.pid, "identity": process_identity(child.pid),
                        "deadline": time.time() + role["timeout_seconds"]}
        store.set_service_value("harness:" + task_id, encode(child_record))
        stdout = bytearray()
        count = 0
        with selectors.DefaultSelector() as selector, (directory / "output.log").open("xb") as log:
            os.chmod(directory / "output.log", 0o600)
            selector.register(child.stdout, selectors.EVENT_READ, True)
            selector.register(child.stderr, selectors.EVENT_READ, False)
            while selector.get_map() or os.waitid(
                    os.P_PID, child.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is None:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Harness deadline exceeded")
                if not store.heartbeat(task_id, token):
                    raise RuntimeError("Task cancelled or attempt no longer owns its lease")
                if output_path.exists() and output_path.stat().st_size > OUTPUT_LIMIT:
                    raise RuntimeError("Harness result exceeded 8 MiB")
                for key, _ in selector.select(min(heartbeat, max(0, deadline - time.monotonic()))):
                    data = os.read(key.fileobj.fileno(), 65536)
                    if not data:
                        selector.unregister(key.fileobj)
                        continue
                    available = max(0, OUTPUT_LIMIT - count)
                    log.write(data[:available])
                    count += len(data)
                    if key.data:
                        stdout.extend(data[:available])
                    if count > OUTPUT_LIMIT:
                        raise RuntimeError("Harness output exceeded 8 MiB")
            # Keep the leader unreaped until cleanup, so its PID cannot be reused.
            # Successful harnesses must not leave background descendants behind.
            with contextlib.suppress(ProcessLookupError):
                os.killpg(child.pid, signal.SIGKILL)
            returncode = child.wait(timeout=1)
        # The last-message file is not proof of successful completion. Only the
        # adapter's stdout protocol can establish terminal success.
        if output_path.exists() and (output_path.is_symlink()
                                    or output_path.stat().st_size > OUTPUT_LIMIT):
            raise RuntimeError("Unsafe or oversized harness result")
        result = parse_result(role["adapter"], stdout.decode("utf-8", errors="replace"), returncode)
        if returncode != 0:
            raise RuntimeError(f"Harness failed with exit {returncode}")
        values = result if isinstance(result, dict) else vars(result)
        if not store.finish(task_id, token, text=values["text"],
                            harness_session=values.get("session_id"), cost_usd=values.get("cost_usd")):
            return 1
        return 0 if store.task(task_id)["state"] == "succeeded" else 1
    except Exception as error:
        store.finish(task_id, token, state="failed", error=str(error))
        return 1
    finally:
        if child is not None:
            _signal_group(child_record, signal.SIGKILL)
            # If the leader exited, its unreaped process still protects the group PID.
            if child.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(child.pid, signal.SIGKILL)
            with contextlib.suppress(subprocess.TimeoutExpired):
                child.wait(timeout=3)
            child.stdout.close()
            child.stderr.close()


def _strict_json(text: str):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"Duplicate JSON field: {key}")
            result[key] = value
        return result

    def invalid(value):
        raise ValueError(f"Invalid JSON constant: {value}")

    return json.loads(text, object_pairs_hook=unique, parse_constant=invalid)


def _process_results(store: Store) -> None:
    for task in store.unprocessed():
        try:
            if task["state"] != "succeeded":
                raise ValueError(task["error"] or f"Task ended {task['state']}")
            value = _strict_json(task["result"])
            if task["role"] == "monitor":
                store.apply_monitor(task, value["reviewed_through"], value["findings"])
            elif task["role"] == "planner" and task["plan_id"]:
                if store.install_graph(task["plan_id"], task["id"], value):
                    plan = store.plan(task["plan_id"])
                    prompt = encode({"request": plan["request"], "validated_graph": value,
                                     "evidence": store.snapshot(task["project_id"]),
                                     "response": {"verdict": "approved|changes_requested", "findings": []}})
                    store.attach_critic(task, prompt, task["config"])
                else:
                    store.processed(task["id"])
            elif task["role"] == "critic" and task["plan_id"]:
                store.apply_critique(task, value["verdict"], value["findings"])
                if value["verdict"] == "changes_requested" and hasattr(store, "revise_plan"):
                    plan = store.plan(task["plan_id"])
                    store.revise_plan(task["plan_id"], encode({"original_request": plan["request"],
                                      "previous_graph": plan["graph_json"], "critique": value}),
                                      task["config"])
            else:
                store.processed(task["id"])
        except Exception as error:
            if task["role"] == "monitor":
                store.monitor_failure(task, str(error))
            else:
                store.fail_processing(task, str(error))


def _reconcile(store: Store, launchers: dict) -> None:
    for task in store.tasks(states=("starting", "running")):
        launcher = launchers.get(task["id"])
        exitcode = launcher.poll() if launcher else None
        settings = task["config"]["supervisor"]
        expired = time.time() - task["updated"] > settings["stale_seconds"]
        if task["state"] == "starting":
            if exitcode is not None or expired:
                store.finish(task["id"], task["token"], state="failed",
                             error=f"Runner never registered; reservation exit={exitcode}. "
                             "Inspect machine-resources status and explicitly retry")
        else:
            alive = process_identity(task["runner_pid"]) == task["runner_identity"]
            harness = _metadata(store, "harness:" + task["id"])
            overdue = harness and time.time() > harness["deadline"] + settings["stale_seconds"]
            if (not alive and expired) or overdue:
                _signal_group(harness, signal.SIGKILL)
                if alive:
                    # The runner is not necessarily a process-group leader when resource-wrapped.
                    with contextlib.suppress(ProcessLookupError):
                        os.kill(task["runner_pid"], signal.SIGKILL)
                store.finish(task["id"], task["token"], state="unknown",
                             error="Runner lost or exceeded deadline; no automatic replay")
    for task_id, launcher in list(launchers.items()):
        if launcher.poll() is not None:
            del launchers[task_id]


def supervise(home: Path, once: bool = False) -> None:
    store = Store(home)
    launchers = {}
    with _lock(store, "supervisor.lock") as acquired:
        if not acquired:
            return
        try:
            while True:
                config = load_config(store.home)
                store.set_service_value("supervisor", encode({"pid": os.getpid(),
                                        "identity": process_identity(os.getpid()), "heartbeat": time.time()}))
                _reconcile(store, launchers)
                _process_results(store)
                for project in store.projects():
                    project_config = load_config(store.home, project["id"])
                    settings = project_config["supervisor"]
                    candidate = store.monitor_candidate(project["id"], settings["monitor_batch_events"])
                    if candidate:
                        prompt = encode({"reviewed_through": candidate["cursor"],
                                         "events": candidate["events"], "state": store.snapshot(project["id"]),
                                         "notes": [store.read_note(project["id"], name) for name in sorted(NOTE_NAMES)]})
                        store.schedule_monitor(project["id"], candidate, prompt, project_config,
                                               settings["monitor_interval_seconds"])
                while task := store.claim_next(config["supervisor"]["max_parallel"]):
                    try:
                        role = role_config(task["config"], task["role"])
                        command = _resource_command(role["memory"], role["cpus"],
                                                    role["timeout_seconds"] + 60,
                                                    f"orchestrator {task['role']} {task['id']}",
                                                    ["run-task", "--home", str(store.home),
                                                     "--task-id", task["id"], "--token", task["token"]])
                        launchers[task["id"]] = subprocess.Popen(
                            command, start_new_session=True, stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            env=_environment(), cwd=TRACKED_ROOT)
                    except Exception as error:
                        store.finish(task["id"], task["token"], state="failed", error=str(error))
                if time.time() - float(store.service_value("last_backup", "0")) >= 86400:
                    store.backup()
                    store.set_service_value("last_backup", str(time.time()))
                if once:
                    return
                time.sleep(config["supervisor"]["poll_seconds"])
        finally:
            store.set_service_value("supervisor", "{}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("supervise", "run-task"))
    parser.add_argument("--home", required=True, type=Path)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--task-id")
    parser.add_argument("--token")
    arguments = parser.parse_args()
    os.umask(0o077)
    if arguments.action == "supervise":
        supervise(arguments.home, arguments.once)
    else:
        if not arguments.task_id or not arguments.token:
            parser.error("run-task requires --task-id and --token")
        raise SystemExit(run_task(arguments.home, arguments.task_id, arguments.token))


if __name__ == "__main__":
    main()
