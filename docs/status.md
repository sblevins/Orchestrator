# Project status

## Goal

A fast, project-bound conversational coordinator for Claude Code and Pi, with independent planning and criticism, a slow ongoing monitor, and eventual temporary workers chosen under First Mate's routing guidelines.
The deterministic supervisor owns task state and dependency enforcement.
Plain version: models decide what to do, while ordinary software remembers the work and prevents tasks from starting too early.

## Implemented

The local Python/SQLite foundation supports project/session ownership, notes, durable events and inboxes, graph validation/readiness, planner-to-critic handoff, bounded revisions, monitor review cursors, blocking holds, and explicit operator approval.
Detached resource-reserved runners have deadlines, cancellation, process-identity checks, strict result parsing, and restart reconciliation.
Claude Code hooks/MCP and the owned Pi extension use this same state instead of separate task lists.
All user prompts are saved; conservative exact-match filtering avoids waking expensive review for simple status questions.
Core role models, efforts, resource limits, personalization, and workflow templates are configurable.
The current First Mate reference was pulled and audited at `8f756bbc287c5bdfacc64a7cc09e8516c64fc919`.

## Validation

The 124 passing offline tests exercise real subprocesses with fake model harnesses, including frontend disconnect/reconnect, large prompt transport, native Claude clear/resume identity, private-home hooks, graph constraints, stale scope fencing, and transaction-boundary recovery.
Python lint and formatting are checked with Ruff.
The installed Pi bridge is tested without inference.
These checks do not prove paid model access, answer quality, native microphone behavior, or live Claude idle wakeup.

## Explicitly pending

- First Mate worker router import, configurable dispatch profiles, and selection-authority enforcement.
- Worker launch, steering, implementation isolation, acceptance verification, and graph execution transitions.
- A user decision on third-party Pi orchestration plugins and any shared graph UI.
- Live model/effort acceptance and native Claude voice/wake checks in the user's terminal.
- Workload-specific evaluation of latency, missed decisions, monitoring quality, and costs.
- Optional OS-managed supervisor restart after a crash or reboot.

The current supervisor is single-host, not a distributed Temporal deployment.
Its state survives restart; reconnecting starts the supervisor, but there is no installed boot service.
Unknown work is not blindly replayed.
Claude reports harness costs, whose resumed-session scope still needs live verification; these must not be summed as verified per-turn spending.
Codex dollar cost remains unknown and has no hard dollar cap here.
Read-only harness configuration is not hostile-process isolation against other programs using the same operating-system account.

## Next decision

Try the native frontend and planning lifecycle first.
Then configure First Mate's worker guidelines, keeping plan workers selected by the monitor and unrelated workers selected by the orchestrator.
Workers remain disabled until that separate implementation is complete.
