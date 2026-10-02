"""Foreground lifecycle and non-consuming, materiality-aware notification delivery."""

import json
import time

from .store import StateError


def _marker(database, session_id):
    row = database.execute(
        "SELECT value FROM service WHERE key=?", (f"foreground:{session_id}",)
    ).fetchone()
    return json.loads(row[0]) if row else None


def _writer(database, session_id):
    session = database.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
    if not session or not session["active"] or session["observer"]:
        raise StateError("Foreground lifecycle requires an active writable session")
    return session


def _save(database, session_id, marker):
    database.execute(
        "INSERT INTO service VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (f"foreground:{session_id}", json.dumps(marker)),
    )
    return marker


def begin_turn(store, session_id: str, turn_id: str) -> dict:
    """Mark a writable foreground busy, including before project binding."""
    if not isinstance(turn_id, str) or not turn_id.strip() or len(turn_id) > 512:
        raise StateError("A nonempty foreground turn_id of at most 512 characters is required")
    with store.transaction() as database:
        _writer(database, session_id)
        previous = _marker(database, session_id)
        if previous and previous["token"] == turn_id:
            return previous
        timestamp = time.time()
        return _save(
            database,
            session_id,
            {
                "active": True,
                "token": turn_id,
                "start": timestamp,
                "completed": None,
                "touched": timestamp,
            },
        )


def finish_turn(store, session_id: str, turn_id: str | None = None) -> dict:
    """A stale completion never clears a newer turn; repeated stops do not debounce again."""
    with store.transaction() as database:
        _writer(database, session_id)
        previous = _marker(database, session_id)
        if previous and (
            not previous["active"] or (turn_id is not None and previous["token"] != turn_id)
        ):
            return previous
        timestamp = time.time()
        return _save(
            database,
            session_id,
            {
                "active": False,
                "token": previous["token"] if previous else turn_id,
                "start": previous["start"] if previous else None,
                "completed": timestamp,
                "touched": timestamp,
            },
        )


def should_review(database, project_id: str, quiet_seconds: float = 20) -> bool:
    """Use the current writer only, within the caller's scheduling transaction."""
    writer = database.execute(
        "SELECT id FROM sessions WHERE project_id=? AND active=1 AND observer=0",
        (project_id,),
    ).fetchone()
    if not writer:
        return True
    marker = _marker(database, writer["id"])
    if marker is None:
        return True  # Legacy CLI sessions do not emit frontend lifecycle events.
    if marker["active"]:
        owner_row = database.execute(
            "SELECT value FROM service WHERE key=?", ("native-owner:" + writer["id"],)
        ).fetchone()
        if owner_row:
            from .runtime import process_identity

            owner = json.loads(owner_row[0])
            if (
                type(owner.get("pid")) is int
                and owner.get("identity")
                and process_identity(owner["pid"]) != owner["identity"]
            ):
                # A dead frontend cannot be interrupted. Do not rewrite its marker here:
                # monitor_candidate is a read path, and a new owner may be binding concurrently.
                return True
    return not marker["active"] and marker["completed"] <= time.time() - quiet_seconds


def delivery_updates(store, session_id: str, quiet_seconds: float = 20) -> dict:
    """Read each category independently in SQL so silent backlogs cannot hide urgent events.

    No acknowledgement is written, and at most 50 event payloads per category are loaded.
    """
    with store.transaction() as database:
        session = database.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        if not session:
            raise StateError("Unknown coordinator session; initialize it first")
        ready = bool(
            session["active"]
            and session["project_id"]
            and should_review(database, session["project_id"], quiet_seconds)
        )
        result = {"ready": ready, "interrupting": [], "silent": []}
        if not ready:
            return result
        interrupting = """CASE
            WHEN e.kind IN ('monitor.unavailable','worker.routing_recommended') THEN 0
            WHEN e.kind='monitor.findings' THEN EXISTS (
                SELECT 1 FROM json_each(e.payload, '$.findings') finding
                WHERE json_extract(finding.value, '$.severity')='blocking'
            ) ELSE 1 END"""
        for category, enabled in (("interrupting", 1), ("silent", 0)):
            rows = database.execute(
                "SELECT e.* FROM inbox i JOIN events e ON e.id=i.event_id "
                "WHERE i.session_id=? AND i.acknowledged IS NULL AND e.project_id=? "
                f"AND ({interrupting})=? ORDER BY e.id LIMIT 50",
                (session_id, session["project_id"], enabled),
            )
            result[category] = [
                {**dict(row), "payload": json.loads(row["payload"])} for row in rows
            ]
        return result
