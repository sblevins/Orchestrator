"""Validated, layered configuration for the local supervisor."""

import contextlib
import hashlib
import json
import math
import os
import re
import stat
import tomllib
import unicodedata
from copy import deepcopy
from pathlib import Path

from .models import model_family


class ConfigurationError(ValueError):
    """Configuration cannot safely be used."""


CORE_ROLES = ("orchestrator", "planner", "critic", "monitor")
DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "config" / "default.toml"
ADAPTER_EFFORTS = {
    "claude": {"low", "medium", "high", "xhigh", "max"},
    "pi": {"off", "minimal", "low", "medium", "high", "xhigh", "max"},
}
READ_ONLY_TOOLS = {"Read", "Glob", "Grep"}
SAFE_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
PROJECT_ID = SAFE_IDENTIFIER
MEMORY = re.compile(r"([1-9][0-9]{0,6})([MG])\Z")


def _fail(message):
    raise ConfigurationError(message)


def image_model_identifier(model) -> bool:
    """Accept one exact image-model identifier, in settings and image requests alike."""
    if not isinstance(model, str) or not 0 < len(model) <= 256 or model.startswith("-"):
        return False
    return not any(
        character.isspace() or unicodedata.category(character) in ("Cc", "Cf", "Cs")
        for character in model
    )


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


def _memory(value, context):
    match = MEMORY.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        _fail(f"{context} must be a positive M or G string, such as 512M or 2G")
    megabytes = int(match[1]) * (1024 if match[2] == "G" else 1)
    if not 64 <= megabytes <= 1048576:
        _fail(f"{context} must be between 64M and 1024G")


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


def validate_executor(settings: dict, context="profile", *, allow_effort_selector=False) -> None:
    """Keep the requested provider on its configured harness, without fallback."""
    adapter = settings.get("adapter", settings.get("harness"))
    model = settings.get("model")
    _text(model, f"{context}.model")
    provider = settings.get("provider")
    if adapter == "claude":
        if provider not in (None, "anthropic") or not (
            model.lower().startswith("claude-") or model_family(model)
        ):
            _fail(
                f"{context}: Claude Code requires an Anthropic claude-* ID or family "
                "(opus, sonnet, haiku, fable); use Pi otherwise"
            )
    elif adapter == "pi":
        _text(provider, f"{context}.provider")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", provider):
            _fail(f"{context}.provider must be a provider identifier")
        if "anthropic" in provider.lower() or "claude" in model.lower() or model_family(model):
            _fail(f"{context}: use Claude Code for Anthropic models, not Pi")
    else:
        _fail(f"{context}: use claude for Anthropic models or pi with an explicit provider")
    if not isinstance(settings.get("effort"), str) or (
        settings["effort"] not in ADAPTER_EFFORTS[adapter]
        and not (
            allow_effort_selector and adapter == "pi" and settings["effort"] == "max-supported"
        )
    ):
        _fail(f"{context}.effort is unsupported by {adapter}; no silent effort downgrade")


def validate_config(config: dict) -> None:
    """Validate required structure and safety limits without model-name enums."""
    if not isinstance(config, dict):
        _fail("config must be a table")
    if "permissions" in config:
        permissions = _table(config, "permissions")
        choices = {"coordinator_approvals", "require_write_approval", "enforce_monitor_holds"}
        if set(permissions) - choices:
            _fail("permissions supports only: " + ", ".join(sorted(choices)))
        if any(type(value) is not bool for value in permissions.values()):
            _fail("permissions settings must be booleans")
    if "images" in config:
        images = _table(config, "images")
        allowed_image_fields = {"enabled", "model", "size", "quality", "output_format"}
        if set(images) != allowed_image_fields or type(images.get("enabled")) is not bool:
            _fail(
                "images requires enabled, model, size, quality and output_format; credentials belong in OPENAI_API_KEY, not project settings"
            )
        if not image_model_identifier(images.get("model")):
            _fail(
                "images.model must be one exact image-model identifier of at most 256 "
                "characters, without a leading hyphen, whitespace or control characters"
            )
        for field, choices in {
            "size": {"auto", "1024x1024", "1536x1024", "1024x1536"},
            "quality": {"auto", "low", "medium", "high"},
            "output_format": {"png", "jpeg"},
        }.items():
            if not isinstance(images.get(field), str) or images[field] not in choices:
                _fail(f"images.{field} must be one of: {', '.join(sorted(choices))}")
    supervisor = _table(config, "supervisor")
    for key in ("poll_seconds", "heartbeat_seconds", "stale_seconds", "monitor_interval_seconds"):
        _number(supervisor.get(key), f"supervisor.{key}", 0.1, 86400)
    if supervisor["stale_seconds"] <= supervisor["heartbeat_seconds"]:
        _fail("supervisor.stale_seconds must exceed heartbeat_seconds")
    _number(supervisor.get("max_parallel"), "supervisor.max_parallel", 1, 64, True)
    _number(
        supervisor.get("monitor_batch_events"), "supervisor.monitor_batch_events", 1, 10000, True
    )
    _number(supervisor.get("task_timeout_seconds"), "supervisor.task_timeout_seconds", 1, 86400)
    for kind in ("task", "frontend"):
        _number(supervisor.get(f"{kind}_cpus"), f"supervisor.{kind}_cpus", 1, 64, True)
        _memory(supervisor.get(f"{kind}_memory"), f"supervisor.{kind}_memory")
    planning = _table(config, "planning")
    if planning.get("structure") != "graph":
        _fail("planning.structure must be graph")
    workflow = planning.get("workflow")
    if not isinstance(workflow, str) or not SAFE_IDENTIFIER.fullmatch(workflow):
        _fail(
            "planning.workflow must be a safe identifier: 1-128 ASCII letters, digits, "
            "underscores or hyphens, beginning with a letter or digit"
        )
    if "templates" in planning:
        from .graphs import validate_plan

        templates = _table(planning, "templates", "planning")
        if len(templates) > 64:
            _fail("planning.templates supports at most 64 templates")
        for name, template in templates.items():
            if not isinstance(name, str) or not SAFE_IDENTIFIER.fullmatch(name):
                _fail(f"planning.templates.{name}: template name must be a safe identifier")
            try:
                validate_plan(template)
            except (ValueError, TypeError) as error:
                _fail(f"planning.templates.{name}: {error}")
    _number(planning.get("max_review_rounds"), "planning.max_review_rounds", 1, 100, True)
    if planning.get("clarification", "material") not in ("material", "always", "none"):
        _fail("planning.clarification must be material, always or none")
    execution = _table(config, "execution")
    if execution.get("mode", "restricted") not in ("restricted", "trusted"):
        _fail("execution.mode must be restricted or trusted")
    if type(execution.get("unattended", False)) is not bool:
        _fail("execution.unattended must be a boolean standing project authorization")
    if execution.get("worker_difficulty", "hard") not in ("easy", "hard", "very-hard"):
        _fail("execution.worker_difficulty must be easy, hard or very-hard")
    base_ref = execution.get("base_ref", "HEAD")
    if (
        not isinstance(base_ref, str)
        or not base_ref.strip()
        or len(base_ref) > 256
        or base_ref.startswith("-")
        or any(ord(character) < 32 for character in base_ref)
    ):
        _fail(
            "execution.base_ref must be an existing Git ref or commit, such as crosschain/integration"
        )
    if "commands" in config:
        commands = _table(config, "commands")
        if set(commands) - {"enabled", "sandbox", "network", "timeout_seconds", "tool_paths"}:
            _fail("commands supports enabled, sandbox, network, timeout_seconds and tool_paths")
        for flag, default in (("enabled", False), ("sandbox", True), ("network", False)):
            if type(commands.get(flag, default)) is not bool:
                _fail(f"commands.{flag} must be a boolean")
        _number(commands.get("timeout_seconds", 300), "commands.timeout_seconds", 1, 900, True)
        paths = commands.get("tool_paths", {})
        if not isinstance(paths, dict) or len(paths) > 64:
            _fail("commands.tool_paths must map at most 64 executable names to absolute paths")
        for name, path in paths.items():
            if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.+-]*", name):
                _fail("commands.tool_paths keys must be executable basenames")
            if not isinstance(path, str) or not Path(path).is_absolute() or "\x00" in path:
                _fail(f"commands.tool_paths.{name} must be an absolute executable path")
    _number(execution.get("max_parallel"), "execution.max_parallel", 1, 64, True)
    if execution.get("dependency_failure") not in ("block", "cancel"):
        _fail("execution.dependency_failure must be block or cancel")
    if "monitoring" in config:
        monitoring = _table(config, "monitoring")
        _number(monitoring.get("quiet_seconds", 20), "monitoring.quiet_seconds", 0, 600)
        _number(
            monitoring.get("foreground_stale_seconds", 900),
            "monitoring.foreground_stale_seconds",
            60,
            86400,
        )
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
    if type(workers.get("enabled")) is not bool or set(workers) != {"enabled"}:
        _fail("workers must contain only a boolean enabled setting")
    routing = _table(config, "routing")
    if (
        type(routing.get("enabled")) is not bool
        or routing.get("rules") != []
        or not isinstance(routing.get("rules"), list)
        or set(routing) - {"enabled", "rules", "first_mate"}
    ):
        _fail("routing uses enabled plus project crew-dispatch.json, not inline rules/defaults")
    if "first_mate" in routing and routing["first_mate"] != {}:
        _fail("Put FirstMate routing preferences in the project's .orchestrator/crew-dispatch.json")
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
        validate_executor(role, f"roles.{name}")
        effort = role.get("effort")
        if not isinstance(effort, str) or effort not in ADAPTER_EFFORTS[adapter]:
            _fail(f"roles.{name}.effort is unsupported by {adapter}")
        if "max_budget_usd" in role:
            _fail(
                f"roles.{name}.max_budget_usd has been removed. "
                "Remove this key from your local/project configuration; "
                "no per-role dollar caps are configured. "
                "Memory, CPU, and timeout limits remain separate from spending."
            )
        for obsolete_key in ("timeout_seconds", "memory", "cpus"):
            if obsolete_key in role:
                _fail(
                    f"roles.{name}.{obsolete_key} has been removed. "
                    "Remove this per-role key; shared supervisor settings handle execution limits."
                )
        tools = role.get("allowed_tools")
        if (
            not isinstance(tools, list)
            or any(not isinstance(tool, str) or tool not in READ_ONLY_TOOLS for tool in tools)
            or len(tools) != len(set(tools))
        ):
            _fail(f"roles.{name}.allowed_tools must be unique read-only Read/Glob/Grep tools")
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
    config = unvalidated_config(home, project_id)
    try:
        validate_config(config)
    except RuntimeError as error:
        raise ConfigurationError(f"cannot validate configuration: {error}") from error
    return config


def repairable_config(home: Path, project_id: str | None = None) -> dict | None:
    """Return validated settings, or None while a project's settings await repair."""
    try:
        return load_config(home, project_id)
    except ConfigurationError:
        return None


def unvalidated_config(home, project_id=None, project_override=None) -> dict:
    """Merge configuration layers, optionally replacing the saved project JSON override."""
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
            if project_override is None:
                project_override, _ = read_project_override(home, project_id)
            config = _merge(config, project_override)
        return config
    except (OSError, RuntimeError) as error:
        raise ConfigurationError(f"cannot resolve configuration path: {error}") from error


PROJECT_SETTINGS_LIMIT = 1024 * 1024


@contextlib.contextmanager
def project_config_directory(home, project_id, *, create=False):
    """Open the private directory without traversing any symlink components."""
    if not isinstance(project_id, str) or not PROJECT_ID.fullmatch(project_id):
        _fail("Invalid bound project identifier")
    path = Path(os.path.abspath(home)) / "config" / "projects"
    with contextlib.ExitStack() as stack:
        directory = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
        stack.callback(os.close, directory)
        for index, component in enumerate(path.parts[1:], start=1):
            if create and index >= len(path.parts) - 2:
                with contextlib.suppress(FileExistsError):
                    os.mkdir(component, mode=0o700, dir_fd=directory)
            directory = os.open(
                component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory
            )
            stack.callback(os.close, directory)
        yield directory


def _json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _fail(f"Duplicate project setting: {key}")
        result[key] = value
    return result


def read_project_override(home, project_id):
    """Read a bounded regular JSON override and its content revision."""
    try:
        with project_config_directory(home, project_id) as directory:
            descriptor = os.open(
                project_id + ".json",
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                dir_fd=directory,
            )
            with os.fdopen(descriptor, "rb") as source:
                if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                    _fail("Project settings must be a regular file")
                raw = source.read(PROJECT_SETTINGS_LIMIT + 1)
        if len(raw) > PROJECT_SETTINGS_LIMIT:
            _fail("Project settings exceed 1 MiB")
        settings = json.loads(raw, object_pairs_hook=_json_object)
        if not isinstance(settings, dict):
            _fail("Project settings must be an object")
        return settings, hashlib.sha256(raw).hexdigest()
    except FileNotFoundError:
        return {}, "missing"
    except (OSError, ValueError, RecursionError) as error:
        raise ConfigurationError(f"Cannot read project settings: {error}") from error


def role_config(config: dict, role: str) -> dict:
    """Return an independent copy of one validated core role's configuration."""
    validate_config(config)
    if not isinstance(role, str) or role not in CORE_ROLES:
        _fail(f"unknown core role: {role!r}")
    return deepcopy(config["roles"][role])
