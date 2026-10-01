"""Pure argv builders and terminal-result decoders for supervised harnesses."""

import json
import math
from pathlib import Path

from orchestrator.config import ConfigurationError, role_config


class AdapterError(ValueError):
    """A harness command or result cannot safely be used."""


def _argument(value, name, *, allow_empty=False):
    if not isinstance(value, str) or "\x00" in value or (not allow_empty and not value.strip()):
        raise AdapterError(f"{name} must be a string without NUL characters")
    return value


def build_command(
    config: dict,
    role: str,
    prompt: str,
    cwd: Path,
    output_path: Path,
    session_id: str | None = None,
    *,
    project_root: Path | None = None,
    stdin_prompt: bool = False,
) -> list[str]:
    """Build a read-only invocation; session_id resumes an existing conversation.

    Runtime supplies cwd, a sanitized environment, prompt-file stdin, and deadlines.
    Other callers retain positional prompts unless stdin_prompt is enabled.
    No files are read/written here, including the optional harness output artifact.
    """
    try:
        settings = role_config(config, role)
    except ConfigurationError as error:
        raise AdapterError(str(error)) from error
    _argument(prompt, "prompt")
    working_directory = _argument(str(cwd), "cwd")
    result_path = _argument(str(output_path), "output_path")
    if session_id is not None:
        _argument(session_id, "session_id")
        if session_id.startswith("-"):
            raise AdapterError("session_id must not start with a dash")
    adapter = settings["adapter"]
    command = list(config["adapters"][adapter]["command"])
    if adapter == "claude":
        tools = ",".join(settings["allowed_tools"])
        command += [
            "-p",
            "--output-format",
            "json",
            "--model",
            settings["model"],
            "--effort",
            settings["effort"],
            "--permission-mode",
            "dontAsk",
            "--tools",
            tools,
            "--allowedTools",
            tools,
            "--settings",
            "{}",
            "--setting-sources",
            "",
            "--strict-mcp-config",
            "--disable-slash-commands",
            "--max-budget-usd",
            str(settings["max_budget_usd"]),
        ]
        if project_root is not None:
            command += ["--add-dir", _argument(str(project_root), "project_root")]
        if session_id is not None:
            command += ["--resume", session_id]
        if stdin_prompt:
            return command
    else:
        if prompt == "-" and not stdin_prompt:
            raise AdapterError("Codex prompt cannot be the stdin sentinel '-'")
        # Codex accepts TOML values. A JSON-escaped validated effort string is
        # also a TOML basic string; never interpolate unquoted configuration.
        command += [
            "-a",
            "never",
            "exec",
            "-s",
            "read-only",
            "-C",
            working_directory,
            "-m",
            settings["model"],
            "-c",
            "model_reasoning_effort=" + json.dumps(settings["effort"]),
        ]
        if session_id is not None:
            command += ["resume"]
        command += [
            "--ignore-user-config",
            "--ignore-rules",
            "--json",
            "--output-last-message",
            result_path,
        ]
        if stdin_prompt:
            prompt = "-"
        if session_id is not None:
            command += ["--", session_id, prompt]
            return command
    command += ["--", prompt]
    return command


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise AdapterError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _invalid_constant(value):
    raise AdapterError(f"invalid JSON constant: {value}")


def _decode(source):
    return json.loads(source, object_pairs_hook=_unique_object, parse_constant=_invalid_constant)


def _events(stdout):
    if not isinstance(stdout, str) or not stdout.strip():
        raise AdapterError("missing harness output")
    try:
        # Claude prints a single, possibly indented JSON object. Codex prints
        # JSONL. Never salvage a valid suffix from a malformed transcript.
        try:
            events = [_decode(stdout)]
        except json.JSONDecodeError:
            events = [_decode(line) for line in stdout.splitlines() if line.strip()]
    except (ValueError, RecursionError) as error:
        raise AdapterError("malformed harness JSON") from error
    if any(
        not isinstance(event, dict) or not isinstance(event.get("type"), str) for event in events
    ):
        raise AdapterError("harness events must be typed JSON objects")
    return events


def _session(previous, value):
    _argument(value, "harness session ID")
    if previous is not None and previous != value:
        raise AdapterError("conflicting harness session IDs")
    return value


def _cost(value):
    if value is None:
        return None
    if type(value) not in (int, float):
        raise AdapterError("cost must be a finite nonnegative number")
    try:
        valid = math.isfinite(value) and value >= 0
    except OverflowError:
        valid = False
    if not valid:
        raise AdapterError("cost must be a finite nonnegative number")
    return value


def _text(value):
    if not isinstance(value, str) or not value.strip():
        raise AdapterError("successful terminal result is missing text")
    return value


def parse_result(adapter: str, stdout: str, returncode: int) -> dict:
    """Require both zero exit status and an unambiguous terminal success.

    Unknown informational event types are ignored, not treated as success.
    Codex token usage is not a dollar price; its cost is always unknown (None).
    """
    if adapter not in ("claude", "codex"):
        raise AdapterError(f"unsupported adapter: {adapter}")
    if type(returncode) is not int or returncode != 0:
        raise AdapterError(f"{adapter} exited unsuccessfully: {returncode!r}")
    events = _events(stdout)
    session_id = None
    terminal = None
    message = None
    completed_items = {}
    for event in events:
        event_type = event["type"]
        if (
            event_type in ("error", "turn.failed")
            or any(word in event_type.lower() for word in ("interrupt", "cancel", "abort"))
            or event.get("is_error") is True
            or event.get("status") in ("failed", "interrupted", "cancelled", "canceled")
        ):
            raise AdapterError("harness reported failure or interruption")
        if adapter == "claude":
            if "session_id" in event:
                session_id = _session(session_id, event["session_id"])
            if event_type != "result":
                if terminal is not None and event_type in ("assistant", "user", "stream_event"):
                    raise AdapterError("Claude activity after terminal success")
                continue
            if event.get("subtype") != "success" or event.get("is_error") is not False:
                raise AdapterError("Claude did not report terminal success")
            if event.get("stop_reason") in ("interrupted", "cancelled", "canceled", "abort"):
                raise AdapterError("Claude result was interrupted")
            if terminal is not None and terminal != event:
                raise AdapterError("conflicting terminal results")
            terminal = event
        else:
            if event_type == "thread.started":
                session_id = _session(session_id, event.get("thread_id"))
            elif event_type == "turn.completed":
                if terminal is not None and terminal != event:
                    raise AdapterError("conflicting terminal results")
                if message is None:
                    raise AdapterError("Codex completed without an agent message")
                terminal = event
            elif event_type in ("turn.started", "item.started", "item.updated", "item.completed"):
                if terminal is not None:
                    raise AdapterError("Codex activity after terminal success")
                if event_type.startswith("item."):
                    item = event.get("item")
                    if not isinstance(item, dict):
                        raise AdapterError("malformed Codex item")
                    if item.get("status") in ("failed", "interrupted", "cancelled", "canceled"):
                        raise AdapterError("Codex item failed or was interrupted")
                    if event_type == "item.completed":
                        item_id = _argument(item.get("id"), "Codex item ID")
                        if item_id in completed_items:
                            if completed_items[item_id] != item:
                                raise AdapterError("conflicting completed Codex items")
                            continue
                        completed_items[item_id] = item
                        if item.get("type") == "agent_message":
                            message = _text(item.get("text"))
    if terminal is None:
        raise AdapterError("missing terminal success")
    if session_id is None:
        raise AdapterError("missing harness session ID")
    if adapter == "claude":
        if "structured_output" in terminal:
            try:
                message = json.dumps(
                    terminal["structured_output"],
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                )
            except (ValueError, RecursionError) as error:
                raise AdapterError("invalid structured output") from error
        else:
            message = _text(terminal.get("result"))
        cost = _cost(terminal.get("total_cost_usd"))
    else:
        cost = None
    return {"text": message, "session_id": session_id, "cost_usd": cost}
