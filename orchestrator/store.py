"""Transactional project state. Conversations and process output are not task authority."""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import time
import uuid
from typing import Any, Iterator


class StateError(ValueError):
    """A requested transition violates the persisted state contract."""


TERMINAL = {"succeeded", "failed", "cancelled", "unknown"}
CORE_ROLES = {"planner", "critic", "monitor"}
NOTE_NAMES = {"BRIEF.md", "DECISIONS.md", "CONSTRAINTS.md", "OPEN_QUESTIONS.md"}


def identifier(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,95}", value):
        raise StateError("Use an identifier containing only letters, digits, underscores and dashes")
    return value


def encode(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)


def now() -> float:
    return time.time()


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            os.fchmod(output.fileno(), 0o600)
            output.write(text)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


SCHEMA = """
CREATE TABLE IF NOT EXISTS projects(
 id TEXT PRIMARY KEY, root TEXT NOT NULL UNIQUE, created REAL NOT NULL,
 monitor_cursor INTEGER NOT NULL DEFAULT 0, next_monitor_at REAL NOT NULL DEFAULT 0,
 monitor_failures INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS sessions(
 id TEXT PRIMARY KEY, project_id TEXT REFERENCES projects(id), frontend TEXT NOT NULL,
 observer INTEGER NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1,
 last_seen REAL NOT NULL, created REAL NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS one_writer ON sessions(project_id)
 WHERE active=1 AND observer=0 AND project_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS plans(
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
 session_id TEXT NOT NULL REFERENCES sessions(id), version INTEGER NOT NULL,
 request TEXT NOT NULL, status TEXT NOT NULL, planner_task TEXT, critic_task TEXT,
 graph_json TEXT, created REAL NOT NULL, UNIQUE(project_id,version));
CREATE TABLE IF NOT EXISTS tasks(
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
 session_id TEXT NOT NULL REFERENCES sessions(id), role TEXT NOT NULL,
 plan_id TEXT REFERENCES plans(id), state TEXT NOT NULL, prompt TEXT NOT NULL,
 config_json TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL,
 token TEXT, runner_pid INTEGER, runner_identity TEXT, heartbeat REAL,
 result TEXT, artifact TEXT, artifact_sha TEXT, error TEXT, harness_session TEXT,
 cost_usd REAL, cancel_requested INTEGER NOT NULL DEFAULT 0,
 processed INTEGER NOT NULL DEFAULT 0, cursor INTEGER,
 idempotency_key TEXT, UNIQUE(project_id,idempotency_key));
CREATE UNIQUE INDEX IF NOT EXISTS one_monitor ON tasks(project_id)
 WHERE role='monitor' AND state IN ('queued','starting','running');
CREATE TABLE IF NOT EXISTS dependencies(
 task_id TEXT NOT NULL REFERENCES tasks(id), prerequisite TEXT NOT NULL REFERENCES tasks(id),
 PRIMARY KEY(task_id,prerequisite), CHECK(task_id<>prerequisite));
CREATE TABLE IF NOT EXISTS events(
 id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT NOT NULL REFERENCES projects(id),
 session_id TEXT, kind TEXT NOT NULL, payload TEXT NOT NULL, created REAL NOT NULL,
 task_id TEXT REFERENCES tasks(id), review_required INTEGER NOT NULL DEFAULT 1);
CREATE TRIGGER IF NOT EXISTS immutable_event_update BEFORE UPDATE ON events
 BEGIN SELECT RAISE(ABORT,'events are immutable'); END;
CREATE TRIGGER IF NOT EXISTS immutable_event_delete BEFORE DELETE ON events
 BEGIN SELECT RAISE(ABORT,'events are immutable'); END;
CREATE INDEX IF NOT EXISTS project_events ON events(project_id,id);
CREATE TABLE IF NOT EXISTS inbox(
 session_id TEXT NOT NULL REFERENCES sessions(id), event_id INTEGER NOT NULL REFERENCES events(id),
 acknowledged REAL, PRIMARY KEY(session_id,event_id));
CREATE TABLE IF NOT EXISTS holds(
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
 source_task TEXT NOT NULL REFERENCES tasks(id), detail TEXT NOT NULL,
 created REAL NOT NULL, resolved REAL, resolution TEXT);
CREATE TABLE IF NOT EXISTS service(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS graph_nodes(
 plan_id TEXT NOT NULL REFERENCES plans(id), node_id TEXT NOT NULL, specification TEXT NOT NULL,
 state TEXT NOT NULL DEFAULT 'pending', evidence TEXT, PRIMARY KEY(plan_id,node_id));
PRAGMA user_version=1;
"""


class Store:
    def __init__(self, home: Path):
        self.home = Path(home).resolve()
        self.data = self.home / "data"
        if self.data.is_symlink():
            raise StateError("The private data directory must not be a symlink")
        self.data.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.data, 0o700)
        self.database = self.data / "state.sqlite3"
        if self.database.is_symlink():
            raise StateError("The state database must not be a symlink")
        with contextlib.closing(self.connect()) as database:
            version = database.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise StateError(f"Unsupported database schema {version}; refusing to downgrade")
            database.execute("PRAGMA journal_mode=WAL")
            database.executescript(SCHEMA)
        os.chmod(self.database, 0o600)

    def connect(self) -> sqlite3.Connection:
        database = sqlite3.connect(self.database, timeout=30, isolation_level=None)
        database.row_factory = sqlite3.Row
        database.execute("PRAGMA foreign_keys=ON")
        database.execute("PRAGMA busy_timeout=30000")
        database.execute("PRAGMA synchronous=FULL")
        return database

    @contextlib.contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        database = self.connect()
        try:
            database.execute("BEGIN IMMEDIATE")
            yield database
            database.commit()
        except BaseException:
            database.rollback()
            raise
        finally:
            database.close()

    def _event(self, database, project_id, kind, payload, session_id=None, task_id=None,
               notify=False, review_required=True) -> int:
        event_id = database.execute(
            "INSERT INTO events(project_id,session_id,kind,payload,created,task_id,review_required) "
            "VALUES(?,?,?,?,?,?,?)", (project_id, session_id, kind, encode(payload), now(), task_id, int(review_required))
        ).lastrowid
        if notify:
            recipients = {row[0] for row in database.execute(
                "SELECT id FROM sessions WHERE project_id=? AND active=1", (project_id,))}
            if session_id:
                recipients.add(session_id)
            for recipient in recipients:
                database.execute("INSERT OR IGNORE INTO inbox VALUES(?,?,NULL)",
                                 (recipient, event_id))
        return event_id

    def add_project(self, project_id: str, root: str) -> dict:
        identifier(project_id)
        project_root = Path(root).expanduser().resolve(strict=True)
        if not project_root.is_dir():
            raise StateError("Project root must be a directory")
        with self.transaction() as database:
            existing = database.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
            if existing:
                if existing["root"] != str(project_root):
                    raise StateError("Project identity is already bound to another directory")
                return dict(existing)
            try:
                database.execute("INSERT INTO projects(id,root,created) VALUES(?,?,?)",
                                 (project_id, str(project_root), now()))
            except sqlite3.IntegrityError as error:
                raise StateError("That canonical project directory is already registered") from error
            self._event(database, project_id, "project.registered", {"root": str(project_root)})
        return self.project(project_id)

    def project(self, project_id: str) -> dict:
        with contextlib.closing(self.connect()) as database:
            row = database.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
            if not row:
                raise StateError(f"Unknown project: {project_id}")
            return dict(row)

    def projects(self) -> list[dict]:
        with contextlib.closing(self.connect()) as database:
            return [dict(row) for row in database.execute("SELECT * FROM projects ORDER BY id")]

    def session(self, session_id: str) -> dict:
        with contextlib.closing(self.connect()) as database:
            row = database.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
            if not row:
                raise StateError("Unknown coordinator session; initialize it first")
            return dict(row)

    def open_session(self, session_id: str, frontend: str, project_id: str | None = None,
                     observer=False, takeover=False) -> dict:
        identifier(session_id)
        if frontend not in {"claude", "pi", "test"}:
            raise StateError("Unsupported frontend")
        with self.transaction() as database:
            existing = database.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
            if existing and existing["project_id"] not in (None, project_id):
                raise StateError("A coordinator session cannot change projects")
            if existing and bool(existing["observer"]) != bool(observer):
                raise StateError("Session observer mode cannot be changed")
            if project_id:
                if not database.execute("SELECT 1 FROM projects WHERE id=?", (project_id,)).fetchone():
                    raise StateError("Register the project before binding it")
                owner = database.execute(
                    "SELECT id FROM sessions WHERE project_id=? AND active=1 AND observer=0 AND id<>?",
                    (project_id, session_id)).fetchone()
                if owner and not observer:
                    if not takeover:
                        raise StateError(f"Project already owned by session {owner[0]}; use explicit takeover")
                    database.execute("UPDATE sessions SET active=0 WHERE id=?", (owner[0],))
                    self._event(database, project_id, "session.takeover", {"old_session": owner[0]},
                                session_id)
            timestamp = now()
            database.execute(
                "INSERT INTO sessions VALUES(?,?,?,?,1,?,?) ON CONFLICT(id) DO UPDATE SET "
                "project_id=excluded.project_id,active=1,last_seen=excluded.last_seen",
                (session_id, project_id, frontend, int(observer), timestamp, timestamp))
            if project_id:
                self._event(database, project_id, "session.bound", {"frontend": frontend}, session_id)
                # Recover unacknowledged project notifications into the replacement session.
                database.execute(
                    "INSERT OR IGNORE INTO inbox(session_id,event_id) SELECT ?,i.event_id FROM inbox i "
                    "JOIN events e ON e.id=i.event_id WHERE e.project_id=? AND i.acknowledged IS NULL",
                    (session_id, project_id))
        return self.session(session_id)

    def touch_session(self, session_id: str) -> None:
        with self.transaction() as database:
            database.execute("UPDATE sessions SET last_seen=? WHERE id=? AND active=1", (now(), session_id))

    def close_session(self, session_id: str) -> None:
        with self.transaction() as database:
            database.execute("UPDATE sessions SET active=0,last_seen=? WHERE id=?", (now(), session_id))

    def require_writer(self, session_id: str) -> dict:
        session = self.session(session_id)
        if not session["project_id"] or not session["active"] or session["observer"]:
            raise StateError("This operation requires the active project coordinator")
        return session

    def record(self, session_id: str, kind: str, payload: dict, *, review_required=True) -> int:
        if kind not in {"user.message", "decision.recorded", "tool.observed", "scope.changed"}:
            raise StateError("Unsupported external event type")
        if len(encode(payload)) > 32768:
            raise StateError("Event exceeds 32 KiB; save the evidence as a project artifact")
        with self.transaction() as database:
            session = database.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
            if not session or not session["active"] or session["observer"] or not session["project_id"]:
                raise StateError("An active writable project session is required")
            if kind == "scope.changed":
                database.execute("UPDATE plans SET status='needs_revision' WHERE project_id=? AND status IN ('approved','reviewed')", (session["project_id"],))
            return self._event(database, session["project_id"], kind, payload, session_id,
                               review_required=review_required if kind == "user.message" else True)

    def events(self, project_id: str, after=0, limit=100) -> list[dict]:
        if type(limit) is not int or not 1 <= limit <= 500:
            raise StateError("Event limit must be 1..500")
        with contextlib.closing(self.connect()) as database:
            rows = database.execute("SELECT * FROM events WHERE project_id=? AND id>? ORDER BY id LIMIT ?",
                                    (project_id, after, limit))
            return [{**dict(row), "payload": json.loads(row["payload"])} for row in rows]

    def updates(self, session_id: str, limit=20) -> list[dict]:
        self.session(session_id)
        with contextlib.closing(self.connect()) as database:
            rows = database.execute(
                "SELECT e.* FROM inbox i JOIN events e ON e.id=i.event_id "
                "WHERE i.session_id=? AND i.acknowledged IS NULL ORDER BY e.id LIMIT ?",
                (session_id, min(max(int(limit), 1), 100)))
            return [{**dict(row), "payload": json.loads(row["payload"])} for row in rows]

    def acknowledge(self, session_id: str, event_ids: list[int]) -> None:
        if not isinstance(event_ids, list) or any(type(item) is not int for item in event_ids):
            raise StateError("Acknowledgments require integer event IDs")
        with self.transaction() as database:
            for event_id in event_ids:
                if not database.execute("SELECT 1 FROM inbox WHERE session_id=? AND event_id=?",
                                        (session_id, event_id)).fetchone():
                    raise StateError("Cannot acknowledge another session's notification")
                database.execute("UPDATE inbox SET acknowledged=COALESCE(acknowledged,?) "
                                 "WHERE session_id=? AND event_id=?", (now(), session_id, event_id))

    def _enqueue(self, database, project_id, session_id, role, prompt, config, plan_id=None,
                 depends_on=(), cursor=None, idempotency_key=None) -> str:
        if role not in CORE_ROLES:
            raise StateError("Worker dispatch is deferred; only planner, critic, and monitor may run")
        session = database.execute("SELECT project_id FROM sessions WHERE id=?", (session_id,)).fetchone()
        if not session or session[0] != project_id:
            raise StateError("Task and session project mismatch")
        if plan_id:
            plan = database.execute("SELECT project_id FROM plans WHERE id=?", (plan_id,)).fetchone()
            if not plan or plan[0] != project_id:
                raise StateError("Task and plan project mismatch")
        if idempotency_key:
            previous = database.execute("SELECT * FROM tasks WHERE project_id=? AND idempotency_key=?",
                                        (project_id, idempotency_key)).fetchone()
            if previous:
                if previous["role"] != role or previous["prompt"] != prompt:
                    raise StateError("Idempotency key was reused for a different request")
                return previous["id"]
        task_id = str(uuid.uuid4())
        timestamp = now()
        database.execute(
            "INSERT INTO tasks(id,project_id,session_id,role,plan_id,state,prompt,config_json,created,"
            "updated,cursor,idempotency_key) VALUES(?,?,?,?,?,'queued',?,?,?,?,?,?)",
            (task_id, project_id, session_id, role, plan_id, prompt, encode(config), timestamp,
             timestamp, cursor, idempotency_key))
        for prerequisite in depends_on:
            dependency = database.execute("SELECT project_id FROM tasks WHERE id=?", (prerequisite,)).fetchone()
            if not dependency or dependency[0] != project_id:
                raise StateError("Dependencies must exist in the same project")
            # Dependencies can only target existing tasks; newly added nodes cannot create a cycle.
            database.execute("INSERT INTO dependencies VALUES(?,?)", (task_id, prerequisite))
        if role != "monitor":
            self._event(database, project_id, "task.queued", {"role": role}, session_id, task_id)
        return task_id

    def enqueue(self, project_id, session_id, role, prompt, config, **kwargs) -> dict:
        with self.transaction() as database:
            task_id = self._enqueue(database, project_id, session_id, role, prompt, config, **kwargs)
        return self.task(task_id)

    def create_plan(self, session_id: str, request: str, config: dict) -> dict:
        if not isinstance(request, str) or not request.strip() or len(request) > 100000:
            raise StateError("Plan request must contain 1..100000 characters")
        with self.transaction() as database:
            session = database.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
            if not session or not session["active"] or session["observer"] or not session["project_id"]:
                raise StateError("Planning requires the active project coordinator")
            project_id = session["project_id"]
            version = database.execute("SELECT COALESCE(MAX(version),0)+1 FROM plans WHERE project_id=?",
                                       (project_id,)).fetchone()[0]
            plan_id = str(uuid.uuid4())
            database.execute("UPDATE plans SET status='superseded' WHERE project_id=? AND status<>'superseded'",
                             (project_id,))
            database.execute("INSERT INTO plans(id,project_id,session_id,version,request,status,created) "
                             "VALUES(?,?,?,?,?,'drafting',?)", (plan_id, project_id, session_id, version, request, now()))
            task_id = self._enqueue(database, project_id, session_id, "planner", request, config,
                                    plan_id=plan_id, idempotency_key=f"planner:{plan_id}")
            database.execute("UPDATE plans SET planner_task=? WHERE id=?", (task_id, plan_id))
            self._event(database, project_id, "plan.created", {"plan_id": plan_id, "version": version}, session_id)
        return self.plan(plan_id)

    def plan(self, plan_id: str) -> dict:
        with contextlib.closing(self.connect()) as database:
            row = database.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()
            if not row:
                raise StateError("Unknown plan")
            return dict(row)

    def task(self, task_id: str) -> dict:
        with contextlib.closing(self.connect()) as database:
            row = database.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not row:
                raise StateError("Unknown task")
            task = dict(row)
            task["config"] = json.loads(task.pop("config_json"))
            return task

    def tasks(self, project_id: str | None = None, states: tuple | None = None) -> list[dict]:
        clauses, arguments = [], []
        if project_id:
            clauses.append("project_id=?")
            arguments.append(project_id)
        if states:
            clauses.append("state IN (" + ",".join("?" for _ in states) + ")")
            arguments.extend(states)
        query = "SELECT id FROM tasks" + (" WHERE " + " AND ".join(clauses) if clauses else "") + " ORDER BY created"
        with contextlib.closing(self.connect()) as database:
            identifiers = [row[0] for row in database.execute(query, arguments)]
        return [self.task(task_id) for task_id in identifiers]

    def claim_next(self, max_parallel: int) -> dict | None:
        with self.transaction() as database:
            active = database.execute("SELECT COUNT(*) FROM tasks WHERE state IN ('starting','running')").fetchone()[0]
            if active >= max_parallel:
                return None
            row = database.execute(
                "SELECT t.id FROM tasks t WHERE t.state='queued' "
                "AND (t.role='monitor' OR NOT EXISTS(SELECT 1 FROM service s WHERE s.key='pause:'||t.project_id)) "
                "AND NOT EXISTS(SELECT 1 "
                "FROM dependencies d JOIN tasks p ON p.id=d.prerequisite "
                "WHERE d.task_id=t.id AND p.state<>'succeeded') "
                "ORDER BY CASE t.role WHEN 'monitor' THEN 0 WHEN 'critic' THEN 1 ELSE 2 END,t.created LIMIT 1"
            ).fetchone()
            if not row:
                return None
            database.execute("UPDATE tasks SET state='starting',token=?,updated=? WHERE id=?",
                             (str(uuid.uuid4()), now(), row[0]))
        return self.task(row[0])

    def runner_started(self, task_id: str, token: str, pid: int, identity: str) -> bool:
        with self.transaction() as database:
            return database.execute(
                "UPDATE tasks SET state='running',runner_pid=?,runner_identity=?,heartbeat=?,updated=? "
                "WHERE id=? AND token=? AND state='starting' AND cancel_requested=0",
                (pid, identity, now(), now(), task_id, token)).rowcount == 1

    def heartbeat(self, task_id: str, token: str) -> bool:
        with self.transaction() as database:
            return database.execute(
                "UPDATE tasks SET heartbeat=?,updated=? WHERE id=? AND token=? AND state='running' "
                "AND cancel_requested=0", (now(), now(), task_id, token)).rowcount == 1

    def finish(self, task_id: str, token: str, *, text="", error=None, state="succeeded",
               harness_session=None, cost_usd=None) -> bool:
        if state not in TERMINAL:
            raise StateError("Invalid terminal task state")
        if state == "succeeded" and (not isinstance(text, str) or not text.strip()):
            raise StateError("A successful task requires a nonempty verified terminal result")
        task = self.task(task_id)
        artifact = self.data / "projects" / task["project_id"] / "artifacts" / f"{task_id}-{token}.txt"
        if text:
            atomic_write(artifact, text)
        digest = hashlib.sha256(text.encode()).hexdigest() if text else None
        with self.transaction() as database:
            current = database.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if current["token"] != token or current["state"] not in {"starting", "running"}:
                return False
            if current["cancel_requested"]:
                state, error = "cancelled", "Cancelled by the coordinator"
            database.execute(
                "UPDATE tasks SET state=?,result=?,artifact=?,artifact_sha=?,error=?,harness_session=?,"
                "cost_usd=?,updated=? WHERE id=?",
                (state, text or None, str(artifact.relative_to(self.home)) if text else None, digest,
                 error, harness_session, cost_usd, now(), task_id))
            kind = "monitor.completed" if task["role"] == "monitor" else f"task.{state}"
            self._event(database, task["project_id"], kind,
                        {"role": task["role"], "state": state, "artifact": str(artifact.relative_to(self.home)) if text else None,
                         "error": error}, task["session_id"], task_id, notify=task["role"] != "monitor" or state != "succeeded")
        return True

    def cancel(self, session_id: str, task_id: str) -> None:
        session = self.require_writer(session_id)
        with self.transaction() as database:
            task = database.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not task or task["project_id"] != session["project_id"]:
                raise StateError("Task is not in this project")
            if task["state"] in TERMINAL:
                return
            new_state = "cancelled" if task["state"] == "queued" else task["state"]
            database.execute("UPDATE tasks SET cancel_requested=1,state=?,updated=? WHERE id=?",
                             (new_state, now(), task_id))
            self._event(database, task["project_id"], "task.cancel_requested", {"task_id": task_id}, session_id, task_id)

    def unprocessed(self) -> list[dict]:
        with contextlib.closing(self.connect()) as database:
            ids = [row[0] for row in database.execute(
                "SELECT id FROM tasks WHERE processed=0 AND state IN ('succeeded','failed','cancelled','unknown')")]
        return [self.task(task_id) for task_id in ids]

    def processed(self, task_id: str) -> None:
        with self.transaction() as database:
            database.execute("UPDATE tasks SET processed=1 WHERE id=?", (task_id,))

    def attach_critic(self, planner_task: dict, prompt: str, config: dict) -> None:
        with self.transaction() as database:
            plan = database.execute("SELECT * FROM plans WHERE id=?", (planner_task["plan_id"],)).fetchone()
            if plan and plan["status"] == "drafting":
                critic_id = self._enqueue(database, plan["project_id"], plan["session_id"], "critic",
                                           prompt, config, plan_id=plan["id"],
                                           depends_on=(planner_task["id"],), idempotency_key=f"critic:{plan['id']}")
                database.execute("UPDATE plans SET status='reviewing',critic_task=? WHERE id=?", (critic_id, plan["id"]))
            database.execute("UPDATE tasks SET processed=1 WHERE id=?", (planner_task["id"],))

    def apply_critique(self, task: dict, verdict: str, findings: list) -> None:
        if verdict not in {"approved", "changes_requested"} or not isinstance(findings, list):
            raise StateError("Critic must return approved or changes_requested and a findings list")
        with self.transaction() as database:
            plan = database.execute("SELECT * FROM plans WHERE id=?", (task["plan_id"],)).fetchone()
            if plan and plan["status"] == "reviewing" and plan["critic_task"] == task["id"]:
                status = "reviewed" if verdict == "approved" else "needs_revision"
                database.execute("UPDATE plans SET status=? WHERE id=?", (status, plan["id"]))
                self._event(database, task["project_id"], "plan.reviewed",
                            {"plan_id": plan["id"], "version": plan["version"], "verdict": verdict,
                             "findings": findings}, task["session_id"], task["id"], notify=True)
            database.execute("UPDATE tasks SET processed=1 WHERE id=?", (task["id"],))

    def fail_processing(self, task: dict, error: str) -> None:
        with self.transaction() as database:
            database.execute("UPDATE tasks SET processed=1,error=? WHERE id=?", (error, task["id"]))
            if task["plan_id"]:
                database.execute("UPDATE plans SET status='needs_revision' WHERE id=? AND status<>'superseded'",
                                 (task["plan_id"],))
            self._event(database, task["project_id"], "review.invalid", {"error": error},
                        task["session_id"], task["id"], notify=True)

    def apply_monitor(self, task: dict, reviewed_through: int, findings: list) -> None:
        if type(reviewed_through) is not int or reviewed_through != task["cursor"] or not isinstance(findings, list):
            raise StateError("Monitor response must identify exactly the supplied event cursor")
        for finding in findings:
            if not isinstance(finding, dict) or finding.get("severity") not in {"info", "warning", "blocking"}:
                raise StateError("Monitor finding has an invalid severity")
            if not isinstance(finding.get("summary"), str) or not finding["summary"].strip():
                raise StateError("Monitor findings require a nonempty summary")
        with self.transaction() as database:
            current = database.execute("SELECT processed FROM tasks WHERE id=?", (task["id"],)).fetchone()
            if current[0]:
                return
            for finding in findings:
                if finding["severity"] == "blocking":
                    database.execute("INSERT INTO holds VALUES(?,?,?,?,?,NULL,NULL)",
                                     (str(uuid.uuid4()), task["project_id"], task["id"], encode(finding), now()))
            database.execute("UPDATE projects SET monitor_cursor=MAX(monitor_cursor,?),monitor_failures=0 WHERE id=?",
                             (reviewed_through, task["project_id"]))
            database.execute("UPDATE tasks SET processed=1 WHERE id=?", (task["id"],))
            if findings:
                self._event(database, task["project_id"], "monitor.findings",
                            {"reviewed_through": reviewed_through, "findings": findings},
                            task["session_id"], task["id"], notify=True)

    def monitor_failure(self, task: dict, error: str) -> None:
        with self.transaction() as database:
            database.execute("UPDATE tasks SET processed=1,error=? WHERE id=?", (error, task["id"]))
            failures = database.execute("SELECT monitor_failures FROM projects WHERE id=?", (task["project_id"],)).fetchone()[0] + 1
            database.execute("UPDATE projects SET monitor_failures=?,next_monitor_at=? WHERE id=?",
                             (failures, now() + min(3600, 30 * 2 ** min(failures, 7)), task["project_id"]))
            self._event(database, task["project_id"], "monitor.unavailable", {"error": error, "failures": failures},
                        task["session_id"], task["id"], notify=True)

    def monitor_candidate(self, project_id: str, limit=50) -> dict | None:
        with contextlib.closing(self.connect()) as database:
            project = database.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
            if not project or project["next_monitor_at"] > now():
                return None
            if database.execute("SELECT 1 FROM tasks WHERE project_id=? AND role='monitor' AND state IN ('queued','starting','running')", (project_id,)).fetchone():
                return None
            owner = database.execute("SELECT id FROM sessions WHERE project_id=? AND observer=0 ORDER BY active DESC,created DESC LIMIT 1", (project_id,)).fetchone()
            if not owner:
                return None
            rows = database.execute(
                "SELECT * FROM events WHERE project_id=? AND id>? AND kind NOT LIKE 'monitor.%' "
                "AND kind NOT IN ('session.bound','session.takeover') AND review_required=1 ORDER BY id LIMIT ?",
                (project_id, project["monitor_cursor"], limit)).fetchall()
            if not rows:
                return None
            cursor = rows[-1]["id"]
            all_rows = database.execute("SELECT * FROM events WHERE project_id=? AND id>? AND id<=? AND kind NOT LIKE 'monitor.%' ORDER BY id",
                                        (project_id, project["monitor_cursor"], cursor)).fetchall()
            return {"session_id": owner[0], "cursor": cursor,
                    "events": [{**dict(row), "payload": json.loads(row["payload"])} for row in all_rows]}

    def schedule_monitor(self, project_id: str, candidate: dict, prompt: str, config: dict,
                         interval: float) -> dict | None:
        try:
            with self.transaction() as database:
                task_id = self._enqueue(database, project_id, candidate["session_id"], "monitor", prompt,
                                        config, cursor=candidate["cursor"],
                                        idempotency_key=f"monitor:{candidate['cursor']}:{uuid.uuid4()}")
                database.execute("UPDATE projects SET next_monitor_at=? WHERE id=?", (now() + interval, project_id))
            return self.task(task_id)
        except sqlite3.IntegrityError:
            return None

    def approve_plan(self, plan_id: str) -> None:
        with self.transaction() as database:
            plan = database.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()
            if not plan or plan["status"] != "reviewed":
                raise StateError("Only the current independently reviewed plan can be approved")
            if database.execute("SELECT 1 FROM holds WHERE project_id=? AND resolved IS NULL", (plan["project_id"],)).fetchone():
                raise StateError("Resolve blocking monitor findings before approving")
            project = database.execute("SELECT monitor_cursor FROM projects WHERE id=?", (plan["project_id"],)).fetchone()
            review_event = database.execute("SELECT MAX(id) FROM events WHERE task_id=? AND kind='plan.reviewed'", (plan["critic_task"],)).fetchone()[0]
            latest_event = database.execute("SELECT MAX(id) FROM events WHERE project_id=? AND review_required=1 AND kind NOT LIKE 'monitor.%' AND kind NOT IN ('session.bound','session.takeover')", (plan["project_id"],)).fetchone()[0]
            if not review_event or project[0] < max(review_event, latest_event or 0):
                raise StateError("The slow monitor has not yet inspected this plan's review")
            database.execute("UPDATE plans SET status='approved' WHERE id=?", (plan_id,))
            self._event(database, plan["project_id"], "plan.approved", {"plan_id": plan_id, "version": plan["version"]},
                        plan["session_id"], notify=True)

    def resolve_hold(self, hold_id: str, resolution: str) -> None:
        if not resolution.strip():
            raise StateError("Resolving a hold requires an explanation")
        with self.transaction() as database:
            hold = database.execute("SELECT * FROM holds WHERE id=?", (hold_id,)).fetchone()
            if not hold:
                raise StateError("Unknown hold")
            database.execute("UPDATE holds SET resolved=?,resolution=? WHERE id=?", (now(), resolution, hold_id))
            self._event(database, hold["project_id"], "hold.resolved", {"hold_id": hold_id, "resolution": resolution}, notify=True)

    def snapshot(self, project_id: str) -> dict:
        project = self.project(project_id)
        with contextlib.closing(self.connect()) as database:
            plans = [dict(row) for row in database.execute("SELECT * FROM plans WHERE project_id=? ORDER BY version DESC LIMIT 5", (project_id,))]
            holds = [dict(row) for row in database.execute("SELECT * FROM holds WHERE project_id=? AND resolved IS NULL", (project_id,))]
            sessions = [dict(row) for row in database.execute("SELECT * FROM sessions WHERE project_id=? AND active=1", (project_id,))]
        tasks = [{key: value for key, value in task.items() if key not in {"prompt", "result", "token", "config"}}
                 for task in self.tasks(project_id)]
        return {"project": project, "sessions": sessions, "plans": plans, "tasks": tasks[-100:], "holds": holds,
                "workers_enabled": False}

    def read_note(self, project_id: str, name: str) -> dict:
        self.project(project_id)
        if name not in NOTE_NAMES:
            raise StateError("Unknown note name; STATUS is generated from actual task records")
        path = self.data / "projects" / project_id / "notes" / name
        if path.is_symlink():
            raise StateError("Notes cannot be symlinks")
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        return {"name": name, "text": text, "revision": hashlib.sha256(text.encode()).hexdigest()}

    def write_note(self, session_id: str, name: str, text: str, expected_revision: str) -> dict:
        session = self.require_writer(session_id)
        if not isinstance(text, str) or len(text.encode()) > 100000:
            raise StateError("Notes must be text of at most 100 KB")
        # Serialize competing note writers with the same database lock as state mutations.
        with self.transaction() as database:
            previous = self.read_note(session["project_id"], name)
            if previous["revision"] != expected_revision:
                raise StateError("Note changed since you read it; read again before updating")
            path = self.data / "projects" / session["project_id"] / "notes" / name
            atomic_write(path, text)
            self._event(database, session["project_id"], "note.updated", {"name": name, "previous_revision": expected_revision,
                        "revision": hashlib.sha256(text.encode()).hexdigest()}, session_id)
        return self.read_note(session["project_id"], name)

    def backup(self) -> Path:
        directory = self.data / "backups"
        directory.mkdir(exist_ok=True, mode=0o700)
        destination = directory / f"state-{time.time_ns()}.sqlite3"
        with contextlib.closing(self.connect()) as source, contextlib.closing(sqlite3.connect(destination)) as target:
            source.backup(target)
        os.chmod(destination, 0o600)
        for obsolete in sorted(directory.glob("state-*.sqlite3"))[:-3]:
            obsolete.unlink()
        return destination

    def install_graph(self, plan_id: str, planner_task_id: str, value: dict) -> bool:
        from .graphs import validate_plan
        graph = validate_plan(value)
        with self.transaction() as database:
            plan = database.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()
            if not plan or plan["planner_task"] != planner_task_id or plan["status"] != "drafting":
                return False
            if plan["graph_json"]:
                if plan["graph_json"] != encode(graph):
                    raise StateError("A plan graph is immutable; create a new plan version")
                return True
            database.execute("UPDATE plans SET graph_json=? WHERE id=?", (encode(graph), plan_id))
            for node in graph["nodes"]:
                database.execute("INSERT INTO graph_nodes(plan_id,node_id,specification) VALUES(?,?,?)",
                                 (plan_id, node["id"], encode(node)))
            self._event(database, plan["project_id"], "plan.graph_validated",
                        {"plan_id": plan_id, "nodes": len(graph["nodes"])}, plan["session_id"], planner_task_id)
        return True

    def graph_snapshot(self, plan_id: str, execution: dict | None = None) -> dict:
        from .graphs import ready_nodes
        plan = self.plan(plan_id)
        if not plan["graph_json"]:
            raise StateError("This plan has no validated graph yet")
        graph = json.loads(plan["graph_json"])
        with contextlib.closing(self.connect()) as database:
            states = {row["node_id"]: row["state"] for row in database.execute(
                "SELECT node_id,state FROM graph_nodes WHERE plan_id=?", (plan_id,))}
        preferences = execution or {}
        readiness = ready_nodes(graph, states, preferences.get("max_parallel", 3),
                                preferences.get("dependency_failure", "block"))
        return {"plan_id": plan_id, "version": plan["version"], "plan_status": plan["status"],
                "graph": graph, "states": states, "readiness": readiness,
                "dispatch_enabled": False, "dispatch_blocker": "Worker router is intentionally not configured"}

    def service_value(self, key: str, default: str | None = None) -> str | None:
        with contextlib.closing(self.connect()) as database:
            row = database.execute("SELECT value FROM service WHERE key=?", (key,)).fetchone()
            return row[0] if row else default

    def set_service_value(self, key: str, value: str) -> None:
        with self.transaction() as database:
            database.execute("INSERT INTO service VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                             (key, value))

    def pause(self, project_id: str, reason: str) -> None:
        self.project(project_id)
        if not reason.strip():
            raise StateError("Pausing requires an explanation")
        with self.transaction() as database:
            database.execute("INSERT INTO service VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                             (f"pause:{project_id}", reason))
            self._event(database, project_id, "project.paused", {"reason": reason}, notify=True)

    def resume(self, project_id: str) -> None:
        self.project(project_id)
        with self.transaction() as database:
            database.execute("DELETE FROM service WHERE key=?", (f"pause:{project_id}",))
            self._event(database, project_id, "project.resumed", {}, notify=True)
