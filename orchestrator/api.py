"""One narrow operation surface shared by CLI, MCP, Claude hooks, and Pi."""

from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path

from .config import load_config
from .intake import needs_review
from .store import NOTE_NAMES, StateError, Store, atomic_write, encode, now

READ_ACTIONS = {"projects", "status", "task", "updates", "read_note", "graph", "workflows"}


def _text(payload, field, maximum=100000):
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise StateError(f"{field} must be nonempty text of at most {maximum} characters")
    return value


def _start_service(home):
    from .runtime import ensure_supervisor

    return ensure_supervisor(home)


def record_prompt(store: Store, session_id: str, prompt: str) -> dict:
    session = store.session(session_id)
    if not isinstance(prompt, str) or len(prompt.encode()) > 2_000_000:
        raise StateError("Prompt must be text of at most 2 MB")
    prompt_id = str(uuid.uuid4())
    document = {"id": prompt_id, "session_id": session_id, "prompt": prompt, "created": now()}
    path = store.data / "sessions" / session_id / "prompts" / f"{prompt_id}.json"
    atomic_write(path, encode(document))
    config = load_config(store.home, session["project_id"])
    important = needs_review(prompt, config)
    result = {"prompt_id": prompt_id, "review_required": important, "path": str(path)}
    if session["project_id"] and session["active"] and not session["observer"]:
        result["event_id"] = _mirror_prompt(store, session_id, document, path, config)
    store.set_service_value(f"last_prompt_review:{session_id}", "true" if important else "false")
    return result


def _mirror_prompt(store, session_id, document, path, config):
    prompt = document["prompt"]
    return store.record(
        session_id,
        "user.message",
        {
            "prompt": prompt[:12000],
            "prompt_id": document["id"],
            "full_prompt_path": str(path),
            "truncated": len(prompt) > 12000,
            "sha256": hashlib.sha256(prompt.encode()).hexdigest(),
        },
        review_required=needs_review(prompt, config),
    )


def _mirror_saved_prompts(store, session_id, project_id):
    config = load_config(store.home, project_id)
    directory = store.data / "sessions" / session_id / "prompts"
    saved = [(path, json.loads(path.read_text())) for path in directory.glob("*.json")]
    for path, document in sorted(saved, key=lambda item: (item[1]["created"], item[1]["id"])):
        if document["session_id"] != session_id:
            raise StateError("Saved prompt belongs to another session")
        _mirror_prompt(store, session_id, document, path, config)


def _bound(store, session_id, *, writer=False):
    session = store.require_writer(session_id) if writer else store.session(session_id)
    if not session["project_id"]:
        raise StateError("Select a project first with bind_project")
    if not session["active"]:
        raise StateError(
            "This session no longer owns an active frontend; start or resume explicitly"
        )
    return session, session["project_id"]


def _project_task(store, project_id, task_id):
    task = store.task(task_id)
    if task["project_id"] != project_id:
        raise StateError("Task belongs to another project")
    return task


def request(home: Path, session_id: str, action: str, payload: dict | None = None) -> dict:
    payload = {} if payload is None else payload
    if not isinstance(payload, dict):
        raise StateError("Operation payload must be an object")
    store = Store(home)
    session = store.session(session_id)
    if action == "projects":
        return {"projects": store.projects()}
    if action == "register_project":
        if session["observer"]:
            raise StateError("An observer cannot register projects")
        return store.add_project(_text(payload, "project_id", 96), _text(payload, "root", 4096))
    if action == "bind_project":
        project_id = _text(payload, "project_id", 96)
        # Model-facing tools never take over another coordinator's authority.
        if payload.get("takeover"):
            raise StateError("Takeover is an operator-only startup option")
        bound = store.open_session(
            session_id, session["frontend"], project_id, observer=bool(session["observer"])
        )
        if not bound["observer"]:
            _mirror_saved_prompts(store, session_id, project_id)
        service = _start_service(home)
        return {
            "session": bound,
            "state": store.snapshot(project_id),
            "service": service,
            "notes": [store.read_note(project_id, name) for name in sorted(NOTE_NAMES)],
        }
    if action == "record_prompt":
        return record_prompt(store, session_id, payload.get("prompt", ""))
    if action == "updates":
        return {"updates": store.updates(session_id)}
    session, project_id = _bound(
        store, session_id, writer=action not in READ_ACTIONS | {"acknowledge"}
    )
    config = load_config(home, project_id)
    if action == "status":
        state = store.snapshot(project_id)
        state["paused"] = store.service_value(f"pause:{project_id}")
        state["pending_updates"] = store.updates(session_id)
        return state
    if action == "start_plan":
        from .graphs import load_workflow

        # Validate the chosen custom template before paying for a planning run.
        load_workflow(home, config["planning"]["workflow"])
        service = _start_service(home)
        plan = store.create_plan(session_id, _text(payload, "request"), config)
        return {"plan": plan, "service": service, "execution_authorized": False}
    if action == "task":
        task = _project_task(store, project_id, _text(payload, "task_id", 96))
        return {
            key: value for key, value in task.items() if key not in {"token", "config", "prompt"}
        }
    if action == "cancel_task":
        task_id = _text(payload, "task_id", 96)
        _project_task(store, project_id, task_id)
        store.cancel(session_id, task_id)
        return {"task_id": task_id, "cancel_requested": True}
    if action == "acknowledge":
        event_ids = payload.get("event_ids")
        store.acknowledge(session_id, event_ids)
        return {"acknowledged": event_ids}
    if action == "record_decision":
        summary = _text(payload, "summary", 12000)
        rationale = payload.get("rationale", "")
        if not isinstance(rationale, str) or len(rationale) > 12000:
            raise StateError("rationale must be text of at most 12000 characters")
        kind = "scope.changed" if payload.get("scope_change") else "decision.recorded"
        event_id = store.record(session_id, kind, {"summary": summary, "rationale": rationale})
        return {"event_id": event_id, "review_pending": True}
    if action == "read_note":
        return store.read_note(project_id, _text(payload, "name", 64))
    if action == "write_note":
        text = payload.get("text")
        return store.write_note(
            session_id, _text(payload, "name", 64), text, _text(payload, "expected_revision", 64)
        )
    if action == "graph":
        plan_id = _text(payload, "plan_id", 96)
        if store.plan(plan_id)["project_id"] != project_id:
            raise StateError("Plan belongs to another project")
        return store.graph_snapshot(plan_id, config["execution"])
    if action == "workflows":
        from .graphs import load_workflow

        directory = Path(__file__).resolve().parent.parent / "workflows"
        names = {path.stem for path in directory.glob("*.json")}
        names.update(path.stem for path in (Path(home) / "config" / "workflows").glob("*.json"))
        return {
            "selected": config["planning"]["workflow"],
            "workflows": {name: load_workflow(home, name) for name in sorted(names)},
        }
    if action == "request_review":
        reason = _text(payload, "reason", 12000)
        event_id = store.record(
            session_id, "decision.recorded", {"explicit_review_request": reason}
        )
        # Do not reset a circuit-breaker's cooldown or overlap an existing monitor.
        service = _start_service(home)
        return {"event_id": event_id, "review_pending": True, "service": service}
    if action == "pause_project":
        store.pause(project_id, _text(payload, "reason", 12000))
        return {"paused": True, "running_tasks_continue": True, "monitor_continues": True}
    if action == "resume_project":
        store.resume(project_id)
        return {"paused": False}
    raise StateError(f"Unknown operation {action!r}; worker routing remains disabled")


# The transport validates JSON schema where available; request() also checks state and values.
FIELDS = {
    "projects": ({}, []),
    "register_project": ({"project_id": "string", "root": "string"}, ["project_id", "root"]),
    "bind_project": ({"project_id": "string"}, ["project_id"]),
    "status": ({}, []),
    "start_plan": ({"request": "string"}, ["request"]),
    "task": ({"task_id": "string"}, ["task_id"]),
    "cancel_task": ({"task_id": "string"}, ["task_id"]),
    "updates": ({}, []),
    "acknowledge": ({"event_ids": "array"}, ["event_ids"]),
    "record_decision": (
        {"summary": "string", "rationale": "string", "scope_change": "boolean"},
        ["summary"],
    ),
    "read_note": ({"name": "string"}, ["name"]),
    "write_note": (
        {"name": "string", "text": "string", "expected_revision": "string"},
        ["name", "text", "expected_revision"],
    ),
    "graph": ({"plan_id": "string"}, ["plan_id"]),
    "workflows": ({}, []),
    "request_review": ({"reason": "string"}, ["reason"]),
    "pause_project": ({"reason": "string"}, ["reason"]),
    "resume_project": ({}, []),
}
DESCRIPTIONS = {
    "projects": "List registered projects before binding this coordinator instance.",
    "register_project": "Register a project directory explicitly identified by the user; never guess ambiguous paths.",
    "bind_project": "Permanently bind this instance to one registered project and load its current state and notes.",
    "status": "Read authoritative current work state, blockers, monitor freshness, and pending updates.",
    "start_plan": "Start tracked graph planning and independent critique, without authorizing implementation.",
    "task": "Inspect a tracked task and its saved result; process exit alone does not prove success.",
    "cancel_task": "Request cancellation of a task owned by this project; wait for terminal confirmation.",
    "updates": "Read unacknowledged results and monitor feedback without removing them.",
    "acknowledge": "Acknowledge exact event IDs only after handling them; acknowledgment does not resolve safety holds.",
    "record_decision": "Record a consequential decision, evidence rationale, or scope change for independent monitoring.",
    "read_note": "Read a private project knowledge note and its revision for safe updates.",
    "write_note": "Save sourced project knowledge only if its expected revision still matches; no runtime status notes.",
    "graph": "Inspect the validated dependency graph, node states, readiness, and dispatch blockers.",
    "workflows": "Inspect customizable workflow graph templates used to guide planning.",
    "request_review": "Ask the slow monitor to inspect an issue; cannot bypass its failure cooldown.",
    "pause_project": "Pause new planning/review launches, not running processes or monitor observations.",
    "resume_project": "Resume queued project work without clearing review or approval holds.",
}


def tool_definitions(require_session: bool) -> list[dict]:
    tools = []
    for name, (fields, required) in FIELDS.items():
        properties = {field: {"type": kind} for field, kind in fields.items()}
        if "event_ids" in properties:
            properties["event_ids"]["items"] = {"type": "integer"}
        required = list(required)
        if require_session:
            properties["session_id"] = {
                "type": "string",
                "description": "Exact instance ID supplied by SessionStart",
            }
            required.append("session_id")
        tools.append(
            {
                "name": name,
                "description": DESCRIPTIONS[name],
                "inputSchema": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                    "additionalProperties": False,
                },
                "annotations": {
                    "readOnlyHint": name in READ_ACTIONS,
                    "destructiveHint": name not in READ_ACTIONS,
                    "openWorldHint": name in {"start_plan", "bind_project", "request_review"},
                },
            }
        )
    return tools
