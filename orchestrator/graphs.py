"""Bounded planning DAGs, dependency scheduling, and editable workflow templates."""

import heapq
import json
import os
import re
import stat
from copy import deepcopy
from pathlib import Path


class GraphError(ValueError):
    """A plan, state snapshot, or workflow cannot safely be used."""


MAX_NODES = 256
MAX_TEXT = 8192
MAX_ITEMS = 64
MAX_WORKFLOW_BYTES = 4 * 1024 * 1024
WORKFLOW_DIRECTORY = Path(__file__).resolve().parent.parent / "workflows"
IDENTIFIER_PATTERN = r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}"
IDENTIFIER = re.compile(IDENTIFIER_PATTERN + r"\Z")
STATES = frozenset(
    {
        "pending",
        "ready",
        "waiting",
        "running",
        "completed",
        "succeeded",
        "failed",
        "cancelled",
        "blocked",
        "approved",
        "unknown",
        "awaiting_review",
    }
)
FAILURES = frozenset({"failed", "cancelled", "blocked"})


def planning_schema() -> dict:
    """Return a fresh JSON Schema; graph-wide constraints are enforced in Python."""
    text = {
        "type": "string",
        "minLength": 1,
        "maxLength": MAX_TEXT,
        "pattern": r"^(?![\s\S]*\u0000)[\s\S]*\S[\s\S]*$",
    }
    identifier = {
        "type": "string",
        "minLength": 1,
        "maxLength": 128,
        "pattern": "^" + IDENTIFIER_PATTERN + "$(?![\\s\\S])",
    }
    text_list = {"type": "array", "maxItems": MAX_ITEMS, "items": text}
    node_properties = {
        "id": identifier,
        "title": {**text, "maxLength": 256},
        "description": text,
        "depends_on": {
            "type": "array",
            "maxItems": MAX_NODES - 1,
            "uniqueItems": True,
            "items": identifier,
        },
        "acceptance_criteria": {**text_list, "minItems": 1},
        "kind": {"type": "string", "enum": ["work", "review", "approval"]},
        "mode": {"type": "string", "enum": ["read", "write"], "default": "read"},
    }
    properties = {
        "summary": text,
        "assumptions": text_list,
        "risks": text_list,
        "questions": text_list,
        "nodes": {
            "type": "array",
            "minItems": 1,
            "maxItems": MAX_NODES,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [key for key in node_properties if key != "mode"],
                "properties": node_properties,
            },
        },
    }
    return deepcopy(
        {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "additionalProperties": False,
            "required": list(properties),
            "properties": properties,
        }
    )


def _object(value, keys, context):
    if type(value) is not dict or set(value) != set(keys):
        raise GraphError(f"{context} must contain exactly: {', '.join(keys)}")


def _text(value, context, maximum=MAX_TEXT):
    if (
        type(value) is not str
        or not 1 <= len(value) <= maximum
        or not value.strip()
        or "\x00" in value
    ):
        raise GraphError(f"{context} must be nonblank text of 1-{maximum} characters without NUL")


def _identifier(value, context):
    if type(value) is not str or not IDENTIFIER.fullmatch(value):
        raise GraphError(f"{context} must be a safe identifier of 1-128 ASCII characters")


def _list(value, context, minimum=0, maximum=MAX_ITEMS):
    if type(value) is not list or not minimum <= len(value) <= maximum:
        raise GraphError(f"{context} must be a list of {minimum}-{maximum} items")


def _topological(nodes):
    """Kahn ordering, breaking all ties by the original node position."""
    positions = {node["id"]: index for index, node in enumerate(nodes)}
    remaining = [len(node["depends_on"]) for node in nodes]
    children = [[] for _ in nodes]
    for index, node in enumerate(nodes):
        for dependency in node["depends_on"]:
            if dependency not in positions:
                raise GraphError(f"node {node['id']} has missing dependency {dependency}")
            children[positions[dependency]].append(index)
    available = [index for index, count in enumerate(remaining) if count == 0]
    heapq.heapify(available)
    ordered = []
    while available:
        index = heapq.heappop(available)
        ordered.append(nodes[index])
        for child in children[index]:
            remaining[child] -= 1
            if remaining[child] == 0:
                heapq.heappush(available, child)
    if len(ordered) != len(nodes):
        raise GraphError("plan contains a dependency cycle")
    return ordered


def validate_plan(value: dict) -> dict:
    """Validate an exact JSON shape and return an independent copy in input order."""
    _object(value, ("summary", "assumptions", "risks", "questions", "nodes"), "plan")
    _text(value["summary"], "summary")
    for field in ("assumptions", "risks", "questions"):
        _list(value[field], field)
        for item in value[field]:
            _text(item, field)
    _list(value["nodes"], "nodes", 1, MAX_NODES)
    identifiers = set()
    for node in value["nodes"]:
        _object(
            {key: item for key, item in node.items() if key != "mode"}
            if type(node) is dict
            else node,
            ("id", "title", "description", "depends_on", "acceptance_criteria", "kind"),
            "node",
        )
        _identifier(node["id"], "node.id")
        if node["id"] in identifiers:
            raise GraphError(f"duplicate node id: {node['id']}")
        identifiers.add(node["id"])
        _text(node["title"], "node.title", 256)
        _text(node["description"], "node.description")
        if type(node["kind"]) is not str or node["kind"] not in ("work", "review", "approval"):
            raise GraphError("node.kind must be work, review, or approval")
        if node.get("mode", "read") not in ("read", "write"):
            raise GraphError("node.mode must be read or write")
        if node.get("mode") == "write" and node["kind"] != "work":
            raise GraphError("Only work nodes can request write mode")
        _list(node["acceptance_criteria"], "node.acceptance_criteria", 1)
        for criterion in node["acceptance_criteria"]:
            _text(criterion, "node.acceptance_criteria")
        _list(node["depends_on"], "node.depends_on", 0, MAX_NODES - 1)
        for dependency in node["depends_on"]:
            _identifier(dependency, "node.depends_on")
        if len(set(node["depends_on"])) != len(node["depends_on"]):
            raise GraphError(f"duplicate dependency on node {node['id']}")
    _topological(value["nodes"])
    return deepcopy(value)


def ready_nodes(
    plan: dict, states: dict[str, str], max_parallel: int = 3, dependency_failure: str = "block"
) -> dict:
    """Compute scheduling decisions, never changing states or marking approvals complete."""
    plan = validate_plan(plan)
    if type(max_parallel) is not int or not 1 <= max_parallel <= 64:
        raise GraphError("max_parallel must be an integer between 1 and 64")
    if type(dependency_failure) is not str or dependency_failure not in ("block", "cancel"):
        raise GraphError("dependency_failure must be block or cancel")
    if type(states) is not dict:
        raise GraphError("states must be a dictionary keyed by node id")
    identifiers = {node["id"] for node in plan["nodes"]}
    for identifier, state in states.items():
        if type(identifier) is not str or identifier not in identifiers:
            raise GraphError(f"unknown state node id: {identifier!r}")
        if type(state) is not str or state not in STATES:
            raise GraphError(f"unsupported state for {identifier}: {state!r}")
    capacity = max(0, max_parallel - sum(state == "running" for state in states.values()))
    result = {"ready": [], "blocked": [], "cancelled": [], "waiting": []}
    successful = set()
    failed = set()
    for node in _topological(plan["nodes"]):
        identifier = node["id"]
        state = states.get(identifier, "pending")
        if state in FAILURES:
            failed.add(identifier)
            if state != "failed":
                result[state].append(identifier)
            continue
        if any(dependency in failed for dependency in node["depends_on"]):
            failed.add(identifier)
            result["blocked" if dependency_failure == "block" else "cancelled"].append(identifier)
            continue
        dependencies_complete = all(dependency in successful for dependency in node["depends_on"])
        complete = (
            state == "approved"
            if node["kind"] == "approval"
            else state in ("completed", "succeeded")
        )
        if dependencies_complete and complete:
            successful.add(identifier)
        elif state == "running" and node["kind"] != "approval":
            # Running jobs already own slots; this function does not relaunch them.
            continue
        elif (
            dependencies_complete
            and node["kind"] != "approval"
            and state in ("pending", "ready", "waiting")
            and capacity
        ):
            result["ready"].append(identifier)
            capacity -= 1
        else:
            result["waiting"].append(identifier)
    return result


def _read_workflow(directory, name, optional=False):
    """Use descriptor-relative no-follow opens, including every directory component.

    Resolving a path then opening it permits a symlink-swap escape. Opening each
    component relative to an already-open directory also rejects dangling links.
    """
    descriptors = []
    try:
        directory = Path(directory).absolute()
        descriptor = os.open(directory.anchor, os.O_RDONLY | os.O_DIRECTORY)
        descriptors.append(descriptor)
        for component in directory.parts[1:]:
            descriptor = os.open(
                component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor
            )
            descriptors.append(descriptor)
        descriptor = os.open(
            name + ".json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor
        )
        descriptors.append(descriptor)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_WORKFLOW_BYTES:
            raise GraphError("workflow must be a regular file of at most 4 MiB")
        with os.fdopen(os.dup(descriptor), "rb") as source:
            content = source.read(MAX_WORKFLOW_BYTES + 1)
        if len(content) > MAX_WORKFLOW_BYTES:
            raise GraphError("workflow exceeds 4 MiB")
        return validate_plan(json.loads(content, object_pairs_hook=_unique_object))
    except FileNotFoundError as error:
        if optional:
            return None
        raise GraphError(f"missing workflow: {directory / (name + '.json')}") from error
    except (OSError, ValueError, RecursionError) as error:
        raise GraphError(f"cannot load workflow {directory / (name + '.json')}: {error}") from error
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise GraphError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def load_workflow(home: Path, name: str) -> dict:
    """Load a complete private template override, otherwise a tracked template."""
    _identifier(name, "workflow name")
    override = _read_workflow(Path(home) / "config" / "workflows", name, optional=True)
    if override is not None:
        return override
    return _read_workflow(WORKFLOW_DIRECTORY, name)


def configured_workflow(home: Path, config: dict) -> dict:
    """Prefer project-layer templates without changing shared workflow files."""
    name = config["planning"]["workflow"]
    templates = config["planning"].get("templates", {})
    return validate_plan(templates[name]) if name in templates else load_workflow(home, name)
