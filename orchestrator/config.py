"""Validated, layered configuration for the local supervisor."""

import math
import re
import tomllib
from copy import deepcopy
from pathlib import Path


class ConfigurationError(ValueError):
    """Configuration cannot safely be used."""


CORE_ROLES = ("orchestrator", "planner", "critic", "monitor")
DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "config" / "default.toml"
ADAPTER_EFFORTS = {
    "claude": {"low", "medium", "high", "xhigh", "max"},
    "codex": {"minimal", "low", "medium", "high", "xhigh"},
}
READ_ONLY_TOOLS = {"Read", "Glob", "Grep"}
SAFE_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
PROJECT_ID = SAFE_IDENTIFIER
MEMORY = re.compile(r"([1-9][0-9]{0,6})([MG])\Z")


def _fail(message):
    raise ConfigurationError(message)


def _table(parent, key, context="config"):
    value = parent.get(key)
    if not isinstance(value, dict):
        _fail(f"{context}.{key} must be a table")
    return value


def _text(value, context):
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        _fail(f"{context} must be a nonempty string without NUL characters")


def _number(value, context, minimum, maximum, integer=False):
    types = (int,) if integer else (int, float)
    if type(value) not in types or not minimum <= value <= maximum:
        _fail(
            f"{context} must be {'an integer' if integer else 'a number'} "
            f"between {minimum} and {maximum}"
        )
    if not math.isfinite(value):
        _fail(f"{context} must be finite")


def _command(value, context):
    if not isinstance(value, list) or not value:
        _fail(f"{context} must be a nonempty argv list")
    for argument in value:
        _text(argument, context)
    # The adapter supplies all flags. Prefix arguments could override its sandbox,
    # permissions, tools, or configuration, even without an obvious bypass flag.
    if len(value) != 1 or value[0].startswith("-"):
        _fail(f"{context} must contain only an executable, without flags or arguments")
    normalized = value[0].lower().replace("_", "-")
    if any(marker in normalized for marker in ("dangerously", "bypass", "skip-permission")):
        _fail(f"{context} contains a forbidden permission bypass")


def validate_config(config: dict) -> None:
    """Validate required structure and safety limits without model-name enums."""
    if not isinstance(config, dict):
        _fail("config must be a table")
    supervisor = _table(config, "supervisor")
    for key in ("poll_seconds", "heartbeat_seconds", "stale_seconds", "monitor_interval_seconds"):
        _number(supervisor.get(key), f"supervisor.{key}", 0.1, 86400)
    if supervisor["stale_seconds"] <= supervisor["heartbeat_seconds"]:
        _fail("supervisor.stale_seconds must exceed heartbeat_seconds")
    _number(supervisor.get("max_parallel"), "supervisor.max_parallel", 1, 64, True)
    _number(
        supervisor.get("monitor_batch_events"), "supervisor.monitor_batch_events", 1, 10000, True
    )
    planning = _table(config, "planning")
    if planning.get("structure") != "graph":
        _fail("planning.structure must be graph")
    workflow = planning.get("workflow")
    if not isinstance(workflow, str) or not SAFE_IDENTIFIER.fullmatch(workflow):
        _fail(
            "planning.workflow must be a safe identifier: 1-128 ASCII letters, digits, "
            "underscores or hyphens, beginning with a letter or digit"
        )
    _number(planning.get("max_review_rounds"), "planning.max_review_rounds", 1, 100, True)
    execution = _table(config, "execution")
    _number(execution.get("max_parallel"), "execution.max_parallel", 1, 64, True)
    if execution.get("dependency_failure") not in ("block", "cancel"):
        _fail("execution.dependency_failure must be block or cancel")
    if "monitoring" in config:
        monitoring = _table(config, "monitoring")
        if type(monitoring.get("review_every_prompt")) is not bool:
            _fail("monitoring.review_every_prompt must be a boolean")
        routine = monitoring.get("routine_prompts")
        if (
            not isinstance(routine, list)
            or len(routine) > 100
            or any(
                not isinstance(prompt, str)
                or not prompt.strip()
                or len(prompt) > 200
                or prompt != prompt.lower().strip()
                for prompt in routine
            )
        ):
            _fail("monitoring.routine_prompts must be at most 100 normalized exact queries")
    personalization = _table(config, "personalization")
    for key in ("name", "communication_style"):
        _text(personalization.get(key), f"personalization.{key}")
    workers = _table(config, "workers")
    if workers.get("enabled") is not False or set(workers) != {"enabled"}:
        _fail("workers must contain only enabled=false; workers are not implemented")
    routing = _table(config, "routing")
    if (
        routing.get("enabled") is not False
        or routing.get("rules") != []
        or not isinstance(routing.get("rules"), list)
        or set(routing) - {"enabled", "rules", "first_mate"}
    ):
        _fail("routing must remain disabled with empty rules and no worker defaults")
    if "first_mate" in routing and routing["first_mate"] != {}:
        _fail("routing.first_mate must be an empty policy table until routing exists")
    adapters = _table(config, "adapters")
    for name, adapter in adapters.items():
        if name not in ADAPTER_EFFORTS or not isinstance(adapter, dict):
            _fail(f"unsupported specialist adapter: {name}")
        _command(adapter.get("command"), f"adapters.{name}.command")
    roles = _table(config, "roles")
    if set(roles) != set(CORE_ROLES):
        _fail("roles must define exactly orchestrator, planner, critic, and monitor")
    for name in CORE_ROLES:
        role = _table(roles, name, "roles")
        adapter = role.get("adapter")
        if not isinstance(adapter, str) or adapter not in adapters:
            _fail(f"roles.{name}.adapter must name a configured specialist adapter")
        _text(role.get("model"), f"roles.{name}.model")
        effort = role.get("effort")
        if not isinstance(effort, str) or effort not in ADAPTER_EFFORTS[adapter]:
            _fail(f"roles.{name}.effort is unsupported by {adapter}")
        _number(role.get("timeout_seconds"), f"roles.{name}.timeout_seconds", 1, 86400)
        _number(role.get("max_budget_usd"), f"roles.{name}.max_budget_usd", 0.01, 1000)
        _number(role.get("cpus"), f"roles.{name}.cpus", 1, 64, True)
        memory = role.get("memory")
        match = MEMORY.fullmatch(memory) if isinstance(memory, str) else None
        if match is None:
            _fail(f"roles.{name}.memory must be a positive M or G string, such as 512M or 2G")
        megabytes = int(match[1]) * (1024 if match[2] == "G" else 1)
        if not 64 <= megabytes <= 1048576:
            _fail(f"roles.{name}.memory must be between 64M and 1024G")
        tools = role.get("allowed_tools")
        if (
            not isinstance(tools, list)
            or any(not isinstance(tool, str) or tool not in READ_ONLY_TOOLS for tool in tools)
            or len(tools) != len(set(tools))
        ):
            _fail(f"roles.{name}.allowed_tools must be unique read-only Read/Glob/Grep tools")
        if name != "orchestrator" and adapter == "codex" and set(tools) != READ_ONLY_TOOLS:
            _fail(
                f"roles.{name}: Codex supports the full read-only sandbox, not per-tool restrictions"
            )
        if "prompt_path" in role and role["prompt_path"] != f"roles/{name}.md":
            _fail(f"roles.{name}.prompt_path must be roles/{name}.md")
    if "frontends" in config:
        frontends = _table(config, "frontends")
        if frontends.get("preferred") not in ("claude", "pi"):
            _fail("frontends.preferred must be claude or pi")
        for name in ("claude", "pi"):
            frontend = _table(frontends, name, "frontends")
            _command(frontend.get("command"), f"frontends.{name}.command")


def _merge(base, override):
    result = deepcopy(base)
    for key, value in override.items():
        if isinstance(result.get(key), dict) and isinstance(value, dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


def _read(path, optional=False):
    try:
        with path.open("rb") as source:
            return tomllib.load(source)
    except FileNotFoundError as error:
        if optional:
            return {}
        raise ConfigurationError(f"missing configuration: {path}") from error
    except (OSError, ValueError) as error:
        raise ConfigurationError(f"cannot read configuration {path}: {error}") from error


def _private_path(home, relative):
    path = home / relative
    if not path.resolve().is_relative_to(home):
        _fail(f"configuration path escapes home: {path}")
    return path


def load_config(home: Path, project_id: str | None = None) -> dict:
    """Merge tracked defaults, home/config/local.toml, then a project override."""
    try:
        home = Path(home).resolve()
        if project_id is not None and (
            not isinstance(project_id, str) or not PROJECT_ID.fullmatch(project_id)
        ):
            _fail(
                "project_id must be 1-128 ASCII letters, digits, underscores or hyphens; "
                "the first character must be a letter or digit"
            )
        config = _read(DEFAULT_CONFIG)
        config = _merge(config, _read(_private_path(home, "config/local.toml"), optional=True))
        if project_id is not None:
            project_path = _private_path(home, f"config/projects/{project_id}.toml")
            config = _merge(config, _read(project_path, optional=True))
        validate_config(config)
        return config
    except (OSError, RuntimeError) as error:
        raise ConfigurationError(f"cannot resolve configuration path: {error}") from error


def role_config(config: dict, role: str) -> dict:
    """Return an independent copy of one validated core role's configuration."""
    validate_config(config)
    if not isinstance(role, str) or role not in CORE_ROLES:
        _fail(f"unknown core role: {role!r}")
    return deepcopy(config["roles"][role])
