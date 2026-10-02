"""Owned task-bound worker MCP server, sharing the strict team stdio transport."""

import hmac
import json
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from orchestrator.store import StateError, Store
from orchestrator.team_mcp import TeamMCPServer
from orchestrator.team_mcp import tool_definitions as team_definitions
from orchestrator.workers import WorkerService


def check_worker(context):
    """Revalidate a current attempt, never trust credentials alone."""
    store = Store(Path(context["home"]))
    with store.transaction() as database:
        current = database.execute(
            "SELECT * FROM tasks WHERE id=?", (context["task_id"],)
        ).fetchone()
        token = context.get("token")
        if (
            not current
            or current["role"] != "worker"
            or not isinstance(token, str)
            or not token
            or not current["token"]
            or not hmac.compare_digest(token.encode(), current["token"].encode())
        ):
            raise StateError("Worker access requires a current attempt")
        return WorkerService(store).task_check(database, current, active=True)


def tool_definitions(names, commands=None):
    sandboxed = (commands or {}).get("sandbox", True)
    definitions = team_definitions() + [
        {
            "name": "run_command",
            "description": "Run a bounded command under host-captured policy "
            + (
                "inside the OS sandbox."
                if sandboxed
                else "on the host without an OS sandbox; file-tool path limits do not apply, "
                "so stay within the authorized project scope."
            ),
            "inputSchema": {
                "type": "object",
                "additionalProperties": False,
                "required": ["command"],
                "properties": {
                    "command": {"type": "string", "minLength": 1, "maxLength": 32000},
                    "cwd": {"type": "string"},
                    "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 900},
                },
            },
        }
    ]
    return [definition for definition in definitions if definition["name"] in names]


def execute(options, name, arguments):
    if name not in options["tool_names"] or not isinstance(arguments, dict):
        raise StateError("Unknown worker tool or malformed arguments")
    context = options["worker_context"]
    check_worker(context)
    if name == "run_command":
        from orchestrator.commands import run_command

        return run_command(options["commands"], arguments)
    if name in ("send_team_message", "read_team_messages"):
        from orchestrator.team_messages import execute as team_execute

        return team_execute(**context, name=name, arguments=arguments)
    raise StateError("Unknown worker tool")


class WorkerMCPServer(TeamMCPServer):
    def __init__(self, options):
        super().__init__(**options["worker_context"])
        self.options = options

    def handle(self, message):
        # Delegate envelopes, initialization, notifications and other methods to
        # the shared implementation; only valid initialized tool requests differ.
        if (
            not isinstance(message, dict)
            or message.get("jsonrpc") != "2.0"
            or set(message) - {"jsonrpc", "id", "method", "params"}
            or type(message.get("id")) not in (str, int)
            or not self.initialized
            or message.get("method") not in ("tools/list", "tools/call")
        ):
            return super().handle(message)
        response = {"jsonrpc": "2.0", "id": message["id"]}
        params = message.get("params", {})
        if (
            not isinstance(params, dict)
            or (message["method"] == "tools/list" and params)
            or (
                message["method"] == "tools/call"
                and (set(params) - {"name", "arguments"} or not isinstance(params.get("name"), str))
            )
        ):
            response["error"] = {"code": -32602, "message": "Invalid parameters"}
            return response
        try:
            if message["method"] == "tools/list":
                check_worker(self.options["worker_context"])
                response["result"] = {
                    "tools": tool_definitions(
                        self.options["tool_names"], self.options.get("commands")
                    )
                }
            else:
                value = execute(self.options, params["name"], params.get("arguments", {}))
                response["result"] = {
                    "content": [{"type": "text", "text": json.dumps(value)}],
                    "isError": False,
                }
        except Exception as error:  # noqa: BLE001 - sanitize errors at the MCP boundary
            # Owned validation errors describe the fix; never expose arbitrary process output.
            message = (
                str(error)[:1000]
                if isinstance(error, (StateError, ValueError))
                else (
                    "Worker tool failed ("
                    + type(error).__name__
                    + "). Check arguments and current attempt authority."
                )
            )
            response["result"] = {"content": [{"type": "text", "text": message}], "isError": True}
        return response


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(2)
    WorkerMCPServer(json.loads(sys.argv[1])).serve()
