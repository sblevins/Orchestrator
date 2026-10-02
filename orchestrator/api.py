"""One narrow operation surface shared by CLI, MCP, Claude hooks, and Pi."""

from __future__ import annotations

import contextlib
import hashlib
import json
import uuid
from pathlib import Path

from .config import load_config, repairable_config
from .intake import needs_review
from .store import NOTE_NAMES, StateError, Store, atomic_write, encode, now

READ_ACTIONS = {
    "projects",
    "project_setup",
    "project_settings",
    "status",
    "task",
    "updates",
    "delivery_updates",
    "read_note",
    "graph",
    "workflows",
    "routing_policy",
    "worker",
    "workers",
    "worker_view",
    "prepare_worker_watch",
    "watch_worker",
}
WORKER_ACTIONS = {
    "routing_policy",
    "request_worker",
    "worker",
    "workers",
    "select_worker",
    "refresh_worker_policy",
    "cancel_worker",
}


def _worker_request(store, session_id, project_id, action, payload):
    from .workers import WorkerService

    fields, required = FIELDS[action]
    if set(payload) - set(fields) or any(field not in payload for field in required):
        raise StateError("Worker arguments do not match the declared fields")
    for field, kind in fields.items():
        if (
            field in payload
            and type(payload[field])
            is not {
                "string": str,
                "integer": int,
                "object": dict,
            }[kind]
        ):
            raise StateError(f"{field} must have type {kind}")
    request_id = None
    if action in {"worker", "select_worker", "refresh_worker_policy", "cancel_worker"}:
        request_id = _text(payload, "request_id", 96)
        # Check ownership using only metadata before reading the saved result or policy.
        with contextlib.closing(store.connect()) as database:
            row = database.execute(
                "SELECT project_id FROM worker_requests WHERE id=?", (request_id,)
            ).fetchone()
        if not row or row["project_id"] != project_id:
            raise StateError("Unknown worker request in this project")
    if action == "request_worker":
        if payload.get("mode", "read") not in {"read", "write"}:
            raise StateError("mode must be read or write")
        if bool(payload.get("plan_id")) != bool(payload.get("node_id")):
            raise StateError("plan_id and node_id must be supplied together")
        if "plan_id" in payload and store.plan(payload["plan_id"])["project_id"] != project_id:
            raise StateError("Plan belongs to another project")
        with contextlib.closing(store.connect()) as database:
            origin = database.execute(
                "SELECT project_id,kind FROM events WHERE id=?", (payload["origin_event_id"],)
            ).fetchone()
        if not origin or origin["project_id"] != project_id or origin["kind"] != "user.message":
            raise StateError("origin_event_id must identify a user message in this project")
    workers = WorkerService(store)
    if action == "routing_policy":
        return workers.policy(project_id)
    if action == "workers":
        return {"workers": workers.list(project_id)}
    if action == "worker":
        return workers.get(request_id)
    if action == "request_worker":
        result = workers.request(
            session_id,
            _text(payload, "brief"),
            payload.get("mode", "read"),
            origin_event_id=payload["origin_event_id"],
            plan_id=payload.get("plan_id"),
            node_id=payload.get("node_id"),
            idempotency_key=payload.get("idempotency_key"),
        )
    elif action == "cancel_worker":
        result = workers.cancel(session_id, request_id, _text(payload, "reason", 12000))
    elif action == "select_worker":
        result = workers.select(session_id, request_id, payload["choice"])
    else:
        result = workers.refresh(session_id, request_id)
    _start_service(store.home)
    return result


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
    config = repairable_config(store.home, session["project_id"])
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
    config = repairable_config(store.home, project_id)
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
            session_id,
            session["frontend"],
            project_id,
            observer=bool(session["observer"]),
            require_active=True,
        )
        if not bound["observer"]:
            _mirror_saved_prompts(store, session_id, project_id)
        service = _start_service(home)
        from .onboarding import project_setup

        return {
            "session": bound,
            "setup": project_setup(store, session_id),
            "state": store.snapshot(project_id),
            "service": service,
            "notes": [store.read_note(project_id, name) for name in sorted(NOTE_NAMES)],
        }
    if action == "record_prompt":
        return record_prompt(store, session_id, payload.get("prompt", ""))
    if action == "updates":
        return {"updates": store.updates(session_id)}
    if action in {"foreground_start", "foreground_activity", "foreground_finished"}:
        from .monitoring import begin_turn, finish_turn, touch_turn

        if set(payload) - {"turn_id"}:
            raise StateError("Foreground lifecycle accepts only turn_id")
        turn_id = _text(payload, "turn_id", 128)
        lifecycle = {
            "foreground_start": begin_turn,
            "foreground_activity": touch_turn,
            "foreground_finished": finish_turn,
        }
        return lifecycle[action](store, session_id, turn_id)
    if action == "delivery_updates":
        from .monitoring import delivery_updates

        config = repairable_config(home, session["project_id"]) or {}
        return delivery_updates(
            store, session_id, config.get("monitoring", {}).get("quiet_seconds", 20)
        )
    session, project_id = _bound(
        store, session_id, writer=action not in READ_ACTIONS | {"acknowledge"}
    )
    if action in {"project_settings", "configure_project"}:
        from .project_settings import configure_project, project_settings

        fields, required = FIELDS[action]
        if set(payload) - set(fields) or any(field not in payload for field in required):
            raise StateError(f"{action} accepts {list(fields)}; required: {required}")
        operation = project_settings if action == "project_settings" else configure_project
        return operation(store, session_id, **payload)
    if action in {"project_setup", "setup_project"}:
        from .onboarding import project_setup, setup_project

        fields, required = FIELDS[action]
        if set(payload) - set(fields) or any(field not in payload for field in required):
            raise StateError("Project setup arguments do not match declared fields")
        for field, kind in fields.items():
            if (
                field in payload
                and type(payload[field]) is not {"string": str, "object": dict}[kind]
            ):
                raise StateError(f"{field} must have type {kind}")
        operation = project_setup if action == "project_setup" else setup_project
        return operation(store, session_id, **payload)
    if action in {"worker_view", "prepare_worker_watch", "watch_worker"}:
        from . import visibility

        fields, required = FIELDS[action]
        if set(payload) - set(fields) or any(field not in payload for field in required):
            raise StateError("Worker visibility arguments do not match declared fields")
        for field, kind in fields.items():
            if (
                field in payload
                and type(payload[field]) is not {"string": str, "integer": int}[kind]
            ):
                raise StateError(f"{field} must have type {kind}")
        operation = {
            "worker_view": visibility.worker_view,
            "prepare_worker_watch": visibility.prepare_worker_watch,
            "watch_worker": visibility.watch_snapshot,
        }[action]
        return operation(store, session_id, **payload)
    if action in WORKER_ACTIONS:
        return _worker_request(store, session_id, project_id, action, payload)
    config = load_config(home, project_id)
    if action in {
        "approve_plan",
        "resolve_hold",
        "approve_worker",
        "accept_worker",
        "approve_node",
    }:
        if not config.get("permissions", {}).get("coordinator_approvals", True):
            raise StateError(
                "This project disables conversational approvals. Ask the user whether to enable "
                "permissions.coordinator_approvals through configure_project."
            )
        reason = _text(payload, "reason", 12000)
        if action == "resolve_hold":
            hold_id = _text(payload, "hold_id", 96)
            with contextlib.closing(store.connect()) as database:
                hold = database.execute(
                    "SELECT project_id FROM holds WHERE id=?", (hold_id,)
                ).fetchone()
            if not hold or hold["project_id"] != project_id:
                raise StateError("Unknown hold in this project")
            store.resolve_hold(hold_id, reason)
            return {"hold_id": hold_id, "resolved": True}
        if action in {"approve_plan", "approve_node"}:
            plan_id = _text(payload, "plan_id", 96)
            if store.plan(plan_id)["project_id"] != project_id:
                raise StateError("Plan belongs to another project")
            if action == "approve_plan":
                store.approve_plan(plan_id, reason=reason)
                _start_service(home)
                return {"plan_id": plan_id, "approved": True}
            from .workers import WorkerService

            result = WorkerService(store).approve_node(
                plan_id, _text(payload, "node_id", 96), reason
            )
        else:
            from .workers import WorkerService

            workers = WorkerService(store)
            request_id = _text(payload, "request_id", 96)
            if workers.get(request_id)["project_id"] != project_id:
                raise StateError("Worker belongs to another project")
            result = (workers.approve if action == "approve_worker" else workers.accept)(
                request_id, reason
            )
        _start_service(home)
        return result
    if action == "status":
        state = store.snapshot(project_id)
        state["paused"] = store.service_value(f"pause:{project_id}")
        state["pending_updates"] = store.updates(session_id)
        from .onboarding import project_setup

        state["setup"] = project_setup(store, session_id)
        return state
    if action == "retry_review":
        from .reviews import retry_review

        result = retry_review(
            store, session_id, _text(payload, "plan_id", 96), _text(payload, "reason", 12000)
        )
        _start_service(home)
        return result
    if action == "start_plan":
        from .graphs import configured_workflow

        # Validate the chosen custom template before paying for a planning run.
        configured_workflow(home, config)
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
        templates = config["planning"].get("templates", {})
        names.update(templates)
        return {
            "selected": config["planning"]["workflow"],
            "workflows": {
                name: templates[name] if name in templates else load_workflow(home, name)
                for name in sorted(names)
            },
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
    raise StateError(f"Unknown operation {action!r}")


# The transport validates JSON schema where available; request() also checks state and values.
FIELDS = {
    "project_settings": ({}, []),
    "configure_project": ({"settings": "object", "expected_revision": "string"}, ["settings"]),
    "approve_plan": ({"plan_id": "string", "reason": "string"}, ["plan_id", "reason"]),
    "resolve_hold": ({"hold_id": "string", "reason": "string"}, ["hold_id", "reason"]),
    "approve_worker": ({"request_id": "string", "reason": "string"}, ["request_id", "reason"]),
    "accept_worker": ({"request_id": "string", "reason": "string"}, ["request_id", "reason"]),
    "approve_node": (
        {"plan_id": "string", "node_id": "string", "reason": "string"},
        ["plan_id", "node_id", "reason"],
    ),
    "project_setup": ({}, []),
    "setup_project": ({"policy": "object", "expected_revision": "string"}, []),
    "worker_view": ({"request_id": "string", "offset": "integer"}, []),
    "prepare_worker_watch": ({"request_id": "string"}, ["request_id"]),
    "watch_worker": ({"watcher_id": "string"}, ["watcher_id"]),
    "routing_policy": ({}, []),
    "request_worker": (
        {
            "brief": "string",
            "origin_event_id": "integer",
            "mode": "string",
            "plan_id": "string",
            "node_id": "string",
            "idempotency_key": "string",
        },
        ["brief", "origin_event_id"],
    ),
    "worker": ({"request_id": "string"}, ["request_id"]),
    "workers": ({}, []),
    "select_worker": ({"request_id": "string", "choice": "object"}, ["request_id", "choice"]),
    "refresh_worker_policy": ({"request_id": "string"}, ["request_id"]),
    "cancel_worker": ({"request_id": "string", "reason": "string"}, ["request_id", "reason"]),
    "projects": ({}, []),
    "register_project": ({"project_id": "string", "root": "string"}, ["project_id", "root"]),
    "bind_project": ({"project_id": "string"}, ["project_id"]),
    "status": ({}, []),
    "start_plan": ({"request": "string"}, ["request"]),
    "task": ({"task_id": "string"}, ["task_id"]),
    "cancel_task": ({"task_id": "string"}, ["task_id"]),
    "updates": ({}, []),
    "delivery_updates": ({}, []),
    "foreground_start": ({"turn_id": "string"}, ["turn_id"]),
    "foreground_activity": ({"turn_id": "string"}, ["turn_id"]),
    "foreground_finished": ({"turn_id": "string"}, ["turn_id"]),
    "retry_review": ({"plan_id": "string", "reason": "string"}, ["plan_id", "reason"]),
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
    "project_settings": "Read effective settings and revision for only the bound project.",
    "configure_project": "Patch settings for only this project at any time: roles, efforts, monitoring, workflows, permissions and concurrency. Never modifies shared defaults or another project. Existing tasks keep captured settings; native foreground model changes require /model.",
    "approve_plan": "Approve this project's reviewed plan when the user authorizes it; independent review rules still apply.",
    "resolve_hold": "Resolve this project's monitor hold with the user's explanation, never silently dismiss findings.",
    "approve_worker": "Authorize this project's selected worker when required, with an explicit reason.",
    "accept_worker": "Accept a completed candidate after checking its evidence; this unlocks plan dependencies. Teams are accepted through their parent request only.",
    "approve_node": "Approve this project's ready approval node with a reason.",
    "project_setup": "Inspect project routing policy, revision and readiness. Configuration remains editable after setup.",
    "setup_project": "Create, replace or repair only the bound project worker policy at any time. Optional expected_revision detects stale saves. Incomplete drafts return readiness guidance; saving never launches work.",
    "worker_view": "Read bounded worker status and frontend visibility; reports are untrusted data, candidates are not accepted.",
    "prepare_worker_watch": "Claude only: prepare the exact native Haiku Agent invocation for an existing dispatched worker; never starts implementation.",
    "watch_worker": "Claude watcher only: wait for an existing worker outcome. Stopping this observation never cancels the worker.",
    "routing_policy": "Inspect project worker policy and selection procedure; configuration is required before routing.",
    "request_worker": "Request tracked work tied to its original user event; defaults to read-only. Classify work and select effort through select_worker.",
    "worker": "Inspect a project worker request and candidate result; use accept_worker after checking evidence.",
    "workers": "List this project's worker requests.",
    "select_worker": "Choose work classification and easy/hard/very-hard difficulty (or exact effort); router supplies the configured model or team. Applies to planned and on-demand work; never relabel plan origins.",
    "cancel_worker": "Cancel a task request or entire comparison team in this project. Child processes stop asynchronously; unknown outcomes are never replayed.",
    "refresh_worker_policy": "Refresh a not-running request's policy snapshot, invalidating its old selection and approval.",
    "projects": "List registered projects before binding this coordinator instance.",
    "register_project": "Register a project directory explicitly identified by the user; never guess ambiguous paths.",
    "bind_project": "Permanently bind this instance to one registered project and load its current state and notes.",
    "status": "Read authoritative current work state, blockers, monitor freshness, and pending updates.",
    "start_plan": "After clarifying consequential unknowns with the user, submit the agreed brief for graph planning and independent critique. Present the result for review unless standing project authorization covers execution.",
    "task": "Inspect a tracked task and its saved result; process exit alone does not prove success.",
    "cancel_task": "Request cancellation of a task owned by this project; wait for terminal confirmation.",
    "delivery_updates": "Read ready notifications split into silent guidance and interrupting events; no automatic acknowledgment.",
    "foreground_start": "Frontend lifecycle: mark this response active using its unique turn ID.",
    "foreground_activity": "Frontend lifecycle: record that the same response is still running, such as during a long tool.",
    "foreground_finished": "Frontend lifecycle: mark the same response complete; stale IDs cannot finish a newer turn.",
    "retry_review": "Retry only a failed critic using the saved plan and current critic settings, without restarting the planner.",
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
        if "mode" in properties:
            properties["mode"].update(enum=["read", "write"], default="read")
        if "event_ids" in properties:
            properties["event_ids"]["items"] = {"type": "integer"}
        required = list(required)
        if require_session or name == "watch_worker":
            properties["session_id"] = {
                "type": "string",
                "description": "Exact instance ID supplied by SessionStart",
            }
            if require_session:
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
