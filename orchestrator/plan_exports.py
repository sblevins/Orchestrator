"""Project-scoped, offline presentation snapshots. Exporting never schedules work."""

from __future__ import annotations

import contextlib
import json
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path

from .config import ConfigurationError, load_config
from .graphs import _topological, ready_nodes, validate_plan
from .plan_presentation import presentation_metadata
from .store import StateError, encode, identifier
from .visibility import _display_text, _rows, _view


def _profiles(profile):
    profiles = profile.get("team", [profile]) if profile else []
    return [
        {
            key: _display_text(item[key], 256)
            for key in ("harness", "provider", "model", "effort")
            if key in item
        }
        for item in profiles
    ]


def plan_snapshot(store, plan_id: str, *, project_id: str | None = None) -> dict:
    """Read plan, node, worker, and team state from one SQLite snapshot.

    A bound caller supplies project_id; ownership is checked before any plan content
    is decoded. The operator CLI may inspect any plan in its own home.
    """
    identifier(plan_id)
    with contextlib.closing(store.connect()) as database:
        database.execute("BEGIN")
        plan = database.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()
        if not plan or (project_id is not None and plan["project_id"] != project_id):
            raise StateError("Unknown plan in this project")
        project_id = plan["project_id"]
        readiness_notice = None
        try:
            config = load_config(store.home, project_id)["execution"]
        except ConfigurationError:
            config = None
            readiness_notice = (
                "Project settings need repair. Saved graph and worker state remain visible; "
                "scheduling readiness is unavailable until settings are valid."
            )
        if not plan["graph_json"]:
            raise StateError("This plan has no validated graph yet; export after drafting finishes")
        graph = validate_plan(json.loads(plan["graph_json"]))
        states = {
            row["node_id"]: row["state"]
            for row in database.execute(
                "SELECT node_id,state FROM graph_nodes WHERE plan_id=?", (plan_id,)
            )
        }
        if config:
            readiness = ready_nodes(
                graph, states, config["max_parallel"], config["dependency_failure"]
            )
            scheduling = {
                node_id: category for category, nodes in readiness.items() for node_id in nodes
            }
        else:
            readiness = None
            scheduling = {node["id"]: "unavailable" for node in graph["nodes"]}
        waves = {}
        for node in _topological(graph["nodes"]):
            waves[node["id"]] = 1 + max(
                (waves[dependency] for dependency in node["depends_on"]), default=0
            )
        workers = {node["id"]: [] for node in graph["nodes"]}
        parents = database.execute(
            "SELECT id,node_id FROM worker_requests "
            "WHERE project_id=? AND plan_id=? AND plan_version=? ORDER BY created,id",
            (project_id, plan_id, plan["version"]),
        ).fetchall()
        for parent in parents:
            if parent["node_id"] not in workers:
                continue
            requests = [parent["id"]] + [
                row["child_request_id"]
                for row in database.execute(
                    "SELECT child_request_id FROM worker_group_members "
                    "WHERE parent_request_id=? ORDER BY round,peer_index,child_request_id",
                    (parent["id"],),
                )
            ]
            for request_id in requests:
                row = _rows(database, project_id, request_id)[0]
                view = _view(row, True, detail=True)
                del view["label"]
                profile = json.loads(row["profile_json"]) if row["profile_json"] else {}
                view["profiles"] = _profiles(profile)
                workers[parent["node_id"]].append(view)
        paused = database.execute(
            "SELECT 1 FROM service WHERE key=?", ("pause:" + project_id,)
        ).fetchone()
        snapshot = {
            "schema_version": 1,
            "project_id": project_id,
            "plan_id": plan_id,
            "version": plan["version"],
            "plan_status": plan["status"],
            "captured_at": datetime.now(UTC).isoformat(),
            "paused": bool(paused),
            "max_parallel": config["max_parallel"] if config else None,
            "readiness": readiness,
            "readiness_notice": readiness_notice,
            **{key: graph[key] for key in ("summary", "assumptions", "risks", "questions")},
            "nodes": [
                {
                    **node,
                    "mode": node.get("mode", "read"),
                    "state": states.get(node["id"], "pending"),
                    "readiness": scheduling.get(node["id"]),
                    "wave": waves[node["id"]],
                    "workers": workers[node["id"]],
                }
                for node in graph["nodes"]
            ],
        }

        snapshot.update(presentation_metadata(snapshot["nodes"]))
        return snapshot


@contextlib.contextmanager
def _export_directory(home: Path, project_id: str, plan_id: str):
    """Create private directories using no-follow descriptor-relative traversal."""
    identifier(project_id)
    identifier(plan_id)
    home = Path(os.path.abspath(home))
    path = home / "data" / "projects" / project_id / "plan-exports" / plan_id
    with contextlib.ExitStack() as stack:
        directory = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
        stack.callback(os.close, directory)
        for index, component in enumerate(path.parts[1:], start=1):
            if index >= len(home.parts):
                with contextlib.suppress(FileExistsError):
                    os.mkdir(component, mode=0o700, dir_fd=directory)
            directory = os.open(
                component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory
            )
            stack.callback(os.close, directory)
        yield directory, path


def export_plan(store, plan_id: str, *, project_id: str | None = None) -> dict:
    """Publish a complete immutable bundle, without accepting HTML or output paths."""
    from .plan_rendering import html_page, mermaid_diagram

    snapshot = plan_snapshot(store, plan_id, project_id=project_id)
    mermaid = mermaid_diagram(snapshot)
    files = {
        "index.html": html_page(snapshot, mermaid),
        "plan.mmd": mermaid,
        "snapshot.json": encode(snapshot) + "\n",
    }
    export_id = uuid.uuid4().hex
    temporary = ".pending-" + export_id
    try:
        with _export_directory(store.home, snapshot["project_id"], plan_id) as (directory, path):
            os.mkdir(temporary, mode=0o700, dir_fd=directory)
            pending = os.open(
                temporary, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory
            )
            published = False
            try:
                for name, text in files.items():
                    descriptor = os.open(
                        name,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                        0o600,
                        dir_fd=pending,
                    )
                    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                        output.write(text)
                        output.flush()
                        os.fsync(output.fileno())
                os.fsync(pending)
                os.rename(temporary, export_id, src_dir_fd=directory, dst_dir_fd=directory)
                published = True
                os.fsync(directory)
            except BaseException:
                if not published:
                    for name in files:
                        with contextlib.suppress(FileNotFoundError):
                            os.unlink(name, dir_fd=pending)
                    with contextlib.suppress(FileNotFoundError):
                        os.rmdir(temporary, dir_fd=directory)
                raise
            finally:
                os.close(pending)
    except OSError as error:
        raise StateError(f"Cannot publish private plan export: {error.strerror}") from error
    bundle = path / export_id
    return {
        "plan_id": plan_id,
        "project_id": snapshot["project_id"],
        "version": snapshot["version"],
        "captured_at": snapshot["captured_at"],
        "html_path": str(bundle / "index.html"),
        "html_uri": (bundle / "index.html").as_uri(),
        "mermaid_path": str(bundle / "plan.mmd"),
        "snapshot_path": str(bundle / "snapshot.json"),
        "node_count": len(snapshot["nodes"]),
        "wave_count": max(node["wave"] for node in snapshot["nodes"]),
        "message": "Open html_uri for the standard offline plan view. This is a static snapshot, "
        "not live status or authorization. Re-export to refresh; share the whole bundle only "
        "with intended recipients because it contains project plan and worker report text.",
    }
