"""Durable worker grants. Only operator transports may expose approval methods."""

import contextlib
import json
import re
import uuid
from pathlib import Path

from .config import load_config
from .store import TERMINAL, StateError, encode, now

SCHEMA_V2 = """
CREATE TABLE IF NOT EXISTS worker_policies(
 digest TEXT PRIMARY KEY, policy_json TEXT NOT NULL, source TEXT NOT NULL, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS worker_requests(
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
 session_id TEXT NOT NULL REFERENCES sessions(id), origin_event_id INTEGER NOT NULL REFERENCES events(id),
 plan_id TEXT REFERENCES plans(id), node_id TEXT, plan_version INTEGER,
 brief TEXT NOT NULL, mode TEXT NOT NULL CHECK(mode IN ('read','write')),
 state TEXT NOT NULL DEFAULT 'pending', generation INTEGER NOT NULL DEFAULT 1,
 policy_digest TEXT REFERENCES worker_policies(digest), policy_error TEXT,
 evidence_json TEXT NOT NULL, profile_json TEXT, selection_source TEXT,
 approval TEXT, accepted TEXT, task_id TEXT UNIQUE REFERENCES tasks(id),
 workspace_json TEXT, result_json TEXT, error TEXT,
 idempotency_key TEXT, created REAL NOT NULL,
 UNIQUE(project_id,idempotency_key), UNIQUE(plan_id,node_id),
 FOREIGN KEY(plan_id,node_id) REFERENCES graph_nodes(plan_id,node_id),
 CHECK((plan_id IS NULL AND node_id IS NULL AND plan_version IS NULL) OR
       (plan_id IS NOT NULL AND node_id IS NOT NULL AND plan_version IS NOT NULL)));
CREATE UNIQUE INDEX IF NOT EXISTS one_task_per_worker_request ON tasks(worker_request_id)
 WHERE worker_request_id IS NOT NULL;
CREATE TRIGGER IF NOT EXISTS immutable_worker_task_link BEFORE UPDATE OF worker_request_id ON tasks
 WHEN OLD.worker_request_id IS NOT NEW.worker_request_id
 BEGIN SELECT RAISE(ABORT,'worker task link is immutable'); END;
CREATE TRIGGER IF NOT EXISTS immutable_worker_origin BEFORE UPDATE OF
 project_id,session_id,origin_event_id,plan_id,node_id,plan_version,brief,mode ON worker_requests
 BEGIN SELECT RAISE(ABORT,'worker origin is immutable'); END;
CREATE TRIGGER IF NOT EXISTS immutable_worker_policy BEFORE UPDATE ON worker_policies
 BEGIN SELECT RAISE(ABORT,'policy snapshots are immutable'); END;
CREATE TRIGGER IF NOT EXISTS immutable_plan_origin BEFORE UPDATE OF origin_event_id ON plans
 WHEN OLD.origin_event_id IS NOT NEW.origin_event_id
 BEGIN SELECT RAISE(ABORT,'plan origin is immutable'); END;
"""


def migrate(database):
    """Add v2 without rebuilding any existing table or lowering the version."""
    database.execute("BEGIN IMMEDIATE")
    try:
        for table, column, definition in (
            ("tasks", "worker_request_id", "TEXT REFERENCES worker_requests(id)"),
            ("plans", "origin_event_id", "INTEGER REFERENCES events(id)"),
        ):
            if column not in {row[1] for row in database.execute(f"PRAGMA table_info({table})")}:
                database.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        # executescript commits implicitly; execute complete statements under our lock instead.
        import sqlite3

        statement = ""
        for line in SCHEMA_V2.splitlines():
            statement += line + "\n"
            if sqlite3.complete_statement(statement):
                database.execute(statement)
                statement = ""
        database.execute("PRAGMA user_version=2")
        database.commit()
    except BaseException:
        database.rollback()
        raise


def _text(value, name):
    if not isinstance(value, str) or not value.strip() or len(value) > 100000:
        raise StateError(f"{name} requires nonempty text of at most 100000 characters")


def _decode(row):
    value = dict(row)
    for name in ("evidence", "profile", "workspace", "result"):
        value[name] = json.loads(value.pop(name + "_json")) if value[name + "_json"] else None
    return value


class WorkerService:
    def __init__(self, store):
        self.store = store

    def _request(self, database, request_id):
        row = database.execute("SELECT * FROM worker_requests WHERE id=?", (request_id,)).fetchone()
        if not row:
            raise StateError("Unknown worker request")
        return _decode(row)

    def get(self, request_id):
        with contextlib.closing(self.store.connect()) as database:
            return self._request(database, request_id)

    def list(self, project_id):
        self.store.project(project_id)
        with contextlib.closing(self.store.connect()) as database:
            return [
                _decode(row)
                for row in database.execute(
                    "SELECT * FROM worker_requests WHERE project_id=? ORDER BY created,id",
                    (project_id,),
                )
            ]

    def _writer(self, database, session_id, project_id=None):
        session = database.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        if (
            not session
            or not session["active"]
            or session["observer"]
            or not session["project_id"]
            or project_id not in (None, session["project_id"])
        ):
            raise StateError("An active writable coordinator in this project is required")
        return session

    def policy(self, project_id):
        from .routing import RoutingError, load_policy

        project = self.store.project(project_id)
        try:
            return {
                **load_policy(self.store.home, Path(project["root"])),
                "available": True,
                "procedure": "Monitor selects plan work; foreground selects unrelated work. "
                "Operator approves writes and accepts results through CLI.",
            }
        except RoutingError as error:
            return {"available": False, "error": str(error)}

    def _snapshot(self, database, project_id):
        from .routing import capture_quota_evidence

        status = self.policy(project_id)
        digest = status.get("digest")
        if status["available"]:
            database.execute(
                "INSERT OR IGNORE INTO worker_policies VALUES(?,?,?,?)",
                (digest, encode(status["policy"]), str(status["source"]), now()),
            )
        return digest, status.get("error"), encode(capture_quota_evidence())

    def _fresh(self, request):
        status = self.policy(request["project_id"])
        if (
            not status["available"]
            or not request["policy_digest"]
            or status["digest"] != request["policy_digest"]
        ):
            raise StateError(
                "Worker policy is unavailable or changed; explicitly refresh the request"
            )
        return status["policy"]

    def _origin(self, database, project_id, origin_event_id, plan_id):
        event = database.execute("SELECT * FROM events WHERE id=?", (origin_event_id,)).fetchone()
        if not event or event["project_id"] != project_id or event["kind"] != "user.message":
            raise StateError("Worker origin must be a persisted user message in this project")
        if (
            not plan_id
            and database.execute(
                "SELECT 1 FROM plans WHERE project_id=? AND origin_event_id=?",
                (project_id, origin_event_id),
            ).fetchone()
        ):
            raise StateError("This user event belongs to planning, not unrelated foreground work")
        if (
            not plan_id
            and database.execute(
                "SELECT 1 FROM plans WHERE project_id=? AND origin_event_id IS NULL AND created>=?",
                (project_id, event["created"]),
            ).fetchone()
        ):
            raise StateError(
                "Legacy planning origin is unavailable; record a new unrelated user event"
            )

    def _insert(
        self,
        database,
        session_id,
        project_id,
        brief,
        mode,
        origin_event_id,
        plan_id=None,
        node_id=None,
        idempotency_key=None,
    ):
        if not plan_id:
            _text(brief, "Worker brief")
        # Plan briefs are loaded from already-validated node specifications below.
        # Their acceptance criteria can legitimately exceed the free-form API limit.
        if mode not in ("read", "write"):
            raise StateError("Worker mode must be read or write")
        self._origin(database, project_id, origin_event_id, plan_id)
        version = None
        if bool(plan_id) != bool(node_id):
            raise StateError("Plan and node must be supplied together")
        if plan_id:
            plan, node = self._node(database, plan_id, node_id)
            if plan["project_id"] != project_id or plan["origin_event_id"] != origin_event_id:
                raise StateError("Request does not match the immutable plan origin")
            specification = json.loads(node["specification"])
            if specification["kind"] == "approval" or specification.get("mode", "read") != mode:
                raise StateError("Worker mode must match a non-approval plan node")
            version = plan["version"]
            # A foreground caller cannot replace an approved node's brief with new work.
            brief = encode(specification)
        existing = database.execute(
            "SELECT * FROM worker_requests WHERE (project_id=? AND idempotency_key=?) "
            "OR (plan_id=? AND node_id=?)",
            (project_id, idempotency_key, plan_id, node_id),
        ).fetchone()
        if existing:
            for key, value in {
                "brief": brief,
                "mode": mode,
                "origin_event_id": origin_event_id,
                "plan_id": plan_id,
                "node_id": node_id,
            }.items():
                if existing[key] != value:
                    raise StateError("Idempotency key or node reused for a different request")
            return existing["id"]
        digest, error, evidence = self._snapshot(database, project_id)
        request_id = str(uuid.uuid4())
        database.execute(
            "INSERT INTO worker_requests(id,project_id,session_id,origin_event_id,plan_id,node_id,"
            "plan_version,brief,mode,policy_digest,policy_error,evidence_json,idempotency_key,created) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                request_id,
                project_id,
                session_id,
                origin_event_id,
                plan_id,
                node_id,
                version,
                brief,
                mode,
                digest,
                error,
                evidence,
                idempotency_key,
                now(),
            ),
        )
        self.store._event(
            database, project_id, "worker.requested", {"request_id": request_id}, session_id
        )
        return request_id

    def request(
        self,
        session_id,
        brief,
        mode="read",
        *,
        origin_event_id,
        plan_id=None,
        node_id=None,
        idempotency_key=None,
    ):
        with self.store.transaction() as database:
            session = self._writer(database, session_id)
            request_id = self._insert(
                database,
                session_id,
                session["project_id"],
                brief,
                mode,
                origin_event_id,
                plan_id,
                node_id,
                idempotency_key,
            )
        return self.get(request_id)

    def _node(self, database, plan_id, node_id):
        plan = database.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()
        node = database.execute(
            "SELECT * FROM graph_nodes WHERE plan_id=? AND node_id=?", (plan_id, node_id)
        ).fetchone()
        if not plan or not node:
            raise StateError("Unknown plan node")
        return plan, node

    def _unlaunched(self, request):
        if request["task_id"] or request["state"] not in ("pending", "selected"):
            raise StateError("An attempted worker cannot be reselected, refreshed, or replayed")

    def _select(self, database, request, choice, source, override=False):
        from .routing import resolve_selection

        self._unlaunched(request)
        policy = self._fresh(request)
        profile = resolve_selection(
            policy, choice, evidence=request["evidence"], operator_override=override
        )
        if profile["harness"] not in ("claude", "pi"):
            raise StateError("Only Claude and Pi worker executors are enabled")
        database.execute(
            "UPDATE worker_requests SET profile_json=?,selection_source=?,approval=NULL,"
            "state='selected',generation=generation+1 WHERE id=? AND generation=?",
            (encode(profile), source, request["id"], request["generation"]),
        )
        self.store._event(
            database,
            request["project_id"],
            "worker.selected",
            {"request_id": request["id"], "source": source, "profile": profile},
        )

    def select(self, session_id, request_id, choice):
        with self.store.transaction() as database:
            request = self._request(database, request_id)
            self._writer(database, session_id, request["project_id"])
            if request["plan_id"]:
                raise StateError("Only an accepted monitor result can select plan work")
            self._origin(database, request["project_id"], request["origin_event_id"], None)
            self._select(database, request, choice, "foreground")
        return self.get(request_id)

    def override(self, request_id, choice):
        with self.store.transaction() as database:
            self._select(database, self._request(database, request_id), choice, "operator", True)
        return self.get(request_id)

    def approve(self, request_id, reason):
        _text(reason, "Approval reason")
        with self.store.transaction() as database:
            request = self._request(database, request_id)
            self._unlaunched(request)
            self._fresh(request)
            if not request["profile"]:
                raise StateError("Select a concrete profile before approval")
            database.execute(
                "UPDATE worker_requests SET approval=? WHERE id=?", (reason, request_id)
            )
            self.store._event(
                database,
                request["project_id"],
                "worker.approved",
                {"request_id": request_id, "reason": reason},
            )
        return self.get(request_id)

    def refresh(self, session_id, request_id):
        with self.store.transaction() as database:
            request = self._request(database, request_id)
            self._writer(database, session_id, request["project_id"])
            self._unlaunched(request)
            digest, error, evidence = self._snapshot(database, request["project_id"])
            database.execute(
                "UPDATE worker_requests SET policy_digest=?,policy_error=?,evidence_json=?,"
                "profile_json=NULL,selection_source=NULL,approval=NULL,state='pending',"
                "generation=generation+1 WHERE id=?",
                (digest, error, evidence, request_id),
            )
            self.store._event(
                database,
                request["project_id"],
                "worker.refreshed",
                {"request_id": request_id, "policy_digest": digest},
                session_id,
            )
        return self.get(request_id)

    def seed_plan(self, plan_id):
        with self.store.transaction() as database:
            plan = database.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()
            if not plan or plan["status"] != "approved" or plan["origin_event_id"] is None:
                return
            for node in database.execute("SELECT * FROM graph_nodes WHERE plan_id=?", (plan_id,)):
                specification = json.loads(node["specification"])
                if specification["kind"] != "approval":
                    self._insert(
                        database,
                        plan["session_id"],
                        plan["project_id"],
                        encode(specification),
                        specification.get("mode", "read"),
                        plan["origin_event_id"],
                        plan_id,
                        node["node_id"],
                    )

    def monitor_requests(self, project_id, cursor):
        pending = []
        with contextlib.closing(self.store.connect()) as database:
            for request in self.list(project_id):
                if (
                    not request["plan_id"]
                    or request["state"] != "pending"
                    or request["origin_event_id"] > cursor
                ):
                    continue
                try:
                    self._plan_gate(database, request)
                except StateError:
                    continue
                snapshot = database.execute(
                    "SELECT * FROM worker_policies WHERE digest=?", (request["policy_digest"],)
                ).fetchone()
                policy = {"available": False, "error": request["policy_error"]}
                if snapshot:
                    policy = {
                        "available": True,
                        "policy": json.loads(snapshot["policy_json"]),
                        "digest": snapshot["digest"],
                        "source": snapshot["source"],
                    }
                pending.append({**request, "policy": policy})
        return pending

    def apply_monitor_selections(self, database, task, selections):
        """Must run inside Store.apply_monitor's acceptance transaction."""
        current = database.execute("SELECT * FROM tasks WHERE id=?", (task["id"],)).fetchone()
        if (
            not current
            or current["role"] != "monitor"
            or current["state"] != "succeeded"
            or current["processed"]
            or current["cancel_requested"]
        ):
            raise StateError(
                "Worker selections require a persisted successful unaccepted monitor result"
            )
        if not isinstance(selections, list) or len(selections) > 256:
            raise StateError("Monitor selections must be an array of at most 256 entries")
        try:
            report = json.loads(current["result"])
            if not isinstance(report, dict):
                raise TypeError("Expected monitor report object")
            supplied = (
                json.loads(current["prompt"]).get("worker_requests", []) if selections else []
            )
        except (ValueError, TypeError) as error:
            raise StateError("Monitor prompt and result must be saved JSON") from error
        if report.get("worker_selections", []) != selections or not isinstance(selections, list):
            raise StateError("Selections must match the saved monitor result")
        if (
            not isinstance(supplied, list)
            or len(supplied) > 256
            or any(
                not isinstance(item, dict) or not isinstance(item.get("id"), str)
                for item in supplied
            )
        ):
            raise StateError("Saved monitor request context is invalid")
        contexts = {item["id"]: item for item in supplied}
        if len(contexts) != len(supplied):
            raise StateError("Saved monitor context contains duplicate request IDs")
        seen = set()
        for selection in selections:
            if not isinstance(selection, dict) or set(selection) != {"request_id", "choice"}:
                raise StateError("Invalid monitor worker selection")
            request_id = selection["request_id"]
            if request_id in seen or request_id not in contexts:
                raise StateError("Monitor selected a duplicate or unsupplied worker request")
            seen.add(request_id)
            request = self._request(database, request_id)
            context = contexts[request_id]
            if not request["plan_id"] or request["project_id"] != current["project_id"]:
                raise StateError("Monitor can select only its project plan requests")
            for key in (
                "generation",
                "policy_digest",
                "plan_id",
                "plan_version",
                "node_id",
                "mode",
                "origin_event_id",
                "evidence",
            ):
                if context.get(key) != request[key]:
                    raise StateError("Monitor request context is stale")
            self._plan_gate(database, request)
            self._select(database, request, selection["choice"], "monitor:" + current["id"])

    def _project_gate(self, database, project_id):
        if database.execute(
            "SELECT 1 FROM service WHERE key=?", ("pause:" + project_id,)
        ).fetchone():
            raise StateError("Project is paused")
        if database.execute(
            "SELECT 1 FROM holds WHERE project_id=? AND resolved IS NULL", (project_id,)
        ).fetchone():
            raise StateError("Project has unresolved monitor holds")

    def _plan_gate(self, database, request):
        plan, node = self._node(database, request["plan_id"], request["node_id"])
        latest = database.execute(
            "SELECT MAX(version) FROM plans WHERE project_id=?", (request["project_id"],)
        ).fetchone()[0]
        if (
            plan["status"] != "approved"
            or plan["version"] != latest
            or plan["version"] != request["plan_version"]
        ):
            raise StateError("Plan is not the current approved version")
        return plan, node

    def _dependencies(self, database, plan_id, specification, seen=None):
        commits = []
        seen = set() if seen is None else seen
        for dependency in specification["depends_on"]:
            if dependency in seen:
                continue
            seen.add(dependency)
            _, node = self._node(database, plan_id, dependency)
            kind = json.loads(node["specification"])["kind"]
            if node["state"] != ("approved" if kind == "approval" else "completed"):
                raise StateError("Prerequisites require explicit acceptance")
            commits.extend(
                self._dependencies(database, plan_id, json.loads(node["specification"]), seen)
            )
            if kind != "approval":
                row = database.execute(
                    "SELECT * FROM worker_requests WHERE plan_id=? AND node_id=? AND state='accepted'",
                    (plan_id, dependency),
                ).fetchone()
                if not row:
                    raise StateError("Prerequisite lacks an accepted worker result")
                workspace = json.loads(row["workspace_json"] or "{}")
                if workspace.get("commit"):
                    commits.append(workspace["commit"])
        return commits

    def _gate(self, database, request):
        self._fresh(request)
        self._project_gate(database, request["project_id"])
        self._origin(
            database, request["project_id"], request["origin_event_id"], request["plan_id"]
        )
        if not request["profile"]:
            raise StateError("Worker has no pinned profile")
        source = request["selection_source"] or ""
        if source.startswith("monitor:"):
            monitor = database.execute("SELECT * FROM tasks WHERE id=?", (source[8:],)).fetchone()
            if (
                not monitor
                or monitor["role"] != "monitor"
                or monitor["state"] != "succeeded"
                or not monitor["processed"]
                or monitor["project_id"] != request["project_id"]
            ):
                raise StateError("Plan selection lacks an accepted monitor result")
        elif source != "operator" and (source != "foreground" or request["plan_id"]):
            raise StateError("Invalid selection authority")
        config = load_config(self.store.home, request["project_id"])
        if not config["workers"]["enabled"] or not config["routing"]["enabled"]:
            raise StateError("Workers and routing must be enabled")
        write_plan = False
        commits = []
        if request["plan_id"]:
            _, node = self._plan_gate(database, request)
            specification = json.loads(node["specification"])
            if (
                specification["kind"] == "approval"
                or specification.get("mode", "read") != request["mode"]
            ):
                raise StateError("Worker grant does not match node permissions")
            commits = self._dependencies(database, request["plan_id"], specification)
            write_plan = request["mode"] == "write"
        if (
            request["profile"].get("requires_approval")
            or (request["mode"] == "write" and not write_plan)
        ) and not request["approval"]:
            raise StateError("Explicit operator approval is required")
        return config, commits

    def _propagate_failures(self, database):
        for plan in database.execute(
            "SELECT * FROM plans WHERE status='approved' AND graph_json IS NOT NULL"
        ).fetchall():
            from .graphs import ready_nodes

            config = load_config(self.store.home, plan["project_id"])
            states = {
                row["node_id"]: row["state"]
                for row in database.execute(
                    "SELECT node_id,state FROM graph_nodes WHERE plan_id=?", (plan["id"],)
                )
            }
            readiness = ready_nodes(
                json.loads(plan["graph_json"]),
                states,
                config["execution"]["max_parallel"],
                config["execution"]["dependency_failure"],
            )
            for state in ("blocked", "cancelled"):
                for node_id in readiness[state]:
                    if states[node_id] not in ("pending", "ready", "waiting"):
                        continue
                    database.execute(
                        "UPDATE graph_nodes SET state=? WHERE plan_id=? AND node_id=?",
                        (state, plan["id"], node_id),
                    )
                    database.execute(
                        "UPDATE worker_requests SET state=?,error=? "
                        "WHERE plan_id=? AND node_id=? AND task_id IS NULL",
                        (state, "Prerequisite failed", plan["id"], node_id),
                    )

    def dispatch_ready(self):
        with self.store.transaction() as database:
            self._propagate_failures(database)
            for row in database.execute(
                "SELECT * FROM worker_requests WHERE state='selected' AND task_id IS NULL ORDER BY created"
            ).fetchall():
                request = _decode(row)
                try:
                    config, commits = self._gate(database, request)
                except ValueError:
                    continue
                active = database.execute(
                    "SELECT COUNT(*) FROM tasks WHERE role='worker' AND project_id=? "
                    "AND state IN ('queued','starting','running')",
                    (request["project_id"],),
                ).fetchone()[0]
                if active >= config["execution"]["max_parallel"]:
                    continue
                config["worker"] = {
                    "request_id": request["id"],
                    "generation": request["generation"],
                    "profile": request["profile"],
                    "mode": request["mode"],
                    "policy_digest": request["policy_digest"],
                    "project_root": self.store.project(request["project_id"])["root"],
                    "dependency_commits": commits,
                }
                task_id = self.store._enqueue(
                    database,
                    request["project_id"],
                    request["session_id"],
                    "worker",
                    request["brief"],
                    config,
                    plan_id=request["plan_id"],
                    worker_request_id=request["id"],
                )
                database.execute(
                    "UPDATE worker_requests SET task_id=?,state='queued' WHERE id=?",
                    (task_id, request["id"]),
                )
                if request["plan_id"]:
                    database.execute(
                        "UPDATE graph_nodes SET state='running' WHERE plan_id=? AND node_id=?",
                        (request["plan_id"], request["node_id"]),
                    )

    def enqueue_check(self, database, project_id, session_id, prompt, config, plan_id, request_id):
        request = self._request(database, request_id)
        self._unlaunched(request)
        self._gate(database, request)
        if (
            request["state"] != "selected"
            or request["project_id"] != project_id
            or request["session_id"] != session_id
            or request["plan_id"] != plan_id
            or request["brief"] != prompt
        ):
            raise StateError("Worker enqueue does not match the durable request")
        self._config_check(request, config)

    def _config_check(self, request, config):
        grant = config.get("worker", {})
        for key, value in {
            "request_id": request["id"],
            "generation": request["generation"],
            "profile": request["profile"],
            "mode": request["mode"],
            "policy_digest": request["policy_digest"],
            "project_root": self.store.project(request["project_id"])["root"],
        }.items():
            if grant.get(key) != value:
                raise StateError("Worker task configuration does not match its saved grant")

    def task_check(self, database, task, *, active=False):
        request = self._request(database, task["worker_request_id"])
        if (
            request["task_id"] != task["id"]
            or request["state"] != "queued"
            or task["role"] != "worker"
            or task["cancel_requested"]
            or task["project_id"] != request["project_id"]
            or task["plan_id"] != request["plan_id"]
            or task["session_id"] != request["session_id"]
            or task["prompt"] != request["brief"]
        ):
            raise StateError("Invalid or consumed worker task grant")
        if active and (task["state"] not in ("starting", "running") or not task["token"]):
            raise StateError("Worker requires a current active attempt")
        live_config, commits = self._gate(database, request)
        active_count = database.execute(
            "SELECT COUNT(*) FROM tasks WHERE project_id=? AND role='worker' "
            "AND state IN ('starting','running') AND id<>?",
            (request["project_id"], task["id"]),
        ).fetchone()[0]
        if active_count >= live_config["execution"]["max_parallel"]:
            raise StateError("Project worker concurrency is exhausted")
        config = json.loads(task["config_json"])
        self._config_check(request, config)
        if config["worker"].get("dependency_commits") != commits:
            raise StateError("Worker dependency provenance changed")
        return request

    def _attempt(self, database, task, terminal=False):
        current = database.execute("SELECT * FROM tasks WHERE id=?", (task["id"],)).fetchone()
        if (
            not current
            or current["token"] != task.get("token")
            or (not task.get("token") and not (terminal and current["state"] == "cancelled"))
        ):
            raise StateError("Stale worker attempt")
        if terminal:
            request = self._request(database, current["worker_request_id"])
            if (
                current["role"] != "worker"
                or current["state"] not in TERMINAL
                or request["task_id"] != current["id"]
            ):
                raise StateError("Expected a linked terminal worker attempt")
        else:
            request = self.task_check(database, current, active=True)
        return current, request

    def start_check(self, task):
        with self.store.transaction() as database:
            current, _ = self._attempt(database, task)
            return json.loads(current["config_json"])["worker"]

    def record_workspace(self, task, workspace):
        if not isinstance(workspace, dict):
            raise StateError("Workspace provenance must be a program-generated object")
        with self.store.transaction() as database:
            current, request = self._attempt(database, task, terminal=task["state"] in TERMINAL)
            if current["processed"] or request["state"] != "queued":
                raise StateError("Worker workspace is already sealed")
            database.execute(
                "UPDATE worker_requests SET workspace_json=? WHERE id=?",
                (encode(workspace), request["id"]),
            )

    def process_result(self, task):
        with self.store.transaction() as database:
            current, request = self._attempt(database, task, terminal=True)
            if current["processed"]:
                return
            state, report, error = current["state"], None, current["error"]
            if state == "succeeded":
                try:

                    def unique_fields(pairs):
                        value = {}
                        for key, item in pairs:
                            if key in value:
                                raise ValueError("Duplicate worker report field")
                            value[key] = item
                        return value

                    if len(current["result"].encode()) > 1024 * 1024:
                        raise ValueError("Worker report exceeds 1 MiB")
                    report = json.loads(current["result"], object_pairs_hook=unique_fields)
                    if not isinstance(report, dict) or set(report) != {
                        "summary",
                        "changes",
                        "checks",
                        "remaining_issues",
                    }:
                        raise ValueError(
                            "Worker report must contain exactly summary, changes, checks, remaining_issues"
                        )
                    _text(report["summary"], "Worker summary")
                    for key in ("changes", "checks", "remaining_issues"):
                        if not isinstance(report[key], list) or len(report[key]) > 256:
                            raise ValueError("Worker report lists must be bounded arrays")
                        for item in report[key]:
                            _text(item, key)
                    if request["mode"] == "write":
                        workspace = request["workspace"] or {}
                        if (
                            not re.fullmatch(
                                r"[0-9a-f]{40}|[0-9a-f]{64}", workspace.get("commit", "")
                            )
                            or not workspace.get("diff_path")
                            or not workspace.get("path")
                            or not isinstance(workspace.get("changed_files"), list)
                        ):
                            raise ValueError(
                                "Write result lacks program-captured commit and diff provenance"
                            )
                    state = "candidate"
                except (TypeError, ValueError) as failure:
                    state, error = "failed", str(failure)
            database.execute(
                "UPDATE worker_requests SET state=?,result_json=?,error=? WHERE id=?",
                (state, encode(report) if report else None, error, request["id"]),
            )
            if request["plan_id"]:
                database.execute(
                    "UPDATE graph_nodes SET state=?,evidence=? WHERE plan_id=? AND node_id=?",
                    (
                        "awaiting_review" if state == "candidate" else state,
                        encode({"request_id": request["id"]}),
                        request["plan_id"],
                        request["node_id"],
                    ),
                )
            database.execute("UPDATE tasks SET processed=1 WHERE id=?", (current["id"],))
            self.store._event(
                database,
                request["project_id"],
                "worker." + state,
                {"request_id": request["id"], "error": error},
                task_id=current["id"],
                notify=True,
            )

    def accept(self, request_id, reason):
        _text(reason, "Acceptance reason")
        with self.store.transaction() as database:
            request = self._request(database, request_id)
            if request["state"] != "candidate":
                raise StateError("Only a completed candidate can be accepted")
            self._gate(database, request)
            database.execute(
                "UPDATE worker_requests SET state='accepted',accepted=? WHERE id=?",
                (reason, request_id),
            )
            if request["plan_id"]:
                database.execute(
                    "UPDATE graph_nodes SET state='completed' WHERE plan_id=? AND node_id=?",
                    (request["plan_id"], request["node_id"]),
                )
            self.store._event(
                database,
                request["project_id"],
                "worker.accepted",
                {"request_id": request_id, "reason": reason},
                notify=True,
            )
        return self.get(request_id)

    def approve_node(self, plan_id, node_id, reason):
        _text(reason, "Approval reason")
        with self.store.transaction() as database:
            plan, node = self._node(database, plan_id, node_id)
            self._project_gate(database, plan["project_id"])
            self._plan_gate(
                database,
                {
                    "plan_id": plan_id,
                    "node_id": node_id,
                    "project_id": plan["project_id"],
                    "plan_version": plan["version"],
                },
            )
            specification = json.loads(node["specification"])
            if specification["kind"] != "approval" or node["state"] == "approved":
                raise StateError("Only an unapproved approval node can be approved")
            self._dependencies(database, plan_id, specification)
            database.execute(
                "UPDATE graph_nodes SET state='approved',evidence=? WHERE plan_id=? AND node_id=?",
                (encode({"reason": reason}), plan_id, node_id),
            )
            self.store._event(
                database,
                plan["project_id"],
                "worker.node_approved",
                {"plan_id": plan_id, "node_id": node_id, "reason": reason},
                notify=True,
            )
        return {"plan_id": plan_id, "node_id": node_id, "state": "approved"}
