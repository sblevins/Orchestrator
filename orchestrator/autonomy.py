"""Standing authorization advances existing gates, never creates worker approval."""

import contextlib
import hashlib

from .config import load_config
from .store import StateError, encode
from .teams import member
from .workers import WorkerService

REASON = "Standing project authorization (execution.unattended)"


def _enabled(store, project_id):
    return load_config(store.home, project_id).get("execution", {}).get("unattended") is True


def _waiting(store, project_id, reference, reason, state=None, attention=False):
    """Persist only distinct waits; these must not invalidate monitor freshness."""
    try:
        config = load_config(store.home, project_id)
    except (ValueError, OSError) as error:
        config = {"error": str(error)}
    fingerprint = hashlib.sha256(encode([state, config, reason]).encode()).hexdigest()
    key = "autonomy.waiting:" + project_id + ":" + encode(reference)
    with store.transaction() as database:
        previous = database.execute("SELECT value FROM service WHERE key=?", (key,)).fetchone()
        if previous and previous[0] == fingerprint:
            return
        # Attention notifications are once per issue report, not per config change.
        attention_key = key + ":attention:" + hashlib.sha256(reason.encode()).hexdigest()
        notified = database.execute(
            "SELECT 1 FROM service WHERE key=?", (attention_key,)
        ).fetchone()
        store._event(
            database,
            project_id,
            "autonomy.waiting",
            {**reference, "reason": reason},
            notify=attention and not notified,
            review_required=False,
        )
        database.execute(
            "INSERT INTO service VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, fingerprint),
        )
        if attention and not notified:
            database.execute("INSERT INTO service VALUES(?,?)", (attention_key, "notified"))


def _advance_project(store, project_id):
    if not _enabled(store, project_id):
        return
    with contextlib.closing(store.connect()) as database:
        latest = database.execute(
            "SELECT * FROM plans WHERE project_id=? ORDER BY version DESC LIMIT 1", (project_id,)
        ).fetchone()
    if latest and latest["status"] == "reviewed":
        try:
            if _enabled(store, project_id):
                if store.service_value("pause:" + project_id):
                    raise StateError("Project is paused")
                store.approve_plan(latest["id"], REASON)
        except (ValueError, OSError) as error:
            _waiting(store, project_id, {"plan_id": latest["id"]}, str(error), dict(latest))

    service = WorkerService(store)
    for snapshot in service.list(project_id):
        if snapshot["state"] != "candidate":
            continue
        try:
            # Reload both authorization and result for every individual transition.
            if not _enabled(store, project_id):
                return
            request = service.get(snapshot["id"])
            with contextlib.closing(store.connect()) as database:
                if member(database, request["id"]):
                    continue
            if request["state"] != "candidate":
                continue
            report = request.get("result") or {}
            issues = report.get("remaining_issues")
            if issues != []:
                reason = (
                    "Needs attention: reported remaining issues: " + encode(issues)
                    if issues
                    else "Result lacks an explicit empty remaining_issues report"
                )
                _waiting(
                    store,
                    project_id,
                    {"request_id": request["id"]},
                    reason,
                    request,
                    attention=bool(issues),
                )
                continue
            if _enabled(store, project_id):
                service.accept(request["id"], REASON)
        except (ValueError, OSError) as error:
            _waiting(store, project_id, {"request_id": snapshot["id"]}, str(error), snapshot)


def advance(store) -> None:
    """Run after processing results, before seeding and dispatching work.

    Public approval/acceptance methods remain responsible for live state gates.
    No selection, retry, explicit worker confirmation, merge, or push occurs here.
    """
    for project in store.projects():
        try:
            _advance_project(store, project["id"])
        except (ValueError, OSError) as error:
            try:
                _waiting(store, project["id"], {}, str(error))
            except (ValueError, OSError):
                # An inaccessible project must not stop other projects' progress.
                continue
