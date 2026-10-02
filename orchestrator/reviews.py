"""Explicit recovery of an independent review without rerunning its planner."""

from .config import load_config
from .onboarding import _session
from .store import StateError, identifier


def retry_review(store, session_id, plan_id, reason) -> dict:
    """Queue only a failed critic, preserving the paid draft and review evidence."""
    identifier(session_id)
    identifier(plan_id)
    if (
        not isinstance(reason, str)
        or not reason.strip()
        or not 1 <= len(reason) <= 12000
        or "\x00" in reason
    ):
        raise StateError(
            "Review retry reason must contain 1..12000 nonblank characters without NUL"
        )
    with store.transaction() as database:
        session, project = _session(database, session_id, writer=True)
        plan = database.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()
        if not plan or plan["project_id"] != project["id"]:
            raise StateError("Review retry requires a plan in the bound project")
        current_version = database.execute(
            "SELECT MAX(version) FROM plans WHERE project_id=?", (project["id"],)
        ).fetchone()[0]
        if plan["version"] != current_version or plan["status"] == "superseded":
            raise StateError("Only the current, non-superseded plan can retry review")
        if plan["graph_json"] is None:
            raise StateError("Review retry requires a saved draft graph; no planner was started")
        planner = database.execute(
            "SELECT * FROM tasks WHERE id=?", (plan["planner_task"],)
        ).fetchone()
        if (
            not planner
            or planner["project_id"] != project["id"]
            or planner["plan_id"] != plan_id
            or planner["role"] != "planner"
            or planner["state"] != "succeeded"
        ):
            raise StateError("Review retry requires the linked persisted successful planner")
        previous_critic = database.execute(
            "SELECT * FROM tasks WHERE id=?", (plan["critic_task"],)
        ).fetchone()
        if (
            not previous_critic
            or previous_critic["project_id"] != project["id"]
            or previous_critic["plan_id"] != plan_id
            or previous_critic["role"] != "critic"
        ):
            raise StateError("Review retry requires this plan's existing linked critic")
        state = previous_critic["state"]
        if state == "unknown":
            raise StateError(
                "Critic outcome is unknown; inspect the saved evidence and arrange an explicit "
                "new review manually. Automatic replay is unsafe; no new planner was started."
            )
        if plan["status"] == "reviewing" and state in {"queued", "starting", "running"}:
            task_id = previous_critic["id"]
        else:
            if plan["status"] not in {"reviewing", "needs_revision"}:
                raise StateError("Only an incomplete review can be retried")
            if state not in {"failed", "cancelled"} and not (
                state == "succeeded" and previous_critic["processed"] and previous_critic["error"]
            ):
                raise StateError("Only a failed, cancelled, or processing-failed critic can retry")
            task_id = store._enqueue(
                database,
                project["id"],
                session["id"],
                "critic",
                previous_critic["prompt"],
                load_config(store.home, project["id"]),
                plan_id=plan_id,
                depends_on=(planner["id"],),
                idempotency_key=f"critic-retry:{previous_critic['id']}",
            )
            database.execute(
                "UPDATE plans SET status='reviewing',critic_task=? WHERE id=?", (task_id, plan_id)
            )
            store._event(
                database,
                project["id"],
                "critic.retry_requested",
                {
                    "plan_id": plan_id,
                    "reason": reason,
                    "previous_critic_id": previous_critic["id"],
                    "critic_task_id": task_id,
                },
                session["id"],
                task_id,
                review_required=True,
                notify=False,
            )
            state = database.execute("SELECT state FROM tasks WHERE id=?", (task_id,)).fetchone()[0]
        return {
            "plan_id": plan_id,
            "task_id": task_id,
            "state": state,
            "reused_draft": True,
            "new_planner_started": False,
        }
