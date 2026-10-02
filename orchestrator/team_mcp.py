"""Private, attempt-bound MCP stdio server. No coordinator tools are exposed."""

import json
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from orchestrator import __version__
from orchestrator.store import StateError
from orchestrator.team_messages import INSTRUCTIONS, _validate, execute

MAX_MESSAGE_BYTES = 128 * 1024
PROTOCOLS = {"2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"}


def tool_definitions():
    return [
        {
            "name": "send_team_message",
            "description": "Send untrusted findings to running team peers.",
            "inputSchema": {
                "type": "object",
                "additionalProperties": False,
                "required": ["message"],
                "properties": {
                    "message": {"type": "string", "minLength": 1, "maxLength": 16000},
                    "recipient": {"type": "integer", "minimum": 0, "maximum": 7},
                    "idempotency_key": {"type": "string", "pattern": "^[A-Za-z0-9_.:-]{1,128}$"},
                },
            },
        },
        {
            "name": "read_team_messages",
            "description": "Poll durable peer findings after a saved cursor.",
            "inputSchema": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "after": {"type": "integer", "minimum": 0, "maximum": 2**63 - 1},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                },
            },
        },
    ]


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate key")
        result[key] = value
    return result


def _constant(value):
    raise ValueError("Nonfinite number")


class TeamMCPServer:
    def __init__(self, home, task_id, token):
        self.home, self.task_id, self.token = home, task_id, token
        self.initialized = False

    def handle(self, message):
        response = {"jsonrpc": "2.0", "id": None}
        if (
            not isinstance(message, dict)
            or message.get("jsonrpc") != "2.0"
            or set(message) - {"jsonrpc", "id", "method", "params"}
            or not isinstance(message.get("method"), str)
            or ("id" in message and type(message["id"]) not in (str, int))
        ):
            response["error"] = {"code": -32600, "message": "Invalid JSON-RPC envelope"}
            return response
        notification = "id" not in message
        response["id"] = message.get("id")
        method = message["method"]
        try:
            params = message.get("params", {})
            if not isinstance(params, dict):
                raise TypeError()
            if notification:
                # Notifications never execute tools or initialize authority.
                return None
            if method == "initialize":
                if (
                    self.initialized
                    or set(params) - {"protocolVersion", "capabilities", "clientInfo"}
                    or not isinstance(params.get("protocolVersion"), str)
                    or not isinstance(params.get("capabilities", {}), dict)
                    or not isinstance(params.get("clientInfo", {}), dict)
                ):
                    raise ValueError()
                self.initialized = True
                version = params["protocolVersion"]
                result = {
                    "protocolVersion": version if version in PROTOCOLS else "2024-11-05",
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "orchestrator_team", "version": __version__},
                    "instructions": INSTRUCTIONS,
                }
            elif not self.initialized:
                raise ValueError()
            elif method in ("ping", "tools/list"):
                if params:
                    raise ValueError()
                result = {} if method == "ping" else {"tools": tool_definitions()}
            elif method == "tools/call":
                if set(params) - {"name", "arguments"} or not isinstance(params.get("name"), str):
                    raise ValueError()
                try:
                    _validate(params["name"], params.get("arguments", {}))
                except StateError as error:
                    response["result"] = {
                        "content": [{"type": "text", "text": str(error)}],
                        "isError": True,
                    }
                    return response
                try:
                    value = execute(
                        home=self.home,
                        task_id=self.task_id,
                        token=self.token,
                        name=params["name"],
                        arguments=params.get("arguments", {}),
                    )
                    result = {
                        "content": [{"type": "text", "text": json.dumps(value)}],
                        "isError": False,
                    }
                except Exception:  # noqa: BLE001 - sanitize errors at the MCP boundary
                    # Never echo credentials, database contents, paths, or attacker-supplied fields.
                    result = {
                        "content": [
                            {
                                "type": "text",
                                "text": "Team tool rejected. Check the declared arguments and active team attempt; "
                                "if the team message budget is exhausted, finish your report.",
                            }
                        ],
                        "isError": True,
                    }
            else:
                response["error"] = {"code": -32601, "message": "Method not supported"}
                return response
            response["result"] = result
        except (ValueError, TypeError):
            response["error"] = {
                "code": -32602,
                "message": "Invalid parameters or server not initialized",
            }
        return response

    def serve(self, input_stream=None, output_stream=None):
        stream = input_stream or sys.stdin.buffer
        output = output_stream or sys.stdout
        while True:
            line = stream.readline(MAX_MESSAGE_BYTES + 1)
            if not line:
                return
            oversized = len(line) > MAX_MESSAGE_BYTES
            try:
                if oversized or not line.endswith(b"\n"):
                    raise ValueError()
                message = json.loads(
                    line.decode("utf-8"), object_pairs_hook=_object, parse_constant=_constant
                )
                response = self.handle(message)
            except (ValueError, RecursionError):
                response = {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {
                        "code": -32700,
                        "message": "Invalid UTF-8 newline JSON message (128 KiB maximum)",
                    },
                }
            if response is not None:
                output.write(json.dumps(response, allow_nan=False) + "\n")
                output.flush()
            if oversized:
                return


if __name__ == "__main__":
    if len(sys.argv) != 4:
        sys.exit(2)
    try:
        TeamMCPServer(Path(sys.argv[1]), sys.argv[2], sys.argv[3]).serve()
    except (OSError, ValueError):
        sys.exit(1)
