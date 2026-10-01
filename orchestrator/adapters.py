"""Pure argv builders and terminal-result decoders for supervised harnesses."""

import json
import math
import sys
from pathlib import Path

from orchestrator.config import ConfigurationError, role_config, validate_executor


class AdapterError(ValueError):
    """A harness command or result cannot safely be used."""


def _argument(value, name, *, allow_empty=False):
    if not isinstance(value, str) or "\x00" in value or (not allow_empty and not value.strip()):
        raise AdapterError(f"{name} must be a string without NUL characters")
    try:
        value.encode("utf-8", "strict")
    except UnicodeError as error:
        raise AdapterError(f"{name} must be valid Unicode") from error
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
    _argument(str(cwd), "cwd")
    _argument(str(output_path), "output_path")
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
        ]
        if project_root is not None:
            command += ["--add-dir", _argument(str(project_root), "project_root")]
        if session_id is not None:
            command += ["--resume", session_id]
        if stdin_prompt:
            return command
    elif adapter == "pi":
        if session_id is not None:
            raise AdapterError("Pi sessions are ephemeral and cannot be resumed")
        return build_pi_command(
            config,
            settings,
            prompt,
            cwd,
            output_path,
            project_root=project_root,
            stdin_prompt=stdin_prompt,
        )
    else:
        raise AdapterError("unsupported execution adapter; use claude or pi")
    command += ["--", prompt]
    return command


def build_pi_command(
    config, settings, prompt, cwd, output_path, *, project_root=None, stdin_prompt=True, mode="read"
):
    """Build an owned SDK bridge invocation, never execute a configured CLI directly.

    The isolated Python launcher resolves the trusted Pi installation at execution
    time and then starts the pinned SDK with canonical auth and no discovery.
    """
    try:
        validate_executor(settings)
    except ConfigurationError as error:
        raise AdapterError(str(error)) from error
    if settings.get("adapter", settings.get("harness")) != "pi":
        raise AdapterError("Pi builder requires a Pi profile")
    _argument(prompt, "prompt")
    _argument(str(output_path), "output_path")
    if mode not in ("read", "write") or type(stdin_prompt) is not bool:
        raise AdapterError("invalid Pi invocation mode")
    command = config.get("adapters", {}).get("pi", {}).get("command")
    if not isinstance(command, list) or len(command) != 1:
        raise AdapterError("Pi command must contain only one executable")
    executable = _argument(command[0], "Pi executable")
    if executable.startswith("-"):
        raise AdapterError("invalid Pi executable")
    tools = settings.get("allowed_tools")
    if not isinstance(tools, list) or any(not isinstance(tool, str) for tool in tools):
        raise AdapterError("Pi tools must be an explicit list")
    tool_mapping = {
        "Read": ["read", "ls"],
        "Glob": ["find"],
        "Grep": ["grep"],
        "Edit": ["edit"],
        "Write": ["write"],
        **{name: [name] for name in ("read", "ls", "find", "grep", "edit", "write")},
    }
    allowed = {"read", "ls", "find", "grep"} | ({"edit", "write"} if mode == "write" else set())
    selected = []
    for tool in tools:
        if tool not in tool_mapping or not set(tool_mapping[tool]) <= allowed:
            raise AdapterError("Pi profile contains a forbidden tool")
        for name in tool_mapping[tool]:
            if name not in selected:
                selected.append(name)
    if mode == "write" and project_root is None:
        raise AdapterError("Pi write mode requires a separate source project root")
    options = {
        "executable": executable,
        "provider": settings["provider"],
        "model": settings["model"],
        "effort": settings["effort"],
        "mode": mode,
        "tools": selected,
        "cwd": _argument(str(cwd), "cwd"),
        "project_root": _argument(str(project_root), "project_root") if project_root else None,
    }
    if not stdin_prompt:
        options["prompt"] = prompt
    return [
        sys.executable,
        "-I",
        str(Path(__file__).with_name("pi_tools.py")),
        "launch",
        json.dumps(options, ensure_ascii=True, allow_nan=False),
    ]


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


def _parse_pi_result(stdout, returncode):
    """Accept only the owned bridge's verified, settled, clean one-run protocol."""
    if type(returncode) is not int or returncode != 0:
        raise AdapterError(f"pi exited unsuccessfully: {returncode!r}")
    try:
        output_size = len(stdout.encode("utf-8", "strict")) if isinstance(stdout, str) else 0
    except UnicodeError as error:
        raise AdapterError("invalid Pi Unicode") from error
    if not isinstance(stdout, str) or not stdout.endswith("\n") or output_size > 4 * 1024 * 1024:
        raise AdapterError("missing, truncated, or oversized Pi output")
    try:
        records = [_decode(line.removesuffix("\r")) for line in stdout[:-1].split("\n")]
    except (ValueError, RecursionError) as error:
        raise AdapterError("malformed Pi JSONL") from error
    if any(
        not isinstance(record, dict) or not isinstance(record.get("type"), str)
        for record in records
    ):
        raise AdapterError("Pi records must be typed objects")
    if len(records) < 5:
        raise AdapterError("incomplete Pi run")
    preflight, header = records[:2]
    if (
        preflight.get("type") != "orchestrator_pi_preflight"
        or preflight.get("verified") is not True
        or preflight.get("version") != "0.99.2"
        or preflight.get("mode") not in ("read", "write")
        or preflight.get("effort") not in ("off", "minimal", "low", "medium", "high", "xhigh")
        or header.get("type") != "session"
        or header.get("version") != 3
    ):
        raise AdapterError("missing verified Pi preflight/session")
    _argument(header.get("id"), "Pi diagnostic session ID")
    provider = _argument(preflight.get("provider"), "Pi provider")
    model = _argument(preflight.get("model"), "Pi model")
    if "anthropic" in provider.lower() or "claude" in model.lower():
        raise AdapterError("Anthropic model routed to Pi")
    tools = preflight.get("tools")
    allowed = {"read", "ls", "find", "grep"}
    if preflight["mode"] == "write":
        allowed |= {"edit", "write"}
    if (
        not isinstance(tools, list)
        or any(not isinstance(tool, str) for tool in tools)
        or len(tools) != len(set(tools))
        or not set(tools) <= allowed
    ):
        raise AdapterError("invalid verified Pi tools")
    started = ended = settled = turn = False
    opened_message = None
    final = None
    requested = {}
    executing = set()
    completed = set()
    tool_results = set()
    for event in records[2:]:
        kind = event["type"]
        if settled:
            raise AdapterError("Pi activity after settlement")
        if (
            "error" in kind.lower()
            or "retry" in kind.lower()
            or "compaction" in kind.lower()
            or "abort" in kind.lower()
            or kind.startswith("extension_")
        ):
            raise AdapterError("Pi reported failure or unexpected recovery")
        if kind == "agent_start":
            if started:
                raise AdapterError("multiple Pi runs")
            started = True
        elif kind == "turn_start":
            if not started or ended or turn or opened_message:
                raise AdapterError("invalid Pi turn start")
            turn = True
        elif kind == "message_start":
            if not turn or opened_message is not None:
                raise AdapterError("invalid Pi message start")
            message = event.get("message")
            if not isinstance(message, dict) or message.get("role") not in (
                "user",
                "assistant",
                "toolResult",
                "system",
            ):
                raise AdapterError("invalid Pi message")
            opened_message = message["role"]
        elif kind == "message_update":
            if opened_message != "assistant":
                raise AdapterError("Pi update without an assistant message")
        elif kind == "message_end":
            message = event.get("message")
            if (
                not turn
                or opened_message is None
                or not isinstance(message, dict)
                or message.get("role") != opened_message
            ):
                raise AdapterError("unmatched Pi message end")
            opened_message = None
            if message["role"] == "assistant":
                if (
                    message.get("provider") != provider
                    or message.get("model") != model
                    or message.get("stopReason") not in ("stop", "toolUse")
                    or not isinstance(message.get("content"), list)
                    or message.get("errorMessage")
                ):
                    raise AdapterError("Pi assistant failed or changed model")
                final = message
                for block in message["content"]:
                    if not isinstance(block, dict) or block.get("type") not in (
                        "text",
                        "thinking",
                        "toolCall",
                    ):
                        raise AdapterError("invalid Pi content block")
                    if block.get("type") == "toolCall":
                        call_id = _argument(block.get("id"), "Pi tool call ID")
                        if block.get("name") not in tools or call_id in requested:
                            raise AdapterError("forbidden or repeated Pi tool request")
                        requested[call_id] = block["name"]
            elif message["role"] == "toolResult":
                call_id = message.get("toolCallId")
                if (
                    not isinstance(call_id, str)
                    or call_id not in completed
                    or call_id in tool_results
                    or message.get("isError") is not False
                    or message.get("toolName") != requested[call_id]
                ):
                    raise AdapterError("Pi tool result failed or mismatched")
                tool_results.add(call_id)
        elif kind == "tool_execution_start":
            call_id = event.get("toolCallId")
            if (
                not turn
                or not isinstance(call_id, str)
                or call_id not in requested
                or call_id in executing
                or call_id in completed
                or event.get("toolName") != requested[call_id]
            ):
                raise AdapterError("invalid Pi tool start")
            executing.add(call_id)
        elif kind == "tool_execution_update":
            call_id = event.get("toolCallId")
            if not isinstance(call_id, str) or call_id not in executing:
                raise AdapterError("unmatched Pi tool update")
        elif kind == "tool_execution_end":
            call_id = event.get("toolCallId")
            if (
                not isinstance(call_id, str)
                or call_id not in executing
                or event.get("isError") is not False
                or event.get("toolName") != requested[call_id]
            ):
                raise AdapterError("Pi tool failed or did not start")
            executing.remove(call_id)
            completed.add(call_id)
        elif kind == "turn_end":
            if not turn or opened_message or executing or event.get("message") != final:
                raise AdapterError("invalid Pi turn end")
            turn = False
        elif kind == "agent_end":
            if (
                not started
                or ended
                or turn
                or opened_message
                or executing
                or event.get("willRetry") is not False
                or not isinstance(event.get("messages"), list)
            ):
                raise AdapterError("invalid Pi agent end")
            assistants = [
                message
                for message in event["messages"]
                if isinstance(message, dict) and message.get("role") == "assistant"
            ]
            if not assistants or assistants[-1] != final:
                raise AdapterError("conflicting Pi final assistant")
            ended = True
        elif kind == "agent_settled":
            if (
                not ended
                or executing
                or set(requested) != completed
                or completed != tool_results
                or not final
                or final.get("stopReason") != "stop"
                or any(block.get("type") == "toolCall" for block in final["content"])
            ):
                raise AdapterError("Pi settled without completed final response")
            settled = True
        elif kind == "queue_update":
            if event.get("steering") or event.get("followUp"):
                raise AdapterError("unexpected queued Pi work")
        else:
            raise AdapterError(f"unexpected Pi event: {kind}")
    if not settled:
        raise AdapterError("missing Pi settlement")
    texts = []
    for block in final["content"]:
        if block.get("type") == "text":
            if not isinstance(block.get("text"), str):
                raise AdapterError("invalid final Pi text")
            _argument(block["text"], "Pi final text", allow_empty=True)
            texts.append(block["text"])
    return {"text": _text("\n".join(texts)), "session_id": None, "cost_usd": None}


def parse_result(adapter: str, stdout: str, returncode: int) -> dict:
    """Require both zero exit status and an unambiguous terminal success.

    Unknown informational event types are ignored, not treated as success.
    Codex token usage is not a dollar price; its cost is always unknown (None).
    """
    if adapter == "pi":
        return _parse_pi_result(stdout, returncode)
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
