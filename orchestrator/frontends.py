"""Native terminal command builders, not a second supervisor."""
from pathlib import Path
import json
import os
import uuid

from .config import ConfigurationError, validate_config
from .store import atomic_write


ROOT = Path(__file__).resolve().parent.parent


def build_frontend_command(home: Path, config: dict, frontend: str,
                           session_id: str, channels: bool = False) -> list[str]:
    validate_config(config)
    if frontend not in {"claude", "pi"}:
        raise ConfigurationError("Frontend must be claude or pi")
    try:
        if str(uuid.UUID(session_id)) != session_id:
            raise ValueError()
    except (ValueError, AttributeError) as error:
        raise ConfigurationError("Frontend session ID must be a canonical UUID") from error
    role = config["roles"]["orchestrator"]
    provider = {"claude": "anthropic", "codex": "openai-codex"}.get(role["adapter"])
    if provider is None or (frontend == "claude" and role["adapter"] != "claude"):
        raise ConfigurationError("Requested orchestrator provider is incompatible with this frontend")
    if channels and frontend != "claude":
        raise ConfigurationError("Preview channels are available only in Claude")
    home = Path(home).resolve()
    private = home / "data" / "frontend"
    if not private.resolve().is_relative_to(home) or (home / "data").is_symlink() or private.is_symlink():
        raise ConfigurationError("Frontend data directory must remain inside home without symlinks")
    private.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(private, 0o700)
    command = list(config.get("frontends", {}).get(frontend, {}).get("command", [frontend]))
    if frontend == "claude":
        mcp_path = private / f"{session_id}.mcp.json"
        arguments = ["--home", str(home), "mcp", "--session", session_id]
        if channels:
            arguments.append("--channels")
        atomic_write(mcp_path, json.dumps({"mcpServers": {"orchestrator": {
            "command": str(ROOT / "bin" / "orchestrator"), "args": arguments}}}))
        command += ["--model", role["model"], "--effort", role["effort"],
                    "--session-id", session_id, "--settings", str(ROOT / ".claude/settings.json"),
                    "--mcp-config", str(mcp_path), "--strict-mcp-config", "--name", "Orchestrator"]
        if channels:
            command += ["--dangerously-load-development-channels", "server:orchestrator"]
    else:
        session_directory = private / session_id
        if session_directory.is_symlink():
            raise ConfigurationError("Frontend session directory must not be a symlink")
        session_directory.mkdir(exist_ok=True, mode=0o700)
        command += ["--provider", provider, "--model", role["model"], "--thinking", role["effort"],
                    "--session-dir", str(session_directory), "--session-id", session_id,
                    "-e", str(ROOT / ".pi/extensions/orchestrator.ts")]
    return command
