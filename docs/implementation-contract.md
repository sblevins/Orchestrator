# Implementation contract

> Historical foundation contract.
> Current worker behavior is specified in [worker implementation contract](worker-implementation-contract.md); non-Anthropic specialists now use Pi.

The user wants Pi and Claude Code frontends over one local durable supervisor.
Claude Code is the preferred interactive frontend and must retain its native voice support.
There are four planning roles: orchestrator, planner, critic, and monitor.
During execution, orchestrator and monitor persist as roles; workers are temporary.
Worker routing is deliberately disabled in this release, with empty First Mate-compatible policy configuration reserved for later.
Future plan-related workers are selected by the monitor; unrelated on-demand workers are selected by the orchestrator, both under the same router guidelines.

The first deployment is local and uses Python 3.11+ stdlib SQLite, subprocess adapters, and private filesystem artifacts.
There is one supervisor per Orchestrator home, not one per frontend.
This is not distributed execution and does not claim exactly-once external effects.
Plain version: both interfaces share saved jobs and notes, while a separate local program keeps checking jobs after a chat window closes.

## Module ownership and stable contracts

`orchestrator.config` exports `load_config(home: Path, project_id: str | None = None) -> dict`, `validate_config(config: dict) -> None`, `role_config(config: dict, role: str) -> dict`, and `ConfigurationError(ValueError)`.
Merge tracked `config/default.toml`, optional private `config/local.toml`, then optional private `config/projects/<project_id>.toml` recursively; lists replace rather than append.
All role/model names are strings, not hardcoded model enums; validate actual structure, supported adapter effort values, bounded numbers, tool permissions, and paths.
Runtime-critical keys: `[supervisor]` poll_seconds, heartbeat_seconds, stale_seconds, max_parallel, monitor_interval_seconds, monitor_batch_events, task_timeout_seconds, task_memory, task_cpus, frontend_memory, frontend_cpus; `[personalization]` name, communication_style; `[workers]` enabled=false; `[roles.<role>]` adapter, model, effort, allowed_tools; `[adapters.<name>]` command as an argv list.
Core role defaults: orchestrator Claude Sonnet 5.5 low, planner Claude Opus 5.5 high, critic Codex GPT-6 Astra high, monitor Claude Opus 5.5 high.
The installed Claude CLI supports low, medium, high, xhigh, and max; model-specific availability still requires validation.
`[routing]` is disabled and contains empty rules, with no worker model default.

`orchestrator.store.Store(home: Path)` initializes private `data/state.sqlite3` with foreign keys, WAL, busy_timeout, schema version, transactional writes, and immutable events.
Store owns stable projects, project-bound sessions, plan versions, tasks and dependencies, run attempts, heartbeats, artifact references, review findings, holds, and acknowledged per-session inbox notifications.
No caller declares success just because a process exits; adapters must report a successful terminal result.
Job launch is recorded before a runner starts, and ownership tokens fence stale attempts.
Store APIs enforce these boundaries for all callers.

`orchestrator.adapters` exposes `build_command(config: dict, role: str, prompt: str, cwd: Path, output_path: Path, session_id: str | None = None) -> list[str]` and `parse_result(adapter: str, stdout: str, returncode: int) -> dict` returning `{text, session_id, cost_usd}` or raising `AdapterError`.
Commands never use shell interpolation or silently downgrade a model/effort.
Claude/Codex core specialists must be read-only, noninteractive, and bounded; no permission bypass flags.
This module does not spawn processes or own persistence.

`orchestrator.runtime` owns independent task runners, resource reservations, supervisor singleton, automatic planner-to-critic handoff, coalesced nonrecursive monitor scheduling, deadlines, cancellation, persisted result delivery, and recovery.
A frontend disconnect does not cancel a task runner.
Uncertain running outcomes are surfaced rather than blindly relaunched.

`orchestrator.cli` exposes JSON-returning operations through `bin/orchestrator`, plus human-friendly start/status/doctor commands.
Frontend adapters call the CLI or share a common JSONL MCP server; neither implements a second task registry.
Native hooks display unacknowledged notifications but do not delete them on display.
Explicit acknowledgment follows actual handling.
An unsupported proactive wake mechanism must be reported honestly; never pretend a hook runs while the frontend is idle.

## Acceptance requirements

Test project isolation, immutable session binding, exclusive coordinator ownership, safe read-only observation, dependency gates, stale approvals, duplicate dispatch, stale worker writes, parent restart, worker timeout/cancellation, bounded monitor scheduling, and notification redelivery until acknowledged.
Use real subprocess E2E tests with deterministic fake harness executables before optional paid smoke tests.
Do not launch paid model calls merely to discover whether a CLI exists.
All heavy commands run under machine-resources reservations.
All changes are made in isolated worktrees, locally committed with the configured sblevins identity, then integrated into the requested root checkout.
